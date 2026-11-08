"""Built-in comparison strategies (BUILD_PROMPT.md section 7.3).

* ``row_count`` (REC-01): total and grouped row counts.
* ``aggregate`` (REC-02): control totals per column, overall or grouped.
* ``profile`` (REC-12): column profiles side by side.
* ``row_hash`` (REC-03, REC-04): key reconciliation and per-row canonical hashes.
* ``full_diff`` (REC-03, REC-05): key reconciliation and column-level differences with
  classification and side-by-side samples.

Counts, totals, and profiles are pushed down to each database; row-level strategies use the
configured engine.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dvf.checks.context import CheckOutcome, pct
from dvf.compare.buckets import search_and_diff
from dvf.compare.classify import classify_pair, summarize
from dvf.compare.context import ComparisonContext
from dvf.compare.partitions import plan_partitions, restricted
from dvf.compare.plan import PlannedColumn
from dvf.core.enums import Dimension, Status
from dvf.core.errors import PlanError
from dvf.engines.base import DiffJob, DiffMode, DiffResult, DiffSide, Engine, merge_results
from dvf.execution.context import QueryRecorder
from dvf.models.results import (
    ColumnMismatch,
    ColumnProfile,
    MetricComparison,
    ProfileComparison,
    RowCounts,
    SampleSet,
    TimingBreakdown,
)
from dvf.normalize.canonical import Kind, canonical_text, resolve_pair
from dvf.plugins.base import Comparator
from dvf.plugins.registry import PluginKind
from dvf.security.masking import to_json_safe
from dvf.sql.dialects import Dialect

MAX_GROUPS = 10_000
MAX_GROUP_METRICS = 500
_NUMERIC_KINDS = (Kind.INTEGER, Kind.DECIMAL, Kind.FLOAT)
_ORDERED_KINDS = (*_NUMERIC_KINDS, Kind.DATE, Kind.TIMESTAMP, Kind.TIME)


def _outcome(kind: str, **kwargs: Any) -> CheckOutcome:
    return CheckOutcome(type=f"compare.{kind}", dimension=Dimension.RECONCILIATION, **kwargs)


def _timing(recorder_source: float, recorder_target: float) -> TimingBreakdown:
    return TimingBreakdown(
        source_query_ms=round(recorder_source, 3),
        target_query_ms=round(recorder_target, 3),
        total_ms=round(recorder_source + recorder_target, 3),
    )


# ====================================================================== grouping


@dataclass(frozen=True)
class GroupColumn:
    label: str
    source_sql: str
    target_sql: str


def group_columns(ctx: ComparisonContext) -> list[GroupColumn]:
    """Canonical SQL per side for ``comparison.group_by`` columns (target or source names)."""
    groups: list[GroupColumn] = []
    for name in ctx.table.comparison.group_by:
        pair = ctx.pairing.pair_for_target(name) or ctx.pairing.target_for_source(name)
        if pair is None or pair.source is None:
            raise PlanError(
                f"group_by column '{name}' is not a paired column of table '{ctx.table.label}'.",
                hint="Use a column present on both sides (or mapped under columns.mappings).",
            )
        spec = resolve_pair(pair.source.type, pair.target.type, ctx.normalization)
        from dvf.normalize.canonical import canonical_sql

        sd, td = ctx.source.connector.sql_dialect, ctx.target.connector.sql_dialect
        groups.append(
            GroupColumn(
                label=pair.target.name,
                source_sql=canonical_sql(sd.quote(pair.source.name), spec.source, sd),
                target_sql=canonical_sql(td.quote(pair.target.name), spec.target, td),
            )
        )
    return groups


def _grouped_select(items: list[str], groups: list[str], relation: str, dialect: Dialect) -> str:
    select = [f"{g} AS g{i}" for i, g in enumerate(groups)] + items
    sql = f"SELECT {', '.join(select)}\nFROM {relation}"
    if groups:
        sql += "\nGROUP BY " + ", ".join(groups)
        sql = dialect.limit(sql, MAX_GROUPS + 1)
    return sql


def _group_key(row: dict[str, Any], n: int) -> tuple[Any, ...]:
    return tuple(row.get(f"g{i}") for i in range(n))


# ====================================================================== row_count


class RowCountComparator(Comparator):
    """Compare total and grouped row counts."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        recorder = ctx.recorder()
        groups = group_columns(ctx)
        sd, td = ctx.source.connector.sql_dialect, ctx.target.connector.sql_dialect
        src_rows, tgt_rows = ctx.run_both(
            recorder,
            _grouped_select(
                ["COUNT(*) AS n"], [g.source_sql for g in groups], ctx.source.relation.sql, sd
            ),
            _grouped_select(
                ["COUNT(*) AS n"], [g.target_sql for g in groups], ctx.target.relation.sql, td
            ),
        )
        n = len(groups)
        source_counts = {_group_key(r, n): int(r["n"]) for r in src_rows}
        target_counts = {_group_key(r, n): int(r["n"]) for r in tgt_rows}
        source_total, target_total = sum(source_counts.values()), sum(target_counts.values())
        ctx.dataset.row_counts = RowCounts(source=source_total, target=target_total)
        pct_tol = ctx.table.comparison.tolerance.row_count_pct
        allowed = abs(source_total) * (pct_tol or 0.0) / 100.0
        difference = target_total - source_total
        passed = abs(difference) <= allowed
        metrics = [
            MetricComparison(
                metric="row_count",
                source=source_total,
                target=target_total,
                difference=float(difference),
                tolerance=allowed if pct_tol else None,
                passed=passed,
            )
        ]
        group_failures = 0
        notes: list[str] = []
        if groups:
            labels = [g.label for g in groups]
            for key in sorted(
                set(source_counts) | set(target_counts), key=lambda k: tuple(str(v) for v in k)
            ):
                s, t = source_counts.get(key, 0), target_counts.get(key, 0)
                if s == t:
                    continue
                group_failures += 1
                if group_failures <= MAX_GROUP_METRICS:
                    metrics.append(
                        MetricComparison(
                            metric="row_count",
                            group=dict(zip(labels, key, strict=True)),
                            source=s,
                            target=t,
                            difference=float(t - s),
                            passed=False,
                        )
                    )
            if len(source_counts) > MAX_GROUPS or len(target_counts) > MAX_GROUPS:
                notes.append(f"more than {MAX_GROUPS:,} groups; only the first were compared")
        passed = passed and group_failures == 0
        if difference == 0:
            message = f"{source_total:,} rows on both sides"
        else:
            message = f"source {source_total:,} rows, target {target_total:,} rows (difference {difference:+,})"
        if group_failures:
            message += f"; {group_failures:,} of {len(set(source_counts) | set(target_counts)):,} groups differ"
        return [
            _outcome(
                "row_count",
                passed=passed,
                message=message,
                observed=target_total,
                expected=source_total,
                unexpected_count=abs(difference),
                total_count=source_total,
                metrics=metrics,
                queries=recorder.records,
                timing=_timing(recorder.source_ms, recorder.target_ms),
                details={"groups_compared": len(set(source_counts) | set(target_counts))}
                if groups
                else {},
                hints=notes,
            )
        ]


# ====================================================================== aggregate and profile


def _kind(col: PlannedColumn) -> Kind | None:
    """The comparable kind of a column when both sides agree, else ``None``."""
    s, t = col.pair_spec.source.kind, col.pair_spec.target.kind
    if s is t:
        return s
    if s in _NUMERIC_KINDS and t in _NUMERIC_KINDS:
        return Kind.FLOAT
    return None


def _aggregate_items(
    col: PlannedColumn, i: int, side: str, wanted: Sequence[str], dialect: Dialect
) -> list[str]:
    expr = col.source if side == "source" else col.target
    kind = _kind(col)
    items: list[str] = []
    for agg in wanted:
        if agg == "count":
            items.append(f"COUNT({expr.canonical}) AS a{i}_count")
        elif agg == "null_count":
            items.append(
                f"SUM(CASE WHEN {expr.canonical} IS NULL THEN 1 ELSE 0 END) AS a{i}_null_count"
            )
        elif agg == "count_distinct":
            items.append(f"COUNT(DISTINCT {expr.canonical}) AS a{i}_count_distinct")
        elif agg in ("sum", "avg") and kind in _NUMERIC_KINDS:
            value = (
                dialect.to_double(expr.normalized)
                if kind is Kind.FLOAT or agg == "avg"
                else expr.normalized
            )
            items.append(f"{agg.upper()}({value}) AS a{i}_{agg}")
        elif agg in ("min", "max") and kind in _ORDERED_KINDS:
            items.append(f"{agg.upper()}({expr.normalized}) AS a{i}_{agg}")
        elif agg == "sum_length" and kind is Kind.TEXT:
            items.append(f"SUM({dialect.length(expr.canonical)}) AS a{i}_sum_length")
    return items


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value))
    except ArithmeticError:
        return None


def _compare_metric(
    agg: str, col: PlannedColumn, s: Any, t: Any, rows: int, float_rel: float
) -> tuple[bool, float | None, float | None, Any, Any]:
    """(passed, difference, tolerance, shown source, shown target)."""
    kind = _kind(col)
    if agg in ("min", "max") and kind in (Kind.DATE, Kind.TIMESTAMP, Kind.TIME):
        cs = None if s is None else canonical_text(s, col.source.spec)
        ct = None if t is None else canonical_text(t, col.target.spec)
        return cs == ct, None, None, cs, ct
    ds, dt = _number(s), _number(t)
    if ds is None or dt is None:
        return (
            ds == dt and s == t if ds is None and dt is None else False,
            None,
            None,
            to_json_safe(s),
            to_json_safe(t),
        )
    numeric = col.pair_spec.numeric
    abs_tol = numeric.abs_tol if numeric is not None else 0.0
    rel_tol = numeric.rel_tol if numeric is not None else 0.0
    inexact = kind is Kind.FLOAT or agg == "avg" or isinstance(s, float) or isinstance(t, float)
    if inexact:
        rel_tol = max(rel_tol, float_rel, 1e-12)
    if agg == "sum":
        abs_tol *= max(rows, 1)
    if agg in ("count", "null_count", "count_distinct", "sum_length"):
        abs_tol, rel_tol = 0.0, 0.0
    diff = dt - ds
    allowed = max(Decimal(repr(abs_tol)), Decimal(repr(rel_tol)) * max(abs(ds), abs(dt)))
    return (
        abs(diff) <= allowed,
        float(diff),
        float(allowed) if allowed else None,
        to_json_safe(s),
        to_json_safe(t),
    )


class AggregateComparator(Comparator):
    """Compare control totals per column (count, sum, min, max, distinct, nulls), overall or grouped."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        plan = ctx.columns_plan
        if plan is None:
            raise ctx.plan_error or PlanError("no columns to aggregate")
        columns = plan.keys + plan.columns
        wanted = list(ctx.table.comparison.aggregates)
        recorder = ctx.recorder()
        groups = group_columns(ctx)
        sd, td = plan.source_dialect, plan.target_dialect
        s_items = [
            x for i, c in enumerate(columns) for x in _aggregate_items(c, i, "source", wanted, sd)
        ]
        t_items = [
            x for i, c in enumerate(columns) for x in _aggregate_items(c, i, "target", wanted, td)
        ]
        s_items.insert(0, "COUNT(*) AS dvf_rows")
        t_items.insert(0, "COUNT(*) AS dvf_rows")
        src_rows, tgt_rows = ctx.run_both(
            recorder,
            _grouped_select(s_items, [g.source_sql for g in groups], ctx.source.relation.sql, sd),
            _grouped_select(t_items, [g.target_sql for g in groups], ctx.target.relation.sql, td),
        )
        n = len(groups)
        src = {_group_key(r, n): r for r in src_rows}
        tgt = {_group_key(r, n): r for r in tgt_rows}
        float_rel = ctx.normalization.float_compare.rel_tol
        metrics: list[MetricComparison] = []
        failures: list[str] = []
        compared = 0
        labels = [g.label for g in groups]
        for key in sorted(set(src) | set(tgt), key=lambda k: tuple(str(v) for v in k)):
            s_row, t_row = src.get(key, {}), tgt.get(key, {})
            rows = int(s_row.get("dvf_rows") or 0)
            group = dict(zip(labels, key, strict=True)) if groups else None
            for i, col in enumerate(columns):
                for agg in wanted:
                    name = f"a{i}_{agg}"
                    if name not in s_row and name not in t_row:
                        continue
                    compared += 1
                    passed, diff, tol, shown_s, shown_t = _compare_metric(
                        agg, col, s_row.get(name), t_row.get(name), rows, float_rel
                    )
                    if not passed:
                        failures.append(f"{agg}({col.label})" + (f" for {group}" if group else ""))
                    if not groups or (not passed and len(metrics) < MAX_GROUP_METRICS):
                        metrics.append(
                            MetricComparison(
                                metric=agg,
                                column=col.label,
                                source_column=col.source_label,
                                group=group,
                                source=shown_s,
                                target=shown_t,
                                difference=diff,
                                tolerance=tol,
                                passed=passed,
                            )
                        )
        unique_failures = list(dict.fromkeys(failures))
        metric_names = list(dict.fromkeys(f.split(" for ")[0] for f in unique_failures))
        failed_groups = len({f.split(" for ", 1)[1] for f in unique_failures if " for " in f})
        message = (
            f"{len(unique_failures):,} of {compared:,} control totals differ"
            + (f" in {failed_groups:,} of {len(set(src) | set(tgt)):,} groups" if groups else "")
            + f": {', '.join(metric_names[:8])}"
            + (" …" if len(metric_names) > 8 else "")
            if unique_failures
            else f"all {compared:,} control totals match"
        )
        return [
            _outcome(
                "aggregate",
                passed=not unique_failures,
                message=message,
                unexpected_count=len(unique_failures),
                total_count=compared,
                metrics=metrics,
                queries=recorder.records,
                timing=_timing(recorder.source_ms, recorder.target_ms),
                details={"aggregates": wanted, "groups_compared": len(set(src) | set(tgt))}
                if groups
                else {"aggregates": wanted},
            )
        ]


_PROFILE_AGGS = ["count", "null_count", "count_distinct", "min", "max", "avg", "sum_length"]


def _profile_items(col: PlannedColumn, i: int, side: str, dialect: Dialect) -> list[str]:
    expr = col.source if side == "source" else col.target
    items = _aggregate_items(
        col, i, side, ["null_count", "count_distinct", "min", "max", "avg"], dialect
    )
    if _kind(col) is Kind.TEXT:
        length = dialect.length(expr.canonical)
        items += [
            f"MIN({length}) AS a{i}_min_length",
            f"MAX({length}) AS a{i}_max_length",
            f"AVG({length}) AS a{i}_avg_length",
        ]
    return items


def _profile(
    row: dict[str, Any], i: int, total: int, col: PlannedColumn, spec_side: str
) -> ColumnProfile:
    spec = col.source.spec if spec_side == "source" else col.target.spec
    kind = _kind(col)

    def show(value: Any) -> Any:
        if value is None:
            return None
        if kind in (Kind.DATE, Kind.TIMESTAMP, Kind.TIME):
            return canonical_text(value, spec)
        return to_json_safe(value)

    nulls = int(row.get(f"a{i}_null_count") or 0)
    avg = row.get(f"a{i}_avg")
    avg_len = row.get(f"a{i}_avg_length")
    return ColumnProfile(
        count=total,
        null_count=nulls,
        null_pct=pct(nulls, total),
        distinct_count=row.get(f"a{i}_count_distinct"),
        min=show(row.get(f"a{i}_min")),
        max=show(row.get(f"a{i}_max")),
        mean=None if avg is None else float(avg),
        min_length=row.get(f"a{i}_min_length"),
        max_length=row.get(f"a{i}_max_length"),
        avg_length=None if avg_len is None else round(float(avg_len), 6),
    )


def _profile_differences(s: ColumnProfile, t: ColumnProfile, rel: float) -> list[str]:
    diffs: list[str] = []
    for field_name in ("null_count", "distinct_count", "min", "max", "min_length", "max_length"):
        a, b = getattr(s, field_name), getattr(t, field_name)
        if a != b and not (_number(a) is not None and _number(a) == _number(b)):
            diffs.append(f"{field_name.replace('_', ' ')}: {a} vs {b}")
    for field_name in ("mean", "avg_length"):
        a, b = getattr(s, field_name), getattr(t, field_name)
        if (a is None) != (b is None) or (
            a is not None and b is not None and abs(a - b) > max(1e-9, rel * max(abs(a), abs(b)))
        ):
            diffs.append(f"{field_name.replace('_', ' ')}: {a} vs {b}")
    return diffs


class ProfileComparator(Comparator):
    """Compare column profiles side by side (informational: differences are warnings)."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        plan = ctx.columns_plan
        if plan is None:
            raise ctx.plan_error or PlanError("no columns to profile")
        columns = plan.keys + plan.columns
        recorder = ctx.recorder()
        sd, td = plan.source_dialect, plan.target_dialect
        s_items = ["COUNT(*) AS dvf_rows"] + [
            x for i, c in enumerate(columns) for x in _profile_items(c, i, "source", sd)
        ]
        t_items = ["COUNT(*) AS dvf_rows"] + [
            x for i, c in enumerate(columns) for x in _profile_items(c, i, "target", td)
        ]
        src_rows, tgt_rows = ctx.run_both(
            recorder,
            f"SELECT {', '.join(s_items)}\nFROM {ctx.source.relation.sql}",
            f"SELECT {', '.join(t_items)}\nFROM {ctx.target.relation.sql}",
        )
        s_row, t_row = src_rows[0], tgt_rows[0]
        s_total, t_total = int(s_row["dvf_rows"] or 0), int(t_row["dvf_rows"] or 0)
        rel = ctx.normalization.float_compare.rel_tol
        profiles: list[ProfileComparison] = []
        differing: list[str] = []
        for i, col in enumerate(columns):
            sp = _profile(s_row, i, s_total, col, "source")
            tp = _profile(t_row, i, t_total, col, "target")
            diffs = _profile_differences(sp, tp, rel)
            if diffs:
                differing.append(col.label)
            profiles.append(
                ProfileComparison(
                    column=col.label,
                    source_column=col.source_label,
                    source=sp,
                    target=tp,
                    differences=diffs,
                )
            )
        ctx.dataset.profiles = profiles
        return [
            _outcome(
                "profile",
                passed=not differing,
                severity_cap="warning",
                message=(
                    f"profiles differ for {len(differing)} of {len(columns)} column(s): {', '.join(differing[:10])}"
                    if differing
                    else f"profiles match for all {len(columns)} columns"
                ),
                unexpected_count=len(differing),
                total_count=len(columns),
                columns=differing,
                queries=recorder.records,
                timing=_timing(recorder.source_ms, recorder.target_ms),
            )
        ]


# ====================================================================== row-level


def _engine(ctx: ComparisonContext) -> Engine:
    entry = ctx.registry.get(PluginKind.ENGINE, ctx.engine)
    if not entry.ready:
        raise PlanError(
            f"Engine '{ctx.engine}' is not implemented in this version.",
            hint="Use engine 'auto', 'pushdown', 'duckdb', or 'polars'.",
        )
    implementation = entry.implementation
    engine = implementation() if isinstance(implementation, type) else implementation
    assert isinstance(engine, Engine)
    return engine


MAX_RECORDED_QUERIES = 60
"""Queries kept on one result; partitioned and drill-down comparisons may run many more."""


def _will_full_diff(ctx: ComparisonContext) -> bool:
    comparison = ctx.table.comparison
    return "full_diff" in comparison.strategies or comparison.on_mismatch == "full_diff"


def _strategy_diff(
    strategy: str, ctx: ComparisonContext, engine: Engine, job: DiffJob
) -> DiffResult:
    comparison = ctx.table.comparison
    if strategy in ("hash_bucket", "bisect"):
        return search_and_diff(
            job.plan,
            job.source,
            job.target,
            "hash" if strategy == "hash_bucket" else "key",
            buckets=comparison.buckets,
            fanout=comparison.drilldown_fanout,
            threshold=comparison.drilldown_threshold,
            engine=engine,
            job_template=job,
        )
    return engine.diff(job)


def _run_diff(
    ctx: ComparisonContext, mode: DiffMode, strategy: str = "row_hash"
) -> tuple[DiffResult, DiffJob] | CheckOutcome:
    """Run (or reuse) the row-level diff of ``ctx`` for ``strategy``, partition by partition."""
    plan = ctx.plan
    if plan is None:
        error = ctx.plan_error
        return _outcome(
            "keys",
            status=Status.SKIPPED,
            message=str(error) if error else "row-level comparison needs key columns",
            hints=[error.hint]
            if error and error.hint
            else ["Set 'keys' or 'keys: auto' on the table."],
        )
    cached = ctx.state.get(f"diff:{mode}") or (
        ctx.state.get("diff:full") if mode == "hash" else None
    )
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    engine = _engine(ctx)
    partitions = plan_partitions(ctx, plan)
    partitioned = ctx.table.comparison.partitions is not None
    recorder = QueryRecorder()
    parts: list[DiffResult] = []
    part_details: list[dict[str, Any]] = []
    for partition in partitions:
        key = f"{strategy}.{mode}.{partition.name}"
        saved = ctx.checkpoints.load(key) if (partitioned and ctx.checkpoints) else None
        if saved is not None:
            part = DiffResult.from_dict(saved)
        else:
            job = DiffJob(
                plan=plan,
                source=DiffSide(
                    ctx.source.connector,
                    restricted(ctx.source.relation, partition.source, plan.source_dialect),
                ),
                target=DiffSide(
                    ctx.target.connector,
                    restricted(ctx.target.relation, partition.target, plan.target_dialect),
                ),
                mode=mode,
                sample_size=ctx.sample_size,
                max_mismatch_rows=ctx.table.comparison.max_mismatch_rows,
                recorder=recorder,
                resources=ctx.resources,
            )
            part = _strategy_diff(strategy, ctx, engine, job)
            if partitioned and ctx.checkpoints is not None:
                ctx.checkpoints.save(key, part.to_dict())
        parts.append(part)
        if partitioned:
            summary = part.summary
            part_details.append(
                {
                    "name": partition.name,
                    "status": "passed" if part.rows_with_differences == 0 else "failed",
                    "source_rows": part.source_rows,
                    "target_rows": part.target_rows,
                    "matched": summary.matched,
                    "mismatched": summary.mismatched,
                    "missing_in_target": summary.missing_in_target,
                    "extra_in_target": summary.extra_in_target,
                    "from_checkpoint": saved is not None,
                }
            )
    if partitioned:
        result = merge_results(
            parts,
            parts[0].engine if parts else engine.name,
            ctx.sample_size,
            ctx.table.comparison.max_mismatch_rows,
        )
        result.details["partitions"] = part_details
        column = ctx.table.comparison.partitions.column  # type: ignore[union-attr]
        if column and result.summary.missing_in_target and result.summary.extra_in_target:
            result.notes.append(
                f"Rows whose '{column}' differs between source and target count as missing in one "
                "partition and extra in another; partition by a column that does not change, such "
                "as a key or a load date."
            )
        for name in ("rows_transferred", "summary_rows_transferred"):
            values = [p.details[name] for p in parts if name in p.details]
            if values:
                result.details[name] = sum(values)
        if parts and "strategy" in parts[0].details:
            result.details["strategy"] = parts[0].details["strategy"]
    else:
        result = parts[0]
    job = DiffJob(
        plan=plan,
        source=ctx.source,
        target=ctx.target,
        mode=mode,
        sample_size=ctx.sample_size,
        max_mismatch_rows=ctx.table.comparison.max_mismatch_rows,
        recorder=recorder,
        resources=ctx.resources,
    )
    if len(recorder.records) > MAX_RECORDED_QUERIES:
        result.notes.append(
            f"{len(recorder.records):,} queries ran; the first {MAX_RECORDED_QUERIES} are recorded."
        )
        recorder.records = recorder.records[:MAX_RECORDED_QUERIES]
    ctx.state[f"diff:{mode}"] = (result, job)
    ctx.dataset.reconciliation = result.summary
    if ctx.dataset.row_counts.source is None:
        ctx.dataset.row_counts = RowCounts(source=result.source_rows, target=result.target_rows)
    return result, job


def _key_outcome(ctx: ComparisonContext, result: DiffResult, job: DiffJob) -> CheckOutcome:
    s = result.summary
    problems = {
        "missing in target": s.missing_in_target,
        "extra in target": s.extra_in_target,
        "duplicate keys in source": s.duplicate_keys_source,
        "duplicate keys in target": s.duplicate_keys_target,
        "NULL keys in source": s.null_keys_source,
        "NULL keys in target": s.null_keys_target,
    }
    found = {k: v for k, v in problems.items() if v}
    labels = [k.label for k in job.plan.keys]
    samples = None
    if result.key_samples and not ctx.privacy.strict:
        columns = ["issue", *labels, "rows"]
        rows: list[list[Any]] = []
        for kind, values in result.key_samples.items():
            rows += [[kind, *v] for v in values]
        samples = SampleSet(
            columns=columns,
            rows=ctx.privacy.mask_rows(columns, rows),
            masked=ctx.privacy.mask_pii,
            truncated=any(len(v) >= ctx.sample_size for v in result.key_samples.values()),
        )
    message = (
        "; ".join(f"{v:,} {k}" for k, v in found.items())
        if found
        else f"all {s.matched + s.mismatched:,} keys match one to one"
    )
    hints = [n for n in result.notes if "partition" in n]
    if s.duplicate_keys_source or s.duplicate_keys_target:
        hints.append("Duplicate keys are compared using one row per key; check the key definition.")
    return _outcome(
        "keys",
        passed=not found,
        message=message,
        observed={k.replace(" ", "_"): v for k, v in problems.items()},
        unexpected_count=sum(found.values()),
        total_count=result.source_rows,
        columns=labels,
        samples=samples,
        hints=hints,
        engine=result.engine,
        normalization=job.plan.describe(),
        queries=job.recorder.records,
        timing=result.timing,
        details={
            "source_rows": result.source_rows,
            "target_rows": result.target_rows,
            "match_pct": s.match_pct,
            **(
                {"partitions": result.details["partitions"]}
                if "partitions" in result.details
                else {}
            ),
        },
    )


class RowHashComparator(Comparator):
    """Pair rows by key and compare a hash of their canonical columns."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        # When full_diff will run anyway, diff in full mode now: phase B only runs if rows differ,
        # and full_diff then reuses this result instead of comparing every row again.
        mode: DiffMode = "full" if _will_full_diff(ctx) else "hash"
        outcome = _run_diff(ctx, mode)
        if isinstance(outcome, CheckOutcome):
            return [outcome]
        result, job = outcome
        outcomes = []
        if not ctx.state.get("keys_reported"):
            outcomes.append(_key_outcome(ctx, result, job))
            ctx.state["keys_reported"] = True
        s = result.summary
        compared = s.matched + s.mismatched
        outcomes.append(
            _outcome(
                "row_hash",
                passed=s.mismatched == 0,
                message=(
                    f"{s.mismatched:,} of {compared:,} paired rows differ"
                    if s.mismatched
                    else f"all {compared:,} paired rows are identical"
                ),
                observed=s.mismatched,
                expected=0,
                unexpected_count=s.mismatched,
                total_count=compared,
                engine=result.engine,
                normalization=job.plan.describe(),
                timing=result.timing,
                hints=["Add 'full_diff' (or on_mismatch: full_diff) to see which columns differ."]
                if s.mismatched and not _will_full_diff(ctx)
                else [],
                details=dict(result.details),
            )
        )
        return outcomes


class FullDiffComparator(Comparator):
    """Column-level differences with classification and side-by-side samples."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        outcome = _run_diff(ctx, "full")
        if isinstance(outcome, CheckOutcome):
            return [outcome]
        result, job = outcome
        outcomes = []
        if not ctx.state.get("keys_reported"):
            outcomes.append(_key_outcome(ctx, result, job))
            ctx.state["keys_reported"] = True
        s = result.summary
        compared = s.matched + s.mismatched
        by_column: dict[str, list[Any]] = defaultdict(list)
        for row in result.mismatches:
            by_column[row.column].append(row)
        key_labels = [k.label for k in job.plan.keys]
        mismatches: list[ColumnMismatch] = []
        all_hints: list[str] = []
        for col in job.plan.columns:
            count = result.column_counts.get(col.label, 0)
            if not count:
                continue
            rows = by_column.get(col.label, [])
            labels = Counter(classify_pair(r.source_value, r.target_value) for r in rows)
            hints = summarize(labels, count)
            samples = None
            if rows and ctx.sample_size and not ctx.privacy.strict:
                columns = [
                    *key_labels,
                    f"source.{col.source_label}",
                    f"target.{col.label}",
                    "classification",
                ]
                sample_rows = [
                    [
                        *r.keys,
                        r.source_value,
                        r.target_value,
                        classify_pair(r.source_value, r.target_value),
                    ]
                    for r in rows[: ctx.sample_size]
                ]
                masked = []
                for raw in sample_rows:
                    masked.append(
                        [
                            *[
                                ctx.privacy.mask(k, v)
                                for k, v in zip(key_labels, raw[: len(key_labels)], strict=True)
                            ],
                            "***"
                            if col.pii and ctx.privacy.mask_pii
                            else ctx.privacy.mask(col.label, raw[-3]),
                            "***"
                            if col.pii and ctx.privacy.mask_pii
                            else ctx.privacy.mask(col.label, raw[-2]),
                            raw[-1],
                        ]
                    )
                samples = SampleSet(
                    columns=columns,
                    rows=masked,
                    masked=ctx.privacy.mask_pii,
                    truncated=len(rows) > ctx.sample_size,
                )
            mismatches.append(
                ColumnMismatch(
                    column=col.label,
                    source_column=col.source_label,
                    mismatch_count=count,
                    mismatch_pct=pct(count, compared) or 0.0,
                    classifications=dict(labels.most_common()),
                    hints=hints,
                    samples=samples,
                )
            )
            all_hints += [f"{col.label}: {h}" for h in hints[:1]]
        mismatches.sort(key=lambda m: -m.mismatch_count)
        total_values = compared * len(job.plan.columns)
        differing_values = sum(m.mismatch_count for m in mismatches)
        outcomes.append(
            _outcome(
                "column_mismatch",
                passed=not mismatches,
                message=(
                    f"{s.mismatched:,} of {compared:,} paired rows differ in {len(mismatches)} column(s): "
                    + ", ".join(f"{m.column} ({m.mismatch_count:,})" for m in mismatches[:8])
                    if mismatches
                    else f"all {len(job.plan.columns)} compared columns match in {compared:,} paired rows"
                ),
                observed=differing_values,
                expected=0,
                unexpected_count=s.mismatched,
                total_count=compared,
                column_mismatches=mismatches,
                columns=[m.column for m in mismatches],
                hints=all_hints,
                engine=result.engine,
                normalization=job.plan.describe(),
                timing=result.timing,
                details={
                    "values_compared": total_values,
                    "values_differing": differing_values,
                    **{
                        k: v
                        for k, v in result.details.items()
                        if k in ("partitions", "rows_transferred")
                    },
                },
                extract=_mismatch_extract(ctx, job, result),
            )
        )
        return outcomes


def _mismatch_extract(ctx: ComparisonContext, job: DiffJob, result: DiffResult) -> Any:
    """Every fetched mismatched value (up to max_mismatch_rows), masked, for an extract file."""
    if not result.mismatches or ctx.privacy.strict:
        return None
    from dvf.execution.artifacts import text_table

    key_labels = [k.label for k in job.plan.keys]
    pii = {c.label for c in job.plan.columns if c.pii}
    rows = []
    for r in result.mismatches:
        hidden = r.column in pii and ctx.privacy.mask_pii
        rows.append(
            [
                *[ctx.privacy.mask(k, v) for k, v in zip(key_labels, r.keys, strict=True)],
                r.column,
                "***" if hidden else ctx.privacy.mask(r.column, r.source_value),
                "***" if hidden else ctx.privacy.mask(r.column, r.target_value),
                classify_pair(r.source_value, r.target_value),
            ]
        )
    return text_table(
        [*key_labels, "column", "source_value", "target_value", "classification"], rows
    )


class _ChecksumComparator(Comparator):
    strategy = "hash_bucket"

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        mode: DiffMode = "full" if _will_full_diff(ctx) else "hash"
        outcome = _run_diff(ctx, mode, self.strategy)
        if isinstance(outcome, CheckOutcome):
            return [outcome]
        result, job = outcome
        outcomes = []
        if not ctx.state.get("keys_reported"):
            outcomes.append(_key_outcome(ctx, result, job))
            ctx.state["keys_reported"] = True
        s = result.summary
        compared = s.matched + s.mismatched
        details = dict(result.details)
        levels = details.get("levels") or []
        first = levels[0] if levels else {}
        transferred = details.get("rows_transferred")
        moved_pct = details.get("transfer_pct")
        unit = "buckets" if self.strategy == "hash_bucket" else "key segments"
        parts = [
            f"{s.mismatched:,} of {compared:,} paired rows differ"
            if s.mismatched
            else f"all {compared:,} paired rows are identical"
        ]
        if first:
            parts.append(
                f"{first.get('buckets_differing', 0):,} of {first.get('buckets_compared', 0):,} "
                f"{unit} differed at the first level, {len(levels)} level(s) searched"
            )
        if transferred is not None:
            parts.append(
                f"{transferred:,} rows transferred"
                + (f" ({moved_pct:.4g}% of rows)" if moved_pct is not None else "")
            )
        hints = list(result.notes)
        if s.mismatched and not _will_full_diff(ctx):
            hints.append("Add on_mismatch: full_diff to see which columns differ.")
        outcomes.append(
            _outcome(
                self.strategy,
                passed=s.mismatched == 0,
                message="; ".join(parts),
                observed=s.mismatched,
                expected=0,
                unexpected_count=s.mismatched,
                total_count=compared,
                engine=result.engine,
                normalization=job.plan.describe(),
                timing=result.timing,
                hints=hints,
                details=details,
            )
        )
        return outcomes


class HashBucketComparator(_ChecksumComparator):
    """Compare bucketed checksums in each database, then drill into mismatched buckets."""

    strategy = "hash_bucket"


class BisectComparator(_ChecksumComparator):
    """Recursive key-range checksums for mostly identical large tables (integer keys)."""

    strategy = "bisect"


BUILTIN_COMPARATORS: dict[str, type[Comparator]] = {
    "row_count": RowCountComparator,
    "aggregate": AggregateComparator,
    "profile": ProfileComparator,
    "row_hash": RowHashComparator,
    "hash_bucket": HashBucketComparator,
    "bisect": BisectComparator,
    "full_diff": FullDiffComparator,
}
