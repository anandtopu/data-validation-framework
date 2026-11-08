"""Seeded defects with a MySQL, SQL Server, or Oracle source and a Parquet target.

The same synthetic data and expectations as the PostgreSQL acceptance test: every injected defect
must be found exactly and classified correctly with the ``duckdb`` and ``polars`` engines.
Set ``DVF_TEST_MYSQL_URL``, ``DVF_TEST_MSSQL_URL``, and ``DVF_TEST_ORACLE_URL`` to run them.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dvf.config.connections import DatabaseConnection
from dvf.config.models import DatasetSpec
from dvf.core.enums import Status
from dvf.core.errors import ConnectorError
from dvf.core.types import LogicalType
from dvf.plugins.registry import PluginKind, Registry
from tests.e2e.test_seeded_defects import (
    DATA,
    _check,
    _config,
    _dataset,
    _run,
    _target_files,
    assert_exact,
)
from tests.server_backends import (
    SERVERS,
    Backend,
    connector_class,
    drop_seeded,
    load_seeded,
    make_backend,
    open_connector,
    server_url,
)

pytestmark = pytest.mark.integration

_EXTRAS = {"mysql": "mysql", "mssql": "mssql", "oracle": "oracle"}


@pytest.fixture
def server_registry(default_registry: Registry) -> Registry:
    """The process registry with the server connectors attached (as builtins.py will)."""
    for name in SERVERS:
        declared = default_registry.find(PluginKind.CONNECTOR, name)
        if declared is not None and declared.ready:
            continue
        default_registry.register(
            PluginKind.CONNECTOR,
            name,
            connector_class(name),
            description=declared.description if declared else None,
            config_model=declared.config_model if declared else None,
            origin="builtin",
            extra=_EXTRAS[name],
        )
    return default_registry


@pytest.fixture(scope="module", params=SERVERS)
def seeded(request: pytest.FixtureRequest) -> Iterator[tuple[Backend, dict[str, str]]]:
    """The seeded source tables loaded into one server (removed afterwards)."""
    backend = make_backend(request.param)
    namespace = f"dvf_seeded_{secrets.token_hex(3)}"
    tables = {
        "customers": DATA.source_customers,
        "orders": DATA.source_orders,
        "customers_t": DATA.target_customers,
        "orders_t": DATA.target_orders,
    }
    try:
        names = load_seeded(
            backend, namespace, tables, {"customers": "customer_id", "orders": "order_id"}
        )
        yield backend, names
    finally:
        drop_seeded(backend, namespace, list(tables))
        backend.close()


def _expected(backend: Backend):  # type: ignore[no-untyped-def]
    """The seeded expectations. Oracle ``NUMBER(p)`` with up to 18 digits reads as an integer, so
    every server reports the same type changes."""
    del backend
    return DATA


@pytest.mark.parametrize("engine", ["duckdb", "polars"])
def test_seeded_defects_server_vs_parquet(
    engine: str,
    tmp_path: Path,
    seeded: tuple[Backend, dict[str, str]],
    server_registry: Registry,
) -> None:
    backend, names = seeded
    target = _target_files(tmp_path, DATA)
    source = {"type": backend.name, "url": server_url(backend.name)}
    config = _config(
        source, {"table": names["customers"]}, {"table": names["orders"]}, target, engine
    )
    output = _run(config, tmp_path)
    data = _expected(backend)
    assert_exact(output.result, data)
    customers = _dataset(output.result, "customers")
    assert _check(customers, "compare.row_hash").status is Status.FAILED
    html = (tmp_path / "reports" / "report.html").read_text(encoding="utf-8")
    for labels in DATA.expected.columns.values():
        for label in labels:
            assert label in html


def test_seeded_source_checks_run_on_server(
    tmp_path: Path, seeded: tuple[Backend, dict[str, str]], server_registry: Registry
) -> None:
    """Data quality checks pushed down to the server side (source) find the same defects."""
    backend, names = seeded
    target = _target_files(tmp_path, DATA)
    source = {"type": backend.name, "url": server_url(backend.name)}
    config = _config(
        source, {"table": names["customers"]}, {"table": names["orders"]}, target, "duckdb"
    )
    table = config["suites"][0]["tables"][0]
    table["checks"] = [
        {"type": "unique", "columns": ["customer_id"], "side": "source"},
        {"type": "not_null", "columns": ["email"], "side": "source"},
        {"type": "row_count_range", "min": 1, "side": "source"},
        {"type": "range", "column": "quantity", "min": 0, "max": 500, "side": "source"},
        {"type": "length", "column": "customer_code", "min": 6, "max": 6, "side": "source"},
        {
            "type": "allowed_values",
            "column": "status",
            "values": ["active", "blocked", "prospect"],
            "side": "source",
        },
    ]
    config["suites"][0]["tables"][1]["checks"] = []
    output = _run(config, tmp_path)
    errors = [c for c in output.result.iter_checks() if c.status is Status.ERROR]
    assert not errors, [(c.check_id, c.message) for c in errors]
    customers = _dataset(output.result, "customers")
    for check_type in (
        "unique",
        "not_null",
        "row_count_range",
        "range",
        "length",
        "allowed_values",
    ):
        result = _check(customers, check_type)
        assert result.status is Status.PASSED, (check_type, result.message)


def test_seeded_referential_integrity_on_server(
    tmp_path: Path,
    seeded: tuple[Backend, dict[str, str]],
    server_registry: Registry,
) -> None:
    """Child and parent on the same server connection: the check runs as one SQL statement."""
    backend, names = seeded
    target = _target_files(tmp_path, DATA)
    source = {"type": backend.name, "url": server_url(backend.name)}
    config = _config(
        source, {"table": names["customers"]}, {"table": names["orders"]}, target, "duckdb"
    )
    config["suites"][0]["tables"][0]["checks"] = []
    config["suites"][0]["tables"][1]["checks"] = [
        {
            "type": "referential_integrity",
            "column": "customer_id",
            "side": "source",
            "parent": {"table": names["customers"], "column": "customer_id"},
        }
    ]
    output = _run(config, tmp_path)
    ri = _check(_dataset(output.result, "orders"), "referential_integrity")
    assert ri.status is Status.PASSED, ri.message
    assert ri.unexpected_count == 0


def test_same_connection_pushdown(
    tmp_path: Path,
    seeded: tuple[Backend, dict[str, str]],
    server_registry: Registry,
) -> None:
    """Source and target tables on one server: the diff runs as SQL inside it (SqlDiff)."""
    backend, names = seeded
    target = _target_files(tmp_path, DATA)
    source = {"type": backend.name, "url": server_url(backend.name)}
    config = _config(
        source, {"table": names["customers"]}, {"table": names["orders"]}, target, "pushdown"
    )
    suite = config["suites"][0]
    suite["target"] = "src"
    suite["tables"][0]["target"] = {"table": names["customers_t"]}
    suite["tables"][1]["target"] = {"table": names["orders_t"]}
    # The parent lives in the Parquet files (a cross-connection check works on every server).
    suite["tables"][1]["checks"][0]["parent"] = {
        "connection": "tgt",
        "table": "customers.parquet",
        "column": "customer_id",
    }
    output = _run(config, tmp_path)
    assert_exact(output.result, DATA)
    mismatch = _check(_dataset(output.result, "customers"), "compare.column_mismatch")
    assert mismatch.engine == "pushdown"


# ---------------------------------------------------------------------- connector behavior

_METADATA_COLUMNS: dict[str, list[tuple[str, str, LogicalType]]] = {
    "mysql": [
        ("id", "BIGINT NOT NULL", LogicalType.INTEGER),
        ("code", "VARCHAR(20) DEFAULT 'x'", LogicalType.STRING),
        ("amount", "DECIMAL(12,3)", LogicalType.DECIMAL),
        ("at", "DATETIME(6)", LogicalType.TIMESTAMP),
        ("at_tz", "TIMESTAMP(6) NULL", LogicalType.TIMESTAMP_TZ),
        ("flag", "BOOLEAN", LogicalType.INTEGER),
        ("raw", "VARBINARY(16)", LogicalType.BINARY),
        ("ratio", "DOUBLE", LogicalType.FLOAT),
    ],
    "mssql": [
        ("id", "BIGINT NOT NULL", LogicalType.INTEGER),
        ("code", "NVARCHAR(20) DEFAULT N'x'", LogicalType.STRING),
        ("amount", "DECIMAL(12,3)", LogicalType.DECIMAL),
        ("at", "DATETIME2(6)", LogicalType.TIMESTAMP),
        ("at_tz", "DATETIMEOFFSET(6)", LogicalType.TIMESTAMP_TZ),
        ("flag", "BIT", LogicalType.BOOLEAN),
        ("raw", "VARBINARY(16)", LogicalType.BINARY),
        ("ratio", "FLOAT", LogicalType.FLOAT),
    ],
    "oracle": [
        ("id", "NUMBER(18) NOT NULL", LogicalType.INTEGER),
        ("code", "VARCHAR2(20 CHAR) DEFAULT 'x'", LogicalType.STRING),
        ("amount", "NUMBER(12,3)", LogicalType.DECIMAL),
        ("at", "TIMESTAMP(6)", LogicalType.TIMESTAMP),
        ("at_tz", "TIMESTAMP(6) WITH TIME ZONE", LogicalType.TIMESTAMP_TZ),
        ("flag", "BOOLEAN", LogicalType.BOOLEAN),
        ("raw", "RAW(16)", LogicalType.BINARY),
        ("ratio", "BINARY_DOUBLE", LogicalType.FLOAT),
    ],
}


def _metadata_table(backend: Backend) -> tuple[str, str, dict[str, LogicalType]]:
    """A table with assorted types, a primary key, a default, and 2500 rows."""
    d = backend.dialect
    name = f"dvf_meta_{secrets.token_hex(3)}"
    columns = _METADATA_COLUMNS[backend.name]
    if backend.name == "oracle":
        qualified, config_name = d.quote(name.upper()), name.upper()
    elif backend.name == "mssql":
        qualified, config_name = f"dbo.{d.quote(name)}", f"dbo.{name}"
    else:
        qualified, config_name = d.quote(name), name
    ddl = ", ".join(f"{d.quote(c)} {t}" for c, t, _ in columns)
    backend.execute(f"CREATE TABLE {qualified} ({ddl}, PRIMARY KEY ({d.quote('id')}))")
    insert = f"INSERT INTO {qualified} ({d.quote('id')}, {d.quote('code')})"
    if backend.name == "mssql":
        backend.execute(
            f"{insert} SELECT TOP (2500) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1, N'c' "
            "FROM sys.all_objects a CROSS JOIN sys.all_objects b"
        )
    else:
        params = f"{backend.param(0)}, {backend.param(1)}"
        backend.executemany(f"{insert} VALUES ({params})", [(i, "c") for i in range(2500)])
    return qualified, config_name, {c: t for c, _, t in columns}


def test_connector_metadata_and_streaming(seeded: tuple[Backend, dict[str, str]]) -> None:
    backend, _ = seeded
    qualified, config_name, logical = _metadata_table(backend)
    q = backend.dialect.quote
    connector = open_connector(backend.name, server_url(backend.name), read_only=True)
    try:
        report = connector.test_connection()
        assert report.ok, report.message
        assert (connector.server_version() or "").split()[0] in ("MySQL", "SQL", "Oracle")
        relation = connector.resolve(DatasetSpec(table=config_name))
        assert connector.exists(relation)
        assert not connector.exists(connector.resolve(DatasetSpec(table=config_name + "_x")))
        schema = connector.get_schema(relation)
        assert {c.name: c.type.logical for c in schema.columns} == logical
        assert [c.name for c in schema.columns] == list(logical)
        assert schema.primary_key == ("id",)
        id_column, code_column = schema.column("id"), schema.column("code")
        assert id_column is not None and id_column.nullable is False
        assert code_column is not None and "x" in (code_column.default or "")
        query = connector.resolve(
            DatasetSpec(query=f"SELECT {q('id')}, {q('amount')} FROM {qualified}")
        )
        assert [c.type.logical for c in connector.get_schema(query).columns] == [
            logical["id"],
            LogicalType.DECIMAL,
        ]
        short = config_name.split(".")[-1]
        assert short in connector.list_datasets(short[:12] + "*")
        sql = f"SELECT {q('id')} AS k FROM {qualified}"
        batches = list(connector.iter_batches(sql, 1000))
        assert [b.num_rows for b in batches] == [1000, 1000, 500]
        assert batches[0].schema.names == ["k"]
        empty = list(connector.iter_batches(f"{sql} WHERE 1 = 0"))
        assert sum(b.num_rows for b in empty) == 0  # like PostgreSQL: no batches at all
        with pytest.raises(ConnectorError, match="Query failed"):
            connector.query("SELECT no_such_column FROM no_such_table")
        if backend.name != "mssql":  # SQL Server has no read-only sessions (documented)
            with pytest.raises(ConnectorError):
                connector.query(f"DELETE FROM {qualified}")
    finally:
        connector.close()
        backend.drop_table(qualified)


_SLOW = {
    "mysql": "SELECT COUNT(*) FROM information_schema.COLUMNS a, information_schema.COLUMNS b, "
    "information_schema.COLUMNS c",
    "mssql": "WAITFOR DELAY '00:00:05'; SELECT 1 AS ok",
    "oracle": "SELECT COUNT(*) FROM all_objects a, all_objects b, all_objects c",
}


def test_query_timeout(seeded: tuple[Backend, dict[str, str]]) -> None:
    backend, _ = seeded
    config = DatabaseConnection.model_validate(
        {"type": backend.name, "url": server_url(backend.name), "query_timeout_s": 1}
    )
    connector = connector_class(backend.name)("timeout", config, Path())
    try:
        started = time.perf_counter()
        with pytest.raises(ConnectorError):
            connector.query(_SLOW[backend.name])
        assert time.perf_counter() - started < 30
    finally:
        connector.close()


def test_as_of_time_travel(seeded: tuple[Backend, dict[str, str]]) -> None:
    backend, _ = seeded
    if backend.name == "mysql":
        pytest.skip("MySQL has no time travel (resolve raises; see the unit tests)")
    d = backend.dialect
    name = f"dvf_tt_{secrets.token_hex(3)}"
    if backend.name == "mssql":
        qualified, config_name = f"dbo.{d.quote(name)}", f"dbo.{name}"
        backend.execute(
            f"CREATE TABLE {qualified} (id INT PRIMARY KEY, v NVARCHAR(10), "
            "vf DATETIME2 GENERATED ALWAYS AS ROW START HIDDEN NOT NULL, "
            "vt DATETIME2 GENERATED ALWAYS AS ROW END HIDDEN NOT NULL, "
            "PERIOD FOR SYSTEM_TIME (vf, vt)) "
            f"WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbo.{name}_history))"
        )
    else:
        qualified, config_name = d.quote(name.upper()), name.upper()
        backend.execute(f"CREATE TABLE {qualified} (id NUMBER PRIMARY KEY, v VARCHAR2(10))")
    try:
        backend.execute(f"INSERT INTO {qualified} (id, v) VALUES (1, 'before')")
        # Oracle maps timestamps to SCNs every few seconds and rejects instants close to the
        # table's creation (ORA-01466), so keep clear of both.
        time.sleep(8)
        instant = datetime.now(UTC).isoformat()
        time.sleep(5)
        backend.execute(f"UPDATE {qualified} SET v = 'after' WHERE id = 1")
        connector = open_connector(backend.name, server_url(backend.name), read_only=True)
        try:
            past = connector.resolve(DatasetSpec(table=config_name, as_of=instant))
            now = connector.resolve(DatasetSpec(table=config_name))
            assert connector.query(f"SELECT v FROM {past.sql}").column("v").to_pylist() == [
                "before"
            ]
            assert connector.query(f"SELECT v FROM {now.sql}").column("v").to_pylist() == ["after"]
        finally:
            connector.close()
    finally:
        if backend.name == "mssql":
            backend.execute(f"ALTER TABLE {qualified} SET (SYSTEM_VERSIONING = OFF)")
            backend.execute(f"DROP TABLE IF EXISTS dbo.{d.quote(name + '_history')}")
        backend.drop_table(qualified)
