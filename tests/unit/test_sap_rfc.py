"""The sap_rfc connector and its function-module backends, against a fake SAP system.

The fake simulates RFC_READ_TABLE as SAP implements it: a 512-character row buffer
(DATA_BUFFER_EXCEEDED unless NO_DATA is set), ROWSKIPS and ROWCOUNT paging, OPTIONS conditions in
72-character lines, field offsets, packed numbers with a trailing minus sign, and the dictionary
(DDIF_FIELDINFO_GET, DD02L, DD03L, DD04T, DD05S, DD08L). All data is synthetic.
"""

from __future__ import annotations

import re
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from dvf import api
from dvf.config.connections import SapRfcConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.sap_rfc import SapRfcConnector
from dvf.core.errors import ConnectorError
from dvf.core.types import LogicalType
from dvf.packs.sap.ddic import DdicField
from dvf.packs.sap.drivers import SapDriverMissingError
from dvf.packs.sap.rfc import backends as backends_module
from dvf.packs.sap.rfc.backends import (
    RemoteColumn,
    RemoteTable,
    SapRfcBackend,
    TableRead,
    backend_names,
    register_rfc_backend,
)
from dvf.packs.sap.rfc.read_table import (
    RfcBufferExceededError,
    parse_value,
    plan_chunks,
    split_where,
)

# ---------------------------------------------------------------------- the fake SAP system


@dataclass
class FakeField:
    name: str
    datatype: str
    length: int
    decimals: int = 0
    key: bool = False
    text: str = ""
    rollname: str = ""
    reffield: str = ""

    @property
    def inttype(self) -> str:
        return {
            "DEC": "P",
            "CURR": "P",
            "QUAN": "P",
            "INT4": "I",
            "NUMC": "N",
            "DATS": "D",
            "TIMS": "T",
            "FLTP": "F",
            "RAW": "X",
        }.get(self.datatype, "C")

    @property
    def width(self) -> int:
        """Characters RFC_READ_TABLE gives the field (packed: digits, sign, point)."""
        if self.inttype == "P":
            return self.length + 1 + (1 if self.decimals else 0)
        if self.datatype == "INT4":
            return 11
        if self.datatype == "RAW":
            return 2 * self.length
        return self.length

    def render(self, value: Any) -> str:
        if self.inttype in ("P", "I"):
            number = Decimal(value or 0)
            text = f"{abs(number):.{self.decimals}f}" + ("-" if number < 0 else " ")
            return text.rjust(self.width)
        if self.datatype == "RAW":
            return (value or b"").hex().upper().ljust(self.width)
        return str(value or "").ljust(self.width)[: self.width]


@dataclass
class FakeTable:
    name: str
    fields: list[FakeField]
    rows: list[dict[str, Any]] = field(default_factory=list)
    client_dependent: bool = True


class FakeAbapError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _char(name: str, length: int, key: bool = False) -> FakeField:
    return FakeField(name, "CHAR", length, key=key)


def _dd_tables(tables: list[FakeTable]) -> list[FakeTable]:
    dd02l = FakeTable(
        "DD02L",
        [_char(n, 30) for n in ("TABNAME", "AS4LOCAL", "TABCLASS", "CLIDEP", "CONTFLAG")],
        client_dependent=False,
    )
    dd02t = FakeTable(
        "DD02T",
        [_char(n, 60) for n in ("TABNAME", "AS4LOCAL", "DDLANGUAGE", "DDTEXT")],
        client_dependent=False,
    )
    dd03l_names = (
        "TABNAME",
        "FIELDNAME",
        "AS4LOCAL",
        "AS4VERS",
        "POSITION",
        "KEYFLAG",
        "DATATYPE",
        "LENG",
        "DECIMALS",
        "ROLLNAME",
        "DOMNAME",
        "NOTNULL",
        "REFTABLE",
        "REFFIELD",
        "CHECKTABLE",
    )
    dd03l = FakeTable("DD03L", [_char(n, 30) for n in dd03l_names], client_dependent=False)
    dd04t = FakeTable(
        "DD04T",
        [_char(n, 60) for n in ("ROLLNAME", "DDLANGUAGE", "AS4LOCAL", "DDTEXT")],
        client_dependent=False,
    )
    dd08l = FakeTable(
        "DD08L",
        [
            _char(n, 30)
            for n in (
                "TABNAME",
                "FIELDNAME",
                "AS4LOCAL",
                "CHECKTABLE",
                "FRKART",
                "CARD",
                "CARDLEFT",
            )
        ],
        client_dependent=False,
    )
    dd05s = FakeTable(
        "DD05S",
        [
            _char(n, 30)
            for n in (
                "TABNAME",
                "FIELDNAME",
                "AS4LOCAL",
                "FORTABLE",
                "FORKEY",
                "CHECKTABLE",
                "CHECKFIELD",
                "PRIMPOS",
            )
        ],
        client_dependent=False,
    )
    for table in tables:
        active = {"TABNAME": table.name, "AS4LOCAL": "A"}
        dd02l.rows.append(
            {**active, "TABCLASS": "TRANSP", "CLIDEP": "X" if table.client_dependent else ""}
        )
        dd02t.rows.append({**active, "DDLANGUAGE": "E", "DDTEXT": f"Table {table.name}"})
        dd03l.rows.append(
            {**active, "FIELDNAME": ".INCLUDE", "AS4VERS": "0000", "POSITION": "0000"}
        )
        for i, f in enumerate(table.fields, start=1):
            dd03l.rows.append(
                {
                    **active,
                    "FIELDNAME": f.name,
                    "AS4VERS": "0000",
                    "POSITION": f"{i:04d}",
                    "KEYFLAG": "X" if f.key else "",
                    "DATATYPE": f.datatype,
                    "LENG": f"{f.length:06d}",
                    "DECIMALS": f"{f.decimals:06d}",
                    "ROLLNAME": f.rollname,
                    "REFFIELD": f.reffield,
                }
            )
            if f.rollname:
                dd04t.rows.append(
                    {"ROLLNAME": f.rollname, "DDLANGUAGE": "E", "AS4LOCAL": "A", "DDTEXT": f.text}
                )
    dd08l.rows.append(
        {
            "TABNAME": "KNA1",
            "FIELDNAME": "LAND1",
            "AS4LOCAL": "A",
            "CHECKTABLE": "T005",
            "FRKART": "",
            "CARD": "CN",
            "CARDLEFT": "1",
        }
    )
    dd05s.rows.append(
        {
            "TABNAME": "KNA1",
            "FIELDNAME": "LAND1",
            "AS4LOCAL": "A",
            "FORTABLE": "KNA1",
            "FORKEY": "LAND1",
            "CHECKTABLE": "T005",
            "CHECKFIELD": "LAND1",
            "PRIMPOS": "0002",
        }
    )
    return [dd02l, dd02t, dd03l, dd04t, dd08l, dd05s]


_CONDITION = re.compile(r"^(\w+)\s*(=|<>|LIKE)\s*'((?:[^']|'')*)'$")
_IN = re.compile(r"^(\w+)\s+IN\s*\((.*)\)$")


def _split_and(where: str) -> list[str]:
    parts, current, quoted = [], [], False
    tokens = re.findall(r"'(?:[^']|'')*'|\S+", where)
    for token in tokens:
        if token.upper() == "AND" and not quoted:
            parts.append(" ".join(current))
            current = []
        else:
            current.append(token)
    if current:
        parts.append(" ".join(current))
    return parts


def _matches(row: dict[str, Any], where: str) -> bool:
    if not where.strip():
        return True
    for part in _split_and(where):
        part = part.strip()
        found_in = _IN.match(part)
        if found_in:
            values = {
                v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", found_in.group(2))
            }
            if str(row.get(found_in.group(1), "")) not in values:
                return False
            continue
        found = _CONDITION.match(part)
        assert found, f"fake SAP cannot evaluate {part!r}"
        name, op, literal = found.group(1), found.group(2), found.group(3).replace("''", "'")
        value = str(row.get(name, "") or "")
        if op == "=" and value != literal:
            return False
        if op == "<>" and value == literal:
            return False
        if op == "LIKE":
            pattern = "^" + re.escape(literal).replace("%", ".*").replace("_", ".") + "$"
            if not re.match(pattern, value):
                return False
    return True


class FakeSap:
    """An SAP system answering RFC_READ_TABLE, DDIF_FIELDINFO_GET, and RFC_SYSTEM_INFO."""

    def __init__(
        self, tables: list[FakeTable], read_functions: dict[str, int] | None = None
    ) -> None:
        self.tables = {t.name: t for t in [*tables, *_dd_tables(tables)]}
        self.read_functions = read_functions or {"RFC_READ_TABLE": 512}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reverse_after: int | None = None
        """Return rows in reverse order from this read call on (an unordered database)."""
        self.reads = 0

    def call(self, function: str, **params: Any) -> dict[str, Any]:
        self.calls.append((function, params))
        if function in self.read_functions:
            return self._read(params, self.read_functions[function])
        if function == "DDIF_FIELDINFO_GET":
            return self._fieldinfo(params["TABNAME"])
        if function == "RFC_SYSTEM_INFO":
            return {"RFCSI_EXPORT": {"RFCSAPRL": "758", "RFCSYSID": "S4D"}}
        raise FakeAbapError("FU_NOT_FOUND")

    def _fieldinfo(self, name: str) -> dict[str, Any]:
        table = self.tables.get(name)
        if table is None:
            raise FakeAbapError("NOT_FOUND")
        rows = [
            {
                "FIELDNAME": f.name,
                "POSITION": f"{i:04d}",
                "KEYFLAG": "X" if f.key else "",
                "DATATYPE": f.datatype,
                "LENG": f"{f.length:06d}",
                "DECIMALS": f"{f.decimals:06d}",
                "INTTYPE": f.inttype,
                "OUTPUTLEN": f"{f.width:06d}",
                "FIELDTEXT": f.text,
                "ROLLNAME": f.rollname,
                "REFFIELD": f.reffield,
            }
            for i, f in enumerate(table.fields, start=1)
        ]
        return {"DFIES_TAB": rows, "DDOBJTYPE": "TRANSP"}

    def _read(self, params: dict[str, Any], buffer: int) -> dict[str, Any]:
        table = self.tables.get(params["QUERY_TABLE"])
        if table is None:
            raise FakeAbapError("TABLE_NOT_AVAILABLE")
        delimiter = params.get("DELIMITER", "") or ""
        for line in params.get("OPTIONS", []):
            assert len(line["TEXT"]) <= 72, "OPTIONS lines hold at most 72 characters"
        requested = [r["FIELDNAME"] for r in params.get("FIELDS", [])]
        by_name = {f.name: f for f in table.fields}
        chosen = [by_name[n] for n in requested] if requested else list(table.fields)
        out_fields, offset = [], 0
        for i, f in enumerate(chosen):
            out_fields.append(
                {
                    "FIELDNAME": f.name,
                    "OFFSET": f"{offset:06d}",
                    "LENGTH": f"{f.width:06d}",
                    "TYPE": f.inttype,
                    "FIELDTEXT": f.text,
                }
            )
            offset += f.width + (len(delimiter) if i < len(chosen) - 1 else 0)
        if params.get("NO_DATA") == "X":
            return {"FIELDS": out_fields, "DATA": []}
        if offset > buffer:
            raise FakeAbapError("DATA_BUFFER_EXCEEDED")
        where = " ".join(line["TEXT"] for line in params.get("OPTIONS", []))
        rows = [r for r in table.rows if _matches(r, where)]
        self.reads += 1
        if self.reverse_after is not None and self.reads > self.reverse_after:
            rows = list(reversed(rows))
        skip, count = int(params.get("ROWSKIPS", 0)), int(params.get("ROWCOUNT", 0))
        rows = rows[skip:]
        if count:
            rows = rows[:count]
        data = [
            {"WA": delimiter.join(f.render(r.get(f.name)) for f in chosen).rstrip()} for r in rows
        ]
        return {"FIELDS": out_fields, "DATA": data}


def kna1_table(rows: int = 7) -> FakeTable:
    fields = [
        FakeField("MANDT", "CLNT", 3, key=True, rollname="MANDT", text="Client"),
        FakeField("KUNNR", "CHAR", 10, key=True, rollname="KUNNR", text="Customer"),
        FakeField("NAME1", "CHAR", 35, rollname="NAME1_GP", text="Name"),
        FakeField("LAND1", "CHAR", 3, rollname="LAND1_GP", text="Country"),
        FakeField("ERDAT", "DATS", 8, rollname="ERDAT", text="Created on"),
        FakeField("UMSAT", "CURR", 15, 2, rollname="UMSAT", reffield="WAERS", text="Sales"),
        FakeField("WAERS", "CUKY", 5, rollname="WAERS", text="Currency"),
        FakeField("STCEG", "CHAR", 20),
        FakeField("RAWID", "RAW", 4),
        FakeField("CNT", "INT4", 10),
    ]
    # Wide text fields push the row past 512 characters, so reads are chunked.
    fields += [FakeField(f"TEXT{i}", "CHAR", 132) for i in range(1, 5)]
    data = []
    for i in range(1, rows + 1):
        row: dict[str, Any] = {
            "MANDT": "100",
            "KUNNR": f"{i:010d}",
            "NAME1": f"Customer {i}",
            "LAND1": "DE" if i % 2 else "JP",
            "ERDAT": "00000000" if i == 3 else f"2024{(i % 12) + 1:02d}15",
            "UMSAT": Decimal(f"{-1 if i == 2 else 1}{i}.5{i}"),
            "WAERS": "EUR" if i % 2 else "JPY",
            "STCEG": f"DE{i:09d}",
            "RAWID": bytes([i, 0, 255, 16]),
            "CNT": -i if i == 4 else i * 1000,
        }
        for j in range(1, 5):
            row[f"TEXT{j}"] = f"text {j} of {i}"
        data.append(row)
    return FakeTable("KNA1", fields, data)


@pytest.fixture
def fake_sap(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSap]:
    system = FakeSap(
        [
            kna1_table(),
            FakeTable(
                "TCURX",
                [_char("CURRKEY", 5, key=True), FakeField("CURRDEC", "INT4", 2)],
                [{"CURRKEY": "JPY", "CURRDEC": 0}],
                client_dependent=False,
            ),
        ],
        read_functions={"RFC_READ_TABLE": 512, "Z_RFC_READ_TABLE": 8000},
    )
    module = types.ModuleType("pyrfc")
    logons: list[dict[str, Any]] = []

    class Connection:
        def __init__(self, **kwargs: Any) -> None:
            logons.append(kwargs)

        def call(self, function: str, **params: Any) -> dict[str, Any]:
            return system.call(function, **params)

        def close(self) -> None:
            return None

    module.Connection = Connection  # type: ignore[attr-defined]
    module.ABAPApplicationError = FakeAbapError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyrfc", module)
    system.logons = logons  # type: ignore[attr-defined]
    yield system


def rfc_config(**overrides: Any) -> SapRfcConnection:
    data = {
        "type": "sap_rfc",
        "ashost": "sap.example.test",
        "sysnr": "00",
        "client": "100",
        "user": "DVF_TEST",
        "password": "synthetic-not-a-secret",
        "page_size": 3,
        **overrides,
    }
    return SapRfcConnection.model_validate(data)


def connector(tmp_path: Path, **overrides: Any) -> SapRfcConnector:
    return SapRfcConnector("sap", rfc_config(**overrides), tmp_path)


# ---------------------------------------------------------------------- pieces


def test_split_where_keeps_literals_whole() -> None:
    where = (
        "BUKRS = '1000' AND GJAHR = '2024' AND NAME1 = 'it''s a long customer name with spaces' "
        "AND LAND1 IN ( 'DE' , 'FR' , 'JP' , 'US' ) AND ERDAT >= '20240101'"
    )
    lines = split_where(where)
    assert all(len(line) <= 72 for line in lines)
    assert " ".join(lines) == " ".join(where.split())
    assert any("'it''s a long customer name with spaces'" in line for line in lines)
    assert split_where("  ") == []
    with pytest.raises(ConnectorError, match="longer than 72"):
        split_where("NAME1 = '" + "x" * 80 + "'")
    with pytest.raises(ConnectorError, match="Unbalanced"):
        split_where("NAME1 = 'abc")


def test_plan_chunks_repeats_keys_and_fits_the_buffer() -> None:
    fields = [DdicField("K", 1, "CHAR", 10, key=True)] + [
        DdicField(f"F{i}", i + 1, "CHAR", 200) for i in range(5)
    ]
    widths = {f.name: f.length for f in fields}
    chunks = plan_chunks(fields, ["K"], widths, 512)
    assert all(c[0] == "K" for c in chunks)
    assert all(sum(widths[n] for n in c) <= 512 for c in chunks)
    assert sorted({n for c in chunks for n in c}) == sorted(widths)
    assert plan_chunks(fields[:2], ["K"], widths, 512) == [("K", "F0")]
    with pytest.raises(ConnectorError, match="no key fields"):
        plan_chunks(fields, [], widths, 512)
    with pytest.raises(ConnectorError, match="does not fit"):
        plan_chunks(
            [fields[0], DdicField("BIG", 2, "CHAR", 600)], ["K"], {"K": 10, "BIG": 600}, 512
        )


def test_parse_value_by_ddic_type() -> None:
    curr = DdicField("UMSAT", 1, "CURR", 15, 2, inttype="P")
    assert parse_value("      1234.56-", curr) == Decimal("-1234.56")
    assert parse_value("   12.50 ", curr) == Decimal("12.50")
    assert parse_value("    ", curr) is None
    assert parse_value("        42-", DdicField("N", 1, "INT4", 10)) == -42
    assert parse_value(" 1.5000000000000000E+00", DdicField("F", 1, "FLTP", 16)) == 1.5
    assert parse_value("0AFF", DdicField("R", 1, "RAW", 2)) == b"\x0a\xff"
    assert parse_value("0000012345  ", DdicField("K", 1, "CHAR", 12)) == "0000012345"
    assert parse_value("20240115", DdicField("D", 1, "DATS", 8)) == "20240115"


# ---------------------------------------------------------------------- the connector


def test_schema_comes_from_the_dictionary(fake_sap: FakeSap, tmp_path: Path) -> None:
    con = connector(tmp_path)
    try:
        rel = con.resolve(DatasetSpec(table="KNA1"))
        schema = con.get_schema(rel)
        assert schema.primary_key == ("MANDT", "KUNNR")
        by_name = {c.name: c for c in schema.columns}
        assert by_name["KUNNR"].native_type == "CHAR(10)"
        assert by_name["UMSAT"].native_type == "CURR(15,2)"
        assert by_name["UMSAT"].type.logical is LogicalType.DECIMAL
        assert by_name["ERDAT"].native_type == "DATS(8)"
        assert by_name["ERDAT"].type.logical is LogicalType.STRING  # stored as text
        table = con.remote_table("KNA1")
        assert table is not None and table.column("NAME1").description == "Name"  # type: ignore[union-attr]
        assert not any(f == "RFC_READ_TABLE" for f, _ in fake_sap.calls)  # described, not read
        assert fake_sap.logons[0]["client"] == "100"
        assert fake_sap.logons[0]["passwd"] == "synthetic-not-a-secret"
    finally:
        con.close()


def test_wide_table_is_chunked_paged_and_converted(fake_sap: FakeSap, tmp_path: Path) -> None:
    con = connector(tmp_path)
    try:
        rel = con.resolve(DatasetSpec(table="KNA1"))
        rows = con.query(f"SELECT * FROM {rel.sql} ORDER BY KUNNR").to_pylist()
    finally:
        con.close()
    assert len(rows) == 7
    first, second, fourth = rows[0], rows[1], rows[3]
    assert first["KUNNR"] == "0000000001" and first["NAME1"] == "Customer 1"
    assert second["UMSAT"] == Decimal("-12.52")
    assert fourth["CNT"] == -4
    assert first["RAWID"] == bytes([1, 0, 255, 16])
    assert first["TEXT4"] == "text 4 of 1"
    reads = [p for f, p in fake_sap.calls if f == "RFC_READ_TABLE" and p.get("NO_DATA") != "X"]
    chunks = {tuple(r["FIELDNAME"] for r in p["FIELDS"]) for p in reads}
    assert len(chunks) > 1 and all(c[:2] == ("MANDT", "KUNNR") for c in chunks)
    assert {p["ROWCOUNT"] for p in reads} == {3}
    assert {p["ROWSKIPS"] for p in reads} == {0, 3, 6}


def test_where_fields_and_max_rows_are_pushed_down(fake_sap: FakeSap, tmp_path: Path) -> None:
    con = connector(tmp_path, page_size=100)
    try:
        spec = DatasetSpec(
            table="KNA1",
            options={"fields": ["KUNNR", "LAND1", "UMSAT"], "where": "LAND1 = 'DE'", "max_rows": 2},
        )
        rel = con.resolve(spec)
        assert [c.name for c in con.get_schema(rel).columns] == ["KUNNR", "LAND1", "UMSAT"]
        rows = con.query(f"SELECT * FROM {rel.sql}").to_pylist()
    finally:
        con.close()
    assert len(rows) == 2 and {r["LAND1"] for r in rows} == {"DE"}
    reads = [p for f, p in fake_sap.calls if f == "RFC_READ_TABLE" and p.get("NO_DATA") != "X"]
    assert reads[-1]["OPTIONS"] == [{"TEXT": "LAND1 = 'DE'"}]
    assert reads[-1]["ROWCOUNT"] == 2


def test_custom_read_module_needs_no_chunks(fake_sap: FakeSap, tmp_path: Path) -> None:
    con = connector(
        tmp_path,
        backend="custom_fm",
        read_function="Z_RFC_READ_TABLE",
        row_width=8000,
        delimiter="|",
        page_size=50,
    )
    try:
        rel = con.resolve(DatasetSpec(table="KNA1"))
        assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 7}]
        assert con.query(f"SELECT NAME1 FROM {rel.sql} WHERE KUNNR = '0000000005'").to_pylist() == [
            {"NAME1": "Customer 5"}
        ]
    finally:
        con.close()
    reads = [p for f, p in fake_sap.calls if f == "Z_RFC_READ_TABLE" and p.get("NO_DATA") != "X"]
    assert len(reads) == 1 and reads[0]["DELIMITER"] == "|"


def test_buffer_exceeded_chunks_are_split(fake_sap: FakeSap, tmp_path: Path) -> None:
    # The function reports narrower fields than it really uses: chunks are rejected and split.
    original = fake_sap._read

    def lying_read(params: dict[str, Any], buffer: int) -> dict[str, Any]:
        result = original(params, buffer)
        if params.get("NO_DATA") == "X":
            for row in result["FIELDS"]:
                row["LENGTH"] = "000001"
        return result

    fake_sap._read = lying_read  # type: ignore[method-assign]
    con = connector(tmp_path, page_size=100)
    try:
        rel = con.resolve(DatasetSpec(table="KNA1"))
        assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 7}]
    finally:
        con.close()


def test_rows_returned_in_another_order_fail_the_read(fake_sap: FakeSap, tmp_path: Path) -> None:
    fake_sap.reverse_after = 1
    con = connector(tmp_path, page_size=100)
    try:
        rel = con.resolve(DatasetSpec(table="KNA1"))
        con.query(f"SELECT COUNT(*) FROM {rel.sql}")  # one page: chunks join on the key
        fake_sap.reverse_after = 0
        fake_sap.reads = 0
    finally:
        con.close()
    paged = connector(
        tmp_path, page_size=3, row_width=8000, backend="custom_fm", read_function="Z_RFC_READ_TABLE"
    )
    fake_sap.reverse_after = 1  # the second page comes back in another order: keys repeat
    try:
        rel = paged.resolve(DatasetSpec(table="KNA1"))
        with pytest.raises(ConnectorError, match="twice while paging"):
            paged.query(f"SELECT COUNT(*) FROM {rel.sql}")
    finally:
        paged.close()


def test_dd03l_description_and_foreign_keys(fake_sap: FakeSap, tmp_path: Path) -> None:
    con = connector(tmp_path, ddic_source="dd03l", foreign_keys=True)
    try:
        table = con.remote_table("KNA1")
        assert table is not None and table.ddic is not None
        ddic = table.ddic
        assert ddic.client_dependent is True and ddic.client_column == "MANDT"
        assert ddic.description == "Table KNA1"
        assert [f.name for f in ddic.fields][:3] == ["MANDT", "KUNNR", "NAME1"]  # .INCLUDE skipped
        assert ddic.field("NAME1") is not None and ddic.field("NAME1").description == "Name"  # type: ignore[union-attr]
        assert ddic.references == {"UMSAT": "WAERS"}
        assert ddic.foreign_keys[0].check_table == "T005"
        assert ddic.foreign_keys[0].columns == (("LAND1", "LAND1"),)
        assert con.list_datasets("KN*") == ["KNA1"]
        assert con.server_version() == "SAP S4D release 758"
        assert con.remote_table("NOPE") is None
        with pytest.raises(ConnectorError, match="not found"):
            con.get_schema(con.resolve(DatasetSpec(table="NOPE")))
    finally:
        con.close()


def test_query_datasets_are_refused(fake_sap: FakeSap, tmp_path: Path) -> None:
    con = connector(tmp_path)
    try:
        with pytest.raises(ConnectorError, match="not SQL queries"):
            con.get_schema(con.resolve(DatasetSpec(query="SELECT * FROM KNA1")))
        with pytest.raises(ConnectorError, match=r"options\.fields"):
            con.get_schema(con.resolve(DatasetSpec(table="KNA1", options={"fields": "KUNNR"})))
        with pytest.raises(ConnectorError, match="no field"):
            con.get_schema(con.resolve(DatasetSpec(table="KNA1", options={"fields": ["NOPE"]})))
    finally:
        con.close()


def test_comparison_with_sap_normalizers(fake_sap: FakeSap, tmp_path: Path) -> None:
    """KNA1 read through RFC against a typed copy: ALPHA keys, DATS, and the JPY shift."""
    target = tmp_path / "target.duckdb"
    rows = fake_sap.tables["KNA1"].rows
    with duckdb.connect(str(target)) as db:
        db.execute(
            "CREATE TABLE customer (customer_id VARCHAR, name VARCHAR, created DATE, "
            "sales DECIMAL(18, 3), currency VARCHAR)"
        )
        for row in rows:
            created = row["ERDAT"]
            shift = Decimal(100) if row["WAERS"] == "JPY" else Decimal(1)
            db.execute(
                "INSERT INTO customer VALUES (?, ?, ?, ?, ?)",
                [
                    row["KUNNR"].lstrip("0"),
                    row["NAME1"],
                    None
                    if created == "00000000"
                    else date.fromisoformat(f"{created[:4]}-{created[4:6]}-{created[6:]}"),
                    row["UMSAT"] * shift,
                    row["WAERS"],
                ],
            )
        db.execute("UPDATE customer SET name = 'Changed' WHERE customer_id = '6'")
    config = {
        "version": 1,
        "project": {"name": "sap-rfc"},
        "connections": {
            "sap": {
                "type": "sap_rfc",
                "ashost": "sap.example.test",
                "sysnr": "00",
                "client": "100",
                "user": "DVF_TEST",
                "password": "synthetic-not-a-secret",
                "page_size": 4,
            },
            "dw": {"type": "duckdb", "database": str(target)},
        },
        "suites": [
            {
                "name": "customers",
                "source": "sap",
                "target": "dw",
                "tables": [
                    {
                        "name": "kna1",
                        "source": {"table": "KNA1", "options": {"where": "MANDT = '100'"}},
                        "target": {"table": "customer"},
                        "keys": [
                            {"source": "KUNNR", "target": "customer_id", "normalize": ["sap_alpha"]}
                        ],
                        "columns": {
                            "include": ["NAME1", "ERDAT", "UMSAT"],
                            "mappings": [
                                {"source": "NAME1", "target": "name"},
                                {"source": "ERDAT", "target": "created", "normalize": ["sap_dats"]},
                                {
                                    "source": "UMSAT",
                                    "target": "sales",
                                    "normalize": [
                                        {
                                            "sap_curr": {
                                                "currency_column": "WAERS",
                                                "target_currency_column": "currency",
                                            }
                                        }
                                    ],
                                },
                            ],
                        },
                        "schema_checks": [],
                        "comparison": {"strategies": ["row_count", "full_diff"]},
                    }
                ],
            }
        ],
    }
    for engine in ("duckdb", "polars"):
        config["suites"][0]["tables"][0]["engine"] = engine  # type: ignore[index]
        loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
        out = api.run(loaded, write_reports=False, history=False)
        checks = {c.type: c for c in out.result.suites[0].datasets[0].checks}
        assert checks["compare.row_count"].status.value == "passed", engine
        mismatch = checks["compare.column_mismatch"]
        assert mismatch.status.value == "failed", engine
        assert [m.column for m in mismatch.column_mismatches] == ["name"], engine
        assert mismatch.column_mismatches[0].mismatch_count == 1, engine


# ---------------------------------------------------------------------- backends and config


def test_backend_configuration_is_validated() -> None:
    with pytest.raises(ValueError, match="unknown RFC backend 'nope'"):
        rfc_config(backend="nope")
    with pytest.raises(ValueError, match="set either 'ashost' or 'mshost'"):
        SapRfcConnection.model_validate({"type": "sap_rfc", "client": "100"})
    with pytest.raises(ValueError, match="read_function"):
        rfc_config(backend="custom_fm")
    with pytest.raises(ValueError, match="needs 'url'"):
        SapRfcConnection.model_validate({"type": "sap_rfc", "client": "100", "backend": "odata"})
    assert rfc_config(ashost=None, mshost="ms.example.test", sysid="S4D", group="PUBLIC").mshost
    names = backend_names()
    assert names["pyrfc"] == "ready" and names["odata"] == "ready"
    assert names["nwrfcsdk"] == "declared"


def test_nwrfcsdk_is_declared_not_implemented(tmp_path: Path) -> None:
    con = connector(tmp_path, backend="nwrfcsdk")
    with pytest.raises(NotImplementedError, match="declared but not implemented"):
        con.get_schema(con.resolve(DatasetSpec(table="KNA1")))
    report = con.test_connection()
    assert not report.ok and "not implemented" in report.message
    con.close()


def test_missing_pyrfc_explains_how_to_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "pyrfc", None)
    con = connector(tmp_path)
    with pytest.raises(SapDriverMissingError) as raised:
        con.get_schema(con.resolve(DatasetSpec(table="KNA1")))
    assert "NW RFC SDK" in (raised.value.hint or "")
    assert "does not install or bundle" in str(raised.value)
    con.close()


class TinyBackend(SapRfcBackend):
    """A plugin backend serving one in-memory table."""

    name = "tiny"

    def describe(self, table: str) -> RemoteTable | None:
        if table.upper() != "T":
            return None
        from dvf.core.types import CanonicalType

        return RemoteTable(
            "T", (RemoteColumn("ID", "INT4", CanonicalType.integer(32), pa.int32(), key=True),)
        )

    def read(self, table: RemoteTable, request: TableRead) -> Iterator[pa.RecordBatch]:
        yield pa.RecordBatch.from_pydict({"ID": pa.array([1, 2, 3], pa.int32())})


def test_plugin_backends_are_selectable(tmp_path: Path) -> None:
    register_rfc_backend("tiny", TinyBackend)
    try:
        con = connector(tmp_path, backend="tiny")
        rel = con.resolve(DatasetSpec(table="T"))
        assert con.get_primary_key(rel) == ("ID",)
        assert con.query(f"SELECT SUM(ID) AS s FROM {rel.sql}").to_pylist() == [{"s": 6}]
        with pytest.raises(NotImplementedError):
            con.call("RFC_PING")
        con.close()
    finally:
        backends_module._BACKENDS.pop("tiny", None)
    with pytest.raises(TypeError):
        register_rfc_backend("bad", object)  # type: ignore[arg-type]


def test_buffer_error_names_the_way_out(fake_sap: FakeSap, tmp_path: Path) -> None:
    # The function's real buffer is smaller than the configured row width: even the keys and one
    # field are rejected, and the error says what to do.
    fake_sap.read_functions["RFC_READ_TABLE"] = 20
    con = connector(tmp_path, row_width=600)
    try:
        spec = DatasetSpec(table="KNA1", options={"fields": ["MANDT", "KUNNR", "TEXT1"]})
        rel = con.resolve(spec)
        with pytest.raises(RfcBufferExceededError) as raised:
            con.query(f"SELECT COUNT(*) FROM {rel.sql}")
    finally:
        con.close()
    assert raised.value.key == "DATA_BUFFER_EXCEEDED"
    assert "custom_fm" in (raised.value.hint or "")
