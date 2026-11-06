"""Conversion of DB-API rows to Arrow, for drivers without native Arrow support."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

import pyarrow as pa

DEFAULT_BATCH_SIZE = 50_000


def _plain(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)


def to_array(values: Sequence[Any]) -> pa.Array:
    """Build an Arrow array, inferring the type; mixed or exotic values fall back to strings."""
    plain = [_plain(v) for v in values]
    if all(v is None for v in plain):
        return pa.array(plain, type=pa.string())
    if any(isinstance(v, timedelta) for v in plain):
        return pa.array(plain, type=pa.duration("us"))
    try:
        return pa.array(plain)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError, TypeError):
        return pa.array([_as_text(v) for v in plain], type=pa.string())


def rows_to_table(names: Sequence[str], rows: Sequence[Sequence[Any]]) -> pa.Table:
    """Convert DB-API rows to an Arrow table (column names may repeat)."""
    if rows:
        columns = list(zip(*rows, strict=True))
        arrays = [to_array(list(col)) for col in columns]
    else:
        arrays = [pa.array([], type=pa.string()) for _ in names]
    return pa.Table.from_arrays(arrays, names=list(names))


def batches_from_rows(
    names: Sequence[str], rows: Iterable[Sequence[Any]], batch_size: int = DEFAULT_BATCH_SIZE
) -> Iterator[pa.RecordBatch]:
    """Group a row iterator into Arrow record batches."""
    buffer: list[Sequence[Any]] = []
    for row in rows:
        buffer.append(row)
        if len(buffer) >= batch_size:
            yield from rows_to_table(names, buffer).to_batches()
            buffer = []
    if buffer:
        yield from rows_to_table(names, buffer).to_batches()


__all__ = ["DEFAULT_BATCH_SIZE", "batches_from_rows", "rows_to_table", "to_array"]
