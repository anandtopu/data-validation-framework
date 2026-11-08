"""NoSQL pack: value classification, canonical JSON, BSON types, and driver value conversion."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from dvf.connectors.cassandra import plain, quote_cql
from dvf.connectors.cosmos import classify_cosmos
from dvf.connectors.dynamodb import classify_dynamodb, from_attribute, item, to_attribute
from dvf.core.errors import ConnectorError
from dvf.packs.nosql.bson_types import extended_json, make_bson_classifier
from dvf.packs.nosql.values import (
    Classified,
    ValueKind,
    canonical_json,
    classify_python,
    json_hash,
    number_text,
    scalar_text,
    timestamp_text,
    unordered_json,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "1"),
        (-0.0, "0"),
        (1.0, "1"),
        (2.5, "2.5"),
        (Decimal("2.50"), "2.5"),
        (Decimal("1E+3"), "1000"),
        (Decimal("-0.000"), "0"),
        (1e-7, "0.0000001"),
        (Decimal("123456789012345678901234567890.12"), "123456789012345678901234567890.12"),
        (float("nan"), None),
        (Decimal("Infinity"), None),
    ],
)
def test_number_text_normalizes(value: object, expected: str | None) -> None:
    assert number_text(value) == expected  # type: ignore[arg-type]


def test_canonical_json_sorts_keys_and_normalizes_values() -> None:
    doc = {
        "b": [1, 2.0, Decimal("3.10")],
        "a": {"z": None, "y": True, "x": "é"},
        "when": datetime(2024, 3, 5, 12, 0, tzinfo=timezone(timedelta(hours=2))),
        "day": date(2024, 3, 5),
        "at": time(1, 2, 3),
        "id": uuid.UUID(int=255),
        "raw": b"\x00\xff",
        "nan": float("nan"),
        1: "int key",
    }
    assert canonical_json(doc) == (
        '{"1":"int key","a":{"x":"é","y":true,"z":null},"at":"01:02:03",'
        '"b":[1,2,3.1],"day":"2024-03-05","id":"00000000-0000-0000-0000-0000000000ff",'
        '"nan":"NaN","raw":"00ff","when":"2024-03-05T10:00:00.000000Z"}'
    )
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2.0, "a": Decimal("1.00")})
    assert json_hash("[]") == json_hash("[]") and len(json_hash("[]")) == 64
    assert unordered_json([{"k": 2}, {"k": 1}], classify_python) == '[{"k":1},{"k":2}]'


def test_classify_python_kinds_and_natives() -> None:
    assert classify_python(None).kind is ValueKind.NULL
    assert classify_python(True) == Classified(ValueKind.BOOL, "bool", True)
    assert classify_python(5).native == "int"
    assert classify_python(2**40).native == "long"
    assert classify_python(Decimal("NaN")).kind is ValueKind.OTHER
    naive = classify_python(datetime(2024, 1, 1, 8))
    assert naive.kind is ValueKind.TIMESTAMP and naive.value.tzinfo is UTC
    assert classify_python({3, 1, 2}) == Classified(ValueKind.ARRAY, "set", [1, 2, 3])
    assert classify_python(memoryview(b"ab")).value == b"ab"
    assert classify_python(object()).kind is ValueKind.OTHER
    assert scalar_text(Classified(ValueKind.TIMESTAMP, "date", datetime(2024, 1, 1))) == (
        "2024-01-01T00:00:00.000000Z"
    )
    assert timestamp_text(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00.000000Z"


def test_bson_classifier_covers_bson_types() -> None:
    import bson

    classify = make_bson_classifier()
    oid = bson.ObjectId("65f1c0de0123456789abcdef")
    cases = {
        "oid": (oid, ValueKind.OBJECT_ID, "objectId", "65f1c0de0123456789abcdef"),
        "int": (7, ValueKind.INT, "int", 7),
        "long": (bson.Int64(7), ValueKind.INT, "long", 7),
        "big": (2**40, ValueKind.INT, "long", 2**40),
        "dec": (bson.Decimal128("9.990"), ValueKind.DECIMAL, "decimal", Decimal("9.990")),
        "nan": (bson.Decimal128("NaN"), ValueKind.OTHER, "decimal", "NaN"),
        "date": (
            datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC),
            ValueKind.TIMESTAMP,
            "date",
            datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC),
        ),
        "uuid": (uuid.UUID(int=5), ValueKind.UUID, "uuid", str(uuid.UUID(int=5))),
        "uuid4": (
            bson.Binary(uuid.UUID(int=6).bytes, 4),
            ValueKind.UUID,
            "uuid",
            str(uuid.UUID(int=6)),
        ),
        "bin": (bson.Binary(b"xy", 0), ValueKind.BINARY, "binData", b"xy"),
        "bytes": (b"xy", ValueKind.BINARY, "binData", b"xy"),
        "ts": (bson.Timestamp(5, 1), ValueKind.OTHER, "timestamp", "Timestamp(5, 1)"),
        "re": (bson.Regex("^a", "im"), ValueKind.OTHER, "regex", "/^a/im"),
        "code": (bson.Code("x()"), ValueKind.STRING, "javascript", "x()"),
        "min": (bson.MinKey(), ValueKind.OTHER, "minKey", "MinKey"),
        "max": (bson.MaxKey(), ValueKind.OTHER, "maxKey", "MaxKey"),
        "bool": (False, ValueKind.BOOL, "bool", False),
        "float": (1.5, ValueKind.FLOAT, "double", 1.5),
    }
    for name, (value, kind, native, plain_value) in cases.items():
        cell = classify(value)
        assert (cell.kind, cell.native, cell.value) == (kind, native, plain_value), name
    ref = classify(bson.DBRef("customers", 3))
    assert ref.kind is ValueKind.OBJECT and ref.native == "dbRef"
    assert canonical_json({"_id": oid, "n": bson.Int64(2)}, classify) == (
        '{"_id":"65f1c0de0123456789abcdef","n":2}'
    )
    from bson.datetime_ms import DatetimeMS

    assert classify(DatetimeMS(-(10**15))).kind is ValueKind.OTHER


def test_extended_json_in_filters() -> None:
    import bson

    parsed = extended_json(
        {
            "_id": {"$oid": "65f1c0de0123456789abcdef"},
            "at": {"$gte": {"$date": "2024-01-01T00:00:00Z"}},
            "total": {"$numberDecimal": "1.50"},
        }
    )
    assert parsed["_id"] == bson.ObjectId("65f1c0de0123456789abcdef")
    assert parsed["at"]["$gte"].year == 2024
    assert parsed["total"] == bson.Decimal128("1.50")


def test_dynamodb_attribute_values_round_trip() -> None:
    attributes = {
        "pk": {"S": "order#1"},
        "n": {"N": "42"},
        "price": {"N": "9.99"},
        "flag": {"BOOL": True},
        "none": {"NULL": True},
        "blob": {"B": b"\x01"},
        "tags": {"SS": ["b", "a"]},
        "scores": {"NS": ["2", "1.5"]},
        "bins": {"BS": [b"\x02", b"\x01"]},
        "meta": {"M": {"k": {"L": [{"S": "x"}, {"N": "1"}]}}},
    }
    doc = item(attributes)
    assert doc["n"] == Decimal(42) and doc["meta"] == {"k": ["x", Decimal(1)]}
    natives = {k: classify_dynamodb(v).native for k, v in doc.items()}
    assert natives == {
        "pk": "S",
        "n": "N",
        "price": "N",
        "flag": "BOOL",
        "none": "NULL",
        "blob": "B",
        "tags": "SS",
        "scores": "NS",
        "bins": "BS",
        "meta": "M",
    }
    assert classify_dynamodb(doc["n"]).kind is ValueKind.INT
    assert classify_dynamodb(doc["price"]).kind is ValueKind.DECIMAL
    assert classify_dynamodb(doc["tags"]).value == ["a", "b"]
    assert to_attribute({"a": [1, 1.5, None, True, "s", b"b"]}) == {
        "M": {
            "a": {
                "L": [
                    {"N": "1"},
                    {"N": "1.5"},
                    {"NULL": True},
                    {"BOOL": True},
                    {"S": "s"},
                    {"B": b"b"},
                ]
            }
        }
    }
    with pytest.raises(ConnectorError):
        from_attribute({"X": 1})
    with pytest.raises(ConnectorError):
        to_attribute(object())


def test_cosmos_and_cassandra_value_conversion() -> None:
    assert classify_cosmos(1).native == "number"
    assert classify_cosmos(1.5).native == "number"
    assert classify_cosmos("x").native == "string"
    assert classify_cosmos({"a": 1}).native == "object"
    assert classify_cosmos(None).native == "null"

    class Date:
        def __init__(self, d: date) -> None:
            self._d = d

        def date(self) -> date:
            return self._d

    class Time:
        def time(self) -> time:
            return time(1, 2, 3)

    class Udt(tuple):  # type: ignore[type-arg]
        _fields = ("street", "city")

        def __new__(cls, *values: object) -> Udt:
            return super().__new__(cls, values)

        @property
        def street(self) -> object:
            return self[0]

        @property
        def city(self) -> object:
            return self[1]

    converted = plain(
        {
            "d": Date(date(2024, 1, 2)),
            "t": Time(),
            "ts": datetime(2024, 1, 1, 10),
            "addr": Udt("Main 1", "Springfield"),
            "tags": {"b", "a"},
            "pair": (1, "x"),
        }
    )
    assert converted["d"] == date(2024, 1, 2)
    assert converted["t"] == time(1, 2, 3)
    assert converted["ts"].tzinfo is UTC
    assert converted["addr"] == {"street": "Main 1", "city": "Springfield"}
    assert converted["tags"] == {"a", "b"}
    assert converted["pair"] == [1, "x"]
    assert quote_cql("orders") == "orders"
    assert quote_cql("Orders") == '"Orders"'
