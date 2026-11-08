"""The ``nested`` comparison strategy: child rows reconciled per parent key.

A relational parent and child table (``orders`` and ``order_lines``) and a document with an
embedded array (``orders.lines``) hold the same child rows. Make both sides child rows — the
child table on one side, the unwound array on the other (``options.unwind``) — and set
``comparison.group_by`` to the parent key. For every parent key, each database computes:

* the number of child rows;
* the sum of every integer and decimal child column (canonical text, so it is exact);
* the count and sum of floating-point child columns (compared within the column's tolerance);
* two independent sums of 32-bit prefixes of each child row's canonical hash, so the child rows
  are compared as a set: their order does not matter, and duplicates do not cancel out.

The per-parent summaries (one small row per parent) are compared in a local DuckDB database, so
memory stays bounded. The result names each parent key that differs and why: missing or extra
on one side, a different number of child rows, different totals, or the same count and totals
with different content. ``on_mismatch: full_diff`` with the child key (for example
``[order_id, line_no]``) then shows the exact child rows and columns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import duckdb
import pyarrow as pa

from dvf.checks.context import CheckOutcome, pct
from dvf.compare.comparators import group_columns
from dvf.compare.context import ComparisonContext
from dvf.compare.plan import PlannedColumn, SideExpr
from dvf.core.enums import Dimension, Side
from dvf.core.errors import PlanError
from dvf.execution.context import QueryRecorder
from dvf.models.results import MetricComparison, RowCounts, SampleSet, TimingBreakdown
from dvf.normalize.canonical import Kind, row_hash_sql
from dvf.plugins.base import Comparator
from dvf.sql.dialects import Dialect

SideName = Literal["source", "target"]

MAX_METRICS = 500
"""Per-parent metrics kept on the result."""
MAX_EXAMPLES = 5
"""Parent keys named in the message."""
ISSUES = (
    "missing in target",
    "extra in target",
    "child rows differ",
    "totals differ",
    "content differs",
)


@dataclass(frozen=True)
class _Parent:
    label: str
    source: str
    target: str

    def sql(self, side: SideName) -> str:
        return self.source if side == "source" else self.target


def _expr(col: PlannedColumn, side: SideName) -> SideExpr:
    return col.source if side == "source" else col.target


def _summable(col: PlannedColumn) -> bool:
    s, t = col.pair_spec.source.kind, col.pair_spec.target.kind
    return s is t and s in (Kind.INTEGER, Kind.DECIMAL)


@dataclass
class _Layout:
    parents: list[_Parent]
    hashed: list[PlannedColumn]
    summed: list[PlannedColumn]
    tolerant: list[PlannedColumn]

    @property
    def names(self) -> list[str]:
        return [
            *(f"p{i}" for i in range(len(self.parents))),
            "n",
            "s1",
            "s2",
            *(f"v{j}" for j in range(len(self.summed))),
            *(x for j in range(len(self.tolerant)) for x in (f"fc{j}", f"fs{j}")),
        ]

    @property
    def local_types(self) -> list[str]:
        return [
            *("VARCHAR" for _ in self.parents),
            "BIGINT",
            "VARCHAR",
            "VARCHAR",
            *("VARCHAR" for _ in self.summed),
            *(x for _ in self.tolerant for x in ("BIGINT", "DOUBLE")),
        ]

    def side_sql(self, side: SideName, relation: str, d: Dialect) -> str:
        """Per-parent summaries, computed inside the side's database."""
        inner = [f"{p.sql(side)} AS p{i}" for i, p in enumerate(self.parents)]
        inner += [f"{_expr(c, side).canonical} AS c{j}" for j, c in enumerate(self.hashed)]
        inner += [f"{_expr(c, side).normalized} AS v{j}" for j, c in enumerate(self.summed)]
        inner += [f"{_expr(c, side).numeric} AS f{j}" for j, c in enumerate(self.tolerant)]
        inner_sql = f"SELECT {', '.join(inner)} FROM {relation}"
        keys = [f"p{i}" for i in range(len(self.parents))]
        row_hash = row_hash_sql([f"c{j}" for j in range(len(self.hashed))], d)
        mid = [*keys, f"{row_hash} AS h"]
        mid += [f"v{j}" for j in range(len(self.summed))]
        mid += [f"f{j}" for j in range(len(self.tolerant))]
        mid_sql = f"SELECT {', '.join(mid)} FROM {d.subquery(inner_sql, 'dvf_i')}"
        first = d.integer_text("SUM(" + d.hash_int("h", 0) + ")")
        second = d.integer_text("SUM(" + d.hash_int("h", 8) + ")")
        outer = [*keys, "COUNT(*) AS n", f"{first} AS s1", f"{second} AS s2"]
        for j, col in enumerate(self.summed):
            total = f"SUM(v{j})"
            spec = _expr(col, side).spec
            text = (
                d.integer_text(total)
                if spec.kind is Kind.INTEGER
                else d.decimal_text(total, spec.scale)
            )
            outer.append(f"{text} AS v{j}")
        for j in range(len(self.tolerant)):
            outer += [f"COUNT(f{j}) AS fc{j}", f"SUM(f{j}) AS fs{j}"]
        return (
            f"SELECT {', '.join(outer)}\nFROM {d.subquery(mid_sql, 'dvf_m')}\n"
            f"GROUP BY {', '.join(keys)}"
        )


class NestedComparator(Comparator):
    """Compare child rows per parent key: child counts, totals, and an order-independent hash of
    the canonical child rows (embedded arrays against child tables)."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        plan = ctx.columns_plan
        if plan is None:
            raise ctx.plan_error or PlanError("no columns to compare")
        if not ctx.table.comparison.group_by:
            raise PlanError(
                f"The nested strategy of table '{ctx.table.label}' needs comparison.group_by: "
                "the parent key column(s) of the child rows.",
                hint="Compare child rows on both sides (a child table, and an array unwound with "
                "options.unwind) and set, for example, group_by: [order_id].",
            )
        layout = _layout(ctx)
        recorder = ctx.recorder()
        con = _local_database(ctx)
        try:
            sides: tuple[tuple[SideName, Any, Side], ...] = (
                ("source", ctx.source, Side.SOURCE),
                ("target", ctx.target, Side.TARGET),
            )
            for side, diff_side, side_enum in sides:
                sql = layout.side_sql(side, diff_side.relation.sql, diff_side.connector.sql_dialect)
                _load(con, f"dvf_{side}", layout, diff_side.connector, sql, recorder, side_enum)
            return [_outcome(ctx, layout, con, recorder)]
        finally:
            con.close()


def _layout(ctx: ComparisonContext) -> _Layout:
    plan = ctx.columns_plan
    assert plan is not None
    columns = plan.keys + plan.columns
    by_label = {c.label.lower(): c for c in columns}
    by_source = {c.source_label.lower(): c for c in columns}
    fallback: dict[str, Any] | None = None
    parents: list[_Parent] = []
    for name in ctx.table.comparison.group_by:
        col = by_label.get(name.lower()) or by_source.get(name.lower())
        if col is not None:
            parents.append(_Parent(col.label, col.source.canonical, col.target.canonical))
            continue
        if fallback is None:
            fallback = {g.label.lower(): g for g in group_columns(ctx)}
        group = fallback.get(name.lower())
        if group is None:  # pragma: no cover - group_columns raised already
            raise PlanError(f"group_by column '{name}' is not a paired column")
        parents.append(_Parent(group.label, group.source_sql, group.target_sql))
    parent_labels = {p.label.lower() for p in parents}
    children = [c for c in columns if c.label.lower() not in parent_labels]
    hashed = [c for c in children if c.pair_spec.hashable]
    wanted = ctx.table.comparison.aggregates
    summed = [c for c in hashed if not c.is_key and _summable(c) and "sum" in wanted]
    tolerant = [c for c in children if not c.pair_spec.hashable]
    return _Layout(parents=parents, hashed=hashed, summed=summed, tolerant=tolerant)


def _local_database(ctx: ComparisonContext) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    resources = ctx.resources
    if resources.memory_limit:
        con.execute(f"SET memory_limit = '{resources.memory_limit}'")
    if resources.threads:
        con.execute(f"SET threads = {int(resources.threads)}")
    if resources.temp_dir:
        folder = resources.temp_dir.replace("'", "''")
        con.execute(f"SET temp_directory = '{folder}'")
    return con


def _load(
    con: duckdb.DuckDBPyConnection,
    table: str,
    layout: _Layout,
    connector: Any,
    sql: str,
    recorder: QueryRecorder,
    side: Side,
) -> None:
    names = layout.names
    columns = ", ".join(f"{n} {t}" for n, t in zip(names, layout.local_types, strict=True))
    con.execute(f"CREATE TABLE {table} ({columns})")
    casts = ", ".join(f"CAST({n} AS {t})" for n, t in zip(names, layout.local_types, strict=True))
    started = time.perf_counter()
    rows = 0
    try:
        for batch in connector.iter_batches(sql):
            if batch.num_rows == 0:
                continue
            frame = pa.Table.from_batches([batch]).rename_columns(names)
            con.register("dvf_batch", frame)
            try:
                con.execute(f"INSERT INTO {table} SELECT {casts} FROM dvf_batch")
            finally:
                con.unregister("dvf_batch")
            rows += batch.num_rows
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        recorder.add(connector, sql, side, elapsed, rows)


def _diff_sql(layout: _Layout) -> str:
    keys = [f"p{i}" for i in range(len(layout.parents))]
    join = " AND ".join(f"s.{k} IS NOT DISTINCT FROM t.{k}" for k in keys)
    select = [f"COALESCE(s.{k}, t.{k}) AS {k}" for k in keys]
    select += ["s.n AS sn", "t.n AS tn", "s.s1 AS ss1", "t.s1 AS ts1", "s.s2 AS ss2", "t.s2 AS ts2"]
    totals: list[str] = []
    for j in range(len(layout.summed)):
        select += [f"s.v{j} AS sv{j}", f"t.v{j} AS tv{j}"]
        totals.append(f"s.v{j} IS DISTINCT FROM t.v{j}")
    for j, col in enumerate(layout.tolerant):
        select += [
            f"s.fc{j} AS sfc{j}",
            f"t.fc{j} AS tfc{j}",
            f"s.fs{j} AS sfs{j}",
            f"t.fs{j} AS tfs{j}",
        ]
        abs_tol, rel_tol = _tolerance(col)
        within = (
            f"(s.fs{j} IS NULL AND t.fs{j} IS NULL) OR ABS(s.fs{j} - t.fs{j}) <= "
            f"GREATEST({abs_tol!r} * GREATEST(s.n, t.n), "
            f"{rel_tol!r} * GREATEST(ABS(s.fs{j}), ABS(t.fs{j})))"
        )
        totals.append(f"(s.fc{j} <> t.fc{j} OR NOT COALESCE({within}, FALSE))")
    totals_sql = " OR ".join(totals) if totals else "FALSE"
    issue = (
        "CASE WHEN t.n IS NULL THEN 'missing in target' "
        "WHEN s.n IS NULL THEN 'extra in target' "
        "WHEN s.n <> t.n THEN 'child rows differ' "
        f"WHEN {totals_sql} THEN 'totals differ' "
        "WHEN s.s1 <> t.s1 OR s.s2 <> t.s2 THEN 'content differs' "
        "ELSE NULL END"
    )
    select.append(f"{issue} AS issue")
    return f"SELECT {', '.join(select)} FROM dvf_source s FULL OUTER JOIN dvf_target t ON {join}"


def _tolerance(col: PlannedColumn) -> tuple[float, float]:
    """Absolute (per child row) and relative tolerance for sums of a floating-point column.

    Sums are accumulated in a different order by each database, so the relative tolerance is
    never below 1e-12."""
    numeric = col.pair_spec.numeric
    abs_tol = numeric.abs_tol if numeric is not None else 0.0
    rel_tol = numeric.rel_tol if numeric is not None else 0.0
    return abs_tol, max(rel_tol, 1e-12)


def _sums_differ(col: PlannedColumn, rows: int, s: float | None, t: float | None) -> bool:
    if s is None or t is None:
        return (s is None) != (t is None)
    abs_tol, rel_tol = _tolerance(col)
    return abs(s - t) > max(abs_tol * rows, rel_tol * max(abs(s), abs(t)))


def _number(text: Any) -> Decimal | None:
    try:
        return None if text is None else Decimal(str(text))
    except InvalidOperation:
        return None


def _outcome(
    ctx: ComparisonContext,
    layout: _Layout,
    con: duckdb.DuckDBPyConnection,
    recorder: QueryRecorder,
) -> CheckOutcome:
    con.execute(f"CREATE TABLE dvf_joined AS {_diff_sql(layout)}")
    total, differing, source_rows, target_rows = con.execute(
        "SELECT COUNT(*), COUNT(issue), "
        "(SELECT COALESCE(SUM(n), 0) FROM dvf_source), "
        "(SELECT COALESCE(SUM(n), 0) FROM dvf_target) FROM dvf_joined"
    ).fetchone() or (0, 0, 0, 0)
    by_issue = dict(
        con.execute(
            "SELECT issue, COUNT(*) FROM dvf_joined WHERE issue IS NOT NULL GROUP BY issue"
        ).fetchall()
    )
    keys = [f"p{i}" for i in range(len(layout.parents))]
    limit = max(ctx.sample_size, MAX_METRICS, MAX_EXAMPLES)
    if not ctx.privacy.strict:
        limit = max(limit, ctx.table.comparison.max_mismatch_rows)
    cursor = con.execute(
        f"SELECT * FROM dvf_joined WHERE issue IS NOT NULL ORDER BY {', '.join(keys)} "
        f"LIMIT {int(limit)}"
    )
    names = [d[0] for d in cursor.description or []]
    rows = [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]
    if ctx.dataset.row_counts.source is None:
        ctx.dataset.row_counts = RowCounts(source=int(source_rows), target=int(target_rows))

    labels = [p.label for p in layout.parents]
    privacy = ctx.privacy
    metrics: list[MetricComparison] = []
    sample_rows: list[list[Any]] = []
    examples: list[str] = []
    for row in rows:
        group = {label: row[k] for label, k in zip(labels, keys, strict=True)}
        reasons = _reasons(row, layout, ctx)
        shown_keys = [privacy.mask(label, row[k]) for label, k in zip(labels, keys, strict=True)]
        if len(examples) < MAX_EXAMPLES and not privacy.strict:
            key_text = ", ".join(
                f"{label}={v}" for label, v in zip(labels, shown_keys, strict=True)
            )
            examples.append(f"{key_text}: {'; '.join(reasons)}")
        sample_rows.append([*shown_keys, row["issue"], row["sn"], row["tn"], "; ".join(reasons)])
        if len(metrics) < MAX_METRICS:
            metrics.extend(_metrics(row, layout, group, privacy.mask_pii))
    metrics = metrics[:MAX_METRICS]

    columns = [*labels, "issue", "source_child_rows", "target_child_rows", "details"]
    samples = None
    extract = None
    if sample_rows and ctx.sample_size and not privacy.strict:
        samples = SampleSet(
            columns=columns,
            rows=sample_rows[: ctx.sample_size],
            masked=privacy.mask_pii,
            truncated=differing > ctx.sample_size,
        )
    if sample_rows and not privacy.strict:
        from dvf.execution.artifacts import text_table

        extract = text_table(columns, sample_rows)

    counts = [f"{by_issue[i]:,} {i}" for i in ISSUES if by_issue.get(i)]
    if differing:
        message = f"{differing:,} of {total:,} parent keys differ ({', '.join(counts)})"
        if examples:
            message += ": " + " | ".join(examples)
            if differing > len(examples):
                message += " | …"
    else:
        message = (
            f"all {total:,} parent keys match: {int(source_rows):,} child rows on each side, "
            f"compared by count, {len(layout.summed)} total(s), and a hash of "
            f"{len(layout.hashed)} column(s)"
        )
    hints: list[str] = []
    if (
        differing
        and ctx.table.comparison.on_mismatch != "full_diff"
        and "full_diff" not in (ctx.table.comparison.strategies)
    ):
        hints.append(
            "Add on_mismatch: full_diff with the child key (for example keys: [order_id, "
            "line_no]) to see the child rows and columns that differ."
        )
    if not layout.hashed and not layout.tolerant:
        hints.append("No child columns besides the parent key are paired; only counts compare.")
    details: dict[str, Any] = {
        "parent_keys": labels,
        "parents_compared": int(total),
        "parents_differing": int(differing),
        **{i.replace(" ", "_"): int(by_issue.get(i, 0)) for i in ISSUES},
        "source_child_rows": int(source_rows),
        "target_child_rows": int(target_rows),
        "hashed_columns": [c.label for c in layout.hashed],
        "summed_columns": [c.label for c in layout.summed],
        "tolerance_columns": [c.label for c in layout.tolerant],
    }
    plan = ctx.columns_plan
    return CheckOutcome(
        type="compare.nested",
        dimension=Dimension.RECONCILIATION,
        passed=differing == 0,
        message=message,
        observed=int(differing),
        expected=0,
        unexpected_count=int(differing),
        total_count=int(total),
        metrics=metrics,
        samples=samples,
        extract=extract,
        hints=hints,
        columns=labels,
        normalization=plan.describe() if plan is not None else [],
        queries=recorder.records,
        timing=TimingBreakdown(
            source_query_ms=round(recorder.source_ms, 3),
            target_query_ms=round(recorder.target_ms, 3),
            total_ms=round(recorder.source_ms + recorder.target_ms, 3),
        ),
        details={**details, "match_pct": pct(int(total) - int(differing), int(total))},
    )


def _reasons(row: dict[str, Any], layout: _Layout, ctx: ComparisonContext) -> list[str]:
    sn, tn = row["sn"], row["tn"]
    if tn is None:
        return [f"not in target (source has {sn:,} child rows)"]
    if sn is None:
        return [f"not in source (target has {tn:,} child rows)"]
    hide = ctx.privacy.mask_pii
    reasons: list[str] = []
    if sn != tn:
        reasons.append(f"child rows {sn:,} vs {tn:,}")
    for j, col in enumerate(layout.summed):
        s, t = row[f"sv{j}"], row[f"tv{j}"]
        if s != t:
            values = "differs" if (col.pii and hide) else f"{s} vs {t}"
            reasons.append(f"sum({col.label}) {values}")
    for j, col in enumerate(layout.tolerant):
        sc, tc, s, t = row[f"sfc{j}"], row[f"tfc{j}"], row[f"sfs{j}"], row[f"tfs{j}"]
        if sc != tc:
            reasons.append(f"non-NULL {col.label} {sc:,} vs {tc:,}")
        elif _sums_differ(col, max(sn, tn), s, t):
            values = "differs" if (col.pii and hide) else f"{s!r} vs {t!r}"
            reasons.append(f"sum({col.label}) {values}")
    if not reasons and (row["ss1"] != row["ts1"] or row["ss2"] != row["ts2"]):
        reasons.append("child rows differ in content (same count and totals)")
    return reasons or [str(row["issue"])]


def _metrics(
    row: dict[str, Any], layout: _Layout, group: dict[str, Any], hide_pii: bool
) -> list[MetricComparison]:
    sn, tn = row["sn"], row["tn"]
    found = []
    if sn is None or tn is None or sn != tn:
        found.append(
            MetricComparison(
                metric="child_rows",
                group=group,
                source=sn,
                target=tn,
                difference=None if sn is None or tn is None else float(tn - sn),
                passed=False,
                note=row["issue"],
            )
        )
    if sn is None or tn is None:
        return found
    for j, col in enumerate(layout.summed):
        s, t = row[f"sv{j}"], row[f"tv{j}"]
        if s == t:
            continue
        ds, dt = _number(s), _number(t)
        hidden = col.pii and hide_pii
        found.append(
            MetricComparison(
                metric="sum",
                column=col.label,
                source_column=col.source_label,
                group=group,
                source=None if hidden else s,
                target=None if hidden else t,
                difference=None if hidden or ds is None or dt is None else float(dt - ds),
                passed=False,
            )
        )
    if not found and (row["ss1"] != row["ts1"] or row["ss2"] != row["ts2"]):
        found.append(
            MetricComparison(
                metric="child_content",
                group=group,
                source=sn,
                target=tn,
                passed=False,
                note="same child count and totals, different child rows",
            )
        )
    return found


__all__ = ["NestedComparator"]
