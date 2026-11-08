"""Value classification and canonical JSON for document and wide-column stores.

Every value read from a NoSQL system is *classified* once: it gets a :class:`ValueKind` (what
dvf does with it), the source system's own type name (``objectId``, ``long``, ``N``, ``SS``,
``number``, ...), and a plain Python value. Connectors supply a classifier for their driver's
types; :func:`classify_python` handles ordinary Python values and is the fallback for all of them.

Canonical JSON (:func:`canonical_json`) is the comparable text form of nested values (documents,
arrays, sets): object keys sorted, no insignificant whitespace, numbers normalized (``1``,
``1.0``, and ``Decimal("1.00")`` are all ``1``), timestamps in UTC, binary values as lower-case
hexadecimal, and ObjectId and UUID values as their canonical strings.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


class ValueKind(StrEnum):
    """What dvf does with a value, independent of the source system's type name."""

    NULL = "null"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    DECIMAL = "decimal"
    STRING = "string"
    TIMESTAMP = "timestamp"
    DATE = "date"
    TIME = "time"
    BINARY = "binary"
    UUID = "uuid"
    OBJECT_ID = "object_id"
    OBJECT = "object"
    ARRAY = "array"
    OTHER = "other"


NUMERIC_KINDS = frozenset({ValueKind.INT, ValueKind.FLOAT, ValueKind.DECIMAL})


@dataclass(frozen=True, slots=True)
class Classified:
    """A classified value.

    ``value`` is a plain Python value: ``bool``, ``int``, ``float``, finite ``Decimal``, ``str``,
    timezone-aware UTC ``datetime``, ``date``, ``time``, ``bytes``, a ``Mapping`` (objects), a
    ``list`` (arrays), or ``None``. ObjectId and UUID values are their canonical strings, and
    values of kind ``other`` are text.
    """

    kind: ValueKind
    native: str
    value: Any


Classifier = Callable[[Any], Classified]
"""Turns a driver value into a :class:`Classified` value."""


def to_utc(value: datetime) -> datetime:
    """A timezone-aware UTC datetime (naive values are taken to be UTC already)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def classify_python(value: Any) -> Classified:
    """Classify a plain Python value (JSON documents, DuckDB rows, and driver fallbacks)."""
    if value is None:
        return Classified(ValueKind.NULL, "null", None)
    if isinstance(value, bool):
        return Classified(ValueKind.BOOL, "bool", value)
    if isinstance(value, int):
        native = "int" if INT32_MIN <= value <= INT32_MAX else "long"
        return Classified(ValueKind.INT, native, int(value))
    if isinstance(value, float):
        return Classified(ValueKind.FLOAT, "double", value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return Classified(ValueKind.OTHER, "decimal", str(value))
        return Classified(ValueKind.DECIMAL, "decimal", value)
    if isinstance(value, str):
        return Classified(ValueKind.STRING, "string", value)
    if isinstance(value, datetime):
        return Classified(ValueKind.TIMESTAMP, "timestamp", to_utc(value))
    if isinstance(value, date):
        return Classified(ValueKind.DATE, "date", value)
    if isinstance(value, time):
        return Classified(ValueKind.TIME, "time", value.replace(tzinfo=None))
    if isinstance(value, bytes | bytearray | memoryview):
        return Classified(ValueKind.BINARY, "binary", bytes(value))
    if isinstance(value, uuid.UUID):
        return Classified(ValueKind.UUID, "uuid", str(value))
    if isinstance(value, Mapping):
        return Classified(ValueKind.OBJECT, "object", value)
    if isinstance(value, list | tuple):
        return Classified(ValueKind.ARRAY, "array", list(value))
    if isinstance(value, set | frozenset):
        return Classified(ValueKind.ARRAY, "set", sorted_set(value, classify_python))
    return Classified(ValueKind.OTHER, type(value).__name__, str(value))


def sorted_set(values: Any, classify: Classifier) -> list[Any]:
    """The elements of a set in a stable order (by their canonical JSON)."""
    return sorted(values, key=lambda v: canonical_json(v, classify))


# ====================================================================== canonical text


def number_text(value: int | float | Decimal) -> str | None:
    """The normalized text of a number: no exponent, no trailing zeros, never ``-0``.

    ``None`` for NaN and infinities, which have no number form in JSON.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        number = Decimal(repr(value))
    else:
        if not value.is_finite():
            return None
        number = value
    if number == 0:
        return "0"
    # Strip trailing fractional zeros without Decimal.normalize(), which rounds to the context's
    # 28 digits.
    sign, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)
    kept = list(digits)
    while exponent < 0 and len(kept) > 1 and kept[-1] == 0:
        kept.pop()
        exponent += 1
    return format(Decimal((sign, tuple(kept), exponent)), "f")


def _non_finite_text(value: float | Decimal) -> str:
    if isinstance(value, float):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if value.is_nan():
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def timestamp_text(value: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffffZ`` in UTC."""
    return to_utc(value).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def scalar_text(cell: Classified) -> str | None:
    """The canonical text of a classified value (canonical JSON for objects and arrays)."""
    kind, value = cell.kind, cell.value
    if kind is ValueKind.NULL:
        return None
    if kind is ValueKind.BOOL:
        return "true" if value else "false"
    if kind in NUMERIC_KINDS:
        text = number_text(value)
        return text if text is not None else _non_finite_text(value)
    if kind is ValueKind.TIMESTAMP:
        return timestamp_text(value)
    if kind in (ValueKind.DATE, ValueKind.TIME):
        return str(value.isoformat())
    if kind is ValueKind.BINARY:
        return bytes(value).hex()
    if kind in (ValueKind.OBJECT, ValueKind.ARRAY):
        return value if isinstance(value, str) else canonical_json(value)
    return str(value)


def canonical_json(value: Any, classify: Classifier = classify_python) -> str:
    """Canonical JSON text of ``value`` (see the module documentation for the rules)."""
    parts: list[str] = []
    _write(value, classify, parts)
    return "".join(parts)


def _write(value: Any, classify: Classifier, out: list[str]) -> None:
    cell = value if isinstance(value, Classified) else classify(value)
    kind = cell.kind
    if kind is ValueKind.NULL:
        out.append("null")
    elif kind is ValueKind.BOOL:
        out.append("true" if cell.value else "false")
    elif kind in NUMERIC_KINDS:
        text = number_text(cell.value)
        out.append(text if text is not None else _quote(_non_finite_text(cell.value)))
    elif kind is ValueKind.OBJECT:
        items = sorted(
            ((_key_text(k, classify), v) for k, v in cell.value.items()), key=lambda kv: kv[0]
        )
        out.append("{")
        for i, (key, item) in enumerate(items):
            if i:
                out.append(",")
            out.append(_quote(key))
            out.append(":")
            _write(item, classify, out)
        out.append("}")
    elif kind is ValueKind.ARRAY:
        out.append("[")
        for i, item in enumerate(cell.value):
            if i:
                out.append(",")
            _write(item, classify, out)
        out.append("]")
    else:
        out.append(_quote(scalar_text(cell) or ""))


def _key_text(key: Any, classify: Classifier) -> str:
    if isinstance(key, str):
        return key
    return scalar_text(classify(key)) or "null"


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def json_hash(text: str) -> str:
    """SHA-256 (hexadecimal) of canonical JSON text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unordered_json(values: list[Any], classify: Classifier) -> str:
    """Canonical JSON of an array whose order does not matter (elements sorted)."""
    elements = sorted(canonical_json(v, classify) for v in values)
    return "[" + ",".join(elements) + "]"


__all__ = [
    "NUMERIC_KINDS",
    "Classified",
    "Classifier",
    "ValueKind",
    "canonical_json",
    "classify_python",
    "json_hash",
    "number_text",
    "scalar_text",
    "sorted_set",
    "timestamp_text",
    "to_utc",
    "unordered_json",
]
