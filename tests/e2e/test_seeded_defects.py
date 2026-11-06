"""The acceptance backbone: every injected defect is found exactly and classified correctly.

The source is CSV, DuckDB, SQLite, or PostgreSQL; the target is Parquet. Each engine must report
the same counts and classifications, and the HTML report must show every classification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dvf import api
from dvf.core.enums import Status
from dvf.models.results import CheckResult, DatasetPairResult, RunResult
from dvf.scaffold.seeded import (
    SeededData,
    generate,
    load_duckdb,
    load_postgres,
    load_sqlite,
    write_csv,
    write_parquet,
)

DATA = generate(rows=200, seed=11)


def _target_files(directory: Path, data: SeededData) -> Path:
    target = directory / "curated"
    write_parquet(data.target_customers, target / "customers.parquet")
    write_parquet(data.target_orders, target / "orders.parquet")
    return target


def _config(
    source: dict[str, Any],
    customers: dict[str, Any],
    orders: dict[str, Any],
    target_dir: Path,
    engine: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "seeded-harness"},
        "defaults": {"engine": engine, "sample_size": 10},
        "connections": {
            "src": source,
            "tgt": {"type": "files", "path": target_dir.as_posix(), "format": "parquet"},
        },
        "suites": [
            {
                "name": "harness",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "customers",
                        "source": customers,
                        "target": {"path": "customers.parquet"},
                        "keys": ["customer_id"],
                        "columns": {"exclude": ["ingested_at"]},
                        "comparison": {
                            "strategies": ["row_count", "aggregate", "row_hash"],
                            "on_mismatch": "full_diff",
                        },
                        "checks": [{"type": "unique", "columns": ["customer_id"]}],
                    },
                    {
                        "name": "orders",
                        "source": orders,
                        "target": {"path": "orders.parquet"},
                        "keys": ["order_id"],
                        "comparison": {"strategies": ["row_count"], "on_mismatch": "full_diff"},
                        "checks": [
                            {
                                "type": "referential_integrity",
                                "column": "customer_id",
                                "parent": {"table": "customers.parquet", "column": "customer_id"},
                            }
                        ],
                    },
                ],
            }
        ],
        "reports": {"formats": ["json", "html"], "output_dir": "./reports"},
    }


def _source(
    kind: str, directory: Path, postgres_url: str | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tables = {"customers": DATA.source_customers, "orders": DATA.source_orders}
    if kind == "csv":
        landing = directory / "landing"
        write_csv(DATA.source_customers, landing / "customers.csv")
        write_csv(DATA.source_orders, landing / "orders.csv")
        return (
            {"type": "files", "path": landing.as_posix(), "format": "csv"},
            {
                "path": "customers.csv",
                "options": {"types": {"customer_code": "VARCHAR", "balance": "DECIMAL(12,3)"}},
            },
            {"path": "orders.csv", "options": {"types": {"amount": "DECIMAL(10,2)"}}},
        )
    if kind == "duckdb":
        load_duckdb(tables, directory / "source.duckdb")
        return (
            {"type": "duckdb", "database": (directory / "source.duckdb").as_posix()},
            {"table": "customers"},
            {"table": "orders"},
        )
    if kind == "sqlite":
        load_sqlite(
            tables, directory / "source.sqlite", {"customers": "customer_id", "orders": "order_id"}
        )
        return (
            {"type": "sqlite", "database": (directory / "source.sqlite").as_posix()},
            {"table": "customers"},
            {"table": "orders"},
        )
    assert postgres_url is not None
    schema = f"dvf_seeded_{directory.name[-12:].replace('-', '_').lower()}"
    load_postgres(tables, postgres_url, schema, {"customers": "customer_id", "orders": "order_id"})
    return (
        {"type": "postgres", "url": postgres_url},
        {"table": f"{schema}.customers"},
        {"table": f"{schema}.orders"},
    )


def _dataset(result: RunResult, name: str) -> DatasetPairResult:
    return next(d for s in result.suites for d in s.datasets if d.name == name)


def _check(dataset: DatasetPairResult, check_type: str) -> CheckResult:
    return next(c for c in dataset.checks if c.type == check_type)


def assert_exact(result: RunResult, data: SeededData) -> None:
    expected = data.expected
    errors = [c for c in result.iter_checks() if c.status is Status.ERROR]
    assert not errors, [(c.check_id, c.message, c.error and c.error.traceback) for c in errors]

    customers = _dataset(result, "customers")
    rec = customers.reconciliation
    assert rec is not None
    assert rec.missing_in_target == expected.customers_missing
    assert rec.extra_in_target == expected.customers_extra
    assert rec.duplicate_keys_target == expected.customers_duplicates
    assert rec.duplicate_keys_source == 0
    assert rec.null_keys_source == rec.null_keys_target == 0
    assert rec.mismatched == expected.customers_mismatched_rows
    mismatch = _check(customers, "compare.column_mismatch")
    found = {m.column: m.classifications for m in mismatch.column_mismatches}
    assert found == expected.columns
    for m in mismatch.column_mismatches:
        assert m.mismatch_count == sum(expected.columns[m.column].values())
        assert m.hints
    types = _check(customers, "schema.type_compatibility")
    assert types.status is Status.WARNING
    assert list(expected.type_changes) == types.columns
    assert _check(customers, "unique").unexpected_count == expected.customers_duplicates
    assert _check(customers, "compare.keys").status is Status.FAILED

    orders = _dataset(result, "orders")
    assert orders.reconciliation is not None
    assert orders.reconciliation.extra_in_target == expected.orders_extra
    assert orders.reconciliation.missing_in_target == 0
    assert orders.reconciliation.mismatched == 0
    assert _check(orders, "referential_integrity").unexpected_count == expected.orphans


def _run(config: dict[str, Any], base: Path) -> api.RunOutput:
    loaded = api.load_config_data(config, base_dir=base, strict=False)
    return api.run(loaded, output_dir=base / "reports")


@pytest.mark.parametrize("engine", ["duckdb", "polars", "auto"])
@pytest.mark.parametrize("backend", ["csv", "duckdb", "sqlite"])
def test_seeded_defects_local(backend: str, engine: str, tmp_path: Path) -> None:
    target = _target_files(tmp_path, DATA)
    source, customers, orders = _source(backend, tmp_path)
    output = _run(_config(source, customers, orders, target, engine), tmp_path)
    assert_exact(output.result, DATA)


def test_same_connection_pushdown(tmp_path: Path) -> None:
    """Both datasets in one DuckDB database: the diff runs entirely inside it."""
    load_duckdb(
        {
            "customers": DATA.source_customers,
            "orders": DATA.source_orders,
            "customers_t": DATA.target_customers,
            "orders_t": DATA.target_orders,
        },
        tmp_path / "both.duckdb",
    )
    conn = {"type": "duckdb", "database": (tmp_path / "both.duckdb").as_posix()}
    config = _config(conn, {"table": "customers"}, {"table": "orders"}, tmp_path, "pushdown")
    del config["connections"]["tgt"]
    config["suites"][0]["target"] = "src"
    tables = config["suites"][0]["tables"]
    tables[0]["target"] = {"table": "customers_t"}
    tables[1]["target"] = {"table": "orders_t"}
    tables[1]["checks"][0]["parent"] = {"table": "customers_t", "column": "customer_id"}
    output = _run(config, tmp_path)
    assert_exact(output.result, DATA)
    mismatch = _check(_dataset(output.result, "customers"), "compare.column_mismatch")
    assert mismatch.engine == "pushdown"


def test_pushdown_rejects_cross_connection(tmp_path: Path) -> None:
    target = _target_files(tmp_path, DATA)
    source, customers, orders = _source("duckdb", tmp_path)
    output = _run(_config(source, customers, orders, target, "pushdown"), tmp_path)
    keys = _check(_dataset(output.result, "customers"), "compare.row_hash")
    assert keys.status is Status.ERROR
    assert keys.error is not None and "different connections" in keys.error.message


def test_html_report_shows_every_classification(tmp_path: Path) -> None:
    target = _target_files(tmp_path, DATA)
    source, customers, orders = _source("csv", tmp_path)
    output = _run(_config(source, customers, orders, target, "duckdb"), tmp_path)
    html = (tmp_path / "reports" / "report.html").read_text(encoding="utf-8")
    for column, labels in DATA.expected.columns.items():
        assert column in html
        for label in labels:
            assert label in html, label
    assert (tmp_path / "reports" / "results.json").exists()
    assert {a.format for a in output.artifacts} == {"json", "html"}


@pytest.mark.integration
@pytest.mark.parametrize("engine", ["duckdb", "polars"])
def test_seeded_defects_postgres_vs_parquet(engine: str, tmp_path: Path, postgres_url: str) -> None:
    """Phase 1 acceptance: PostgreSQL source, Parquet target."""
    target = _target_files(tmp_path, DATA)
    source, customers, orders = _source("postgres", tmp_path, postgres_url)
    output = _run(_config(source, customers, orders, target, engine), tmp_path)
    assert_exact(output.result, DATA)
    html = (tmp_path / "reports" / "report.html").read_text(encoding="utf-8")
    for labels in DATA.expected.columns.values():
        for label in labels:
            assert label in html
