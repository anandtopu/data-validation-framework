"""Protection of personal data in samples, profiles, and results (``defaults.privacy``).

* **Column tagging.** Columns named in ``privacy.pii_columns`` (glob patterns) or mapped with
  ``pii: true`` are personal data: their sample values are always protected.
* **Pattern detection.** Other text values that look like an email address, a phone number, a
  national identifier (US SSN, UK NINO, Aadhaar), a payment card number (Luhn-valid), or an IBAN are
  protected too (``privacy.detect_patterns``).
* **Masking modes** (``privacy.masking``): ``mask`` replaces values with ``***`` (detected values
  keep a hint such as ``j***@example.com`` or the last digits), ``hash`` writes a keyed HMAC-SHA256
  digest (``hash:...``; equal values give equal digests, stable across runs when
  ``privacy.hash_key`` is set), and ``tokenize`` writes a token such as ``tok_000042`` that is
  consistent within a run and never stored.
* **Strict mode** (``privacy.mode: strict``): no raw value leaves the process. Samples and extracts
  are not captured, literal values are removed from recorded SQL (results, reports, repro files),
  minimum and maximum values are hidden, and non-numeric observed values are masked.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from dvf.security.redaction import MASK

if TYPE_CHECKING:
    from dvf.config.models import PrivacyConfig
    from dvf.models.results import CheckResult, ColumnProfile

MASKING_MODES = ("mask", "hash", "tokenize")
_MAX_TEXT = 500

_EMAIL = re.compile(r"^([^@\s])[^@\s]*(@[^@\s]+\.[^@\s]+)$")
_PHONE_INTL = re.compile(r"^\+[1-9][0-9()\s.\-]{5,20}[0-9]$")
_PHONE_LOCAL = re.compile(r"^(?:\(\d{2,5}\)\s?|\d{2,5}[\s.\-])\d{2,4}(?:[\s.\-]\d{2,4}){1,3}$")
_NOT_PHONE = re.compile(
    r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$"  # dates
    r"|^\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?$"  # grouped numbers
    r"|^\d{3}-\d{2}-\d{4}$"  # the shape of a US SSN
)
_SSN = re.compile(r"^(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}$")
_NINO = re.compile(r"^[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]$", re.I)
_AADHAAR = re.compile(r"^[2-9]\d{3}\s\d{4}\s\d{4}$")
_IBAN = re.compile(r"^[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}$")
_CARD = re.compile(r"^\d(?:[\s-]?\d){12,18}$")

_PROCESS_KEY = secrets.token_bytes(32)
_tokens: dict[bytes, int] = {}
_tokens_lock = threading.Lock()


def detect_pii(text: str) -> str | None:
    """The kind of personal data ``text`` looks like (``email``, ``phone``, ``national_id``,
    ``payment_card``, ``iban``), or ``None``."""
    value = text.strip()
    if len(value) < 6 or len(value) > 254:
        return None
    if "@" in value:
        return "email" if _EMAIL.match(value) else None
    if _SSN.match(value) or _NINO.match(value) or _AADHAAR.match(value):
        return "national_id"
    if _CARD.match(value) and _luhn(re.sub(r"\D", "", value)):
        return "payment_card"
    if _IBAN.match(value.upper()) and _iban_valid(value):
        return "iban"
    phone_like = bool(_PHONE_INTL.match(value) or _PHONE_LOCAL.match(value))
    if phone_like and not _NOT_PHONE.match(value) and 7 <= len(re.sub(r"\D", "", value)) <= 15:
        return "phone"
    return None


def _luhn(digits: str) -> bool:
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_valid(text: str) -> bool:
    compact = re.sub(r"\s", "", text).upper()
    rearranged = compact[4:] + compact[:4]
    try:
        number = int("".join(str(int(ch, 36)) for ch in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


def _partial(text: str, kind: str | None) -> str:
    """The ``mask`` form of a value: ``***``, keeping a non-identifying hint for detected kinds."""
    if kind == "email":
        match = _EMAIL.match(text.strip())
        if match:
            return f"{match.group(1)}***{match.group(2)}"
    digits = re.sub(r"\D", "", text)
    if kind == "phone" and len(digits) >= 7:
        return f"{MASK}{digits[-2:]}"
    if kind in ("payment_card", "iban") and len(text) >= 8:
        compact = re.sub(r"\s", "", text)
        return f"{MASK}{compact[-4:]}"
    return MASK


@dataclass(frozen=True)
class Privacy:
    """Applies privacy settings to sample values."""

    mode: str = "standard"
    mask_pii: bool = True
    pii_columns: tuple[str, ...] = field(default_factory=tuple)
    masking: str = "mask"
    """``mask``, ``hash``, or ``tokenize``."""
    detect_patterns: bool = True
    """Protect text values that look like emails, phone numbers, national IDs, cards, IBANs."""
    hash_key: bytes | None = field(default=None, repr=False)
    """Key for ``hash`` mode; without one a random key per process is used."""

    @classmethod
    def from_config(cls, config: PrivacyConfig, extra_columns: Iterable[str] = ()) -> Privacy:
        hash_key = getattr(config, "hash_key", None)
        key_text = hash_key.get_secret_value() if hash_key is not None else None
        return cls(
            mode=config.mode,
            mask_pii=config.mask_pii,
            pii_columns=(*config.pii_columns, *extra_columns),
            masking=str(getattr(config, "masking", "mask")),
            detect_patterns=bool(getattr(config, "detect_patterns", True)),
            hash_key=key_text.encode("utf-8") if key_text else None,
        )

    @property
    def strict(self) -> bool:
        """In strict mode no raw value leaves the process: samples are dropped."""
        return self.mode == "strict"

    def is_pii(self, column: str) -> bool:
        lowered = column.lower()
        return any(fnmatchcase(lowered, p.lower()) for p in self.pii_columns)

    def protect(self, value: Any, kind: str | None = None) -> Any:
        """Protect one value with the configured masking mode (``kind`` is a detected pattern)."""
        if value is None:
            return None
        text = str(to_json_safe(value))
        if self.masking == "hash":
            digest = hmac.new(self.hash_key or _PROCESS_KEY, text.encode("utf-8"), hashlib.sha256)
            return "hash:" + digest.hexdigest()[:16]
        if self.masking == "tokenize":
            return tokenize(text)
        return _partial(text, kind)

    def mask(self, column: str, value: Any) -> Any:
        """Return a JSON-safe, masked-as-needed version of ``value`` for ``column``."""
        if value is None:
            return None
        if self.mask_pii and self.is_pii(column):
            return self.protect(value)
        plain = to_json_safe(value)
        if self.mask_pii and self.detect_patterns and isinstance(plain, str):
            kind = detect_pii(plain)
            if kind is not None:
                return self.protect(plain, kind)
        return plain

    def mask_rows(self, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
        return [[self.mask(c, v) for c, v in zip(columns, row, strict=True)] for row in rows]


def tokenize(text: str) -> str:
    """A token for ``text`` that is the same for equal values within this process (one run)."""
    digest = hmac.new(_PROCESS_KEY, text.encode("utf-8"), hashlib.sha256).digest()
    with _tokens_lock:
        number = _tokens.get(digest)
        if number is None:
            number = len(_tokens) + 1
            _tokens[digest] = number
    return f"tok_{number:06d}"


def to_json_safe(value: Any) -> Any:
    """Convert a database value into something JSON can hold, shortening very long text."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value)[:64].hex()
    text = str(value)
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"


# ====================================================================== strict mode

_SQL_TOKEN = re.compile(
    r"(?P<string>(?:[NnEeXxBbUu]&?)?'(?:[^']|'')*')"
    r'|(?P<ident>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]\n]*\])'
    r"|(?P<comment>--[^\n]*|/\*.*?\*/)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$#]*)"
    r"|(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<other>.)",
    re.DOTALL,
)
_COMPARISON_TAIL = re.compile(r"(?:=|<>|!=|<|>|<=|>=)\s*-?$")


def scrub_sql_literals(sql: str) -> str:
    """Replace literal values in SQL with ``'***'`` or ``0``.

    String literals are always replaced; numbers are replaced inside ``IN (...)`` lists and after
    comparison operators, where generated SQL carries key values and partition bounds. Quoted
    identifiers and comments are kept. The result is for reading, not for running.
    """
    out: list[str] = []
    parens: list[bool] = []  # True for an IN (...) list
    last = ""  # the last significant token, upper-cased
    for match in _SQL_TOKEN.finditer(sql):
        kind, text = match.lastgroup, match.group(0)
        if kind == "string":
            out.append("'***'")
            last = "'"
        elif kind == "number":
            in_list = bool(parens) and parens[-1]
            hide = in_list or _COMPARISON_TAIL.search("".join(out[-3:])) is not None
            out.append("0" if hide else text)
            last = "0"
        elif kind in ("word", "ident"):
            out.append(text)
            last = text.upper()
        elif kind == "comment":
            out.append(text)
        else:
            if text == "(":
                parens.append(last == "IN")
            elif text == ")" and parens:
                parens.pop()
            out.append(text)
            if not text.isspace():
                last = text
    return "".join(out)


def _numeric(value: Any) -> bool:
    return value is None or isinstance(value, bool | int | float)


def hide_profile(profile: ColumnProfile | None) -> ColumnProfile | None:
    """``profile`` with its minimum and maximum values masked."""
    if profile is None:
        return None
    return profile.model_copy(
        update={
            "min": None if profile.min is None else MASK,
            "max": None if profile.max is None else MASK,
        }
    )


def hide_profile_differences(differences: Iterable[str]) -> list[str]:
    """Profile difference texts without the raw minimum and maximum values."""
    hidden: list[str] = []
    for text in differences:
        name = text.split(":", 1)[0]
        hidden.append(f"{name}: differs (values hidden)" if name in ("min", "max") else text)
    return hidden


def strict_check_result(result: CheckResult) -> CheckResult:
    """``result`` with every raw value removed, for ``privacy.mode: strict``."""
    updates: dict[str, Any] = {}
    if result.samples is not None:
        updates["samples"] = None
    if result.queries:
        updates["queries"] = [
            q.model_copy(
                update={
                    "sql": scrub_sql_literals(q.sql),
                    "parameters": {k: v for k, v in q.parameters.items() if k == "attempts"},
                }
            )
            for q in result.queries
        ]
    if not _numeric(result.observed):
        updates["observed"] = MASK
        message = result.message or ""
        for form in (repr(result.observed), str(result.observed)):
            if len(form) >= 2 and form in message:
                message = message.replace(form, MASK)
        if message != (result.message or ""):
            updates["message"] = message
    if any(m.metric in ("min", "max") for m in result.metrics):
        updates["metrics"] = [
            m.model_copy(
                update={
                    "source": None if m.source is None else MASK,
                    "target": None if m.target is None else MASK,
                }
            )
            if m.metric in ("min", "max")
            else m
            for m in result.metrics
        ]
    if any(c.samples is not None for c in result.column_mismatches):
        updates["column_mismatches"] = [
            c.model_copy(update={"samples": None}) for c in result.column_mismatches
        ]
    return result.model_copy(update=updates) if updates else result
