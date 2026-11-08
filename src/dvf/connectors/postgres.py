"""PostgreSQL connector (psycopg 3; install with ``pip install 'dvf[postgres]'``)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from dvf.config.connections import DatabaseConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import batches_from_rows, rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import Connector, Relation

if TYPE_CHECKING:
    import psycopg

_DRIVER_SUFFIX = re.compile(r"^(postgres(?:ql)?)\+[a-z0-9_]+://", re.IGNORECASE)

_COLUMNS_SQL = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), a.attnum, col_description(a.attrelid, a.attnum)
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

_PK_SQL = """
SELECT a.attname
FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
WHERE i.indrelid = to_regclass(%s) AND i.indisprimary
ORDER BY array_position(i.indkey::int2[], a.attnum)
"""


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise DependencyError("psycopg", "postgres", "The PostgreSQL connector") from exc
    return psycopg


class PostgresConnector(Connector):
    """PostgreSQL (and compatible systems that speak its protocol)."""

    dialect = "postgres"
    Config = DatabaseConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: psycopg.Connection[Any] | None = None

    def _connect_args(self) -> tuple[str, dict[str, Any]]:
        config = self.config
        assert isinstance(config, DatabaseConnection)
        kwargs: dict[str, Any] = {}
        conninfo = ""
        if config.url is not None:
            conninfo = _DRIVER_SUFFIX.sub(r"\1://", config.url.get_secret_value())
        else:
            kwargs.update(host=config.host, port=config.port or 5432, dbname=config.database)
        if config.user is not None:
            kwargs["user"] = config.user
        if config.password is not None:
            kwargs["password"] = config.password.get_secret_value()
        kwargs.setdefault("connect_timeout", 15)
        kwargs.update(config.options)
        return conninfo, {k: v for k, v in kwargs.items() if v is not None}

    def open(self) -> None:
        if self._con is not None:
            return
        psycopg = _import_psycopg()
        conninfo, kwargs = self._connect_args()
        try:
            con = psycopg.connect(conninfo, autocommit=True, **kwargs)
        except psycopg.Error as exc:
            raise ConnectorError(
                f"Could not connect to PostgreSQL for connection '{self.name}': {exc}",
                hint="Check host, port, database, and credentials; run 'dvf connections test'.",
            ) from exc
        con.execute("SET TIME ZONE 'UTC'")
        if self.config.read_only:
            con.execute("SET default_transaction_read_only = on")
        assert isinstance(self.config, DatabaseConnection)
        if self.config.schema_:
            con.execute(f"SET search_path TO {_quote(self.config.schema_)}, public")
        if self.config.query_timeout_s:
            con.execute(f"SET statement_timeout = {int(self.config.query_timeout_s) * 1000}")
        self._con = con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    @property
    def connection(self) -> psycopg.Connection[Any]:
        self.open()
        assert self._con is not None
        return self._con

    def is_transient(self, exc: BaseException) -> bool:
        psycopg = _import_psycopg()
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        if isinstance(cause, psycopg.errors.QueryCanceled):  # statement_timeout: not transient
            return False
        if isinstance(
            cause,
            psycopg.errors.SerializationFailure
            | psycopg.errors.DeadlockDetected
            | psycopg.OperationalError,
        ):
            return True
        return super().is_transient(exc)

    def server_version(self) -> str | None:
        version = self.connection.info.server_version
        return f"PostgreSQL {version // 10000}.{version % 10000}"

    def _cursor_rows(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        psycopg = _import_psycopg()
        try:
            cursor = self.connection.execute(sql, params)
            names = [d.name for d in cursor.description or ()]
            return names, cursor.fetchall() if cursor.description else []
        except psycopg.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc

    def query(self, sql: str) -> pa.Table:
        names, rows = self._cursor_rows(sql)
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        psycopg = _import_psycopg()
        try:
            with self.connection.cursor() as cursor:
                stream = cursor.stream(sql)
                first = next(stream, None)
                names = [d.name for d in cursor.description or ()]
                if first is None:
                    yield from rows_to_table(names, []).to_batches()
                    return
                yield from batches_from_rows(names, _chain(first, stream), batch_size)
        except psycopg.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc

    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.as_of is not None:
            raise ConnectorError(f"Connection '{self.name}' (postgres) does not support 'as_of'.")
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is a PostgreSQL database; use 'table' or 'query', not 'path'."
            )
        table: str | None = None
        if spec.table is not None:
            base, table, kind, label = spec.table, spec.table, DatasetKind.TABLE, spec.table
        else:
            assert spec.query is not None
            base, kind, label = f"(\n{spec.query}\n) AS dvf_query", DatasetKind.QUERY, "query"
        ref = DatasetRef(
            connection=self.name,
            kind=kind,
            name=label,
            query=spec.query if kind is DatasetKind.QUERY else None,
        )
        sql = (
            base
            if not spec.filter
            else f"(SELECT * FROM {base} WHERE {spec.filter}) AS dvf_filtered"
        )
        return Relation(ref, sql, base, table, spec.filter, self.name)

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        _, rows = self._cursor_rows("SELECT to_regclass(%s) IS NOT NULL", (relation.table,))
        return bool(rows and rows[0][0])

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            _, rows = self._cursor_rows(_COLUMNS_SQL, (relation.table,))
            if not rows:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'."
                )
            columns = tuple(
                ColumnSchema(
                    name=r[0],
                    native_type=r[1],
                    type=parse_native_type(r[1], "postgres"),
                    nullable=bool(r[2]),
                    default=r[3],
                    ordinal=int(r[4]),
                    comment=r[5],
                )
                for r in rows
            )
            return DatasetSchema(
                ref=relation.ref, columns=columns, primary_key=self.get_primary_key(relation)
            )
        psycopg = _import_psycopg()
        try:
            cursor = self.connection.execute(f"SELECT * FROM {relation.base_sql} LIMIT 0")
        except psycopg.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        columns = tuple(
            ColumnSchema(
                name=d.name,
                native_type=d.type_display,
                type=parse_native_type(d.type_display, "postgres"),
                ordinal=i + 1,
            )
            for i, d in enumerate(cursor.description or ())
        )
        return DatasetSchema(ref=relation.ref, columns=columns)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        _, rows = self._cursor_rows(_PK_SQL, (relation.table,))
        return tuple(r[0] for r in rows)

    def list_datasets(self, pattern: str) -> list[str]:
        _, rows = self._cursor_rows(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
        names: list[str] = []
        for schema, table in rows:
            qualified = f"{schema}.{table}"
            target = qualified if "." in pattern else table
            if fnmatchcase(target.lower(), pattern.lower()):
                names.append(target)
        return sorted(names)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _chain(first: Any, rest: Iterator[Any]) -> Iterator[Any]:
    yield first
    yield from rest
