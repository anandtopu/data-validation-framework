"""Microsoft SQL Server connector (mssql-python; install with ``pip install 'dvf-framework[mssql]'``).

``mssql-python`` is Microsoft's DB-API driver; it bundles its own TDS client, so no ODBC driver
needs to be installed. Query timeouts use the driver's per-statement timeout. SQL Server has no
read-only session mode: grant the dvf login read permissions only (for example
``db_datareader``); dvf itself never writes. Time travel (``as_of``) reads system-versioned
temporal tables with ``FOR SYSTEM_TIME AS OF``, in UTC.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pyarrow as pa

from dvf.config.connections import DatabaseConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import batches_from_rows, rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import CollationInfo, Connector, ObjectInventory, PhysicalDesign, Relation

logger = logging.getLogger(__name__)

DEFAULT_PORT = 1433
_SCHEMES = ("mssql", "sqlserver")
_LOGIN_TIMEOUT_KEYS = ("login_timeout", "timeout", "connect_timeout")

_COLUMNS_SQL = """
SELECT c.name,
       CASE WHEN t.is_user_defined = 1 AND t.is_assembly_type = 0
            THEN TYPE_NAME(c.system_type_id) ELSE t.name END,
       c.max_length, c.precision, c.scale, c.is_nullable,
       OBJECT_DEFINITION(c.default_object_id), c.column_id,
       CAST(ep.value AS NVARCHAR(4000))
FROM sys.columns c
JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.extended_properties ep
  ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id
 AND ep.name = 'MS_Description'
WHERE c.object_id = OBJECT_ID(?)
ORDER BY c.column_id
"""

_PK_SQL = """
SELECT c.name
FROM sys.indexes i
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = OBJECT_ID(?) AND i.is_primary_key = 1
ORDER BY ic.key_ordinal
"""

_DESCRIBE_SQL = """
SELECT name, system_type_name, is_nullable, column_ordinal
FROM sys.dm_exec_describe_first_result_set(?, NULL, 0)
WHERE is_hidden = 0
ORDER BY column_ordinal
"""

_CHAR_TYPES = frozenset({"char", "varchar", "binary", "varbinary"})
_NCHAR_TYPES = frozenset({"nchar", "nvarchar"})
_SCALED_TYPES = frozenset({"datetime2", "datetimeoffset", "time"})
_PRECISION_TYPES = frozenset({"decimal", "numeric"})


def _import_driver() -> Any:
    try:
        import mssql_python
    except ImportError as exc:
        raise DependencyError("mssql-python", "mssql", "The SQL Server connector") from exc
    return mssql_python


def parse_url(url: str) -> dict[str, Any]:
    """Settings from ``mssql://user:pass@host:1433/database?Encrypt=yes``.

    ``mssql+pyodbc://``, ``mssql+mssql_python://``, and ``sqlserver://`` are accepted. Query
    parameters are connection-string attributes (``Encrypt``, ``TrustServerCertificate``, ...).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0].lower()
    if scheme not in _SCHEMES:
        raise ConnectorError(
            f"Unsupported URL scheme '{parts.scheme}' for the SQL Server connector.",
            hint="Use a URL like mssql://user:password@host:1433/database",
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ConnectorError(f"Invalid port in SQL Server URL: {exc}") from None
    settings: dict[str, Any] = {"host": parts.hostname or "localhost", "port": port}
    if parts.username:
        settings["user"] = unquote(parts.username)
    if parts.password is not None:
        settings["password"] = unquote(parts.password)
    database = unquote(parts.path.lstrip("/"))
    if database:
        settings["database"] = database
    settings["options"] = dict(parse_qsl(parts.query, keep_blank_values=True))
    return settings


def quote_value(value: Any) -> str:
    """A connection-string attribute value, braced when it contains special characters."""
    text = "yes" if value is True else "no" if value is False else str(value)
    if re.search(r"[;{}=\s]", text) or text != text.strip() or not text:
        return "{" + text.replace("}", "}}") + "}"
    return text


def build_connection_string(
    host: str | None,
    port: int | None,
    database: str | None,
    user: str | None,
    password: str | None,
    options: Mapping[str, Any],
) -> str:
    """An ODBC-style connection string for ``mssql_python.connect`` (contains the password)."""
    server = host or "localhost"
    if "\\" not in server and "," not in server:
        server = f"{server},{port or DEFAULT_PORT}"
    if not server.lower().startswith(("tcp:", "np:", "lpc:", "admin:")):
        server = f"tcp:{server}"
    attrs: dict[str, Any] = {"Server": server}
    if database:
        attrs["Database"] = database
    if user is not None:
        attrs["UID"] = user
    if password is not None:
        attrs["PWD"] = password
    for key, value in options.items():
        if key.lower() in _LOGIN_TIMEOUT_KEYS or value is None:
            continue
        attrs[key] = value
    parts = []
    for key, value in attrs.items():
        rendered = "{" + str(value).replace("}", "}}") + "}" if key == "PWD" else quote_value(value)
        parts.append(f"{key}={rendered}")
    return ";".join(parts)


class MSSQLConnector(Connector):
    """SQL Server 2019+ and Azure SQL through mssql-python."""

    dialect = "mssql"
    Config = DatabaseConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: Any = None

    # ------------------------------------------------------------------ lifecycle
    def _settings(self) -> dict[str, Any]:
        config = self.config
        assert isinstance(config, DatabaseConnection)
        settings: dict[str, Any] = {"options": {}}
        if config.url is not None:
            settings.update(parse_url(config.url.get_secret_value()))
        else:
            settings.update(host=config.host, port=config.port)
        if config.database is not None:
            settings["database"] = config.database
        if config.user is not None:
            settings["user"] = config.user
        if config.password is not None:
            settings["password"] = config.password.get_secret_value()
        settings["options"] = {**settings["options"], **config.options}
        return settings

    def connection_string(self) -> str:
        """The connection string (contains the password; never log it)."""
        s = self._settings()
        return build_connection_string(
            s.get("host"),
            s.get("port"),
            s.get("database"),
            s.get("user"),
            s.get("password"),
            s["options"],
        )

    def _login_timeout(self) -> int:
        options = self._settings()["options"]
        for key, value in options.items():
            if key.lower() in _LOGIN_TIMEOUT_KEYS:
                return int(value)
        return 15

    def open(self) -> None:
        if self._con is not None:
            return
        driver = _import_driver()
        settings = self._settings()
        secret = settings.get("password")
        try:
            con = driver.connect(
                self.connection_string(), autocommit=True, timeout=self._login_timeout()
            )
        except driver.Error as exc:
            raise ConnectorError(
                f"Could not connect to SQL Server for connection '{self.name}' "
                f"({settings.get('host')}:{settings.get('port') or DEFAULT_PORT}): "
                f"{_message(exc, secret)}",
                hint="Check host, port, database, and credentials. For a local server with a "
                "self-signed certificate add TrustServerCertificate=yes to the URL.",
            ) from None
        except Exception as exc:  # connection-string validation errors
            raise ConnectorError(
                f"Invalid SQL Server settings for connection '{self.name}': "
                f"{_message(exc, secret)}",
                hint="Connection options must be connection-string attributes such as Encrypt "
                "or TrustServerCertificate.",
            ) from None
        try:
            cur = con.cursor()
            cur.execute("SET DATEFORMAT ymd; SET ARITHABORT ON; SET ANSI_WARNINGS ON")
            cur.close()
            if self.config.query_timeout_s:
                con.timeout = int(self.config.query_timeout_s)
        except driver.Error as exc:
            con.close()
            raise ConnectorError(
                f"Could not set up the SQL Server session for connection '{self.name}': "
                f"{_message(exc)}"
            ) from None
        self._con = con

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                logger.debug("closing SQL Server connection '%s' failed", self.name, exc_info=True)
            self._con = None

    @property
    def connection(self) -> Any:
        self.open()
        return self._con

    def server_version(self) -> str | None:
        _, rows = self._rows(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)), "
            "CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128))"
        )
        if not rows:
            return None
        version, edition = rows[0]
        return f"SQL Server {version} ({edition})"

    # ------------------------------------------------------------------ queries
    def _error(self, exc: Exception) -> ConnectorError:
        return ConnectorError(f"Query failed on connection '{self.name}': {_message(exc)}")

    def _rows(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        driver = _import_driver()
        try:
            cur = self.connection.cursor()
            try:
                if params is None:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)
                if not cur.description:
                    return [], []
                names = [d[0] for d in cur.description]
                return names, [tuple(r) for r in cur.fetchall()]
            finally:
                cur.close()
        except driver.Error as exc:
            raise self._error(exc) from None

    def query(self, sql: str) -> pa.Table:
        names, rows = self._rows(sql)
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        driver = _import_driver()
        cur = None
        try:
            cur = self.connection.cursor()
            cur.execute(sql)
            names = [d[0] for d in cur.description or ()]
            empty = True
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                empty = False
                yield from batches_from_rows(names, [tuple(r) for r in rows], batch_size)
            if empty:
                yield from rows_to_table(names, []).to_batches()
        except driver.Error as exc:
            raise self._error(exc) from None
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    logger.debug("closing a SQL Server cursor failed", exc_info=True)

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is a SQL Server database; use 'table' or 'query', "
                "not 'path'."
            )
        table: str | None = None
        if spec.table is not None:
            base, table, kind, label = spec.table, spec.table, DatasetKind.TABLE, spec.table
            if spec.as_of is not None:
                instant = as_of_utc(spec.as_of)
                base = f"{spec.table} FOR SYSTEM_TIME AS OF '{instant}'"
        else:
            assert spec.query is not None
            if spec.as_of is not None:
                raise ConnectorError(
                    f"Connection '{self.name}': 'as_of' applies to tables, not queries.",
                    hint="Write FOR SYSTEM_TIME AS OF into the query instead.",
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

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        _, rows = self._rows(
            "SELECT CASE WHEN OBJECT_ID(?) IS NULL THEN 0 ELSE 1 END", (relation.table,)
        )
        return bool(rows and rows[0][0])

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            _, rows = self._rows(_COLUMNS_SQL, (relation.table,))
            if not rows:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'.",
                    hint="Check the name (schema.table) and that the login can read it.",
                )
            columns = tuple(
                ColumnSchema(
                    name=str(r[0]),
                    native_type=(native := native_type_name(str(r[1]), r[2], r[3], r[4])),
                    type=parse_native_type(native, "mssql"),
                    nullable=bool(r[5]),
                    default=None if r[6] is None else str(r[6]),
                    ordinal=int(r[7]),
                    comment=None if r[8] is None else str(r[8]),
                )
                for r in rows
            )
            return DatasetSchema(
                ref=relation.ref, columns=columns, primary_key=self.get_primary_key(relation)
            )
        _, rows = self._rows(_DESCRIBE_SQL, (f"SELECT * FROM {relation.base_sql}",))
        columns = tuple(
            ColumnSchema(
                name=str(r[0]),
                native_type=str(r[1]),
                type=parse_native_type(str(r[1]), "mssql"),
                nullable=bool(r[2]),
                ordinal=int(r[3]),
            )
            for r in rows
        )
        return DatasetSchema(ref=relation.ref, columns=columns)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        _, rows = self._rows(_PK_SQL, (relation.table,))
        return tuple(str(r[0]) for r in rows)

    def list_datasets(self, pattern: str) -> list[str]:
        _, rows = self._rows(
            "SELECT TABLE_SCHEMA, TABLE_NAME, CASE WHEN TABLE_SCHEMA = SCHEMA_NAME() THEN 1 "
            "ELSE 0 END FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')"
        )
        names: list[str] = []
        for schema, table, current in rows:
            if "." in pattern:
                target = f"{schema}.{table}"
            elif current:
                target = str(table)
            else:
                continue
            if fnmatchcase(target.lower(), pattern.lower()):
                names.append(target)
        return sorted(names)

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


def native_type_name(type_name: str, max_length: Any, precision: Any, scale: Any) -> str:
    """A SQL Server type string (``nvarchar(50)``, ``decimal(12,3)``) from ``sys.columns``."""
    name = type_name.lower()
    if name in _NCHAR_TYPES:
        size = int(max_length)
        return f"{name}(max)" if size == -1 else f"{name}({size // 2})"
    if name in _CHAR_TYPES:
        size = int(max_length)
        return f"{name}(max)" if size == -1 else f"{name}({size})"
    if name in _PRECISION_TYPES:
        return f"{name}({int(precision)},{int(scale)})"
    if name in _SCALED_TYPES:
        return f"{name}({int(scale)})"
    if name == "float":
        return f"float({int(precision)})"
    return name


def as_of_utc(value: str) -> str:
    """An ``as_of`` timestamp as UTC ``YYYY-MM-DDTHH:MM:SS.ffffff`` (naive values are UTC)."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ConnectorError(
            f"'as_of' must be an ISO 8601 timestamp, got {value!r}.",
            hint="For example as_of: '2024-06-30T23:59:59Z'.",
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.isoformat(timespec="microseconds")


def _message(exc: Exception, secret: str | None = None) -> str:
    text = " ".join(str(exc).split())
    if secret:
        text = text.replace(secret, "***")
    return text
