"""Live MySQL, SQL Server, and Oracle backends for the parity and seeded-defect tests.

Each backend reads its URL from an environment variable and is skipped when it is unset:

* ``DVF_TEST_MYSQL_URL``  ``mysql://root:<password>@localhost:3306/dvf``
* ``DVF_TEST_MSSQL_URL``  ``mssql://sa:<password>@127.0.0.1:1433/master?TrustServerCertificate=yes``
* ``DVF_TEST_ORACLE_URL`` ``oracle://<user>:<password>@localhost:1521/FREEPDB1``

The backends open dvf's own connectors (so session setup is exercised) with ``read_only`` off,
because the tests create scratch tables. All data is synthetic.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from dvf.config.connections import DatabaseConnection
from dvf.normalize.canonical import Kind
from dvf.plugins.base import Connector
from dvf.sql.dialects import Dialect, get_dialect

ENV_VARS = {
    "mysql": "DVF_TEST_MYSQL_URL",
    "mssql": "DVF_TEST_MSSQL_URL",
    "oracle": "DVF_TEST_ORACLE_URL",
}
SERVERS = tuple(ENV_VARS)


def server_url(name: str) -> str:
    url = os.environ.get(ENV_VARS[name])
    if not url:
        pytest.skip(f"{ENV_VARS[name]} is not set")
    return url


def connector_class(name: str) -> type[Connector]:
    if name == "mysql":
        from dvf.connectors.mysql import MySQLConnector

        return MySQLConnector
    if name == "mssql":
        from dvf.connectors.mssql import MSSQLConnector

        return MSSQLConnector
    from dvf.connectors.oracle import OracleConnector

    return OracleConnector


def open_connector(name: str, url: str, *, read_only: bool = False) -> Connector:
    config = DatabaseConnection.model_validate({"type": name, "url": url, "read_only": read_only})
    connector = connector_class(name)(f"test_{name}", config, Path())
    connector.open()
    return connector


@dataclass(frozen=True)
class ColumnKind:
    """How values of one canonical kind are stored on a backend."""

    sql_type: str
    placeholder: str = ""
    """Insert expression around the bind placeholder (``{}``), for example a conversion."""
    convert: Callable[[Any], Any] = lambda v: v


@dataclass
class Backend:
    """A live database used by the tests."""

    name: str
    connector: Connector
    kinds: dict[str, ColumnKind]
    tables: dict[str, str] = field(default_factory=dict)
    _maps: dict[str, list[Any]] = field(default_factory=dict)

    @property
    def dialect(self) -> Dialect:
        return get_dialect(self.name)

    @property
    def raw(self) -> Any:
        return self.connector.connection  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ SQL helpers
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        cur = self.raw.cursor()
        try:
            cur.execute(sql, params) if params is not None else cur.execute(sql)
        finally:
            cur.close()
        if self.name == "oracle":
            self.raw.commit()

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]], sizes: Any = None) -> None:
        if not rows:
            return
        if self.name == "mssql":
            # mssql-python's executemany infers parameter sizes from the first rows (a leading
            # NULL breaks it); one multi-row VALUES statement binds every value on its own.
            head, values = sql.split(" VALUES ", 1)
            per_row = max(1, 2000 // max(1, len(rows[0])))
            for start in range(0, len(rows), per_row):
                chunk = rows[start : start + per_row]
                statement = f"{head} VALUES " + ", ".join([values] * len(chunk))
                self.execute(statement, [v for row in chunk for v in row])
            return
        cur = self.raw.cursor()
        try:
            if sizes is not None:
                cur.setinputsizes(*sizes)
            cur.executemany(sql, list(rows))
        finally:
            cur.close()
        if self.name == "oracle":
            self.raw.commit()

    def param(self, index: int) -> str:
        return {"mysql": "%s", "mssql": "?", "oracle": f":{index + 1}"}[self.name]

    def drop_table(self, table: str) -> None:
        if self.name == "oracle":
            with contextlib.suppress(Exception):  # it may not exist
                self.execute(f"DROP TABLE {table} PURGE")
        else:
            self.execute(f"DROP TABLE IF EXISTS {table}")

    # ------------------------------------------------------------------ scratch tables
    def table_for(self, kind_key: str) -> str:
        """A scratch table ``(i, v)`` whose ``v`` stores ``kind_key`` values."""
        found = self.tables.get(kind_key)
        if found is not None:
            return found
        spec = self.kinds[kind_key]
        if self.name == "mysql":
            # Not TEMPORARY: MySQL cannot refer to a temporary table twice in one query.
            table = f"dvf_p_{kind_key}_{secrets.token_hex(3)}"
            self.execute(f"CREATE TABLE {table} (i INT PRIMARY KEY, v {spec.sql_type})")
        elif self.name == "mssql":
            table = f"#dvf_p_{kind_key}"
            self.execute(f"CREATE TABLE {table} (i INT PRIMARY KEY, v {spec.sql_type})")
        else:
            table = f"dvf_p_{kind_key}_{secrets.token_hex(3)}"
            self.execute(f"CREATE TABLE {table} (i NUMBER(9) PRIMARY KEY, v {spec.sql_type})")
        self.tables[kind_key] = table
        return table

    def load(self, kind_key: str, values: Sequence[Any]) -> str:
        table = self.table_for(kind_key)
        spec = self.kinds[kind_key]
        self.execute(f"DELETE FROM {table}")
        value_sql = spec.placeholder.format(self.param(1)) if spec.placeholder else self.param(1)
        sizes = None
        if self.name == "oracle":
            sizes = _oracle_sizes(spec.sql_type)
        self.executemany(
            f"INSERT INTO {table} (i, v) VALUES ({self.param(0)}, {value_sql})",
            [(i, None if v is None else spec.convert(v)) for i, v in enumerate(values)],
            sizes,
        )
        return table

    def evaluate(
        self, kind_key: str, values: Sequence[Any], build: Callable[[str], str]
    ) -> list[Any]:
        """``build('v')`` evaluated for every value, in order."""
        table = self.load(kind_key, values)
        result = self.connector.query(f"SELECT {build('v')} AS c FROM {table} ORDER BY i")
        return [_plain(v) for v in result.column("c").to_pylist()]

    def close(self) -> None:
        if self.name != "mssql":  # SQL Server #temp tables vanish with the session
            for table in self.tables.values():
                self.drop_table(table)
        self.connector.close()

    def load_rows(
        self, key: str, column_types: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> str:
        """A scratch table ``(i, c0, c1, ...)`` holding ``rows``."""
        table = self.tables.get(key)
        if table is None:
            columns = ", ".join(f"c{j} {t}" for j, t in enumerate(column_types))
            int_type = "NUMBER(9)" if self.name == "oracle" else "INT"
            if self.name == "mssql":
                table = f"#dvf_p_{key}"
            else:
                table = f"dvf_p_{key}_{secrets.token_hex(3)}"
            self.execute(f"CREATE TABLE {table} (i {int_type} PRIMARY KEY, {columns})")
            self.tables[key] = table
        self.execute(f"DELETE FROM {table}")
        names = ", ".join(f"c{j}" for j in range(len(column_types)))
        params = ", ".join(self.param(j) for j in range(len(column_types) + 1))
        self.executemany(
            f"INSERT INTO {table} (i, {names}) VALUES ({params})",
            [(i, *row) for i, row in enumerate(rows)],
        )
        return table

    # ------------------------------------------------------------------ character probes
    def map_chars(self, key: str, build: Callable[[str], str], chars: Sequence[str]) -> list[Any]:
        """``build(v)`` for every character (or short string) of ``chars``, cached per key."""
        cached = self._maps.get(key)
        if cached is not None:
            return cached
        out: list[Any] = []
        step = 2000 if self.name == "oracle" else 4000  # Oracle binds at most 32767 bytes
        for start in range(0, len(chars), step):
            out.extend(self._map_chunk(build, list(chars[start : start + step])))
        self._maps[key] = out
        return out

    def _map_chunk(self, build: Callable[[str], str], chunk: list[str]) -> list[Any]:
        payload = json.dumps(chunk)
        if self.name == "mysql":
            sql = (
                f"SELECT {build('v')} FROM JSON_TABLE(%s, '$[*]' COLUMNS (i FOR ORDINALITY, "
                "v VARCHAR(16) CHARACTER SET utf8mb4 PATH '$')) t ORDER BY i"
            )
            params: Any = (payload,)
        elif self.name == "mssql":
            sql = (
                f"SELECT {build('x.v')} FROM OPENJSON(?) j "
                "CROSS APPLY (SELECT CAST(j.[value] AS NVARCHAR(16)) AS v) x "
                "ORDER BY CAST(j.[key] AS INT)"
            )
            params = [payload]
        else:
            sql = (
                f"SELECT {build('v')} FROM JSON_TABLE(:1, '$[*]' COLUMNS (i FOR ORDINALITY, "
                "v VARCHAR2(64 CHAR) PATH '$')) ORDER BY i"
            )
            params = [payload]
        cur = self.raw.cursor()
        try:
            cur.execute(sql, params)
            return [_plain(r[0]) for r in cur.fetchall()]
        finally:
            cur.close()


def _plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _oracle_sizes(sql_type: str) -> list[Any] | None:
    import oracledb

    upper = sql_type.upper()
    if upper.startswith("TIMESTAMP") and "ZONE" not in upper:
        return [None, oracledb.DB_TYPE_TIMESTAMP]
    if upper.startswith("RAW"):
        return [None, oracledb.DB_TYPE_RAW]
    return None


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _iso_offset(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(sep=" ")


KINDS: dict[str, dict[str, ColumnKind]] = {
    "mysql": {
        "text": ColumnKind("LONGTEXT CHARACTER SET utf8mb4"),
        "integer": ColumnKind("BIGINT"),
        "decimal": ColumnKind("DECIMAL(24,6)"),
        "boolean": ColumnKind("BOOLEAN"),
        "date": ColumnKind("DATE"),
        "timestamp": ColumnKind("DATETIME(6)"),
        "timestamp_tz": ColumnKind("TIMESTAMP(6) NULL", convert=_utc_naive),
        "time": ColumnKind("TIME(6)"),
        "binary": ColumnKind("VARBINARY(64)"),
    },
    "mssql": {
        "text": ColumnKind("NVARCHAR(MAX)"),
        "integer": ColumnKind("BIGINT"),
        "decimal": ColumnKind("DECIMAL(24,6)"),
        "boolean": ColumnKind("BIT"),
        "date": ColumnKind("DATE"),
        "timestamp": ColumnKind("DATETIME2(6)"),
        "datetime": ColumnKind("DATETIME"),
        "timestamp_tz": ColumnKind(
            "DATETIMEOFFSET(6)", "CAST({} AS DATETIMEOFFSET(6))", convert=_iso_offset
        ),
        "time": ColumnKind("TIME(6)"),
        # NULL parameters arrive as VARCHAR, which converts to VARBINARY only explicitly.
        "binary": ColumnKind("VARBINARY(64)", "CONVERT(VARBINARY(64), {})"),
    },
    "oracle": {
        "text": ColumnKind("VARCHAR2(4000 CHAR)"),
        "integer": ColumnKind("NUMBER(19)"),
        "decimal": ColumnKind("NUMBER(24,6)"),
        "boolean": ColumnKind("BOOLEAN"),
        "date": ColumnKind("DATE"),
        "timestamp": ColumnKind("TIMESTAMP(6)"),
        "timestamp_tz": ColumnKind(
            "TIMESTAMP(6) WITH TIME ZONE",
            "TO_TIMESTAMP_TZ({}, 'YYYY-MM-DD HH24:MI:SS.FF6TZH:TZM')",
            convert=lambda v: v.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00:00"),
        ),
        "time": ColumnKind("TIMESTAMP(6)", convert=lambda v: datetime.combine(date(1970, 1, 1), v)),
        "binary": ColumnKind("RAW(64)"),
    },
}

KIND_KEYS = {
    Kind.TEXT: "text",
    Kind.INTEGER: "integer",
    Kind.DECIMAL: "decimal",
    Kind.BOOLEAN: "boolean",
    Kind.DATE: "date",
    Kind.TIMESTAMP: "timestamp",
    Kind.TIME: "time",
    Kind.BINARY: "binary",
}


def make_backend(name: str) -> Backend:
    connector = open_connector(name, server_url(name))
    return Backend(name, connector, KINDS[name])


# ---------------------------------------------------------------------- seeded data loaders

_SEEDED_TYPES = {
    "mysql": {
        "int64": "BIGINT",
        "string": "LONGTEXT",
        "date32[day]": "DATE",
        "timestamp[us]": "DATETIME(6)",
        "decimal": "DECIMAL({p},{s})",
    },
    "mssql": {
        "int64": "BIGINT",
        "string": "NVARCHAR(MAX)",
        "date32[day]": "DATE",
        "timestamp[us]": "DATETIME2(6)",
        "decimal": "DECIMAL({p},{s})",
    },
    "oracle": {
        # NUMBER(18) fits in int64 (NUMBER(19) does not), so the schema check sees a widening.
        "int64": "NUMBER(18)",
        "string": "VARCHAR2(4000 CHAR)",
        "date32[day]": "DATE",
        "timestamp[us]": "TIMESTAMP(6)",
        "decimal": "NUMBER({p},{s})",
    },
}


def _seeded_type(name: str, field_type: pa.DataType) -> str:
    types = _SEEDED_TYPES[name]
    if pa.types.is_decimal(field_type):
        return types["decimal"].format(p=field_type.precision, s=field_type.scale)
    return types[str(field_type)]


def load_seeded(
    backend: Backend,
    namespace: str,
    tables: dict[str, pa.Table],
    primary_keys: dict[str, str],
) -> dict[str, str]:
    """Create ``tables`` under ``namespace`` (a MySQL database, SQL Server schema, or a table
    prefix on Oracle) and return the qualified names to use in a configuration."""
    d = backend.dialect
    names: dict[str, str] = {}
    if backend.name == "mysql":
        backend.execute(f"DROP DATABASE IF EXISTS {d.quote(namespace)}")
        backend.execute(f"CREATE DATABASE {d.quote(namespace)} CHARACTER SET utf8mb4")
    elif backend.name == "mssql":
        drop_seeded(backend, namespace, list(tables))
        backend.execute(f"CREATE SCHEMA {d.quote(namespace)}")
    for table_name, table in tables.items():
        if backend.name == "oracle":
            physical = f"{namespace}_{table_name}".upper()
            qualified = d.quote(physical)
            backend.drop_table(qualified)
            names[table_name] = physical
        else:
            qualified = f"{d.quote(namespace)}.{d.quote(table_name)}"
            names[table_name] = f"{namespace}.{table_name}"
        columns = [f"{d.quote(f.name)} {_seeded_type(backend.name, f.type)}" for f in table.schema]
        key = primary_keys.get(table_name)
        if key:
            columns.append(f"PRIMARY KEY ({d.quote(key)})")
        backend.execute(f"CREATE TABLE {qualified} ({', '.join(columns)})")
        column_list = ", ".join(d.quote(f.name) for f in table.schema)
        params = ", ".join(backend.param(i) for i in range(len(table.schema)))
        rows = [
            tuple(_seeded_value(backend.name, v) for v in r.values()) for r in table.to_pylist()
        ]
        sizes = None
        if backend.name == "oracle":
            import oracledb

            sizes = [
                oracledb.DB_TYPE_TIMESTAMP if pa.types.is_timestamp(f.type) else None
                for f in table.schema
            ]
        backend.executemany(
            f"INSERT INTO {qualified} ({column_list}) VALUES ({params})", rows, sizes
        )
    return names


def _seeded_value(name: str, value: Any) -> Any:
    if name == "oracle" and isinstance(value, str) and value == "":
        return None
    return value


def drop_seeded(backend: Backend, namespace: str, tables: list[str]) -> None:
    d = backend.dialect
    if backend.name == "mysql":
        backend.execute(f"DROP DATABASE IF EXISTS {d.quote(namespace)}")
    elif backend.name == "mssql":
        for table_name in tables:
            backend.execute(f"DROP TABLE IF EXISTS {d.quote(namespace)}.{d.quote(table_name)}")
        backend.execute(
            f"IF SCHEMA_ID({d.literal(namespace)}) IS NOT NULL "
            f"EXEC('DROP SCHEMA {d.quote(namespace)}')"
        )
    else:
        for table_name in tables:
            backend.drop_table(d.quote(f"{namespace}_{table_name}".upper()))


__all__ = [
    "ENV_VARS",
    "KINDS",
    "KIND_KEYS",
    "SERVERS",
    "Backend",
    "ColumnKind",
    "drop_seeded",
    "load_seeded",
    "make_backend",
    "open_connector",
    "server_url",
]
