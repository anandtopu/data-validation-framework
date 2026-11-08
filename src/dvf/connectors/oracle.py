"""Oracle Database connector (python-oracledb in thin mode; install with
``pip install 'dvf[oracle]'``).

Thin mode needs no Oracle Client libraries. Sessions use UTC (``TIME_ZONE = '+00:00'``), binary
comparison and sorting (``NLS_COMP``/``NLS_SORT = BINARY``), ISO date formats, and ``.`` as the
decimal separator. On Oracle 23ai sessions are read-only (``ALTER SESSION SET READ_ONLY``);
earlier releases have no read-only session mode, so grant the dvf user read access only.
``query_timeout_s`` sets the driver's call timeout. Time travel (``as_of``) uses Flashback Query
(``AS OF TIMESTAMP``), in UTC.

Statements pass through :func:`dvf.sql.oracle.strip_subquery_alias_as`, because Oracle rejects
``AS`` before table aliases, and unquoted (upper-case) result column names are lower-cased so
they match the names dvf's SQL uses.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from decimal import Decimal
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pyarrow as pa

from dvf.config.connections import DatabaseConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import batches_from_rows, rows_to_table
from dvf.connectors.mssql import as_of_utc
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import ConnectionReport, Connector, Relation
from dvf.sql.oracle import strip_subquery_alias_as

logger = logging.getLogger(__name__)

DEFAULT_PORT = 1521
_SCHEMES = ("oracle",)
_UNQUOTED_RESULT_NAME = re.compile(r"^[A-Z][A-Z0-9_$#]*$")

_SESSION_SQL = (
    "ALTER SESSION SET TIME_ZONE = '+00:00'",
    "ALTER SESSION SET NLS_COMP = BINARY",
    "ALTER SESSION SET NLS_SORT = BINARY",
    "ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'",
    "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'",
    "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6'",
    "ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6 TZH:TZM'",
)

_COLUMNS_SQL = """
SELECT c.column_name, c.data_type, c.data_length, c.char_length, c.char_used,
       c.data_precision, c.data_scale, c.nullable, c.data_default, c.column_id, cc.comments
FROM all_tab_columns c
LEFT JOIN all_col_comments cc
  ON cc.owner = c.owner AND cc.table_name = c.table_name AND cc.column_name = c.column_name
WHERE c.owner = :owner AND c.table_name = :name
ORDER BY c.column_id
"""

_PK_SQL = """
SELECT cc.column_name
FROM all_constraints c
JOIN all_cons_columns cc
  ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name AND cc.table_name = c.table_name
WHERE c.constraint_type = 'P' AND c.owner = :owner AND c.table_name = :name
ORDER BY cc.position
"""

_EXISTS_SQL = """
SELECT COUNT(*) FROM all_objects
WHERE owner = :owner AND object_name = :name
  AND object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW', 'SYNONYM')
"""

_LIST_SQL = """
SELECT o.owner, o.object_name, CASE WHEN o.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
       THEN 1 ELSE 0 END
FROM all_objects o
JOIN all_users u ON u.username = o.owner
WHERE o.object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW') AND u.oracle_maintained = 'N'
"""

_CHAR_TYPES = frozenset({"VARCHAR2", "CHAR"})
_NCHAR_TYPES = frozenset({"NVARCHAR2", "NCHAR"})


def _import_oracledb() -> Any:
    try:
        import oracledb
    except ImportError as exc:
        raise DependencyError("oracledb", "oracle", "The Oracle connector") from exc
    return oracledb


def parse_url(url: str) -> dict[str, Any]:
    """Settings from ``oracle://user:pass@host:1521/service_name``.

    ``oracle+oracledb://`` is accepted. ``?sid=XE`` connects by SID instead of service name;
    other query parameters are passed to ``oracledb.connect``.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0].lower()
    if scheme not in _SCHEMES:
        raise ConnectorError(
            f"Unsupported URL scheme '{parts.scheme}' for the Oracle connector.",
            hint="Use a URL like oracle://user:password@host:1521/service_name",
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ConnectorError(f"Invalid port in Oracle URL: {exc}") from None
    settings: dict[str, Any] = {"host": parts.hostname or "localhost", "port": port or DEFAULT_PORT}
    if parts.username:
        settings["user"] = unquote(parts.username)
    if parts.password is not None:
        settings["password"] = unquote(parts.password)
    service = unquote(parts.path.lstrip("/"))
    if service:
        settings["service_name"] = service
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        settings[key] = int(value) if value.isdigit() else value
    return settings


def split_name(name: str, default_owner: str | None) -> tuple[str | None, str]:
    """``owner.table`` -> catalog names (unquoted parts upper-cased, quoted parts kept)."""
    parts = re.findall(r'"((?:[^"]|"")*)"|([^."]+)', name.strip())
    names = [q.replace('""', '"') if q else p.strip().upper() for q, p in parts]
    names = [n for n in names if n]
    if len(names) == 1:
        return default_owner, names[0]
    if len(names) == 2:
        return names[0], names[1]
    raise ConnectorError(f"Cannot parse Oracle table name '{name}'; use 'table' or 'owner.table'.")


def result_name(name: str) -> str:
    """Lower-case unquoted (upper-case) result names, so ``AS k0`` reads back as ``k0``."""
    return name.lower() if _UNQUOTED_RESULT_NAME.match(name) else name


def native_type_name(
    data_type: str,
    data_length: Any,
    char_length: Any,
    char_used: Any,
    precision: Any,
    scale: Any,
) -> str:
    """An Oracle type string (``VARCHAR2(100 CHAR)``, ``NUMBER(12,3)``) from the catalog."""
    name = data_type.upper()
    if name in _CHAR_TYPES:
        if char_used == "C" and char_length:
            return f"{name}({int(char_length)} CHAR)"
        return f"{name}({int(data_length)})"
    if name in _NCHAR_TYPES:
        return f"{name}({int(char_length or data_length)})"
    if name == "NUMBER":
        if precision is None and scale is None:
            return "NUMBER"
        if precision is None:
            return f"NUMBER(38,{int(scale)})"
        return f"NUMBER({int(precision)},{int(scale or 0)})"
    if name == "FLOAT" and precision is not None:
        return f"FLOAT({int(precision)})"
    if name == "RAW":
        return f"RAW({int(data_length)})"
    return name


def native_type_from_description(description: Any) -> str:
    """An Oracle type string from a DB-API description entry (for query datasets)."""
    type_code = description[1]
    code = str(getattr(type_code, "name", type_code)).upper().removeprefix("DB_TYPE_")
    size = description[3] if len(description) > 3 else None
    precision = description[4] if len(description) > 4 else None
    scale = description[5] if len(description) > 5 else None
    if code == "NUMBER":
        if precision in (None, 0) and scale in (None, -127):
            return "NUMBER"
        return f"NUMBER({int(precision or 38)},{int(scale or 0)})"
    if code in ("VARCHAR", "CHAR", "NVARCHAR", "NCHAR"):
        name = {"VARCHAR": "VARCHAR2", "NVARCHAR": "NVARCHAR2"}.get(code, code)
        return f"{name}({int(size or 4000)})"
    if code in ("TIMESTAMP", "TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
        suffix = {"TIMESTAMP_TZ": " WITH TIME ZONE", "TIMESTAMP_LTZ": " WITH LOCAL TIME ZONE"}
        return f"TIMESTAMP({int(scale if scale is not None else 6)}){suffix.get(code, '')}"
    if code == "RAW":
        return f"RAW({int(size or 2000)})"
    if code == "INTERVAL_DS":
        return "INTERVAL DAY TO SECOND"
    return code.replace("_", " ")


class OracleConnector(Connector):
    """Oracle Database 12.2+ through python-oracledb (thin mode)."""

    dialect = "oracle"
    Config = DatabaseConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: Any = None
        self._schema: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def connect_args(self) -> dict[str, Any]:
        """Keyword arguments for ``oracledb.connect`` (the password included; never log them)."""
        config = self.config
        assert isinstance(config, DatabaseConnection)
        settings: dict[str, Any] = {}
        if config.url is not None:
            settings.update(parse_url(config.url.get_secret_value()))
        else:
            settings.update(host=config.host, port=config.port or DEFAULT_PORT)
        if config.database is not None:
            settings["service_name"] = config.database
        if config.user is not None:
            settings["user"] = config.user
        if config.password is not None:
            settings["password"] = config.password.get_secret_value()
        settings.setdefault("tcp_connect_timeout", 15)
        settings.update(config.options)
        if "sid" in settings:
            settings.pop("service_name", None)
        return {k: v for k, v in settings.items() if v is not None}

    def open(self) -> None:
        if self._con is not None:
            return
        oracledb = _import_oracledb()
        kwargs = self.connect_args()
        secret = kwargs.get("password")
        try:
            con = oracledb.connect(**kwargs)
        except oracledb.Error as exc:
            raise ConnectorError(
                f"Could not connect to Oracle for connection '{self.name}' "
                f"({kwargs.get('host')}:{kwargs.get('port')}/"
                f"{kwargs.get('service_name') or kwargs.get('sid')}): {_message(exc, secret)}",
                hint="Check host, port, service name, and credentials; run 'dvf connections test'.",
            ) from None
        con.outputtypehandler = _output_type_handler
        try:
            with con.cursor() as cur:
                for statement in _SESSION_SQL:
                    cur.execute(statement)
                assert isinstance(self.config, DatabaseConnection)
                if self.config.schema_:
                    cur.execute(
                        f'ALTER SESSION SET CURRENT_SCHEMA = "{self.config.schema_.upper()}"'
                        if self.config.schema_.isidentifier()
                        else f"ALTER SESSION SET CURRENT_SCHEMA = {self.config.schema_}"
                    )
                if self.config.read_only:
                    try:
                        cur.execute("ALTER SESSION SET READ_ONLY = TRUE")
                    except oracledb.DatabaseError:
                        logger.debug("Oracle before 23ai has no read-only sessions")
                cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
                self._schema = str(cur.fetchone()[0])
            if self.config.query_timeout_s:
                con.call_timeout = int(self.config.query_timeout_s) * 1000
        except oracledb.Error as exc:
            con.close()
            raise ConnectorError(
                f"Could not set up the Oracle session for connection '{self.name}': "
                f"{_message(exc, secret)}"
            ) from None
        self._con = con

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                logger.debug("closing Oracle connection '%s' failed", self.name, exc_info=True)
            self._con = None

    @property
    def connection(self) -> Any:
        self.open()
        return self._con

    def server_version(self) -> str | None:
        return f"Oracle {self.connection.version}"

    def test_connection(self) -> ConnectionReport:
        # The base class runs "SELECT 1 AS ok", which needs FROM DUAL before 23ai.
        import time

        started = time.perf_counter()
        try:
            self.open()
            self.query("SELECT 1 AS ok FROM DUAL")
            version = self.server_version()
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            return ConnectionReport(False, f"{type(exc).__name__}: {exc}", None, elapsed)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        return ConnectionReport(True, "connected", version, elapsed)

    # ------------------------------------------------------------------ queries
    def _error(self, exc: Exception) -> ConnectorError:
        return ConnectorError(f"Query failed on connection '{self.name}': {_message(exc)}")

    def _rows(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        oracledb = _import_oracledb()
        try:
            with self.connection.cursor() as cur:
                cur.execute(strip_subquery_alias_as(sql), params or {})
                if not cur.description:
                    return [], []
                names = [result_name(d[0]) for d in cur.description]
                return names, list(cur.fetchall())
        except oracledb.Error as exc:
            raise self._error(exc) from None

    def query(self, sql: str) -> pa.Table:
        names, rows = self._rows(sql)
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        oracledb = _import_oracledb()
        cur = None
        try:
            cur = self.connection.cursor()
            cur.arraysize = min(batch_size, 10_000)
            cur.prefetchrows = cur.arraysize + 1
            cur.execute(strip_subquery_alias_as(sql))
            names = [result_name(d[0]) for d in cur.description or ()]
            empty = True
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                empty = False
                yield from batches_from_rows(names, rows, batch_size)
            if empty:
                yield from rows_to_table(names, []).to_batches()
        except oracledb.Error as exc:
            raise self._error(exc) from None
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    logger.debug("closing an Oracle cursor failed", exc_info=True)

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is an Oracle database; use 'table' or 'query', "
                "not 'path'."
            )
        table: str | None = None
        if spec.table is not None:
            base, table, kind, label = spec.table, spec.table, DatasetKind.TABLE, spec.table
            if spec.as_of is not None:
                instant = as_of_utc(spec.as_of).replace("T", " ")
                base = f"{spec.table} AS OF TIMESTAMP TIMESTAMP '{instant} +00:00'"
        else:
            assert spec.query is not None
            if spec.as_of is not None:
                raise ConnectorError(
                    f"Connection '{self.name}': 'as_of' applies to tables, not queries.",
                    hint="Write AS OF TIMESTAMP into the query instead.",
                )
            base, kind, label = f'(\n{spec.query}\n) "dvf_query"', DatasetKind.QUERY, "query"
        ref = DatasetRef(
            connection=self.name,
            kind=kind,
            name=label,
            query=spec.query if kind is DatasetKind.QUERY else None,
        )
        sql = (
            base
            if not spec.filter
            else f'(SELECT * FROM {base} WHERE {spec.filter}) "dvf_filtered"'
        )
        return Relation(ref, sql, base, table, spec.filter, self.name)

    def _catalog_name(self, table: str) -> dict[str, Any]:
        self.open()
        owner, name = split_name(table, self._schema)
        return {"owner": owner, "name": name}

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        _, rows = self._rows(_EXISTS_SQL, self._catalog_name(relation.table))
        if rows and rows[0][0]:
            return True
        return super().exists(relation)  # public synonyms and other indirections

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            _, rows = self._rows(_COLUMNS_SQL, self._catalog_name(relation.table))
            if rows:
                columns = tuple(
                    ColumnSchema(
                        name=str(r[0]),
                        native_type=(
                            native := native_type_name(r[1], r[2], r[3], r[4], r[5], r[6])
                        ),
                        type=parse_native_type(native, "oracle"),
                        nullable=str(r[7]).upper() == "Y",
                        default=None if r[8] is None else str(r[8]).strip(),
                        ordinal=int(r[9]),
                        comment=None if r[10] is None else str(r[10]),
                    )
                    for r in rows
                )
                return DatasetSchema(
                    ref=relation.ref, columns=columns, primary_key=self.get_primary_key(relation)
                )
            if not super().exists(relation):
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'.",
                    hint="Check the name (owner.table; unquoted names are upper-case in Oracle) "
                    "and that the user can read it.",
                )
        return DatasetSchema(ref=relation.ref, columns=self._describe(relation))

    def _describe(self, relation: Relation) -> tuple[ColumnSchema, ...]:
        oracledb = _import_oracledb()
        try:
            with self.connection.cursor() as cur:
                cur.execute(
                    strip_subquery_alias_as(f"SELECT * FROM {relation.base_sql} WHERE 1 = 0")
                )
                description = list(cur.description or ())
        except oracledb.Error as exc:
            raise self._error(exc) from None
        return tuple(
            ColumnSchema(
                name=str(d[0]),
                native_type=(native := native_type_from_description(d)),
                type=parse_native_type(native, "oracle"),
                nullable=bool(d[6]) if len(d) > 6 and d[6] is not None else True,
                ordinal=i + 1,
            )
            for i, d in enumerate(description)
        )

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        _, rows = self._rows(_PK_SQL, self._catalog_name(relation.table))
        return tuple(str(r[0]) for r in rows)

    def list_datasets(self, pattern: str) -> list[str]:
        _, rows = self._rows(_LIST_SQL)
        names: list[str] = []
        for owner, table, current in rows:
            if "." in pattern:
                target = f"{owner}.{table}"
            elif current:
                target = str(table)
            else:
                continue
            if fnmatchcase(target.lower(), pattern.lower()):
                names.append(target)
        return sorted(set(names))


def _output_type_handler(cursor: Any, metadata: Any) -> Any:
    """Fetch NUMBER as Decimal (except plain integers) and LOBs as str/bytes."""
    import oracledb

    type_code = metadata.type_code
    if type_code is oracledb.DB_TYPE_NUMBER:
        if metadata.scale == 0 and metadata.precision and metadata.precision <= 18:
            return None
        return cursor.var(Decimal, arraysize=cursor.arraysize)
    if type_code in (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB):
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if type_code is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None


def _message(exc: Exception, secret: str | None = None) -> str:
    text = " ".join(str(exc).split())
    if secret:
        text = text.replace(secret, "***")
    return text
