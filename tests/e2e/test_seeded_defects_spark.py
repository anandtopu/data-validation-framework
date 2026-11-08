"""Phase 3 acceptance: the seeded-defect harness passes on Spark.

Spark reads the CSV source and the Parquet target (written with pyarrow: Spark's own file
writes need winutils on Windows), and the ``spark`` engine diffs them in one Spark SQL
statement. Mixed cases put a DuckDB, SQLite, or PostgreSQL source against the Spark target,
staged into Spark as Parquet. Every injected defect must be found exactly and classified like
the ``duckdb`` engine classifies it on the same data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dvf import api
from dvf.core.enums import Status
from dvf.models.results import RunResult
from dvf.scaffold.seeded import load_duckdb, load_postgres, load_sqlite, write_csv, write_parquet
from tests.e2e.test_seeded_defects import DATA, _check, _dataset, assert_exact
from tests.unit.test_spark_support import PANDAS_WARNING, local_spark

pytestmark = [pytest.mark.spark, pytest.mark.filterwarnings(PANDAS_WARNING)]

CUSTOMERS_DDL = (
    "customer_id BIGINT, customer_code STRING, first_name STRING, last_name STRING, "
    "email STRING, country_code STRING, city STRING, balance DECIMAL(12,3), "
    "created_at TIMESTAMP_NTZ, quantity BIGINT, status STRING, ingested_at TIMESTAMP_NTZ"
)
ORDERS_DDL = (
    "order_id BIGINT, customer_id BIGINT, order_date DATE, ordered_at TIMESTAMP_NTZ, "
    "shipped_at TIMESTAMP_NTZ, amount DECIMAL(10,2)"
)


@pytest.fixture(scope="module", autouse=True)
def spark() -> Any:
    return local_spark()


def _files(directory: Path) -> Path:
    """Write the source CSV files and the target Parquet files; return the target folder."""
    landing, curated = directory / "landing", directory / "curated"
    write_csv(DATA.source_customers, landing / "customers.csv")
    write_csv(DATA.source_orders, landing / "orders.csv")
    write_parquet(DATA.target_customers, curated / "customers.parquet")
    write_parquet(DATA.target_orders, curated / "orders.parquet")
    return curated


def _config(
    source: dict[str, Any],
    customers: dict[str, Any],
    orders: dict[str, Any],
    target_dir: Path,
    engine: str,
) -> dict[str, Any]:
    parent = f"parquet.`{(target_dir / 'customers.parquet').absolute().as_uri()}`"
    return {
        "version": 1,
        "project": {"name": "seeded-harness-spark"},
        "defaults": {"engine": engine, "sample_size": 10},
        "connections": {
            "src": source,
            "tgt": {"type": "spark", "conf": {"spark.sql.shuffle.partitions": "2"}},
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
                        "target": {"path": "curated/customers.parquet"},
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
                        "target": {"path": "curated/orders.parquet"},
                        "keys": ["order_id"],
                        "comparison": {"strategies": ["row_count"], "on_mismatch": "full_diff"},
                        "checks": [
                            {
                                "type": "referential_integrity",
                                "column": "customer_id",
                                "parent": {"table": parent, "column": "customer_id"},
                            }
                        ],
                    },
                ],
            }
        ],
        "reports": {"formats": ["json", "html"], "output_dir": "./reports"},
    }


def _run(config: dict[str, Any], base: Path) -> RunResult:
    loaded = api.load_config_data(config, base_dir=base, strict=False)
    return api.run(loaded, output_dir=base / "reports").result


def _spark_source() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        {"type": "spark"},
        {"path": "landing/customers.csv", "options": {"schema": CUSTOMERS_DDL}},
        {"path": "landing/orders.csv", "options": {"schema": ORDERS_DDL}},
    )


def _outcome(result: RunResult) -> dict[str, Any]:
    """What the harness compares between engines: reconciliation and classifications."""
    customers = _dataset(result, "customers")
    mismatch = _check(customers, "compare.column_mismatch")
    rec = customers.reconciliation
    assert rec is not None
    return {
        "reconciliation": rec.model_dump(),
        "columns": {
            m.column: (m.mismatch_count, m.classifications) for m in mismatch.column_mismatches
        },
        "orders_extra": _dataset(result, "orders").reconciliation.extra_in_target,  # type: ignore[union-attr]
    }


@pytest.fixture(scope="module")
def duckdb_baseline(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The duckdb engine's result on the same files, read by the files connector."""
    base = tmp_path_factory.mktemp("baseline")
    target = _files(base)
    config = _config(
        {"type": "files", "path": (base / "landing").as_posix(), "format": "csv"},
        {
            "path": "customers.csv",
            "options": {"types": {"customer_code": "VARCHAR", "balance": "DECIMAL(12,3)"}},
        },
        {"path": "orders.csv", "options": {"types": {"amount": "DECIMAL(10,2)"}}},
        target,
        "duckdb",
    )
    config["connections"]["tgt"] = {"type": "files", "path": target.as_posix()}
    tables = config["suites"][0]["tables"]
    tables[0]["target"] = {"path": "customers.parquet"}
    tables[1]["target"] = {"path": "orders.parquet"}
    tables[1]["checks"][0]["parent"]["table"] = "customers.parquet"
    result = _run(config, base)
    assert_exact(result, DATA)
    return _outcome(result)


@pytest.mark.parametrize("engine", ["spark", "duckdb", "auto"])
def test_seeded_defects_spark_sides(
    engine: str, tmp_path: Path, duckdb_baseline: dict[str, Any]
) -> None:
    """Both sides read by Spark: CSV source, Parquet target."""
    target = _files(tmp_path)
    source, customers, orders = _spark_source()
    result = _run(_config(source, customers, orders, target, engine), tmp_path)
    assert_exact(result, DATA)
    assert _outcome(result) == duckdb_baseline
    mismatch = _check(_dataset(result, "customers"), "compare.column_mismatch")
    # Both sides are Spark datasets in one application, so auto picks the Spark engine.
    assert mismatch.engine == ("duckdb" if engine == "duckdb" else "spark")


def test_spark_engine_moves_no_rows_between_spark_sides(tmp_path: Path) -> None:
    target = _files(tmp_path)
    source, customers, orders = _spark_source()
    result = _run(_config(source, customers, orders, target, "spark"), tmp_path)
    customers = _dataset(result, "customers")
    assert _check(customers, "compare.column_mismatch").details.get("rows_transferred") == 0
    queries = [q for c in customers.checks if c.type.startswith("compare.") for q in c.queries]
    diffs = [q for q in queries if "FULL OUTER JOIN" in q.sql]
    assert diffs, [q.sql[:80] for q in queries]
    assert all(q.dialect == "spark" and q.engine == "spark" for q in diffs)


@pytest.mark.parametrize("backend", ["duckdb", "sqlite"])
def test_seeded_defects_mixed_sources(
    backend: str, tmp_path: Path, duckdb_baseline: dict[str, Any]
) -> None:
    """A DuckDB or SQLite source against the Spark target, diffed by the Spark engine."""
    target = _files(tmp_path)
    tables = {"customers": DATA.source_customers, "orders": DATA.source_orders}
    if backend == "duckdb":
        load_duckdb(tables, tmp_path / "source.duckdb")
        source = {"type": "duckdb", "database": (tmp_path / "source.duckdb").as_posix()}
    else:
        keys = {"customers": "customer_id", "orders": "order_id"}
        load_sqlite(tables, tmp_path / "source.sqlite", keys)
        source = {"type": "sqlite", "database": (tmp_path / "source.sqlite").as_posix()}
    config = _config(source, {"table": "customers"}, {"table": "orders"}, target, "spark")
    # The parent table lives on the target connection.
    config["suites"][0]["tables"][1]["checks"][0]["parent"]["connection"] = "tgt"
    result = _run(config, tmp_path)
    assert_exact(result, DATA)
    assert _outcome(result) == duckdb_baseline
    mismatch = _check(_dataset(result, "customers"), "compare.column_mismatch")
    assert mismatch.engine == "spark"
    staged = mismatch.details.get("rows_transferred")
    assert staged is not None and staged > 0


def test_spark_engine_without_spark_connections(tmp_path: Path) -> None:
    """With no Spark side, the engine stages both sides into dvf's own Spark session."""
    tables = {"customers": DATA.source_customers, "orders": DATA.source_orders}
    load_duckdb(tables, tmp_path / "source.duckdb")
    target = tmp_path / "curated"
    write_parquet(DATA.target_customers, target / "customers.parquet")
    write_parquet(DATA.target_orders, target / "orders.parquet")
    config = _config(
        {"type": "duckdb", "database": (tmp_path / "source.duckdb").as_posix()},
        {"table": "customers"},
        {"table": "orders"},
        target,
        "spark",
    )
    config["connections"]["tgt"] = {"type": "files", "path": target.as_posix()}
    tables_cfg = config["suites"][0]["tables"]
    tables_cfg[0]["target"] = {"path": "customers.parquet"}
    tables_cfg[1]["target"] = {"path": "orders.parquet"}
    tables_cfg[1]["checks"][0]["parent"] = {
        "connection": "tgt",
        "table": "customers.parquet",
        "column": "customer_id",
    }
    result = _run(config, tmp_path)
    assert_exact(result, DATA)
    mismatch = _check(_dataset(result, "customers"), "compare.column_mismatch")
    assert mismatch.engine == "spark"


def test_keep_temp_leaves_staged_files(tmp_path: Path) -> None:
    target = _files(tmp_path)
    load_duckdb({"customers": DATA.source_customers}, tmp_path / "source.duckdb")
    config = _config(
        {"type": "duckdb", "database": (tmp_path / "source.duckdb").as_posix()},
        {"table": "customers"},
        {"table": "orders"},
        target,
        "spark",
    )
    config["suites"][0]["tables"] = config["suites"][0]["tables"][:1]
    config["defaults"]["execution"] = {"temp_dir": "spill"}
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    output = api.run(loaded, output_dir=tmp_path / "reports", keep_temp=True)
    assert not [c for c in output.result.iter_checks() if c.status is Status.ERROR]
    staged = list((tmp_path / "spill").glob("dvf-spark-*/*.parquet"))
    assert staged, "the staged projections are kept with keep_temp"


@pytest.mark.integration
def test_seeded_defects_postgres_vs_spark(
    tmp_path: Path, postgres_url: str, duckdb_baseline: dict[str, Any]
) -> None:
    """PostgreSQL source against the Spark target, diffed by the Spark engine."""
    target = _files(tmp_path)
    schema = f"dvf_spark_{tmp_path.name[-12:].replace('-', '_').lower()}"
    load_postgres(
        {"customers": DATA.source_customers, "orders": DATA.source_orders},
        postgres_url,
        schema,
        {"customers": "customer_id", "orders": "order_id"},
    )
    try:
        config = _config(
            {"type": "postgres", "url": postgres_url},
            {"table": f"{schema}.customers"},
            {"table": f"{schema}.orders"},
            target,
            "spark",
        )
        config["suites"][0]["tables"][1]["checks"][0]["parent"]["connection"] = "tgt"
        result = _run(config, tmp_path)
        assert_exact(result, DATA)
        assert _outcome(result) == duckdb_baseline
    finally:
        import psycopg

        with psycopg.connect(postgres_url, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
