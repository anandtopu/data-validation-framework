"""Migration validations inside MySQL, SQL Server, and Oracle.

The same data and expectations as ``test_migration_postgres.py``: SCD Type 2 rules (window
functions and NULL ordering) and the pair checks run in the server when both sides share its
connection. Each backend skips unless its ``DVF_TEST_*_URL`` is set (see tests/server_backends.py).
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dvf import api
from dvf.models.results import RunResult
from tests.e2e.test_migration_checks import violations
from tests.server_backends import SERVERS, open_connector, server_url

pytestmark = pytest.mark.integration

_INT = {"mysql": "INT", "mssql": "INT", "oracle": "NUMBER(9)"}
_TEXT = {"mysql": "VARCHAR(20)", "mssql": "VARCHAR(20)", "oracle": "VARCHAR2(20)"}
_FLAG = {"mysql": "SMALLINT", "mssql": "SMALLINT", "oracle": "NUMBER(1)"}

SCD2_ROWS = [
    (1, "2020-01-01", "2021-01-01", 0),
    (1, "2021-01-01", None, 1),
    (2, "2020-01-01", "9999-12-31", 1),
    (3, "2020-01-01", None, 1),
    (3, "2021-01-01", None, 1),
    (4, "2020-01-01", "2021-06-01", 0),
    (4, "2021-01-01", None, 1),
    (5, "2020-01-01", "2020-06-01", 0),
    (5, "2020-07-01", None, 1),
    (6, "2020-01-01", "2021-01-01", 0),
    (7, "2021-01-01", "2021-01-01", 0),
    (7, "2021-01-01", None, 1),
]


class _Server:
    def __init__(self, name: str) -> None:
        self.name = name
        self.url = server_url(name)
        self.connector = open_connector(name, self.url)
        self.prefix = f"p3mig_{secrets.token_hex(3)}"
        self.created: list[str] = []

    def table(self, suffix: str) -> str:
        name = f"{self.prefix}_{suffix}"
        return name.upper() if self.name == "oracle" else name

    def execute(self, sql: str) -> None:
        raw = self.connector.connection  # type: ignore[attr-defined]
        cur = raw.cursor()
        try:
            cur.execute(sql)
        finally:
            cur.close()
        if self.name in ("oracle", "mssql"):
            raw.commit()

    def literal(self, value: Any, kind: str) -> str:
        if value is None:
            return "NULL"
        if kind == "date":
            return f"'{value}'" if self.name == "mssql" else f"DATE '{value}'"
        if kind == "text":
            return "'" + str(value).replace("'", "''") + "'"
        return str(int(value))

    def create(
        self, suffix: str, columns: list[tuple[str, str]], rows: list[tuple[Any, ...]]
    ) -> str:
        table = self.table(suffix)
        types = {
            "int": _INT[self.name],
            "text": _TEXT[self.name],
            "flag": _FLAG[self.name],
            "date": "DATE",
        }
        self.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {types[k]}' for c, k in columns)})")
        self.created.append(table)
        for row in rows:
            values = ", ".join(self.literal(v, k) for v, (_, k) in zip(row, columns, strict=True))
            self.execute(f"INSERT INTO {table} VALUES ({values})")
        return table

    def close(self) -> None:
        for table in self.created:
            with contextlib.suppress(Exception):
                self.execute(
                    f"DROP TABLE {table} PURGE" if self.name == "oracle" else f"DROP TABLE {table}"
                )
        self.connector.close()

    def run(self, tmp_path: Path, tables: list[dict[str, Any]]) -> RunResult:
        for table in tables:
            table.setdefault("comparison", {"enabled": False})
            table.setdefault("schema_checks", [])
        data = {
            "version": 1,
            "project": {"name": f"migration-{self.name}"},
            "connections": {"srv": {"type": self.name, "url": self.url}},
            "history": {"enabled": False},
            "suites": [{"name": "m", "source": "srv", "target": "srv", "tables": tables}],
        }
        loaded = api.load_config_data(data, base_dir=tmp_path)
        return api.run(loaded, write_reports=False, history=False).result


@pytest.fixture(params=SERVERS)
def server(request: pytest.FixtureRequest) -> Iterator[_Server]:
    srv = _Server(request.param)
    try:
        yield srv
    finally:
        srv.close()


def test_scd2_in_server(tmp_path: Path, server: _Server) -> None:
    hist = server.create(
        "hist",
        [("cust_id", "int"), ("valid_from", "date"), ("valid_to", "date"), ("is_current", "flag")],
        SCD2_ROWS,
    )
    versions = server.create(
        "versions",
        [("cust_id", "int")],
        [(k,) for k in (1, 1, 2, 3, 3, 4, 4, 5, 5, 6, 7, 7, 7)],
    )
    result = server.run(
        tmp_path,
        [
            {
                "name": "cust_hist",
                "source": {"table": versions},
                "target": {"table": hist},
                "checks": [
                    {
                        "type": "scd2",
                        "business_key": "cust_id",
                        "valid_from": "valid_from",
                        "valid_to": "valid_to",
                        "open_end": "9999-12-31",
                        "current_flag": {"column": "is_current"},
                        "history": {"table": versions, "keys": "cust_id"},
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
    checks = [c for c in result.iter_checks() if c.type == "scd2"]
    assert len(checks) == 2, [(c.check_id, c.status, c.message) for c in result.iter_checks()]
    exclusive, inclusive = checks
    assert exclusive.error is None and inclusive.error is None, (exclusive.error, inclusive.error)
    assert violations(exclusive) == {
        "invalid_range": 1,
        "no_current": 1,
        "multiple_current": 1,
        "overlap": 2,
        "gap": 1,
        "history_count": 1,
    }
    assert violations(inclusive) == {"overlap": 1, "gap": 1}
    assert all(q.dialect == server.name for c in checks for q in c.queries)


def test_pair_checks_in_server(tmp_path: Path, server: _Server) -> None:
    source = server.create(
        "src",
        [
            ("id", "int"),
            ("deleted", "text"),
            ("middle", "text"),
            ("cust_no", "text"),
            ("status", "text"),
        ],
        [
            (1, "N", None, "A1", "OPEN"),
            (2, "Y", "Al", "A2", "OPEN"),
            (3, "Y", "Bo", "A3", "OPEN"),
            (4, "N", "Cy", "A4", "OPEN"),
            (5, "N", "Di", "A5", "ARCHIVED"),
        ],
    )
    target = server.create(
        "tgt",
        [("id", "int"), ("is_deleted", "flag"), ("middle", "text"), ("new_id", "int")],
        [(1, 0, "N/A", 1), (2, 1, "Al", 2), (3, 0, "Bo", 3), (4, 1, "N/A", 4), (9, 0, "Ed", 9)],
    )
    xref = server.create(
        "xref",
        [("legacy_id", "text"), ("new_id", "int")],
        [("A1", 1), ("A2", 2), ("A3", 3), ("A3", 33), ("A4", 4)],
    )
    result = server.run(
        tmp_path,
        [
            {
                "name": "cust",
                "source": {"table": source, "filter": "status <> 'ARCHIVED'"},
                "target": {"table": target},
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
                            "table": xref,
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
    for outcome in by_type.values():
        assert outcome.error is None, (outcome.check_id, outcome.error)
    assert violations(by_type["delete_propagation"]) == {
        "hard_delete_not_propagated": 1,
        "soft_delete_not_propagated": 1,
        "wrongly_deleted": 1,
    }
    assert violations(by_type["substitution_rules"]) == {"unmatched_1": 1}
    assert violations(by_type["key_map"]) == {"legacy_multi": 1, "unknown_target": 1}
    scope = by_type["scope_reconciliation"]
    assert scope.details["source_rows"] == 5 and scope.details["excluded_rows"] == 1
    assert scope.observed == -1
    for outcome in by_type.values():
        assert all(q.dialect == server.name for q in outcome.queries), outcome.check_id
