"""Generic SQLAlchemy connector: any database with a SQLAlchemy dialect.

The dvf SQL dialect (which canonical SQL and checks are generated in) comes from the SQLAlchemy
dialect name of the URL (``postgresql+psycopg://`` is ``postgres``), or from ``dialect:`` on the
connection. Each new session is prepared like dvf's native connector for that dialect (UTC,
read-only where the database supports it; SQLite gets dvf's SQL functions, and a read-only SQLite
file is opened in read-only mode). Queries go to the driver unchanged (no bind parameter parsing)
and stream with server-side cursors where the driver supports them (``stream_results``); rows are
converted to Arrow in batches. Table schemas and primary keys come from SQLAlchemy's inspector.

Verified against SQLite and PostgreSQL (canonical parity and seeded defects) and against MySQL and
Oracle (seeded defects); other databases work when dvf has their dialect, but are verified only
through their native connectors. Install SQLAlchemy and the database's driver
(``pip install 'dvf-framework[sqlalchemy]'`` plus, for example, ``psycopg``).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pyarrow as pa
from pydantic import Field

from dvf.config.connections import SqlAlchemyConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import batches_from_rows, rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import (
    CollationInfo,
    Connector,
    ObjectInventory,
    PhysicalDesign,
    Relation,
)

log = logging.getLogger(__name__)

_PG_OIDS = {
    16: "boolean",
    17: "bytea",
    20: "bigint",
    21: "smallint",
    23: "integer",
    25: "text",
    700: "real",
    701: "double precision",
    1042: "char",
    1043: "varchar",
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1184: "timestamptz",
    1700: "numeric",
    2950: "uuid",
}
"""PostgreSQL type OIDs (psycopg2 reports only these in result descriptions)."""


class SqlAlchemyConfig(SqlAlchemyConnection):
    """Any database with a SQLAlchemy dialect.

    ``options`` are passed to ``sqlalchemy.create_engine`` (for example ``connect_args``).
    """

    dialect: str | None = None
    """dvf SQL dialect (``postgres``, ``sqlite``, ``mysql``, ``mssql``, ``oracle``, ``duckdb``,
    ...); detected from the URL's SQLAlchemy dialect when omitted."""
    schema_: str | None = Field(default=None, alias="schema")
    """Schema of unqualified table names in catalog lookups (default: the session's)."""


def _import_sqlalchemy() -> Any:
    try:
        import sqlalchemy
    except ImportError as exc:
        raise DependencyError("sqlalchemy", "sqlalchemy", "The SQLAlchemy connector") from exc
    return sqlalchemy


def backend_name(url: str) -> str:
    """The SQLAlchemy dialect name of a URL (the scheme before ``+driver``)."""
    scheme = url.split("://", 1)[0] if "://" in url else url.split(":", 1)[0]
    return scheme.split("+", 1)[0].strip().lower()


class SqlAlchemyConnector(Connector):
    """Any database reachable through a SQLAlchemy URL."""

    Config = SqlAlchemyConfig

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        from dvf.packs.catalog.generic import dialect_for_sqlalchemy

        cfg = self._config
        backend = backend_name(cfg.url.get_secret_value())
        setattr(self, "dialect", dialect_for_sqlalchemy(backend, cfg.dialect, name))  # noqa: B010
        self._engine: Any = None
        self._connection: Any = None
        self._inspector: Any = None
        self._probe = 0
        self._streams = 0

    @property
    def _config(self) -> SqlAlchemyConfig:
        config = self.config
        if not isinstance(config, SqlAlchemyConfig):
            config = SqlAlchemyConfig.model_validate(config.model_dump(by_alias=True))
        return config

    # ------------------------------------------------------------------ lifecycle
    def _url(self, sa: Any) -> Any:
        url = sa.engine.make_url(self._config.url.get_secret_value())
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            path = Path(url.database)
            if not str(url.database).startswith("file:") and not path.is_absolute():
                url = url.set(database=str(self.base_dir / path))
        return url

    def _engine_args(self, sa: Any, url: Any) -> dict[str, Any]:
        kwargs = dict(self._config.options)
        if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
            database = str(url.database)
            if self.config.read_only and not database.startswith("file:"):
                if not Path(database).exists():
                    raise ConnectorError(
                        f"SQLite database '{database}' for connection '{self.name}' does not exist."
                    )
                uri = f"file:{Path(database).as_posix()}?mode=ro"
                kwargs.setdefault(
                    "creator",
                    lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
                )
            else:
                connect_args = dict(kwargs.get("connect_args") or {})
                connect_args.setdefault("check_same_thread", False)
                kwargs["connect_args"] = connect_args
        elif self.dialect == "postgres" and url.get_driver_name() in ("psycopg", "psycopg2"):
            connect_args = dict(kwargs.get("connect_args") or {})
            connect_args.setdefault("connect_timeout", 15)  # as the native connector
            kwargs["connect_args"] = connect_args
        return kwargs

    def _prepare(self, dbapi_connection: Any, _record: Any = None) -> None:
        """Prepare a new DBAPI connection like the native connector for the dialect."""
        from dvf.packs.catalog.generic import prepare_sqlite, session_statements

        if self.dialect == "sqlite":
            prepare_sqlite(dbapi_connection)
            return
        statements = session_statements(
            self.dialect, read_only=self.config.read_only, timeout_s=self.config.query_timeout_s
        )
        if not statements:
            return
        cursor = dbapi_connection.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
        finally:
            cursor.close()
        try:  # session settings made inside a transaction would vanish with its rollback
            dbapi_connection.commit()
        except Exception:
            log.debug("commit after session setup failed", exc_info=True)

    def open(self) -> None:
        if self._connection is not None:
            return
        sa = _import_sqlalchemy()
        try:
            url = self._url(sa)
            engine = sa.create_engine(url, **self._engine_args(sa, url))
            sa.event.listen(engine, "connect", self._prepare)
            connection = engine.connect()
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(
                f"Could not connect through SQLAlchemy for connection '{self.name}': "
                f"{type(exc).__name__}: {exc}",
                hint="Check the URL, the installed driver, and credentials; run "
                "'dvf connections test'.",
            ) from None
        self._engine, self._connection = engine, connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
                self._inspector = None
                if self._engine is not None:
                    self._engine.dispose()
                    self._engine = None

    @property
    def connection(self) -> Any:
        """The SQLAlchemy ``Connection``."""
        self.open()
        return self._connection

    @property
    def dbapi_connection(self) -> Any:
        return self.connection.connection.dbapi_connection

    @property
    def inspector(self) -> Any:
        if self._inspector is None:
            self._inspector = _import_sqlalchemy().inspect(self.connection)
        return self._inspector

    def server_version(self) -> str | None:
        info = getattr(self.connection.dialect, "server_version_info", None)
        name = self.connection.dialect.name
        return f"{name} {'.'.join(str(p) for p in info)}" if info else name

    # ------------------------------------------------------------------ queries
    def _run(self, sql: str, *, stream: bool = False) -> Any:
        options: dict[str, Any] = {"no_parameters": True}
        if stream:
            options["stream_results"] = True
        try:
            return self.connection.execution_options(**options).exec_driver_sql(sql)
        except Exception as exc:
            raise ConnectorError(
                f"Query failed on connection '{self.name}': {type(exc).__name__}: {exc}"
            ) from exc

    def _finish(self) -> None:
        """End the implicit transaction (no snapshot or locks are held between queries)."""
        if self._streams == 0 and self._connection is not None:
            try:
                self._connection.rollback()
            except Exception:
                log.debug("rollback on connection '%s' failed", self.name, exc_info=True)

    def query(self, sql: str) -> pa.Table:
        try:
            result = self._run(sql)
            try:
                names = list(result.keys()) if result.returns_rows else []
                rows = [tuple(r) for r in result.fetchall()] if result.returns_rows else []
            finally:
                result.close()
        finally:
            self._finish()
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        # Server-side cursors (stream_results) need a transaction; it ends when the stream does.
        try:
            result = self._run(sql, stream=True)
        except Exception:
            self._finish()
            raise
        self._streams += 1
        try:
            names = list(result.keys()) if result.returns_rows else []
            empty = True
            while True:
                rows = result.fetchmany(batch_size)
                if not rows:
                    break
                empty = False
                yield from batches_from_rows(names, [tuple(r) for r in rows], batch_size)
            if empty:
                yield from rows_to_table(names, []).to_batches()
        finally:
            result.close()
            self._streams -= 1
            self._finish()

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.as_of is not None:
            raise ConnectorError(
                f"Connection '{self.name}' (sqlalchemy) does not support 'as_of'; write the "
                "time-travel clause in a 'query'."
            )
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is a database; use 'table' or 'query', not 'path'."
            )
        table: str | None = None
        if spec.table is not None:
            base, table, kind, label = spec.table, spec.table, DatasetKind.TABLE, spec.table
        else:
            assert spec.query is not None
            base = self.sql_dialect.subquery(spec.query, "dvf_query")
            kind, label = DatasetKind.QUERY, "query"
        ref = DatasetRef(
            connection=self.name,
            kind=kind,
            name=label,
            query=spec.query if kind is DatasetKind.QUERY else None,
        )
        sql = base
        if spec.filter:
            sql = self.sql_dialect.subquery(
                f"SELECT * FROM {base} WHERE {spec.filter}", "dvf_filtered"
            )
        return Relation(ref, sql, base, table, spec.filter, self.name)

    def _split(self, table: str) -> tuple[str | None, str]:
        from dvf.packs.catalog.introspect import split_table

        name = split_table(table, self.dialect)
        return name.schema or self._config.schema_, name.name

    def _exists_impl(self, relation: Relation) -> bool:
        if relation.table is not None:
            schema, name = self._split(relation.table)
            try:
                if self.inspector.has_table(name, schema=schema):
                    return True
            except Exception:
                log.debug("has_table failed for %s", relation.table, exc_info=True)
        return Connector.exists(self, relation)

    def _get_schema_impl(self, relation: Relation) -> DatasetSchema:
        if self.dialect == "sqlite":
            return self._sqlite_schema(relation)
        if relation.table is not None:
            schema, name = self._split(relation.table)
            try:
                reflected = self.inspector.get_columns(name, schema=schema)
            except Exception as exc:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}': {exc}"
                ) from exc
            dialect = self.connection.dialect
            columns: list[ColumnSchema] = []
            for i, col in enumerate(reflected):
                try:
                    native = str(col["type"].compile(dialect=dialect))
                except Exception:
                    native = str(col["type"])
                default = col.get("default")
                columns.append(
                    ColumnSchema(
                        name=str(col["name"]),
                        native_type=native,
                        type=parse_native_type(native, self.dialect),
                        nullable=bool(col.get("nullable", True)),
                        default=None if default is None else str(default),
                        ordinal=i + 1,
                        comment=col.get("comment"),
                    )
                )
            return DatasetSchema(
                ref=relation.ref,
                columns=tuple(columns),
                primary_key=self._get_primary_key_impl(relation),
            )
        return DatasetSchema(ref=relation.ref, columns=self._query_columns(relation))

    def _query_columns(self, relation: Relation) -> tuple[ColumnSchema, ...]:
        from dvf.packs.catalog.generic import describe_columns

        result = self._run(self.sql_dialect.limit(f"SELECT * FROM {relation.base_sql}", 100))
        try:
            description = list(result.cursor.description or ())
            rows = [tuple(r) for r in result.fetchall()]
        finally:
            result.close()
        return describe_columns(description, rows, self.dialect, self._native_namer())

    def _native_namer(self) -> Any:
        driver = str(getattr(self.connection.dialect, "driver", "") or "")
        if self.dialect in ("postgres", "redshift"):

            def postgres(entry: Any) -> str | None:
                display = getattr(entry, "type_display", None)
                if display:
                    return str(display)
                code = entry[1]
                return _PG_OIDS.get(code) if isinstance(code, int) else None

            return postgres
        if self.dialect == "mysql" and driver in ("pymysql", "mysqldb"):
            from dvf.connectors.mysql import native_type_from_description as mysql_native

            return lambda entry: mysql_native(entry) if isinstance(entry[1], int) else None
        if self.dialect == "oracle":
            from dvf.connectors.oracle import native_type_from_description as oracle_native

            return lambda entry: None if isinstance(entry[1], type) else oracle_native(entry)
        return None

    def _sqlite_schema(self, relation: Relation) -> DatasetSchema:
        """SQLite reports declared types through PRAGMA table_info, as the native connector."""
        con = self.dbapi_connection
        lite = self.sql_dialect
        if relation.table is not None:
            schema, table = self._split(relation.table)
            prefix = f"{lite.quote(schema)}." if schema else ""
            info = con.execute(f"PRAGMA {prefix}table_info({lite.literal(table)})").fetchall()
            if not info:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'."
                )
            nullable_known = True
        else:
            self._probe += 1
            view = f"dvf_schema_probe_{self._probe}"
            con.execute(f"CREATE TEMP VIEW {view} AS SELECT * FROM {relation.base_sql}")
            try:
                info = con.execute(f"PRAGMA temp.table_info({lite.literal(view)})").fetchall()
            finally:
                con.execute(f"DROP VIEW temp.{view}")
            nullable_known = False
        columns = tuple(
            ColumnSchema(
                name=row[1],
                native_type=row[2] or "",
                type=parse_native_type(row[2] or "", "sqlite"),
                nullable=(not row[3]) if nullable_known else None,
                default=None if row[4] is None else str(row[4]),
                ordinal=int(row[0]) + 1,
            )
            for row in info
        )
        pk = tuple(row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5]))
        return DatasetSchema(
            ref=relation.ref, columns=columns, primary_key=pk if nullable_known else ()
        )

    def _get_primary_key_impl(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        if self.dialect == "sqlite":
            return self._sqlite_schema(relation).primary_key
        schema, name = self._split(relation.table)
        try:
            constraint = self.inspector.get_pk_constraint(name, schema=schema) or {}
        except Exception:
            log.debug("primary key of %s could not be read", relation.table, exc_info=True)
            return ()
        return tuple(str(c) for c in constraint.get("constrained_columns") or ())

    def _list_datasets_impl(self, pattern: str) -> list[str]:
        schema = self._config.schema_
        if "." in pattern:
            schema = pattern.rsplit(".", 1)[0]
        names = [*self.inspector.get_table_names(schema=schema)]
        names += self.inspector.get_view_names(schema=schema)
        found = []
        for table in names:
            qualified = f"{schema}.{table}" if schema and "." in pattern else table
            if fnmatchcase(qualified.lower(), pattern.lower()):
                found.append(qualified)
        return sorted(set(found))

    # Catalog lookups through the inspector run in an implicit transaction, ended afterwards.
    def exists(self, relation: Relation) -> bool:
        try:
            return self._exists_impl(relation)
        finally:
            self._finish()

    def get_schema(self, relation: Relation) -> DatasetSchema:
        try:
            return self._get_schema_impl(relation)
        finally:
            self._finish()

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        try:
            return self._get_primary_key_impl(relation)
        finally:
            self._finish()

    def list_datasets(self, pattern: str) -> list[str]:
        try:
            return self._list_datasets_impl(pattern)
        finally:
            self._finish()

    # ------------------------------------------------------------------ catalog metadata
    def get_physical_design(self, relation: Relation) -> PhysicalDesign | None:
        from dvf.packs.catalog.introspect import physical_design

        return physical_design(self, relation)

    def get_collations(self, relation: Relation) -> CollationInfo | None:
        from dvf.packs.catalog.introspect import collations

        return collations(self, relation)

    def get_object_inventory(self, relation: Relation) -> ObjectInventory | None:
        from dvf.packs.catalog.introspect import object_inventory

        return object_inventory(self, relation)


__all__ = ["SqlAlchemyConfig", "SqlAlchemyConnector", "backend_name"]
