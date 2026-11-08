"""Shared logic of the SQL result sinks: create the tables, then replace one run's rows.

Every write runs in one transaction: create missing tables (and add columns a newer dvf version
introduced), delete the run's existing rows, and insert the new ones. Writing the same run twice
therefore never duplicates rows, and a failed write leaves the tables as they were. Values are
always passed as query parameters; identifiers are quoted.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from dvf.core.errors import ConfigError, ConnectorError, DvfError
from dvf.plugins.base import ResultSink
from dvf.security.redaction import global_redactor
from dvf.sinks._schema import TABLES, ColumnType, SinkTable, table_rows

if TYPE_CHECKING:
    from dvf.config.models import SinkConfig
    from dvf.models.results import RunResult

DEFAULT_BATCH_SIZE = 1_000


class Session(Protocol):
    """The few database operations a sink needs, over one connection."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None: ...

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]: ...

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None: ...

    def begin(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def quote(identifier: str) -> str:
    """A double-quoted SQL identifier (valid in DuckDB, SQLite, and PostgreSQL)."""
    return '"' + identifier.replace('"', '""') + '"'


def chunks(rows: Iterable[tuple[Any, ...]], size: int) -> Iterator[list[tuple[Any, ...]]]:
    iterator = iter(rows)
    while chunk := list(islice(iterator, size)):
        yield chunk


class SqlResultSink(ResultSink):
    """Base class of the DuckDB, SQLite, and PostgreSQL sinks."""

    label: ClassVar[str] = ""
    """Database name used in messages."""
    placeholder: ClassVar[str] = "?"
    type_names: ClassVar[Mapping[ColumnType, str]] = {}
    primary_keys: ClassVar[bool] = True
    """Declare primary keys on new tables."""
    supports_schema: ClassVar[bool] = True
    driver_option_names: ClassVar[frozenset[str] | None] = frozenset()
    """``options`` keys passed to the driver besides ``batch_size``; None accepts any key."""

    def __init__(self, config: SinkConfig, base_dir: Path) -> None:
        super().__init__(config, base_dir)
        options = dict(config.options)
        self.batch_size = self._batch_size(options.pop("batch_size", DEFAULT_BATCH_SIZE))
        self.driver_options = self._driver_options(options)
        if config.schema_ and not self.supports_schema:
            raise ConfigError(
                f"The {self.label} result sink does not support 'schema'; "
                "use 'table_prefix' to keep dvf tables apart."
            )
        self.schema = config.schema_ or None
        self.prefix = config.table_prefix

    # ------------------------------------------------------------------ configuration
    def _batch_size(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(
                f"The {self.label} result sink option 'batch_size' must be a positive integer, "
                f"got {value!r}."
            )
        return value

    def _driver_options(self, options: dict[str, Any]) -> dict[str, Any]:
        allowed = self.driver_option_names
        if allowed is None:
            return options
        unknown = sorted(set(options) - allowed)
        if unknown:
            supported = ", ".join(sorted({"batch_size", *allowed}))
            raise ConfigError(
                f"Unknown option(s) for the {self.label} result sink: {', '.join(unknown)}. "
                f"Supported: {supported}."
            )
        return options

    def _resolve_path(self) -> Path:
        """The database file for file-based sinks: ``path``, relative to the configuration."""
        if not self.config.path:
            raise ConfigError(
                f"The {self.label} result sink needs 'path': the database file to write, "
                "relative to the configuration file."
            )
        path = Path(self.config.path).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    # ------------------------------------------------------------------ SQL
    def table_name(self, table: SinkTable) -> str:
        name = quote(self.prefix + table.name)
        return f"{quote(self.schema)}.{name}" if self.schema else name

    def column_sql(self, table: SinkTable) -> list[str]:
        return [f"{quote(c.name)} {self.type_names[c.type]}" for c in table.columns]

    def create_sql(self, table: SinkTable) -> str:
        parts = self.column_sql(table)
        if self.primary_keys and table.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(quote(c) for c in table.primary_key)})")
        body = ",\n  ".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {self.table_name(table)} (\n  {body}\n)"

    def add_column_sql(self, table: SinkTable, column: str) -> str:
        spec = next(c for c in table.columns if c.name == column)
        return (
            f"ALTER TABLE {self.table_name(table)} "
            f"ADD COLUMN {quote(spec.name)} {self.type_names[spec.type]}"
        )

    def delete_sql(self, table: SinkTable) -> str:
        return f"DELETE FROM {self.table_name(table)} WHERE {quote('run_id')} = {self.placeholder}"

    def insert_sql(self, table: SinkTable) -> str:
        names = ", ".join(quote(name) for name in table.column_names)
        marks = ", ".join([self.placeholder] * len(table.columns))
        return f"INSERT INTO {self.table_name(table)} ({names}) VALUES ({marks})"

    # ------------------------------------------------------------------ backend hooks
    def connect(self) -> Session:  # pragma: no cover - interface
        raise NotImplementedError

    def driver_errors(self) -> tuple[type[BaseException], ...]:  # pragma: no cover - interface
        """Exception classes of the database driver, reported as connector errors."""
        raise NotImplementedError

    def existing_columns(self, session: Session, table: SinkTable) -> set[str]:
        """Column names of ``table`` if it exists, else an empty set."""
        raise NotImplementedError  # pragma: no cover - interface

    def ensure_schema(self, session: Session) -> None:
        """Create ``schema`` if it is set and missing."""
        if self.schema:
            session.execute(f"CREATE SCHEMA IF NOT EXISTS {quote(self.schema)}")

    def adapt(self, kind: ColumnType, value: Any) -> Any:
        """Convert a cleaned value for this database (for example booleans for SQLite)."""
        return value

    def insert(self, session: Session, table: SinkTable, rows: Iterable[tuple[Any, ...]]) -> None:
        sql = self.insert_sql(table)
        kinds = [column.type for column in table.columns]
        for chunk in chunks(rows, self.batch_size):
            adapted = [
                tuple(self.adapt(kind, value) for kind, value in zip(kinds, row, strict=True))
                for row in chunk
            ]
            session.executemany(sql, adapted)

    # ------------------------------------------------------------------ write
    def ensure_tables(self, session: Session) -> None:
        self.ensure_schema(session)
        for table in TABLES:
            existing = self.existing_columns(session, table)
            if not existing:
                session.execute(self.create_sql(table))
                continue
            for name in table.column_names:
                if name not in existing:
                    session.execute(self.add_column_sql(table, name))

    def write(self, run: RunResult) -> None:
        """Replace ``run``'s rows in every sink table, in one transaction."""
        errors = self.driver_errors()
        try:
            session = self.connect()
        except errors as exc:
            detail = self.redact(str(exc))
            # Keep the driver exception as the cause only when it carries no secret.
            cause = exc if detail == str(exc) else None
            raise ConnectorError(
                f"Could not open the {self.label} result sink: {detail}",
                hint=self._connect_hint(),
            ) from cause
        try:
            session.begin()
            self.ensure_tables(session)
            for table in TABLES:
                session.execute(self.delete_sql(table), (run.run_id,))
            for table in TABLES:
                self.insert(session, table, table_rows(run, table))
            session.commit()
        except BaseException as exc:
            with contextlib.suppress(Exception):
                session.rollback()
            if isinstance(exc, errors) and not isinstance(exc, DvfError):
                detail = self.redact(str(exc))
                raise ConnectorError(
                    f"Could not write run '{run.run_id}' to the {self.label} result sink: {detail}",
                    hint="Nothing was written for this run; fix the problem and write it again.",
                ) from (exc if detail == str(exc) else None)
            raise
        finally:
            with contextlib.suppress(Exception):
                session.close()

    def _connect_hint(self) -> str:
        return "Check the sink's connection settings and that the database is reachable."

    def redact(self, text: str) -> str:
        """Mask registered secrets and URL passwords in a driver message."""
        return global_redactor().redact_text(text)
