"""Redaction of secret values from text and structured data.

Every resolved secret is added to a redactor. Logging, ``dvf config show``, and reports pass their
output through it, so secret values never leave the process in clear text.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any

MASK = "***"
_MIN_SECRET_LENGTH = 4

_SENSITIVE_KEY_RE = re.compile(
    r"pass(word|wd|phrase)?|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|"
    r"credential|webhook|connection[_-]?string|auth[_-]?key",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^:/@\s]+:)(?P<secret>[^@\s]+)(?P<at>@)"
)


def is_sensitive_key(key: str) -> bool:
    """True if a field name suggests it holds a credential."""
    return bool(_SENSITIVE_KEY_RE.search(key))


class Redactor:
    """Masks registered secret values and credential-like fields."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def add(self, value: str | None) -> None:
        """Register a secret value. Values shorter than 4 characters are ignored to avoid noise."""
        if value and len(value) >= _MIN_SECRET_LENGTH:
            with self._lock:
                self._values.add(value)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def __contains__(self, value: object) -> bool:
        return value in self._values

    def redact_text(self, text: str) -> str:
        """Mask registered secret values and passwords embedded in URLs."""
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, MASK)
        return _URL_USERINFO_RE.sub(lambda m: f"{m['prefix']}{MASK}{m['at']}", text)

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
