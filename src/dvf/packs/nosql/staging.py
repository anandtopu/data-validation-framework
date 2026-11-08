"""Unify the types of shaped rows into one Arrow schema and stream them as record batches.

Documents in one collection rarely agree on types: a field may be an ``int`` in one document, a
``long`` in another, and a string in a third. :class:`RowStager` reads the shaped rows once,
spooling them to a temporary file (memory stays bounded) while it counts the kinds seen per
column. It then picks one Arrow type per column and replays the spool as record batches:

* integers (``int``, ``long``) become 64-bit integers;
* integers mixed with decimals become decimals, and any mix with floating-point values becomes
  ``double`` (with a note);
* decimals take the largest scale seen (up to 38 digits in total);
* timestamps become UTC timestamps; ObjectId and UUID values become their canonical strings;
* nested documents and arrays left in a column become canonical JSON;
* any other mix of types becomes canonical text, with a note naming the types.

The source system's type names are kept for schema checks (``objectId``, ``long``,
``int|string``, ...), and canonical types (UUID, JSON, decimal precision, timestamp precision)
override the ones derived from Arrow.
"""

from __future__ import annotations

import pickle
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import IO, Any

import pyarrow as pa

from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, LogicalType
from dvf.normalize.canonical import parse_date, parse_datetime
from dvf.packs.nosql.values import (
    INT64_MAX,
    INT64_MIN,
    NUMERIC_KINDS,
    Classified,
    ValueKind,
    canonical_json,
    number_text,
    scalar_text,
)

MAX_DECIMAL_DIGITS = 38
"""Precision of Arrow ``decimal128``; wider decimals fall back to canonical text."""


@dataclass
class _ColumnStats:
    kinds: Counter[ValueKind] = field(default_factory=Counter)
    natives: Counter[str] = field(default_factory=Counter)
    nulls: int = 0
    int_digits: int = 1
    scale: int = 0
    int_min: int = 0
    int_max: int = 0

    def observe(self, cell: Classified) -> None:
        kind = cell.kind
        if kind is ValueKind.NULL:
            self.nulls += 1
            return
        self.kinds[kind] += 1
        self.natives[cell.native] += 1
        if kind is ValueKind.INT:
            value = cell.value
            self.int_min = min(self.int_min, value)
            self.int_max = max(self.int_max, value)
            self.int_digits = max(self.int_digits, len(str(abs(value))))
        elif kind is ValueKind.DECIMAL:
            _, digits, exponent = cell.value.as_tuple()
            assert isinstance(exponent, int)
            self.scale = max(self.scale, -exponent)
            self.int_digits = max(self.int_digits, len(digits) + exponent)


@dataclass(frozen=True)
class ColumnPlan:
    """The type chosen for one column and how cells are converted to it."""

    name: str
    arrow_type: pa.DataType
    mode: str
    """``int``, ``decimal``, ``float``, ``bool``, ``string``, ``binary``, ``timestamp``,
    ``date``, ``time``, or ``text`` (canonical text of any value)."""
    native: str
    canonical: CanonicalType | None = None
    note: str | None = None


@dataclass
class StagedColumns:
    """The outcome of staging: the schema, per-column metadata, and the batches."""

    schema: pa.Schema
    plans: list[ColumnPlan]
    batches: Iterator[pa.RecordBatch]
    rows: int
    notes: list[str]

    @property
    def native_types(self) -> dict[str, str]:
        return {p.name: p.native for p in self.plans}

    @property
    def canonical_types(self) -> dict[str, CanonicalType]:
        return {p.name: p.canonical for p in self.plans if p.canonical is not None}

    @property
    def comments(self) -> dict[str, str]:
        return {p.name: p.note for p in self.plans if p.note}


class _Spool:
    """Shaped rows written to a temporary file in chunks, and read back once."""

    def __init__(self, folder: str | None) -> None:
        # Closed by close(), which RowStager calls when the replay ends or staging fails.
        self._file: IO[bytes] = tempfile.TemporaryFile(prefix="dvf-spool-", dir=folder)  # noqa: SIM115
        self.chunks = 0

    def write(self, rows: list[dict[str, Classified]]) -> None:
        packed = [{k: (c.kind, c.value) for k, c in row.items()} for row in rows]
        pickle.dump(packed, self._file, protocol=pickle.HIGHEST_PROTOCOL)
        self.chunks += 1

    def read(self) -> Iterator[list[dict[str, tuple[ValueKind, Any]]]]:
        self._file.seek(0)
        for _ in range(self.chunks):
            # The spool is a private temporary file written by this process just before.
            yield pickle.load(self._file)  # noqa: S301

    def close(self) -> None:
        self._file.close()


@dataclass
class TypeHints:
    """Connector knowledge used when choosing column types."""

    decimal_precision: int = MAX_DECIMAL_DIGITS
    """Precision reported for decimal columns (34 for MongoDB's Decimal128)."""
    timestamp_precision: int | None = None
    """Fractional-second digits of the source's timestamps (3 for BSON dates)."""
    native_types: Mapping[str, str] = field(default_factory=dict)
    """Declared native types of top-level columns (Cassandra), shown instead of value types."""
    canonical_types: Mapping[str, CanonicalType] = field(default_factory=dict)
    """Declared canonical types of top-level columns (Cassandra)."""


class RowStager:
    """Collects shaped rows (pass one) and replays them with unified types (pass two)."""

    def __init__(
        self,
        *,
        forced: Mapping[str, str] | None = None,
        hints: TypeHints | None = None,
        temp_dir: str | None = None,
        chunk_rows: int = 5_000,
        batch_rows: int = 50_000,
    ) -> None:
        self.forced = dict(forced or {})
        self.hints = hints or TypeHints()
        self.stats: dict[str, _ColumnStats] = {}
        self.order: list[str] = []
        self.rows = 0
        self._spool = _Spool(temp_dir)
        self._pending: list[dict[str, Classified]] = []
        self.chunk_rows = chunk_rows
        self.batch_rows = batch_rows

    def add(self, row: dict[str, Classified]) -> None:
        for name, cell in row.items():
            stats = self.stats.get(name)
            if stats is None:
                stats = self.stats[name] = _ColumnStats()
                self.order.append(name)
            if name in self.forced:
                cell = force_cell(cell, self.forced[name], name, self.rows + 1)
                row[name] = cell
            stats.observe(cell)
        self.rows += 1
        self._pending.append(row)
        if len(self._pending) >= self.chunk_rows:
            self._flush()

    def add_all(self, rows: Iterable[dict[str, Classified]]) -> None:
        for row in rows:
            self.add(row)

    def _flush(self) -> None:
        if self._pending:
            self._spool.write(self._pending)
            self._pending = []

    def finish(self, *, columns: list[str] | None = None) -> StagedColumns:
        """Choose the column types and return the schema and a stream of batches.

        ``columns`` fixes the column order (and adds columns no row had, as NULL strings).
        """
        self._flush()
        names = list(columns) if columns is not None else []
        names += [n for n in self.order if n not in names]
        plans = [self._plan(name) for name in names]
        schema = pa.schema([pa.field(p.name, p.arrow_type) for p in plans])
        notes = [f"{p.name}: {p.note}" for p in plans if p.note]
        return StagedColumns(
            schema=schema,
            plans=plans,
            batches=self._replay(plans, schema),
            rows=self.rows,
            notes=notes,
        )

    def close(self) -> None:
        self._spool.close()

    # ------------------------------------------------------------------ type choice
    def _plan(self, name: str) -> ColumnPlan:
        stats = self.stats.get(name) or _ColumnStats()
        natives = [n for n, _ in stats.natives.most_common()]
        native = self.hints.native_types.get(name) or ("|".join(natives) if natives else "null")
        declared = self.hints.canonical_types.get(name)
        plan = _choose(name, stats, native, self.hints)
        if declared is not None and plan.note is None:
            plan = ColumnPlan(plan.name, plan.arrow_type, plan.mode, native, declared, None)
        return plan

    # ------------------------------------------------------------------ replay
    def _replay(self, plans: list[ColumnPlan], schema: pa.Schema) -> Iterator[pa.RecordBatch]:
        try:
            buffer: list[dict[str, tuple[ValueKind, Any]]] = []
            for chunk in self._spool.read():
                buffer.extend(chunk)
                if len(buffer) >= self.batch_rows:
                    yield _batch(buffer, plans, schema)
                    buffer = []
            if buffer:
                yield _batch(buffer, plans, schema)
        finally:
            self.close()


def _choose(name: str, stats: _ColumnStats, native: str, hints: TypeHints) -> ColumnPlan:
    kinds = set(stats.kinds)
    if not kinds:
        return ColumnPlan(name, pa.string(), "text", native)
    if kinds == {ValueKind.INT}:
        if stats.int_min >= INT64_MIN and stats.int_max <= INT64_MAX:
            return ColumnPlan(name, pa.int64(), "int", native)
        kinds = {ValueKind.DECIMAL}
    if kinds <= {ValueKind.INT, ValueKind.DECIMAL}:
        precision = stats.int_digits + stats.scale
        if precision <= MAX_DECIMAL_DIGITS:
            note = (
                "integer and decimal values; compared as decimals"
                if ValueKind.INT in kinds and ValueKind.DECIMAL in stats.kinds
                else None
            )
            canonical = CanonicalType.decimal(max(hints.decimal_precision, precision), stats.scale)
            return ColumnPlan(
                name,
                pa.decimal128(MAX_DECIMAL_DIGITS, stats.scale),
                "decimal",
                native,
                canonical,
                note,
            )
        return ColumnPlan(
            name,
            pa.string(),
            "text",
            native,
            None,
            f"decimal values need {precision} digits (more than {MAX_DECIMAL_DIGITS}); "
            "compared as canonical text",
        )
    if kinds <= NUMERIC_KINDS:
        return ColumnPlan(
            name,
            pa.float64(),
            "float",
            native,
            None,
            None
            if kinds == {ValueKind.FLOAT}
            else f"mixed numeric types ({native}); compared as double",
        )
    simple: dict[ValueKind, tuple[pa.DataType, str, CanonicalType | None]] = {
        ValueKind.BOOL: (pa.bool_(), "bool", None),
        ValueKind.STRING: (pa.string(), "string", None),
        ValueKind.OBJECT_ID: (pa.string(), "string", CanonicalType.string(24, fixed=True)),
        ValueKind.UUID: (pa.string(), "string", CanonicalType.simple(LogicalType.UUID)),
        ValueKind.BINARY: (pa.binary(), "binary", None),
        ValueKind.DATE: (pa.date32(), "date", None),
        ValueKind.TIME: (pa.time64("us"), "time", None),
        ValueKind.TIMESTAMP: (
            pa.timestamp("us", tz="UTC"),
            "timestamp",
            CanonicalType.timestamp(hints.timestamp_precision, tz=True),
        ),
    }
    if len(kinds) == 1 and next(iter(kinds)) in simple:
        arrow_type, mode, declared = simple[next(iter(kinds))]
        return ColumnPlan(name, arrow_type, mode, native, declared)
    if kinds <= {ValueKind.OBJECT, ValueKind.ARRAY}:
        return ColumnPlan(name, pa.string(), "text", native, CanonicalType.simple(LogicalType.JSON))
    if kinds == {ValueKind.TIMESTAMP, ValueKind.DATE}:
        return ColumnPlan(
            name,
            pa.timestamp("us", tz="UTC"),
            "timestamp",
            native,
            CanonicalType.timestamp(hints.timestamp_precision, tz=True),
            "dates and timestamps; dates are taken as midnight UTC",
        )
    return ColumnPlan(
        name,
        pa.string(),
        "text",
        native,
        None,
        f"mixed types ({native}); values are compared as canonical text",
    )


def _convert(plan: ColumnPlan, kind: ValueKind, value: Any) -> Any:
    mode = plan.mode
    if kind is ValueKind.NULL:
        return None
    if mode == "text":
        return scalar_text(Classified(kind, "", value))
    if mode == "float":
        return float(value)
    if mode == "decimal":
        return value if isinstance(value, Decimal) else Decimal(value)
    if mode == "timestamp" and kind is ValueKind.DATE:
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value


def _batch(
    rows: list[dict[str, tuple[ValueKind, Any]]], plans: list[ColumnPlan], schema: pa.Schema
) -> pa.RecordBatch:
    arrays = []
    for plan in plans:
        values = []
        for row in rows:
            cell = row.get(plan.name)
            values.append(None if cell is None else _convert(plan, cell[0], cell[1]))
        try:
            arrays.append(pa.array(values, type=plan.arrow_type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
            raise ConnectorError(
                f"Column '{plan.name}' could not be converted to {plan.arrow_type}: {exc}",
                hint="Force a type with the dataset option 'types', for example "
                f"types: {{{plan.name}: string}}.",
            ) from exc
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


# ====================================================================== forced types


def force_cell(cell: Classified, target: str, column: str, row: int) -> Classified:
    """Convert a cell to a forced column type (dataset option ``types``)."""
    kind, value = cell.kind, cell.value
    if kind is ValueKind.NULL:
        return cell
    try:
        converted = _force(kind, value, target)
    except (ValueError, TypeError, ArithmeticError, InvalidOperation) as exc:
        raise ConnectorError(
            f"Row {row:,}: a {cell.native} value in column '{column}' cannot be converted to "
            f"{target}: {exc}",
            hint=f"Remove '{column}' from the dataset option 'types', or clean the source data.",
        ) from exc
    return Classified(converted[0], cell.native, converted[1])


def _force(kind: ValueKind, value: Any, target: str) -> tuple[ValueKind, Any]:
    if target == "string":
        return ValueKind.STRING, scalar_text(Classified(kind, "", value))
    if target == "json":
        if kind in (ValueKind.OBJECT, ValueKind.ARRAY):
            return kind, value
        return ValueKind.STRING, canonical_json(Classified(kind, "", value))
    if target == "integer":
        number = _as_decimal(kind, value)
        if number != number.to_integral_value():
            raise ValueError("not a whole number")
        return ValueKind.INT, int(number)
    if target == "decimal":
        return ValueKind.DECIMAL, _as_decimal(kind, value)
    if target == "double":
        return ValueKind.FLOAT, float(_as_decimal(kind, value))
    if target == "boolean":
        if kind is ValueKind.BOOL:
            return kind, value
        if kind is ValueKind.INT and value in (0, 1):
            return ValueKind.BOOL, bool(value)
        if kind is ValueKind.STRING and value.strip().lower() in ("true", "false", "1", "0"):
            return ValueKind.BOOL, value.strip().lower() in ("true", "1")
        raise ValueError("not a boolean")
    if target == "timestamp":
        if kind is ValueKind.TIMESTAMP:
            return kind, value
        if kind is ValueKind.DATE:
            return ValueKind.TIMESTAMP, datetime(value.year, value.month, value.day, tzinfo=UTC)
        if kind is ValueKind.STRING:
            parsed = parse_datetime(value)
            parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
            return ValueKind.TIMESTAMP, parsed.astimezone(UTC)
        raise ValueError("not a timestamp")
    if target == "date":
        if kind is ValueKind.DATE:
            return kind, value
        if kind is ValueKind.TIMESTAMP:
            return ValueKind.DATE, value.date()
        if kind is ValueKind.STRING:
            return ValueKind.DATE, parse_date(value)
        raise ValueError("not a date")
    raise ValueError(f"unknown type '{target}'")


def _as_decimal(kind: ValueKind, value: Any) -> Decimal:
    if kind in (ValueKind.INT, ValueKind.DECIMAL):
        return Decimal(value)
    if kind is ValueKind.FLOAT:
        text = number_text(value)
        if text is None:
            raise ValueError("not a finite number")
        return Decimal(text)
    if kind is ValueKind.STRING:
        return Decimal(value.strip())
    if kind is ValueKind.BOOL:
        return Decimal(int(value))
    raise ValueError("not a number")


__all__ = ["ColumnPlan", "RowStager", "StagedColumns", "TypeHints", "force_cell"]
