"""DuckDB result sink: run and check results as tables in a DuckDB file.

Timestamps are stored as UTC ``TIMESTAMP`` values (without a time zone, so no extra Python
packages are needed to read them back). Rows are inserted through Arrow, which is far faster than
row-by-row statements for runs with many checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import duckdb
import pyarrow as pa

from dvf.sinks._schema import ColumnType, SinkTable
from dvf.sinks.sql import Session, SqlResultSink, chunks, quote

if TYPE_CHECKING:
    from dvf.config.models import SinkConfig

_ARROW_TYPES = {
    ColumnType.TEXT: pa.string(),
    ColumnType.BIGINT: pa.int64(),
    ColumnType.DOUBLE: pa.float64(),
    ColumnType.BOOLEAN: pa.bool_(),
    ColumnType.TIMESTAMP: pa.timestamp("us"),
}
_STAGING = "dvf_sink_staging"


class DuckDBSession:
    """A :class:`~dvf.sinks.sql.Session` over a DuckDB connection."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.con.execute(sql, list(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return self.con.execute(sql, list(params)).fetchall()

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self.con.executemany(sql, [list(row) for row in rows])

    def begin(self) -> None:
        self.con.begin()

    def commit(self) -> None:
        self.con.commit()

    def rollback(self) -> None:
        self.con.rollback()

    def close(self) -> None:
        self.con.close()


class DuckDBSink(SqlResultSink):
    """Write run and check results to tables in a DuckDB file (``path``)."""

    label: ClassVar[str] = "DuckDB"
    type_names: ClassVar[dict[ColumnType, str]] = {
        ColumnType.TEXT: "VARCHAR",
        ColumnType.BIGINT: "BIGINT",
        ColumnType.DOUBLE: "DOUBLE",
        ColumnType.BOOLEAN: "BOOLEAN",
        ColumnType.TIMESTAMP: "TIMESTAMP",
    }
    primary_keys: ClassVar[bool] = False
    """DuckDB checks unique keys eagerly, so deleting and re-inserting a run's key in one
    transaction can fail; idempotence comes from delete-then-insert instead."""

    def __init__(self, config: SinkConfig, base_dir: Path) -> None:
        super().__init__(config, base_dir)
        self.path = self._resolve_path()

    def driver_errors(self) -> tuple[type[BaseException], ...]:
        return (duckdb.Error,)

    def connect(self) -> Session:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return DuckDBSession(duckdb.connect(str(self.path)))

    def _connect_hint(self) -> str:
        return (
            f"Check that '{self.path}' is a DuckDB database file that no other process holds open "
            "for writing."
        )

    def existing_columns(self, session: Session, table: SinkTable) -> set[str]:
        rows = session.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
            (self.schema or "main", self.prefix + table.name),
        )
        return {str(row[0]) for row in rows}

    def adapt(self, kind: ColumnType, value: Any) -> Any:
        if kind is ColumnType.TIMESTAMP and isinstance(value, datetime):
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    def insert(self, session: Session, table: SinkTable, rows: Iterable[tuple[Any, ...]]) -> None:
        assert isinstance(session, DuckDBSession)
        names = table.column_names
        kinds = [column.type for column in table.columns]
        schema = pa.schema(
            [pa.field(name, _ARROW_TYPES[kind]) for name, kind in zip(names, kinds, strict=True)]
        )
        columns = ", ".join(quote(name) for name in names)
        sql = f"INSERT INTO {self.table_name(table)} ({columns}) SELECT {columns} FROM {_STAGING}"
        for chunk in chunks(rows, self.batch_size):
            arrays = [
                pa.array(
                    [self.adapt(kind, row[position]) for row in chunk], type=_ARROW_TYPES[kind]
                )
                for position, kind in enumerate(kinds)
            ]
            batch = pa.Table.from_arrays(arrays, schema=schema)
            session.con.register(_STAGING, batch)
            try:
                session.con.execute(sql)
            finally:
                session.con.unregister(_STAGING)
