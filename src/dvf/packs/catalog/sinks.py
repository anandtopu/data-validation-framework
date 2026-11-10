"""Result sinks ``delta`` (Delta Lake tables) and ``snowflake`` (Snowflake tables, beta).

Both write the same three tables as the other sinks (:mod:`dvf.sinks._schema`): ``runs``,
``checks``, and ``column_mismatches``, named ``<table_prefix><name>``, and writing a run again
replaces its rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qsl, unquote, urlsplit

import pyarrow as pa

from dvf.core.errors import ConfigError, ConnectorError, DependencyError, DvfError
from dvf.plugins.base import ResultSink
from dvf.security.redaction import MASK, global_redactor
from dvf.sinks._schema import TABLES, ColumnType, SinkTable, table_rows
from dvf.sinks.sql import Session, SqlResultSink

if TYPE_CHECKING:
    from dvf.config.models import SinkConfig
    from dvf.models.results import RunResult

_ARROW_TYPES = {
    ColumnType.TEXT: pa.string(),
    ColumnType.BIGINT: pa.int64(),
    ColumnType.DOUBLE: pa.float64(),
    ColumnType.BOOLEAN: pa.bool_(),
    ColumnType.TIMESTAMP: pa.timestamp("us", tz="UTC"),
}


def arrow_schema(table: SinkTable) -> pa.Schema:
    """The Arrow schema of a sink table (timestamps in UTC)."""
    return pa.schema([pa.field(c.name, _ARROW_TYPES[c.type]) for c in table.columns])


def arrow_rows(run: RunResult, table: SinkTable) -> pa.Table:
    """The rows of ``table`` for ``run`` as an Arrow table."""
    rows = list(table_rows(run, table))
    schema = arrow_schema(table)
    arrays = [pa.array([row[i] for row in rows], type=field.type) for i, field in enumerate(schema)]
    return pa.Table.from_arrays(arrays, schema=schema)


def _is_url(path: str) -> bool:
    return "://" in path


# ====================================================================== Delta Lake


def _import_deltalake() -> Any:
    try:
        import deltalake
    except ImportError as exc:
        raise DependencyError("deltalake", "delta", "The Delta Lake result sink") from exc
    return deltalake


class DeltaSink(ResultSink):
    """Write run and check results to Delta Lake tables under ``path`` (a folder or URL).

    Each table is a folder ``<path>/<table_prefix><name>``. A run's rows are replaced with one
    atomic Delta commit per table (an overwrite restricted to the run's ``run_id``), so writing a
    run twice never duplicates rows. Columns that a newer dvf adds are merged into existing tables.
    ``options.storage_options`` is passed to deltalake for object storage (use secret references
    for credentials).
    """

    _OPTIONS = frozenset({"storage_options"})

    def __init__(self, config: SinkConfig, base_dir: Path) -> None:
        super().__init__(config, base_dir)
        options = dict(config.options)
        unknown = sorted(set(options) - self._OPTIONS)
        if unknown:
            raise ConfigError(
                f"Unknown option(s) for the Delta result sink: {', '.join(unknown)}. "
                "Supported: storage_options."
            )
        storage = options.get("storage_options") or {}
        if not isinstance(storage, dict):
            raise ConfigError("The Delta result sink option 'storage_options' must be a mapping.")
        self.storage_options = {str(k): str(v) for k, v in storage.items()}
        if config.schema_:
            raise ConfigError(
                "The Delta result sink does not support 'schema'; tables are folders under 'path'."
            )
        if not config.path:
            raise ConfigError(
                "The Delta result sink needs 'path': the folder (or URL) holding the tables, "
                "relative to the configuration file."
            )
        self.prefix = config.table_prefix
        self.location = self._location(config.path)

    def _location(self, path: str) -> str:
        if _is_url(path):
            return path.rstrip("/")
        local = Path(path).expanduser()
        return str(local if local.is_absolute() else self.base_dir / local)

    def table_uri(self, table: SinkTable) -> str:
        name = self.prefix + table.name
        if _is_url(self.location):
            return f"{self.location}/{name}"
        return str(Path(self.location) / name)

    def write(self, run: RunResult) -> None:
        deltalake = _import_deltalake()
        if not _is_url(self.location):
            Path(self.location).mkdir(parents=True, exist_ok=True)
        predicate = "run_id = '" + run.run_id.replace("'", "''") + "'"
        storage = self.storage_options or None
        for table in TABLES:
            uri = self.table_uri(table)
            data = arrow_rows(run, table)
            try:
                if not deltalake.DeltaTable.is_deltatable(uri, storage_options=storage):
                    deltalake.write_deltalake(uri, data, mode="append", storage_options=storage)
                else:
                    deltalake.write_deltalake(
                        uri,
                        data,
                        mode="overwrite",
                        predicate=predicate,
                        schema_mode="merge",
                        storage_options=storage,
                    )
            except DvfError:
                raise
            except Exception as exc:
                detail = self.redact(f"{type(exc).__name__}: {exc}")
                raise ConnectorError(
                    f"Could not write run '{run.run_id}' to the Delta table {uri}: {detail}",
                    hint="Check the path, storage credentials, and that no other writer changed "
                    "the table's schema; each table is written atomically, so write the run again.",
                ) from None

    def redact(self, text: str) -> str:
        for value in sorted(self.storage_options.values(), key=len, reverse=True):
            if len(value) >= 4:
                text = text.replace(value, MASK)
        return global_redactor().redact_text(text)


# ====================================================================== Snowflake

_CONNECTION_FIELDS = frozenset(
    {
        "account",
        "user",
        "password",
        "authenticator",
        "private_key_path",
        "private_key_passphrase",
        "warehouse",
        "database",
        "role",
    }
)


def _ident(name: str) -> str:
    """An upper-case quoted Snowflake identifier (equal to the unquoted name)."""
    return '"' + name.upper().replace('"', '""') + '"'


def parse_snowflake_url(url: str) -> dict[str, Any]:
    """Settings from ``snowflake://user:password@account/database/schema?warehouse=WH&role=R``."""
    parts = urlsplit(url)
    if parts.scheme.split("+", 1)[0].lower() != "snowflake":
        raise ConfigError(
            "The Snowflake result sink 'url' must look like "
            "snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE"
        )
    settings: dict[str, Any] = {}
    if parts.hostname:
        settings["account"] = parts.hostname
    if parts.username:
        settings["user"] = unquote(parts.username)
    if parts.password is not None:
        settings["password"] = unquote(parts.password)
    path = [unquote(p) for p in parts.path.split("/") if p]
    if path:
        settings["database"] = path[0]
    if len(path) > 1:
        settings["schema"] = path[1]
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        settings[key.lower()] = value
    return settings


class SnowflakeSession:
    """A :class:`~dvf.sinks.sql.Session` over a Snowflake connection.

    DDL commits implicitly in Snowflake, so the sink creates its tables first and then opens
    the transaction that deletes and inserts the run's rows (see :meth:`SnowflakeSink.ensure_tables`).
    """

    def __init__(self, con: Any) -> None:
        self.con = con

    def _cursor(self) -> Any:
        return self.con.cursor()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        cursor = self._cursor()
        try:
            if params:
                cursor.execute(sql, tuple(params))
            else:
                cursor.execute(sql)
        finally:
            cursor.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cursor = self._cursor()
        try:
            if params:
                cursor.execute(sql, tuple(params))
            else:
                cursor.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        cursor = self._cursor()
        try:
            cursor.executemany(sql, [tuple(row) for row in rows])
        finally:
            cursor.close()

    def begin(self) -> None:
        """The transaction starts after the tables exist (DDL commits implicitly)."""

    def commit(self) -> None:
        self.execute("COMMIT")

    def rollback(self) -> None:
        self.execute("ROLLBACK")

    def close(self) -> None:
        self.con.close()


class SnowflakeSink(SqlResultSink):
    """Write run and check results to Snowflake tables (beta: verified with a fake driver only).

    Connect with ``url`` (``snowflake://user:password@account/database/schema?warehouse=WH``) or
    with ``options`` holding the Snowflake connection settings (``account``, ``user``,
    ``password``, ``authenticator``, ``private_key_path``, ``private_key_passphrase``,
    ``warehouse``, ``database``, ``role``), authenticated exactly like the ``snowflake``
    connector; other options go to ``snowflake.connector.connect``. Table and column names are
    upper case (``DVF_RUNS.RUN_ID``), so they can be queried without quotes.
    """

    label: ClassVar[str] = "Snowflake"
    placeholder: ClassVar[str] = "%s"
    type_names: ClassVar[dict[ColumnType, str]] = {
        ColumnType.TEXT: "VARCHAR",
        ColumnType.BIGINT: "NUMBER(38, 0)",
        ColumnType.DOUBLE: "FLOAT",
        ColumnType.BOOLEAN: "BOOLEAN",
        ColumnType.TIMESTAMP: "TIMESTAMP_TZ",
    }
    primary_keys: ClassVar[bool] = True
    """Snowflake records primary keys without enforcing them (delete-then-insert stays valid)."""
    driver_option_names: ClassVar[frozenset[str] | None] = None

    def __init__(self, config: SinkConfig, base_dir: Path) -> None:
        super().__init__(config, base_dir)
        settings: dict[str, Any] = {}
        if config.url is not None and config.url.get_secret_value().strip():
            settings.update(parse_snowflake_url(config.url.get_secret_value().strip()))
        extra: dict[str, Any] = {}
        for key, value in self.driver_options.items():
            if key in _CONNECTION_FIELDS:
                settings[key] = value
            else:
                extra[key] = value
        if self.schema:
            settings["schema"] = self.schema
        elif settings.get("schema"):
            self.schema = str(settings["schema"])
        if not settings.get("account"):
            raise ConfigError(
                "The Snowflake result sink needs an account: set 'url' "
                "(snowflake://user:password@account/database/schema?warehouse=WH) or "
                "'options.account'."
            )
        from dvf.config.connections import SnowflakeConnection

        try:
            self.connection_config = SnowflakeConnection.model_validate(
                {"type": "snowflake", **settings, "options": extra, "read_only": False}
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid Snowflake result sink settings: {exc}") from None
        self._driver: Any = None

    # ------------------------------------------------------------------ driver
    def _module(self) -> Any:
        if self._driver is None:
            from dvf.connectors.snowflake import _import_snowflake

            self._driver = _import_snowflake()
        return self._driver

    def driver_errors(self) -> tuple[type[BaseException], ...]:
        from dvf.connectors.snowflake import _error_type

        return (_error_type(self._module()),)

    def connect_args(self) -> dict[str, Any]:
        """Keyword arguments for ``snowflake.connector.connect`` (contains secrets)."""
        from dvf.connectors.snowflake import SnowflakeConnector

        connector = SnowflakeConnector("dvf_sink", self.connection_config, self.base_dir)
        args = connector._connect_args(self._module())
        session = dict(args.get("session_parameters") or {})
        session["QUERY_TAG"] = "dvf-sink"
        session.pop("STATEMENT_TIMEOUT_IN_SECONDS", None)
        args["session_parameters"] = session
        args.pop("arrow_number_to_decimal", None)
        return args

    def connect(self) -> Session:
        return SnowflakeSession(self._module().connect(**self.connect_args()))

    def _connect_hint(self) -> str:
        return (
            "Check the account, credentials, warehouse, database, and role, and that the role "
            "may create tables in the schema."
        )

    def redact(self, text: str) -> str:
        secrets = []
        config = self.connection_config
        for secret in (config.password, config.private_key_passphrase):
            if secret is not None:
                secrets.append(secret.get_secret_value())
        if self.config.url is not None:
            secrets.append(self.config.url.get_secret_value())
        for value in sorted((s for s in secrets if s), key=len, reverse=True):
            text = text.replace(value, MASK)
        return super().redact(text)

    # ------------------------------------------------------------------ SQL (upper-case names)
    def table_name(self, table: SinkTable) -> str:
        name = _ident(self.prefix + table.name)
        return f"{_ident(self.schema)}.{name}" if self.schema else name

    def column_sql(self, table: SinkTable) -> list[str]:
        return [f"{_ident(c.name)} {self.type_names[c.type]}" for c in table.columns]

    def create_sql(self, table: SinkTable) -> str:
        parts = self.column_sql(table)
        if self.primary_keys and table.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(_ident(c) for c in table.primary_key)})")
        body = ",\n  ".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {self.table_name(table)} (\n  {body}\n)"

    def add_column_sql(self, table: SinkTable, column: str) -> str:
        spec = next(c for c in table.columns if c.name == column)
        return (
            f"ALTER TABLE {self.table_name(table)} "
            f"ADD COLUMN {_ident(spec.name)} {self.type_names[spec.type]}"
        )

    def delete_sql(self, table: SinkTable) -> str:
        return f"DELETE FROM {self.table_name(table)} WHERE {_ident('run_id')} = {self.placeholder}"

    def insert_sql(self, table: SinkTable) -> str:
        names = ", ".join(_ident(name) for name in table.column_names)
        marks = ", ".join([self.placeholder] * len(table.columns))
        return f"INSERT INTO {self.table_name(table)} ({names}) VALUES ({marks})"

    def ensure_schema(self, session: Session) -> None:
        if self.schema:
            session.execute(f"CREATE SCHEMA IF NOT EXISTS {_ident(self.schema)}")

    def existing_columns(self, session: Session, table: SinkTable) -> set[str]:
        rows = session.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = COALESCE(%s, CURRENT_SCHEMA()) AND table_name = %s",
            (self.schema.upper() if self.schema else None, (self.prefix + table.name).upper()),
        )
        return {str(row[0]).lower() for row in rows}

    def ensure_tables(self, session: Session) -> None:
        """Create or extend the tables (DDL commits implicitly), then open the transaction."""
        super().ensure_tables(session)
        session.execute("BEGIN")


__all__ = [
    "DeltaSink",
    "SnowflakeSink",
    "arrow_rows",
    "arrow_schema",
    "parse_snowflake_url",
]
