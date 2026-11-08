"""Row filters applied to both datasets of a table: the watermark window (MIG-02) and the
in-flight exclusion for live replication (MIG-09).

The runner calls :func:`apply_row_filters` while resolving a table, so every comparison and
check sees the restricted datasets, and :func:`exclude_in_flight_keys` once the comparison plan
is known, so a row that changed on one side only is excluded from both sides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from dvf.compare.buckets import restrict_relation
from dvf.config.base import parse_duration
from dvf.config.models import InFlightConfig, TableConfig, WindowConfig
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.execution.context import RunInfo
from dvf.plugins.base import Relation
from dvf.sql.dialects import Dialect

_COMPACT_TS = re.compile(r"^(\d{8})T(\d{6})Z$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def parse_bound(value: Any, run: RunInfo) -> Any:
    """A window bound or cutoff as a Python value: ``date``, naive ``datetime`` (offsets
    converted to UTC), number, or text. ``${run.*}`` placeholders are rendered first."""
    if isinstance(value, datetime):
        return _naive_utc(value)
    if isinstance(value, date | int | float) and not isinstance(value, bool):
        return value
    text = run.render(str(value)).strip()
    compact = _COMPACT_TS.match(text)
    if compact:
        return datetime.strptime(compact.group(1) + compact.group(2), "%Y%m%d%H%M%S")
    if _ISO_DATE.match(text):
        return date.fromisoformat(text)
    if _ISO_DATETIME.match(text):
        try:
            return _naive_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return text
    return text


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def bound_literal(value: Any, dialect: Dialect) -> str:
    """SQL literal for a window bound. SQLite compares timestamps as text, so its datetimes use
    the ``YYYY-MM-DD HH:MM:SS`` form SQLite itself writes."""
    if isinstance(value, datetime) and dialect.name == "sqlite":
        return dialect.literal(value.isoformat(sep=" "))
    return dialect.literal(value)


def _side_column(config: WindowConfig | InFlightConfig, side: Side) -> str | None:
    if side is Side.SOURCE:
        return config.source_column or config.column
    return config.target_column or config.column or config.source_column


def _real_column(schema: DatasetSchema, name: str | None, what: str, side: Side) -> str:
    if name is None:
        raise PlanError(f"The table's '{what}' has no column for the {side.value} side.")
    found = schema.column(name)
    if found is None:
        available = ", ".join(c.name for c in schema.columns[:30])
        raise PlanError(
            f"{what.capitalize()} column '{name}' not found in the {side.value} dataset "
            f"{schema.ref.display()}.",
            hint=f"Available columns: {available}",
        )
    return found.name


def window_predicate(
    config: WindowConfig, side: Side, schema: DatasetSchema, dialect: Dialect, run: RunInfo
) -> str:
    """``start <= column < end`` on one side (rows with a NULL watermark are excluded)."""
    col = dialect.quote(_real_column(schema, _side_column(config, side), "window", side))
    parts: list[str] = []
    if config.start is not None:
        parts.append(f"{col} >= {bound_literal(parse_bound(config.start, run), dialect)}")
    if config.end is not None:
        op = "<=" if config.end_inclusive else "<"
        parts.append(f"{col} {op} {bound_literal(parse_bound(config.end, run), dialect)}")
    return " AND ".join(parts)


def in_flight_cutoff(config: InFlightConfig, run: RunInfo, timezone: str) -> datetime:
    """The cutoff as a naive local time in ``timezone`` (the run's start by default)."""
    zone = UTC if timezone == "UTC" else ZoneInfo(timezone)
    if config.cutoff is None:
        return run.started_at.astimezone(zone).replace(tzinfo=None)
    raw = config.cutoff
    if isinstance(raw, datetime) and raw.tzinfo is not None:
        return raw.astimezone(zone).replace(tzinfo=None)
    if isinstance(raw, str):
        text = run.render(raw).strip()
        compact = _COMPACT_TS.match(text)
        if compact:
            moment = datetime.strptime(compact.group(1) + compact.group(2), "%Y%m%d%H%M%S")
            return moment.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PlanError(
                f"in_flight.cutoff must be an ISO timestamp, got {text!r}.",
                hint="For example cutoff: '2026-11-30T22:00:00Z', or omit it to use the run start.",
            ) from exc
        if parsed.tzinfo is not None:
            return parsed.astimezone(zone).replace(tzinfo=None)
        return parsed
    assert isinstance(raw, datetime)
    return raw


@dataclass(frozen=True)
class RowFilters:
    """The window and in-flight predicates of one side, and the relation before the in-flight
    exclusion (used to find the keys in flight)."""

    window: str | None = None
    in_flight: str | None = None
    """Predicate keeping rows that are *not* in flight."""
    before_in_flight: Relation | None = None
    cutoff: datetime | None = None
    threshold: datetime | None = None

    @property
    def predicates(self) -> list[str]:
        return [p for p in (self.window, self.in_flight) if p]


def apply_row_filters(
    table: TableConfig,
    side: Side,
    relation: Relation,
    schema: DatasetSchema,
    dialect: Dialect,
    run: RunInfo,
) -> tuple[Relation, RowFilters]:
    """Restrict ``relation`` to the table's window and in-flight exclusion."""
    window = in_flight = None
    cutoff = threshold = None
    if table.window is not None:
        window = window_predicate(table.window, side, schema, dialect, run)
        relation = restrict_relation(relation, window, dialect, "dvf_window")
    before = relation
    if table.in_flight is not None:
        config = table.in_flight
        cutoff = in_flight_cutoff(config, run, table.normalization.timezone)
        threshold = cutoff - parse_duration(config.window)
        col = dialect.quote(_real_column(schema, _side_column(config, side), "in_flight", side))
        in_flight = f"({col} IS NULL OR {col} < {bound_literal(threshold, dialect)})"
        relation = restrict_relation(relation, in_flight, dialect, "dvf_in_flight")
    return relation, RowFilters(window, in_flight, before, cutoff, threshold)


def exclude_in_flight_keys(
    ctx: Any, filters: dict[str, RowFilters], config: InFlightConfig
) -> int | None:
    """Exclude, on both sides of ``ctx`` (a ComparisonContext), the keys of rows in flight on
    either side. Returns the number of keys excluded, or ``None`` without keys."""
    from dvf.engines.base import DiffSide, key_list_predicate, projection_sql

    plan = ctx.plan
    if plan is None:
        return None
    keys: set[tuple[Any, ...]] = set()
    for side, diff_side in (("source", ctx.source), ("target", ctx.target)):
        rf = filters.get(side)
        if rf is None or rf.in_flight is None or rf.before_in_flight is None:
            continue
        dialect = diff_side.connector.sql_dialect
        sql = projection_sql(
            plan,
            side,  # type: ignore[arg-type]
            rf.before_in_flight.sql,
            "keys",
            where=f"NOT {rf.in_flight}",
        )
        table = diff_side.connector.query(dialect.limit(sql, config.max_keys + 1))
        for row in table.to_pylist():
            names = [f"k{i}" for i in range(len(plan.keys))]
            values = tuple(row.get(k, row.get(k.upper())) for k in names)
            if all(v is not None for v in values):
                keys.add(values)
        if len(keys) > config.max_keys:
            raise PlanError(
                f"More than {config.max_keys:,} keys are in flight, too many to exclude on both "
                "sides.",
                hint="Narrow 'in_flight.window', raise 'in_flight.max_keys', or set "
                "'exclude_keys_on_both_sides: false'.",
            )
    if not keys:
        return 0
    ordered = sorted(keys)
    for attr, side in (("source", "source"), ("target", "target")):
        diff_side = getattr(ctx, attr)
        dialect = diff_side.connector.sql_dialect
        predicate = key_list_predicate(plan, side, ordered)  # type: ignore[arg-type]
        keep = f"(CASE WHEN {predicate} THEN 1 ELSE 0 END) = 0"
        setattr(
            ctx,
            attr,
            DiffSide(
                diff_side.connector,
                restrict_relation(diff_side.relation, keep, dialect, "dvf_in_flight_keys"),
            ),
        )
    return len(ordered)


__all__ = [
    "RowFilters",
    "apply_row_filters",
    "bound_literal",
    "exclude_in_flight_keys",
    "in_flight_cutoff",
    "parse_bound",
    "window_predicate",
]
