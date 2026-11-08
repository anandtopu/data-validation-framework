"""The Spark engine: co-located and staged sides, phase B paths, strategies, and cleanup.

Each case runs the same configuration with the ``duckdb`` engine and requires identical
reconciliation, per-column counts, and mismatched values.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dvf import api
from dvf.core.enums import Status
from dvf.models.results import CheckResult, RunResult
from tests.unit.test_spark_support import PANDAS_WARNING, local_spark

pytestmark = [pytest.mark.spark, pytest.mark.filterwarnings(PANDAS_WARNING)]

ROWS = 300


def _source_rows() -> list[dict[str, Any]]:
    base = datetime(2024, 3, 1, 8)
    return [
        {
            "id": i,
            "name": f"name {i}",
            "amount": Decimal(i * 7 % 1000).scaleb(-2),
            "ratio": i / 7,
            "at": base + timedelta(hours=i),
        }
        for i in range(1, ROWS + 1)
    ]


SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("name", pa.string()),
        ("amount", pa.decimal128(10, 2)),
        ("ratio", pa.float64()),
        ("at", pa.timestamp("us")),
    ]
)


def _target_rows() -> list[dict[str, Any]]:
    rows = [dict(r) for r in _source_rows() if r["id"] not in (5, 6, 7)]  # 3 missing
    by_id = {r["id"]: r for r in rows}
    by_id[10]["name"] = "NAME 10"  # case
    by_id[11]["name"] = "name 11 "  # whitespace
    by_id[12]["amount"] = Decimal("99.99")  # value
    by_id[13]["ratio"] = by_id[13]["ratio"] + 1.0  # beyond tolerance
    by_id[14]["ratio"] = by_id[14]["ratio"] + 1e-12  # within tolerance
    by_id[15]["at"] = by_id[15]["at"] + timedelta(hours=2)  # time zone shift
    rows += [dict(by_id[20]), dict(by_id[21])]  # 2 duplicated keys
    rows.append({**by_id[30], "id": None})  # a NULL key
    rows += [{**by_id[40], "id": 9000 + k} for k in range(2)]  # 2 extra
    return rows


@pytest.fixture(scope="module", autouse=True)
def spark() -> Any:
    return local_spark()


@pytest.fixture
def data(tmp_path: Path) -> Path:
    pq.write_table(pa.Table.from_pylist(_source_rows(), SCHEMA), tmp_path / "source.parquet")
    pq.write_table(pa.Table.from_pylist(_target_rows(), SCHEMA), tmp_path / "target.parquet")
    con = sqlite3.connect(tmp_path / "source.sqlite")
    con.execute(
        "CREATE TABLE src (id INTEGER PRIMARY KEY, name TEXT, amount DECIMAL(10,2), "
        "ratio REAL, at TIMESTAMP)"
    )
    con.executemany(
        "INSERT INTO src VALUES (?, ?, ?, ?, ?)",
        [
            (r["id"], r["name"], str(r["amount"]), r["ratio"], r["at"].isoformat(sep=" "))
            for r in _source_rows()
        ],
    )
    con.commit()
    con.close()
    return tmp_path


def _config(
    source: dict[str, Any], source_ds: dict[str, Any], engine: str, **comparison: Any
) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "spark-engine"},
        "defaults": {"engine": engine, "sample_size": 20},
        "connections": {"src": source, "tgt": {"type": "spark"}},
        "suites": [
            {
                "name": "s",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "t",
                        "source": source_ds,
                        "target": {"path": "target.parquet"},
                        "keys": ["id"],
                        "comparison": {
                            "strategies": ["row_hash"],
                            "on_mismatch": "full_diff",
                            "tolerance": {"numeric_abs": 1e-9},
                            **comparison,
                        },
                    }
                ],
            }
        ],
        "reports": {"formats": ["json"]},
    }


def _run(config: dict[str, Any], base: Path, **kwargs: Any) -> RunResult:
    loaded = api.load_config_data(config, base_dir=base, strict=False)
    return api.run(loaded, output_dir=base / "reports", write_reports=False, **kwargs).result


def _check(result: RunResult, check_type: str) -> CheckResult:
    return next(c for c in result.iter_checks() if c.type == check_type)


def _outcome(result: RunResult) -> dict[str, Any]:
    errors = [(c.check_id, c.message) for c in result.iter_checks() if c.status is Status.ERROR]
    assert not errors, errors
    dataset = result.suites[0].datasets[0]
    assert dataset.reconciliation is not None
    mismatch = _check(result, "compare.column_mismatch")
    return {
        "reconciliation": dataset.reconciliation.model_dump(),
        "columns": {
            m.column: (m.mismatch_count, m.classifications) for m in mismatch.column_mismatches
        },
    }


EXPECTED_RECONCILIATION = {
    "missing_in_target": 3,
    "extra_in_target": 2,
    "duplicate_keys_target": 2,
    "null_keys_target": 1,
    "mismatched": 5,
}


def _assert_expected(outcome: dict[str, Any]) -> None:
    rec = outcome["reconciliation"]
    for name, value in EXPECTED_RECONCILIATION.items():
        assert rec[name] == value, (name, rec)
    assert {name: count for name, (count, _) in outcome["columns"].items()} == {
        "name": 2,
        "amount": 1,
        "ratio": 1,
        "at": 1,
    }


def test_two_spark_sides(data: Path) -> None:
    config = _config({"type": "spark"}, {"path": "source.parquet"}, "spark")
    spark_result = _run(config, data)
    outcome = _outcome(spark_result)
    _assert_expected(outcome)
    assert outcome == _outcome(
        _run(_config({"type": "spark"}, {"path": "source.parquet"}, "duckdb"), data)
    )
    mismatch = _check(spark_result, "compare.column_mismatch")
    assert mismatch.engine == "spark"
    assert mismatch.details["rows_transferred"] == 0


def test_same_spark_connection(data: Path) -> None:
    config = _config({"type": "spark"}, {"path": "source.parquet"}, "spark")
    del config["connections"]["tgt"]
    config["suites"][0]["target"] = "src"
    _assert_expected(_outcome(_run(config, data)))


def test_staged_side_with_key_list(data: Path) -> None:
    """SQLite cannot receive a key table: phase B filters it with a literal key list."""
    config = _config({"type": "sqlite", "database": "source.sqlite"}, {"table": "src"}, "spark")
    result = _run(config, data)
    outcome = _outcome(result)
    _assert_expected(outcome)
    duck = _outcome(
        _run(
            _config({"type": "sqlite", "database": "source.sqlite"}, {"table": "src"}, "duckdb"),
            data,
        )
    )
    assert outcome == duck
    mismatch = _check(result, "compare.column_mismatch")
    assert mismatch.details["rows_transferred"] >= ROWS
    queries = [q for c in result.iter_checks() if c.type.startswith("compare.") for q in c.queries]
    assert any(q.dialect == "sqlite" and " IN (" in q.sql for q in queries)


def test_staged_side_without_key_filter(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Beyond the key-list limit every row comes across and Spark keeps the mismatched ones."""
    import dvf.engines.spark_engine as engine

    monkeypatch.setattr(engine, "KEY_LIST_LIMIT", 0)
    config = _config({"type": "sqlite", "database": "source.sqlite"}, {"table": "src"}, "spark")
    _assert_expected(_outcome(_run(config, data)))


def test_staged_side_with_key_table(data: Path) -> None:
    """DuckDB receives the mismatched keys as a table."""
    config = _config(
        {"type": "duckdb"},
        {"query": f"SELECT * FROM read_parquet('{(data / 'source.parquet').as_posix()}')"},
        "spark",
    )
    _assert_expected(_outcome(_run(config, data)))


def test_staging_through_the_driver(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a cluster without a shared temp_dir, projections are sent through the driver."""
    import dvf.engines.spark_engine as engine

    monkeypatch.setattr(engine, "_local", lambda session, job: False)
    config = _config({"type": "sqlite", "database": "source.sqlite"}, {"table": "src"}, "spark")
    _assert_expected(_outcome(_run(config, data)))


@pytest.mark.parametrize("strategy", ["hash_bucket", "bisect"])
def test_checksum_search_with_the_spark_engine(data: Path, strategy: str) -> None:
    config = _config(
        {"type": "spark"},
        {"path": "source.parquet"},
        "spark",
        strategies=[strategy],
        buckets=8,
        drilldown_threshold=20,
    )
    outcome = _outcome(_run(config, data))
    rec = outcome["reconciliation"]
    assert rec["missing_in_target"] == 3
    assert rec["extra_in_target"] == 2
    assert rec["mismatched"] == 5


def test_partitions_with_the_spark_engine(data: Path) -> None:
    config = _config(
        {"type": "spark"},
        {"path": "source.parquet"},
        "spark",
        partitions={"hash_buckets": 3},
    )
    _assert_expected(_outcome(_run(config, data)))


def test_views_and_files_are_cleaned_up(spark: Any, data: Path) -> None:
    config = _config({"type": "sqlite", "database": "source.sqlite"}, {"table": "src"}, "spark")
    config["defaults"]["execution"] = {"temp_dir": "spill"}
    _outcome(_run(config, data))
    assert list((data / "spill").iterdir()) == []
    leftovers = [
        t.name for t in spark.catalog.listTables("global_temp") if t.name.startswith("dvf_")
    ]
    assert leftovers == []


def test_engine_failures_become_errors(data: Path) -> None:
    config = _config({"type": "spark"}, {"path": "source.parquet"}, "spark")
    config["suites"][0]["tables"][0]["target"] = {
        "path": "target.parquet",
        "filter": "no_such_column > 1",
    }
    result = _run(config, data)
    errors = [c for c in result.iter_checks() if c.status is Status.ERROR]
    assert errors
