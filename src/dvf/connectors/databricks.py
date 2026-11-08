"""Databricks SQL connector (beta; install with ``pip install 'dvf[databricks]'``).

Implemented from the databricks-sql-connector documentation and tested against a fake driver;
it has not been verified against a live workspace.

Connect to a SQL warehouse or cluster with ``server_hostname`` and ``http_path``. Authenticate
with a personal access token (``access_token``) or another ``auth_type`` supported by the driver,
such as ``databricks-oauth`` (browser sign-in); further driver arguments go in ``options``.

Every session runs ``SET TIME ZONE 'UTC'`` (the Databricks dialect relies on it to render
``TIMESTAMP`` values in UTC) and, when ``query_timeout_s`` is set, ``SET STATEMENT_TIMEOUT``
(SQL warehouses only; clusters ignore the setting). There is no read-only session mode: grant
the principal ``USE CATALOG``, ``USE SCHEMA``, and ``SELECT`` only.

Schemas come from Unity Catalog's ``information_schema`` (with nullability and declared,
informational primary keys), falling back to ``DESCRIBE TABLE`` for the Hive metastore.
``as_of`` reads Delta tables with ``VERSION AS OF`` (an integer) or ``TIMESTAMP AS OF``.
"""

from __future__ import annotations

import contextlib
import importlib
import re
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlglot import exp

from dvf.config.connections import DatabricksConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import Connector, Relation
from dvf.sql.databricks import DIALECT as _SQL

_VERSION = re.compile(r"\d+")
_HIVE = "hive_metastore"


def _import_databricks() -> Any:
    try:
        return importlib.import_module("databricks.sql")
    except ImportError as exc:
        raise DependencyError(
            "databricks-sql-connector", "databricks", "The Databricks connector"
        ) from exc


def _error_type(module: Any) -> type[BaseException]:
    error = getattr(module, "Error", None)
    return error if isinstance(error, type) and issubclass(error, BaseException) else Exception


def _hint(exc: BaseException) -> str:
    text = str(exc).lower()
    if any(w in text for w in ("401", "403", "unauthorized", "invalid access token", "token")):
        return (
            "Authentication failed: check 'access_token' (a personal access token) or "
            "'auth_type', and that the token has not expired."
        )
    if "permission" in text or "insufficient_permissions" in text or "not authorized" in text:
        return "Grant USE CATALOG, USE SCHEMA, and SELECT on the table to the dvf principal."
    if "table_or_view_not_found" in text or "not found" in text:
        return "Check the catalog, schema, and table names (and the connection's catalog/schema)."
    if "http_path" in text or "warehouse" in text or "404" in text:
        return "Check 'server_hostname' and 'http_path' (from the warehouse's Connection details)."
    return "Check server_hostname, http_path, and credentials; run 'dvf connections test'."


def hostname(value: str) -> str:
    """The bare host name (people often paste the workspace URL)."""
    text = value.strip()
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    return text.rstrip("/")


def as_of_clause(as_of: str) -> str:
    """``VERSION AS OF n`` for an integer, ``TIMESTAMP AS OF '...'`` otherwise."""
    value = as_of.strip()
    if _VERSION.fullmatch(value):
        return f"VERSION AS OF {int(value)}"
    return f"TIMESTAMP AS OF {_SQL.literal(value)}"


def describe_rows(rows: list[tuple[Any, ...]]) -> list[tuple[str, str, str | None]]:
    """Column rows of ``DESCRIBE TABLE`` output, which ends with partition and detail sections."""
    columns: list[tuple[str, str, str | None]] = []
    for row in rows:
        name = str(row[0] or "").strip()
        if not name or name.startswith("#"):
            break
        comment = row[2] if len(row) > 2 else None
        columns.append((name, str(row[1]), comment or None))
    return columns


class DatabricksConnector(Connector):
    """Databricks SQL warehouses and clusters, through databricks-sql-connector."""

    dialect = "databricks"
    Config = DatabricksConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: Any = None
        self._defaults: tuple[str, str] | None = None

    @property
    def _config(self) -> DatabricksConnection:
        config = self.config
        assert isinstance(config, DatabricksConnection)
        return config

    def _connect_args(self) -> dict[str, Any]:
        """Keyword arguments for ``databricks.sql.connect`` (contains secrets)."""
        c = self._config
        kwargs: dict[str, Any] = {
            "server_hostname": hostname(c.server_hostname),
            "http_path": c.http_path,
        }
        if c.access_token is not None:
            kwargs["access_token"] = c.access_token.get_secret_value()
        if c.auth_type:
            kwargs["auth_type"] = c.auth_type
        if c.catalog:
            kwargs["catalog"] = c.catalog
        if c.schema_:
            kwargs["schema"] = c.schema_
        kwargs.update(c.options)
        return kwargs

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self._con is not None:
            return
        dbsql = _import_databricks()
        kwargs = self._connect_args()
        try:
            con = dbsql.connect(**kwargs)
        except (_error_type(dbsql), OSError) as exc:
            raise ConnectorError(
                f"Could not connect to Databricks for connection '{self.name}': {exc}",
                hint=_hint(exc),
            ) from exc
        self._con = con
        try:
            self._execute("SET TIME ZONE 'UTC'", fetch=False)
        except ConnectorError:
            self.close()
            raise
        timeout = self._config.query_timeout_s
        if timeout:
            # SQL warehouses only: clusters have no statement timeout parameter.
            with contextlib.suppress(ConnectorError):
                self._execute(f"SET STATEMENT_TIMEOUT = {int(timeout)}", fetch=False)

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None
                self._defaults = None

    @property
    def connection(self) -> Any:
        self.open()
        return self._con

    def server_version(self) -> str | None:
        rows = self._execute("SELECT version()")[1]
        return f"Databricks (Spark {str(rows[0][0]).split()[0]})" if rows and rows[0][0] else None

    # ------------------------------------------------------------------ queries
    def _fail(self, exc: BaseException) -> ConnectorError:
        return ConnectorError(f"Query failed on connection '{self.name}': {exc}", hint=_hint(exc))

    def _execute(self, sql: str, *, fetch: bool = True) -> tuple[list[str], list[tuple[Any, ...]]]:
        dbsql = _import_databricks()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            if not fetch:
                return [], []
            names = [str(d[0]) for d in cursor.description or ()]
            rows = [tuple(r) for r in cursor.fetchall()] if cursor.description else []
        except _error_type(dbsql) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()
        return names, rows

    def query(self, sql: str) -> pa.Table:
        dbsql = _import_databricks()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            fetch = getattr(cursor, "fetchall_arrow", None)
            if fetch is not None:
                table: pa.Table = fetch()
            else:
                names = [str(d[0]) for d in cursor.description or ()]
                table = rows_to_table(names, [tuple(r) for r in cursor.fetchall()])
        except _error_type(dbsql) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()
        return table

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        dbsql = _import_databricks()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            while True:
                table = cursor.fetchmany_arrow(batch_size)
                if table.num_rows == 0:
                    return
                yield from table.to_batches(max_chunksize=batch_size)
        except _error_type(dbsql) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is Databricks SQL; use 'table' or 'query', not 'path'."
            )
        table: str | None = None
        if spec.table is not None:
            kind, label = DatasetKind.TABLE, spec.table
            if spec.as_of is not None:
                base = f"{spec.table} {as_of_clause(spec.as_of)}"
                label = f"{spec.table} AS OF {spec.as_of}"
            else:
                base, table = spec.table, spec.table
        else:
            assert spec.query is not None
            if spec.as_of is not None:
                raise ConnectorError(
                    f"'as_of' applies to tables on connection '{self.name}'; "
                    "write TIMESTAMP AS OF or VERSION AS OF inside the query instead."
                )
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

    def _current(self) -> tuple[str, str]:
        """The session's current catalog and schema."""
        if self._defaults is None:
            rows = self._execute("SELECT current_catalog(), current_schema()")[1]
            self._defaults = (str(rows[0][0]), str(rows[0][1])) if rows else ("", "")
        return self._defaults

    def _parts(self, name: str) -> tuple[str, str, str]:
        """Catalog, schema, and table of ``name``, lower-cased as Unity Catalog stores them."""
        try:
            parsed = exp.to_table(name, dialect="databricks")
        except Exception as exc:  # sqlglot raises several error types for bad names
            raise ConnectorError(f"Cannot parse table name '{name}': {exc}") from exc
        catalog, schema = parsed.catalog, parsed.db
        if not catalog or not schema:
            current_catalog, current_schema = self._current()
            catalog = catalog or current_catalog
            schema = schema or current_schema
        return catalog.lower(), schema.lower(), parsed.name.lower()

    def _info_schema_columns(self, catalog: str, schema: str, table: str) -> list[tuple[Any, ...]]:
        if not catalog or catalog == _HIVE:
            return []
        sql = (
            "SELECT column_name, full_data_type, is_nullable, column_default, ordinal_position, "
            f"comment FROM {_SQL.quote(catalog)}.information_schema.columns "
            f"WHERE table_schema = {_SQL.literal(schema)} AND table_name = {_SQL.literal(table)} "
            "ORDER BY ordinal_position"
        )
        try:
            return self._execute(sql)[1]
        except ConnectorError:
            return []  # no Unity Catalog information schema here; fall back to DESCRIBE

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        try:
            self._execute(f"DESCRIBE TABLE {relation.table}")
        except ConnectorError:
            return False
        return True

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is None:
            rows = self._execute(f"DESCRIBE QUERY SELECT * FROM {relation.base_sql}")[1]
            return DatasetSchema(ref=relation.ref, columns=_described(describe_rows(rows)))
        catalog, schema, table = self._parts(relation.table)
        info = self._info_schema_columns(catalog, schema, table)
        if info:
            columns = tuple(
                ColumnSchema(
                    name=str(r[0]),
                    native_type=str(r[1]),
                    type=parse_native_type(str(r[1]), "databricks"),
                    nullable=str(r[2]).upper() != "NO",
                    default=None if r[3] is None else str(r[3]),
                    ordinal=int(r[4]),
                    comment=r[5] or None,
                )
                for r in info
            )
        else:
            try:
                rows = self._execute(f"DESCRIBE TABLE {relation.table}")[1]
            except ConnectorError as exc:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}': {exc}",
                    hint=exc.hint,
                ) from exc
            columns = _described(describe_rows(rows))
        return DatasetSchema(
            ref=relation.ref, columns=columns, primary_key=self.get_primary_key(relation)
        )

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        catalog, schema, table = self._parts(relation.table)
        if not catalog or catalog == _HIVE:
            return ()
        info = f"{_SQL.quote(catalog)}.information_schema"
        sql = (
            f"SELECT k.column_name FROM {info}.table_constraints t "
            f"JOIN {info}.key_column_usage k ON k.constraint_catalog = t.constraint_catalog "
            "AND k.constraint_schema = t.constraint_schema AND k.constraint_name = t.constraint_name "
            f"WHERE t.constraint_type = 'PRIMARY KEY' AND t.table_schema = {_SQL.literal(schema)} "
            f"AND t.table_name = {_SQL.literal(table)} ORDER BY k.ordinal_position"
        )
        try:
            rows = self._execute(sql)[1]
        except ConnectorError:
            return ()
        return tuple(str(r[0]) for r in rows)

    def _catalog_tables(self, catalog: str) -> list[tuple[str, str]] | None:
        """(schema, table) pairs from Unity Catalog, or ``None`` without an information schema."""
        if not catalog or catalog == _HIVE:
            return None
        sql = (
            f"SELECT table_schema, table_name FROM {_SQL.quote(catalog)}.information_schema.tables "
            "WHERE table_schema <> 'information_schema'"
        )
        try:
            rows = self._execute(sql)[1]
        except ConnectorError:
            return None
        return [(str(r[0]), str(r[1])) for r in rows]

    def list_datasets(self, pattern: str) -> list[str]:
        catalog, current_schema = self._current()
        pairs = self._catalog_tables(catalog)
        if pairs is None:
            # Hive metastore: list one schema (the pattern's, which must not be a glob).
            schema = pattern.split(".")[0] if "." in pattern else current_schema
            rows = self._execute(f"SHOW TABLES IN {_SQL.quote(schema)}")[1]
            pairs = [(str(r[0] or schema), str(r[1])) for r in rows]
        qualified_pattern = "." in pattern
        names: list[str] = []
        for schema, table in pairs:
            if qualified_pattern:
                target = f"{schema}.{table}"
            elif schema.lower() == current_schema.lower():
                target = table
            else:
                continue
            if fnmatchcase(target.lower(), pattern.lower()):
                names.append(target)
        return sorted(set(names))


def _described(columns: list[tuple[str, str, str | None]]) -> tuple[ColumnSchema, ...]:
    return tuple(
        ColumnSchema(
            name=name,
            native_type=native,
            type=parse_native_type(native, "databricks"),
            ordinal=i + 1,
            comment=comment,
        )
        for i, (name, native, comment) in enumerate(columns)
    )
