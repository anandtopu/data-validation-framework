"""MySQL connector (PyMySQL; install with ``pip install 'dvf[mysql]'``).

Sessions are read-only (``SET SESSION TRANSACTION READ ONLY``), use UTC (``time_zone =
'+00:00'``, so ``TIMESTAMP`` columns are read in UTC), and limit statements with
``max_execution_time`` when ``query_timeout_s`` is set. Large results stream through an
unbuffered server-side cursor.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
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
from dvf.plugins.base import Connector, Relation

logger = logging.getLogger(__name__)

DEFAULT_PORT = 3306
_SCHEMES = ("mysql", "mariadb")
_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")

_COLUMNS_SQL = """
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION, COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s
ORDER BY ORDINAL_POSITION
"""

_PK_SQL = """
SELECT k.COLUMN_NAME
FROM information_schema.TABLE_CONSTRAINTS c
JOIN information_schema.KEY_COLUMN_USAGE k
  ON k.CONSTRAINT_SCHEMA = c.CONSTRAINT_SCHEMA AND k.CONSTRAINT_NAME = c.CONSTRAINT_NAME
 AND k.TABLE_SCHEMA = c.TABLE_SCHEMA AND k.TABLE_NAME = c.TABLE_NAME
WHERE c.CONSTRAINT_TYPE = 'PRIMARY KEY'
  AND c.TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND c.TABLE_NAME = %s
ORDER BY k.ORDINAL_POSITION
"""

_EXISTS_SQL = """
SELECT COUNT(*) FROM information_schema.TABLES
WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s
"""

# pymysql.constants.FIELD_TYPE codes -> MySQL type names, for the schema of query datasets.
_FIELD_TYPES = {
    0: "decimal",
    1: "tinyint",
    2: "smallint",
    3: "int",
    4: "float",
    5: "double",
    6: "null",
    7: "timestamp",
    8: "bigint",
    9: "mediumint",
    10: "date",
    11: "time",
    12: "datetime",
    13: "year",
    15: "varchar",
    16: "bit",
    245: "json",
    246: "decimal",
    247: "enum",
    248: "set",
    249: "tinyblob",
    250: "mediumblob",
    251: "longblob",
    252: "blob",
    253: "varchar",
    254: "char",
    255: "geometry",
}
_BINARY_CHARSET = 63
_TEMPORAL_WITH_FRACTION = frozenset({"timestamp", "datetime", "time"})


def _import_pymysql() -> Any:
    try:
        import pymysql  # type: ignore[import-untyped]
    except ImportError as exc:
        raise DependencyError("pymysql", "mysql", "The MySQL connector") from exc
    return pymysql


def parse_url(url: str) -> dict[str, Any]:
    """Connection keyword arguments from ``mysql://user:pass@host:3306/db?opt=value``.

    ``mysql+pymysql://`` and ``mariadb://`` are accepted. Query parameters become driver
    options (numbers and booleans are converted).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0].lower()
    if scheme not in _SCHEMES:
        raise ConnectorError(
            f"Unsupported URL scheme '{parts.scheme}' for the MySQL connector.",
            hint="Use a URL like mysql://user:password@host:3306/database",
        )
    kwargs: dict[str, Any] = {"host": parts.hostname or "localhost"}
    try:
        port = parts.port
    except ValueError as exc:
        raise ConnectorError(f"Invalid port in MySQL URL: {exc}") from None
    kwargs["port"] = port or DEFAULT_PORT
    if parts.username:
        kwargs["user"] = unquote(parts.username)
    if parts.password is not None:
        kwargs["password"] = unquote(parts.password)
    database = unquote(parts.path.lstrip("/"))
    if database:
        kwargs["database"] = database
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        kwargs[key] = _option_value(value)
    return kwargs


def _option_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def split_table(name: str) -> tuple[str | None, str]:
    """``schema.table`` (backticks or double quotes allowed) -> (schema or None, table)."""
    parts = re.findall(r'`((?:[^`]|``)*)`|"((?:[^"]|"")*)"|([^.`"]+)', name.strip())
    names = [
        (b.replace("``", "`") if b else d.replace('""', '"') if d else p.strip())
        for b, d, p in parts
    ]
    names = [n for n in names if n]
    if len(names) == 1:
        return None, names[0]
    if len(names) == 2:
        return names[0], names[1]
    raise ConnectorError(
        f"Cannot parse MySQL table name '{name}'; use 'table' or 'database.table'."
    )


class MySQLConnector(Connector):
    """MySQL 8.0.17+ through PyMySQL."""

    dialect = "mysql"
    Config = DatabaseConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: Any = None

    # ------------------------------------------------------------------ lifecycle
    def connect_args(self) -> dict[str, Any]:
        """Keyword arguments for ``pymysql.connect`` (the password included; never log them)."""
        config = self.config
        assert isinstance(config, DatabaseConnection)
        kwargs: dict[str, Any] = {}
        if config.url is not None:
            kwargs.update(parse_url(config.url.get_secret_value()))
        else:
            kwargs.update(host=config.host, port=config.port or DEFAULT_PORT)
        if config.database is not None:
            kwargs["database"] = config.database
        if config.schema_ is not None:
            kwargs["database"] = config.schema_
        if config.user is not None:
            kwargs["user"] = config.user
        if config.password is not None:
            kwargs["password"] = config.password.get_secret_value()
        kwargs.setdefault("connect_timeout", 15)
        kwargs.update(config.options)
        kwargs["charset"] = "utf8mb4"
        kwargs["autocommit"] = True
        return {k: v for k, v in kwargs.items() if v is not None}

    def open(self) -> None:
        if self._con is not None:
            return
        pymysql = _import_pymysql()
        kwargs = self.connect_args()
        try:
            con = pymysql.connect(**kwargs)
        except pymysql.MySQLError as exc:
            raise ConnectorError(
                f"Could not connect to MySQL for connection '{self.name}' "
                f"({kwargs.get('host')}:{kwargs.get('port')}): {_message(exc)}",
                hint="Check host, port, database, and credentials; run 'dvf connections test'.",
            ) from None
        try:
            with con.cursor() as cur:
                cur.execute("SET time_zone = '+00:00'")
                if self.config.read_only:
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                if self.config.query_timeout_s:
                    cur.execute(
                        f"SET SESSION max_execution_time = {int(self.config.query_timeout_s) * 1000}"
                    )
                cur.execute("SELECT CONVERT_TZ('2000-01-01 00:00:00', 'Europe/Berlin', 'UTC')")
                if cur.fetchone()[0] is None:
                    logger.warning(
                        "MySQL connection '%s' has no time zone tables; timestamps with "
                        "normalization.timezone other than UTC canonicalize to NULL "
                        "(load them with mysql_tzinfo_to_sql).",
                        self.name,
                    )
        except pymysql.MySQLError as exc:
            con.close()
            raise ConnectorError(
                f"Could not set up the MySQL session for connection '{self.name}': {_message(exc)}"
            ) from None
        self._con = con

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                logger.debug("closing MySQL connection '%s' failed", self.name, exc_info=True)
            self._con = None

    @property
    def connection(self) -> Any:
        self.open()
        return self._con

    def server_version(self) -> str | None:
        _, rows = self._rows("SELECT VERSION()")
        version = str(rows[0][0]) if rows else ""
        product = "MariaDB" if "mariadb" in version.lower() else "MySQL"
        return f"{product} {version.split('-')[0]}" if version else None

    # ------------------------------------------------------------------ queries
    def _error(self, exc: Exception) -> ConnectorError:
        return ConnectorError(f"Query failed on connection '{self.name}': {_message(exc)}")

    def _rows(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        pymysql = _import_pymysql()
        try:
            with self.connection.cursor() as cur:
                cur.execute(sql, params)
                names = [d[0] for d in cur.description or ()]
                return names, list(cur.fetchall()) if cur.description else []
        except pymysql.MySQLError as exc:
            raise self._error(exc) from None

    def query(self, sql: str) -> pa.Table:
        names, rows = self._rows(sql)
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        pymysql = _import_pymysql()
        cursor = None
        try:
            cursor = self.connection.cursor(pymysql.cursors.SSCursor)
            cursor.execute(sql)
            names = [d[0] for d in cursor.description or ()]
            empty = True
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                empty = False
                yield from batches_from_rows(names, rows, batch_size)
            if empty:
                yield from rows_to_table(names, []).to_batches()
        except pymysql.MySQLError as exc:
            raise self._error(exc) from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()  # drains unread rows of the unbuffered cursor
                except Exception:
                    logger.debug("closing a MySQL cursor failed", exc_info=True)

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.as_of is not None:
            raise ConnectorError(
                f"Connection '{self.name}' (mysql) does not support 'as_of': MySQL has no "
                "time travel queries.",
                hint="Remove 'as_of', or read a snapshot table instead.",
            )
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is a MySQL database; use 'table' or 'query', not 'path'."
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
        _, rows = self._rows(_EXISTS_SQL, split_table(relation.table))
        return bool(rows and rows[0][0])

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            _, rows = self._rows(_COLUMNS_SQL, split_table(relation.table))
            if not rows:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'.",
                    hint="Check the name (database.table) and that the user can read it.",
                )
            columns = tuple(
                ColumnSchema(
                    name=str(r[0]),
                    native_type=str(r[1]),
                    type=parse_native_type(str(r[1]), "mysql"),
                    nullable=str(r[2]).upper() == "YES",
                    default=None if r[3] is None else str(r[3]),
                    ordinal=int(r[4]),
                    comment=str(r[5]) or None,
                )
                for r in rows
            )
            return DatasetSchema(
                ref=relation.ref, columns=columns, primary_key=self.get_primary_key(relation)
            )
        return DatasetSchema(ref=relation.ref, columns=self._describe(relation))

    def _describe(self, relation: Relation) -> tuple[ColumnSchema, ...]:
        pymysql = _import_pymysql()
        try:
            with self.connection.cursor() as cur:
                cur.execute(f"SELECT * FROM {relation.base_sql} LIMIT 0")
                fields = list(getattr(cur, "_result", None).fields or ())  # type: ignore[union-attr]
                description = list(cur.description or ())
        except pymysql.MySQLError as exc:
            raise self._error(exc) from None
        columns = []
        for i, d in enumerate(description):
            charset = getattr(fields[i], "charsetnr", None) if i < len(fields) else None
            native = native_type_from_description(d, charset)
            columns.append(
                ColumnSchema(
                    name=str(d[0]),
                    native_type=native,
                    type=parse_native_type(native, "mysql"),
                    nullable=bool(d[6]) if len(d) > 6 and d[6] is not None else True,
                    ordinal=i + 1,
                )
            )
        return tuple(columns)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        _, rows = self._rows(_PK_SQL, split_table(relation.table))
        return tuple(str(r[0]) for r in rows)

    def list_datasets(self, pattern: str) -> list[str]:
        placeholders = ", ".join(["%s"] * len(_SYSTEM_SCHEMAS))
        _, rows = self._rows(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_SCHEMA = DATABASE() "
            f"FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ({placeholders})",
            _SYSTEM_SCHEMAS,
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


def native_type_from_description(description: Any, charset: int | None = None) -> str:
    """A MySQL type name from a DB-API description entry (for query datasets)."""
    code = description[1]
    name = _FIELD_TYPES.get(int(code), "unknown") if code is not None else "unknown"
    length = description[3] if len(description) > 3 else None
    scale = description[5] if len(description) > 5 else None
    if charset == _BINARY_CHARSET:
        name = {"varchar": "varbinary", "char": "binary", "json": "json"}.get(name, name)
    if name == "decimal":
        precision = max(1, int(length or 11) - (1 if scale else 0) - 1)
        return f"decimal({precision},{int(scale or 0)})"
    if name in _TEMPORAL_WITH_FRACTION and scale:
        return f"{name}({int(scale)})"
    return name


def _message(exc: Exception) -> str:
    args: tuple[Any, ...] = tuple(getattr(exc, "args", ()))
    if len(args) >= 2 and isinstance(args[0], int):
        return f"({args[0]}) {args[1]}"
    return str(exc)
