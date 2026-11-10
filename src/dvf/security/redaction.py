"""Redaction of secret values from text and structured data.

Every resolved secret is added to a redactor. Logging, ``dvf config show``, and reports pass their
output through it, so secret values never leave the process in clear text.

Besides registered values, :meth:`Redactor.redact_text` masks credentials recognizable by shape:
passwords in URLs (``scheme://user:password@host``), credential query parameters (``?sig=...``,
``&access_token=...``), ``PWD=...`` and ``Password=...`` in connection strings, ``Authorization``
headers, and the secret path of Slack, Teams, and Discord webhook URLs.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, quote_plus

MASK = "***"
_MIN_SECRET_LENGTH = 4

_SENSITIVE_KEY_RE = re.compile(
    r"pass(word|wd|phrase)?|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|"
    r"credential|webhook|connection[_-]?string|auth[_-]?key|^pwd$",
    re.IGNORECASE,
)
# scheme://user:password@host — the password may contain '@' (greedy up to the last '@' of the
# authority) and the user may be empty (redis://:password@host).
_URL_USERINFO_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^:/@\s]*:)(?P<secret>[^\s/?#]+)(?P<at>@)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&](?:password|passwd|pwd|secret|client_secret|token|access_token|"
    r"refresh_token|id_token|api[_-]?key|apikey|sig|signature|private_key|"
    r"x-amz-signature|x-amz-security-token|x-goog-signature)=)(?P<secret>[^&#\s\"']+)",
    re.IGNORECASE,
)
_CONN_STRING_SECRET_RE = re.compile(
    r"(?P<prefix>\b(?:password|passwd|pwd)\s*=\s*)"
    r"(?P<secret>\{(?:[^}]|\}\})*\}|'[^']*'|\"[^\"]*\"|[^;\s,&]+)",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"(?P<prefix>\b(?:authorization|x-vault-token|x-api-key)[\"']?\s*[:=]\s*[\"']?"
    r"(?:(?:bearer|basic|token)\s+)?)(?P<secret>[^\s\"',;]+)",
    re.IGNORECASE,
)
_WEBHOOK_RE = re.compile(
    r"(?P<prefix>https://(?:hooks\.slack\.com/(?:services|workflows|triggers)/|"
    r"[A-Za-z0-9.\-]*webhook\.office\.com/|"
    r"(?:discord|discordapp)\.com/api/webhooks/))(?P<secret>[^\s\"'<>]+)",
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    """True if a field name suggests it holds a credential."""
    return bool(_SENSITIVE_KEY_RE.search(key))


def redact_patterns(text: str) -> str:
    """Mask credentials recognizable by shape (URL passwords, query tokens, webhook paths)."""
    if not text:
        return text
    text = _URL_USERINFO_RE.sub(_keep_prefix, text)
    if "=" in text:
        text = _QUERY_SECRET_RE.sub(_keep_prefix, text)
        text = _CONN_STRING_SECRET_RE.sub(_keep_prefix, text)
    lowered = text.lower()
    if "authorization" in lowered or "x-vault-token" in lowered or "x-api-key" in lowered:
        text = _AUTH_HEADER_RE.sub(_keep_prefix, text)
    if "https://" in lowered:
        text = _WEBHOOK_RE.sub(_keep_prefix, text)
    return text


def redact_args(args: list[str] | tuple[str, ...], redactor: Redactor | None = None) -> list[str]:
    """Command-line arguments with secrets masked, including ``--set path.password=value``.

    A ``name=value`` argument whose last name segment looks like a credential has its value
    masked, unless the value is an unresolved ``${secret:...}`` reference.
    """
    redact = (redactor or global_redactor()).redact_text
    out: list[str] = []
    for arg in args:
        flag = ""
        if arg.startswith("-") and arg.count("=") >= 2:  # --set=path.password=value
            flag, _, arg = arg.partition("=")
            flag += "="
        name, sep, value = arg.partition("=")
        last = name.lstrip("-").replace("__", ".").rsplit(".", 1)[-1]
        if sep and value and is_sensitive_key(last):
            shown = value if value.startswith("${secret:") and value.endswith("}") else MASK
            out.append(f"{flag}{name}={shown}")
        else:
            out.append(flag + redact(arg))
    return out


def _keep_prefix(match: re.Match[str]) -> str:
    secret = match["secret"]
    if secret == MASK:
        return match.group(0)
    suffix = match["at"] if "at" in match.groupdict() and match["at"] else ""
    return f"{match['prefix']}{MASK}{suffix}"


def _variants(value: str) -> set[str]:
    """The forms a secret takes when embedded in URLs or JSON text."""
    forms = {value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]}
    return {f for f in forms if len(f) >= _MIN_SECRET_LENGTH}


class Redactor:
    """Masks registered secret values and credential-like fields."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._ordered: list[str] = []
        self._lock = threading.Lock()

    def add(self, value: str | None) -> None:
        """Register a secret value, with its URL-encoded and JSON-escaped forms.

        Values shorter than 4 characters are ignored to avoid masking ordinary text.
        """
        if not value or len(value) < _MIN_SECRET_LENGTH:
            return
        with self._lock:
            new = _variants(value) - self._values
            if new:
                self._values |= new
                self._ordered = sorted(self._values, key=len, reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._ordered = []

    def __contains__(self, value: object) -> bool:
        return value in self._values

    def redact_text(self, text: str) -> str:
        """Mask registered secret values and credentials recognizable by shape."""
        with self._lock:
            values = self._ordered
        for value in values:
            if value in text:
                text = text.replace(value, MASK)
        return redact_patterns(text)

    def redact(self, obj: Any, key: str | None = None) -> Any:
        """Return a copy of ``obj`` with secrets masked.

        Values under credential-like keys are masked entirely, except unresolved
        ``${secret:...}`` references, which are safe and more useful to show.
        """
        if isinstance(obj, Mapping):
            return {k: self.redact(v, str(k)) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.redact(v, key) for v in obj]
        if isinstance(obj, str):
            if (
                key is not None
                and is_sensitive_key(key)
                and obj
                and not obj.startswith("${secret:")
            ):
                return MASK
            return self.redact_text(obj)
        return obj


_global_redactor = Redactor()


def global_redactor() -> Redactor:
    """The process-wide redactor used by logging and the CLI."""
    return _global_redactor
