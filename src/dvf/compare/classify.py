"""Mismatch classification and fix hints (BUILD_PROMPT.md section 9).

Each differing value pair gets one label. Labels are aggregated per column, and the dominant
labels produce a hint such as "98% of mismatches are whitespace only; consider trim: rtrim".
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from dvf.normalize.canonical import parse_datetime

SENTINELS = frozenset(
    {
        "N/A",
        "NA",
        "NULL",
        "NONE",
        "UNKNOWN",
        "-",
        "?",
        "00000000",
        "0000-00-00",
        "9999-12-31",
        "99991231",
        "1900-01-01",
        "19000101",
        "1970-01-01",
        "#",
        "TBD",
    }
)

LABELS: dict[str, tuple[str, str]] = {
    "whitespace_only": (
        "whitespace only",
        "consider normalization.trim (rtrim for blank-padded CHAR columns) or collapse_whitespace",
    ),
    "case_only": (
        "letter case only",
        "consider normalization.case_sensitive: false or the 'lower' normalizer",
    ),
    "case_and_whitespace": (
        "letter case and whitespace",
        "consider normalization.trim together with case_sensitive: false",
    ),
    "leading_zeros": (
        "leading zeros",
        "consider the 'strip_leading_zeros' normalizer (or 'sap_alpha' for SAP keys)",
    ),
    "numeric_rounding": (
        "numeric rounding",
        "consider tolerance.numeric_abs or the 'round' normalizer, or check the target scale",
    ),
    "precision_truncation": (
        "truncated precision",
        "digits or fractional seconds were cut; check the target precision and scale",
    ),
    "string_truncation": (
        "truncated text (the target is a prefix of the source)",
        "widen the target column; see the length_precision schema check",
    ),
    "timezone_shift": (
        "a whole-hour time shift",
        "check time zone conversion (normalization.timezone or the 'to_utc' normalizer)",
    ),
    "date_format": (
        "a date or time format difference",
        "align the date formats or parse the text into dates",
    ),
    "null_vs_empty": (
        "NULL on one side and empty text on the other",
        "consider normalization.null_equals_empty: true",
    ),
    "null_vs_value": (
        "a value missing on one side",
        "check the mapping and transformation of this column",
    ),
    "sentinel_substitution": (
        "a default or sentinel value substitution",
        "verify the substitution rule (for example NULL to 'N/A') with an explicit check",
    ),
    "encoding": (
        "damaged characters (encoding or code page conversion)",
        "check the character set conversion between source and target (UTF-8, Latin-1, code pages)",
    ),
    "type_coercion": (
        "equal values in a different representation",
        "align the column types or formats on both sides",
    ),
    "value_changed": ("changed values", "inspect the samples; the transformation may be wrong"),
}

_DIGITS = re.compile(r"^\d+$")
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_WS = re.compile(r"\s+")


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _numeric(s: Decimal, t: Decimal) -> str:
    if s == t:
        return "type_coercion"
    for a, b in ((s, t), (t, s)):
        places = _places(b)
        if places >= _places(a):
            continue
        quantum = Decimal(1).scaleb(-places)
        if a.quantize(quantum, rounding=ROUND_HALF_UP) == b:
            return "numeric_rounding"
        if a.quantize(quantum, rounding=ROUND_DOWN) == b:
            return "precision_truncation"
    return "value_changed"


def _temporal(s: datetime, t: datetime, s_text: str, t_text: str) -> str | None:
    if (s.tzinfo is None) != (t.tzinfo is None):
        return None
    delta = t - s
    if delta == timedelta(0):
        return "date_format" if s_text != t_text else "type_coercion"
    quarter = timedelta(minutes=15)
    if (
        abs(delta) <= timedelta(hours=14)
        and delta % quarter == timedelta(0)
        and abs(delta) >= quarter
    ):
        return "timezone_shift"
    for step in (1_000_000, 1_000):
        if s.replace(microsecond=s.microsecond - s.microsecond % step) == t:
            return "precision_truncation"
        if t.replace(microsecond=t.microsecond - t.microsecond % step) == s:
            return "precision_truncation"
    return None


def _mojibake(s: str, t: str) -> bool:
    if "�" in t and "�" not in s:
        return True
    for a, b in ((s, t), (t, s)):
        for codec in ("cp1252", "latin-1"):
            try:
                if a.encode("utf-8").decode(codec) == b:
                    return True
            except UnicodeDecodeError:
                continue
    return False


def classify_pair(source: object, target: object) -> str:
    """Label why ``source`` and ``target`` differ (both are raw values, usually text)."""
    if source is None or target is None:
        present = str(target if source is None else source)
        if present.strip() == "":
            return "null_vs_empty"
        if present.strip().upper() in SENTINELS:
            return "sentinel_substitution"
        return "null_vs_value"
    s, t = str(source), str(target)
    s_sentinel, t_sentinel = s.strip().upper() in SENTINELS, t.strip().upper() in SENTINELS
    if s_sentinel != t_sentinel:
        return "sentinel_substitution"
    if s.strip() == t.strip() or _WS.sub(" ", s).strip() == _WS.sub(" ", t).strip():
        return "whitespace_only"
    if s.lower() == t.lower():
        return "case_only"
    if _WS.sub(" ", s).strip().lower() == _WS.sub(" ", t).strip().lower():
        return "case_and_whitespace"
    if (
        _DIGITS.match(s.strip())
        and _DIGITS.match(t.strip())
        and s.strip().lstrip("0") == t.strip().lstrip("0")
    ):
        return "leading_zeros"
    ds, dt = _decimal(s), _decimal(t)
    if ds is not None and dt is not None:
        return _numeric(ds, dt)
    if _DATE_LIKE.match(s.strip()) and _DATE_LIKE.match(t.strip()):
        try:
            label = _temporal(parse_datetime(s), parse_datetime(t), s, t)
        except ValueError:
            label = None
        if label is not None:
            return label
    if _mojibake(s, t):
        return "encoding"
    if t and len(t.rstrip()) < len(s.rstrip()) and s.startswith(t.rstrip()):
        return "string_truncation"
    return "value_changed"


def summarize(labels: Counter[str], total: int | None = None) -> list[str]:
    """Hints for the dominant labels (those covering at least 10% of classified mismatches)."""
    classified = sum(labels.values())
    if classified == 0:
        return []
    hints: list[str] = []
    for label, count in labels.most_common():
        share = count / classified
        if share < 0.1 and hints:
            break
        description, advice = LABELS.get(label, (label, ""))
        hint = f"{share:.0%} of mismatches are {description}; {advice}"
        if total is not None and classified < total:
            hint += f" (based on {classified:,} of {total:,} mismatches)"
        hints.append(hint)
    return hints
