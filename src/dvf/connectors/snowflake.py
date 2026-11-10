"""Snowflake connector (beta; install with ``pip install 'dvf-framework[snowflake]'``).

Implemented from the snowflake-connector-python documentation and tested against a fake driver;
it has not been verified against a live account.

Authentication follows the connection's ``authenticator``:

* password (default): ``user`` and ``password``;
* key pair: ``private_key_path`` (relative to the configuration's folder) and, for an encrypted
  key, ``private_key_passphrase``; the authenticator defaults to ``SNOWFLAKE_JWT``;
* ``externalbrowser`` (SSO in a browser): ``user`` only;
* ``oauth``: the access token in ``options.token`` or, failing that, in ``password``.

Every session uses ``TIMEZONE = 'UTC'``, a ``QUERY_TAG`` of ``dvf``, and
``STATEMENT_TIMEOUT_IN_SECONDS`` from ``query_timeout_s``. Snowflake has no read-only session
mode: use a role that has only ``USAGE`` on the warehouse, database, and schema and ``SELECT`` on
the compared tables.

Snowflake upper-cases unquoted identifiers, including column aliases, so result columns whose
names look like unquoted identifiers (``ID``, ``SOURCE_ROWS``) are returned in lower case
(``id``, ``source_rows``), as dvf's generated SQL expects. Schemas (``get_schema``) keep the
names exactly as stored.
"""

from __future__ import annotations

import importlib
import inspect
import re
from collections.abc import Callable, Iterator, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pyarrow as pa

from dvf.config.connections import SnowflakeConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import batches_from_rows, rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import CanonicalType, parse_native_type
from dvf.plugins.base import Connector, Relation

_UNQUOTED = re.compile(r"[A-Z_][A-Z0-9_$]*")
"""Names Snowflake reports for unquoted identifiers (always upper case)."""
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_OFFSET = re.compile(r"-?\d+")

_TYPE_CODES = {
    0: "NUMBER",
    1: "FLOAT",
    2: "VARCHAR",
    3: "DATE",
    4: "TIMESTAMP_NTZ",
    5: "VARIANT",
    6: "TIMESTAMP_LTZ",
    7: "TIMESTAMP_TZ",
    8: "TIMESTAMP_NTZ",
    9: "OBJECT",
    10: "ARRAY",
    11: "BINARY",
    12: "TIME",
    13: "BOOLEAN",
    14: "GEOGRAPHY",
    15: "GEOMETRY",
    16: "VECTOR",
    17: "MAP",
    18: "FILE",
}
"""Result metadata type codes (``snowflake.connector.constants.FIELD_ID_TO_NAME``)."""

_HINT = (
    "Check account, user, credentials (password, key pair, or authenticator), warehouse, and role; "
    "run 'dvf connections test'."
)


def _import_snowflake() -> Any:
    try:
        return importlib.import_module("snowflake.connector")
    except ImportError as exc:
        raise DependencyError(
            "snowflake-connector-python", "snowflake", "The Snowflake connector"
        ) from exc


def _error_type(module: Any) -> type[BaseException]:
    error = getattr(module, "Error", None)
    return error if isinstance(error, type) and issubclass(error, BaseException) else Exception


def _unsupported_types(module: Any) -> tuple[type[BaseException], ...]:
    found = getattr(module, "NotSupportedError", None)
    return (found,) if isinstance(found, type) and issubclass(found, BaseException) else ()


def _major_version(module: Any) -> int:
    match = re.match(r"\d+", str(getattr(module, "__version__", "") or ""))
    return int(match.group()) if match else 0


def _hint(exc: BaseException) -> str:
    text = str(exc).lower()
    if "warehouse" in text:
        return (
            "Set 'warehouse' on the connection, and grant USAGE on it to the role; check that "
            "the warehouse is not suspended for lack of credits."
        )
    if "does not exist or not authorized" in text or "insufficient privileges" in text:
        return (
            "The object does not exist or the role cannot see it: check the name (unquoted "
            "names are upper case), and grant USAGE on the database and schema and SELECT on "
            "the table to the role."
        )
    if "role" in text:
        return "Check 'role': it must exist and be granted to the user."
    if any(word in text for word in ("password", "authenticat", "jwt", "private key", "token")):
        return (
            "Authentication failed: check user and password, the key pair (private_key_path, "
            "private_key_passphrase), or the authenticator (externalbrowser, oauth)."
        )
    return _HINT


def _supported(method: Callable[..., Any], **wanted: Any) -> dict[str, Any]:
    """The keyword arguments in ``wanted`` that ``method`` accepts (driver versions differ)."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(wanted)
    return {k: v for k, v in wanted.items() if k in params}


def lower_unquoted(table: pa.Table) -> pa.Table:
    """Lower-case result column names that Snowflake upper-cased from unquoted identifiers."""
    names = [n.lower() if _UNQUOTED.fullmatch(n) else n for n in table.column_names]
    return table if names == table.column_names else table.rename_columns(names)


def snowflake_type(native: str) -> CanonicalType:
    """Parse a Snowflake type: ``BINARY(n)`` is variable-length in Snowflake, not fixed."""
    text = native.strip()
    if text.upper().startswith("BINARY"):
        text = "VARBINARY" + text[len("BINARY") :]
    return parse_native_type(text, "snowflake")


def native_from_description(column: Sequence[Any]) -> str:
    """A Snowflake type string from a cursor description entry (``ResultMetadata``)."""
    code = column[1]
    internal_size = column[3] if len(column) > 3 else None
    precision = column[4] if len(column) > 4 else None
    scale = column[5] if len(column) > 5 else None
    name = _TYPE_CODES.get(code, str(code).upper() if isinstance(code, str) else "UNKNOWN")
    if name == "NUMBER":
        return f"NUMBER({precision if precision is not None else 38},{scale or 0})"
    if name in ("VARCHAR", "BINARY") and internal_size:
        return f"{name}({internal_size})"
    if name.startswith(("TIMESTAMP", "TIME")) and scale is not None:
        return f"{name}({scale})"
    return name


def identifier(name: str) -> str:
    """``name`` as written in SQL: bare when Snowflake would store it unquoted, else quoted."""
    return name if _UNQUOTED.fullmatch(name) else '"' + name.replace('"', '""') + '"'


def as_of_clause(as_of: str, literal: Callable[[Any], str]) -> str:
    """The ``AT(...)`` clause for ``as_of``: seconds offset, query ID, or timestamp."""
    value = as_of.strip()
    if _OFFSET.fullmatch(value):
        return f"AT(OFFSET => {int(value)})"
    if _UUID.fullmatch(value):
        return f"AT(STATEMENT => {literal(value)})"
    return f"AT(TIMESTAMP => TO_TIMESTAMP_TZ({literal(value)}))"


class SnowflakeConnector(Connector):
    """Snowflake, through snowflake-connector-python."""

    dialect = "snowflake"
    Config = SnowflakeConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: Any = None

    @property
    def _config(self) -> SnowflakeConnection:
        config = self.config
        assert isinstance(config, SnowflakeConnection)
        return config

    def _connect_args(self, driver: Any = None) -> dict[str, Any]:
        """Keyword arguments for ``snowflake.connector.connect`` (contains secrets)."""
        c = self._config
        kwargs: dict[str, Any] = {"account": c.account}
        for key, value in (
            ("user", c.user),
            ("warehouse", c.warehouse),
            ("database", c.database),
            ("schema", c.schema_),
            ("role", c.role),
        ):
            if value:
                kwargs[key] = value
        options = dict(c.options)
        authenticator = (c.authenticator or "").strip()
        if c.private_key_path:
            path = Path(c.private_key_path).expanduser()
            kwargs["private_key_file"] = str(path if path.is_absolute() else self.base_dir / path)
            if c.private_key_passphrase is not None:
                # Renamed from private_key_file_pwd in the 5.x driver.
                name = (
                    "private_key_password"
                    if _major_version(driver) >= 5
                    else "private_key_file_pwd"
                )
                kwargs[name] = c.private_key_passphrase.get_secret_value()
            authenticator = authenticator or "SNOWFLAKE_JWT"
        if authenticator:
            kwargs["authenticator"] = authenticator
        mode = authenticator.lower()
        if c.password is not None:
            if mode == "oauth":
                options.setdefault("token", c.password.get_secret_value())
            elif mode not in ("externalbrowser", "snowflake_jwt"):
                kwargs["password"] = c.password.get_secret_value()
        session: dict[str, Any] = {"QUERY_TAG": "dvf"}
        session.update(options.pop("session_parameters", None) or {})
        session["TIMEZONE"] = "UTC"
        if c.query_timeout_s:
            session["STATEMENT_TIMEOUT_IN_SECONDS"] = int(c.query_timeout_s)
        kwargs["session_parameters"] = session
        kwargs["arrow_number_to_decimal"] = True  # decimals as Decimal, not float
        kwargs.update(options)
        return kwargs

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self._con is not None:
            return
        sf = _import_snowflake()
        kwargs = self._connect_args(sf)
        try:
            self._con = sf.connect(**kwargs)
        except _error_type(sf) as exc:
            raise ConnectorError(
                f"Could not connect to Snowflake for connection '{self.name}': {exc}",
                hint=_hint(exc),
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

    def server_version(self) -> str | None:
        rows = self._rows("SELECT CURRENT_VERSION()")
        return f"Snowflake {rows[0][0]}" if rows else None

    # ------------------------------------------------------------------ queries
    def _fail(self, exc: BaseException) -> ConnectorError:
        return ConnectorError(f"Query failed on connection '{self.name}': {exc}", hint=_hint(exc))

    def _rows(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        _, rows = self._execute(sql, params)
        return rows

    def _execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        sf = _import_snowflake()
        cursor = self.connection.cursor()
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
            names = [str(d[0]) for d in cursor.description or ()]
            rows = [tuple(r) for r in cursor.fetchall()] if cursor.description else []
        except _error_type(sf) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()
        return names, rows

    def _dicts(self, sql: str) -> list[dict[str, Any]]:
        names, rows = self._execute(sql)
        keys = [n.lower() for n in names]
        return [dict(zip(keys, row, strict=False)) for row in rows]

    def query(self, sql: str) -> pa.Table:
        sf = _import_snowflake()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            table: pa.Table | None = None
            fetch = getattr(cursor, "fetch_arrow_all", None)
            if fetch is not None:
                try:
                    table = fetch(
                        **_supported(
                            fetch, force_return_table=True, force_microsecond_precision=True
                        )
                    )
                except _unsupported_types(sf):
                    table = None  # results not in Arrow format (SHOW, DESCRIBE, ...)
            if table is None:
                names = [str(d[0]) for d in cursor.description or ()]
                rows = cursor.fetchall() if cursor.description else []
                table = rows_to_table(names, [tuple(r) for r in rows])
        except _error_type(sf) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()
        return lower_unquoted(table)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        sf = _import_snowflake()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            tables: Iterator[pa.Table] | None = None
            table: pa.Table | None = None
            fetch = getattr(cursor, "fetch_arrow_batches", None)
            if fetch is not None:
                try:
                    tables = iter(fetch(**_supported(fetch, force_microsecond_precision=True)))
                    table = next(tables, None)
                except _unsupported_types(sf):
                    tables = None  # results not in Arrow format
            if tables is not None:
                while table is not None:
                    yield from lower_unquoted(table).to_batches(max_chunksize=batch_size)
                    table = next(tables, None)
                return
            names = [str(d[0]) for d in cursor.description or ()]
            for batch in batches_from_rows(names, _fetch_rows(cursor, batch_size), batch_size):
                yield from lower_unquoted(pa.Table.from_batches([batch])).to_batches()
        except _error_type(sf) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is a Snowflake database; use 'table' or 'query', not 'path'."
            )
        table: str | None = None
        if spec.table is not None:
            kind, label = DatasetKind.TABLE, spec.table
            if spec.as_of is not None:
                # Time travel: metadata lookups go through the relation, not the live table.
                base = f"{spec.table} {as_of_clause(spec.as_of, self.sql_dialect.literal)}"
                label = f"{spec.table} AS OF {spec.as_of}"
            else:
                base, table = spec.table, spec.table
        else:
            assert spec.query is not None
            if spec.as_of is not None:
                raise ConnectorError(
                    f"'as_of' applies to tables on connection '{self.name}'; "
                    "write AT(...) inside the query instead."
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
        try:
            self._execute(f"DESCRIBE TABLE {relation.table}")
        except ConnectorError:
            return False
        return True

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            try:
                rows = self._dicts(f"DESCRIBE TABLE {relation.table}")
            except ConnectorError as exc:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}': {exc}",
                    hint=exc.hint,
                ) from exc
            columns: list[ColumnSchema] = []
            for row in rows:
                if str(row.get("kind") or "COLUMN").upper() != "COLUMN":
                    continue
                native = str(row["type"])
                columns.append(
                    ColumnSchema(
                        name=str(row["name"]),
                        native_type=native,
                        type=snowflake_type(native),
                        nullable=str(row.get("null?") or "Y").upper() == "Y",
                        default=None if row.get("default") is None else str(row["default"]),
                        ordinal=len(columns) + 1,
                        comment=row.get("comment") or None,
                    )
                )
            return DatasetSchema(
                ref=relation.ref,
                columns=tuple(columns),
                primary_key=self.get_primary_key(relation),
            )
        sf = _import_snowflake()
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"SELECT * FROM {relation.base_sql} LIMIT 0")
            description = list(cursor.description or ())
        except _error_type(sf) as exc:
            raise self._fail(exc) from exc
        finally:
            cursor.close()
        described = []
        for i, d in enumerate(description):
            native = native_from_description(d)
            nullable = d[6] if len(d) > 6 else None
            described.append(
                ColumnSchema(
                    name=str(d[0]),
                    native_type=native,
                    type=snowflake_type(native),
                    nullable=None if nullable is None else bool(nullable),
                    ordinal=i + 1,
                )
            )
        return DatasetSchema(ref=relation.ref, columns=tuple(described))

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        try:
            rows = self._dicts(f"SHOW PRIMARY KEYS IN TABLE {relation.table}")
        except ConnectorError:
            return ()  # views and objects the role cannot inspect have no visible key
        rows.sort(key=lambda r: int(r.get("key_sequence") or 0))
        return tuple(str(r["column_name"]) for r in rows)

    def list_datasets(self, pattern: str) -> list[str]:
        rows = self._rows(
            "SELECT table_schema, table_name, table_schema = CURRENT_SCHEMA() "
            "FROM information_schema.tables WHERE table_schema <> 'INFORMATION_SCHEMA'"
        )
        qualified_pattern = "." in pattern
        names: list[str] = []
        for schema, table, current in rows:
            if qualified_pattern:
                target, shown = f"{schema}.{table}", f"{identifier(schema)}.{identifier(table)}"
            elif current:
                target, shown = str(table), identifier(table)
            else:
                continue
            if fnmatchcase(target.lower(), pattern.lower()):
                names.append(shown)
        return sorted(names)


def _fetch_rows(cursor: Any, size: int) -> Iterator[tuple[Any, ...]]:
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        for row in rows:
            yield tuple(row)
