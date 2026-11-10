"""The ``sample`` comparison strategy (REC-07): sizing, selection parity, and estimates."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dvf import api
from dvf.config.models import SampleConfig
from dvf.connectors.sqlite import register_functions
from dvf.core.enums import Status
from dvf.core.types import LogicalType
from dvf.models.results import CheckResult, RunResult
from dvf.normalize.canonical import CanonSpec, Kind, canonical_sql, canonical_text
from dvf.packs.catalog.sample import (
    ClassStats,
    Stratum,
    allocate,
    cochran_size,
    estimate,
    sample_hash,
    sample_hash_sql,
    threshold,
    wilson_interval,
    z_value,
)
from dvf.sql.dialects import get_dialect

PARITY = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
INT_SPEC = CanonSpec(kind=Kind.INTEGER, logical=LogicalType.INTEGER)

# ---------------------------------------------------------------------- statistics


def test_cochran_size_matches_published_values() -> None:
    # 95% confidence, +-5 points, p = 0.5: n0 = 384.16; the classic table values.
    assert cochran_size(10**9, 0.05, 0.95) == 385
    assert cochran_size(1_000, 0.05, 0.95) == 278
    assert cochran_size(100, 0.05, 0.95) == 80
    assert cochran_size(10**9, 0.01, 0.95) == 9604
    assert cochran_size(50, 0.001, 0.99) == 50  # never more than the population
    assert cochran_size(0, 0.05, 0.95) == 0
    assert cochran_size(10**9, 0.05, 0.95, expected_rate=0.1) < 385
    assert z_value(0.95) == pytest.approx(1.959964, abs=1e-6)


def test_wilson_interval() -> None:
    low, high = wilson_interval(0.0, 100, 0.95)
    assert low == 0.0
    assert high == pytest.approx(0.0370, abs=5e-4)
    low, high = wilson_interval(1.0, 100, 0.95)
    assert high == 1.0
    assert low == pytest.approx(1 - 0.0370, abs=5e-4)
    low, high = wilson_interval(0.5, 400, 0.95)
    assert (low, high) == pytest.approx((0.4513, 0.5487), abs=5e-4)
    assert wilson_interval(0.1, 0, 0.95) == (0.0, 1.0)


def test_estimate_census_is_exact() -> None:
    est = estimate(
        [ClassStats(1.0, 500, sampled=500, differing=7, mismatched=5, missing_in_target=2)], 0.95
    )
    assert est.census
    assert est.rate == est.low == est.high == pytest.approx(7 / 500)
    assert est.extrapolated == {
        "differing": 7,
        "mismatched": 5,
        "missing_in_target": 2,
        "extra_in_target": 0,
    }


def test_estimate_is_stratified_by_class() -> None:
    # A small oversampled class with many differences and a large clean class: the rate is the
    # population-weighted mean, not the pooled sample rate.
    small = ClassStats(0.5, 1_000, sampled=500, differing=50, mismatched=50)
    large = ClassStats(0.01, 99_000, sampled=990, differing=0)
    est = estimate([small, large], 0.95)
    assert est.rate == pytest.approx(0.01 * 0.1)
    assert est.low < est.rate < est.high
    assert est.extrapolated["differing"] == 100
    pooled = 50 / 1490
    assert est.rate < pooled


def test_estimate_no_difference_still_has_an_upper_bound() -> None:
    est = estimate([ClassStats(0.01, 1_000_000, sampled=10_000, differing=0)], 0.95)
    assert est.rate == 0.0
    assert 0.0 < est.high < 0.001


def test_allocation_with_minimum_per_stratum() -> None:
    strata = [Stratum(("big",), 100_000, 100_000), Stratum(("small",), 1_000, 900)]
    base, classes = allocate(strata, 1_010, min_per_stratum=100)
    assert base == pytest.approx(0.01)
    default, raised = classes
    assert [s.values for s in default.strata] == [("big",)]
    assert [s.values for s in raised.strata] == [("small",)]
    # 100 of 1,000 rows needs 10%: rounded up to base * 2^k = 16%.
    assert raised.fraction == pytest.approx(0.16)
    _, single = allocate(strata, 1_010, min_per_stratum=0)
    assert len(single) == 1


def test_sample_config_sizes_exactly_one_way() -> None:
    assert SampleConfig(margin_of_error=0.01).confidence == 0.95
    assert SampleConfig(fraction=0.1).seed == 42
    assert SampleConfig(rows=5).rows == 5
    with pytest.raises(ValueError, match="set only one"):
        SampleConfig(fraction=0.1, margin_of_error=0.01)
    with pytest.raises(ValueError, match="set one of"):
        SampleConfig()


# ---------------------------------------------------------------------- selection parity

_KEY_TEXT = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs", "Co", "Cn"), blacklist_characters="\x00"
        ),
        max_size=10,
    ),
)


def _duck(rows: list[tuple[Any, ...]], expr: str) -> list[Any]:
    names = [f"k{i}" for i in range(len(rows[0]))] if rows else ["k0"]
    table = pa.table(
        {"i": list(range(len(rows))), **{n: [r[j] for r in rows] for j, n in enumerate(names)}},
        schema=pa.schema([("i", pa.int64()), *[(n, pa.string()) for n in names]]),
    )
    con = duckdb.connect()
    try:
        con.register("t", table)
        return [r[0] for r in con.execute(f"SELECT {expr} FROM t ORDER BY i").fetchall()]
    finally:
        con.close()


def _lite(rows: list[tuple[Any, ...]], expr: str) -> list[Any]:
    width = len(rows[0]) if rows else 1
    con = sqlite3.connect(":memory:")
    try:
        register_functions(con)
        cols = ", ".join(f"k{j} TEXT" for j in range(width))
        con.execute(f"CREATE TABLE t (i INTEGER, {cols})")
        marks = ", ".join("?" for _ in range(width + 1))
        con.executemany(f"INSERT INTO t VALUES ({marks})", [(i, *r) for i, r in enumerate(rows)])
        return [r[0] for r in con.execute(f"SELECT {expr} FROM t ORDER BY i").fetchall()]
    finally:
        con.close()


@PARITY
@given(
    rows=st.lists(st.tuples(_KEY_TEXT, _KEY_TEXT), min_size=1, max_size=20),
    seed=st.integers(-(2**31), 2**31),
)
def test_sample_hash_parity_duckdb_sqlite(
    rows: list[tuple[str | None, str | None]], seed: int
) -> None:
    expected = [sample_hash(list(r), seed) for r in rows]
    assert _duck(rows, sample_hash_sql(["k0", "k1"], get_dialect("duckdb"), seed)) == expected
    assert _lite(rows, sample_hash_sql(["k0", "k1"], get_dialect("sqlite"), seed)) == expected


def _selected_ids_reference(ids: range, seed: int, fraction: float) -> set[int]:
    limit = threshold(fraction)
    return {i for i in ids if sample_hash([canonical_text(i, INT_SPEC)], seed) < limit}


def _predicate(dialect_name: str, seed: int, fraction: float) -> str:
    dialect = get_dialect(dialect_name)
    key = canonical_sql(dialect.quote("id"), INT_SPEC, dialect)
    return f"{sample_hash_sql([key], dialect, seed)} < {threshold(fraction)}"


def test_same_keys_are_selected_in_duckdb_and_sqlite() -> None:
    ids = range(-500, 2_500)
    expected = _selected_ids_reference(ids, 99, 0.1)
    assert 200 < len(expected) < 400
    con = duckdb.connect()
    con.execute(
        f"CREATE TABLE t AS SELECT i::BIGINT AS id FROM range({ids.start}, {ids.stop}) r(i)"
    )
    duck = {
        r[0]
        for r in con.execute(f"SELECT id FROM t WHERE {_predicate('duckdb', 99, 0.1)}").fetchall()
    }
    con.close()
    lite = sqlite3.connect(":memory:")
    register_functions(lite)
    lite.execute("CREATE TABLE t (id INTEGER)")
    lite.executemany("INSERT INTO t VALUES (?)", [(i,) for i in ids])
    found = {r[0] for r in lite.execute(f"SELECT id FROM t WHERE {_predicate('sqlite', 99, 0.1)}")}
    lite.close()
    assert duck == expected
    assert found == expected


@pytest.mark.integration
def test_same_keys_are_selected_in_postgres(postgres_url: str) -> None:
    import psycopg

    ids = range(-500, 2_500)
    expected = _selected_ids_reference(ids, 99, 0.1)
    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute("SET TIME ZONE 'UTC'")
        rows = con.execute(
            f"SELECT id FROM (SELECT g::BIGINT AS id FROM generate_series({ids.start}, "
            f"{ids.stop - 1}) AS g) AS t WHERE {_predicate('postgres', 99, 0.1)}"
        ).fetchall()
    assert {r[0] for r in rows} == expected


# ---------------------------------------------------------------------- end to end


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, int]]:
    """A DuckDB source and a SQLite target with seeded differences (synthetic data)."""
    base = tmp_path_factory.mktemp("sample")
    rows = 20_000
    rng = random.Random(5)  # noqa: S311 - synthetic test data
    con = duckdb.connect(str(base / "src.duckdb"))
    con.execute(
        f"CREATE TABLE t AS SELECT i AS id, 'name ' || i AS name, (i % 5)::VARCHAR AS region, "
        f"ROUND(i * 1.25, 2)::DECIMAL(12, 2) AS amount FROM range({rows}) r(i)"
    )
    con.close()
    lite = sqlite3.connect(base / "tgt.sqlite")
    lite.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, region TEXT, amount NUMERIC(12, 2))"
    )
    data = []
    truth = {"mismatched": 0, "missing_in_target": 0, "extra_in_target": 0}
    for i in range(rows):
        name, roll = f"name {i}", rng.random()
        if roll < 0.02:
            name = name.upper()
            truth["mismatched"] += 1
        elif roll < 0.025:
            truth["missing_in_target"] += 1
            continue
        data.append((i, name, str(i % 5), round(i * 1.25, 2)))
    for i in range(rows, rows + 40):
        data.append((i, f"name {i}", "9", 1.0))
        truth["extra_in_target"] += 1
    lite.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", data)
    lite.commit()
    lite.close()
    return base, truth


def _run_sample(base: Path, sample: dict[str, Any] | None, **comparison: Any) -> CheckResult:
    config: dict[str, Any] = {
        "version": 1,
        "project": {"name": "sample"},
        "connections": {
            "src": {"type": "duckdb", "database": "src.duckdb"},
            "tgt": {"type": "sqlite", "database": "tgt.sqlite"},
        },
        "defaults": {"schema_checks": []},
        "suites": [
            {
                "name": "s",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "t",
                        "source": {"table": "t"},
                        "target": {"table": "t"},
                        "keys": ["id"],
                        "comparison": {
                            "strategies": ["sample"],
                            "on_mismatch": None,
                            **({"sample": sample} if sample is not None else {}),
                            **comparison,
                        },
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }
    loaded = api.load_config_data(config, base_dir=base, strict=False)
    result: RunResult = api.run(loaded, write_reports=False).result
    return next(c for c in result.iter_checks() if c.type == "compare.sample")


def test_census_is_exact(pair: tuple[Path, dict[str, int]]) -> None:
    base, truth = pair
    check = _run_sample(base, {"fraction": 1.0})
    assert check.status is Status.FAILED
    extrapolated = check.details["extrapolated"]
    assert {k: extrapolated[k] for k in truth} == truth
    assert check.details["estimate"]["census"] is True
    assert check.column_mismatches[0].column == "name"
    assert check.column_mismatches[0].classifications == {"case_only": truth["mismatched"]}


def test_margin_of_error_sizing_and_interval(pair: tuple[Path, dict[str, int]]) -> None:
    base, truth = pair
    check = _run_sample(base, {"margin_of_error": 0.01, "confidence": 0.95, "seed": 3})
    design = check.details["sample"]
    population = design["population"]
    assert population == 20_000  # the larger side's rows with keys
    assert design["sample_size_target"] == cochran_size(population, 0.01, 0.95)
    assert (
        abs(design["sample_size"] - design["sample_size_target"])
        < 5 * design["sample_size_target"] ** 0.5
    )
    assert design["seed"] == 3 and design["confidence"] == 0.95
    low, high = check.details["estimate"]["confidence_interval"]
    true_rate = sum(truth.values()) / population
    assert low <= true_rate <= high
    assert check.details["extrapolated"]["rows_with_differences_interval"][0] <= sum(truth.values())
    assert "95% CI" in (check.message or "")
    # The same seed selects the same keys; another seed does not.
    again = _run_sample(base, {"margin_of_error": 0.01, "confidence": 0.95, "seed": 3})
    assert again.details["sampled"] == check.details["sampled"]
    other = _run_sample(base, {"margin_of_error": 0.01, "confidence": 0.95, "seed": 4})
    assert other.details["sampled"] != check.details["sampled"]


def test_stratified_sample_oversamples_small_strata(pair: tuple[Path, dict[str, int]]) -> None:
    base, truth = pair
    check = _run_sample(
        base, {"rows": 500, "stratify_by": ["region"], "min_per_stratum": 150, "seed": 11}
    )
    design = check.details["sample"]
    assert design["strata"] == 6  # five regions and the target-only region '9'
    classes = check.details["classes"]
    assert sum(c["population"] for c in classes) == design["population"]
    low, high = check.details["estimate"]["confidence_interval"]
    assert low <= sum(truth.values()) / design["population"] <= high


def test_passes_within_max_mismatch_rate(pair: tuple[Path, dict[str, int]]) -> None:
    base, _ = pair
    check = _run_sample(base, {"fraction": 0.2, "max_mismatch_rate": 0.1})
    assert check.status is Status.PASSED
    strict = _run_sample(base, {"fraction": 0.2})
    assert strict.status is Status.FAILED
    assert any("full_diff" in h for h in strict.hints)


def test_default_sample_and_partitions_note(pair: tuple[Path, dict[str, int]]) -> None:
    base, _ = pair
    check = _run_sample(base, None, partitions={"hash_buckets": 2})
    assert check.details["sample"]["margin_of_error"] == 0.01
    assert any("partitions are ignored" in h for h in check.hints)


def test_sample_needs_keys(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "src.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
    con.close()
    lite = sqlite3.connect(tmp_path / "tgt.sqlite")
    lite.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    lite.commit()
    lite.close()
    config: dict[str, Any] = {
        "version": 1,
        "project": {"name": "sample"},
        "connections": {
            "src": {"type": "duckdb", "database": "src.duckdb"},
            "tgt": {"type": "sqlite", "database": "tgt.sqlite"},
        },
        "suites": [
            {
                "name": "s",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "t",
                        "source": {"table": "t"},
                        "target": {"table": "t"},
                        "comparison": {"strategies": ["sample"], "on_mismatch": None},
                    },
                    {
                        "name": "empty",
                        "source": {"table": "t"},
                        "target": {"table": "t"},
                        "keys": ["id"],
                        "comparison": {"strategies": ["sample"], "on_mismatch": None},
                    },
                ],
            }
        ],
        "history": {"enabled": False},
    }
    result = api.run(
        api.load_config_data(config, base_dir=tmp_path, strict=False), write_reports=False
    ).result
    checks = {c.check_id: c for c in result.iter_checks() if c.type == "compare.sample"}
    assert checks["s.t.compare.sample"].status is Status.SKIPPED
    assert checks["s.empty.compare.sample"].status is Status.PASSED
    assert "nothing to sample" in (checks["s.empty.compare.sample"].message or "")


@pytest.mark.integration
def test_sample_postgres_source_matches_duckdb_selection(
    pair: tuple[Path, dict[str, int]], postgres_url: str, tmp_path: Path
) -> None:
    """PostgreSQL and DuckDB select the same keys, so a sample of identical data finds nothing."""
    import psycopg

    schema = "p4_cat_sample"
    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        con.execute(f"CREATE SCHEMA {schema}")
        con.execute(
            f"CREATE TABLE {schema}.t AS SELECT g AS id, 'name ' || g AS name FROM "
            "generate_series(0, 4999) AS g"
        )
    try:
        con2 = duckdb.connect(str(tmp_path / "tgt.duckdb"))
        con2.execute("CREATE TABLE t AS SELECT i AS id, 'name ' || i AS name FROM range(5000) r(i)")
        con2.close()
        config: dict[str, Any] = {
            "version": 1,
            "project": {"name": "sample"},
            "connections": {
                "pg": {"type": "postgres", "url": postgres_url},
                "duck": {"type": "duckdb", "database": "tgt.duckdb"},
            },
            "defaults": {"schema_checks": []},
            "suites": [
                {
                    "name": "s",
                    "source": "pg",
                    "target": "duck",
                    "tables": [
                        {
                            "name": "t",
                            "source": {"table": f"{schema}.t"},
                            "target": {"table": "t"},
                            "keys": ["id"],
                            "comparison": {
                                "strategies": ["sample"],
                                "on_mismatch": None,
                                "sample": {"fraction": 0.1, "seed": 99},
                            },
                        }
                    ],
                }
            ],
            "history": {"enabled": False},
        }
        loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
        check = next(
            c
            for c in api.run(loaded, write_reports=False).result.iter_checks()
            if c.type == "compare.sample"
        )
        assert check.status is Status.PASSED, check.message
        expected = len(_selected_ids_reference(range(5000), 99, 0.1))
        assert check.details["sampled"]["matched"] == expected
        assert check.details["sampled"]["missing_in_target"] == 0
        assert check.details["sampled"]["extra_in_target"] == 0
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.mark.integration
@pytest.mark.parametrize("server", ["mysql", "mssql", "oracle"])
def test_same_keys_are_selected_on_servers(server: str) -> None:
    """MySQL, SQL Server, and Oracle select the same keys (DVF_TEST_*_URL variables)."""
    import contextlib

    from tests.server_backends import make_backend

    ids = range(-500, 2_500)
    backend = make_backend(server)
    try:
        backend.drop_table("p4_cat_ids")
        int_type = "NUMBER(10)" if server == "oracle" else "BIGINT"
        column = get_dialect(server).quote("id")  # the predicate quotes the column name
        backend.execute(f"CREATE TABLE p4_cat_ids ({column} {int_type})")
        values = [(i,) for i in ids]
        for start in range(0, len(values), 500):  # SQL Server allows 1,000 rows per VALUES
            backend.executemany(
                f"INSERT INTO p4_cat_ids ({column}) VALUES ({backend.param(0)})",
                values[start : start + 500],
            )
        rows = backend.connector.query(
            f"SELECT {column} FROM p4_cat_ids WHERE {_predicate(server, 99, 0.1)}"
        ).to_pylist()
        found = {int(next(iter(r.values()))) for r in rows}
        assert found == _selected_ids_reference(ids, 99, 0.1)
    finally:
        with contextlib.suppress(Exception):
            backend.drop_table("p4_cat_ids")
        backend.close()
