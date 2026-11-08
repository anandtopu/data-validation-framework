"""Amazon Redshift connector (beta; install with ``pip install 'dvf[redshift]'``).

Implemented from the redshift_connector and Amazon Redshift documentation and tested against a
fake driver; it has not been verified against a live cluster or workgroup.

The connection uses the ``database`` connection model: ``host`` (default port 5439),
``database``, ``user``, and ``password``, or a ``url`` such as
``redshift://user:password@host:5439/dev``. IAM authentication and other driver settings go in
``options`` (for example ``{iam: true, cluster_identifier: ..., db_user: ..., region: ...}``)
and are passed to ``redshift_connector.connect``. The dvf-specific option ``dvf_fetch_size``
(default 1000, the most a single-node cluster accepts) sets how many rows each server-side
cursor ``FETCH`` returns when streaming; raise it on multi-node clusters.

Sessions run in autocommit mode with ``timezone`` UTC, ``search_path`` from ``schema``, and
``statement_timeout`` from ``query_timeout_s``. Redshift has no read-only session setting, so use
a user with ``USAGE`` on the schemas and ``SELECT`` on the compared tables only.

Large results stream through a server-side cursor (``DECLARE ... CURSOR``) because the driver
otherwise buffers whole result sets in memory; Redshift materializes cursor results on the leader
node. Redshift has no time travel, so ``as_of`` is rejected.
"""

from __future__ import annotations

import importlib
import re
import secrets
from collections.abc import Iterator, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pyarrow as pa
from sqlglot import exp

from dvf.config.connections import DatabaseConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import Connector, Relation

DEFAULT_PORT = 5439
DEFAULT_FETCH_SIZE = 1000
FETCH_SIZE_OPTION = "dvf_fetch_size"
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_internal", "pg_automv")
_STRING_TYPES = frozenset(
    {"character varying", "varchar", "character", "char", "bpchar", "nchar", "nvarchar"}
)
_BINARY_TYPES = frozenset({"binary varying", "varbyte", "varbinary"})

_COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default, ordinal_position,
       character_maximum_length, numeric_precision, numeric_scale, remarks
FROM svv_columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position
"""

_PK_SQL = """
SELECT kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name
 AND kcu.table_schema = tc.table_schema
 AND kcu.table_name = tc.table_name
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s AND tc.table_name = %s
ORDER BY kcu.ordinal_position
"""


def _import_redshift() -> Any:
    try:
        return importlib.import_module("redshift_connector")
    except ImportError as exc:
        raise DependencyError(
            "redshift-connector", "redshift", "The Amazon Redshift connector"
        ) from exc


def _error_type(module: Any) -> type[BaseException]:
    error = getattr(module, "Error", None)
    return error if isinstance(error, type) and issubclass(error, BaseException) else Exception


def _hint(exc: BaseException) -> str:
    text = str(exc).lower()
    if "password" in text or "authentication" in text or "iam" in text:
        return (
            "Authentication failed: check user and password, or the IAM settings in 'options' "
            "(iam, cluster_identifier or serverless_work_group, db_user, region)."
        )
    if "permission denied" in text:
        return "Grant USAGE on the schema and SELECT on the table to the dvf user."
    if "does not exist" in text:
        return "Check the schema and table names (Redshift folds unquoted names to lower case)."
    if "timeout" in text or "timed out" in text or "connect" in text:
        return (
            "Check host, port (default 5439), and network access: security groups and "
            "public accessibility of the cluster or workgroup."
        )
    return "Check host, port, database, and credentials; run 'dvf connections test'."


def _option_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    return int(value) if value.isdigit() else value


def native_type(
    data_type: str, length: int | None, precision: int | None, scale: int | None
) -> str:
    """A Redshift type string from ``svv_columns`` fields, such as ``numeric(18,2)``."""
    base = data_type.strip().lower()
    if base in _STRING_TYPES and length:
        return f"{base}({length})"
    if base in _BINARY_TYPES and length:
        return f"{base}({length})"
    if base in ("numeric", "decimal") and precision is not None:
        return f"{base}({precision},{scale or 0})"
    return base


class RedshiftConnector(Connector):
    """Amazon Redshift (provisioned clusters and Serverless), through redshift_connector."""

    dialect = "redshift"
    Config = DatabaseConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: Any = None

    @property
    def _config(self) -> DatabaseConnection:
        config = self.config
        assert isinstance(config, DatabaseConnection)
        return config

    def _fetch_size(self) -> int:
        value = self._config.options.get(FETCH_SIZE_OPTION, DEFAULT_FETCH_SIZE)
        return max(1, int(value))

    def _connect_args(self) -> dict[str, Any]:
        """Keyword arguments for ``redshift_connector.connect`` (contains secrets)."""
        c = self._config
        kwargs: dict[str, Any] = {}
        if c.url is not None:
            parts = urlsplit(c.url.get_secret_value())
            if not parts.scheme.lower().startswith(("redshift", "postgres")):
                raise ConnectorError(
                    f"Connection '{self.name}' has a '{parts.scheme}' URL, not a Redshift URL.",
                    hint="Use redshift://user:password@host:5439/database.",
                )
            kwargs.update(
                host=parts.hostname,
                port=parts.port or DEFAULT_PORT,
                database=unquote(parts.path.lstrip("/")) or None,
                user=unquote(parts.username) if parts.username else None,
                password=unquote(parts.password) if parts.password else None,
            )
            kwargs.update({k: _option_value(v) for k, v in parse_qsl(parts.query)})
        else:
            kwargs.update(host=c.host, port=c.port or DEFAULT_PORT, database=c.database)
        if c.user is not None:
            kwargs["user"] = c.user
        if c.password is not None:
            kwargs["password"] = c.password.get_secret_value()
        kwargs.setdefault("application_name", "dvf")
        kwargs.update({k: v for k, v in c.options.items() if k != FETCH_SIZE_OPTION})
        return {k: v for k, v in kwargs.items() if v is not None}

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self._con is not None:
            return
        rs = _import_redshift()
        kwargs = self._connect_args()
        try:
            con = rs.connect(**kwargs)
        except (_error_type(rs), OSError) as exc:
            raise ConnectorError(
                f"Could not connect to Amazon Redshift for connection '{self.name}': {exc}",
                hint=_hint(exc),
            ) from exc
        con.autocommit = True
        self._con = con
        c = self._config
        try:
            self._execute("SET timezone TO 'UTC'")
            if c.schema_:
                self._execute(f"SET search_path TO {_quote(c.schema_)}, public")
            if c.query_timeout_s:
                self._execute(f"SET statement_timeout TO {int(c.query_timeout_s) * 1000}")
        except ConnectorError:
            self.close()
            raise

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
        rows = self._rows("SELECT version()")
        if not rows:
            return None
        text = str(rows[0][0])
        match = re.search(r"Redshift\s+([\w.]+)", text)
        return f"Amazon Redshift {match.group(1)}" if match else text

    # ------------------------------------------------------------------ queries
    def _fail(self, exc: BaseException) -> ConnectorError:
        return ConnectorError(f"Query failed on connection '{self.name}': {exc}", hint=_hint(exc))

    def _execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        rs = _import_redshift()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, params)
            names = [_name(d[0]) for d in cursor.description or ()]
            rows = [tuple(r) for r in cursor.fetchall()] if cursor.description else []
        except _error_type(rs) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()
        return names, rows

    def _rows(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        return self._execute(sql, params)[1]

    def query(self, sql: str) -> pa.Table:
        names, rows = self._execute(sql)
        return rows_to_table(names, rows)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        """Stream through a server-side cursor inside a transaction (the session autocommits)."""
        rs = _import_redshift()
        cursor = self.connection.cursor()
        name = f"dvf_cursor_{secrets.token_hex(4)}"
        fetch = min(self._fetch_size(), max(1, batch_size))
        finished = False
        try:
            cursor.execute("BEGIN")
            cursor.execute(f"DECLARE {name} CURSOR FOR {sql}")
            names: list[str] = []
            buffer: list[tuple[Any, ...]] = []
            while True:
                cursor.execute(f"FETCH FORWARD {fetch} FROM {name}")
                names = names or [_name(d[0]) for d in cursor.description or ()]
                rows = [tuple(r) for r in cursor.fetchall()]
                buffer.extend(rows)
                if len(buffer) >= batch_size:
                    yield from rows_to_table(names, buffer).to_batches()
                    buffer = []
                if len(rows) < fetch:
                    break
            if buffer:
                yield from rows_to_table(names, buffer).to_batches()
            cursor.execute(f"CLOSE {name}")
            cursor.execute("COMMIT")
            finished = True
        except _error_type(rs) as exc:
            raise self._fail(exc) from exc
        finally:
            if not finished:
                try:
                    cursor.execute("ROLLBACK")
                except _error_type(rs):
                    pass
            cursor.close()

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.as_of is not None:
            raise ConnectorError(
                f"Connection '{self.name}' (redshift) does not support 'as_of': "
                "Amazon Redshift has no time travel.",
                hint="Compare against a snapshot table or a restored cluster instead.",
            )
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is an Amazon Redshift database; use 'table' or "
                "'query', not 'path'."
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

    def _schema_and_table(self, name: str) -> tuple[str, str]:
        """Catalog names for ``name`` (unquoted parts fold to lower case)."""
        try:
            parsed = exp.to_table(name, dialect="redshift")
        except Exception as exc:  # sqlglot raises several error types for bad names
            raise ConnectorError(f"Cannot parse table name '{name}': {exc}") from exc

        def part(node: Any) -> str | None:
            if not isinstance(node, exp.Identifier):
                return None
            return str(node.this) if node.quoted else str(node.this).lower()

        table = part(parsed.args.get("this"))
        schema = part(parsed.args.get("db")) or (self._config.schema_ or "public").lower()
        if table is None:
            raise ConnectorError(f"Cannot parse table name '{name}'.")
        return schema, table

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        schema, table = self._schema_and_table(relation.table)
        rows = self._rows(
            "SELECT 1 FROM svv_tables WHERE table_schema = %s AND table_name = %s", (schema, table)
        )
        return bool(rows)

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            schema, table = self._schema_and_table(relation.table)
            rows = self._rows(_COLUMNS_SQL, (schema, table))
            if not rows:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'.",
                    hint=f"Looked for {schema}.{table} in svv_columns; check the name and grants.",
                )
            columns = []
            for r in rows:
                native = native_type(str(r[1]), r[5], r[6], r[7])
                columns.append(
                    ColumnSchema(
                        name=str(r[0]),
                        native_type=native,
                        type=parse_native_type(native, "redshift"),
                        nullable=str(r[2]).upper() != "NO",
                        default=None if r[3] is None else str(r[3]),
                        ordinal=int(r[4]),
                        comment=r[8] or None,
                    )
                )
            return DatasetSchema(
                ref=relation.ref,
                columns=tuple(columns),
                primary_key=self.get_primary_key(relation),
            )
        # A query: describe its result columns from the driver's metadata.
        rs = _import_redshift()
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"SELECT * FROM {relation.base_sql} LIMIT 0")
            description = list(cursor.description or ())
        except _error_type(rs) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()
        described = tuple(
            ColumnSchema(
                name=_name(d[0]),
                native_type=_type_name(d[1]),
                type=parse_native_type(_type_name(d[1]), "redshift"),
                ordinal=i + 1,
            )
            for i, d in enumerate(description)
        )
        return DatasetSchema(ref=relation.ref, columns=described)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        schema, table = self._schema_and_table(relation.table)
        try:
            rows = self._rows(_PK_SQL, (schema, table))
        except ConnectorError:
            return ()
        return tuple(str(r[0]) for r in rows)

    def list_datasets(self, pattern: str) -> list[str]:
        placeholders = ", ".join(["%s"] * len(_SYSTEM_SCHEMAS))
        rows = self._rows(
            f"SELECT table_schema, table_name FROM svv_tables WHERE table_schema NOT IN ({placeholders})",
            _SYSTEM_SCHEMAS,
        )
        default_schema = (self._config.schema_ or "public").lower()
        qualified_pattern = "." in pattern
        names: list[str] = []
        for schema, table in rows:
            if qualified_pattern:
                target = f"{schema}.{table}"
            elif str(schema).lower() == default_schema:
                target = str(table)
            else:
                continue
            if fnmatchcase(target.lower(), pattern.lower()):
                names.append(target)
        return sorted(names)


_PG_TYPE_NAMES = {
    16: "boolean",
    17: "bytea",
    20: "bigint",
    21: "smallint",
    23: "integer",
    25: "text",
    700: "real",
    701: "double precision",
    1042: "character",
    1043: "character varying",
    1082: "date",
    1083: "time without time zone",
    1114: "timestamp without time zone",
    1184: "timestamp with time zone",
    1266: "time with time zone",
    1700: "numeric",
    3000: "geometry",
    3001: "geography",
    4000: "super",
    6551: "varbyte",
}
"""Type OIDs reported in cursor descriptions (``redshift_connector.utils.oids.RedshiftOID``)."""


def _type_name(code: Any) -> str:
    if isinstance(code, str):
        return code
    return _PG_TYPE_NAMES.get(code, "unknown") if isinstance(code, int) else "unknown"


def _name(value: Any) -> str:
    return value.decode() if isinstance(value, bytes | bytearray) else str(value)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
