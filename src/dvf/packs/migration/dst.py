"""MIG-11: local timestamps that fall into daylight saving time transitions.

When clocks move forward, local times in the skipped hour do not exist; when they move back,
local times in the repeated hour are ambiguous. Converting such values to UTC (or a DATE to a
TIMESTAMP at midnight in zones that switch at midnight) silently picks or invents an instant, so
the source values are found before the comparison. The transitions come from the IANA time zone
database (``zoneinfo``); the counting runs in the database as range predicates.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

from dvf.checks.context import CheckContext, CheckOutcome, mostly_passes
from dvf.checks.pair import masked_evidence
from dvf.core.enums import Dimension
from dvf.core.types import LogicalType
from dvf.models.results import MetricComparison
from dvf.normalize.canonical import parse_datetime
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int
from dvf.packs.migration.windows import bound_literal
from dvf.plugins.base import Check

Interval = tuple[datetime, datetime]


def transitions(
    zone: ZoneInfo, first_year: int, last_year: int
) -> list[tuple[datetime, timedelta, timedelta]]:
    """``(UTC instant, offset before, offset after)`` of every UTC offset change in the years."""
    out: list[tuple[datetime, timedelta, timedelta]] = []
    start = int(datetime(first_year, 1, 1, tzinfo=UTC).timestamp())
    end = (
        int(datetime(last_year + 1, 1, 1, tzinfo=UTC).timestamp())
        if last_year < 9999
        else 253402300799
    )

    def offset(ts: int) -> timedelta:
        value = datetime.fromtimestamp(ts, tz=zone).utcoffset()
        return value if value is not None else timedelta(0)

    day = 86_400
    previous = offset(start)
    t = start
    while t < end:
        nxt = min(t + day, end)
        current = offset(nxt)
        if current != previous:
            lo, hi = t, nxt
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if offset(mid) == previous:
                    lo = mid
                else:
                    hi = mid
            instant = datetime.fromtimestamp(hi, tz=UTC).replace(tzinfo=None)
            out.append((instant, previous, current))
            previous = current
        t = nxt
    return out


def local_intervals(
    zone: ZoneInfo, first_year: int, last_year: int
) -> tuple[list[Interval], list[Interval]]:
    """Nonexistent (skipped) and ambiguous (repeated) local time ranges, half-open."""
    gaps: list[Interval] = []
    overlaps: list[Interval] = []
    for instant, before, after in transitions(zone, first_year, last_year):
        if after > before:
            gaps.append((instant + before, instant + after))
        else:
            overlaps.append((instant + after, instant + before))
    return gaps, overlaps


def _midnights(intervals: list[Interval]) -> list[date]:
    days: list[date] = []
    for low, high in intervals:
        midnight = datetime.combine(low.date(), time())
        if midnight < low:
            midnight += timedelta(days=1)
        if midnight < high:
            days.append(midnight.date())
    return days


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_datetime(value)
    except (TypeError, ValueError):
        return None


class DstEdgeCases(Check):
    """Local timestamps that do not exist (spring forward) or are ambiguous (fall back) in a
    time zone (MIG-11)."""

    dimension = Dimension.VALIDITY
    Config = cfg.DstEdgeCasesCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.DstEdgeCasesCheck)
        assert ctx.schema is not None
        zone_name = config.timezone or ctx.normalization.timezone
        d = ctx.dialect
        counts = {"nonexistent": 0, "ambiguous": 0}
        total = 0
        metrics: list[MetricComparison] = []
        notes: list[str] = []
        sample_tables: list[pa.Table] = []
        limit = max(ctx.sample_size, ctx.extract_rows)
        if zone_name == "UTC":
            return CheckOutcome(
                passed=True,
                message="the time zone is UTC, which has no daylight saving time",
                details={"timezone": zone_name},
            )
        zone = ZoneInfo(zone_name)
        for name in config.columns:
            col = ctx.col(name)
            found = ctx.schema.column(name)
            assert found is not None
            if found.type.logical is LogicalType.TIMESTAMP_TZ:
                notes.append(f"{name} holds instants with an offset; DST edge cases do not apply")
                continue
            is_date = found.type.logical is LogicalType.DATE
            bounds = ctx.row(
                f"SELECT COUNT(*) AS n, MIN({col}) AS lo, MAX({col}) AS hi\n"
                f"{ctx.from_where(f'{col} IS NOT NULL')}"
            )
            rows = as_int(bounds.get("n"))
            total += rows
            lo, hi = _as_datetime(bounds.get("lo")), _as_datetime(bounds.get("hi"))
            if not rows or lo is None or hi is None:
                continue
            first, last = max(lo.year, config.min_year), min(hi.year, config.max_year)
            if first > last:
                continue
            gaps, overlaps = local_intervals(zone, first, last)
            conditions: dict[str, str] = {}
            for rule, intervals in (("nonexistent", gaps), ("ambiguous", overlaps)):
                if rule not in config.rules:
                    continue
                if is_date:
                    # A DATE becomes midnight when converted to a timestamp; only dates whose
                    # midnight is skipped or repeated are affected.
                    days = [x for x in _midnights(intervals) if lo.date() <= x <= hi.date()]
                    if days:
                        conditions[rule] = f"{col} IN ({', '.join(d.literal(x) for x in days)})"
                    continue
                parts = [
                    f"({col} >= {bound_literal(a, d)} AND {col} < {bound_literal(b, d)})"
                    for a, b in intervals
                    if b > lo and a <= hi
                ]
                if parts:
                    conditions[rule] = "(" + " OR ".join(parts) + ")"
            if not conditions:
                metrics.append(
                    MetricComparison(
                        metric="dst_edge_cases", column=name, source=rows, target=0, passed=True
                    )
                )
                continue
            sums = ", ".join(f"{d.count_if(c)} AS {r}" for r, c in conditions.items())
            found_counts = ctx.row(f"SELECT {sums}\n{ctx.from_where()}")
            column_bad = 0
            for rule in conditions:
                n = as_int(found_counts.get(rule))
                counts[rule] += n
                column_bad += n
            metrics.append(
                MetricComparison(
                    metric="dst_edge_cases",
                    column=name,
                    source=rows,
                    target=column_bad,
                    passed=column_bad == 0,
                    note="values checked, values in DST transitions",
                )
            )
            if column_bad and limit > 0 and not ctx.privacy.strict:
                label = " ".join(f"WHEN {c} THEN {d.literal(r)}" for r, c in conditions.items())
                any_rule = " OR ".join(f"({c})" for c in conditions.values())
                sample_tables.append(
                    ctx.query(
                        d.limit(
                            f"SELECT {ctx.sample_columns([name])}, {d.literal(name)} AS dvf_column, "
                            f"CASE {label} END AS dvf_rule\n{ctx.from_where(any_rule)}",
                            limit,
                        )
                    )
                )
        unexpected = sum(counts.values())
        samples = extract = None
        if sample_tables:
            merged = pa.concat_tables(
                [
                    t.cast(pa.schema([(f.name, pa.string()) for f in t.schema]))
                    for t in sample_tables
                ],
                promote_options="permissive",
            )
            samples, extract = masked_evidence(
                merged, ctx.sample_size, ctx.extract_rows, ctx.privacy
            )
        parts = [f"{counts[r]:,} {r}" for r in ("nonexistent", "ambiguous") if counts[r]]
        message = (
            f"{' and '.join(parts)} local timestamps in {zone_name}"
            if parts
            else f"no local timestamps fall into {zone_name} DST transitions ({total:,} checked)"
        )
        hints: list[str] = []
        if counts["nonexistent"]:
            hints.append(
                "Nonexistent local times have no UTC instant; converting them shifts them by the "
                "DST offset, differently in each system. Fix them at the source or agree a rule."
            )
        if counts["ambiguous"]:
            hints.append(
                "Ambiguous local times occur twice; a UTC conversion must pick one occurrence "
                "(dvf's Python reference picks the first). Check that both systems pick the same."
            )
        return CheckOutcome(
            passed=mostly_passes(unexpected, total, config.mostly),
            message=message,
            observed=dict(counts),
            expected=0,
            unexpected_count=unexpected,
            total_count=total,
            samples=samples,
            extract=extract,
            metrics=metrics,
            hints=hints + notes,
            details={"timezone": zone_name, "violations": counts},
        )


__all__ = ["DstEdgeCases", "local_intervals", "transitions"]
