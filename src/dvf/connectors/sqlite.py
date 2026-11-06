"""SQLite connector.

SQLite lacks regular expressions, Unicode case mapping, decimals, and date functions with
fractional seconds, so dvf registers Python functions on every connection. Canonicalization
functions call the reference implementation in :mod:`dvf.normalize.canonical` directly.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pyarrow as pa

from dvf.config.connections import SQLiteConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.arrow import batches_from_rows, rows_to_table
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError
from dvf.core.types import LogicalType, parse_native_type
from dvf.normalize import canonical as canon
from dvf.normalize.normalizers import truncate_datetime
from dvf.plugins.base import Connector, Relation
from dvf.sql.dialects import get_dialect

_SQL = get_dialect("sqlite")


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _null_safe(func: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(value: Any, *args: Any) -> Any:
        if value is None:
            return None
        try:
            return func(value, *args)
        except (ValueError, TypeError, ArithmeticError):
            return None

    return wrapper


def _regexp(pattern: str, value: Any, full: int) -> int | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    regex = _compiled(pattern)
    return int(bool(regex.fullmatch(text) if full else regex.search(text)))


def _spec(kind: canon.Kind, **kwargs: Any) -> canon.CanonSpec:
    return canon.CanonSpec(kind=kind, logical=LogicalType.UNKNOWN, **kwargs)


def _epoch_us(value: Any, timezone: str) -> float | None:
    compare = canon.NumericCompare(epoch=True)
    return canon.numeric_value(value, _spec(canon.Kind.TIMESTAMP, timezone=timezone), compare)


def _datetime_text(dt: Any) -> str:
    return dt.isoformat(sep=" ")  # type: ignore[no-any-return]


FUNCTIONS: dict[str, tuple[int, Callable[..., Any]]] = {
    "dvf_regexp": (3, _regexp),
    "dvf_md5": (
        1,
        _null_safe(
            lambda v: hashlib.md5(str(v).encode("utf-8"), usedforsecurity=False).hexdigest()
        ),
    ),
    "dvf_lower": (1, _null_safe(lambda v: canon.simple_lower(str(v)))),
    "dvf_upper": (1, _null_safe(lambda v: canon.simple_upper(str(v)))),
    "dvf_unicode_normalize": (
        2,
        _null_safe(lambda v, form: unicodedata.normalize(form, str(v))),
    ),
    "dvf_collapse_whitespace": (1, _null_safe(lambda v: canon._WS_RE.sub(" ", str(v)))),
    "dvf_canon_integer": (1, _null_safe(canon._integer)),
    "dvf_canon_decimal": (2, _null_safe(canon.decimal_text)),
    "dvf_canon_date": (1, _null_safe(lambda v: canon.parse_date(v).isoformat())),
    "dvf_canon_timestamp": (
        3,
        _null_safe(
            lambda v, precision, tz: canon.canonical_text(
                v, _spec(canon.Kind.TIMESTAMP, precision=precision, timezone=tz)
            )
        ),
    ),
    "dvf_canon_time": (2, _null_safe(lambda v, precision: canon._time(v, precision))),
    "dvf_canon_boolean": (1, _null_safe(canon._boolean)),
    "dvf_epoch_us": (2, _null_safe(_epoch_us)),
    "dvf_truncate_timestamp": (
        2,
        _null_safe(
            lambda v, unit: _datetime_text(truncate_datetime(canon.parse_datetime(v), unit))
        ),
    ),
    "dvf_to_utc": (2, _null_safe(lambda v, tz: _datetime_text(canon.to_utc_naive(v, tz)))),
}


def register_functions(con: sqlite3.Connection) -> None:
    """Register dvf's SQL functions on a SQLite connection."""
    for name, (arity, func) in FUNCTIONS.items():
        con.create_function(name, arity, func, deterministic=True)


def _split_table(name: str) -> tuple[str | None, str]:
    schema, _, table = name.rpartition(".")
    return (schema or None), table.strip('"')


class SQLiteConnector(Connector):
    """A SQLite database file."""

    dialect = "sqlite"
    Config = SQLiteConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: sqlite3.Connection | None = None
        self._probe = 0

    def _path(self) -> str:
        assert isinstance(self.config, SQLiteConnection)
        database = self.config.database
        if database == ":memory:":
            return database
        path = Path(database)
        return str(path if path.is_absolute() else self.base_dir / path)

    def open(self) -> None:
        if self._con is not None:
            return
        path = self._path()
        try:
            if self.config.read_only and path != ":memory:":
                if not Path(path).exists():
                    raise ConnectorError(
                        f"SQLite database '{path}' for connection '{self.name}' does not exist."
                    )
                # Engines may read result batches from a worker thread; access is still serialized.
                con = sqlite3.connect(
                    f"file:{Path(path).as_posix()}?mode=ro", uri=True, check_same_thread=False
                )
            else:
                con = sqlite3.connect(path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise ConnectorError(f"Could not open SQLite database '{path}': {exc}") from exc
        register_functions(con)
        self._con = con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    @property
    def connection(self) -> sqlite3.Connection:
        self.open()
        assert self._con is not None
        return self._con

    def server_version(self) -> str | None:
        return f"SQLite {sqlite3.sqlite_version}"

    def _execute(self, sql: str) -> sqlite3.Cursor:
        try:
            return self.connection.execute(sql)
        except sqlite3.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc

    def query(self, sql: str) -> pa.Table:
        cursor = self._execute(sql)
        names = [d[0] for d in cursor.description or ()]
        return rows_to_table(names, cursor.fetchall())

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        cursor = self._execute(sql)
        names = [d[0] for d in cursor.description or ()]
        yield from batches_from_rows(names, iter(cursor), batch_size)

    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.as_of is not None:
            raise ConnectorError(f"Connection '{self.name}' (sqlite) does not support 'as_of'.")
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is a SQLite database; use 'table' or 'query', not 'path'."
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

    def _table_info(self, schema: str | None, table: str) -> list[tuple[Any, ...]]:
        prefix = f"{_SQL.quote(schema)}." if schema else ""
        return self._execute(f"PRAGMA {prefix}table_info({_SQL.literal(table)})").fetchall()

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            schema, table = _split_table(relation.table)
            info = self._table_info(schema, table)
            if not info:
                raise ConnectorError(
                    f"Table '{relation.table}' not found on connection '{self.name}'."
                )
            nullable_known = True
        else:
            self._probe += 1
            view = f"dvf_schema_probe_{self._probe}"
            self._execute(f"CREATE TEMP VIEW {view} AS SELECT * FROM {relation.base_sql}")
            try:
                info = self._table_info("temp", view)
            finally:
                self._execute(f"DROP VIEW temp.{view}")
            nullable_known = False
        columns = tuple(
            ColumnSchema(
                name=row[1],
                native_type=row[2] or "",
                type=parse_native_type(row[2] or "", "sqlite"),
                nullable=(not row[3]) if nullable_known else None,
                default=None if row[4] is None else str(row[4]),
                ordinal=int(row[0]) + 1,
            )
            for row in info
        )
        pk = tuple(row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5]))
        return DatasetSchema(ref=relation.ref, columns=columns, primary_key=pk)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        return self.get_schema(relation).primary_key

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        schema, table = _split_table(relation.table)
        return bool(self._table_info(schema, table))

    def list_datasets(self, pattern: str) -> list[str]:
        rows = self._execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        from fnmatch import fnmatchcase

        return sorted(r[0] for r in rows if fnmatchcase(r[0].lower(), pattern.lower()))
