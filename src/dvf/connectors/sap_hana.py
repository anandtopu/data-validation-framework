"""SAP HANA connector (beta): database-level access through SAP's ``hdbcli`` driver.

Install ``hdbcli`` yourself (dvf does not bundle SAP drivers). Database access is the preferred
way to validate SAP data: counts, control totals, and hashes run inside HANA, and only differing
rows move.

* **Client filtering.** With ``client`` set, every client-dependent table is filtered to that
  client. The client column is the dictionary's client-type key field (with ``ddic``), the
  configured ``client_column``, or a first column named MANDT, CLIENT, RCLNT, or MANDANT. Query
  datasets are not filtered (write the condition yourself), and ``options: {client_filter:
  false}`` turns the filter off for one dataset.
* **Schemas** come from the HANA catalog (``SYS.TABLE_COLUMNS``, ``SYS.VIEW_COLUMNS``), primary
  keys from ``SYS.CONSTRAINTS``. With ``ddic: true`` the ABAP dictionary adds ABAP types and
  lengths (shown as native types), field texts, and dictionary keys.
* dvf only runs ``SELECT`` statements; use a database user with read-only privileges.

This connector is unit-tested with a fake ``hdbcli`` module and has not yet been verified against
a live HANA system.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pyarrow as pa

from dvf.config.connections import SapHanaConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import batches_from_rows, rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, LogicalType
from dvf.packs.sap.ddic import CLIENT_COLUMN_NAMES, DdicCache, DdicTable, read_table
from dvf.packs.sap.drivers import HDBCLI_HINT, import_driver
from dvf.plugins.base import ConnectionReport, Connector, Relation

_TRANSIENT_CODES = frozenset({-10709, -10807, -10108, 133})
"""hdbcli error codes worth a retry: connection failed or lost, session reconnected, deadlock."""

_COLUMNS_SQL = (
    "SELECT COLUMN_NAME, POSITION, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE, DEFAULT_VALUE, "
    "COMMENTS FROM SYS.{view} WHERE SCHEMA_NAME = {schema} AND {name_column} = ? ORDER BY POSITION"
)
_PK_SQL = (
    "SELECT COLUMN_NAME FROM SYS.CONSTRAINTS WHERE SCHEMA_NAME = {schema} AND TABLE_NAME = ? "
    "AND IS_PRIMARY_KEY = 'TRUE' ORDER BY POSITION"
)
_LIST_SQL = (
    "SELECT TABLE_NAME FROM SYS.TABLES WHERE SCHEMA_NAME = {schema} "
    "UNION ALL SELECT VIEW_NAME FROM SYS.VIEWS WHERE SCHEMA_NAME = {schema}"
)

# hdbcli type codes (cursor.description) by name, for query datasets.
_TYPE_CODES = {
    1: "TINYINT",
    2: "SMALLINT",
    3: "INTEGER",
    4: "BIGINT",
    5: "DECIMAL",
    6: "REAL",
    7: "DOUBLE",
    8: "CHAR",
    9: "VARCHAR",
    10: "NCHAR",
    11: "NVARCHAR",
    12: "BINARY",
    13: "VARBINARY",
    14: "DATE",
    15: "TIME",
    16: "TIMESTAMP",
    25: "CLOB",
    26: "NCLOB",
    27: "BLOB",
    28: "BOOLEAN",
    29: "VARCHAR",
    30: "NVARCHAR",
    47: "SMALLDECIMAL",
    51: "TEXT",
    52: "SHORTTEXT",
    55: "ALPHANUM",
    61: "TIMESTAMP",
    62: "SECONDDATE",
    63: "DATE",
    64: "TIME",
}


def hana_type(name: str, length: int | None = None, scale: int | None = None) -> CanonicalType:
    """The canonical type of a HANA column type (``SYS.TABLE_COLUMNS.DATA_TYPE_NAME``)."""
    code = name.strip().upper()
    if code == "TINYINT":
        return CanonicalType.integer(8, unsigned=True)
    if code == "SMALLINT":
        return CanonicalType.integer(16)
    if code == "INTEGER":
        return CanonicalType.integer(32)
    if code == "BIGINT":
        return CanonicalType.integer(64)
    if code == "DECIMAL":
        if length is None or scale is None:
            return CanonicalType.decimal(34, None)  # floating-point decimal
        return CanonicalType.decimal(length, scale)
    if code == "SMALLDECIMAL":
        return CanonicalType.decimal(16, None)
    if code == "REAL":
        return CanonicalType.float(32)
    if code in ("DOUBLE", "FLOAT"):
        return CanonicalType.float(64)
    if code in ("VARCHAR", "NVARCHAR", "ALPHANUM", "SHORTTEXT"):
        return CanonicalType.string(length or None, unicode=code != "VARCHAR")
    if code in ("CHAR", "NCHAR"):
        return CanonicalType.string(length or 1, fixed=True, unicode=code == "NCHAR")
    if code in ("CLOB", "NCLOB", "TEXT", "BINTEXT", "STRING", "NSTRING"):
        return CanonicalType.string()
    if code == "VARBINARY":
        return CanonicalType.binary(length or None)
    if code == "BINARY":
        return CanonicalType.binary(length or None, fixed=True)
    if code == "BLOB":
        return CanonicalType.binary()
    if code == "DATE":
        return CanonicalType.date()
    if code == "TIME":
        return CanonicalType.time(0)
    if code == "SECONDDATE":
        return CanonicalType.timestamp(0)
    if code == "TIMESTAMP":
        return CanonicalType.timestamp(7)
    if code == "BOOLEAN":
        return CanonicalType.boolean()
    if code.startswith("ST_"):
        return CanonicalType.simple(LogicalType.SPATIAL)
    return CanonicalType.unknown()


def hana_native(name: str, length: int | None, scale: int | None) -> str:
    code = name.strip().upper()
    if code == "DECIMAL" and length is not None and scale is not None:
        return f"DECIMAL({length},{scale})"
    if length and code in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "ALPHANUM", "SHORTTEXT"):
        return f"{code}({length})"
    if length and code in ("VARBINARY", "BINARY"):
        return f"{code}({length})"
    return code


def split_name(text: str) -> tuple[str | None, str]:
    """Split ``SCHEMA.TABLE`` into its parts, HANA style: unquoted parts are upper-cased, quoted
    parts (``"/BIC/AZSALES00"``) keep their case. A name without a dot has no schema."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    was_quoted = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if quoted and i + 1 < len(text) and text[i + 1] == '"':
                current.append('"')
                i += 2
                continue
            quoted = not quoted
            was_quoted = True
        elif ch == "." and not quoted:
            parts.append("".join(current) if was_quoted else "".join(current).strip().upper())
            current, was_quoted = [], False
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current) if was_quoted else "".join(current).strip().upper())
    if quoted or len(parts) > 2 or not all(parts):
        raise ConnectorError(f"Cannot read the table name {text!r}; use TABLE or SCHEMA.TABLE.")
    if len(parts) == 1:
        return None, parts[0]
    return parts[0], parts[1]


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True)
class CatalogColumn:
    name: str
    position: int
    type_name: str
    length: int | None
    scale: int | None
    nullable: bool
    default: str | None
    comment: str | None


class _HanaDdicSource:
    """Reads dictionary tables with SQL (see :class:`dvf.packs.sap.ddic.DdicSource`)."""

    def __init__(self, connector: SapHanaConnector, schema: str | None) -> None:
        self.connector = connector
        self.schema = schema

    def select(self, table: str, fields: Sequence[str], where: str) -> list[dict[str, Any]]:
        target = f"{quote(self.schema)}.{quote(table)}" if self.schema else quote(table)
        columns = ", ".join(quote(f) for f in fields)
        names, rows = self.connector.execute(f"SELECT {columns} FROM {target} WHERE {where}")
        return [dict(zip(names, row, strict=True)) for row in rows]


class SapHanaConnector(Connector):
    """SAP HANA (HANA 2.0 and HANA Cloud) through ``hdbcli``."""

    dialect = "hana"
    Config = SapHanaConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: Any = None
        self._lock = threading.Lock()
        self._columns: dict[tuple[str | None, str], list[CatalogColumn]] = {}
        self._ddic = DdicCache()

    @property
    def cfg(self) -> SapHanaConnection:
        assert isinstance(self.config, SapHanaConnection)
        return self.config

    # ------------------------------------------------------------------ lifecycle
    def _dbapi(self) -> Any:
        return import_driver("hdbcli.dbapi", "hdbcli", "The SAP HANA connector", HDBCLI_HINT)

    def connect_args(self) -> dict[str, Any]:
        cfg = self.cfg
        kwargs: dict[str, Any] = {
            "address": cfg.host,
            "port": cfg.port,
            "encrypt": cfg.encrypt,
            "sslValidateCertificate": cfg.ssl_validate_certificate,
        }
        if cfg.user is not None:
            kwargs["user"] = cfg.user
        if cfg.password is not None:
            kwargs["password"] = cfg.password.get_secret_value()
        if cfg.database_name:
            kwargs["databaseName"] = cfg.database_name
        if cfg.schema_:
            kwargs["currentSchema"] = cfg.schema_
        kwargs.update(cfg.options)
        return kwargs

    def open(self) -> None:
        if self._con is not None:
            return
        dbapi = self._dbapi()
        try:
            self._con = dbapi.connect(**self.connect_args())
        except dbapi.Error as exc:
            raise ConnectorError(
                f"Could not connect to SAP HANA for connection '{self.name}': {exc}",
                hint="Check host, port (3<instance>15 or a tenant port), database_name, and "
                "credentials; run 'dvf connections test'.",
            ) from exc

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

    def is_transient(self, exc: BaseException) -> bool:
        current: BaseException | None = exc
        while current is not None:
            if getattr(current, "errorcode", None) in _TRANSIENT_CODES:
                return True
            current = current.__cause__
        return super().is_transient(exc)

    def server_version(self) -> str | None:
        _, rows = self.execute("SELECT VERSION FROM SYS.M_DATABASE")
        return f"SAP HANA {rows[0][0]}" if rows else None

    def test_connection(self) -> ConnectionReport:
        import time

        started = time.perf_counter()
        try:
            self.open()
            self.execute("SELECT 1 AS OK FROM DUMMY")
            version = self.server_version()
        except Exception as exc:
            return ConnectionReport(False, f"{type(exc).__name__}: {exc}")
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        return ConnectionReport(True, "connected", version, elapsed)

    # ------------------------------------------------------------------ queries
    def _cursor(self) -> Any:
        cursor = self.connection.cursor()
        timeout = self.cfg.query_timeout_s
        if timeout and hasattr(cursor, "setquerytimeout"):
            cursor.setquerytimeout(int(timeout))
        return cursor

    def execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run ``sql`` and return column names and all rows."""
        dbapi = self._dbapi()
        cursor = self._cursor()
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, tuple(params))
            names = [d[0] for d in cursor.description or ()]
            rows = [_plain_row(r) for r in cursor.fetchall()] if cursor.description else []
            return names, rows
        except dbapi.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        finally:
            cursor.close()

    def query(self, sql: str) -> pa.Table:
        names, rows = self.execute(sql)
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        dbapi = self._dbapi()
        cursor = self._cursor()
        try:
            cursor.execute(sql)
            names = [d[0] for d in cursor.description or ()]
            emitted = False
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                emitted = True
                yield from batches_from_rows(names, [_plain_row(r) for r in rows], batch_size)
            if not emitted:
                yield from rows_to_table(names, []).to_batches()
        except dbapi.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        finally:
            cursor.close()

    # ------------------------------------------------------------------ catalog
    def _schema_sql(self, schema: str | None) -> tuple[str, list[Any]]:
        if schema is None:
            return "CURRENT_SCHEMA", []
        return "?", [schema]

    def catalog_columns(self, schema: str | None, table: str) -> list[CatalogColumn]:
        key = (schema, table)
        with self._lock:
            cached = self._columns.get(key)
        if cached is not None:
            return cached
        found: list[CatalogColumn] = []
        for view, name_column in (("TABLE_COLUMNS", "TABLE_NAME"), ("VIEW_COLUMNS", "VIEW_NAME")):
            schema_sql, params = self._schema_sql(schema)
            sql = _COLUMNS_SQL.format(view=view, schema=schema_sql, name_column=name_column)
            _, rows = self.execute(sql, [*params, table])
            if rows:
                found = [
                    CatalogColumn(
                        name=str(r[0]),
                        position=int(r[1]),
                        type_name=str(r[2]),
                        length=None if r[3] is None else int(r[3]),
                        scale=None if r[4] is None else int(r[4]),
                        nullable=str(r[5]).upper() == "TRUE",
                        default=None if r[6] is None else str(r[6]),
                        comment=None if r[7] is None else str(r[7]),
                    )
                    for r in rows
                ]
                break
        with self._lock:
            self._columns[key] = found
        return found

    def ddic_table(self, table: str) -> DdicTable | None:
        """The dictionary definition of ``table`` (cached), or ``None`` if it has none."""
        if self._ddic.has(table):
            return self._ddic.get(table)
        source = _HanaDdicSource(self, self.cfg.ddic_schema or self.cfg.schema_)
        found = read_table(source, table, language=self.cfg.ddic_language)
        self._ddic.put(table, found)
        return found

    def client_column(self, schema: str | None, table: str) -> str | None:
        """The client column of a client-dependent table, or ``None``."""
        columns = self.catalog_columns(schema, table)
        names = {c.name.upper(): c.name for c in columns}
        configured = self.cfg.client_column
        if configured:
            return names.get(configured.upper())
        if self.cfg.ddic:
            ddic = self.ddic_table(table)
            if ddic is not None:
                column = ddic.client_column
                return names.get(column.upper()) if column else None
        if columns:
            first = min(columns, key=lambda c: c.position)
            text_type = first.type_name.upper() in ("NVARCHAR", "VARCHAR", "CHAR", "NCHAR")
            if first.name.upper() in CLIENT_COLUMN_NAMES and text_type and first.length == 3:
                return first.name
        return None

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.as_of is not None:
            raise ConnectorError(f"Connection '{self.name}' (sap_hana) does not support 'as_of'.")
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is an SAP HANA database; use 'table' or 'query'."
            )
        table: str | None = None
        if spec.table is not None:
            schema, name = split_name(spec.table)
            schema = schema or self.cfg.schema_
            table = f"{quote(schema)}.{quote(name)}" if schema else quote(name)
            base, kind, label = table, DatasetKind.TABLE, spec.table
            client = self.cfg.client
            if client and spec.options.get("client_filter", True):
                column = self.client_column(schema, name)
                if column is not None:
                    base = (
                        f"(SELECT * FROM {table} WHERE {quote(column)} = '{client}') "
                        f'AS "dvf_client"'
                    )
        else:
            assert spec.query is not None
            base, kind, label = f'(\n{spec.query}\n) AS "dvf_query"', DatasetKind.QUERY, "query"
        ref = DatasetRef(
            connection=self.name,
            kind=kind,
            name=label,
            query=spec.query if kind is DatasetKind.QUERY else None,
        )
        sql = (
            base
            if not spec.filter
            else f'(SELECT * FROM {base} WHERE {spec.filter}) AS "dvf_filtered"'
        )
        return Relation(ref, sql, base, table, spec.filter, self.name)

    def _table_parts(self, relation: Relation) -> tuple[str | None, str]:
        assert relation.table is not None
        schema, name = split_name(relation.table)
        return schema or self.cfg.schema_, name

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        return bool(self.catalog_columns(*self._table_parts(relation)))

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is None:
            return self._query_schema(relation)
        schema, name = self._table_parts(relation)
        columns = self.catalog_columns(schema, name)
        if not columns:
            raise ConnectorError(
                f"Table '{relation.ref.name}' not found on connection '{self.name}'."
            )
        ddic = self.ddic_table(name) if self.cfg.ddic else None
        out: list[ColumnSchema] = []
        for c in columns:
            native = hana_native(c.type_name, c.length, c.scale)
            comment = c.comment
            ddic_field = ddic.field(c.name) if ddic is not None else None
            if ddic_field is not None:
                native = ddic_field.native_type
                comment = ddic_field.description or comment
            out.append(
                ColumnSchema(
                    name=c.name,
                    native_type=native,
                    type=hana_type(c.type_name, c.length, c.scale),
                    nullable=c.nullable,
                    default=c.default,
                    ordinal=c.position,
                    comment=comment,
                )
            )
        key = self._primary_key(schema, name)
        if not key and ddic is not None:
            key = ddic.primary_key
        return DatasetSchema(ref=relation.ref, columns=tuple(out), primary_key=key)

    def _primary_key(self, schema: str | None, name: str) -> tuple[str, ...]:
        schema_sql, params = self._schema_sql(schema)
        _, rows = self.execute(_PK_SQL.format(schema=schema_sql), [*params, name])
        return tuple(str(r[0]) for r in rows)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        schema, name = self._table_parts(relation)
        key = self._primary_key(schema, name)
        if not key and self.cfg.ddic:
            ddic = self.ddic_table(name)
            key = ddic.primary_key if ddic is not None else ()
        return key

    def _query_schema(self, relation: Relation) -> DatasetSchema:
        dbapi = self._dbapi()
        cursor = self._cursor()
        try:
            cursor.execute(f"SELECT * FROM {relation.base_sql} LIMIT 0")
            description = list(cursor.description or ())
        except dbapi.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        finally:
            cursor.close()
        columns = []
        for i, d in enumerate(description):
            type_name = _TYPE_CODES.get(d[1], "UNKNOWN") if isinstance(d[1], int) else str(d[1])
            size = d[3] if len(d) > 3 else None
            precision = d[4] if len(d) > 4 else None
            scale = d[5] if len(d) > 5 else None
            length = precision if type_name == "DECIMAL" else size
            columns.append(
                ColumnSchema(
                    name=str(d[0]),
                    native_type=hana_native(type_name, length, scale),
                    type=hana_type(type_name, length, scale),
                    nullable=None if len(d) < 7 else bool(d[6]),
                    ordinal=i + 1,
                )
            )
        return DatasetSchema(ref=relation.ref, columns=tuple(columns))

    def list_datasets(self, pattern: str) -> list[str]:
        schema: str | None
        if "." in pattern:
            schema_part, table_pattern = pattern.split(".", 1)
            schema = schema_part.strip('"')
        else:
            schema, table_pattern = self.cfg.schema_, pattern
        schema_sql, params = self._schema_sql(schema)
        _, rows = self.execute(_LIST_SQL.format(schema=schema_sql), [*params, *params])
        names = sorted({str(r[0]) for r in rows})
        matched = [n for n in names if fnmatchcase(n.upper(), table_pattern.upper())]
        if "." in pattern:
            return [f"{schema}.{n}" for n in matched]
        return matched


def _plain_row(row: Sequence[Any]) -> tuple[Any, ...]:
    """hdbcli rows with LOB values read into bytes or text."""
    out = []
    for value in row:
        if hasattr(value, "read") and not isinstance(value, bytes | str):
            value = value.read()
        out.append(value)
    return tuple(out)


__all__ = ["SapHanaConnector", "hana_type", "split_name"]
