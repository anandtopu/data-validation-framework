"""Checks, comparisons, and the runner, on small synthetic DuckDB and SQLite datasets."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from dvf import api
from dvf.core.enums import Severity, Status
from dvf.execution.runner import exit_code_for
from dvf.models.results import CheckResult, RunResult
from dvf.plugins import CheckConfig, Dimension, RowCheck, hookimpl
from dvf.plugins.registry import Registry
from dvf.scaffold.seeded import load_duckdb, load_sqlite

NOW = datetime.now().replace(microsecond=0)

PEOPLE = pa.table(
    {
        "id": pa.array([1, 2, 3, 4, 5, 5], pa.int64()),
        "name": pa.array(["Ann", "Bob ", None, "  ", "Eve", "Eve"]),
        "email": pa.array(
            ["ann@example.com", "bob@x", None, "d@example.com", "e@example.com", "e@example.com"]
        ),
        "country": pa.array(["DE", "fr", "US", "XX", "GB", "GB"]),
        "age": pa.array([30, 17, 45, 200, 25, 25], pa.int64()),
        "amount": pa.array(
            [
                Decimal("1.50"),
                Decimal("2.25"),
                None,
                Decimal("4.00"),
                Decimal("5.10"),
                Decimal("5.10"),
            ],
            pa.decimal128(8, 2),
        ),
        "start": pa.array([date(2024, 1, 1)] * 6, pa.date32()),
        "finish": pa.array(
            [
                date(2024, 2, 1),
                date(2023, 1, 1),
                None,
                date(2024, 3, 1),
                date(2024, 1, 2),
                date(2024, 1, 2),
            ],
            pa.date32(),
        ),
        "updated_at": pa.array([NOW - timedelta(hours=h) for h in range(6)], pa.timestamp("us")),
        "parent_id": pa.array([10, 10, 11, 99, None, None], pa.int64()),
    }
)
PARENTS = pa.table({"pid": pa.array([10, 11], pa.int64())})

CHECKS: list[dict[str, Any]] = [
    {"type": "not_null", "columns": ["name"]},
    {"type": "not_blank", "columns": ["name"]},
    {"type": "null_ratio", "column": "amount", "max_ratio": 0.1},
    {"type": "unique", "columns": ["id"]},
    {"type": "regex", "column": "email", "pattern": r"^[^@]+@[^@]+\.[a-z]+$"},
    {
        "type": "regex",
        "name": "partial",
        "column": "email",
        "pattern": "example",
        "full_match": False,
        "mostly": 0.5,
    },
    {"type": "allowed_values", "column": "country", "values": ["DE", "FR", "US", "GB"]},
    {
        "type": "allowed_values",
        "name": "ci",
        "column": "country",
        "values": ["de", "fr", "us", "gb"],
        "case_sensitive": False,
    },
    {"type": "allowed_values", "name": "numbers", "column": "age", "values": [30, 17, 45, 25]},
    {"type": "range", "column": "age", "min": 18, "max": 120},
    {"type": "range", "name": "exclusive", "column": "age", "min": 0, "inclusive": False},
    {"type": "length", "column": "country", "min": 2, "max": 2},
    {
        "type": "referential_integrity",
        "column": "parent_id",
        "parent": {"table": "parents", "column": "pid"},
    },
    {"type": "custom_sql", "name": "minors", "sql": "SELECT * FROM {table} WHERE age < 18"},
    {
        "type": "custom_sql",
        "name": "total",
        "sql": "SELECT SUM(age) FROM {table}",
        "returns": "scalar",
        "expect": {"operator": ">", "value": 100},
    },
    {
        "type": "custom_sql",
        "name": "text_scalar",
        "sql": "SELECT 'x'",
        "returns": "scalar",
        "expect": {"operator": "==", "value": 1},
    },
    {"type": "expression", "name": "ordered", "expression": "finish >= start"},
    {"type": "row_count_range", "min": 1, "max": 5},
    {"type": "freshness", "column": "updated_at", "max_age": "1d"},
    {
        "type": "freshness",
        "name": "stale",
        "column": "updated_at",
        "max_age": "1s",
        "severity": "warning",
    },
    {"type": "not_null", "name": "where", "columns": ["amount"], "where": "id <> 3"},
]

EXPECTED: dict[str, tuple[Status, int | None]] = {
    "not_null.name": (Status.FAILED, 1),
    "not_blank.name": (Status.FAILED, 2),
    "null_ratio.amount": (Status.FAILED, 1),
    "unique.id": (Status.FAILED, 1),
    "regex.email": (Status.FAILED, 1),
    "regex.email.partial": (Status.PASSED, 1),
    "allowed_values.country": (Status.FAILED, 2),
    "allowed_values.country.ci": (Status.FAILED, 1),
    "allowed_values.age.numbers": (Status.FAILED, 1),
    "range.age": (Status.FAILED, 2),
    "range.age.exclusive": (Status.PASSED, 0),
    "length.country": (Status.PASSED, 0),
    "referential_integrity.parent_id": (Status.FAILED, 1),
    "custom_sql.minors": (Status.FAILED, 1),
    "custom_sql.total": (Status.PASSED, None),
    "custom_sql.text_scalar": (Status.FAILED, None),
    "expression.ordered": (Status.FAILED, 2),
    "row_count_range": (Status.FAILED, None),
    "freshness.updated_at": (Status.PASSED, None),
    "freshness.updated_at.stale": (Status.WARNING, None),
    "not_null.amount.where": (Status.PASSED, 0),
}


def _dq_config(connection: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "unit"},
        "connections": {"db": connection},
        "suites": [
            {
                "name": "dq",
                "source": "db",
                "tables": [
                    {
                        "name": "people",
                        "source": {"table": "people"},
                        "keys": ["id"],
                        "checks": CHECKS,
                    }
                ],
            }
        ],
    }


def _run(config: dict[str, Any], base: Path, **kwargs: Any) -> RunResult:
    loaded = api.load_config_data(config, base_dir=base, strict=False)
    return api.run(loaded, write_reports=False, **kwargs).result


def _by_suffix(result: RunResult) -> dict[str, CheckResult]:
    return {c.check_id.split(".", 2)[2]: c for c in result.iter_checks()}


@pytest.mark.parametrize("backend", ["duckdb", "sqlite"])
def test_quality_checks(backend: str, tmp_path: Path) -> None:
    tables = {"people": PEOPLE, "parents": PARENTS}
    if backend == "duckdb":
        load_duckdb(tables, tmp_path / "db.duckdb")
        conn: dict[str, Any] = {"type": "duckdb", "database": "db.duckdb"}
    else:
        load_sqlite(tables, tmp_path / "db.sqlite")
        conn = {"type": "sqlite", "database": "db.sqlite"}
    checks = _by_suffix(_run(_dq_config(conn), tmp_path))
    for suffix, (status, unexpected) in EXPECTED.items():
        check = checks[suffix]
        assert check.status is status, (backend, suffix, check.message, check.error)
        if unexpected is not None:
            assert check.unexpected_count == unexpected, (backend, suffix, check.message)
    assert checks["not_null.name"].samples is not None
    assert checks["not_null.name"].samples.columns[0] == "id"
    assert checks["regex.email"].dimension is Dimension.VALIDITY
    assert checks["regex.email"].queries and checks["regex.email"].queries[0].dialect == backend
    assert checks["regex.email.partial"].mostly == 0.5


def test_checks_on_both_sides_and_missing_column(tmp_path: Path) -> None:
    load_duckdb({"people": PEOPLE, "copy": PEOPLE}, tmp_path / "db.duckdb")
    config = {
        "version": 1,
        "project": {"name": "unit"},
        "connections": {"db": {"type": "duckdb", "database": "db.duckdb"}},
        "suites": [
            {
                "name": "cmp",
                "source": "db",
                "target": "db",
                "tables": [
                    {
                        "name": "people",
                        "source": {"table": "people"},
                        "target": {"table": "copy"},
                        "keys": ["id"],
                        "schema_checks": [],
                        "comparison": {"enabled": False},
                        "checks": [
                            {"type": "not_null", "columns": ["name"], "side": "both"},
                            {"type": "not_null", "name": "ghost", "columns": ["no_such_column"]},
                        ],
                    }
                ],
            }
        ],
    }
    checks = _by_suffix(_run(config, tmp_path))
    assert checks["not_null.name.source"].status is Status.FAILED
    assert checks["not_null.name.target"].side.value == "target"  # type: ignore[union-attr]
    ghost = checks["not_null.no_such_column.ghost"]
    assert ghost.status is Status.ERROR
    assert ghost.error is not None and "Available columns" in (ghost.error.hint or "")


def _pair_config(source: str, target: str, **table: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "unit"},
        "connections": {"db": {"type": "duckdb", "database": "db.duckdb"}},
        "suites": [
            {
                "name": "cmp",
                "source": "db",
                "target": "db",
                "tables": [
                    {"name": "t", "source": {"table": source}, "target": {"table": target}, **table}
                ],
            }
        ],
    }


def test_comparisons_tolerance_profile_and_groups(tmp_path: Path) -> None:
    source = pa.table(
        {
            "id": pa.array([1, 2, 3, 4], pa.int64()),
            "grp": pa.array(["a", "a", "b", "b"]),
            "price": pa.array([1.0, 2.0, 3.0, 4.0], pa.float64()),
            "qty": pa.array([10, 20, 30, 40], pa.int64()),
            "label": pa.array(["x", "y", "z", "w"]),
        }
    )
    target = pa.table(
        {
            "id": pa.array([1, 2, 3, 4], pa.int64()),
            "grp": pa.array(["a", "a", "b", "b"]),
            "price": pa.array([1.0004, 2.0, 3.0, 4.5], pa.float64()),
            "qty": pa.array([10, 20, 30, 41], pa.int64()),
            "label": pa.array(["x", "y", "z", "W"]),
        }
    )
    load_duckdb({"s": source, "t": target}, tmp_path / "db.duckdb")
    config = _pair_config(
        "s",
        "t",
        keys=["id"],
        comparison={
            "strategies": ["row_count", "aggregate", "profile", "full_diff"],
            "group_by": ["grp"],
            "tolerance": {"numeric_abs": 0.001, "row_count_pct": 1},
        },
    )
    result = _run(config, tmp_path)
    checks = _by_suffix(result)
    assert checks["compare.row_count"].status is Status.PASSED
    assert checks["compare.aggregate"].status is Status.FAILED
    assert any(m.group == {"grp": "b"} for m in checks["compare.aggregate"].metrics)
    assert checks["compare.profile"].severity is Severity.WARNING
    assert checks["compare.profile"].status is Status.WARNING
    mismatches = {m.column: m for m in checks["compare.column_mismatch"].column_mismatches}
    assert set(mismatches) == {"price", "qty", "label"}  # 1.0004 is within the tolerance
    assert mismatches["label"].classifications == {"case_only": 1}
    dataset = result.suites[0].datasets[0]
    assert {p.column for p in dataset.profiles} >= {"price", "label"}


def test_no_keys_skips_row_level_and_auto_keys(tmp_path: Path) -> None:
    load_duckdb(
        {"s": pa.table({"id": [1, 2]}), "t": pa.table({"id": [1, 3]})}, tmp_path / "db.duckdb"
    )
    result = _run(
        _pair_config("s", "t", comparison={"strategies": ["row_count", "row_hash"]}), tmp_path
    )
    keys = _by_suffix(result)["compare.keys"]
    assert keys.status is Status.SKIPPED
    import duckdb

    con = duckdb.connect(str(tmp_path / "db.duckdb"))
    con.execute("CREATE TABLE sp (id INTEGER PRIMARY KEY, v VARCHAR)")
    con.execute("INSERT INTO sp VALUES (1, 'a'), (2, 'b')")
    con.execute("CREATE TABLE tp AS SELECT * FROM sp")
    con.close()
    result = _run(
        _pair_config(
            "sp",
            "tp",
            keys="auto",
            schema_checks=["constraints"],
            comparison={"strategies": ["row_hash"]},
        ),
        tmp_path,
    )
    checks = _by_suffix(result)
    assert checks["compare.row_hash"].status is Status.PASSED
    assert checks["schema.constraints"].status is Status.PASSED  # target has no PK: data check


def test_missing_target_and_unknown_strategy(tmp_path: Path) -> None:
    load_duckdb({"s": pa.table({"id": [1]})}, tmp_path / "db.duckdb")
    result = _run(
        _pair_config(
            "s", "nope", keys=["id"], comparison={"strategies": ["row_count", "hash_bucket"]}
        ),
        tmp_path,
    )
    checks = _by_suffix(result)
    assert checks["schema.object_exists"].status is Status.FAILED
    assert checks["schema.columns_present"].status is Status.SKIPPED
    result = _run(
        _pair_config("s", "s", keys=["id"], comparison={"strategies": ["hash_bucket"]}), tmp_path
    )
    assert _by_suffix(result)["compare.hash_bucket"].status is Status.ERROR


def test_bad_connection_is_contained(tmp_path: Path) -> None:
    config = _pair_config("s", "t", keys=["id"])
    config["connections"]["db"] = {"type": "sqlite", "database": "missing.sqlite"}
    result = _run(config, tmp_path)
    check = next(result.iter_checks())
    assert check.type == "dataset.access"
    assert check.status is Status.ERROR
    assert exit_code_for(result) == 2


def test_selectors_and_exit_codes(tmp_path: Path) -> None:
    load_duckdb({"people": PEOPLE, "parents": PARENTS}, tmp_path / "db.duckdb")
    config = _dq_config({"type": "duckdb", "database": "db.duckdb"})
    result = _run(config, tmp_path, only=["check:not_null"])
    assert {c.type for c in result.iter_checks()} == {"not_null"}
    assert exit_code_for(result) == 1
    assert exit_code_for(result, fail_on=Severity.CRITICAL) == 0
    result = _run(config, tmp_path, only=["check:freshness", "severity:warning"])
    assert [c.check_id for c in result.iter_checks()] == [
        "dq.people.freshness.updated_at",
        "dq.people.freshness.updated_at.stale",
    ]
    assert exit_code_for(result) == 0
    assert exit_code_for(result, strict=True) == 1
    result = _run(config, tmp_path, only=["check:length"])
    assert exit_code_for(result, min_score=100.0) == 0
    assert exit_code_for(_run(config, tmp_path, only=["check:regex"]), min_score=90) == 1


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    @hookimpl
    def on_run_start(self, run_id: str) -> None:
        self.events.append("run_start")

    @hookimpl
    def enrich_result(self, result: CheckResult) -> None:
        result.details["enriched"] = True

    @hookimpl
    def on_failure(self, result: CheckResult) -> None:
        self.events.append(f"failure:{result.type}")

    @hookimpl
    def on_run_end(self, result: RunResult) -> None:
        self.events.append("run_end")

    @hookimpl
    def report_sections(self, result: RunResult, report_format: str) -> list[dict[str, str]]:
        return [{"title": "Sign-off", "content": f"{report_format} approved"}]


class _EvenIds(RowCheck):
    """Ids must be even."""

    dimension = Dimension.VALIDITY

    class Config(CheckConfig):
        column: str

    def predicate(self, ctx: Any) -> str:
        return f"{ctx.col(self.config.column)} % 2 = 0"


def test_hooks_plugins_and_reports(tmp_path: Path, registry: Registry) -> None:
    load_duckdb({"people": PEOPLE, "parents": PARENTS}, tmp_path / "db.duckdb")
    recorder = _Recorder()
    registry.register_hooks(recorder)
    registry.register("check", "acme.even_ids", _EvenIds)
    config = _dq_config({"type": "duckdb", "database": "db.duckdb"})
    config["suites"][0]["tables"][0]["checks"] = [
        {"type": "acme.even_ids", "column": "id", "details": {"rule": "R-1"}},
        {"type": "not_null", "columns": ["name"]},
    ]
    config["reports"] = {"formats": ["markdown"], "output_dir": "out/${run.id}"}
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry, strict=False)
    output = api.run(loaded, run_id="r1")
    checks = _by_suffix(output.result)
    assert checks["acme.even_ids.id"].unexpected_count == 4
    assert checks["acme.even_ids.id"].details == {"rule": "R-1", "enriched": True}
    assert recorder.events[0] == "run_start" and recorder.events[-1] == "run_end"
    assert "failure:not_null" in recorder.events
    assert output.output_dir == tmp_path / "out" / "r1"
    summary = (tmp_path / "out" / "r1" / "summary.md").read_text(encoding="utf-8")
    assert "markdown approved" in summary
    assert (tmp_path / "out" / "r1" / "results.json").exists()


def test_privacy_masks_samples(tmp_path: Path) -> None:
    load_duckdb({"people": PEOPLE, "parents": PARENTS}, tmp_path / "db.duckdb")
    config = _dq_config({"type": "duckdb", "database": "db.duckdb"})
    config["defaults"] = {"privacy": {"pii_columns": ["name"]}}
    config["suites"][0]["tables"][0]["checks"] = [
        {"type": "regex", "column": "email", "pattern": "^nomatch$"}
    ]
    check = _by_suffix(_run(config, tmp_path))["regex.email"]
    assert check.samples is not None
    emails = [row[1] for row in check.samples.rows]
    assert all("***@" in e or e == "bob@x" for e in emails if e)
    assert "ann@example.com" not in emails
    config["defaults"] = {"privacy": {"mode": "strict"}}
    check = _by_suffix(_run(config, tmp_path))["regex.email"]
    assert check.samples is None


def test_pattern_tables(tmp_path: Path) -> None:
    rows = pa.table({"id": [1, 2]})
    load_duckdb(
        {"zfi_a": rows, "zfi_b": rows, "core_zfi_a": rows, "core_zfi_b": rows, "other": rows},
        tmp_path / "db.duckdb",
    )
    config = {
        "version": 1,
        "project": {"name": "unit"},
        "connections": {"db": {"type": "duckdb", "database": "db.duckdb"}},
        "suites": [
            {
                "name": "p",
                "source": "db",
                "target": "db",
                "tables": [
                    {
                        "pattern": {"source": "zfi_*", "target": "core_zfi_*"},
                        "keys": ["id"],
                        "comparison": {"strategies": ["row_count"]},
                    }
                ],
            }
        ],
    }
    result = _run(config, tmp_path)
    names = sorted(d.name for d in result.suites[0].datasets)
    assert names == ["zfi_a", "zfi_b"]
    assert all(d.status is Status.PASSED for d in result.suites[0].datasets)


def test_example_plugin_check_runs(tmp_path: Path, registry: Registry) -> None:
    from dvf.plugins.local import load_local_plugins
    from tests.conftest import EXAMPLES

    load_local_plugins(registry, [str(EXAMPLES / "sap_to_snowflake" / "plugins")])
    vat = pa.table(
        {"id": [1, 2, 3], "vat": ["DE123456789", "XX1", None], "country": ["DE", "DE", "FR"]}
    )
    load_duckdb({"people": vat}, tmp_path / "db.duckdb")
    config = _dq_config({"type": "duckdb", "database": "db.duckdb"})
    config["suites"][0]["tables"][0]["checks"] = [
        {"type": "acme.vat_number_valid", "column": "vat", "country_column": "country"}
    ]
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry, strict=False)
    check = _by_suffix(api.run(loaded, write_reports=False).result)["acme.vat_number_valid.vat"]
    assert check.status is Status.FAILED
    assert check.unexpected_count == 1
