from __future__ import annotations

import sqlite3
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf.config.models import SinkConfig
from dvf.core.enums import Status
from dvf.core.errors import ConfigError, ConnectorError, DependencyError
from dvf.plugins.base import ResultSink
from dvf.plugins.registry import Registry
from dvf.sinks import postgres as postgres_module
from dvf.sinks._schema import CHECKS, COLUMN_MISMATCHES, RUNS, TABLES, clean
from dvf.sinks.builtin import BUILTIN_SINKS
from dvf.sinks.duckdb import DuckDBSink
from dvf.sinks.postgres import PostgresSink
from dvf.sinks.sql import SqlResultSink
from tests.unit.reporting_fixtures import BASELINE_RUN_ID, build_run, many_checks_run

FILE_SINKS = ["duckdb", "sqlite"]


def _config(kind: str, **fields: Any) -> SinkConfig:
    return SinkConfig.model_validate({"type": kind, **fields})


def _sink(kind: str, base: Path, **fields: Any) -> SqlResultSink:
    fields.setdefault("path", "results/dvf_results.db")
    sink = BUILTIN_SINKS[kind](_config(kind, **fields), base)
    assert isinstance(sink, SqlResultSink)
    return sink


Query = Callable[[str, tuple[Any, ...]], list[tuple[Any, ...]]]


@pytest.fixture
def query_for() -> Iterator[Callable[[SqlResultSink], Query]]:
    """Open the sink's database file and return a query function (closed after the test)."""
    opened: list[Any] = []

    def factory(sink: SqlResultSink) -> Query:
        path = str(sink.path)  # type: ignore[attr-defined]
        con: Any = duckdb.connect(path) if isinstance(sink, DuckDBSink) else sqlite3.connect(path)
        opened.append(con)

        def run_query(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
            return list(con.execute(sql, list(params)).fetchall())

        return run_query

    yield factory
    for con in opened:
        con.close()


def _counts(query: Query, prefix: str = "dvf_", schema: str | None = None) -> dict[str, int]:
    qualifier = f'"{schema}".' if schema else ""
    return {
        table.name: query(f'SELECT COUNT(*) FROM {qualifier}"{prefix}{table.name}"', ())[0][0]
        for table in TABLES
    }


def _as_dicts(query: Query, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    table = sql.split("FROM ", 1)[1].split()[0].strip('"')
    columns = {"dvf_runs": RUNS, "dvf_checks": CHECKS, "dvf_column_mismatches": COLUMN_MISMATCHES}
    names = columns[table].column_names
    return [dict(zip(names, row, strict=True)) for row in query(sql, params)]


# ---------------------------------------------------------------------- registry map


def test_builtin_sinks_map() -> None:
    assert set(BUILTIN_SINKS) == {"duckdb", "sqlite", "postgres"}
    for cls in BUILTIN_SINKS.values():
        assert issubclass(cls, ResultSink)
        assert cls.__doc__
    registry = Registry()
    for name, cls in BUILTIN_SINKS.items():
        registry.register("sink", name, cls, origin="builtin")
    assert registry.get("sink", "duckdb").implementation is DuckDBSink


# ---------------------------------------------------------------------- file sinks


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_writes_every_table_and_is_idempotent(
    kind: str, tmp_path: Path, query_for: Callable[[SqlResultSink], Query]
) -> None:
    run = build_run()
    sink = _sink(kind, tmp_path)
    sink.write(run)
    sink.write(run)  # writing the same run again replaces its rows
    assert sink.path == tmp_path / "results" / "dvf_results.db"  # type: ignore[attr-defined]
    query = query_for(sink)
    assert _counts(query) == {"runs": 1, "checks": 11, "column_mismatches": 4}

    (row,) = _as_dicts(query, 'SELECT * FROM "dvf_runs"')
    assert row["run_id"] == run.run_id and row["project"] == "synthetic-migration"
    assert row["status"] == "error"
    assert row["quality_score"] == 50.0
    assert (row["checks_total"], row["checks_passed"], row["checks_failed"]) == (11, 3, 3)
    assert (row["checks_error"], row["checks_skipped"], row["checks_waived"]) == (1, 2, 1)
    assert row["checks_warning"] == 1
    assert row["rows_compared"] == 60238 and row["duration_ms"] == 3000.0
    assert row["profile"] == "ci" and row["config_hash"] == "sha256:feedface"
    assert row["git_sha"].startswith("0123456789") and row["dvf_version"] == "0.1.0"
    assert row["baseline_run_id"] == BASELINE_RUN_ID and row["new_failures"] == 2
    assert not row["interrupted"] and not row["resumed_from_checkpoint"]
    if kind == "duckdb":
        assert row["started_at"] == datetime(2026, 11, 6, 9, 30)  # UTC, no time zone
        assert row["interrupted"] is False
    else:
        assert row["started_at"] == "2026-11-06 09:30:00.000000"
        assert row["interrupted"] == 0

    checks = {
        c["check_id"]: c
        for c in _as_dicts(query, 'SELECT * FROM "dvf_checks" ORDER BY "check_index"')
    }
    assert list(checks) == [c.check_id for c in run.iter_checks()]
    compare = checks["sales_migration.customers.column_compare"]
    assert compare["check_index"] == 1
    assert (compare["suite"], compare["dataset"]) == ("sales_migration", "customers")
    assert compare["message"] == "38 rows differ | see side-by-side samples"
    assert compare["columns"] == "email, full_name, balance, created_at"
    assert compare["side"] == "both" and compare["severity"] == "critical"
    assert compare["dimension"] == "reconciliation" and compare["status"] == "failed"
    assert (compare["unexpected_count"], compare["total_count"]) == (38, 9988)
    assert compare["unexpected_pct"] == pytest.approx(0.380457)
    assert compare["partition"] == "region=EU" and compare["duration_ms"] == 2000.0
    assert bool(checks["sales_migration.orders.accepted_values.status"]["waived"]) is True
    assert bool(checks["sales_migration.customers.row_count"]["waiver_expired"]) is True
    assert bool(checks["sales_migration.customers.row_count"]["waived"]) is False
    assert checks["sales_migration.orders.not_null.order_id"]["columns"] == "order_id"

    mismatches = {m["column"]: m for m in _as_dicts(query, 'SELECT * FROM "dvf_column_mismatches"')}
    assert set(mismatches) == {"email", "full_name", "balance", "created_at"}
    assert mismatches["email"]["top_classification"] == "whitespace_only"
    assert mismatches["email"]["source_column"] == "EMAIL_ADDR"
    assert mismatches["email"]["mismatch_count"] == 15 and mismatches["email"]["check_index"] == 1
    assert bool(mismatches["full_name"]["waived"]) and not bool(mismatches["email"]["waived"])


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_keeps_other_runs(
    kind: str, tmp_path: Path, query_for: Callable[[SqlResultSink], Query]
) -> None:
    sink = _sink(kind, tmp_path)
    first = build_run()
    second = many_checks_run(25)
    sink.write(first)
    sink.write(second)
    sink.write(first)
    query = query_for(sink)
    assert _counts(query) == {"runs": 2, "checks": 36, "column_mismatches": 4}
    per_run = dict(query('SELECT "run_id", COUNT(*) FROM "dvf_checks" GROUP BY "run_id"', ()))
    assert per_run == {first.run_id: 11, second.run_id: 25}


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_prefix_and_absolute_path(
    kind: str, tmp_path: Path, query_for: Callable[[SqlResultSink], Query]
) -> None:
    target = tmp_path / "abs" / "nested" / f"results.{kind}"
    sink = _sink(kind, tmp_path / "elsewhere", path=str(target), table_prefix="qa_")
    sink.write(build_run())
    assert target.is_file()
    assert _counts(query_for(sink), prefix="qa_") == {
        "runs": 1,
        "checks": 11,
        "column_mismatches": 4,
    }


def test_duckdb_sink_schema(tmp_path: Path, query_for: Callable[[SqlResultSink], Query]) -> None:
    sink = _sink("duckdb", tmp_path, schema="data quality")
    sink.write(build_run())
    sink.write(build_run())
    query = query_for(sink)
    assert _counts(query, schema="data quality")["checks"] == 11
    tables = query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
        ("data quality",),
    )
    assert sorted(t[0] for t in tables) == ["dvf_checks", "dvf_column_mismatches", "dvf_runs"]


def test_sqlite_sink_rejects_schema(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not support 'schema'"):
        _sink("sqlite", tmp_path, schema="dq")


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_config_errors(kind: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs 'path'"):
        BUILTIN_SINKS[kind](_config(kind), tmp_path)
    with pytest.raises(ConfigError, match="'batch_size' must be a positive integer"):
        _sink(kind, tmp_path, options={"batch_size": 0})
    with pytest.raises(ConfigError, match="'batch_size' must be a positive integer"):
        _sink(kind, tmp_path, options={"batch_size": True})
    with pytest.raises(ConfigError, match=r"Unknown option\(s\).*sslmode.*Supported: batch_size"):
        _sink(kind, tmp_path, options={"sslmode": "require"})


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_small_batches(
    kind: str, tmp_path: Path, query_for: Callable[[SqlResultSink], Query]
) -> None:
    sink = _sink(kind, tmp_path, options={"batch_size": 7})
    sink.write(many_checks_run(30))
    assert _counts(query_for(sink))["checks"] == 30


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_rolls_back_a_failed_write(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query_for: Callable[[SqlResultSink], Query],
) -> None:
    run = build_run()
    sink = _sink(kind, tmp_path)
    sink.write(run)
    original = sink.insert
    driver_error = duckdb.Error if kind == "duckdb" else sqlite3.OperationalError

    def failing_insert(session: Any, table: Any, rows: Any) -> None:
        if table is CHECKS:
            raise driver_error("disk full (simulated)")
        original(session, table, rows)

    monkeypatch.setattr(sink, "insert", failing_insert)
    with pytest.raises(ConnectorError, match=r"Could not write run .* disk full") as info:
        sink.write(run)
    assert "Nothing was written" in (info.value.hint or "")
    # The delete and the runs insert were rolled back with the failed checks insert.
    assert _counts(query_for(sink)) == {"runs": 1, "checks": 11, "column_mismatches": 4}

    def broken_insert(session: Any, table: Any, rows: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(sink, "insert", broken_insert)
    with pytest.raises(RuntimeError, match="bug"):  # not a driver error: raised as is
        sink.write(many_checks_run(3))
    assert _counts(query_for(sink))["runs"] == 1


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_adds_missing_columns(
    kind: str, tmp_path: Path, query_for: Callable[[SqlResultSink], Query]
) -> None:
    """Tables created by an older dvf version gain the new columns."""
    sink = _sink(kind, tmp_path)
    sink.path.parent.mkdir(parents=True)  # type: ignore[attr-defined]
    path = str(sink.path)  # type: ignore[attr-defined]
    con: Any = duckdb.connect(path) if kind == "duckdb" else sqlite3.connect(path)
    con.execute('CREATE TABLE "dvf_checks" ("run_id" TEXT, "check_id" TEXT)')
    if kind == "sqlite":
        con.commit()
    con.close()
    sink.write(build_run())
    query = query_for(sink)
    rows = query(
        'SELECT "check_id", "status", "waived" FROM "dvf_checks" ORDER BY "check_index"', ()
    )
    assert len(rows) == 11 and rows[0][:2] == ("sales_migration.customers.column_compare", "failed")


def test_sqlite_sink_cannot_open(tmp_path: Path) -> None:
    folder = tmp_path / "a_folder"
    folder.mkdir()
    sink = _sink("sqlite", tmp_path, path=str(folder))
    with pytest.raises(ConnectorError, match="Could not open the SQLite result sink") as info:
        sink.write(build_run())
    assert str(folder) in (info.value.hint or "")


def test_duckdb_sink_cannot_open(tmp_path: Path) -> None:
    not_a_database = tmp_path / "notes.duckdb"
    not_a_database.write_text("this is not a database\n" * 100, encoding="utf-8")
    sink = _sink("duckdb", tmp_path, path=str(not_a_database))
    with pytest.raises(ConnectorError, match="Could not open the DuckDB result sink"):
        sink.write(build_run())


@pytest.mark.parametrize("kind", FILE_SINKS)
def test_file_sink_values_are_parameters(
    kind: str, tmp_path: Path, query_for: Callable[[SqlResultSink], Query]
) -> None:
    hostile = "x'); DROP TABLE dvf_runs; --"
    run = build_run().model_copy(
        update={
            "run_id": hostile,
            "started_at": datetime(2026, 11, 6, 11, 30, tzinfo=timezone(timedelta(hours=2))),
        }
    )
    run.suites[0].datasets[0].checks[0] = (
        run.suites[0]
        .datasets[0]
        .checks[0]
        .model_copy(update={"message": "bad\x00byte", "unexpected_pct": float("nan")})
    )
    sink = _sink(kind, tmp_path)
    sink.write(run)
    query = query_for(sink)
    (row,) = _as_dicts(query, 'SELECT * FROM "dvf_runs"')
    assert row["run_id"] == hostile
    expected = datetime(2026, 11, 6, 9, 30)  # converted to UTC
    assert row["started_at"] == (expected if kind == "duckdb" else "2026-11-06 09:30:00.000000")
    first = query(
        'SELECT "message", "unexpected_pct" FROM "dvf_checks" WHERE "check_index" = 1', ()
    )
    assert first == [("badbyte", None)]


def test_clean_values() -> None:
    assert clean(Status.FAILED) == "failed"
    assert clean(float("inf")) is None and clean(1.5) == 1.5
    assert clean(True) is True and clean(None) is None and clean(7) == 7
    assert clean(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)
    long = clean("y" * 9000)
    assert len(long) == 8000 and long.endswith("…")


# ---------------------------------------------------------------------- PostgreSQL (fake driver)


class FakeError(Exception):
    pass


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeCursor:
    def __init__(self, con: FakeConnection) -> None:
        self.con = con

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        if self.con.fail_on and self.con.fail_on in sql:
            raise FakeError("value too long (simulated)")
        self.con.batches.append((sql, list(rows)))


class FakeConnection:
    def __init__(self, namespace: bool, columns: dict[str, list[str]], fail_on: str | None) -> None:
        self.namespace = namespace
        self.columns = columns
        self.fail_on = fail_on
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.batches: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.committed = self.rolled_back = self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> FakeResult:
        self.statements.append((sql, params))
        if "pg_namespace" in sql:
            return FakeResult([(1,)] if self.namespace else [])
        if "information_schema.columns" in sql:
            return FakeResult([(name,) for name in self.columns.get(params[1], [])])
        return FakeResult([])

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakePsycopg:
    Error = FakeError

    def __init__(
        self,
        *,
        namespace: bool = False,
        columns: dict[str, list[str]] | None = None,
        fail_on: str | None = None,
        connect_error: str | None = None,
    ) -> None:
        self.namespace = namespace
        self.columns = columns or {}
        self.fail_on = fail_on
        self.connect_error = connect_error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connection: FakeConnection | None = None

    def connect(self, conninfo: str, **kwargs: Any) -> FakeConnection:
        self.calls.append((conninfo, kwargs))
        if self.connect_error:
            raise FakeError(self.connect_error)
        self.connection = FakeConnection(self.namespace, self.columns, self.fail_on)
        return self.connection


URL = "postgresql+psycopg://dq_writer:s3cr3t-pw@db.example.test:5432/quality"


def _postgres(monkeypatch: pytest.MonkeyPatch, fake: FakePsycopg, **fields: Any) -> PostgresSink:
    monkeypatch.setattr(postgres_module, "_import_psycopg", lambda: fake)
    fields.setdefault("url", URL)
    return PostgresSink(_config("postgres", **fields), Path.cwd())


def test_postgres_sink_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePsycopg()
    sink = _postgres(monkeypatch, fake, schema="dq", options={"sslmode": "require"})
    run = build_run()
    sink.write(run)

    ((conninfo, kwargs),) = fake.calls
    assert conninfo == "postgresql://dq_writer:s3cr3t-pw@db.example.test:5432/quality"
    assert kwargs == {"autocommit": False, "connect_timeout": 15, "sslmode": "require"}
    con = fake.connection
    assert con is not None and con.committed and con.closed and not con.rolled_back

    sql = [statement for statement, _ in con.statements]
    assert sql[0] == "SELECT 1 FROM pg_namespace WHERE nspname = %s"
    assert con.statements[0][1] == ("dq",)
    assert 'CREATE SCHEMA IF NOT EXISTS "dq"' in sql
    creates = [s for s in sql if s.startswith("CREATE TABLE")]
    assert [c.split("(")[0].strip() for c in creates] == [
        'CREATE TABLE IF NOT EXISTS "dq"."dvf_runs"',
        'CREATE TABLE IF NOT EXISTS "dq"."dvf_checks"',
        'CREATE TABLE IF NOT EXISTS "dq"."dvf_column_mismatches"',
    ]
    assert '"started_at" TIMESTAMPTZ' in creates[0] and 'PRIMARY KEY ("run_id")' in creates[0]
    assert '"unexpected_pct" DOUBLE PRECISION' in creates[1] and '"waived" BOOLEAN' in creates[1]
    assert 'PRIMARY KEY ("run_id", "check_index")' in creates[1]
    assert '"column" TEXT' in creates[2]
    deletes = [(s, p) for s, p in con.statements if s.startswith("DELETE")]
    assert deletes == [
        ('DELETE FROM "dq"."dvf_runs" WHERE "run_id" = %s', (run.run_id,)),
        ('DELETE FROM "dq"."dvf_checks" WHERE "run_id" = %s', (run.run_id,)),
        ('DELETE FROM "dq"."dvf_column_mismatches" WHERE "run_id" = %s', (run.run_id,)),
    ]
    inserts = {s.split('"."')[1].split('"')[0]: rows for s, rows in con.batches}
    assert {name: len(rows) for name, rows in inserts.items()} == {
        "dvf_runs": 1,
        "dvf_checks": 11,
        "dvf_column_mismatches": 4,
    }
    for statement, rows in con.batches:
        marks = statement.split("VALUES (", 1)[1].rstrip(")").split(", ")
        assert set(marks) == {"%s"} and all(len(row) == len(marks) for row in rows)
    executed = " ".join(sql) + " ".join(s for s, _ in con.batches)
    assert run.run_id not in executed  # every value is a parameter
    run_row = inserts["dvf_runs"][0]
    assert run_row[3] == datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
    assert isinstance(run_row[RUNS.column_names.index("interrupted")], bool)


def test_postgres_sink_existing_schema_and_new_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePsycopg(
        namespace=True,
        columns={
            "dvf_runs": RUNS.column_names,
            "dvf_checks": [c for c in CHECKS.column_names if c != "waiver_expired"],
            "dvf_column_mismatches": COLUMN_MISMATCHES.column_names,
        },
    )
    sink = _postgres(monkeypatch, fake, schema="dq")
    sink.write(build_run())
    assert fake.connection is not None
    sql = [s for s, _ in fake.connection.statements]
    assert not any(s.startswith("CREATE") for s in sql)
    assert 'ALTER TABLE "dq"."dvf_checks" ADD COLUMN "waiver_expired" BOOLEAN' in sql


def test_postgres_sink_without_schema_uses_current_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePsycopg()
    _postgres(monkeypatch, fake).write(build_run())
    assert fake.connection is not None
    statements = fake.connection.statements
    assert not any("pg_namespace" in s for s, _ in statements)
    lookups = [p for s, p in statements if "information_schema.columns" in s]
    assert lookups[0] == (None, "dvf_runs")
    assert any(s.startswith('CREATE TABLE IF NOT EXISTS "dvf_runs"') for s, _ in statements)


def test_postgres_sink_rolls_back_on_driver_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePsycopg(fail_on="dvf_checks")
    sink = _postgres(monkeypatch, fake)
    with pytest.raises(ConnectorError, match="value too long") as info:
        sink.write(build_run())
    assert isinstance(info.value.__cause__, FakeError)
    con = fake.connection
    assert con is not None and con.rolled_back and con.closed and not con.committed


def test_postgres_sink_connect_error_hides_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePsycopg(connect_error=f'invalid dsn: missing "=" after "{URL}"; password s3cr3t-pw')
    sink = _postgres(monkeypatch, fake)
    with pytest.raises(ConnectorError, match="Could not open the PostgreSQL result sink") as info:
        sink.write(build_run())
    text = str(info.value)
    assert "s3cr3t-pw" not in text and URL not in text and "***" in text
    assert info.value.__cause__ is None  # the driver message held the secret
    assert "url" in (info.value.hint or "")


def test_postgres_sink_config_and_dependency_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="needs 'url'"):
        PostgresSink(_config("postgres"), Path.cwd())
    with pytest.raises(ConfigError, match="needs 'url'"):
        PostgresSink(_config("postgres", url="  "), Path.cwd())
    with pytest.raises(ConfigError, match="'batch_size' must be a positive integer"):
        PostgresSink(_config("postgres", url=URL, options={"batch_size": "10"}), Path.cwd())
    monkeypatch.setitem(sys.modules, "psycopg", None)
    sink = PostgresSink(_config("postgres", url=URL), Path.cwd())  # no import until writing
    with pytest.raises(DependencyError, match="psycopg") as info:
        sink.write(build_run())
    assert "dvf[postgres]" in (info.value.hint or "")


def test_postgres_sink_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePsycopg()
    _postgres(monkeypatch, fake, options={"batch_size": 4}).write(many_checks_run(10))
    assert fake.connection is not None
    sizes = [len(rows) for s, rows in fake.connection.batches if '"dvf_checks"' in s]
    assert sizes == [4, 4, 2]


# ---------------------------------------------------------------------- PostgreSQL (real server)


@pytest.mark.integration
def test_postgres_sink_round_trip(postgres_url: str) -> None:
    import psycopg

    schema = f"dvf_sink_{uuid.uuid4().hex[:10]}"
    sink = PostgresSink(_config("postgres", url=postgres_url, schema=schema), Path.cwd())
    run = build_run()
    try:
        sink.write(run)
        sink.write(run)
        sink.write(many_checks_run(5))
        conninfo = postgres_module._DRIVER_SUFFIX.sub(r"\1://", postgres_url)
        with psycopg.connect(conninfo) as con:
            counts = {
                table.name: con.execute(
                    f'SELECT COUNT(*) FROM "{schema}"."dvf_{table.name}"'
                ).fetchone()
                for table in TABLES
            }
            assert counts == {"runs": (2,), "checks": (16,), "column_mismatches": (4,)}
            row = con.execute(
                f'SELECT "status", "quality_score", "started_at", "interrupted" '
                f'FROM "{schema}"."dvf_runs" WHERE "run_id" = %s',
                (run.run_id,),
            ).fetchone()
            assert row == ("error", 50.0, datetime(2026, 11, 6, 9, 30, tzinfo=UTC), False)
            waived = con.execute(
                f'SELECT "column" FROM "{schema}"."dvf_column_mismatches" WHERE "waived"'
            ).fetchall()
            assert waived == [("full_name",)]
    finally:
        conninfo = postgres_module._DRIVER_SUFFIX.sub(r"\1://", postgres_url)
        with psycopg.connect(conninfo, autocommit=True) as con:
            con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
