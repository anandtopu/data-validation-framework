"""End to end: lakehouse and converted formats compared through dvf.api.run, and a landing-zone
suite of file-level checks with extracts, control records, checksums, and parallel tables."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import fastavro
import pyarrow as pa
import pyarrow.orc as orc
import pyarrow.parquet as pq
import pytest
from deltalake import write_deltalake

import dvf.packs
from dvf import api
from dvf.core.enums import Status
from dvf.models.results import CheckResult
from dvf.packs.files.formats import ensure_orc_time_zones
from dvf.plugins.registry import Registry, new_registry
from tests.unit.test_files_support import orders_table, write_iceberg


@pytest.fixture
def files_registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """The built-in registry with only the file pack loaded (other packs cannot interfere)."""
    monkeypatch.setattr(dvf.packs, "PACKS", ("dvf.packs.files",))
    return new_registry()


def _run(
    root: Path, registry: Registry, config: dict[str, Any], **options: Any
) -> tuple[dict[str, CheckResult], Any]:
    loaded = api.load_config_data(config, base_dir=root, strict=False, registry=registry)
    output = api.run(loaded, history=False, **options)
    results = {
        check.check_id: check
        for suite in output.result.suites
        for dataset in suite.datasets
        for check in dataset.checks
    }
    return results, output


def _lake(root: Path) -> None:
    """Delta, Iceberg, ORC, Avro, and Parquet copies of the same synthetic orders."""
    ensure_orc_time_zones()
    ids = list(range(1, 51))
    write_deltalake(str(root / "delta" / "orders"), orders_table(ids[:20]))
    write_deltalake(str(root / "delta" / "orders"), orders_table(ids[20:]), mode="append")
    write_iceberg(
        root / "iceberg", "sales.orders", [orders_table(ids[:20]), orders_table(ids[20:])]
    )
    orc.write_table(orders_table(ids), str(root / "orders.orc"))
    pq.write_table(orders_table(ids), root / "orders.parquet")
    pq.write_table(orders_table(ids[:20]), root / "first20.parquet")
    # The migrated copy: id 7 has a different amount, id 50 is missing, id 51 is new.
    changed = orders_table([*ids[:49], 51]).to_pylist()
    changed[6]["amount"] = Decimal("999.99")
    pq.write_table(
        pa.Table.from_pylist(changed, schema=orders_table([1]).schema), root / "migrated.parquet"
    )
    schema = {
        "type": "record",
        "name": "Order",
        "fields": [
            {"name": "id", "type": "long"},
            {"name": "customer", "type": "string"},
            {
                "name": "amount",
                "type": {"type": "bytes", "logicalType": "decimal", "precision": 12, "scale": 2},
            },
            {"name": "order_date", "type": {"type": "int", "logicalType": "date"}},
            {
                "name": "updated_at",
                "type": {"type": "long", "logicalType": "local-timestamp-micros"},
            },
        ],
    }
    with (root / "orders.avro").open("wb") as handle:
        fastavro.writer(handle, fastavro.parse_schema(schema), orders_table(ids).to_pylist())


def _comparison_config(root: Path) -> dict[str, Any]:
    def table(name: str, source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "source": source,
            "target": target,
            "keys": ["id"],
            "comparison": {"strategies": ["row_count", "aggregate", "full_diff"]},
        }

    return {
        "version": 1,
        "project": {"name": "lakehouse"},
        "connections": {
            "lake": {"type": "files", "path": root.as_posix()},
            "out": {"type": "files", "path": root.as_posix()},
        },
        "suites": [
            {
                "name": "lake",
                "source": "lake",
                "target": "out",
                "tables": [
                    table("delta_parquet", {"path": "delta/orders"}, {"path": "orders.parquet"}),
                    table(
                        "delta_v0",
                        {"path": "delta/orders", "as_of": "0"},
                        {"path": "first20.parquet"},
                    ),
                    table("delta_migrated", {"path": "delta/orders"}, {"path": "migrated.parquet"}),
                    table("iceberg_orc", {"path": "iceberg/sales/orders"}, {"path": "orders.orc"}),
                    table("avro_delta", {"path": "orders.avro"}, {"path": "delta/orders"}),
                ],
            }
        ],
    }


def test_lakehouse_comparisons(tmp_path: Path, files_registry: Registry) -> None:
    _lake(tmp_path)
    results, _ = _run(
        tmp_path, files_registry, _comparison_config(tmp_path), write_reports=False, parallel=3
    )
    for name in ("delta_parquet", "delta_v0", "iceberg_orc", "avro_delta"):
        for key, result in results.items():
            if key.startswith(f"lake.{name}.") and result.status is not Status.SKIPPED:
                assert result.status is Status.PASSED, (key, result.message)
    keys = results["lake.delta_migrated.compare.keys"]
    assert keys.status is Status.FAILED
    assert keys.observed["missing_in_target"] == 1 and keys.observed["extra_in_target"] == 1
    assert keys.message == "1 missing in target; 1 extra in target"
    columns = results["lake.delta_migrated.compare.column_mismatch"]
    assert columns.status is Status.FAILED
    assert [(m.column, m.mismatch_count) for m in columns.column_mismatches] == [("amount", 1)]
    assert results["lake.delta_migrated.compare.row_count"].status is Status.PASSED


def test_iceberg_time_travel_comparison(tmp_path: Path, files_registry: Registry) -> None:
    table, _ = write_iceberg(
        tmp_path / "iceberg", "sales.orders", [orders_table(list(range(1, 21))), orders_table([21])]
    )
    first = table.snapshots()[0].snapshot_id
    pq.write_table(orders_table(list(range(1, 21))), tmp_path / "first20.parquet")
    config = _comparison_config(tmp_path)
    config["suites"][0]["tables"] = [
        {
            "name": "snapshot",
            "source": {"path": "iceberg/sales/orders", "as_of": str(first)},
            "target": {"path": "first20.parquet"},
            "keys": ["id"],
            "comparison": {"strategies": ["row_count", "full_diff"]},
        },
        {
            "name": "current",
            "source": {"path": "iceberg/sales/orders"},
            "target": {"path": "first20.parquet"},
            "keys": ["id"],
            "comparison": {"strategies": ["row_count"]},
        },
    ]
    results, _ = _run(tmp_path, files_registry, config, write_reports=False)
    assert results["lake.snapshot.compare.row_count"].status is Status.PASSED
    assert results["lake.snapshot.compare.keys"].status is Status.PASSED
    assert results["lake.current.compare.row_count"].status is Status.FAILED


def _landing(root: Path) -> None:
    incoming = root / "incoming"
    incoming.mkdir(parents=True)
    rows = "\n".join(f"{i}|{i * 1.25:.2f}" for i in range(1, 6))
    (incoming / "orders_20260101.csv").write_bytes(
        f"H|20260101|ORDERS\nid|amount\n{rows}\nT|5|18.75\n".encode()
    )
    (incoming / "orders_20260102.csv").write_bytes(
        b"H|20260102|ORDERS\nid|amount\n6|1.00\n7|oops\n8|2.00\n9|1|2\nT|4|3.00\n"
    )
    manifest = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(incoming.glob("*.csv"))
    )
    (incoming / "MANIFEST.sha256").write_text(manifest, encoding="utf-8")
    archive = root / "archive"
    archive.mkdir()
    for path in incoming.glob("*.csv"):
        (archive / path.name).write_bytes(path.read_bytes())


def test_landing_zone_suite(tmp_path: Path, files_registry: Registry) -> None:
    _landing(tmp_path)
    reader = {
        "delim": "|",
        "skip": 1,
        "skip_footer": 1,
        "header": True,
        "columns": {"id": "INTEGER", "amount": "DECIMAL(10,2)"},
        "ignore_errors": True,
    }
    trailer = {"pattern": r"^T\|(?P<count>\d+)\|(?P<total>[\d.]+)$"}
    config = {
        "version": 1,
        "project": {"name": "landing"},
        "connections": {
            "landing": {"type": "files", "path": tmp_path.as_posix()},
            "archive": {"type": "files", "path": (tmp_path / "archive").as_posix()},
        },
        "reports": {"extracts": {"enabled": True, "format": "csv", "max_rows_per_check": 1000}},
        "suites": [
            {
                "name": "landing",
                "source": "landing",
                "tables": [
                    {
                        "name": "orders",
                        "sample_size": 1,
                        "source": {"path": "incoming/orders_*.csv", "options": reader},
                        "checks": [
                            {"type": "file_exists"},
                            {"type": "file_count", "min": 1, "max": 31},
                            {"type": "file_name_pattern", "pattern": r"orders_\d{8}\.csv"},
                            {"type": "file_size", "min_bytes": 10},
                            {"type": "file_freshness", "max_age": "1d"},
                            {"type": "file_checksum", "manifest": "incoming/MANIFEST.sha256"},
                            {
                                "type": "file_checksum",
                                "name": "archived",
                                "compare_to": {"connection": "archive", "path": "*.csv"},
                            },
                            {
                                "type": "control_totals",
                                "trailer": trailer,
                                "record_count": "count",
                                "sums": {"amount": "total"},
                            },
                            {
                                "type": "schema_on_read",
                                "columns": {"id": "INTEGER", "amount": "DECIMAL(10,2)"},
                            },
                            {"type": "bad_records"},
                        ],
                    }
                ],
            }
        ],
    }
    results, output = _run(tmp_path, files_registry, config, output_dir=tmp_path / "reports")
    status = {k.removeprefix("landing.orders."): r.status for k, r in results.items()}
    assert status == {
        "schema.object_exists": Status.PASSED,
        "file_exists": Status.PASSED,
        "file_count": Status.PASSED,
        "file_name_pattern": Status.PASSED,
        "file_size": Status.PASSED,
        "file_freshness": Status.PASSED,
        "file_checksum": Status.PASSED,
        "file_checksum.archived": Status.PASSED,
        # The second file's trailer counts its two unreadable rows: 9 records announced, 7 read.
        "control_totals": Status.FAILED,
        "schema_on_read": Status.PASSED,
        "bad_records": Status.FAILED,
    }
    control = results["landing.orders.control_totals"]
    assert control.message is not None and "9 records, the data has 7" in control.message
    bad = results["landing.orders.bad_records"]
    assert bad.unexpected_count == 2 and bad.samples is not None
    assert bad.samples.rows == [
        ["incoming/orders_20260102.csv", 4, "amount", "CAST", bad.samples.rows[0][4], "7|oops"]
    ]
    # The samples hold one row (sample_size); the extract holds every rejected row.
    extract = next(a for a in bad.artifacts if a.kind == "extract")
    assert output.output_dir is not None
    text = (output.output_dir / extract.path).read_text(encoding="utf-8")
    assert "7|oops" in text and "9|1|2" in text and "TOO MANY COLUMNS" in text


def test_file_exists_on_a_dataset_without_files(tmp_path: Path, files_registry: Registry) -> None:
    config = {
        "version": 1,
        "project": {"name": "landing"},
        "connections": {"landing": {"type": "files", "path": tmp_path.as_posix()}},
        "suites": [
            {
                "name": "landing",
                "source": "landing",
                "tables": [
                    {
                        "name": "orders",
                        "source": {"path": "incoming/orders_${run.date}*.csv"},
                        "checks": [{"type": "file_exists"}],
                    }
                ],
            }
        ],
    }
    results, _ = _run(tmp_path, files_registry, config, write_reports=False)
    assert results["landing.orders.file_exists"].status is Status.FAILED
