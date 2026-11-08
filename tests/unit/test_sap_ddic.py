"""ABAP dictionary rows (DD02L, DD03L, DD04L, DD04T, DD05S, DD08L) mapped to dvf schemas."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa

from dvf.core.datasets import DatasetKind, DatasetRef
from dvf.core.types import CanonicalType, LogicalType
from dvf.packs.sap.ddic import (
    DdicCache,
    DdicField,
    DdicTable,
    fields_from_rows,
    foreign_keys_from_rows,
    quote_literal,
    read_table,
)


def _dd03l(
    name: str, pos: int, datatype: str, leng: int, dec: int = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "FIELDNAME": name,
        "POSITION": f"{pos:04d}",
        "DATATYPE": datatype,
        "LENG": f"{leng:06d}",
        "DECIMALS": f"{dec:06d}",
        **extra,
    }


def test_fields_skip_includes_and_fill_types_from_data_elements() -> None:
    rows = [
        _dd03l("NAME1", 3, "", 0, ROLLNAME="NAME1_GP"),
        _dd03l("MANDT", 1, "CLNT", 3, KEYFLAG="X", ROLLNAME="MANDT"),
        _dd03l(".INCLUDE", 2, "", 0),
        _dd03l("WRBTR", 4, "CURR", 13, 2, REFFIELD="WAERS", ROLLNAME="WRBTR"),
        _dd03l("BUDAT", 5, "DATS", 8),
        _dd03l("UZEIT", 6, "TIMS", 6),
        _dd03l("POSNR", 7, "NUMC", 6),
        _dd03l("MENGE", 8, "QUAN", 13, 3, REFFIELD="MEINS"),
        _dd03l("FLAG", 9, "CHAR", 1, NOTNULL="X"),
    ]
    texts = {"NAME1_GP": "Name", "WRBTR": "Amount in document currency"}
    elements = {"NAME1_GP": {"DATATYPE": "CHAR", "LENG": "000035", "DECIMALS": "000000"}}
    fields = fields_from_rows(rows, texts, elements)
    assert [f.name for f in fields] == [
        "MANDT",
        "NAME1",
        "WRBTR",
        "BUDAT",
        "UZEIT",
        "POSNR",
        "MENGE",
        "FLAG",
    ]
    by_name = {f.name: f for f in fields}
    assert by_name["NAME1"].native_type == "CHAR(35)" and by_name["NAME1"].description == "Name"
    assert by_name["WRBTR"].native_type == "CURR(13,2)"
    assert by_name["WRBTR"].canonical_type == CanonicalType.decimal(13, 2)
    assert by_name["BUDAT"].canonical_type == CanonicalType.string(8, fixed=True)
    assert by_name["BUDAT"].logical_type == CanonicalType.date()
    assert by_name["UZEIT"].logical_type.logical is LogicalType.TIME
    assert by_name["POSNR"].canonical_type == CanonicalType.string(6, fixed=True)
    assert by_name["MANDT"].is_client and by_name["MANDT"].key
    assert by_name["FLAG"].not_null


def test_arrow_types() -> None:
    def arrow(datatype: str, leng: int = 10, dec: int = 0) -> pa.DataType:
        return DdicField("F", 1, datatype, leng, dec).arrow_type()

    assert arrow("CURR", 13, 2) == pa.decimal128(13, 2)
    assert arrow("DEC", 45, 5) == pa.decimal128(38, 5)
    assert arrow("INT1", 3) == pa.int16()
    assert arrow("INT4") == pa.int32()
    assert arrow("INT8") == pa.int64()
    assert arrow("FLTP", 16) == pa.float64()
    assert arrow("RAW", 16) == pa.binary()
    assert arrow("DATS", 8) == pa.string()
    assert arrow("CHAR") == pa.string()


def test_table_schema_keys_client_and_references() -> None:
    fields = (
        DdicField("MANDT", 1, "CLNT", 3, key=True),
        DdicField("BELNR", 2, "CHAR", 10, key=True, description="Document"),
        DdicField("WRBTR", 3, "CURR", 13, 2, reffield="WAERS"),
        DdicField("WAERS", 4, "CUKY", 5),
    )
    table = DdicTable("BSEG", fields, client_dependent=True)
    assert table.primary_key == ("MANDT", "BELNR")
    assert table.client_column == "MANDT"
    assert table.references == {"WRBTR": "WAERS"}
    ref = DatasetRef(connection="c", kind=DatasetKind.TABLE, name="BSEG")
    schema = table.schema(ref)
    assert [c.native_type for c in schema.columns] == [
        "CLNT(3)",
        "CHAR(10)",
        "CURR(13,2)",
        "CUKY(5)",
    ]
    assert schema.columns[1].comment == "Document" and schema.columns[1].nullable is False
    subset = table.schema(ref, ["belnr", "WRBTR"])
    assert [c.name for c in subset.columns] == ["BELNR", "WRBTR"]
    assert subset.primary_key == ("BELNR",)
    assert DdicTable("T005", fields[1:], client_dependent=False).client_column is None
    assert DdicTable("X", fields, client_dependent=False).client_column is None
    assert DdicTable("X", fields).client_column == "MANDT"  # unknown: the CLNT key decides


def test_foreign_keys() -> None:
    dd08l = [
        {"FIELDNAME": "LAND1", "CHECKTABLE": "T005", "FRKART": "", "CARD": "CN", "CARDLEFT": "1"},
        {"FIELDNAME": ".APPEND", "CHECKTABLE": "", "FRKART": ""},
    ]
    dd05s = [
        {"FIELDNAME": "LAND1", "FORKEY": "LAND1", "CHECKFIELD": "LAND1", "PRIMPOS": "0002"},
        {"FIELDNAME": "LAND1", "FORKEY": "MANDT", "CHECKFIELD": "MANDT", "PRIMPOS": "0001"},
    ]
    (key,) = foreign_keys_from_rows(dd08l, dd05s)
    assert key.check_table == "T005" and key.cardinality == "1:CN"
    assert key.columns == (("MANDT", "MANDT"), ("LAND1", "LAND1"))


class DictSource:
    """A DdicSource over in-memory rows; conditions are recorded, not evaluated."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables
        self.queries: list[tuple[str, str]] = []

    def select(self, table: str, fields: Sequence[str], where: str) -> list[dict[str, Any]]:
        self.queries.append((table, where))
        return [{f: r.get(f, "") for f in fields} for r in self.tables.get(table, [])]


def test_read_table_through_a_source() -> None:
    rollnames = [f"ZELEMENT{i:02d}" for i in range(45)]
    source = DictSource(
        {
            "DD02L": [{"TABNAME": "ZTAB", "TABCLASS": "TRANSP", "CLIDEP": "", "CONTFLAG": "A"}],
            "DD02T": [{"DDTEXT": "Custom table"}],
            "DD03L": [
                _dd03l(f"F{i:02d}", i + 1, "CHAR", 10, KEYFLAG="X" if i == 0 else "", ROLLNAME=r)
                for i, r in enumerate(rollnames)
            ],
            "DD04T": [{"ROLLNAME": "ZELEMENT00", "DDTEXT": "Key"}],
        }
    )
    table = read_table(source, "ztab", language="D")
    assert table is not None
    assert table.name == "ZTAB" and table.description == "Custom table"
    assert table.client_dependent is False and table.client_column is None
    assert table.primary_key == ("F00",) and len(table.fields) == 45
    assert table.fields[0].description == "Key"
    dd04t = [w for t, w in source.queries if t == "DD04T"]
    assert len(dd04t) == 2  # 45 data elements in IN lists of at most 40
    assert all("DDLANGUAGE = 'D'" in w for w in dd04t)
    assert ("DD02L", "TABNAME = 'ZTAB' AND AS4LOCAL = 'A'") in source.queries
    assert not any(t == "DD04L" for t, _ in source.queries)  # DD03L had every type
    assert read_table(DictSource({}), "NOPE") is None
    no_keys = read_table(source, "ZTAB", foreign_keys=False)
    assert no_keys is not None and no_keys.foreign_keys == ()


def test_cache_and_literals() -> None:
    cache = DdicCache()
    assert not cache.has("kna1")
    cache.put("kna1", None)
    assert cache.has("KNA1") and cache.get("KNA1") is None
    assert quote_literal("it's") == "'it''s'"
