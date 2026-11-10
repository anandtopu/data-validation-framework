"""Generic ODBC connector (pyodbc): any database with an ODBC driver.

Connect with a ``dsn`` or a full ``connection_string`` (treated as a secret). The dvf SQL dialect
comes from the DBMS name the driver reports (``SQL_DBMS_NAME``: ``PostgreSQL`` is ``postgres``),
or from ``dialect:`` on the connection. Each session is prepared like dvf's native connector for
that dialect (UTC, read-only where the database supports it) and asks the driver for a read-only
connection (``SQL_ATTR_ACCESS_MODE``) when the connection is read-only. Table schemas and primary
keys come from the ODBC catalog functions (``SQLColumns``, ``SQLPrimaryKeys``); query schemas from
the result description. Rows are fetched in batches and converted to Arrow.

Verified against PostgreSQL with the psqlODBC driver. Install ``pyodbc`` and the database's
ODBC driver.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pyarrow as pa
from pydantic import Field

from dvf.config.connections import OdbcConnection
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
from dvf.sql.dialects import Dialect, get_dialect

log = logging.getLogger(__name__)

_CURRENT_SCHEMA = {
    "postgres": "SELECT current_schema()",
    "redshift": "SELECT current_schema()",
    "duckdb": "SELECT current_schema()",
    "mssql": "SELECT SCHEMA_NAME()",
    "oracle": "SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL",
    "snowflake": "SELECT CURRENT_SCHEMA()",
    "hana": "SELECT CURRENT_SCHEMA FROM DUMMY",
}
"""How each dialect reports the schema of unqualified names (ODBC catalog lookups need it)."""


class OdbcConfig(OdbcConnection):
    """Any database with an ODBC driver.

    ``options`` are passed to ``pyodbc.connect`` as keyword arguments (for example ``timeout``,
    the login timeout in seconds).
    """

    dialect: str | None = None
    """dvf SQL dialect; detected from the driver's DBMS name when omitted."""
    schema_: str | None = Field(default=None, alias="schema")
    """Schema of unqualified table names in catalog lookups (default: the session's)."""


def _import_pyodbc() -> Any:
    try:
        return importlib.import_module("pyodbc")
    except ImportError as exc:
        raise DependencyError("pyodbc", "odbc", "The ODBC connector") from exc


class OdbcConnector(Connector):
    """Any database reachable through an ODBC driver."""

    Config = OdbcConfig

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        override = self._config.dialect
        if override:
            from dvf.packs.catalog.generic import dialect_for_dbms

            setattr(self, "dialect", dialect_for_dbms("", override, name))  # noqa: B010
        self._con: Any = None
        self._dbms: str | None = None
        self._default_schema: str | None = None

    @property
    def _config(self) -> OdbcConfig:
        config = self.config
        if not isinstance(config, OdbcConfig):
            config = OdbcConfig.model_validate(config.model_dump(by_alias=True))
        return config

    @property
    def sql_dialect(self) -> Dialect:
        if not self.dialect:
            self.open()  # the dialect comes from the driver's DBMS name
        return get_dialect(self.dialect)

    # ------------------------------------------------------------------ lifecycle
    def _connection_string(self) -> str:
        cfg = self._config
        if cfg.connection_string is not None:
            return cfg.connection_string.get_secret_value()
        assert cfg.dsn is not None
        return f"DSN={cfg.dsn}"

    def open(self) -> None:
        if self._con is not None:
            return
        pyodbc = _import_pyodbc()
        kwargs: dict[str, Any] = {"autocommit": True, **self._config.options}
        if self.config.read_only:
            kwargs.setdefault("readonly", True)
        try:
            con = pyodbc.connect(self._connection_string(), **kwargs)
        except pyodbc.Error as exc:
            raise ConnectorError(
                f"Could not connect through ODBC for connection '{self.name}': "
                f"{self._redact(str(exc))}",
                hint="Check the DSN or connection string, the driver name, and credentials; run "
                "'dvf connections test'.",
            ) from None
        try:
            self._setup(pyodbc, con)
        except Exception:
            con.close()
            raise
        self._con = con

    def _setup(self, pyodbc: Any, con: Any) -> None:
        from dvf.packs.catalog.generic import dialect_for_dbms, session_statements

        self._dbms = str(con.getinfo(pyodbc.SQL_DBMS_NAME) or "")
        if not self.dialect:
            setattr(  # noqa: B010
                self, "dialect", dialect_for_dbms(self._dbms, self._config.dialect, self.name)
            )
        if self.dialect == "sqlite":
            raise ConnectorError(
                f"Connection '{self.name}': the SQLite dialect needs dvf's SQL functions, which "
                "cannot be registered through ODBC.",
                hint="Use a 'sqlite' or 'sqlalchemy' (sqlite:///) connection for SQLite files.",
            )
        if self.config.query_timeout_s:
            try:
                con.timeout = int(self.config.query_timeout_s)
            except pyodbc.Error:  # psqlODBC and others reject SQL_ATTR_CONNECTION_TIMEOUT
                log.debug("ODBC driver of '%s' has no connection timeout", self.name)
        statements = session_statements(
            self.dialect, read_only=self.config.read_only, timeout_s=self.config.query_timeout_s
        )
        cursor = con.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
        except pyodbc.Error as exc:
            raise ConnectorError(
                f"Could not set up the ODBC session for connection '{self.name}': {exc}"
            ) from None
        finally:
            cursor.close()

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None

    @property
    def connection(self) -> Any:
        self.open()
        return self._con

    def server_version(self) -> str | None:
        pyodbc = _import_pyodbc()
        version = self.connection.getinfo(pyodbc.SQL_DBMS_VER)
        return f"{self._dbms} {version}".strip()

    def _redact(self, text: str) -> str:
        cfg = self._config
        if cfg.connection_string is not None:
            secret = cfg.connection_string.get_secret_value()
            text = text.replace(secret, "***")
            for part in secret.split(";"):
                key, _, value = part.partition("=")
                if key.strip().lower() in ("pwd", "password") and value:
                    text = text.replace(value, "***")
        return text

    # ------------------------------------------------------------------ queries
    def _execute(self, sql: str) -> Any:
        pyodbc = _import_pyodbc()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
        except pyodbc.Error as exc:
            cursor.close()
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        return cursor

    def query(self, sql: str) -> pa.Table:
        cursor = self._execute(sql)
        try:
            names = [d[0] for d in cursor.description or ()]
            rows = [tuple(r) for r in cursor.fetchall()] if cursor.description else []
        finally:
            cursor.close()
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        cursor = self._execute(sql)
        try:
            names = [d[0] for d in cursor.description or ()]
            empty = True
            while cursor.description:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                empty = False
                yield from batches_from_rows(names, [tuple(r) for r in rows], batch_size)
            if empty:
                yield from rows_to_table(names, []).to_batches()
        finally:
            cursor.close()

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.as_of is not None:
            raise ConnectorError(
                f"Connection '{self.name}' (odbc) does not support 'as_of'; write the "
                "time-travel clause in a 'query'."
            )
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is a database; use 'table' or 'query', not 'path'."
            )
        table: str | None = None
        dialect = self.sql_dialect
        if spec.table is not None:
            base, table, kind, label = spec.table, spec.table, DatasetKind.TABLE, spec.table
        else:
            assert spec.query is not None
            base = dialect.subquery(spec.query, "dvf_query")
            kind, label = DatasetKind.QUERY, "query"
        ref = DatasetRef(
            connection=self.name,
            kind=kind,
            name=label,
            query=spec.query if kind is DatasetKind.QUERY else None,
        )
        sql = base
        if spec.filter:
            sql = dialect.subquery(f"SELECT * FROM {base} WHERE {spec.filter}", "dvf_filtered")
        return Relation(ref, sql, base, table, spec.filter, self.name)

    def _session_schema(self) -> str | None:
        if self._default_schema is None:
            sql = _CURRENT_SCHEMA.get(self.dialect)
            if sql is not None:
                try:
                    rows = self.query(sql).to_pylist()
                    value = next(iter(rows[0].values())) if rows else None
                    self._default_schema = None if value is None else str(value)
                except ConnectorError:
                    log.debug("current schema of '%s' unknown", self.name, exc_info=True)
        return self._default_schema

    def _split(self, table: str) -> tuple[str | None, str]:
        from dvf.packs.catalog.introspect import split_table

        self.open()
        name = split_table(table, self.dialect)
        return name.schema or self._config.schema_ or self._session_schema(), name.name

    @staticmethod
    def _same(value: Any, wanted: str | None) -> bool:
        return wanted is None or (value is not None and str(value).lower() == wanted.lower())

    def _catalog(self, function: str, table: str | None, schema: str | None) -> list[Any]:
        """Rows of an ODBC catalog function, filtered to the exact table and schema (catalog
        patterns treat ``_`` and ``%`` as wildcards)."""
        pyodbc = _import_pyodbc()
        cursor = self.connection.cursor()
        try:
            if self.dialect == "mysql":  # MySQL drivers report databases as catalogs
                rows = list(getattr(cursor, function)(table=table, catalog=schema))
            else:
                rows = list(getattr(cursor, function)(table=table, schema=schema))
        except pyodbc.Error as exc:
            raise ConnectorError(
                f"Catalog lookup failed on connection '{self.name}': {exc}"
            ) from exc
        finally:
            cursor.close()
        schema_attr = "table_cat" if self.dialect == "mysql" else "table_schem"
        return [
            r
            for r in rows
            if (table is None or self._same(r.table_name, table))
            and self._same(getattr(r, schema_attr, None), schema)
        ]

    def exists(self, relation: Relation) -> bool:
        if relation.table is not None:
            schema, name = self._split(relation.table)
            try:
                if self._catalog("tables", name, schema):
                    return True
            except ConnectorError:
                log.debug("catalog lookup of %s failed", relation.table, exc_info=True)
        return super().exists(relation)

    def get_schema(self, relation: Relation) -> DatasetSchema:
        from dvf.packs.catalog.generic import describe_columns, odbc_native_type

        if relation.table is not None:
            schema, name = self._split(relation.table)
            rows = self._catalog("columns", name, schema)
            if not rows:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'."
                )
            rows.sort(key=lambda r: int(getattr(r, "ordinal_position", 0) or 0))
            columns = []
            for i, row in enumerate(rows):
                native = odbc_native_type(row.type_name, row.column_size, row.decimal_digits)
                nullable = getattr(row, "nullable", None)
                default = getattr(row, "column_def", None)
                columns.append(
                    ColumnSchema(
                        name=str(row.column_name),
                        native_type=native,
                        type=parse_native_type(native, self.dialect),
                        nullable=None if nullable is None else int(nullable) != 0,
                        default=None if default is None else str(default),
                        ordinal=int(getattr(row, "ordinal_position", 0) or i + 1),
                        comment=getattr(row, "remarks", None) or None,
                    )
                )
            return DatasetSchema(
                ref=relation.ref,
                columns=tuple(columns),
                primary_key=self.get_primary_key(relation),
            )
        cursor = self._execute(f"SELECT * FROM {relation.base_sql} WHERE 1 = 0")
        try:
            description = list(cursor.description or ())
        finally:
            cursor.close()
        return DatasetSchema(
            ref=relation.ref, columns=describe_columns(description, [], self.dialect)
        )

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        schema, name = self._split(relation.table)
        try:
            rows = self._catalog("primaryKeys", name, schema)
        except ConnectorError:
            log.debug("primary key of %s could not be read", relation.table, exc_info=True)
            return ()
        rows.sort(key=lambda r: int(getattr(r, "key_seq", 0) or 0))
        return tuple(str(r.column_name) for r in rows)

    def list_datasets(self, pattern: str) -> list[str]:
        schema = self._config.schema_ or self._session_schema()
        qualified_pattern = "." in pattern
        if qualified_pattern:
            schema = pattern.rsplit(".", 1)[0]
        pyodbc = _import_pyodbc()
        cursor = self.connection.cursor()
        try:
            rows = list(cursor.tables(schema=schema, tableType="TABLE,VIEW"))
        except pyodbc.Error as exc:
            raise ConnectorError(
                f"Catalog lookup failed on connection '{self.name}': {exc}"
            ) from exc
        finally:
            cursor.close()
        found = []
        for row in rows:
            if not self._same(row.table_schem, schema):
                continue
            name = (
                f"{row.table_schem}.{row.table_name}" if qualified_pattern else str(row.table_name)
            )
            if fnmatchcase(name.lower(), pattern.lower()):
                found.append(name)
        return sorted(set(found))

    # ------------------------------------------------------------------ catalog metadata
    def get_physical_design(self, relation: Relation) -> PhysicalDesign | None:
        from dvf.packs.catalog.introspect import physical_design

        self.open()
        return physical_design(self, relation)

    def get_collations(self, relation: Relation) -> CollationInfo | None:
        from dvf.packs.catalog.introspect import collations

        self.open()
        return collations(self, relation)

    def get_object_inventory(self, relation: Relation) -> ObjectInventory | None:
        from dvf.packs.catalog.introspect import object_inventory

        self.open()
        return object_inventory(self, relation)


__all__ = ["OdbcConfig", "OdbcConnector"]
