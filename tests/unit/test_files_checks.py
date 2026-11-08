"""File-level checks: configuration models, helpers, and each check through a run."""

from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

import dvf.packs
from dvf import api
from dvf.core.enums import Status
from dvf.models.results import CheckResult
from dvf.packs.files import config as cfg
from dvf.packs.files.checks import FILE_CHECKS, _control_number, _parse_manifest
from dvf.plugins.registry import PluginKind, Registry, new_registry


@pytest.fixture
def files_registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """The built-in registry with only the file pack loaded (other packs cannot interfere)."""
    monkeypatch.setattr(dvf.packs, "PACKS", ("dvf.packs.files",))
    return new_registry()


def run_checks(
    root: Path,
    registry: Registry,
    tables: list[dict[str, Any]],
    *,
    target: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, CheckResult]:
    """Run one suite over a files connection rooted at ``root``; results by check ID."""
    suite: dict[str, Any] = {"name": "s", "source": "files", "tables": tables}
    if target:
        suite["target"] = "files"
    config: dict[str, Any] = {
        "version": 1,
        "project": {"name": "files-tests"},
        "connections": {"files": {"type": "files", "path": root.as_posix()}},
        "suites": [suite],
        **(extra or {}),
    }
    loaded = api.load_config_data(config, base_dir=root, strict=False, registry=registry)
    output = api.run(loaded, write_reports=False, history=False)
    return {
        check.check_id: check
        for suite_result in output.result.suites
        for dataset in suite_result.datasets
        for check in dataset.checks
    }


# ============================================================================ models


def test_check_models_validate() -> None:
    with pytest.raises(ValidationError, match="'min' must not exceed 'max'"):
        cfg.FileCountCheck(type="file_count", min=3, max=1)
    with pytest.raises(ValidationError, match="exactly one of"):
        cfg.FileChecksumCheck(type="file_checksum")
    with pytest.raises(ValidationError, match="exactly one of"):
        cfg.FileChecksumCheck(type="file_checksum", sidecar=True, manifest="m.txt")
    with pytest.raises(ValidationError, match="hexadecimal"):
        cfg.FileChecksumCheck(type="file_checksum", expected={"a.csv": "xyz"})
    with pytest.raises(ValidationError, match="named groups"):
        cfg.ControlRecord(pattern=r"^T(\d+)$")
    with pytest.raises(ValidationError, match="either 'pattern' or 'fields'"):
        cfg.ControlRecord()
    with pytest.raises(ValidationError, match="not defined"):
        cfg.ControlTotalsCheck(
            type="control_totals",
            trailer=cfg.ControlRecord(pattern=r"(?P<n>\d+)"),
            record_count="count",
        )
    with pytest.raises(ValidationError, match="invalid regular expression"):
        cfg.FileNamePatternCheck(type="file_name_pattern", pattern="(")
    with pytest.raises(ValidationError, match="either 'columns' or 'schema_file'"):
        cfg.SchemaOnReadCheck(type="schema_on_read")
    shorthand = cfg.SchemaOnReadCheck.model_validate(
        {"type": "schema_on_read", "columns": {"id": "INTEGER", "name": None}}
    )
    assert [(c.name, c.type) for c in shorthand.columns] == [("id", "INTEGER"), ("name", None)]
    assert shorthand.target_columns() == []
    names = cfg.SchemaOnReadCheck.model_validate({"type": "schema_on_read", "columns": ["a", "b"]})
    assert [c.name for c in names.columns] == ["a", "b"]


def test_helpers() -> None:
    sha = "a" * 64
    assert _parse_manifest(f"{sha}  in/a.csv\n{sha} *b.csv\n") == {"in/a.csv": sha, "b.csv": sha}
    assert _parse_manifest(f"file,sha256\na.csv,{sha.upper()}\n") == {"a.csv": sha}
    assert _parse_manifest(f"{sha},a.csv\n") == {"a.csv": sha}
    assert _control_number("0001234", 2) == Decimal("12.34")
    assert _control_number(" 1,234.50 ") == Decimal("1234.50")
    assert _control_number("150-") == Decimal("-150")
    with pytest.raises(ValueError, match="not a number"):
        _control_number("12x")


def test_checks_are_registered(files_registry: Registry) -> None:
    for name in FILE_CHECKS:
        entry = files_registry.get(PluginKind.CHECK, name)
        assert entry.ready and entry.status == "ready", name
        assert entry.config_model is not None and entry.description


def test_unknown_check_option_is_a_config_error(tmp_path: Path, files_registry: Registry) -> None:
    config = {
        "version": 1,
        "project": {"name": "x"},
        "connections": {"files": {"type": "files", "path": "."}},
        "suites": [
            {
                "name": "s",
                "source": "files",
                "tables": [
                    {
                        "name": "t",
                        "source": {"path": "a.csv"},
                        "checks": [{"type": "file_count", "mn": 1}],
                    }
                ],
            }
        ],
    }
    with pytest.raises(Exception, match="mn"):
        api.load_config_data(config, base_dir=tmp_path, registry=files_registry)


# ============================================================================ existence and counts


def _landing(root: Path) -> None:
    folder = root / "in"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "orders_20260101.csv").write_bytes(b"id,amount\n1,10.50\n2,20.00\n")
    (folder / "orders_20260102.csv").write_bytes(b"id,amount\n3,5.25\n")
    (folder / "README").write_bytes(b"")


def test_existence_count_name_and_size(tmp_path: Path, files_registry: Registry) -> None:
    _landing(tmp_path)
    checks = [
        {"type": "file_exists", "name": "any", "path": "in/orders_*.csv"},
        {"type": "file_exists", "name": "exact", "path": "in/orders_*.csv", "count": 3},
        {"type": "file_exists", "name": "missing", "path": "in/nothing_*.csv"},
        {"type": "file_count", "name": "ok", "path": "in/*", "min": 1, "max": 5},
        {"type": "file_count", "name": "too_many", "path": "in/*", "max": 2},
        {
            "type": "file_name_pattern",
            "name": "names",
            "path": "in/*",
            "pattern": r"orders_\d{8}\.csv",
        },
        {
            "type": "file_name_pattern",
            "name": "paths",
            "path": "in/*.csv",
            "pattern": r"in/orders_.*",
            "match_on": "path",
        },
        {
            "type": "file_name_pattern",
            "name": "mostly",
            "path": "in/*",
            "pattern": r"orders_\d{8}\.csv",
            "mostly": 0.5,
        },
        {"type": "file_size", "name": "empty", "path": "in/*"},
        {
            "type": "file_size",
            "name": "allowed",
            "path": "in/*",
            "allow_empty": True,
            "max_bytes": 100,
        },
        {"type": "file_size", "name": "small", "path": "in/*.csv", "min_bytes": 20},
    ]
    results = run_checks(
        tmp_path,
        files_registry,
        [{"name": "orders", "source": {"path": "in/orders_*.csv"}, "checks": checks}],
    )
    status = {key.removeprefix("s.orders."): r.status for key, r in results.items()}
    assert status == {
        "schema.object_exists": Status.PASSED,
        "file_exists.any": Status.PASSED,
        "file_exists.exact": Status.FAILED,
        "file_exists.missing": Status.FAILED,
        "file_count.ok": Status.PASSED,
        "file_count.too_many": Status.FAILED,
        "file_name_pattern.names": Status.FAILED,
        "file_name_pattern.paths": Status.PASSED,
        "file_name_pattern.mostly": Status.PASSED,
        "file_size.empty": Status.FAILED,
        "file_size.allowed": Status.PASSED,
        "file_size.small": Status.FAILED,
    }
    missing = results["s.orders.file_exists.missing"]
    assert missing.message == "no file matches 'in/nothing_*.csv'" and missing.hints
    exact = results["s.orders.file_exists.exact"]
    assert exact.message == "2 file(s) match 'in/orders_*.csv'; expected exactly 3"
    assert exact.details["files"][0]["name"] == "in/orders_20260101.csv"
    names = results["s.orders.file_name_pattern.names"]
    assert names.unexpected_count == 1 and "in/README" in (names.message or "")
    assert names.samples is not None and names.samples.rows[0][0] == "in/README"
    empty = results["s.orders.file_size.empty"]
    assert (
        "in/README (0 bytes, empty)" in (empty.message or "") and empty.details["empty_files"] == 1
    )
    small = results["s.orders.file_size.small"]
    assert "orders_20260102.csv" in (small.message or "") and small.unexpected_count == 1


# ============================================================================ checksums and freshness


def test_checksums(tmp_path: Path, files_registry: Registry) -> None:
    _landing(tmp_path)
    first = tmp_path / "in" / "orders_20260101.csv"
    second = tmp_path / "in" / "orders_20260102.csv"
    sha1 = hashlib.sha256(first.read_bytes()).hexdigest()
    md5_1 = hashlib.md5(first.read_bytes()).hexdigest()  # noqa: S324 - checksum under test
    md5_2 = hashlib.md5(second.read_bytes()).hexdigest()  # noqa: S324 - checksum under test
    (tmp_path / "in" / "orders_20260101.csv.sha256").write_text(
        f"{sha1}  orders_20260101.csv\n", encoding="utf-8"
    )
    (tmp_path / "manifest.txt").write_text(
        f"{sha1}  in/orders_20260101.csv\n{'0' * 64}  in/orders_20260102.csv\n", encoding="utf-8"
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    archive.joinpath("orders_20260101.csv").write_bytes(first.read_bytes())
    archive.joinpath("orders_20260102.csv").write_bytes(b"id,amount\n3,5.26\n")
    archive.joinpath("orders_20260103.csv").write_bytes(b"id,amount\n")
    checks = [
        {"type": "file_checksum", "name": "sidecar", "sidecar": True},
        {
            "type": "file_checksum",
            "name": "expected",
            "algorithm": "md5",
            "expected": {
                "orders_20260102.csv": md5_2,
                "*20260101*": md5_1,
            },
        },
        {"type": "file_checksum", "name": "manifest", "manifest": "manifest.txt"},
        {"type": "file_checksum", "name": "compare", "compare_to": {"path": "archive/*.csv"}},
        {
            "type": "file_checksum",
            "name": "relative",
            "match_by": "relative_path",
            "path": "in/orders_20260101.csv",
            "compare_to": {"path": "archive/orders_20260101.csv"},
        },
    ]
    results = run_checks(
        tmp_path,
        files_registry,
        [{"name": "orders", "source": {"path": "in/orders_*.csv"}, "checks": checks}],
    )
    sidecar = results["s.orders.file_checksum.sidecar"]
    assert sidecar.status is Status.FAILED  # the second file has no sidecar
    assert "1 have no expected checksum (in/orders_20260102.csv)" in (sidecar.message or "")
    assert results["s.orders.file_checksum.expected"].status is Status.PASSED
    manifest = results["s.orders.file_checksum.manifest"]
    assert manifest.status is Status.FAILED and manifest.unexpected_count == 1
    assert manifest.samples is not None and manifest.samples.rows[0][3] == "checksum differs"
    compare = results["s.orders.file_checksum.compare"]
    assert compare.status is Status.FAILED
    assert compare.message == (
        "1 of 3 file(s) are identical; 1 differ (orders_20260102.csv); "
        "1 only in 'archive/*.csv' (orders_20260103.csv)"
    )
    assert results["s.orders.file_checksum.relative"].status is Status.PASSED


def test_freshness(tmp_path: Path, files_registry: Registry) -> None:
    _landing(tmp_path)
    old = tmp_path / "in" / "orders_20260101.csv"
    stamp = time.time() - 3 * 86400
    os.utime(old, (stamp, stamp))
    checks = [
        {"type": "file_freshness", "name": "newest", "max_age": "1h"},
        {"type": "file_freshness", "name": "oldest", "max_age": "2d", "which": "oldest"},
        {"type": "file_freshness", "name": "none", "max_age": "1h", "path": "in/*.xml"},
    ]
    results = run_checks(
        tmp_path,
        files_registry,
        [{"name": "orders", "source": {"path": "in/orders_*.csv"}, "checks": checks}],
    )
    assert results["s.orders.file_freshness.newest"].status is Status.PASSED
    oldest = results["s.orders.file_freshness.oldest"]
    assert oldest.status is Status.FAILED and oldest.details["stale_files"] == 1
    assert "the oldest file, in/orders_20260101.csv, was modified 72.0 h ago" in (
        oldest.message or ""
    )
    assert results["s.orders.file_freshness.none"].status is Status.FAILED


# ============================================================================ control records


def test_control_totals(tmp_path: Path, files_registry: Registry) -> None:
    (tmp_path / "a.csv").write_bytes(b"H|20260101|ORDERS\nid|amount\n1|10.50\n2|20.00\nT|2|30.50\n")
    (tmp_path / "b.csv").write_bytes(b"H|20260102|ORDERS\nid|amount\n3|1.00\nT|2|1.00\n")
    (tmp_path / "c.dat").write_bytes(b"HDR000002\n00001000105\n00002000200\n")
    (tmp_path / "c.ctl").write_bytes(b"count=2;total=305.00\n")
    reader = {"delim": "|", "skip": 1, "skip_footer": 1, "header": True}
    trailer = {"pattern": r"^T\|(?P<count>\d+)\|(?P<total>[\d.]+)$"}
    fixed = {
        "format": "fixed_width",
        "widths": [5, 6],
        "names": ["id", "amount"],
        "skip_header": 1,
        "types": {"amount": "DECIMAL(8,2)"},
    }
    tables = [
        {
            "name": "one",
            "source": {"path": "a.csv", "options": reader},
            "checks": [
                {
                    "type": "control_totals",
                    "trailer": trailer,
                    "record_count": "count",
                    "sums": {"amount": "total"},
                },
            ],
        },
        {
            "name": "many",
            "source": {"path": "[ab].csv", "options": reader},
            "checks": [
                {
                    "type": "control_totals",
                    "trailer": trailer,
                    "record_count": "count",
                    "sums": {"amount": "total"},
                },
            ],
        },
        {
            "name": "fixed",
            "source": {"path": "c.dat", "options": fixed},
            "checks": [
                {
                    "type": "control_totals",
                    "header": {"fields": {"n": {"start": 4, "width": 6}}},
                    "record_count": "n",
                },
                {
                    "type": "control_totals",
                    "name": "ctl",
                    "path": "c.ctl",
                    "header": {"pattern": r"count=(?P<n>\d+);total=(?P<t>[\d.]+)"},
                    "record_count": "n",
                    "sums": {"amount": "t"},
                    "tolerance": 0.001,
                },
            ],
        },
        {
            "name": "nomatch",
            "source": {"path": "a.csv", "options": reader},
            "checks": [
                {
                    "type": "control_totals",
                    "trailer": {"pattern": r"^X(?P<n>\d+)$"},
                    "record_count": "n",
                },
            ],
        },
    ]
    results = run_checks(tmp_path, files_registry, tables)
    one = results["s.one.control_totals"]
    assert one.status is Status.PASSED, one.message
    assert [(m.metric, m.source, m.target) for m in one.metrics] == [
        ("record_count", 2, 2),
        ("sum", "30.50", "30.5"),  # amount is read as DOUBLE
    ]
    many = results["s.many.control_totals"]
    assert many.status is Status.FAILED
    assert "control records say 4 records, the data has 3" in (many.message or "")
    assert many.details["control_values"][1] == {"file": "b.csv", "count": "2", "total": "1.00"}
    assert results["s.fixed.control_totals"].status is Status.PASSED
    assert results["s.fixed.control_totals.ctl"].status is Status.PASSED
    nomatch = results["s.nomatch.control_totals"]
    assert nomatch.status is Status.ERROR and "does not match the pattern" in (
        nomatch.message or ""
    )


# ============================================================================ schema on read


def test_schema_on_read(tmp_path: Path, files_registry: Registry) -> None:
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1], pa.int32()),
                "amount": pa.array([Decimal("1.50")], pa.decimal128(10, 2)),
                "extra": ["x"],
            }
        ),
        tmp_path / "t.parquet",
    )
    (tmp_path / "schema.json").write_text(
        json.dumps(
            {
                "columns": [
                    {"name": "ID", "type": "BIGINT"},
                    {"name": "amount", "type": "DECIMAL(12,2)"},
                    {"name": "extra"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "schema.yaml").write_text("id: INTEGER\namount: DECIMAL(10,2)\n", encoding="utf-8")
    checks = [
        {"type": "schema_on_read", "name": "logical", "schema_file": "schema.json"},
        {
            "type": "schema_on_read",
            "name": "exact",
            "schema_file": "schema.json",
            "type_match": "exact",
        },
        {"type": "schema_on_read", "name": "extra", "schema_file": "schema.yaml"},
        {
            "type": "schema_on_read",
            "name": "allowed",
            "schema_file": "schema.yaml",
            "allow_extra_columns": True,
        },
        {
            "type": "schema_on_read",
            "name": "order",
            "columns": ["amount", "id"],
            "ordered": True,
            "allow_extra_columns": True,
        },
        {
            "type": "schema_on_read",
            "name": "case",
            "columns": ["ID", "amount", "extra"],
            "case_sensitive": True,
        },
        {
            "type": "schema_on_read",
            "name": "text",
            "columns": {"id": "VARCHAR", "amount": None, "extra": None},
        },
    ]
    results = run_checks(
        tmp_path, files_registry, [{"name": "t", "source": {"path": "t.parquet"}, "checks": checks}]
    )
    status = {
        key.removeprefix("s.t.schema_on_read."): r.status
        for key, r in results.items()
        if "schema_on_read" in key
    }
    assert status == {
        "logical": Status.PASSED,
        "exact": Status.FAILED,
        "extra": Status.FAILED,
        "allowed": Status.PASSED,
        "order": Status.FAILED,
        "case": Status.FAILED,
        "text": Status.FAILED,
    }
    exact = results["s.t.schema_on_read.exact"]
    assert "'ID' type differs (INTEGER, expected BIGINT)" in (exact.message or "")
    assert "'extra' not expected" in (results["s.t.schema_on_read.extra"].message or "")
    assert exact.samples is not None and exact.samples.columns[0] == "column"


# ============================================================================ bad records


def test_bad_records(tmp_path: Path, files_registry: Registry) -> None:
    (tmp_path / "bad.csv").write_bytes(b"id,amount\n1,10\n2,abc\n3,4,5\n4,8\n")
    (tmp_path / "good.csv").write_bytes(b"id,amount\n1,10\n")
    (tmp_path / "lines.ndjson").write_bytes(b'{"id": 1}\n{"id": \n[2]\n{"id": 4}\n')
    (tmp_path / "f.dat").write_bytes(b"00001x\n0002\n0000ab\n")
    pq.write_table(pa.table({"id": [1]}), tmp_path / "ok.parquet")
    (tmp_path / "corrupt.parquet").write_bytes(b"PAR1 this is not parquet PAR1")
    typed = {
        "columns": {"id": "INTEGER", "amount": "INTEGER"},
        "header": True,
        "ignore_errors": True,
    }
    fixed = {
        "format": "fixed_width",
        "widths": [5, 1],
        "names": ["id", "flag"],
        "types": {"id": "INTEGER"},
        "ignore_errors": True,
    }
    tables = [
        {
            "name": "csv",
            "source": {"path": "bad.csv", "options": typed},
            "checks": [
                {"type": "bad_records"},
                {"type": "bad_records", "name": "budget", "max_count": 2},
                {"type": "bad_records", "name": "mostly", "mostly": 0.9},
            ],
        },
        {"name": "good", "source": {"path": "good.csv"}, "checks": [{"type": "bad_records"}]},
        {
            "name": "ndjson",
            "source": {"path": "lines.ndjson", "options": {"ignore_errors": True}},
            "checks": [{"type": "bad_records"}],
        },
        {
            "name": "fixed",
            "source": {"path": "f.dat", "options": fixed},
            "checks": [{"type": "bad_records"}],
        },
        {
            "name": "parquet",
            "source": {"path": "ok.parquet"},
            "checks": [
                {"type": "bad_records"},
                {"type": "bad_records", "name": "glob", "where": "1 = 1"},
            ],
        },
    ]
    results = run_checks(tmp_path, files_registry, tables)
    csv = results["s.csv.bad_records"]
    assert csv.status is Status.FAILED
    assert csv.message == "2 bad record(s) of 4 records (50%): rows rejected by the CSV reader"
    assert csv.samples is not None
    assert [row[3] for row in csv.samples.rows] == ["CAST", "TOO MANY COLUMNS"]
    assert csv.samples.rows[0][:3] == ["bad.csv", 3, "amount"] and csv.hints
    assert results["s.csv.bad_records.budget"].status is Status.PASSED
    assert results["s.csv.bad_records.mostly"].status is Status.FAILED
    assert results["s.good.bad_records"].message == "no bad records (all 1 records read cleanly)"
    ndjson = results["s.ndjson.bad_records"]
    assert ndjson.unexpected_count == 2 and ndjson.total_count == 4
    fixed_result = results["s.fixed.bad_records"]
    assert fixed_result.unexpected_count == 2, fixed_result.message  # short line, bad integer
    assert {row[3] for row in fixed_result.samples.rows} == {"RECORD LENGTH", "CAST"}  # type: ignore[union-attr]
    assert results["s.parquet.bad_records"].status is Status.PASSED


def test_unreadable_files(tmp_path: Path, files_registry: Registry) -> None:
    folder = tmp_path / "parts"
    folder.mkdir()
    pq.write_table(pa.table({"id": [1]}), folder / "a.parquet")
    (folder / "b.parquet").write_bytes(b"PAR1" + b"\0" * 64 + b"PAR1")
    tables = [
        {
            "name": "parts",
            "source": {"path": "parts/*.parquet"},
            "checks": [{"type": "bad_records"}],
        }
    ]
    result = run_checks(tmp_path, files_registry, tables)["s.parts.bad_records"]
    assert result.status is Status.FAILED
    assert result.message == "1 bad record(s): files that cannot be read"
    assert result.samples is not None
    assert result.samples.rows[0][0] == "parts/b.parquet"
    assert result.samples.rows[0][3] == "UNREADABLE FILE"


def test_file_checks_need_a_files_connection(tmp_path: Path, files_registry: Registry) -> None:
    config = {
        "version": 1,
        "project": {"name": "x"},
        "connections": {"db": {"type": "duckdb"}},
        "suites": [
            {
                "name": "s",
                "source": "db",
                "tables": [
                    {
                        "name": "t",
                        "source": {"query": "SELECT 1 AS id"},
                        "checks": [
                            {"type": "file_count", "min": 1},
                            {"type": "bad_records"},
                            {"type": "schema_on_read", "columns": {"id": "INTEGER"}},
                        ],
                    },
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False, registry=files_registry)
    output = api.run(loaded, write_reports=False, history=False)
    checks = {c.type: c for c in output.result.suites[0].datasets[0].checks}
    assert checks["file_count"].status is Status.ERROR
    assert "is not a files connection" in (checks["file_count"].message or "")
    assert checks["bad_records"].status is Status.ERROR
    assert checks["schema_on_read"].status is Status.PASSED  # works on any dataset
