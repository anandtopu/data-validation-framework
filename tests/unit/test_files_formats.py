"""File formats of the files connector: ORC, Delta, Iceberg, Avro, Excel, fixed width, XML,
compressed and transcoded text, Hive-style folders, time travel, forks, and fsspec URLs."""

from __future__ import annotations

import bz2
import gzip
import lzma
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import fastavro
import fsspec
import openpyxl
import pyarrow as pa
import pyarrow.orc as orc
import pyarrow.parquet as pq
import pytest
import zstandard
from deltalake import DeltaTable, write_deltalake

from dvf.config.connections import DuckDBConnection, FileConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.duckdb import DuckDBConnector, FileConnector
from dvf.core.errors import ConnectorError
from dvf.core.types import LogicalType
from dvf.packs.files import formats, storage
from tests.unit.test_files_support import JsonFileCatalog, iso_ms, orders_table, write_iceberg

formats.ensure_orc_time_zones()


def _connector(root: Path, **options: Any) -> FileConnector:
    return FileConnector(
        "files", FileConnection(type="files", path=root.as_posix(), **options), root
    )


def _rows(con: FileConnector, spec: DatasetSpec, order: str = "id") -> list[dict[str, Any]]:
    rel = con.resolve(spec)
    return con.query(f"SELECT * FROM {rel.sql} ORDER BY {order}").to_pylist()


def _types(con: FileConnector, spec: DatasetSpec) -> dict[str, LogicalType]:
    schema = con.get_schema(con.resolve(spec))
    return {c.name: c.type.logical for c in schema.columns}


# ============================================================================ ORC


def test_orc_file_and_hive_folder(tmp_path: Path) -> None:
    orc.write_table(orders_table([1, 2, 3]), str(tmp_path / "orders.orc"))
    for region, ids in (("eu", [1, 2]), ("us", [3])):
        folder = tmp_path / "parts" / f"region={region}"
        folder.mkdir(parents=True)
        orc.write_table(orders_table(ids), str(folder / "part-0.orc"))
    with _connector(tmp_path) as con:
        types = _types(con, DatasetSpec(path="orders.orc"))
        assert types == {
            "id": LogicalType.INTEGER,
            "customer": LogicalType.STRING,
            "amount": LogicalType.DECIMAL,
            "order_date": LogicalType.DATE,
            "updated_at": LogicalType.TIMESTAMP,
        }
        rows = _rows(con, DatasetSpec(path="orders.orc", filter="amount > 3"))
        assert [r["id"] for r in rows] == [2, 3]
        spec = DatasetSpec(path="parts", options={"format": "orc"}, filter="region = 'eu'")
        assert [r["region"] for r in _rows(con, spec)] == ["eu", "eu"]
        rel = con.resolve(DatasetSpec(path="parts", options={"format": "orc"}))
        assert [f.name for f in con.list_files(rel)] == [
            "parts/region=eu/part-0.orc",
            "parts/region=us/part-0.orc",
        ]
        with pytest.raises(ConnectorError, match="Invalid options for the 'orc' format"):
            con.resolve(DatasetSpec(path="orders.orc", options={"sheet": 1}))


# ============================================================================ Delta Lake


def _delta(tmp_path: Path) -> tuple[Path, str]:
    location = tmp_path / "lake" / "orders"
    write_deltalake(str(location), orders_table([1, 2, 3]), partition_by=["customer"])
    marker = datetime.now(UTC)
    write_deltalake(str(location), orders_table([4]), mode="append", partition_by=["customer"])
    write_deltalake(
        str(location), orders_table([1, 2], shift=1), mode="overwrite", partition_by=["customer"]
    )
    return location, marker.isoformat()


def test_delta_versions_timestamps_and_partitions(tmp_path: Path) -> None:
    location, between = _delta(tmp_path)
    with _connector(tmp_path) as con:
        current = DatasetSpec(path="lake/orders")
        assert [r["id"] for r in _rows(con, current)] == [1, 2]
        assert [r["id"] for r in _rows(con, DatasetSpec(path="lake/orders", as_of="0"))] == [
            1,
            2,
            3,
        ]
        assert [r["id"] for r in _rows(con, DatasetSpec(path="lake/orders", as_of="1"))] == [
            1,
            2,
            3,
            4,
        ]
        # Between version 0 and version 1: version 0.
        rows = _rows(con, DatasetSpec(path="lake/orders", as_of=between))
        assert [r["id"] for r in rows] == [1, 2, 3]
        types = _types(con, current)
        assert types["customer"] is LogicalType.STRING  # partition column
        assert types["amount"] is LogicalType.DECIMAL
        rel = con.resolve(DatasetSpec(path="lake/orders", filter="customer = 'c1'"))
        assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").column("n")[0].as_py() == 1
        files = con.list_files(con.resolve(DatasetSpec(path="lake/orders", as_of="1")))
        assert len(files) == 4 and all(f.size > 0 and f.modified for f in files)
        assert all("customer=" in f.name for f in files)
        assert con.exists(con.resolve(current))
        assert not con.exists(con.resolve(DatasetSpec(path="missing", options={"format": "delta"})))
        with pytest.raises(ConnectorError, match=r"no version|Could not open"):
            _rows(con, DatasetSpec(path="lake/orders", as_of="99"))
        with pytest.raises(ConnectorError, match=r"Invalid 'as_of'"):
            con.resolve(DatasetSpec(path="lake/orders", as_of="yesterday-ish"))
            _rows(con, DatasetSpec(path="lake/orders", as_of="yesterday-ish"))
    assert location.exists()


def test_delta_with_deletion_vectors_is_staged(tmp_path: Path) -> None:
    location = tmp_path / "dv"
    config = {"delta.enableDeletionVectors": "true"}
    write_deltalake(str(location), orders_table([1, 2, 3]), configuration=config)
    DeltaTable(str(location)).delete("id = 2")
    with _connector(tmp_path) as con:
        spec = DatasetSpec(path="dv")
        assert [r["id"] for r in _rows(con, spec)] == [1, 3]
        assert [r["id"] for r in _rows(con, DatasetSpec(path="dv", as_of="0"))] == [1, 2, 3]
        assert _types(con, spec)["amount"] is LogicalType.DECIMAL
        assert con.list_files(con.resolve(spec))


def test_as_of_only_for_lakehouse_tables(tmp_path: Path) -> None:
    pq.write_table(orders_table([1]), tmp_path / "a.parquet")
    with _connector(tmp_path) as con, pytest.raises(ConnectorError, match="as_of"):
        con.resolve(DatasetSpec(path="a.parquet", as_of="2026-01-01"))
    duck = DuckDBConnector("d", DuckDBConnection(type="duckdb"), tmp_path)
    with duck, pytest.raises(ConnectorError, match="files"):
        duck.resolve(DatasetSpec(path="x.avro", options={"format": "avro"}))


# ============================================================================ Iceberg


def test_iceberg_static_table_and_time_travel(tmp_path: Path) -> None:
    table, stamps = write_iceberg(
        tmp_path / "warehouse", "sales.orders", [orders_table([1, 2]), orders_table([3])]
    )
    folder = tmp_path / "warehouse" / "sales" / "orders"
    snapshots = [s.snapshot_id for s in table.snapshots()]
    with _connector(tmp_path) as con:
        # A table folder (newest metadata) and an explicit metadata file.
        assert [r["id"] for r in _rows(con, DatasetSpec(path="warehouse/sales/orders"))] == [
            1,
            2,
            3,
        ]
        metadata = sorted((folder / "metadata").glob("*.metadata.json"))[-1]
        spec = DatasetSpec(path=metadata.relative_to(tmp_path).as_posix())
        assert len(_rows(con, spec)) == 3
        first = DatasetSpec(path="warehouse/sales/orders", as_of=str(snapshots[0]))
        assert [r["id"] for r in _rows(con, first)] == [1, 2]
        at = DatasetSpec(path="warehouse/sales/orders", as_of=iso_ms(stamps[0] + 1))
        assert [r["id"] for r in _rows(con, at)] == [1, 2]
        types = _types(con, first)
        assert (
            types["amount"] is LogicalType.DECIMAL and types["updated_at"] is LogicalType.TIMESTAMP
        )
        files = con.list_files(con.resolve(first))
        assert len(files) == 1 and files[0].name.startswith("data/") and files[0].modified
        with pytest.raises(ConnectorError, match="snapshot 42 not found"):
            _rows(con, DatasetSpec(path="warehouse/sales/orders", as_of="42"))
        with pytest.raises(ConnectorError, match="no snapshot at or before"):
            _rows(con, DatasetSpec(path="warehouse/sales/orders", as_of="2000-01-01T00:00:00"))
        assert not con.exists(
            con.resolve(DatasetSpec(path="nowhere", options={"format": "iceberg"}))
        )
        filtered = DatasetSpec(path="warehouse/sales/orders", options={"row_filter": "id >= 2"})
        assert [r["id"] for r in _rows(con, filtered)] == [2, 3]


def test_iceberg_catalog_and_staged_scan(tmp_path: Path) -> None:
    table, _ = write_iceberg(tmp_path / "wh", "sales.orders", [orders_table([1, 2])])
    with table.update_schema() as update:  # schema evolution forces the staged (exact) path
        update.add_column("channel", pa_string_type())
    catalog = JsonFileCatalog("tests", warehouse=str(tmp_path / "wh"))
    evolved = catalog.load_table("sales.orders")
    evolved.append(orders_table([3]).append_column("channel", pa.array(["web"], pa.large_string())))
    options = {
        "format": "iceberg",
        "catalog": {
            "name": "tests",
            "py-catalog-impl": "tests.unit.test_files_support.JsonFileCatalog",
            "warehouse": str(tmp_path / "wh"),
        },
    }
    with _connector(tmp_path) as con:
        spec = DatasetSpec(table="sales.orders", options=options)
        rows = _rows(con, spec)
        assert [(r["id"], r["channel"]) for r in rows] == [(1, None), (2, None), (3, "web")]
        rel = con.resolve(spec)
        assert con.exists(rel)
        assert len(con.list_files(rel)) == 2
        missing = con.resolve(DatasetSpec(table="sales.nothing", options=options))
        assert not con.exists(missing)
        bad = {"format": "iceberg", "catalog": {"type": "sql", "uri": "sqlite:///:memory:"}}
        with pytest.raises(ConnectorError, match="Iceberg catalog"):
            _rows(con, DatasetSpec(table="sales.orders", options=bad))


def pa_string_type() -> Any:
    from pyiceberg.types import StringType

    return StringType()


# ============================================================================ Avro


def test_avro_types(tmp_path: Path) -> None:
    schema = {
        "type": "record",
        "name": "Order",
        "fields": [
            {"name": "id", "type": "long"},
            {"name": "note", "type": ["null", "string"], "default": None},
            {
                "name": "amount",
                "type": {"type": "bytes", "logicalType": "decimal", "precision": 12, "scale": 2},
            },
            {"name": "booked", "type": {"type": "int", "logicalType": "date"}},
            {"name": "at", "type": {"type": "long", "logicalType": "timestamp-micros"}},
            {"name": "local", "type": {"type": "long", "logicalType": "local-timestamp-millis"}},
            {"name": "ref", "type": {"type": "string", "logicalType": "uuid"}},
            {"name": "tags", "type": {"type": "array", "items": "string"}},
            {"name": "attrs", "type": {"type": "map", "values": "long"}},
            {"name": "status", "type": {"type": "enum", "name": "S", "symbols": ["OPEN", "DONE"]}},
            {"name": "either", "type": ["null", "long", "string"]},
            {
                "name": "address",
                "type": {
                    "type": "record",
                    "name": "A",
                    "fields": [{"name": "city", "type": "string"}],
                },
            },
        ],
    }
    records = [
        {
            "id": i,
            "note": None if i == 2 else f"n{i}",
            "amount": Decimal("12.50") * i,
            "booked": date(2026, 1, i),
            "at": datetime(2026, 1, i, 10, tzinfo=UTC),
            "local": datetime(2026, 1, i, 10),
            "ref": str(uuid.UUID(int=i)),
            "tags": ["a", "b"][:i],
            "attrs": {"k": i},
            "status": "OPEN",
            "either": i if i % 2 else "two",
            "address": {"city": "Oslo"},
        }
        for i in (1, 2)
    ]
    with (tmp_path / "orders.avro").open("wb") as handle:
        fastavro.writer(handle, fastavro.parse_schema(schema), records, codec="deflate")
    with _connector(tmp_path) as con:
        spec = DatasetSpec(path="orders.avro")
        types = _types(con, spec)
        assert types["amount"] is LogicalType.DECIMAL
        assert types["booked"] is LogicalType.DATE
        assert types["at"] is LogicalType.TIMESTAMP_TZ
        assert types["local"] is LogicalType.TIMESTAMP
        assert types["tags"] is LogicalType.ARRAY and types["attrs"] is LogicalType.MAP
        assert types["address"] is LogicalType.STRUCT
        rows = _rows(con, spec)
        assert rows[0]["amount"] == Decimal("12.50") and rows[1]["note"] is None
        assert rows[0]["ref"] == "00000000-0000-0000-0000-000000000001"
        assert rows[0]["either"] == "1" and rows[1]["either"] == "two"
        typed = DatasetSpec(path="orders.avro", options={"types": {"either": "VARCHAR"}})
        assert len(_rows(con, typed)) == 2


# ============================================================================ Excel


def _workbook(path: Path) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Summary"
    sheet.append(["ignored"])
    data = book.create_sheet("Orders")
    data.append(["Orders report"])
    data.append([])
    data.append([None, "id", "customer", "amount", "booked", "flag"])
    data.append([None, 1, "c1", 10.5, date(2026, 1, 1), True])
    data.append([None, 2, "c2", 20, date(2026, 1, 2), False])
    data.append([None, 3, "c3", "#DIV/0!", date(2026, 1, 3), True])
    data.append([])
    data.append([None, "Total", None, 30.5, None, None])
    for row in data.iter_rows(min_row=4, max_row=6, min_col=5, max_col=5):
        row[0].number_format = "yyyy-mm-dd"
    book.save(path)


def test_excel_sheet_header_and_range(tmp_path: Path) -> None:
    _workbook(tmp_path / "orders.xlsx")
    with _connector(tmp_path) as con:
        spec = DatasetSpec(path="orders.xlsx", options={"sheet": "Orders", "cell_range": "B3:F6"})
        types = _types(con, spec)
        assert types == {
            "id": LogicalType.INTEGER,
            "customer": LogicalType.STRING,
            "amount": LogicalType.FLOAT,
            "booked": LogicalType.DATE,
            "flag": LogicalType.BOOLEAN,
        }
        rows = _rows(con, spec)
        assert [r["amount"] for r in rows] == [10.5, 20.0, None]  # error cells read as NULL
        assert rows[0]["booked"] == date(2026, 1, 1)
        by_index = DatasetSpec(path="orders.xlsx", options={"sheet": 1, "header_row": 3})
        rows = _rows(con, by_index, order="customer NULLS LAST")
        assert len(rows) == 4 and rows[-1]["id"] == "Total"  # mixed column -> text
        no_header = DatasetSpec(
            path="orders.xlsx", options={"sheet": "Orders", "cell_range": "B4:C5", "header_row": 0}
        )
        assert _rows(con, no_header, order="B") == [{"B": 1, "C": "c1"}, {"B": 2, "C": "c2"}]
        typed = DatasetSpec(
            path="orders.xlsx",
            options={
                "sheet": "Orders",
                "cell_range": "B3:F6",
                "types": {"amount": "DECIMAL(10,2)"},
            },
        )
        assert _rows(con, typed)[0]["amount"] == Decimal("10.50")
        with pytest.raises(ConnectorError, match="no sheet 'Nope'"):
            _rows(con, DatasetSpec(path="orders.xlsx", options={"sheet": "Nope"}))
        with pytest.raises(ConnectorError, match="Invalid options for the 'excel' format"):
            con.resolve(DatasetSpec(path="orders.xlsx", options={"shet": "Orders"}))


# ============================================================================ fixed width


def test_fixed_width_layouts(tmp_path: Path) -> None:
    text = "HDR20260101\n00001Ann       0001050\n00002Bjørn     0002000\n\n00003Cy\nTRL000003\n"
    (tmp_path / "orders.dat").write_text(text, encoding="cp1252")
    columns = [
        {"name": "id", "start": 1, "width": 5, "type": "INTEGER"},
        {"name": "name", "start": 6, "width": 10},
        {"name": "amount", "start": 16, "width": 7, "type": "DECIMAL(9,2)"},
    ]
    base = {"format": "fixed_width", "encoding": "cp1252", "skip_header": 1, "skip_footer": 1}
    with _connector(tmp_path) as con:
        spec = DatasetSpec(path="orders.dat", options={**base, "columns": columns})
        rows = _rows(con, spec)
        assert [(r["id"], r["name"], r["amount"]) for r in rows] == [
            (1, "Ann", Decimal("1050.00")),
            (2, "Bjørn", Decimal("2000.00")),
            (3, "Cy", None),
        ]
        widths = DatasetSpec(
            path="orders.dat",
            options={
                "format": "fixed_width",
                "encoding": "cp1252",
                "widths": [5, 10, 7],
                "names": ["id", "name", "amount"],
                "skip_pattern": "^(HDR|TRL)",
                "trim": "none",
            },
        )
        rows = _rows(con, widths)
        assert rows[0]["name"] == "Ann       " and len(rows) == 3
        strict = DatasetSpec(
            path="orders.dat", options={**base, "columns": columns, "strict": True}
        )
        with pytest.raises(ConnectorError, match=r"Line 5 of 'orders\.dat'"):
            _rows(con, strict)
        lenient = DatasetSpec(
            path="orders.dat",
            options={**base, "columns": columns, "strict": True, "ignore_errors": True},
        )
        assert [r["id"] for r in _rows(con, lenient)] == [1, 2]
        with pytest.raises(ConnectorError, match="either 'columns' or 'widths'"):
            con.resolve(DatasetSpec(path="orders.dat", options={"format": "fixed_width"}))


def test_fixed_length_ebcdic_records(tmp_path: Path) -> None:
    records = ["00001ALPHA", "00002BETA "]
    (tmp_path / "mainframe.bin").write_bytes("".join(records).encode("cp037"))
    with _connector(tmp_path) as con:
        spec = DatasetSpec(
            path="mainframe.bin",
            options={
                "format": "fixed_width",
                "encoding": "cp037",
                "record_length": 10,
                "widths": [5, 5],
                "names": ["id", "name"],
                "types": {"id": "INTEGER"},
            },
        )
        assert _rows(con, spec) == [{"id": 1, "name": "ALPHA"}, {"id": 2, "name": "BETA"}]


# ============================================================================ XML


_XML = """<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="urn:example:orders">
  <meta><created>2026-01-01</created></meta>
  <orders>
    <order id="1" status="open"><customer><name>Ann</name><id>c1</id></customer><amount>10.50</amount></order>
    <order id="2" status="done"><customer><name>Bob</name></customer><amount>20</amount><note></note></order>
  </orders>
  <archive><order id="99"><amount>1</amount></order></archive>
</export>
"""


def test_xml_rows_fields_and_types(tmp_path: Path) -> None:
    (tmp_path / "orders.xml").write_text(_XML, encoding="utf-8")
    (tmp_path / "orders.xml.gz").write_bytes(gzip.compress(_XML.encode()))
    with _connector(tmp_path) as con:
        auto = DatasetSpec(path="orders.xml", options={"row_path": "orders/order"})
        rows = _rows(con, auto)
        assert [r["id"] for r in rows] == ["1", "2"]
        assert set(rows[0]) == {"id", "status", "customer", "amount", "note"}
        assert rows[1]["note"] is None  # empty element -> NULL
        mapped = DatasetSpec(
            path="orders.xml.gz",
            options={
                "format": "xml",
                "row_path": "order",
                "fields": {
                    "id": "@id",
                    "name": "customer/name",
                    "cid": "customer/id",
                    "amount": "amount",
                },
                "types": {"id": "INTEGER", "amount": "DECIMAL(10,2)"},
            },
        )
        rows = _rows(con, mapped)
        assert [(r["id"], r["name"], r["cid"], r["amount"]) for r in rows] == [
            (1, "Ann", "c1", Decimal("10.50")),
            (2, "Bob", None, Decimal("20.00")),
            (99, None, None, Decimal("1.00")),
        ]
        anchored = DatasetSpec(path="orders.xml", options={"row_path": "/export/orders/order"})
        assert len(_rows(con, anchored)) == 2
        (tmp_path / "broken.xml").write_text("<a><b>1</a>", encoding="utf-8")
        with pytest.raises(ConnectorError, match="not well-formed"):
            _rows(con, DatasetSpec(path="broken.xml", options={"row_path": "b"}), order="1")


# ============================================================================ text formats


def test_compressed_transcoded_and_trailer_csv(tmp_path: Path) -> None:
    body = "id,name\n1,José\n2,Zoë\n"
    (tmp_path / "a.csv.gz").write_bytes(gzip.compress(body.encode()))
    (tmp_path / "b.csv.zst").write_bytes(zstandard.ZstdCompressor().compress(body.encode()))
    (tmp_path / "c.csv.bz2").write_bytes(bz2.compress(body.encode()))
    (tmp_path / "d.csv.xz").write_bytes(lzma.compress(body.encode()))
    (tmp_path / "e.csv").write_bytes(body.encode("cp1252"))
    (tmp_path / "f.csv").write_text(body + "TRAILER,2\n", encoding="utf-8")
    (tmp_path / "g.ndjson.bz2").write_bytes(bz2.compress(b'{"id": 1}\n{"id": 2}\n'))
    with _connector(tmp_path) as con:
        for spec in (
            DatasetSpec(path="a.csv.gz"),
            DatasetSpec(path="b.csv.zst"),
            DatasetSpec(path="c.csv.bz2"),
            DatasetSpec(path="d.csv.xz"),
            DatasetSpec(path="e.csv", options={"encoding": "cp1252"}),
            DatasetSpec(path="f.csv", options={"skip_footer": 1}),
        ):
            assert [r["name"] for r in _rows(con, spec)] == ["José", "Zoë"], spec.path
        assert len(_rows(con, DatasetSpec(path="g.ndjson.bz2"))) == 2


def test_hive_folders_and_directories(tmp_path: Path) -> None:
    for year, ids in (("2025", [1, 2]), ("2026", [3])):
        folder = tmp_path / "sales" / f"year={year}"
        folder.mkdir(parents=True)
        pq.write_table(orders_table(ids), folder / "part-0.parquet")
        (folder / "part-0.csv").write_text(
            "id\n" + "\n".join(map(str, ids)) + "\n", encoding="utf-8"
        )
    (tmp_path / "sales" / "_SUCCESS").write_text("", encoding="utf-8")
    (tmp_path / "sales" / "year=2025" / ".part-0.parquet.crc").write_bytes(b"x")
    with _connector(tmp_path) as con:
        folder = DatasetSpec(path="sales", options={"format": "parquet"})
        rows = _rows(con, folder)
        assert [(r["id"], r["year"]) for r in rows] == [(1, 2025), (2, 2025), (3, 2026)]
        glob = DatasetSpec(
            path="sales/*/*.csv", options={"hive_partitioning": True}, filter="year = 2026"
        )
        assert [r["id"] for r in _rows(con, glob)] == [3]
        # A folder dataset holds the files of its format; a pattern lists every matching file.
        assert [f.name for f in con.list_files(con.resolve(folder))] == [
            "sales/year=2025/part-0.parquet",
            "sales/year=2026/part-0.parquet",
        ]
        assert len(con.list_files(pattern="sales")) == 4
        assert [f.name for f in con.list_files(pattern="sales/**/*.parquet")] == [
            "sales/year=2025/part-0.parquet",
            "sales/year=2026/part-0.parquet",
        ]
        assert not con.exists(con.resolve(DatasetSpec(path="sales/*.orc")))
        with pytest.raises(ConnectorError, match="Unknown file format"):
            con.resolve(DatasetSpec(path="sales", options={"format": "dbf"}))


def test_format_detection() -> None:
    assert formats.detect_format("a/b.avro") == "avro"
    assert formats.detect_format("x.XLSX") == "excel"
    assert formats.detect_format("x.xml.gz") == "xml"
    assert formats.detect_format("s3://b/t/metadata/v2.metadata.json") == "iceberg"
    assert formats.detect_format("x.unknown") is None
    assert formats.parse_as_of(" 12 ") == 12
    assert formats.parse_as_of("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert formats.parse_as_of("2026-01-01T01:00:00+01:00").astimezone(UTC).hour == 0


# ============================================================================ sessions


def test_forks_share_prepared_data_and_close_cleans_up(tmp_path: Path) -> None:
    orc.write_table(orders_table(list(range(1, 101))), str(tmp_path / "orders.orc"))
    (tmp_path / "orders.xml").write_text(_XML, encoding="utf-8")
    con = _connector(tmp_path)
    con.temp_root = str(tmp_path)
    con.open()
    rel_orc = con.resolve(DatasetSpec(path="orders.orc"))
    rel_xml = con.resolve(DatasetSpec(path="orders.xml", options={"row_path": "order"}))
    forks = [con.fork() for _ in range(4)]
    counts: list[int] = []
    errors: list[BaseException] = []

    def work(session: Any) -> None:
        try:
            for rel in (rel_orc, rel_xml):
                counts.append(
                    session.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").column("n")[0].as_py()
                )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s,)) for s in forks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and sorted(set(counts)) == [3, 100]
    folders = list(tmp_path.glob("dvf-files-*"))
    assert len(folders) == 1 and any(folders[0].rglob("*.parquet"))
    for session in forks:
        session.close()
    assert folders[0].exists()  # forks never delete the shared folder
    con.close()
    assert not folders[0].exists()


def test_fsspec_urls(tmp_path: Path) -> None:
    fs = fsspec.filesystem("memory")
    root = f"memory://dvf-files-{uuid.uuid4().hex[:8]}"
    try:
        with fs.open(f"{root}/in/a.csv", "wb") as handle:
            handle.write(b"id,name\n1,a\n2,b\n")
        with fs.open(f"{root}/in/b.xml", "wb") as handle:
            handle.write(b"<r><row id='1'/></r>")
        con = FileConnector("mem", FileConnection(type="files", path=root), tmp_path)
        with con:
            rel = con.resolve(DatasetSpec(path="in/a.csv"))
            assert con.exists(rel)
            assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").column("n")[0].as_py() == 2
            files = con.list_files(pattern="in/*")
            assert [f.name for f in files] == ["in/a.csv", "in/b.xml"] and files[0].size == 16
            xml = con.resolve(DatasetSpec(path="in/b.xml", options={"row_path": "row"}))
            assert con.query(f"SELECT id FROM {xml.sql}").to_pylist() == [{"id": "1"}]
            assert con.list_datasets("in/*.csv") == ["in/a.csv"]
            assert not con.exists(con.resolve(DatasetSpec(path="in/none.csv")))
    finally:
        fs.rm(root.removeprefix("memory://"), recursive=True)


def test_storage_helpers(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"H1\nrow\nrow\nT2\n\n")
    first, last, count = storage.head_and_tail((tmp_path / "a.txt").as_posix(), 1, 1)
    assert (first, last, count) == (["H1"], ["T2"], 4)
    assert storage.is_data_file("x/part-0.parquet") and not storage.is_data_file(
        "_delta_log/0.json"
    )
    assert (
        storage.compression_of("A.CSV.GZ") == "gzip"
        and storage.strip_compression("a.csv.zst") == "a.csv"
    )
    assert storage.list_files((tmp_path / "missing").as_posix(), tmp_path.as_posix()) == []
    info = storage.list_files((tmp_path / "a.txt").as_posix(), tmp_path.as_posix())[0]
    assert info.basename == "a.txt" and info.as_dict()["size"] == 15
    assert info.modified is not None and info.modified > datetime.now(UTC) - timedelta(minutes=5)
    with pytest.raises(ConnectorError, match="No fsspec implementation"):
        storage.list_files("nosuchproto://bucket/x", "nosuchproto://bucket")
    duckdb.connect().close()
