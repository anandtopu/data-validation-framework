"""Migration validations against a live PostgreSQL server: the portable SQL (window functions,
derived tables, CASE flags) runs in PostgreSQL when both sides share the connection, and in the
local workspace when PostgreSQL is paired with DuckDB.

Set ``DVF_TEST_POSTGRES_URL`` to run them; every test uses its own ``p3_mig_*`` schemas and
drops them afterwards.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf import api
from dvf.core.enums import Status
from dvf.models.results import RunResult
from tests.e2e.test_migration_checks import SCD2_TARGET, check, violations

URL = os.environ.get("DVF_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="set DVF_TEST_POSTGRES_URL to run PostgreSQL tests"),
]


@pytest.fixture
def pg() -> Iterator[tuple[str, str]]:
    """Two fresh schemas (source, target) in the test server, dropped afterwards."""
    import psycopg

    assert URL is not None
    suffix = secrets.token_hex(3)
    s, t = f"p3_mig_{suffix}_s", f"p3_mig_{suffix}_t"
    with psycopg.connect(URL, autocommit=True) as con:
        con.execute(f"CREATE SCHEMA {s}")
        con.execute(f"CREATE SCHEMA {t}")
    try:
        yield s, t
    finally:
        with psycopg.connect(URL, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
            con.execute(f"DROP SCHEMA IF EXISTS {t} CASCADE")


def _qualify(sql: str, schemas: tuple[str, str]) -> str:
    """Point ``s.`` and ``t.`` table prefixes at the test's schemas."""
    s, t = schemas
    return re.sub(r"\bt\.", f"{t}.", re.sub(r"\bs\.", f"{s}.", sql))


def _execute(statements: list[str], schemas: tuple[str, str]) -> None:
    import psycopg

    assert URL is not None
    with psycopg.connect(URL, autocommit=True) as con:
        for statement in statements:
            con.execute(_qualify(statement, schemas))


def _run(
    tmp_path: Path,
    schemas: tuple[str, str],
    tables: list[dict[str, Any]],
    *,
    target: str = "pg",
    history: bool = False,
) -> RunResult:
    for table in tables:
        for side in ("source", "target"):
            spec = table.get(side)
            on_postgres = side == "source" or target == "pg"
            if spec and on_postgres and isinstance(spec.get("table"), str):
                spec["table"] = _qualify(spec["table"], schemas)
        table.setdefault("comparison", {"enabled": False})
        table.setdefault("schema_checks", [])
    connections: dict[str, Any] = {"pg": {"type": "postgres", "url": URL}}
    if target != "pg":
        connections["duck"] = {"type": "duckdb", "database": (tmp_path / "t.duckdb").as_posix()}
    data = {
        "version": 1,
        "project": {"name": "migration-postgres"},
        "connections": connections,
        "history": {"enabled": history},
        "suites": [{"name": "m", "source": "pg", "target": target, "tables": tables}],
    }
    loaded = api.load_config_data(data, base_dir=tmp_path)
    return api.run(loaded, write_reports=False, history=history).result


def test_scd2_in_postgres(tmp_path: Path, pg: tuple[str, str]) -> None:
    s, _ = pg
    _execute(
        [
            *SCD2_TARGET,
            "CREATE TABLE s.cust_versions AS SELECT * FROM (VALUES "
            "(1), (1), (2), (3), (3), (4), (4), (5), (5), (6), (7), (7), (7)) v(cust_id)",
        ],
        pg,
    )
    result = _run(
        tmp_path,
        pg,
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
                        "history": {"table": f"{s}.cust_versions", "keys": "cust_id"},
                    },
                    {
                        "type": "scd2",
                        "name": "inclusive",
                        "business_key": "cust_id",
                        "valid_from": "valid_from",
                        "valid_to": "valid_to",
                        "end": "inclusive",
                        "where": "cust_id IN (1, 5)",
                    },
                ],
            }
        ],
    )
    exclusive, inclusive = [c for c in result.iter_checks() if c.type == "scd2"]
    assert violations(exclusive) == {
        "invalid_range": 1,
        "no_current": 1,
        "multiple_current": 1,
        "overlap": 2,
        "gap": 1,
        "history_count": 1,
    }
    # Inclusive ends: key 1's next version starts on the old end date, which overlaps; key 5
    # leaves a month out.
    assert violations(inclusive) == {"overlap": 1, "gap": 1}


def test_pair_checks_in_postgres(tmp_path: Path, pg: tuple[str, str]) -> None:
    _, t = pg
    _execute(
        [
            "CREATE TABLE s.cust AS SELECT * FROM (VALUES "
            "(1, 'N', NULL, 'A1', 'OPEN'), (2, 'Y', 'Al', 'A2', 'OPEN'), "
            "(3, 'Y', 'Bo', 'A3', 'OPEN'), (4, 'N', 'Cy', 'A4', 'OPEN'), "
            "(5, 'N', 'Di', 'A5', 'ARCHIVED')) v(id, deleted, middle, cust_no, status)",
            "CREATE TABLE t.cust AS SELECT * FROM (VALUES "
            "(1, false, 'N/A', 1), (2, true, 'Al', 2), (3, false, 'Bo', 3), (4, true, 'N/A', 4), "
            "(9, false, 'Ed', 9)) v(id, is_deleted, middle, new_id)",
            "CREATE TABLE t.xref AS SELECT * FROM (VALUES "
            "('A1', 1), ('A2', 2), ('A3', 3), ('A3', 33), ('A4', 4)) v(legacy_id, new_id)",
        ],
        pg,
    )
    result = _run(
        tmp_path,
        pg,
        [
            {
                "name": "cust",
                "source": {"table": "s.cust", "filter": "status <> 'ARCHIVED'"},
                "target": {"table": "t.cust"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "delete_propagation",
                        "source_flag": {"column": "deleted", "values": ["Y"]},
                        "target_flag": {"column": "is_deleted"},
                    },
                    {
                        "type": "substitution_rules",
                        "rules": [{"column": "middle", "source_null": True, "target_value": "N/A"}],
                    },
                    {
                        "type": "key_map",
                        "map": {
                            "table": f"{t}.xref",
                            "legacy_columns": "legacy_id",
                            "new_columns": "new_id",
                        },
                        "source_key": ["cust_no"],
                        "target_key": ["new_id"],
                    },
                    {
                        "type": "scope_reconciliation",
                        "excluded_where": "status = 'ARCHIVED'",
                        "expected_excluded": 1,
                        "excluded_absent": True,
                    },
                ],
            }
        ],
    )
    by_type = {c.type: c for c in result.iter_checks()}
    assert violations(by_type["delete_propagation"]) == {
        "hard_delete_not_propagated": 1,
        "soft_delete_not_propagated": 1,
        "wrongly_deleted": 1,
    }
    assert violations(by_type["substitution_rules"]) == {"unmatched_1": 1}  # 4: 'Cy' -> 'N/A'
    # A3 maps to 3 and 33; target key 9 is not in the map (archived A5 is out of scope).
    assert violations(by_type["key_map"]) == {"legacy_multi": 1, "unknown_target": 1}
    scope = by_type["scope_reconciliation"]
    assert scope.details["source_rows"] == 5 and scope.details["excluded_rows"] == 1
    assert scope.observed == -1  # five target rows for four in-scope source rows
    for outcome in by_type.values():
        assert all(q.dialect == "postgres" for q in outcome.queries), outcome.check_id


def test_postgres_source_with_duckdb_target(tmp_path: Path, pg: tuple[str, str]) -> None:
    _execute(
        [
            "CREATE TABLE s.cust AS SELECT * FROM (VALUES "
            "(1, 'N', NULL), (2, 'Y', 'Al'), (3, 'Y', 'Bo')) v(id, deleted, middle)"
        ],
        pg,
    )
    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    con.execute(
        "CREATE TABLE cust AS SELECT * FROM (VALUES "
        "(1, false, NULL), (2, true, 'Al'), (4, false, 'Cy')) v(id, is_deleted, middle)"
    )
    con.close()
    result = _run(
        tmp_path,
        pg,
        [
            {
                "name": "cust",
                "source": {"table": "s.cust"},
                "target": {"table": "cust"},
                "keys": ["id"],
                "checks": [
                    {
                        "type": "delete_propagation",
                        "source_flag": {"column": "deleted", "values": ["Y"]},
                        "target_flag": {"column": "is_deleted"},
                        "soft_deleted_rows": "flagged",
                    },
                    {
                        "type": "substitution_rules",
                        "rules": [{"column": "middle", "source_null": True, "target_value": "N/A"}],
                    },
                ],
            }
        ],
        target="duck",
    )
    by_type = {c.type: c for c in result.iter_checks()}
    assert violations(by_type["delete_propagation"]) == {
        "hard_delete_not_propagated": 1,  # 4
        "soft_deleted_missing": 1,  # 3
    }
    assert violations(by_type["substitution_rules"]) == {"rule_1": 1}
    engines = {q.engine for q in by_type["delete_propagation"].queries}
    assert "duckdb" in engines  # the rule ran in the local workspace


def test_single_side_checks_in_postgres(tmp_path: Path, pg: tuple[str, str]) -> None:
    s, _ = pg
    _execute(
        [
            "CREATE TABLE s.changes AS SELECT * FROM (VALUES "
            "(1, 'I', 1, 'a'), (1, 'U', 2, 'b'), (2, 'I', 1, 'x'), (2, 'D', 2, NULL), "
            "(3, 'I', 1, 'c')) v(cust_id, op, seq, name)",
            "CREATE TABLE t.cust AS SELECT * FROM (VALUES "
            "(1, 'a', TIMESTAMP '2026-01-02 10:00:00', 'B1', 'SAP', DATE '2026-01-31', "
            "TIMESTAMP '2021-03-28 02:30:00', 'CafÃ©'), "
            "(2, 'x', NULL, 'B2', 'XXX', DATE '2026-02-02', TIMESTAMP '2021-10-31 02:30:00', "
            "'ok'), "
            "(3, 'c', TIMESTAMP '2026-02-01 08:00:00', 'B1', 'SAP', DATE '2026-01-30', "
            "TIMESTAMP '2021-06-01 12:00:00', '€uro')"
            ") v(id, name, loaded_at, batch_id, src_sys, posting_date, local_ts, label)",
        ],
        pg,
    )
    result = _run(
        tmp_path,
        pg,
        [
            {
                "name": "cust",
                "source": {"table": "s.changes"},
                "target": {"table": "t.cust"},
                "keys": [{"source": "cust_id", "target": "id"}],
                "checks": [
                    {
                        "type": "cdc_applied",
                        "changes": {
                            "table": f"{s}.changes",
                            "key": "cust_id",
                            "operation": "op",
                            "sequence": "seq",
                        },
                        "compare_columns": [{"change": "name"}],
                    },
                    {
                        "type": "audit_columns",
                        "load_timestamp": "loaded_at",
                        "batch_id": "batch_id",
                        "source_system": "src_sys",
                        "max_batches": 1,
                        "allowed_source_systems": ["SAP"],
                    },
                    {
                        "type": "late_arriving",
                        "event_column": "posting_date",
                        "load_column": "loaded_at",
                        "period_start": "2026-02-01",
                        "max_lateness": "1d",
                    },
                    {
                        "type": "dst_edge_cases",
                        "columns": ["local_ts"],
                        "timezone": "Europe/Berlin",
                    },
                    {"type": "charset_conversion", "columns": ["label"], "code_page": "cp037"},
                    {"type": "idempotency"},
                ],
            }
        ],
        history=True,
    )
    by_type = {c.type: c for c in result.iter_checks()}
    assert violations(by_type["cdc_applied"]) == {"delete_present": 1, "value_mismatch": 1}
    assert violations(by_type["audit_columns"]) == {
        "load_timestamp_null": 1,
        "too_many_batches": 1,
        "source_system_not_allowed": 1,
    }
    assert by_type["late_arriving"].unexpected_count == 1  # 3: posted in January, loaded in Feb
    assert by_type["dst_edge_cases"].details["violations"] == {"nonexistent": 1, "ambiguous": 1}
    assert by_type["charset_conversion"].details["violations"]["mojibake"] == 1
    assert by_type["charset_conversion"].details["violations"]["round_trip"] == 1
    assert by_type["idempotency"].status is Status.PASSED
    assert by_type["idempotency"].details["row_count"] == 3


def test_window_and_in_flight_in_postgres(tmp_path: Path, pg: tuple[str, str]) -> None:
    _execute(
        [
            "CREATE TABLE s.o AS SELECT * FROM (VALUES "
            "(1, 10, TIMESTAMP '2026-01-10 09:00:00'), (2, 21, TIMESTAMP '2026-01-10 11:55:00'), "
            "(3, 30, TIMESTAMP '2026-01-10 11:58:00'), (4, 41, TIMESTAMP '2026-01-10 11:59:00'), "
            "(5, 50, TIMESTAMP '2025-12-01 00:00:00')) v(id, amount, changed_at)",
            "CREATE TABLE t.o AS SELECT * FROM (VALUES "
            "(1, 10, TIMESTAMP '2026-01-10 09:00:00'), (2, 20, TIMESTAMP '2026-01-09 10:00:00'), "
            "(4, 40, TIMESTAMP '2026-01-08 10:00:00')) v(id, amount, changed_at)",
        ],
        pg,
    )
    result = _run(
        tmp_path,
        pg,
        [
            {
                "name": "o",
                "source": {"table": "s.o"},
                "target": {"table": "t.o"},
                "keys": ["id"],
                "columns": {"exclude": ["changed_at"]},
                "window": {"column": "changed_at", "start": "2026-01-01"},
                "in_flight": {
                    "column": "changed_at",
                    "window": "15m",
                    "cutoff": "2026-01-10T12:00:00",
                },
                "comparison": {"strategies": ["row_count", "full_diff"], "on_mismatch": None},
            }
        ],
    )
    dataset = result.suites[0].datasets[0]
    assert dataset.status is Status.PASSED, [(c.check_id, c.message) for c in dataset.checks]
    assert dataset.details["in_flight_keys_excluded"] == 3
    assert dataset.row_counts.source == 1 and dataset.row_counts.target == 1


def test_baseline_capture_from_postgres(tmp_path: Path, pg: tuple[str, str]) -> None:
    _execute(
        [
            "CREATE TABLE s.acct AS SELECT g AS id, g * 1.5 AS amount, "
            "CASE WHEN g % 2 = 0 THEN 'EU' ELSE 'US' END AS region FROM generate_series(1, 10) g",
            "CREATE TABLE t.acct AS SELECT * FROM s.acct WHERE id <> 10",
        ],
        pg,
    )
    s, t = pg
    data = {
        "version": 1,
        "project": {"name": "pg-baseline"},
        "history": {"enabled": False},
        "connections": {"pg": {"type": "postgres", "url": URL}},
        "suites": [
            {
                "name": "m",
                "source": "pg",
                "target": "pg",
                "tables": [
                    {
                        "name": "acct",
                        "source": {"table": f"{s}.acct"},
                        "target": {"table": f"{t}.acct"},
                        "keys": ["id"],
                        "schema_checks": [],
                        "comparison": {"enabled": False, "group_by": ["region"]},
                        "checks": [
                            {
                                "type": "baseline_metrics",
                                "baseline": "pre",
                                "metrics": ["row_count", "sum", "max", "groups"],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(data, base_dir=tmp_path)
    _, document = api.capture_baseline(loaded, "pre")
    entry = document["tables"]["m.acct"]
    assert entry["row_count"] == 10 and entry["columns"]["amount"]["sum"] == "82.5"
    outcome = check(api.run(loaded, write_reports=False).result, "baseline_metrics")
    assert outcome.status is Status.FAILED
    failed = {m.metric for m in outcome.metrics if not m.passed}
    assert failed == {"row_count", "sum", "max", "group_row_count"}
