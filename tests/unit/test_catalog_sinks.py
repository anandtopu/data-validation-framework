"""Result sinks ``delta`` (local Delta Lake tables) and ``snowflake`` (beta, with a fake driver)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from dvf.config.models import SinkConfig
from dvf.core.errors import ConfigError, ConnectorError
from dvf.packs.catalog.sinks import DeltaSink, SnowflakeSink, arrow_rows, parse_snowflake_url
from dvf.plugins.registry import PluginKind, new_registry
from dvf.sinks._schema import CHECKS, COLUMN_MISMATCHES, RUNS, TABLES
from tests.unit.cloud_fakes import FakeCursor, FakeDriver, Result
from tests.unit.reporting_fixtures import build_run, many_checks_run


def _config(kind: str, **fields: Any) -> SinkConfig:
    return SinkConfig.model_validate({"type": kind, **fields})


def test_sinks_are_registered() -> None:
    registry = new_registry()
    delta = registry.get(PluginKind.SINK, "delta")
    snowflake = registry.get(PluginKind.SINK, "snowflake")
    assert delta.ready and delta.maturity == "stable"
    assert snowflake.ready and snowflake.maturity == "beta"


# ---------------------------------------------------------------------- Delta Lake


def _read(path: Path) -> pa.Table:
    from deltalake import DeltaTable

    return DeltaTable(str(path)).to_pyarrow_table()


def test_delta_sink_writes_and_replaces_runs(tmp_path: Path) -> None:
    run = build_run()
    sink = DeltaSink(_config("delta", path="results/delta"), tmp_path)
    sink.write(run)
    root = tmp_path / "results" / "delta"
    for table in TABLES:
        assert (root / f"dvf_{table.name}" / "_delta_log").is_dir()
    runs = _read(root / "dvf_runs")
    checks = _read(root / "dvf_checks")
    mismatches = _read(root / "dvf_column_mismatches")
    assert runs.num_rows == 1 and runs.column("run_id")[0].as_py() == run.run_id
    assert checks.num_rows == len(list(run.iter_checks()))
    assert mismatches.num_rows == arrow_rows(run, COLUMN_MISMATCHES).num_rows > 0
    assert runs.schema.field("started_at").type == pa.timestamp("us", tz="UTC")
    assert checks.column_names == [c.name for c in CHECKS.columns]

    sink.write(run)  # writing the same run again replaces its rows
    assert _read(root / "dvf_checks").num_rows == checks.num_rows

    other = many_checks_run(3)
    sink.write(other)
    assert sorted(_read(root / "dvf_runs").column("run_id").to_pylist()) == sorted(
        [run.run_id, other.run_id]
    )
    assert _read(root / "dvf_checks").num_rows == checks.num_rows + 3


def test_delta_sink_merges_new_columns(tmp_path: Path) -> None:
    from deltalake import write_deltalake

    root = tmp_path / "delta"
    old = arrow_rows(build_run(), RUNS).drop_columns(["new_failures", "baseline_run_id"])
    write_deltalake(str(root / "dvf_runs"), old, mode="append")
    sink = DeltaSink(_config("delta", path=str(root), table_prefix="dvf_"), tmp_path)
    run = many_checks_run(2)
    sink.write(run)
    table = _read(root / "dvf_runs")
    assert "new_failures" in table.column_names
    assert table.num_rows == 2


def test_delta_sink_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs 'path'"):
        DeltaSink(_config("delta"), tmp_path)
    with pytest.raises(ConfigError, match="Unknown option"):
        DeltaSink(_config("delta", path="x", options={"mode": "append"}), tmp_path)
    with pytest.raises(ConfigError, match="schema"):
        DeltaSink(_config("delta", path="x", schema="s"), tmp_path)
    sink = DeltaSink(
        _config("delta", path="s3://bucket/dvf", options={"storage_options": {"AWS_REGION": "x"}}),
        tmp_path,
    )
    assert sink.table_uri(RUNS) == "s3://bucket/dvf/dvf_runs"
    assert sink.storage_options == {"AWS_REGION": "x"}


def test_delta_sink_errors_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import deltalake

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("request signed with super-secret-value was rejected")

    monkeypatch.setattr(deltalake, "write_deltalake", fail)
    sink = DeltaSink(
        _config(
            "delta",
            path="lake",
            options={"storage_options": {"AWS_SECRET_ACCESS_KEY": "super-secret-value"}},
        ),
        tmp_path,
    )
    with pytest.raises(ConnectorError, match="Could not write run") as info:
        sink.write(build_run())
    assert "super-secret-value" not in str(info.value)


def test_delta_sink_through_a_run(tmp_path: Path) -> None:
    from dvf import api

    (tmp_path / "a.csv").write_text("id,v\n1,x\n2,y\n", encoding="utf-8")
    config = {
        "version": 1,
        "project": {"name": "delta-sink"},
        "connections": {"f": {"type": "files", "path": tmp_path.as_posix()}},
        "suites": [
            {
                "name": "s",
                "source": "f",
                "tables": [
                    {
                        "name": "a",
                        "source": {"path": "a.csv"},
                        "checks": [{"type": "not_null", "columns": ["id"]}],
                    }
                ],
            }
        ],
        "sinks": [{"type": "delta", "path": "lake"}],
        "history": {"enabled": False},
    }
    result = api.run(
        api.load_config_data(config, base_dir=tmp_path, strict=False), write_reports=False
    ).result
    assert "sink_errors" not in result.metadata
    assert _read(tmp_path / "lake" / "dvf_checks").num_rows == len(list(result.iter_checks()))


# ---------------------------------------------------------------------- Snowflake (fake driver)


class _ManyCursor(FakeCursor):
    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.connection.executed.append((sql, rows))


@pytest.fixture
def sf(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    driver = FakeDriver("snowflake.connector", version="3.12.0")
    monkeypatch.setitem(sys.modules, "snowflake.connector", driver.module)
    monkeypatch.setattr("tests.unit.cloud_fakes.FakeConnection.cursor", _cursor_factory)
    return driver


def _cursor_factory(self: Any) -> FakeCursor:
    cursor = _ManyCursor(self)
    self.cursors.append(cursor)
    return cursor


def test_parse_snowflake_url() -> None:
    settings = parse_snowflake_url(
        "snowflake://dvf%40acme:p%40ss@acme-xy12345/ANALYTICS/QA?warehouse=QA_WH&role=WRITER"
    )
    assert settings == {
        "account": "acme-xy12345",
        "user": "dvf@acme",
        "password": "p@ss",
        "database": "ANALYTICS",
        "schema": "QA",
        "warehouse": "QA_WH",
        "role": "WRITER",
    }
    with pytest.raises(ConfigError, match="snowflake://"):
        parse_snowflake_url("postgres://x")


def test_snowflake_sink_writes_one_transaction(tmp_path: Path, sf: FakeDriver) -> None:
    sf.responder.add(r"information_schema\.columns", Result(names=["COLUMN_NAME"], rows=[]))
    sink = SnowflakeSink(
        _config(
            "snowflake",
            url="snowflake://dvf:secret@acme-xy12345/ANALYTICS/DVF?warehouse=QA_WH",
            options={"role": "WRITER", "batch_size": 2},
        ),
        tmp_path,
    )
    run = build_run()
    sink.write(run)
    con = sf.connection
    kwargs = con.kwargs
    assert kwargs["account"] == "acme-xy12345" and kwargs["password"] == "secret"
    assert kwargs["warehouse"] == "QA_WH" and kwargs["role"] == "WRITER"
    assert kwargs["database"] == "ANALYTICS" and kwargs["schema"] == "DVF"
    assert kwargs["session_parameters"]["TIMEZONE"] == "UTC"
    assert kwargs["session_parameters"]["QUERY_TAG"] == "dvf-sink"
    statements = con.statements
    begin = statements.index("BEGIN")
    assert statements[-1] == "COMMIT"
    ddl = [s for s in statements[:begin] if s.startswith("CREATE")]
    assert ddl[0] == 'CREATE SCHEMA IF NOT EXISTS "DVF"'
    assert any(s.startswith('CREATE TABLE IF NOT EXISTS "DVF"."DVF_RUNS"') for s in ddl)
    assert '"RUN_ID" VARCHAR' in ddl[1] and "TIMESTAMP_TZ" in ddl[1]
    deletes = [s for s in statements[begin:] if s.startswith("DELETE")]
    assert deletes[0] == 'DELETE FROM "DVF"."DVF_RUNS" WHERE "RUN_ID" = %s'
    inserts = [(sql, rows) for sql, rows in con.executed if sql.startswith("INSERT")]
    assert inserts and all(len(rows) <= 2 for _, rows in inserts)
    assert sum(len(rows) for sql, rows in inserts if '"DVF_CHECKS"' in sql) == len(
        list(run.iter_checks())
    )
    assert con.closed


def test_snowflake_sink_adds_missing_columns(tmp_path: Path, sf: FakeDriver) -> None:
    existing = [(c.name.upper(),) for c in RUNS.columns if c.name != "new_failures"]
    sf.responder.add(
        r"information_schema\.columns.*",
        lambda sql: Result(names=["COLUMN_NAME"], rows=existing),
    )
    sink = SnowflakeSink(
        _config("snowflake", options={"account": "acme", "user": "u", "password": "p"}), tmp_path
    )
    sink.write(many_checks_run(1))
    alters = [s for s in sf.connection.statements if s.startswith("ALTER TABLE")]
    assert 'ALTER TABLE "DVF_RUNS" ADD COLUMN "NEW_FAILURES" NUMBER(38, 0)' in alters


def test_snowflake_sink_rolls_back_and_redacts(tmp_path: Path, sf: FakeDriver) -> None:
    from tests.unit.cloud_fakes import FakeError

    sf.responder.add(r"^DELETE", FakeError("permission denied for user with password hunter2"))
    sink = SnowflakeSink(
        _config("snowflake", options={"account": "acme", "user": "u", "password": "hunter2"}),
        tmp_path,
    )
    with pytest.raises(ConnectorError) as info:
        sink.write(build_run())
    assert "hunter2" not in str(info.value)
    assert sf.connection.statements[-1] == "ROLLBACK"


def test_snowflake_sink_needs_an_account(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs an account"):
        SnowflakeSink(_config("snowflake", options={"user": "u"}), tmp_path)


def test_snowflake_sink_without_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "snowflake.connector", None)
    sink = SnowflakeSink(_config("snowflake", options={"account": "acme"}), tmp_path)
    with pytest.raises(Exception, match="snowflake"):
        sink.write(build_run())
