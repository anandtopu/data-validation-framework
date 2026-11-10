"""The generic ``sqlalchemy`` and ``odbc`` connectors: dialect detection, sessions, schemas, and
canonical parity through the connectors (SQLite and PostgreSQL)."""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dvf.config.models import DatasetSpec
from dvf.connectors.odbc import OdbcConfig, OdbcConnector
from dvf.connectors.sqlalchemy import SqlAlchemyConfig, SqlAlchemyConnector, backend_name
from dvf.core.errors import ConnectorError
from dvf.core.types import LogicalType
from dvf.normalize.canonical import (
    CanonSpec,
    Kind,
    StringOptions,
    canonical_sql,
    canonical_text,
    row_hash,
    row_hash_sql,
)
from dvf.packs.catalog.generic import (
    describe_columns,
    dialect_for_dbms,
    dialect_for_sqlalchemy,
    odbc_native_type,
    session_statements,
)
from dvf.sql.dialects import get_dialect
from tests.parity.test_canonical_parity import (
    _PG_TYPES,
    DST_EDGES,
    EDGE_TEXT,
    PARITY,
    _spec,
    _sqlite_value,
)

ODBC_DRIVERS = ("PostgreSQL ODBC Driver(UNICODE)", "PostgreSQL Unicode", "PostgreSQL Unicode(x64)")


def sqlalchemy_postgres_url(postgres_url: str) -> str:
    """The test PostgreSQL URL with SQLAlchemy's psycopg 3 driver."""
    scheme, rest = postgres_url.split("://", 1)
    return f"postgresql+psycopg://{rest}" if scheme.startswith("postgres") else postgres_url


def odbc_connection_string(postgres_url: str) -> str:
    """An ODBC connection string for the test PostgreSQL (skips without a psqlODBC driver)."""
    import pyodbc

    driver = next((d for d in ODBC_DRIVERS if d in pyodbc.drivers()), None)
    if driver is None:
        pytest.skip("no PostgreSQL ODBC driver is installed")
    parts = urlsplit(postgres_url)
    return (
        f"Driver={{{driver}}};Server={parts.hostname};Port={parts.port or 5432};"
        f"Database={parts.path.lstrip('/')};Uid={parts.username};Pwd={parts.password};"
    )


def _sa(url: str, base: Path, **extra: Any) -> SqlAlchemyConnector:
    config = SqlAlchemyConfig.model_validate({"type": "sqlalchemy", "url": url, **extra})
    return SqlAlchemyConnector("generic", config, base)


def _odbc(connection_string: str, **extra: Any) -> OdbcConnector:
    config = OdbcConfig.model_validate(
        {"type": "odbc", "connection_string": connection_string, **extra}
    )
    return OdbcConnector("generic", config, Path())


# ---------------------------------------------------------------------- dialects and sessions


def test_sqlalchemy_dialect_detection() -> None:
    assert backend_name("postgresql+psycopg://u:p@h/db") == "postgresql"
    assert backend_name("sqlite:///file.db") == "sqlite"
    assert backend_name("mssql+pyodbc://dsn") == "mssql"
    assert dialect_for_sqlalchemy("postgresql", None, "c") == "postgres"
    assert dialect_for_sqlalchemy("oracle", None, "c") == "oracle"
    assert dialect_for_sqlalchemy("trino", "postgres", "c") == "postgres"
    with pytest.raises(ConnectorError, match="no SQL dialect"):
        dialect_for_sqlalchemy("trino", None, "c")
    with pytest.raises(ConnectorError, match="MariaDB"):
        dialect_for_sqlalchemy("mariadb", None, "c")
    with pytest.raises(ConnectorError, match="not available"):
        dialect_for_sqlalchemy("postgresql", "nonsense", "c")


def test_odbc_dialect_detection() -> None:
    assert dialect_for_dbms("PostgreSQL", None, "c") == "postgres"
    assert dialect_for_dbms("Microsoft SQL Server", None, "c") == "mssql"
    assert dialect_for_dbms("Oracle", None, "c") == "oracle"
    assert dialect_for_dbms("MySQL", None, "c") == "mysql"
    assert dialect_for_dbms("Snowflake", None, "c") == "snowflake"
    assert dialect_for_dbms("Spark SQL", None, "c") == "databricks"
    assert dialect_for_dbms("Anything", "duckdb", "c") == "duckdb"
    with pytest.raises(ConnectorError, match="MariaDB"):
        dialect_for_dbms("MariaDB", None, "c")
    with pytest.raises(ConnectorError, match="no SQL dialect"):
        dialect_for_dbms("Teradata", None, "c")
    connector = _odbc("Driver=x", dialect="mssql")
    assert connector.dialect == "mssql"
    assert connector.sql_dialect.name == "mssql"


def test_session_statements() -> None:
    pg = session_statements("postgres", read_only=True, timeout_s=10)
    assert pg == [
        "SET TIME ZONE 'UTC'",
        "SET default_transaction_read_only = on",
        "SET statement_timeout = 10000",
    ]
    assert session_statements("mysql", read_only=False, timeout_s=None) == [
        "SET time_zone = '+00:00'"
    ]
    assert "ALTER SESSION SET TIME_ZONE = '+00:00'" in session_statements(
        "oracle", read_only=True, timeout_s=None
    )
    assert session_statements("bigquery", read_only=True, timeout_s=None) == []


def test_describe_columns_from_odbc_style_description() -> None:
    description = [
        ("id", int, None, 10, 10, 0, False),
        ("amount", Decimal, None, 12, 12, 2, True),
        ("name", str, None, 20, 20, 0, True),
        ("at", dt.datetime, None, 26, 26, 6, True),
        ("d", dt.date, None, 10, 10, 0, True),
        ("flag", bool, None, 1, 1, 0, True),
        ("raw", bytes, None, 8, 8, 0, True),
        ("u", uuid.UUID, None, 36, 36, 0, True),
        ("x", None, None, None, None, None, True),
    ]
    columns = describe_columns(
        description, [(1, Decimal("1.50"), "a", None, None, True, b"", None, 2.5)], "postgres"
    )
    logical = [c.type.logical for c in columns]
    assert logical == [
        LogicalType.INTEGER,
        LogicalType.DECIMAL,
        LogicalType.STRING,
        LogicalType.TIMESTAMP,
        LogicalType.DATE,
        LogicalType.BOOLEAN,
        LogicalType.BINARY,
        LogicalType.STRING,
        LogicalType.FLOAT,  # inferred from the sample value
    ]
    assert (columns[1].type.precision, columns[1].type.scale) == (12, 2)
    assert columns[2].type.length == 20
    native = describe_columns(
        [("v", 1700, None, None, None, None, None)], [], "postgres", lambda e: "numeric(9,3)"
    )
    assert native[0].type.scale == 3


def test_odbc_native_type() -> None:
    assert odbc_native_type("varchar", 20, None) == "varchar(20)"
    assert odbc_native_type("numeric", 12, 3) == "numeric(12,3)"
    assert odbc_native_type("int4", 10, 0) == "int4"
    assert odbc_native_type("text", 8190, None) == "text"
    assert odbc_native_type("bpchar", 3, None) == "bpchar(3)"


# ---------------------------------------------------------------------- SQLAlchemy and SQLite


@pytest.fixture
def lite_db(tmp_path: Path) -> Path:
    path = tmp_path / "data.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, code VARCHAR(10), amount DECIMAL(12, 2))"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "a", "1.50"), (2, "b", "2.25")])
    con.execute("CREATE VIEW v_orders AS SELECT * FROM orders")
    con.commit()
    con.close()
    return path


def test_sqlalchemy_sqlite_datasets(lite_db: Path) -> None:
    connector = _sa("sqlite:///data.sqlite", lite_db.parent)  # relative to the configuration
    with connector:
        assert connector.dialect == "sqlite"
        assert connector.server_version()
        table = connector.resolve(DatasetSpec(table="orders"))
        assert connector.exists(table)
        assert not connector.exists(connector.resolve(DatasetSpec(table="nope")))
        schema = connector.get_schema(table)
        assert [c.name for c in schema.columns] == ["id", "code", "amount"]
        assert schema.columns[2].type.scale == 2
        assert schema.primary_key == ("id",)
        assert connector.get_primary_key(table) == ("id",)
        query = connector.resolve(
            DatasetSpec(query="SELECT id, amount * 2 AS doubled FROM orders", filter="id > 1")
        )
        assert [c.name for c in connector.get_schema(query).columns] == ["id", "doubled"]
        assert connector.query(f"SELECT COUNT(*) AS n FROM {query.sql}").to_pylist() == [{"n": 1}]
        batches = list(connector.iter_batches("SELECT id FROM orders ORDER BY id", batch_size=1))
        assert sum(b.num_rows for b in batches) == 2
        empty = list(connector.iter_batches("SELECT id FROM orders WHERE 1 = 0"))
        assert sum(b.num_rows for b in empty) == 0
        assert connector.list_datasets("*orders") == ["orders", "v_orders"]
        design = connector.get_physical_design(table)
        assert design is not None and design.indexes[0].primary
        inventory = connector.get_object_inventory(table)
        assert inventory is not None and inventory.objects["view"] == ("v_orders",)


def test_sqlalchemy_sqlite_is_read_only(lite_db: Path) -> None:
    connector = _sa(f"sqlite:///{lite_db.as_posix()}", lite_db.parent)
    with connector, pytest.raises(ConnectorError, match="readonly"):
        connector.query("INSERT INTO orders VALUES (3, 'c', 1)")
    writable = _sa(f"sqlite:///{lite_db.as_posix()}", lite_db.parent, read_only=False)
    with writable:
        writable.query("CREATE TABLE scratch (x INTEGER)")
    missing = _sa("sqlite:///missing.sqlite", lite_db.parent)
    with pytest.raises(ConnectorError, match="does not exist"):
        missing.open()


def test_sqlalchemy_query_errors_are_contained(lite_db: Path) -> None:
    with _sa(f"sqlite:///{lite_db.as_posix()}", lite_db.parent) as connector:
        with pytest.raises(ConnectorError, match="Query failed"):
            connector.query("SELECT * FROM nope")
        assert connector.query("SELECT 1 AS ok").to_pylist() == [{"ok": 1}]
    bad = _sa(
        "postgresql+psycopg://u:secret@127.0.0.1:1/db",
        Path(),
        options={"connect_args": {"connect_timeout": 1}},
    )
    with pytest.raises(ConnectorError, match="Could not connect") as info:
        bad.open()
    assert "secret" not in str(info.value)


def _canonical_cases() -> list[tuple[CanonSpec, list[Any]]]:
    return [
        (
            _spec(
                Kind.TEXT,
                strings=StringOptions(trim="both", case_sensitive=False, collapse_whitespace=True),
            ),
            [None, *EDGE_TEXT, "Ünïcödé  X"],
        ),
        (_spec(Kind.TEXT, strings=StringOptions(null_equals_empty=True)), ["", " ", "a", None]),
        (_spec(Kind.INTEGER), [None, 0, -1, 2**63 - 1, -(2**63)]),
        (
            _spec(Kind.DECIMAL, scale=2),
            [
                None,
                Decimal("-1.005"),
                Decimal("1.005"),
                Decimal("-0.001"),
                Decimal("123456789012.345678"),
            ],
        ),
        (_spec(Kind.BOOLEAN), [None, True, False]),
        (_spec(Kind.DATE), [None, dt.date(1900, 1, 1), dt.date(2024, 2, 29)]),
        (_spec(Kind.TIMESTAMP, precision="ms"), [None, *DST_EDGES]),
        (
            _spec(Kind.TIMESTAMP, precision="us", timezone="Europe/Berlin"),
            [dt.datetime(2024, 7, 1, 10), dt.datetime(2024, 1, 1, 10)],
        ),
        (_spec(Kind.TIME, precision="ms"), [None, dt.time(10, 11, 12, 345678), dt.time(0, 0)]),
        (_spec(Kind.BINARY), [None, b"", b"\xab\x00"]),
    ]


def _sqlite_parity(
    connector_factory: Any, base: Path, cases: list[tuple[CanonSpec, list[Any]]]
) -> None:
    dialect = get_dialect("sqlite")
    for n, (spec, values) in enumerate(cases):
        path = base / f"parity_{n}.sqlite"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE t (i INTEGER, v)")
        con.executemany(
            "INSERT INTO t VALUES (?, ?)", [(i, _sqlite_value(v)) for i, v in enumerate(values)]
        )
        con.commit()
        con.close()
        with connector_factory(path) as connector:
            rows = connector.query(
                f"SELECT {canonical_sql('v', spec, dialect)} AS c FROM t ORDER BY i"
            )
        assert rows.column("c").to_pylist() == [canonical_text(v, spec) for v in values], spec


def test_sqlalchemy_sqlite_canonical_parity(tmp_path: Path) -> None:
    _sqlite_parity(
        lambda path: _sa(f"sqlite:///{path.as_posix()}", tmp_path), tmp_path, _canonical_cases()
    )


_HASH_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Co", "Cn"), blacklist_characters="\x00"),
    max_size=8,
)


@PARITY
@given(
    rows=st.lists(
        st.lists(st.one_of(st.none(), _HASH_TEXT), min_size=3, max_size=3), min_size=1, max_size=10
    )
)
def test_sqlalchemy_sqlite_row_hash_parity(rows: list[list[str | None]]) -> None:
    # Rows are inserted through the connector's own (writable, in-memory) session.
    connector = _sa("sqlite://", Path(), read_only=False)
    with connector:
        connector.query("CREATE TABLE h (i INTEGER, c0 TEXT, c1 TEXT, c2 TEXT)")
        raw = connector.dbapi_connection
        raw.executemany("INSERT INTO h VALUES (?, ?, ?, ?)", [(i, *r) for i, r in enumerate(rows)])
        got = connector.query(
            f"SELECT {row_hash_sql(['c0', 'c1', 'c2'], get_dialect('sqlite'))} AS h FROM h ORDER BY i"
        )
    assert got.column("h").to_pylist() == [row_hash(r) for r in rows]


# ---------------------------------------------------------------------- PostgreSQL (live)

_PG_PARITY_SCHEMA = "p4_cat_parity"


@pytest.fixture
def pg_parity_tables(postgres_url: str) -> Iterator[list[tuple[str, CanonSpec, list[Any]]]]:
    import psycopg

    cases = _canonical_cases()
    tables: list[tuple[str, CanonSpec, list[Any]]] = []
    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute(f"DROP SCHEMA IF EXISTS {_PG_PARITY_SCHEMA} CASCADE")
        con.execute(f"CREATE SCHEMA {_PG_PARITY_SCHEMA}")
        for n, (spec, values) in enumerate(cases):
            table = f"{_PG_PARITY_SCHEMA}.t{n}"
            pg_type = _PG_TYPES[spec.kind]
            con.execute(f"CREATE TABLE {table} (i INT, v {pg_type})")
            with con.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} VALUES (%s, %s::{pg_type})", list(enumerate(values))
                )
            tables.append((table, spec, values))
        con.execute(f"CREATE TABLE {_PG_PARITY_SCHEMA}.h (i INT, c0 TEXT, c1 TEXT, c2 TEXT)")
        with con.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {_PG_PARITY_SCHEMA}.h VALUES (%s, %s, %s, %s)",
                [(0, "ab", None, "c"), (1, "", "x", None)],
            )
    try:
        yield tables
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {_PG_PARITY_SCHEMA} CASCADE")


def _pg_parity(connector: Any, tables: list[tuple[str, CanonSpec, list[Any]]]) -> None:
    dialect = get_dialect("postgres")
    with connector:
        assert connector.dialect == "postgres"
        for table, spec, values in tables:
            rows = connector.query(
                f"SELECT {canonical_sql('v', spec, dialect)} AS c FROM {table} ORDER BY i"
            )
            assert rows.column("c").to_pylist() == [canonical_text(v, spec) for v in values], spec
        hashes = connector.query(
            f"SELECT {row_hash_sql(['c0', 'c1', 'c2'], dialect)} AS h FROM {_PG_PARITY_SCHEMA}.h ORDER BY i"
        )
        assert hashes.column("h").to_pylist() == [
            row_hash(["ab", None, "c"]),
            row_hash(["", "x", None]),
        ]
        with pytest.raises(ConnectorError, match="read-only"):
            connector.query(f"CREATE TABLE {_PG_PARITY_SCHEMA}.nope (x INT)")


@pytest.mark.integration
def test_sqlalchemy_postgres_canonical_parity(
    postgres_url: str, pg_parity_tables: list[tuple[str, CanonSpec, list[Any]]]
) -> None:
    _pg_parity(_sa(sqlalchemy_postgres_url(postgres_url), Path()), pg_parity_tables)


@pytest.mark.integration
def test_odbc_postgres_canonical_parity(
    postgres_url: str, pg_parity_tables: list[tuple[str, CanonSpec, list[Any]]]
) -> None:
    _pg_parity(_odbc(odbc_connection_string(postgres_url)), pg_parity_tables)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["sqlalchemy", "odbc"])
def test_generic_postgres_datasets(
    kind: str, postgres_url: str, pg_parity_tables: list[tuple[str, CanonSpec, list[Any]]]
) -> None:
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute(
            f"CREATE TABLE {_PG_PARITY_SCHEMA}.orders_x (id INT, line INT, amount NUMERIC(12, 3), "
            "code VARCHAR(5) NOT NULL, PRIMARY KEY (id, line))"
        )
        con.execute(f"INSERT INTO {_PG_PARITY_SCHEMA}.orders_x VALUES (1, 1, 2.5, 'a')")
    connector: Any = (
        _sa(sqlalchemy_postgres_url(postgres_url), Path())
        if kind == "sqlalchemy"
        else _odbc(odbc_connection_string(postgres_url))
    )
    with connector:
        assert "PostgreSQL" in (connector.server_version() or "") or "postgresql" in (
            connector.server_version() or ""
        )
        relation = connector.resolve(DatasetSpec(table=f"{_PG_PARITY_SCHEMA}.orders_x"))
        assert connector.exists(relation)
        assert not connector.exists(
            connector.resolve(DatasetSpec(table=f"{_PG_PARITY_SCHEMA}.orders_"))
        )
        schema = connector.get_schema(relation)
        assert [c.name for c in schema.columns] == ["id", "line", "amount", "code"]
        assert (schema.columns[2].type.precision, schema.columns[2].type.scale) == (12, 3)
        assert schema.columns[3].type.length == 5 and schema.columns[3].nullable is False
        assert schema.primary_key == ("id", "line")
        query = connector.resolve(
            DatasetSpec(query=f"SELECT id, amount FROM {_PG_PARITY_SCHEMA}.orders_x")
        )
        types = [c.type.logical for c in connector.get_schema(query).columns]
        assert types == [LogicalType.INTEGER, LogicalType.DECIMAL]
        assert f"{_PG_PARITY_SCHEMA}.orders_x" in connector.list_datasets(
            f"{_PG_PARITY_SCHEMA}.orders*"
        )
