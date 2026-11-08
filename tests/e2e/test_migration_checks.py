"""End-to-end migration validations (MIG-01 to MIG-12) through ``dvf run``.

Each scenario builds small synthetic tables in DuckDB: schema ``s`` (source) and ``t`` (target).
``same`` puts both schemas in one database, so pair checks run their rules in the database;
``cross`` puts them in two databases, so rows are streamed into a local DuckDB workspace.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf import api
from dvf.core.enums import Side, Status
from dvf.models.results import CheckResult, RunResult

MODES = ["same", "cross"]


def _duckdb(path: Path, statements: list[str]) -> None:
    con = duckdb.connect(str(path))
    try:
        for statement in statements:
            con.execute(statement)
    finally:
        con.close()


def run_tables(
    tmp_path: Path,
    source_sql: list[str],
    target_sql: list[str],
    tables: list[dict[str, Any]],
    *,
    mode: str = "same",
    history: bool = False,
    extra: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Create the tables and run ``tables`` (with comparisons and schema checks off by default)."""
    if mode == "same":
        db = tmp_path / "db.duckdb"
        if not db.exists():
            _duckdb(db, ["CREATE SCHEMA s", "CREATE SCHEMA t", *source_sql, *target_sql])
        connections: dict[str, Any] = {"db": {"type": "duckdb", "database": db.as_posix()}}
        source, target = "db", "db"
    else:
        src, tgt = tmp_path / "source.duckdb", tmp_path / "target.duckdb"
        if not src.exists():
            _duckdb(src, ["CREATE SCHEMA s", *source_sql])
            _duckdb(tgt, ["CREATE SCHEMA t", *target_sql])
        connections = {
            "src": {"type": "duckdb", "database": src.as_posix()},
            "tgt": {"type": "duckdb", "database": tgt.as_posix()},
        }
        source, target = "src", "tgt"
    for table in tables:
        table.setdefault("comparison", {"enabled": False})
        table.setdefault("schema_checks", [])
    data: dict[str, Any] = {
        "version": 1,
        "project": {"name": "migration-e2e"},
        "connections": connections,
        "history": {"enabled": history},
        "suites": [{"name": "m", "source": source, "target": target, "tables": tables}],
        **(extra or {}),
    }
    loaded = api.load_config_data(data, base_dir=tmp_path)
    return api.run(loaded, write_reports=False, history=history, run_id=run_id).result


def check(result: RunResult, check_type: str) -> CheckResult:
    found = [c for c in result.iter_checks() if c.type == check_type]
    assert found, [(c.check_id, c.status, c.message) for c in result.iter_checks()]
    return found[0]


def violations(result: CheckResult) -> dict[str, int]:
    return {k: v for k, v in result.details["violations"].items() if v}


# ---------------------------------------------------------------------- pair-check plumbing


def test_pair_check_skipped_in_single_dataset_suite(tmp_path: Path) -> None:
    db = tmp_path / "db.duckdb"
    _duckdb(db, ["CREATE TABLE a AS SELECT 1 AS id"])
    data = {
        "version": 1,
        "project": {"name": "p"},
        "connections": {"db": {"type": "duckdb", "database": db.as_posix()}},
        "history": {"enabled": False},
        "suites": [
            {
                "name": "dq",
                "source": "db",
                "tables": [
                    {
                        "name": "a",
                        "source": {"table": "a"},
                        "checks": [{"type": "scope_reconciliation", "excluded_where": "1 = 0"}],
                    }
                ],
            }
        ],
    }
    result = api.run(api.load_config_data(data, base_dir=tmp_path), write_reports=False).result
    outcome = check(result, "scope_reconciliation")
    assert outcome.status is Status.SKIPPED
    assert "has no target" in (outcome.message or "")


def test_keyed_pair_check_skipped_without_keys(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        ["CREATE TABLE s.a AS SELECT 1 AS id"],
        ["CREATE TABLE t.a AS SELECT 1 AS id"],
        [
            {
                "name": "a",
                "source": {"table": "s.a"},
                "target": {"table": "t.a"},
                "checks": [{"type": "delete_propagation"}],
            }
        ],
    )
    outcome = check(result, "delete_propagation")
    assert outcome.status is Status.SKIPPED and outcome.side is Side.BOTH


# ---------------------------------------------------------------------- MIG-01


SCOPE_SOURCE = [
    "CREATE TABLE s.acct AS SELECT i AS id, CASE WHEN i > 7 THEN 'ARCHIVED' ELSE 'OPEN' END "
    "AS status FROM range(1, 11) r(i)"
]


@pytest.mark.parametrize("mode", MODES)
def test_scope_reconciliation_balances(tmp_path: Path, mode: str) -> None:
    result = run_tables(
        tmp_path,
        SCOPE_SOURCE,
        ["CREATE TABLE t.acct AS SELECT i AS id FROM range(1, 8) r(i)"],
        [
            {
                "name": "acct",
                "source": {"table": "s.acct", "filter": "status <> 'ARCHIVED'"},
                "target": {"table": "t.acct"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "scope_reconciliation",
                        "excluded_where": "status = 'ARCHIVED'",
                        "expected_excluded": 3,
                        "excluded_absent": True,
                    }
                ],
            }
        ],
        mode=mode,
    )
    outcome = check(result, "scope_reconciliation")
    assert outcome.status is Status.PASSED, outcome.message
    assert outcome.details["source_rows"] == 10
    assert outcome.details["excluded_rows"] == 3
    assert outcome.details["migrated_rows"] == 7
    assert outcome.message == "source 10 rows = migrated 7 + excluded 3"


@pytest.mark.parametrize("mode", MODES)
def test_scope_reconciliation_finds_lost_and_leaked_rows(tmp_path: Path, mode: str) -> None:
    # id 7 was lost and archived id 9 leaked into the target: the totals balance, but the
    # excluded row in the target and the excluded range both fail.
    result = run_tables(
        tmp_path,
        SCOPE_SOURCE,
        ["CREATE TABLE t.acct AS SELECT * FROM (VALUES (1),(2),(3),(4),(5),(6),(9)) v(id)"],
        [
            {
                "name": "acct",
                "source": {"table": "s.acct", "filter": "status <> 'ARCHIVED'"},
                "target": {"table": "t.acct"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "scope_reconciliation",
                        "excluded_where": "status = 'ARCHIVED'",
                        "expected_excluded": {"min": 4},
                        "excluded_absent": True,
                    }
                ],
            }
        ],
        mode=mode,
    )
    outcome = check(result, "scope_reconciliation")
    assert outcome.status is Status.FAILED
    assert outcome.details["excluded_rows_in_target"] == 1
    assert "expected >= 4" in (outcome.message or "")
    assert outcome.samples is not None and outcome.samples.rows == [["9"]]


def test_scope_reconciliation_unaccounted_rows_and_tolerance(tmp_path: Path) -> None:
    tables = [
        {
            "name": "acct",
            "source": {"table": "s.acct"},
            "target": {"table": "t.acct"},
            "checks": [
                {"type": "scope_reconciliation", "excluded_where": "status = 'ARCHIVED'"},
                {
                    "type": "scope_reconciliation",
                    "name": "tolerant",
                    "excluded_where": "status = 'ARCHIVED'",
                    "tolerance_pct": 10,
                },
            ],
        }
    ]
    result = run_tables(
        tmp_path,
        SCOPE_SOURCE,
        ["CREATE TABLE t.acct AS SELECT i AS id FROM range(1, 7) r(i)"],
        tables,
    )
    strict, tolerant = [c for c in result.iter_checks() if c.type == "scope_reconciliation"]
    assert strict.status is Status.FAILED and strict.observed == 1
    assert "1 rows unaccounted for" in (strict.message or "")
    assert tolerant.status is Status.PASSED


# ---------------------------------------------------------------------- MIG-02 window


ORDERS_SOURCE = [
    "CREATE TABLE s.orders AS SELECT i AS id, TIMESTAMP '2026-01-01 00:00:00' + "
    "INTERVAL (i - 1) DAY AS changed_at, i * 10 AS amount FROM range(1, 11) r(i)"
]
ORDERS_TARGET = [
    "CREATE TABLE t.orders AS SELECT i AS id, TIMESTAMP '2026-01-01 00:00:00' + "
    "INTERVAL (i - 1) DAY AS changed_at, i * 10 AS amount FROM range(6, 11) r(i)"
]


@pytest.mark.parametrize("mode", MODES)
def test_window_restricts_both_sides(tmp_path: Path, mode: str) -> None:
    def table(window: dict[str, Any] | None) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "name": "orders" if window else "orders_all",
            "source": {"table": "s.orders"},
            "target": {"table": "t.orders"},
            "keys": ["id"],
            "comparison": {"strategies": ["row_count", "full_diff"], "on_mismatch": None},
        }
        if window:
            spec["window"] = window
        return spec

    result = run_tables(
        tmp_path,
        ORDERS_SOURCE,
        ORDERS_TARGET,
        [
            table({"column": "changed_at", "start": "${var:since}", "end": "${run.date}"}),
            table(None),
        ],
        mode=mode,
        extra={"vars": {"since": "2026-01-06"}},
    )
    datasets = {d.name: d for s in result.suites for d in s.datasets}
    windowed = datasets["orders"]
    assert windowed.status is Status.PASSED, [(c.check_id, c.message) for c in windowed.checks]
    assert windowed.row_counts.source == 5 and windowed.row_counts.target == 5
    assert windowed.details["row_filters"]["source"][0].startswith('"changed_at" >= ')
    assert datasets["orders_all"].status is Status.FAILED


def test_window_with_integer_watermark_and_inclusive_end(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        ORDERS_SOURCE,
        ORDERS_TARGET,
        [
            {
                "name": "orders",
                "source": {"table": "s.orders"},
                "target": {"table": "t.orders"},
                "keys": ["id"],
                "window": {
                    "source_column": "id",
                    "target_column": "ID",
                    "start": 6,
                    "end": 8,
                    "end_inclusive": True,
                },
                "comparison": {"strategies": ["row_count"], "on_mismatch": None},
            }
        ],
    )
    dataset = result.suites[0].datasets[0]
    assert dataset.status is Status.PASSED
    assert dataset.row_counts.source == 3 and dataset.row_counts.target == 3


def test_window_column_missing_is_an_error(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        ORDERS_SOURCE,
        ORDERS_TARGET,
        [
            {
                "name": "orders",
                "source": {"table": "s.orders"},
                "target": {"table": "t.orders"},
                "window": {"column": "nope", "start": 1},
            }
        ],
    )
    error = next(result.iter_checks())
    assert error.status is Status.ERROR and "Window column 'nope' not found" in (
        error.message or ""
    )


# ---------------------------------------------------------------------- MIG-02 cdc_applied


CHANGES = [
    "CREATE TABLE s.changes AS SELECT * FROM (VALUES "
    "(1, 'I', 1, 'a'), (1, 'U', 2, 'b'), (2, 'I', 1, 'x'), (2, 'D', 2, NULL), "
    "(3, 'I', 1, 'c'), (4, 'D', 1, NULL), (5, 'U', 1, 'e')"
    ") v(cust_id, op, seq, name)"
]


@pytest.mark.parametrize("mode", MODES)
def test_cdc_applied(tmp_path: Path, mode: str) -> None:
    changes_conn = "db" if mode == "same" else "src"
    target_good = [
        "CREATE TABLE t.cust AS SELECT * FROM (VALUES (1, 'b'), (3, 'c'), (5, 'e')) v(id, name)",
        "CREATE TABLE t.cust_bad AS SELECT * FROM (VALUES "
        "(1, 'a'), (2, 'x'), (5, 'e'), (4, 'gone')) v(id, name)",
    ]

    def table(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "source": {"table": "s.changes"},
            "target": {"table": f"t.{name}"},
            "keys": [{"source": "cust_id", "target": "id"}],
            "checks": [
                {
                    "type": "cdc_applied",
                    "changes": {
                        "connection": changes_conn,
                        "table": "s.changes",
                        "key": "cust_id",
                        "operation": "op",
                        "sequence": "seq",
                    },
                    "compare_columns": [{"change": "name"}],
                }
            ],
        }

    result = run_tables(
        tmp_path, CHANGES, target_good, [table("cust"), table("cust_bad")], mode=mode
    )
    good, bad = [c for c in result.iter_checks() if c.type == "cdc_applied"]
    assert good.status is Status.PASSED, good.message
    assert good.total_count == 5
    assert good.details["latest_operations"] == {"u": 2, "d": 2, "i": 1}
    assert bad.status is Status.FAILED
    assert violations(bad) == {
        "upsert_missing": 1,  # 3 was inserted but is missing
        "delete_present": 2,  # 2 and 4 were deleted
        "value_mismatch": 1,  # 1 still has the before-image value
    }
    assert bad.samples is not None and len(bad.samples.rows) == 4


def test_cdc_applied_soft_delete_and_unknown_operations(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        [*CHANGES, "INSERT INTO s.changes VALUES (6, 'X', 1, 'f')"],
        [
            "CREATE TABLE t.cust AS SELECT * FROM (VALUES "
            "(1, false), (2, true), (3, false), (4, true), (5, true), (6, false)) v(id, deleted)"
        ],
        [
            {
                "name": "cust",
                "source": {"table": "s.changes"},
                "target": {"table": "t.cust"},
                "checks": [
                    {
                        "type": "cdc_applied",
                        "key_columns": ["id"],
                        "changes": {
                            "connection": "db",
                            "table": "s.changes",
                            "key": ["cust_id"],
                            "operation": "op",
                            "sequence": ["seq"],
                        },
                        "soft_delete": {"column": "deleted"},
                    }
                ],
            }
        ],
    )
    outcome = check(result, "cdc_applied")
    assert violations(outcome) == {"upsert_deleted": 1, "unknown_operation": 1}


# ---------------------------------------------------------------------- MIG-02 late arriving


def test_late_arriving(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        ["CREATE TABLE s.gl AS SELECT 1 AS id"],
        [
            "CREATE TABLE t.gl AS SELECT * FROM (VALUES "
            "(1, DATE '2026-01-31', TIMESTAMP '2026-02-01 08:00:00'), "
            "(2, DATE '2026-02-02', TIMESTAMP '2026-02-02 09:00:00'), "
            "(3, DATE '2026-01-10', TIMESTAMP '2026-01-11 09:00:00'), "
            "(4, DATE '2026-01-01', TIMESTAMP '2026-01-05 00:00:00'), "
            "(5, NULL, TIMESTAMP '2026-02-05 00:00:00')"
            ") v(id, posting_date, loaded_at)"
        ],
        [
            {
                "name": "gl",
                "source": {"table": "s.gl"},
                "target": {"table": "t.gl"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "late_arriving",
                        "name": "period",
                        "event_column": "posting_date",
                        "load_column": "loaded_at",
                        "period_start": "2026-02-01",
                    },
                    {
                        "type": "late_arriving",
                        "name": "lateness",
                        "event_column": "posting_date",
                        "load_column": "loaded_at",
                        "max_lateness": "2d",
                        "severity": "warning",
                    },
                ],
            }
        ],
    )
    period, lateness = [c for c in result.iter_checks() if c.type == "late_arriving"]
    assert period.status is Status.FAILED and period.unexpected_count == 1
    assert period.samples is not None and period.samples.rows[0][0] == 1
    assert "arrived late" in (period.message or "")
    assert lateness.status is Status.WARNING and lateness.unexpected_count == 1


# ---------------------------------------------------------------------- MIG-03 idempotency


def test_idempotency_against_the_previous_run(tmp_path: Path) -> None:
    def tables() -> list[dict[str, Any]]:
        return [
            {
                "name": "loads",
                "source": {"table": "s.loads"},
                "target": {"table": "t.loads"},
                "keys": ["id"],
                "checks": [{"type": "idempotency", "exclude": ["loaded_at"]}],
            }
        ]

    source = ["CREATE TABLE s.loads AS SELECT 1 AS id"]
    target = [
        "CREATE TABLE t.loads AS SELECT i AS id, 'v' || i AS v, now() AS loaded_at "
        "FROM range(1, 21) r(i)"
    ]
    first = check(run_tables(tmp_path, source, target, tables(), history=True), "idempotency")
    assert first.status is Status.PASSED and "no baseline run" in (first.message or "")
    db = tmp_path / "db.duckdb"
    _duckdb(db, ["UPDATE t.loads SET loaded_at = loaded_at + INTERVAL 1 HOUR"])
    second = check(run_tables(tmp_path, source, target, tables(), history=True), "idempotency")
    assert second.status is Status.PASSED, second.message
    assert second.details["checksum"] == first.details["checksum"]
    assert second.details["baseline_run_id"]
    _duckdb(
        db,
        [
            "UPDATE t.loads SET v = 'changed' WHERE id = 3",
            "INSERT INTO t.loads VALUES (4, 'v4', now())",
        ],
    )
    third = check(run_tables(tmp_path, source, target, tables(), history=True), "idempotency")
    assert third.status is Status.FAILED
    message = third.message or ""
    assert "1 duplicated key(s)" in message
    assert "row count changed from 20 to 21" in message
    assert "content checksum changed" in message
    assert third.samples is not None


# ---------------------------------------------------------------------- MIG-04 SCD2


SCD2_TARGET = [
    "CREATE TABLE t.cust_hist AS SELECT * FROM (VALUES "
    # 1: clean history
    "(1, DATE '2020-01-01', DATE '2021-01-01', false), (1, DATE '2021-01-01', NULL, true), "
    # 2: clean, single version with the open-end sentinel
    "(2, DATE '2020-01-01', DATE '9999-12-31', true), "
    # 3: two current rows
    "(3, DATE '2020-01-01', NULL, true), (3, DATE '2021-01-01', NULL, true), "
    # 4: overlapping ranges
    "(4, DATE '2020-01-01', DATE '2021-06-01', false), (4, DATE '2021-01-01', NULL, true), "
    # 5: a gap
    "(5, DATE '2020-01-01', DATE '2020-06-01', false), (5, DATE '2020-07-01', NULL, true), "
    # 6: no current row, and 7: an empty range
    "(6, DATE '2020-01-01', DATE '2021-01-01', false), "
    "(7, DATE '2021-01-01', DATE '2021-01-01', false), (7, DATE '2021-01-01', NULL, true)"
    ") v(cust_id, valid_from, valid_to, is_current)"
]


@pytest.mark.parametrize("mode", MODES)
def test_scd2_rules(tmp_path: Path, mode: str) -> None:
    result = run_tables(
        tmp_path,
        [
            "CREATE TABLE s.cust_versions AS SELECT * FROM (VALUES "
            "(1), (1), (2), (3), (3), (4), (4), (5), (5), (6), (7), (7), (7)) v(cust_id)"
        ],
        SCD2_TARGET,
        [
            {
                "name": "cust_hist",
                "source": {"table": "s.cust_versions"},
                "target": {"table": "t.cust_hist"},
                "checks": [
                    {
                        "type": "scd2",
                        "business_key": "cust_id",
                        "valid_from": "valid_from",
                        "valid_to": "valid_to",
                        "open_end": "9999-12-31",
                        "current_flag": {"column": "is_current"},
                        "history": {
                            "connection": "db" if mode == "same" else "src",
                            "table": "s.cust_versions",
                            "keys": "cust_id",
                        },
                    }
                ],
            }
        ],
        mode=mode,
    )
    outcome = check(result, "scd2")
    assert outcome.status is Status.FAILED
    assert violations(outcome) == {
        "invalid_range": 1,  # key 7
        "no_current": 1,  # key 6
        "multiple_current": 1,  # key 3
        "overlap": 2,  # key 3 (an open row followed by another) and key 4
        "gap": 1,  # key 5
        "history_count": 1,  # key 7 has 2 versions, the reference history 3
    }
    assert outcome.details["business_keys"] == 7
    assert outcome.samples is not None
    assert {row[-1] for row in outcome.samples.rows} >= {"overlap", "gap", "multiple_current"}


def test_scd2_inclusive_ends_and_gaps_allowed(tmp_path: Path) -> None:
    target = [
        "CREATE TABLE t.h AS SELECT * FROM (VALUES "
        "(1, DATE '2020-01-01', DATE '2020-12-31'), (1, DATE '2021-01-01', NULL), "
        "(2, DATE '2020-01-01', DATE '2020-12-31'), (2, DATE '2021-02-01', NULL), "
        "(3, DATE '2020-01-01', DATE '2020-12-31'), (3, DATE '2020-12-31', NULL)"
        ") v(k, vf, vt)"
    ]
    base = {
        "type": "scd2",
        "business_key": ["k"],
        "valid_from": "vf",
        "valid_to": "vt",
        "end": "inclusive",
        "granularity": "1d",
    }
    result = run_tables(
        tmp_path,
        ["CREATE TABLE s.h AS SELECT 1 AS k"],
        target,
        [
            {
                "name": "h",
                "source": {"table": "s.h"},
                "target": {"table": "t.h"},
                "checks": [base, {**base, "name": "gaps_ok", "allow_gaps": True}],
            }
        ],
    )
    strict, relaxed = [c for c in result.iter_checks() if c.type == "scd2"]
    assert violations(strict) == {"gap": 1, "overlap": 1}
    assert violations(relaxed) == {"overlap": 1}


def test_scd2_in_a_single_dataset_suite(tmp_path: Path) -> None:
    db = tmp_path / "db.duckdb"
    _duckdb(db, ["CREATE SCHEMA t", *SCD2_TARGET])
    data = {
        "version": 1,
        "project": {"name": "p"},
        "connections": {"db": {"type": "duckdb", "database": db.as_posix()}},
        "history": {"enabled": False},
        "suites": [
            {
                "name": "dq",
                "source": "db",
                "tables": [
                    {
                        "name": "h",
                        "source": {"table": "t.cust_hist", "filter": "cust_id IN (1, 2)"},
                        "checks": [
                            {
                                "type": "scd2",
                                "business_key": "cust_id",
                                "valid_from": "valid_from",
                                "valid_to": "valid_to",
                                "open_end": "9999-12-31",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    result = api.run(api.load_config_data(data, base_dir=tmp_path), write_reports=False).result
    outcome = check(result, "scd2")
    assert outcome.status is Status.PASSED, outcome.message
    assert outcome.total_count == 3


# ---------------------------------------------------------------------- MIG-05 deletes


@pytest.mark.parametrize("mode", MODES)
def test_delete_propagation(tmp_path: Path, mode: str) -> None:
    result = run_tables(
        tmp_path,
        [
            "CREATE TABLE s.cust AS SELECT * FROM (VALUES "
            "(1, 'N'), (2, 'Y'), (3, 'Y'), (4, 'N'), (5, 'Y')) v(id, deleted)"
        ],
        [
            "CREATE TABLE t.cust AS SELECT * FROM (VALUES "
            "(1, false), (2, true), (3, false), (4, true), (9, false), (10, true)) v(id, is_deleted)"
        ],
        [
            {
                "name": "cust",
                "source": {"table": "s.cust"},
                "target": {"table": "t.cust"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "delete_propagation",
                        "source_flag": {"column": "deleted", "values": ["Y"]},
                        "target_flag": {"column": "is_deleted"},
                    },
                    {
                        "type": "delete_propagation",
                        "name": "absent",
                        "hard_deletes": False,
                        "source_flag": {"expression": "deleted = 'Y'"},
                        "soft_deleted_rows": "absent",
                    },
                ],
            }
        ],
        mode=mode,
    )
    flagged, absent = [c for c in result.iter_checks() if c.type == "delete_propagation"]
    assert flagged.status is Status.FAILED
    assert violations(flagged) == {
        "hard_delete_not_propagated": 1,  # 9
        "soft_delete_not_propagated": 1,  # 3
        "wrongly_deleted": 1,  # 4
    }
    assert flagged.total_count == 7  # 5 source keys plus 2 target-only keys
    assert violations(absent) == {"soft_deleted_present": 2}  # 2 and 3 (5 is absent)


# ---------------------------------------------------------------------- MIG-06 key map


@pytest.mark.parametrize("mode", MODES)
def test_key_map(tmp_path: Path, mode: str) -> None:
    result = run_tables(
        tmp_path,
        [
            "CREATE TABLE s.cust AS SELECT * FROM (VALUES "
            "('A1'), ('A2'), ('A3'), ('A4'), ('A5')) v(cust_no)"
        ],
        [
            "CREATE TABLE t.cust AS SELECT * FROM (VALUES "
            "(1, 'A1'), (2, 'A2'), (4, 'A3'), (5, 'A9')) v(id, legacy_no)",
            "CREATE TABLE t.xref AS SELECT * FROM (VALUES "
            "('A1', 1), ('A2', 2), ('A2', 3), ('A3', 4), ('A4', 4), ('A6', 6), (NULL, 7)"
            ") v(legacy_id, new_id)",
        ],
        [
            {
                "name": "cust",
                "source": {"table": "s.cust"},
                "target": {"table": "t.cust"},
                "keys": [{"source": "cust_no", "target": "legacy_no"}],
                "checks": [
                    {
                        "type": "key_map",
                        "map": {
                            "connection": "db" if mode == "same" else "tgt",
                            "table": "t.xref",
                            "legacy_columns": "legacy_id",
                            "new_columns": "new_id",
                        },
                        "target_key": ["id"],
                        "dangling": True,
                    }
                ],
            }
        ],
        mode=mode,
    )
    outcome = check(result, "key_map")
    assert outcome.status is Status.FAILED
    assert violations(outcome) == {
        "map_null_key": 1,
        "legacy_multi": 1,  # A2 -> 2 and 3
        "new_multi": 1,  # 4 <- A3 and A4
        "unmapped_source": 1,  # A5
        "unknown_target": 1,  # 5
        "dangling": 3,  # 3, 6, and 7 are not in the target
    }
    assert outcome.details["map_entries"] == 7
    assert outcome.samples is not None
    assert outcome.samples.columns == ["rule", "legacy.legacy_id", "new.new_id", "count"]


# ---------------------------------------------------------------------- MIG-07 audit columns


def test_audit_columns(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        ["CREATE TABLE s.l AS SELECT 1 AS id"],
        [
            "CREATE TABLE t.l AS SELECT * FROM (VALUES "
            "(1, TIMESTAMP '2026-01-02 10:00:00', 'B1', 'SAP'), "
            "(2, TIMESTAMP '2026-01-02 10:00:00', 'B1', 'CRM'), "
            "(3, NULL, 'B1', 'SAP'), "
            "(4, TIMESTAMP '2999-01-01 00:00:00', 'B2', 'SAP'), "
            "(5, TIMESTAMP '2025-12-31 00:00:00', 'B1', 'XXX'), "
            "(6, TIMESTAMP '2026-01-02 10:00:00', NULL, NULL)"
            ") v(id, loaded_at, batch_id, src_sys)"
        ],
        [
            {
                "name": "l",
                "source": {"table": "s.l"},
                "target": {"table": "t.l"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "audit_columns",
                        "load_timestamp": "loaded_at",
                        "batch_id": "batch_id",
                        "source_system": "src_sys",
                        "not_before": "2026-01-01",
                        "max_batches": 1,
                        "allowed_batches": ["b1"],
                        "allowed_source_systems": ["SAP", "CRM"],
                    }
                ],
            }
        ],
    )
    outcome = check(result, "audit_columns")
    assert outcome.status is Status.FAILED
    assert violations(outcome) == {
        "load_timestamp_null": 1,
        "load_timestamp_future": 1,
        "load_timestamp_too_early": 1,
        "batch_id_null": 1,
        "batch_not_allowed": 1,
        "too_many_batches": 1,
        "source_system_null": 1,
        "source_system_not_allowed": 1,
    }
    assert outcome.details["batch_count"] == 2
    assert outcome.columns == ["loaded_at", "batch_id", "src_sys"]


# ---------------------------------------------------------------------- MIG-10 charset


NAMES = [
    "Café",
    "CafÃ©",
    "bad�",
    "tab\tok",
    "ctl\x01",
    "€uro",
    "Straße",
]


def test_charset_conversion(tmp_path: Path) -> None:
    rows = ", ".join(f"({i}, ?)" for i in range(len(NAMES)))
    db = tmp_path / "db.duckdb"
    _duckdb(db, ["CREATE SCHEMA s", "CREATE SCHEMA t", "CREATE TABLE s.n AS SELECT 1 AS id"])
    con = duckdb.connect(str(db))
    con.execute(f"CREATE TABLE t.n AS SELECT * FROM (VALUES {rows}) v(id, name)", NAMES)
    con.execute("INSERT INTO t.n VALUES (100, 'Café')")  # a duplicate value: counted per row
    con.close()
    result = run_tables(
        tmp_path,
        [],
        [],
        [
            {
                "name": "n",
                "source": {"table": "s.n"},
                "target": {"table": "t.n"},
                "keys": ["id"],
                "checks": [
                    {"type": "charset_conversion", "columns": ["name"], "code_page": "cp037"},
                    {
                        "type": "charset_conversion",
                        "name": "latin",
                        "columns": ["name"],
                        "rules": ["round_trip"],
                        "code_page": "latin-1",
                    },
                ],
            }
        ],
    )
    ebcdic, latin = [c for c in result.iter_checks() if c.type == "charset_conversion"]
    assert ebcdic.status is Status.FAILED
    assert ebcdic.details["violations"] == {
        "replacement_char": 1,
        "mojibake": 1,
        "control_chars": 1,
        "round_trip": 2,  # the euro sign and U+FFFD do not exist in cp037
    }
    assert ebcdic.total_count == 8 and ebcdic.unexpected_count == 4
    assert any("\\x01" in str(row[1]) for row in ebcdic.samples.rows)  # type: ignore[union-attr]
    assert latin.details["violations"] == {"round_trip": 2}


# ---------------------------------------------------------------------- MIG-11 DST


def test_dst_edge_cases(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        [
            "CREATE TABLE s.ev AS SELECT * FROM (VALUES "
            "(1, TIMESTAMP '2021-03-28 02:30:00', DATE '2018-11-04'), "
            "(2, TIMESTAMP '2021-10-31 02:30:00', DATE '2018-11-05'), "
            "(3, TIMESTAMP '2021-06-01 12:00:00', DATE '2018-11-03'), "
            "(4, TIMESTAMP '9999-12-31 00:00:00', NULL)"
            ") v(id, created_local, booking_date)"
        ],
        ["CREATE TABLE t.ev AS SELECT 1 AS id"],
        [
            {
                "name": "ev",
                "source": {"table": "s.ev"},
                "target": {"table": "t.ev"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "dst_edge_cases",
                        "side": "source",
                        "columns": ["created_local"],
                        "timezone": "Europe/Berlin",
                    },
                    {
                        "type": "dst_edge_cases",
                        "name": "brazil",
                        "side": "source",
                        "columns": ["booking_date"],
                        "timezone": "America/Sao_Paulo",
                    },
                    {
                        "type": "dst_edge_cases",
                        "name": "utc",
                        "side": "source",
                        "columns": ["created_local"],
                        "timezone": "UTC",
                    },
                ],
            }
        ],
    )
    berlin, brazil, utc = [c for c in result.iter_checks() if c.type == "dst_edge_cases"]
    assert berlin.status is Status.FAILED
    assert berlin.details["violations"] == {"nonexistent": 1, "ambiguous": 1}
    assert {row[-1] for row in berlin.samples.rows} == {"nonexistent", "ambiguous"}  # type: ignore[union-attr]
    assert brazil.details["violations"] == {"nonexistent": 1, "ambiguous": 0}
    assert utc.status is Status.PASSED


# ---------------------------------------------------------------------- MIG-12 substitutions


@pytest.mark.parametrize("mode", MODES)
def test_substitution_rules(tmp_path: Path, mode: str) -> None:
    result = run_tables(
        tmp_path,
        [
            "CREATE TABLE s.p AS SELECT * FROM (VALUES "
            "(1, NULL, '19800101'), (2, NULL, '19800101'), (3, 'Ann', '19800101'), "
            "(4, 'N/A', '19800101'), (5, 'Bo', '00000000'), (6, 'Cy', '00000000'), "
            "(7, 'Di', '20200101')) v(id, middle, dob)"
        ],
        [
            "CREATE TABLE t.p AS SELECT * FROM (VALUES "
            "(1, 'N/A', DATE '1980-01-01'), (2, NULL, DATE '1980-01-01'), "
            "(3, 'N/A', DATE '1980-01-01'), (4, 'N/A', DATE '1980-01-01'), "
            "(5, 'Bo', NULL), (6, 'Cy', DATE '1900-01-01'), (7, 'Di', NULL)"
            ") v(id, middle_name, birth_date)"
        ],
        [
            {
                "name": "p",
                "source": {"table": "s.p"},
                "target": {"table": "t.p"},
                "keys": ["id"],
                "columns": {"mappings": [{"source": "middle", "target": "middle_name"}]},
                "checks": [
                    {
                        "type": "substitution_rules",
                        "rules": [
                            {
                                "name": "middle_na",
                                "column": "middle",
                                "source_null": True,
                                "target_value": "N/A",
                            },
                            {
                                "name": "dob_null",
                                "column": "dob",
                                "target_column": "birth_date",
                                "source_values": ["00000000"],
                                "target_null": True,
                            },
                        ],
                    }
                ],
            }
        ],
        mode=mode,
    )
    outcome = check(result, "substitution_rules")
    assert outcome.status is Status.FAILED
    assert violations(outcome) == {
        "rule_middle_na": 1,  # 2 stayed NULL
        "rule_dob_null": 1,  # 6 got 1900-01-01
        "unmatched_1": 1,  # 3: 'N/A' although the source had a value (4 is natural)
        "unmatched_2": 1,  # 7: NULL although the source was a real date
    }
    assert outcome.details["rules"]["rule_middle_na"] == {"applicable": 2, "violations": 1}
    assert len(outcome.metrics) == 4
    rules = {row[1] for row in outcome.samples.rows}  # type: ignore[union-attr]
    assert rules == {"rule_middle_na", "rule_dob_null", "unmatched_1", "unmatched_2"}


# ---------------------------------------------------------------------- MIG-09 in flight


IN_FLIGHT_SOURCE = [
    "CREATE TABLE s.o AS SELECT * FROM (VALUES "
    "(1, 10, TIMESTAMP '2026-01-10 09:00:00'), "
    "(2, 21, TIMESTAMP '2026-01-10 11:55:00'), "  # updated, not replicated yet
    "(3, 30, TIMESTAMP '2026-01-10 11:58:00'), "  # inserted, not replicated yet
    "(4, 41, TIMESTAMP '2026-01-10 11:59:00')"  # updated, target still has the old version
    ") v(id, amount, changed_at)"
]
IN_FLIGHT_TARGET = [
    "CREATE TABLE t.o AS SELECT * FROM (VALUES "
    "(1, 10, TIMESTAMP '2026-01-10 09:00:00'), "
    "(2, 20, TIMESTAMP '2026-01-09 10:00:00'), "
    "(4, 40, TIMESTAMP '2026-01-08 10:00:00')"
    ") v(id, amount, changed_at)"
]


@pytest.mark.parametrize("mode", MODES)
def test_in_flight_exclusion(tmp_path: Path, mode: str) -> None:
    def table(name: str, in_flight: dict[str, Any] | None) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "name": name,
            "source": {"table": "s.o"},
            "target": {"table": "t.o"},
            "keys": ["id"],
            "columns": {"exclude": ["changed_at"]},
            "comparison": {"strategies": ["full_diff"], "on_mismatch": None},
        }
        if in_flight is not None:
            spec["in_flight"] = in_flight
        return spec

    flight = {"column": "changed_at", "window": "15m", "cutoff": "2026-01-10T12:00:00"}
    result = run_tables(
        tmp_path,
        IN_FLIGHT_SOURCE,
        IN_FLIGHT_TARGET,
        [
            table("excluded", flight),
            table("timestamps_only", {**flight, "exclude_keys_on_both_sides": False}),
            table("plain", None),
        ],
        mode=mode,
    )
    datasets = {d.name: d for s in result.suites for d in s.datasets}
    excluded = datasets["excluded"]
    assert excluded.status is Status.PASSED, [(c.check_id, c.message) for c in excluded.checks]
    assert excluded.details["in_flight_keys_excluded"] == 3
    assert excluded.reconciliation is not None and excluded.reconciliation.matched == 1
    # Without excluding keys on both sides, rows 2 and 4 remain in the target only.
    only_ts = datasets["timestamps_only"]
    assert only_ts.reconciliation is not None and only_ts.reconciliation.extra_in_target == 2
    assert datasets["plain"].status is Status.FAILED


def test_in_flight_too_many_keys_is_an_error(tmp_path: Path) -> None:
    result = run_tables(
        tmp_path,
        IN_FLIGHT_SOURCE,
        IN_FLIGHT_TARGET,
        [
            {
                "name": "o",
                "source": {"table": "s.o"},
                "target": {"table": "t.o"},
                "keys": ["id"],
                "in_flight": {
                    "column": "changed_at",
                    "window": "15m",
                    "cutoff": "2026-01-10T12:00:00",
                    "max_keys": 2,
                },
                "comparison": {"strategies": ["row_count"]},
                "checks": [{"type": "delete_propagation"}],
            }
        ],
    )
    statuses = {c.type: c for c in result.iter_checks()}
    assert statuses["in_flight"].status is Status.ERROR
    assert "More than 2 keys are in flight" in (statuses["in_flight"].message or "")
    assert statuses["delete_propagation"].status is Status.SKIPPED


# ---------------------------------------------------------------------- SQLite and files


def test_pair_checks_across_sqlite_and_parquet(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(sqlite_path)
    con.execute("CREATE TABLE cust (id INTEGER, deleted TEXT, middle TEXT)")
    con.executemany(
        "INSERT INTO cust VALUES (?, ?, ?)",
        [(1, "N", None), (2, "Y", "Al"), (3, "N", "Bo")],
    )
    con.commit()
    con.close()
    files = tmp_path / "files"
    files.mkdir()
    duckdb.connect().execute(
        "COPY (SELECT * FROM (VALUES (1, false, 'N/A'), (2, true, 'Al'), (3, false, 'Bo'), "
        f"(4, false, 'Cy')) v(id, is_deleted, middle)) TO '{(files / 'cust.parquet').as_posix()}'"
    ).close()
    data = {
        "version": 1,
        "project": {"name": "p"},
        "history": {"enabled": False},
        "connections": {
            "legacy": {"type": "sqlite", "database": sqlite_path.as_posix()},
            "lake": {"type": "files", "path": files.as_posix(), "format": "parquet"},
        },
        "suites": [
            {
                "name": "m",
                "source": "legacy",
                "target": "lake",
                "tables": [
                    {
                        "name": "cust",
                        "source": {"table": "cust"},
                        "target": {"path": "cust.parquet"},
                        "keys": ["id"],
                        "schema_checks": [],
                        "comparison": {"enabled": False},
                        "checks": [
                            {
                                "type": "delete_propagation",
                                "source_flag": {"column": "deleted", "values": ["Y"]},
                                "target_flag": {"column": "is_deleted"},
                            },
                            {
                                "type": "substitution_rules",
                                "rules": [
                                    {"column": "middle", "source_null": True, "target_value": "N/A"}
                                ],
                            },
                            {
                                "type": "scope_reconciliation",
                                "excluded_where": "deleted = 'Y'",
                                "expected_excluded": 1,
                                "tolerance_rows": 2,
                            },
                        ],
                    }
                ],
            }
        ],
    }
    result = api.run(api.load_config_data(data, base_dir=tmp_path), write_reports=False).result
    by_type = {c.type: c for c in result.iter_checks()}
    assert violations(by_type["delete_propagation"]) == {"hard_delete_not_propagated": 1}
    assert by_type["substitution_rules"].status is Status.PASSED
    scope = by_type["scope_reconciliation"]
    assert scope.status is Status.PASSED and scope.observed == -2
