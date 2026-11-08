"""BSON value classification for the MongoDB connector.

``pymongo`` decodes BSON into Python values plus a few ``bson`` classes. This module maps every
BSON type to a :class:`~dvf.packs.nosql.values.ValueKind` and keeps MongoDB's own type names
(the ones ``$type`` uses) for schema checks:

=============== ============ =================================================================
BSON type       Native name  Staged as
=============== ============ =================================================================
ObjectId        objectId     24-character lower-case hexadecimal string
32-bit integer  int          64-bit integer
64-bit integer  long         64-bit integer
double          double       double
Decimal128      decimal      decimal (largest scale seen); NaN and infinities as text
date            date         UTC timestamp (millisecond precision); out-of-range dates as text
UUID (binary 4) uuid         canonical UUID string
other binary    binData      binary
string          string       string
boolean         bool         boolean
document        object       flattened, or canonical JSON
array           array        an array strategy (canonical JSON by default)
timestamp       timestamp    text ``Timestamp(<seconds>, <increment>)``
regex           regex        text ``/<pattern>/<flags>``
JavaScript      javascript   string (the code)
DBRef           dbRef        document with ``$ref``, ``$id``, and ``$db``
MinKey, MaxKey  minKey, ...  text
=============== ============ =================================================================

``bson`` is imported only when the classifier is built (by an open MongoDB connection).
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from dvf.core.errors import DependencyError
from dvf.packs.nosql.values import (
    INT32_MAX,
    INT32_MIN,
    Classified,
    Classifier,
    ValueKind,
    classify_python,
    to_utc,
)

_REGEX_FLAGS = (("i", 2), ("l", 4), ("m", 8), ("s", 16), ("u", 32), ("x", 64))


def import_bson() -> Any:
    try:
        return importlib.import_module("bson")
    except ImportError as exc:
        raise DependencyError("pymongo", "mongodb", "The MongoDB connector") from exc


def make_bson_classifier() -> Classifier:
    """A classifier for values decoded by ``pymongo``."""
    bson = import_bson()
    object_id = bson.ObjectId
    decimal128 = bson.Decimal128
    int64 = bson.Int64
    binary = bson.Binary
    timestamp = bson.Timestamp
    regex = bson.Regex
    code = bson.Code
    min_key = bson.MinKey
    max_key = bson.MaxKey
    dbref = bson.DBRef
    datetime_ms = getattr(importlib.import_module("bson.datetime_ms"), "DatetimeMS", None)

    def classify(value: Any) -> Classified:
        if value is None:
            return Classified(ValueKind.NULL, "null", None)
        if isinstance(value, bool):
            return Classified(ValueKind.BOOL, "bool", value)
        if isinstance(value, int64):
            return Classified(ValueKind.INT, "long", int(value))
        if isinstance(value, int):
            native = "int" if INT32_MIN <= value <= INT32_MAX else "long"
            return Classified(ValueKind.INT, native, int(value))
        if isinstance(value, float):
            return Classified(ValueKind.FLOAT, "double", value)
        if isinstance(value, code):  # a str subclass
            return Classified(ValueKind.STRING, "javascript", str(value))
        if isinstance(value, str):
            return Classified(ValueKind.STRING, "string", value)
        if isinstance(value, Mapping):
            return Classified(ValueKind.OBJECT, "object", value)
        if isinstance(value, list | tuple):
            return Classified(ValueKind.ARRAY, "array", list(value))
        if isinstance(value, object_id):
            return Classified(ValueKind.OBJECT_ID, "objectId", str(value))
        if isinstance(value, decimal128):
            number = value.to_decimal()
            if not number.is_finite():
                return Classified(ValueKind.OTHER, "decimal", str(number))
            return Classified(ValueKind.DECIMAL, "decimal", number)
        if isinstance(value, datetime):
            return Classified(ValueKind.TIMESTAMP, "date", to_utc(value))
        if datetime_ms is not None and isinstance(value, datetime_ms):
            # A date outside Python's range (years 1 to 9999).
            return Classified(ValueKind.OTHER, "date", f"{int(value)} ms since 1970-01-01")
        if isinstance(value, uuid.UUID):
            return Classified(ValueKind.UUID, "uuid", str(value))
        if isinstance(value, binary):
            if value.subtype == 4 and len(value) == 16:
                return Classified(ValueKind.UUID, "uuid", str(uuid.UUID(bytes=bytes(value))))
            return Classified(ValueKind.BINARY, "binData", bytes(value))
        if isinstance(value, bytes | bytearray):
            return Classified(ValueKind.BINARY, "binData", bytes(value))
        if isinstance(value, timestamp):
            return Classified(ValueKind.OTHER, "timestamp", f"Timestamp({value.time}, {value.inc})")
        if isinstance(value, regex):
            flags = value.flags if isinstance(value.flags, int) else 0
            letters = "".join(letter for letter, bit in _REGEX_FLAGS if flags & bit)
            return Classified(ValueKind.OTHER, "regex", f"/{value.pattern}/{letters}")
        if isinstance(value, dbref):
            return Classified(ValueKind.OBJECT, "dbRef", dict(value.as_doc()))
        if isinstance(value, min_key):
            return Classified(ValueKind.OTHER, "minKey", "MinKey")
        if isinstance(value, max_key):
            return Classified(ValueKind.OTHER, "maxKey", "MaxKey")
        return classify_python(value)

    return classify


def extended_json(value: Any) -> Any:
    """Convert MongoDB Extended JSON in configuration (``{"$oid": ...}``, ``{"$date": ...}``,
    ``{"$numberDecimal": ...}``) to BSON values, so filters and pipelines can name them."""
    import json

    json_util = importlib.import_module("bson.json_util")
    return json_util.loads(json.dumps(value, default=str))


__all__ = ["extended_json", "import_bson", "make_bson_classifier"]
