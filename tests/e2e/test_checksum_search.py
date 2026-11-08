"""Checksum search (hash_bucket, bisect) and partitions find every seeded defect exactly, and move
rows in proportion to the differences, not to the table size."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf import api
from dvf.core.enums import Status
from tests.e2e.test_seeded_defects import (
    DATA,
    _check,
    _config,
    _dataset,
    _run,
    _source,
    _target_files,
    assert_exact,
)


def _customers(config: dict[str, Any]) -> dict[str, Any]:
    return config["suites"][0]["tables"][0]  # type: ignore[no-any-return]


def _search_config(
    backend: str, tmp_path: Path, strategy: str, engine: str = "auto", **comparison: Any
) -> dict[str, Any]:
    target = _target_files(tmp_path, DATA)
    source, customers, orders = _source(backend, tmp_path)
    config = _config(source, customers, orders, target, engine)
    _customers(config)["comparison"] = {
        "strategies": ["row_count", strategy],
        "on_mismatch": "full_diff",
        "buckets": 4,
        "drilldown_fanout": 4,
        "drilldown_threshold": 5,
        **comparison,
    }
    return config


@pytest.mark.parametrize("strategy", ["hash_bucket", "bisect"])
@pytest.mark.parametrize("backend", ["csv", "duckdb", "sqlite"])
def test_checksum_search_is_exact(backend: str, strategy: str, tmp_path: Path) -> None:
    config = _search_config(backend, tmp_path, strategy)
    output = _run(config, tmp_path)
    assert_exact(output.result, DATA)
    customers = _dataset(output.result, "customers")
    result = _check(customers, f"compare.{strategy}")
    assert result.status is Status.FAILED
    levels = result.details["levels"]
    assert len(levels) >= 2, levels
    assert result.details["rows_transferred"] > 0


@pytest.mark.parametrize("strategy", ["hash_bucket", "bisect"])
def test_checksum_search_with_polars_leaf_engine(strategy: str, tmp_path: Path) -> None:
    output = _run(_search_config("duckdb", tmp_path, strategy, engine="polars"), tmp_path)
    assert_exact(output.result, DATA)


def test_identical_tables_need_one_level(tmp_path: Path) -> None:
    target = _target_files(tmp_path, DATA)
    config = _config(
        {"type": "files", "path": target.as_posix(), "format": "parquet"},
        {"path": "customers.parquet"},
        {"path": "orders.parquet"},
        target,
        "auto",
    )
    _customers(config)["comparison"] = {"strategies": ["hash_bucket"], "on_mismatch": "full_diff"}
    output = _run(config, tmp_path)
    result = _check(_dataset(output.result, "customers"), "compare.hash_bucket")
    assert result.status is Status.PASSED
    assert len(result.details["levels"]) == 1
    assert "full_diff" not in {c.type for c in _dataset(output.result, "customers").checks}


@pytest.mark.parametrize(
    "partitions",
    [
        {"hash_buckets": 3},
        {"column": "country_code", "values": "auto"},
        {"column": "customer_id", "ranges": [50, 120]},
        {
            "predicates": [
                {"name": "low", "source": "customer_id < 100"},
                {"name": "high", "source": "customer_id >= 100 OR customer_id IS NULL"},
            ]
        },
    ],
    ids=["hash", "values", "ranges", "predicates"],
)
@pytest.mark.parametrize("strategy", ["row_hash", "hash_bucket"])
def test_partitions_add_up(strategy: str, partitions: dict[str, Any], tmp_path: Path) -> None:
    config = _search_config("duckdb", tmp_path, strategy, partitions=partitions)
    output = _run(config, tmp_path)
    assert_exact(output.result, DATA)
    keys = _check(_dataset(output.result, "customers"), "compare.keys")
    parts = keys.details.get("partitions") or _check(
        _dataset(output.result, "customers"), f"compare.{strategy}"
    ).details.get("partitions")
    assert parts and len(parts) >= 2


def test_partition_column_that_changes_is_flagged(tmp_path: Path) -> None:
    """Rows whose partition value differs between sides look missing in one partition and extra
    in another; the result says so."""
    config = _search_config(
        "duckdb", tmp_path, "row_hash", partitions={"column": "status", "values": "auto"}
    )
    output = _run(config, tmp_path)
    keys = _check(_dataset(output.result, "customers"), "compare.keys")
    assert any("partition" in h for h in keys.hints), keys.hints


def test_bisect_rejects_text_keys(tmp_path: Path) -> None:
    config = _search_config("duckdb", tmp_path, "bisect")
    _customers(config)["keys"] = ["customer_code"]
    output = _run(config, tmp_path)
    result = _check(_dataset(output.result, "customers"), "compare.bisect")
    assert result.status is Status.ERROR
    assert result.error is not None and "integer key" in result.error.message


def _large_pair(directory: Path, rows: int, changed_every: int) -> None:
    """Two DuckDB files with ``rows`` rows; every ``changed_every``-th row differs in one column."""
    sql = (
        f"SELECT i AS id, 'name_' || (i * 7 % 1000) AS name, (i % 97) * 1.25 AS amount, "
        f"DATE '2020-01-01' + CAST(i % 1000 AS INTEGER) AS day FROM range({rows}) t(i)"
    )
    for name, change in (("source", False), ("target", True)):
        con = duckdb.connect(str(directory / f"{name}.duckdb"))
        body = sql
        if change:
            body = (
                f"SELECT id, CASE WHEN id % {changed_every} = 3 THEN name || 'x' ELSE name END AS name, "
                f"amount, day FROM ({sql})"
            )
        con.execute(f"CREATE TABLE wide AS {body}")
        con.close()


@pytest.mark.parametrize("strategy", ["hash_bucket", "bisect"])
def test_transfer_is_proportional_to_differences(strategy: str, tmp_path: Path) -> None:
    rows, changed_every = 200_000, 10_000  # 20 differences, 0.01%
    _large_pair(tmp_path, rows, changed_every)
    config = {
        "version": 1,
        "project": {"name": "transfer"},
        "connections": {
            "a": {"type": "duckdb", "database": (tmp_path / "source.duckdb").as_posix()},
            "b": {"type": "duckdb", "database": (tmp_path / "target.duckdb").as_posix()},
        },
        "suites": [
            {
                "name": "s",
                "source": "a",
                "target": "b",
                "tables": [
                    {
                        "name": "wide",
                        "source": {"table": "wide"},
                        "target": {"table": "wide"},
                        "keys": ["id"],
                        "comparison": {
                            "strategies": [strategy],
                            "on_mismatch": "full_diff",
                            "drilldown_threshold": 200,
                        },
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    result = api.run(loaded, write_reports=False).result
    dataset = _dataset(result, "wide")
    assert dataset.reconciliation is not None
    assert dataset.reconciliation.mismatched == rows // changed_every
    assert dataset.reconciliation.matched == rows - rows // changed_every
    search = _check(dataset, f"compare.{strategy}")
    transferred = search.details["rows_transferred"]
    # Proportional to the 20 differences: far below the 400,000 rows of both sides.
    assert transferred < 20_000, search.details
    mismatch = _check(dataset, "compare.column_mismatch")
    assert [(m.column, m.mismatch_count) for m in mismatch.column_mismatches] == [("name", 20)]


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["hash_bucket", "bisect"])
def test_checksum_search_postgres_vs_parquet(
    strategy: str, tmp_path: Path, postgres_url: str
) -> None:
    target = _target_files(tmp_path, DATA)
    source, customers, orders = _source("postgres", tmp_path, postgres_url)
    config = _config(source, customers, orders, target, "auto")
    _customers(config)["comparison"] = {
        "strategies": ["row_count", strategy],
        "on_mismatch": "full_diff",
        "buckets": 4,
        "drilldown_fanout": 4,
        "drilldown_threshold": 5,
    }
    output = _run(config, tmp_path)
    assert_exact(output.result, DATA)
    assert (
        len(_check(_dataset(output.result, "customers"), f"compare.{strategy}").details["levels"])
        >= 2
    )


@pytest.mark.integration
def test_two_phase_pushdown_in_postgres(tmp_path: Path, postgres_url: str) -> None:
    """Both datasets in PostgreSQL: phase B's EXISTS filter runs inside the database."""
    from dvf.scaffold.seeded import load_postgres

    schema = f"dvf_pushdown_{tmp_path.name[-12:].replace('-', '_').lower()}"
    load_postgres(
        {
            "customers": DATA.source_customers,
            "orders": DATA.source_orders,
            "customers_t": DATA.target_customers,
            "orders_t": DATA.target_orders,
        },
        postgres_url,
        schema,
        {"customers": "customer_id", "orders": "order_id"},
    )
    conn = {"type": "postgres", "url": postgres_url}
    config = _config(
        conn, {"table": f"{schema}.customers"}, {"table": f"{schema}.orders"}, tmp_path, "pushdown"
    )
    del config["connections"]["tgt"]
    config["suites"][0]["target"] = "src"
    tables = config["suites"][0]["tables"]
    tables[0]["target"] = {"table": f"{schema}.customers_t"}
    tables[1]["target"] = {"table": f"{schema}.orders_t"}
    tables[1]["checks"][0]["parent"] = {"table": f"{schema}.customers_t", "column": "customer_id"}
    output = _run(config, tmp_path)
    assert_exact(output.result, DATA)
    mismatch = _check(_dataset(output.result, "customers"), "compare.column_mismatch")
    assert mismatch.engine == "pushdown"
