"""NoSQL pack: dataset options, flattening, unwinding, array strategies, and type unification."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, LogicalType
from dvf.packs.nosql.flatten import MISSING, DocumentShaper, resolve_path
from dvf.packs.nosql.options import DocumentOptions, parse_options
from dvf.packs.nosql.staging import RowStager, TypeHints, force_cell
from dvf.packs.nosql.values import Classified, ValueKind, classify_python

ORDER = {
    "_id": "o1",
    "order_id": 1001,
    "customer": {"name": "Ada", "address": {"city": "Leeds", "zip": "LS1"}},
    "tags": ["b", "a"],
    "lines": [
        {"line_no": 1, "sku": "A", "qty": 2, "serials": ["x", "y"]},
        {"line_no": 2, "sku": "B", "qty": 1},
    ],
}


def _options(**options: Any) -> DocumentOptions:
    return parse_options(DocumentOptions, options, connection="c", dataset="orders")


def _rows(doc: dict[str, Any], **options: Any) -> list[dict[str, Any]]:
    shaper = DocumentShaper(_options(**options), classify_python, default_keep=("_id",))
    return [{k: c.value for k, c in row.items()} for row in shaper.rows(doc)]


def test_resolve_path_follows_mongodb_semantics() -> None:
    assert resolve_path(ORDER, ["customer", "address", "city"]) == "Leeds"
    assert resolve_path(ORDER, ["lines", "1", "sku"]) == "B"
    assert resolve_path(ORDER, ["lines", "sku"]) == ["A", "B"]
    assert resolve_path(ORDER, ["lines", "serials"]) == [["x", "y"]]
    assert resolve_path(ORDER, ["lines", "9"]) is MISSING
    assert resolve_path(ORDER, ["nope"]) is MISSING
    assert resolve_path(ORDER, ["order_id", "x"]) is MISSING


def test_auto_flatten_names_columns_by_dot_path() -> None:
    (row,) = _rows(ORDER)
    assert row["customer.address.city"] == "Leeds"
    assert row["tags"] == '["b","a"]'
    assert row["lines"].startswith('[{"line_no":1,"qty":2')
    (row,) = _rows(ORDER, separator="_", flatten=True, max_depth=2)
    assert row["customer_name"] == "Ada"
    assert row["customer_address"] == '{"city":"Leeds","zip":"LS1"}'
    (row,) = _rows(ORDER, flatten=False)
    assert row["customer"] == '{"address":{"city":"Leeds","zip":"LS1"},"name":"Ada"}'


def test_array_strategies() -> None:
    (row,) = _rows(
        ORDER,
        arrays={"tags": "unordered_hash", "lines": "index"},
        index_limit=1,
        default_array="count",
    )
    (same,) = _rows({**ORDER, "tags": ["a", "b"]}, arrays={"tags": "unordered_hash"})
    assert row["tags"] == same["tags"] and len(row["tags"]) == 64
    assert row["lines.0.sku"] == "A"
    assert row["lines.0.serials"] == 2  # nested arrays use default_array (count)
    assert "lines.1.sku" not in row
    (hashed,) = _rows(ORDER, arrays={"tags": "hash"})
    (other,) = _rows({**ORDER, "tags": ["a", "b"]}, arrays={"tags": "hash"})
    assert hashed["tags"] != other["tags"]
    shaper = DocumentShaper(_options(arrays={"lines": "index"}, index_limit=1), classify_python)
    list(shaper.rows(ORDER))
    assert "more than 1 elements" in shaper.finish_notes()[0]


def test_unwind_produces_one_row_per_element() -> None:
    rows = _rows(ORDER, unwind={"path": "lines", "keep": ["order_id", "customer.name"]})
    assert [r["line_no"] for r in rows] == [1, 2]
    assert rows[0]["order_id"] == 1001 and rows[0]["customer.name"] == "Ada"
    assert [r["lines_index"] for r in rows] == [0, 1]
    assert rows[0]["serials"] == '["x","y"]'
    assert rows[1]["qty"] == 1 and "serials" not in rows[1]
    default_keep = _rows(ORDER, unwind="lines")
    assert default_keep[0]["_id"] == "o1"
    prefixed = _rows(ORDER, unwind={"path": "lines", "prefix": "line_", "index": "pos"})
    assert prefixed[0]["line_sku"] == "A" and prefixed[1]["pos"] == 1
    tags = _rows(ORDER, arrays={"tags": "explode"})
    assert [r["tags"] for r in tags] == ["b", "a"]
    assert _rows({"_id": 1, "lines": []}, unwind="lines") == []
    empty = _rows({"_id": 1}, unwind={"path": "lines", "preserve_empty": True})
    assert empty == [{"_id": 1, "lines_index": None}]
    single = _rows({"_id": 1, "lines": {"sku": "Z"}}, unwind="lines")
    assert single == [{"_id": 1, "lines_index": None, "sku": "Z"}]


def test_fields_with_unwind_use_unwound_paths() -> None:
    rows = _rows(
        ORDER,
        unwind={"path": "lines", "index": "line_index"},
        fields={"order_id": "order_id", "sku": "lines.sku", "city": "customer.address.city"},
    )
    assert rows == [
        {"order_id": 1001, "sku": "A", "city": "Leeds", "line_index": 0},
        {"order_id": 1001, "sku": "B", "city": "Leeds", "line_index": 1},
    ]
    (row,) = _rows(ORDER, fields={"first": "lines.0.sku", "skus": "lines.sku", "x": "missing"})
    assert row == {"first": "A", "skus": '["A","B"]', "x": None}
    (row,) = _rows(ORDER, fields={"cust": "customer"})
    assert row["cust"].startswith('{"address"')


def test_column_conflicts_and_bad_options_are_clear() -> None:
    with pytest.raises(ConnectorError, match="Two values map to column 'sku'"):
        _rows(
            {"_id": 1, "sku": "p", "lines": [{"sku": "c"}]},
            unwind={"path": "lines", "keep": ["sku"]},
        )
    with pytest.raises(ConnectorError, match=r"options\.unwnd: unknown option") as info:
        _options(unwnd="lines")
    assert "unwind" in (info.value.hint or "")
    with pytest.raises(ConnectorError, match="only one array can be exploded"):
        _options(arrays={"a": "explode", "b": "explode"})
    with pytest.raises(ConnectorError, match="conflicts with"):
        _options(arrays={"a": "explode"}, unwind="b")
    with pytest.raises(ConnectorError, match="'fields' is empty"):
        _options(fields={})
    with pytest.raises(ConnectorError, match=r"arrays\.tags"):
        _options(arrays={"tags": "zip"})


def _stage(rows: list[dict[str, Any]], **kwargs: Any) -> tuple[pa.Table, Any]:
    stager = RowStager(**kwargs)
    for row in rows:
        stager.add({k: classify_python(v) for k, v in row.items()})
    staged = stager.finish()
    table = pa.Table.from_batches(list(staged.batches), schema=staged.schema)
    return table, staged


def test_type_unification_rules() -> None:
    rows = [
        {
            "i": 1,
            "n": 1,
            "d": Decimal("1.5"),
            "f": 1,
            "s": "a",
            "mix": 1,
            "t": datetime(2024, 1, 1),
        },
        {"i": 2**40, "n": Decimal("2.25"), "d": Decimal("10"), "f": 2.5, "mix": "x", "nul": None},
        {"i": None, "n": None, "d": None, "f": None, "s": None, "mix": True, "t": date(2024, 1, 2)},
    ]
    table, staged = _stage(rows, hints=TypeHints(decimal_precision=34, timestamp_precision=3))
    types = {f.name: f.type for f in table.schema}
    assert types["i"] == pa.int64()
    assert types["n"] == pa.decimal128(38, 2) and types["d"] == pa.decimal128(38, 1)
    assert types["f"] == pa.float64()
    assert types["s"] == pa.string() and types["nul"] == pa.string()
    assert types["mix"] == pa.string()
    assert types["t"] == pa.timestamp("us", tz="UTC")
    data = table.to_pydict()
    assert data["n"] == [Decimal("1.00"), Decimal("2.25"), None]
    assert data["mix"] == ["1", "x", "true"]
    assert data["t"][2] == datetime(2024, 1, 2, tzinfo=UTC)
    assert staged.native_types["i"] == "int|long"
    assert staged.native_types["mix"] in ("int|string|bool", "int|bool|string")
    assert "compared as canonical text" in staged.comments["mix"]
    assert "compared as decimals" in staged.comments["n"]
    assert "compared as double" in staged.comments["f"]
    assert staged.canonical_types["n"] == CanonicalType.decimal(34, 2)
    assert staged.canonical_types["t"] == CanonicalType.timestamp(3, tz=True)
    assert staged.rows == 3


def test_special_kinds_and_wide_values() -> None:
    rows: list[dict[str, Any]] = [
        {"oid": Classified(ValueKind.OBJECT_ID, "objectId", "65f1c0de0123456789abcdef")},
        {"u": Classified(ValueKind.UUID, "uuid", "00000000-0000-0000-0000-000000000001")},
        {"j": Classified(ValueKind.OBJECT, "object", '{"a":1}')},
        {"j": Classified(ValueKind.ARRAY, "array", "[1]")},
        {"big": Classified(ValueKind.INT, "long", 10**30)},
        {"wide": Classified(ValueKind.DECIMAL, "decimal", Decimal("1" * 30 + "." + "1" * 10))},
        {"b": Classified(ValueKind.BINARY, "binData", b"\x01")},
    ]
    stager = RowStager()
    for row in rows:
        stager.add(dict(row))
    staged = stager.finish(columns=["first", "oid"])
    table = pa.Table.from_batches(list(staged.batches), schema=staged.schema)
    assert table.schema.names[:2] == ["first", "oid"]
    assert staged.canonical_types["oid"] == CanonicalType.string(24, fixed=True)
    assert staged.canonical_types["u"].logical is LogicalType.UUID
    assert staged.canonical_types["j"].logical is LogicalType.JSON
    assert table.schema.field("big").type == pa.decimal128(38, 0)
    assert table.schema.field("wide").type == pa.string()
    assert "40 digits" in staged.comments["wide"]
    assert table.schema.field("b").type == pa.binary()


def test_forced_types_convert_or_fail_clearly() -> None:
    table, _ = _stage(
        [
            {"when": "2024-01-02T03:04:05+01:00", "day": "2024-01-02", "n": "12", "ok": "true"},
            {"when": "2024-01-03", "day": datetime(2024, 1, 3), "n": 13.0, "ok": 0},
        ],
        forced={"when": "timestamp", "day": "date", "n": "integer", "ok": "boolean"},
    )
    data = table.to_pydict()
    assert data["when"][0] == datetime(2024, 1, 2, 2, 4, 5, tzinfo=UTC)
    assert data["day"] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert data["n"] == [12, 13] and data["ok"] == [True, False]
    cell = force_cell(classify_python({"a": 1}), "json", "c", 1)
    assert cell.kind is ValueKind.OBJECT
    assert force_cell(classify_python(5), "json", "c", 1).value == "5"
    assert force_cell(classify_python(1.5), "decimal", "c", 1).value == Decimal("1.5")
    assert force_cell(classify_python(Decimal("2")), "double", "c", 1).value == 2.0
    assert force_cell(classify_python(7), "string", "c", 1).value == "7"
    with pytest.raises(ConnectorError, match="Row 3: a string value in column 'n'"):
        force_cell(classify_python("x"), "integer", "n", 3)
    with pytest.raises(ConnectorError, match="not a whole number"):
        force_cell(classify_python(1.5), "integer", "n", 1)
    with pytest.raises(ConnectorError, match="not a boolean"):
        force_cell(classify_python("maybe"), "boolean", "b", 1)
    with pytest.raises(ConnectorError, match="not a timestamp"):
        force_cell(classify_python(5), "timestamp", "t", 1)


def test_stager_spools_in_chunks_and_batches() -> None:
    stager = RowStager(chunk_rows=3, batch_rows=4)
    for i in range(10):
        stager.add({"id": classify_python(i)})
    staged = stager.finish()
    batches = list(staged.batches)
    assert [b.num_rows for b in batches] == [6, 4]
    assert sum(b.num_rows for b in batches) == 10
