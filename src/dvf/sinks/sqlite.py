"""SQLite result sink: run and check results as tables in a SQLite file.

SQLite has no boolean or timestamp types: booleans are stored as 0 or 1, and timestamps as UTC
text in the form ``YYYY-MM-DD HH:MM:SS.ffffff``, which SQLite's date functions understand and
which sorts in time order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from dvf.sinks._schema import ColumnType, SinkTable
from dvf.sinks.sql import Session, SqlResultSink

if TYPE_CHECKING:
    from dvf.config.models import SinkConfig


class SQLiteSession:
    """A :class:`~dvf.sinks.sql.Session` over a SQLite connection in explicit-transaction mode."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.con.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return list(self.con.execute(sql, tuple(params)).fetchall())

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self.con.executemany(sql, [tuple(row) for row in rows])

    def begin(self) -> None:
        self.con.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.con.execute("COMMIT")

    def rollback(self) -> None:
        if self.con.in_transaction:
            self.con.execute("ROLLBACK")

    def close(self) -> None:
        self.con.close()


class SQLiteSink(SqlResultSink):
    """Write run and check results to tables in a SQLite file (``path``)."""

    label: ClassVar[str] = "SQLite"
    type_names: ClassVar[dict[ColumnType, str]] = {
        ColumnType.TEXT: "TEXT",
        ColumnType.BIGINT: "INTEGER",
        ColumnType.DOUBLE: "REAL",
        ColumnType.BOOLEAN: "INTEGER",
        ColumnType.TIMESTAMP: "TEXT",
    }
    supports_schema: ClassVar[bool] = False

    def __init__(self, config: SinkConfig, base_dir: Path) -> None:
        super().__init__(config, base_dir)
        self.path = self._resolve_path()

    def driver_errors(self) -> tuple[type[BaseException], ...]:
        return (sqlite3.Error,)

    def connect(self) -> Session:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        return SQLiteSession(con)

    def _connect_hint(self) -> str:
        return f"Check that '{self.path}' is a SQLite database file that can be written."

    def existing_columns(self, session: Session, table: SinkTable) -> set[str]:
        rows = session.query(
            "SELECT name FROM pragma_table_info(?)",
            (self.prefix + table.name,),
        )
        return {str(row[0]) for row in rows}

    def adapt(self, kind: ColumnType, value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, datetime):
            utc = value.astimezone(UTC).replace(tzinfo=None)
            return utc.isoformat(sep=" ", timespec="microseconds")
        return value
