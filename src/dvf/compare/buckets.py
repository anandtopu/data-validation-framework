"""Checksum search: compare bucket checksums inside each database, drill into the buckets that
differ, and diff only the rows of the buckets that still differ (BUILD_PROMPT.md section 8.2).

Two grids share one search:

* ``hash_bucket`` (REC-04 at scale): rows are bucketed by a 32-bit hash of their canonical key.
  Level ``L`` has ``buckets * fanout**L`` buckets, and every bucket nests inside one bucket of
  the level above, so the search narrows down without ever touching matched buckets again.
* ``bisect`` (REC-06): rows are segmented by ranges of an integer key. Range predicates let the
  database use an index on the key, so each level reads only the segments still in question.

Every bucket (or segment) is summarized on each side by its row count, distinct key count, and
two independent sums of 32-bit row-hash prefixes (not XOR, so duplicate rows never cancel out).
Floating-point columns, which are never hashed, contribute their non-NULL count, sum, and sum of
absolute values, compared with the column tolerance. Buckets whose summaries match are counted as
matched; the rest are drilled into until they hold at most ``drilldown_threshold`` rows (or
drilling stops narrowing), and then the configured engine diffs just those rows, exactly.

Rows moved between the databases and dvf therefore grow with the number of differences (about
``O(d log n)``), not with the size of the tables.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from dvf.compare.plan import ComparisonPlan, PlannedColumn
from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.engines.base import DiffJob, DiffResult, DiffSide, Engine
from dvf.execution.context import QueryRecorder
from dvf.models.results import ReconciliationSummary
from dvf.normalize.canonical import Kind, row_hash_sql
from dvf.plugins.base import Relation
from dvf.sql.dialects import Dialect

Side_ = Literal["source", "target"]
GridKind = Literal["hash", "key"]

MAX_CELLS_IN_PREDICATE = 4_000
"""Most buckets named in one predicate; beyond it the search stops drilling."""
MAX_RANGES = 1_000
"""Most key ranges in one ``bisect`` predicate before it switches to segment numbers."""
MAX_GRANULARITY = 2**31
"""Finest hash grid: bucket numbers times hash values must fit in a signed 64-bit integer."""
MIN_ROWS_PER_CHILD = 4
"""Drilling deeper is pointless when a child bucket would hold fewer rows than this."""


# ====================================================================== SQL per side


def _side(col: PlannedColumn, side: Side_) -> Any:
    return col.source if side == "source" else col.target


def _dialect(plan: ComparisonPlan, side: Side_) -> Dialect:
    return plan.source_dialect if side == "source" else plan.target_dialect


def key_hash_sql(plan: ComparisonPlan, side: Side_) -> str:
    """MD5 of the canonical key, in terms of the relation's own columns."""
    return row_hash_sql([_side(k, side).canonical for k in plan.keys], _dialect(plan, side))


def key_int_sql(plan: ComparisonPlan, side: Side_) -> str:
    """The 32-bit hash integer of the canonical key that ``hash_bucket`` buckets by."""
    return _dialect(plan, side).hash_int(key_hash_sql(plan, side), 0)


def keys_not_null_sql(plan: ComparisonPlan, side: Side_) -> str:
    return " AND ".join(f"{_side(k, side).canonical} IS NOT NULL" for k in plan.keys)


def keys_null_sql(plan: ComparisonPlan, side: Side_) -> str:
    return " OR ".join(f"{_side(k, side).canonical} IS NULL" for k in plan.keys)


def base_sql(plan: ComparisonPlan, side: Side_, relation_sql: str, key_value: str | None) -> str:
    """Per-row summary columns: ``dvf_null`` (a key is NULL), ``kh`` (key hash), ``x`` (its 32-bit
    integer), ``h1``/``h2`` (row-hash prefixes), ``n{j}`` (tolerance columns), and ``v`` (the
    integer key, for ``bisect``)."""
    d = _dialect(plan, side)
    inner: list[str] = []
    knames: list[str] = []
    for i, key in enumerate(plan.keys):
        inner.append(f"{_side(key, side).canonical} AS k{i}")
        knames.append(f"k{i}")
    hashed: list[str] = []
    numeric: list[str] = []
    for j, col in enumerate(plan.columns):
        expr = _side(col, side)
        if col.pair_spec.hashable:
            inner.append(f"{expr.canonical} AS c{j}")
            hashed.append(f"c{j}")
        else:
            inner.append(f"{expr.numeric} AS n{j}")
            numeric.append(f"n{j}")
    if key_value is not None:
        inner.append(f"{key_value} AS v")
    inner_sql = "SELECT " + ", ".join(inner) + f" FROM {relation_sql}"
    null_flag = " OR ".join(f"{k} IS NULL" for k in knames)
    mid = [
        f"CASE WHEN {null_flag} THEN 1 ELSE 0 END AS dvf_null",
        f"{row_hash_sql(knames, d)} AS kh",
        f"{row_hash_sql(hashed, d)} AS h",
        *numeric,
    ]
    if key_value is not None:
        mid.append("v")
    mid_sql = "SELECT " + ", ".join(mid) + f" FROM {d.subquery(inner_sql, 'dvf_i')}"
    outer = [
        "dvf_null",
        "kh",
        f"{d.hash_int('kh', 0)} AS x",
        f"{d.hash_int('h', 0)} AS h1",
        f"{d.hash_int('h', 8)} AS h2",
        *numeric,
    ]
    if key_value is not None:
        outer.append("v")
    return "SELECT " + ", ".join(outer) + f" FROM {d.subquery(mid_sql, 'dvf_m')}"


def stats_sql(
    plan: ComparisonPlan,
    side: Side_,
    base: str,
    group: str,
    where: str | None,
    *,
    with_nulls: bool,
) -> str:
    """Per-bucket summaries. With ``with_nulls``, rows with a NULL key form bucket -1."""
    d = _dialect(plan, side)
    if with_nulls:
        group = f"(CASE WHEN dvf_null = 1 THEN -1 ELSE {group} END)"
        conditions = []
    else:
        conditions = ["dvf_null = 0"]
    if where:
        conditions.append(f"({where})")
    items = [
        f"{group} AS g",
        "COUNT(*) AS n",
        "COUNT(DISTINCT kh) AS nk",
        "SUM(h1) AS s1",
        "SUM(h2) AS s2",
    ]
    for j, col in enumerate(plan.columns):
        if not col.pair_spec.hashable:
            items += [
                f"COUNT(n{j}) AS fc{j}",
                f"SUM(n{j}) AS fs{j}",
                f"SUM(ABS(n{j})) AS fa{j}",
            ]
    where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return f"SELECT {', '.join(items)} FROM {d.subquery(base, 'dvf_b')}{where_sql} GROUP BY {group}"


# ====================================================================== grids


class Grid:
    """Nested buckets: ``depth`` levels, and the SQL that assigns rows to them."""

    kind: GridKind = "hash"
    depth: int = 1
    fanout: int = 2

    def group(self, d: Dialect, level: int, value: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def restrict(self, d: Dialect, level: int, cells: list[int], value: str) -> str:
        raise NotImplementedError  # pragma: no cover - abstract

    def describe(self, level: int) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


def _in_lists(expr: str, values: list[int]) -> str:
    chunks = [values[i : i + 1000] for i in range(0, len(values), 1000)]
    return " OR ".join(f"{expr} IN ({', '.join(str(v) for v in chunk)})" for chunk in chunks)


class HashGrid(Grid):
    """Level ``L`` has ``buckets * fanout**L`` buckets over the 32-bit key hash."""

    kind: GridKind = "hash"

    def __init__(self, buckets: int, fanout: int) -> None:
        self.fanout = fanout
        self.granularities = [buckets]
        while self.granularities[-1] * fanout <= MAX_GRANULARITY:
            self.granularities.append(self.granularities[-1] * fanout)
        self.depth = len(self.granularities)

    def group(self, d: Dialect, level: int, value: str) -> str:
        return d.bucket(value, self.granularities[level])

    def restrict(self, d: Dialect, level: int, cells: list[int], value: str) -> str:
        return _in_lists(self.group(d, level, value), sorted(cells))

    def describe(self, level: int) -> str:
        return f"{self.granularities[level]:,} hash buckets"


class KeyGrid(Grid):
    """Segments of an integer key: level ``L`` segments are ``fanout**(depth - 1 - L)`` keys wide,
    starting at the smallest key, so each segment nests in one segment of the level above."""

    kind: GridKind = "key"

    def __init__(self, low: int, high: int, buckets: int, fanout: int) -> None:
        self.low = low
        self.fanout = fanout
        span = high - low + 1
        exponent = 0
        while math.ceil(span / fanout**exponent) > buckets:
            exponent += 1
        self.widths = [fanout ** (exponent - level) for level in range(exponent + 1)]
        self.depth = len(self.widths)

    def _offset(self, value: str) -> str:
        return f"({value} - {self.low})" if self.low else value

    def group(self, d: Dialect, level: int, value: str) -> str:
        width = self.widths[level]
        return self._offset(value) if width == 1 else d.int_div(self._offset(value), width)

    def ranges(self, level: int, cells: list[int]) -> list[tuple[int, int]]:
        width = self.widths[level]
        merged: list[tuple[int, int]] = []
        for cell in sorted(cells):
            lo, hi = self.low + cell * width, self.low + (cell + 1) * width
            if merged and merged[-1][1] == lo:
                merged[-1] = (merged[-1][0], hi)
            else:
                merged.append((lo, hi))
        return merged

    def restrict(self, d: Dialect, level: int, cells: list[int], value: str) -> str:
        ranges = self.ranges(level, cells)
        if len(ranges) > MAX_RANGES:
            return _in_lists(self.group(d, level, value), sorted(cells))
        return " OR ".join(f"({value} >= {lo} AND {value} < {hi})" for lo, hi in ranges)

    def describe(self, level: int) -> str:
        width = self.widths[level]
        return f"key segments of {width:,}" if width > 1 else "single keys"


# ====================================================================== search


@dataclass
class CellStats:
    rows: int
    keys: int
    s1: int
    s2: int
    floats: dict[int, tuple[int, float, float]] = field(default_factory=dict)


def _cell(row: dict[str, Any], plan: ComparisonPlan) -> CellStats:
    floats = {}
    for j, col in enumerate(plan.columns):
        if not col.pair_spec.hashable:
            floats[j] = (
                int(row.get(f"fc{j}") or 0),
                float(row.get(f"fs{j}") or 0.0),
                float(row.get(f"fa{j}") or 0.0),
            )
    return CellStats(
        rows=int(row["n"]),
        keys=int(row["nk"] or 0),
        s1=int(row["s1"] or 0),
        s2=int(row["s2"] or 0),
        floats=floats,
    )


def _float_sums_match(plan: ComparisonPlan, s: CellStats, t: CellStats) -> bool:
    for j, (count_s, sum_s, abs_s) in s.floats.items():
        count_t, sum_t, abs_t = t.floats.get(j, (0, 0.0, 0.0))
        if count_s != count_t:
            return False
        numeric = plan.columns[j].pair_spec.numeric
        abs_tol = numeric.abs_tol if numeric is not None else 0.0
        rel_tol = max(numeric.rel_tol if numeric is not None else 0.0, 1e-9)
        allowed = abs_tol * max(s.rows, 1) + rel_tol * max(abs_s, abs_t)
        if abs(sum_s - sum_t) > allowed:
            return False
    return True


def cells_match(plan: ComparisonPlan, s: CellStats | None, t: CellStats | None) -> bool:
    if s is None or t is None:
        return False
    return (
        s.rows == t.rows
        and s.keys == t.keys
        and s.s1 == t.s1
        and s.s2 == t.s2
        and _float_sums_match(plan, s, t)
    )


@dataclass
class SearchResult:
    """What the checksum search established before the leaf diff."""

    source_rows: int = 0
    target_rows: int = 0
    null_keys_source: int = 0
    null_keys_target: int = 0
    matched: int = 0
    duplicates_source: int = 0
    duplicates_target: int = 0
    leaf_level: int | None = None
    leaf_cells: list[int] | None = None
    """Buckets still differing at ``leaf_level``; ``None`` with ``unrestricted`` False means
    everything matched."""
    unrestricted: bool = False
    """The leaf diff covers every row (differences were too widespread to narrow down)."""
    levels: list[dict[str, Any]] = field(default_factory=list)
    leaf_rows_source: int = 0
    leaf_rows_target: int = 0


class ChecksumSearch:
    """Runs the level-by-level bucket comparison for one table (or one partition)."""

    def __init__(
        self,
        plan: ComparisonPlan,
        source: DiffSide,
        target: DiffSide,
        grid: Grid,
        recorder: QueryRecorder,
        *,
        threshold: int,
        key_values: tuple[str, str] | None = None,
    ) -> None:
        self.plan = plan
        self.sides = {"source": source, "target": target}
        self.grid = grid
        self.recorder = recorder
        self.threshold = threshold
        self.key_values = key_values
        self.base = {
            side: base_sql(
                plan,
                side,
                self.sides[side].relation.sql,
                key_values[0 if side == "source" else 1] if key_values else None,
            )
            for side in ("source", "target")
        }

    def _value(self, side: Side_) -> str:
        return "v" if self.grid.kind == "key" else "x"

    def _stats(
        self, level: int, where_cells: tuple[int, list[int]] | None
    ) -> dict[str, dict[int, CellStats]]:
        out: dict[str, dict[int, CellStats]] = {}
        for side in ("source", "target"):
            s: Side_ = side
            d = _dialect(self.plan, s)
            value = self._value(s)
            where = None
            if where_cells is not None:
                where = self.grid.restrict(d, where_cells[0], where_cells[1], value)
            sql = stats_sql(
                self.plan,
                s,
                self.base[s],
                self.grid.group(d, level, value),
                where,
                with_nulls=where_cells is None,
            )
            label = Side.SOURCE if s == "source" else Side.TARGET
            rows = self.recorder.run(self.sides[s].connector, sql, label).to_pylist()
            out[s] = {int(r["g"]): _cell(r, self.plan) for r in rows}
        return out

    def run(self) -> SearchResult:
        result = SearchResult()
        active: list[int] | None = None
        level = 0
        while True:
            started = time.perf_counter()
            stats = self._stats(level, None if active is None else (level - 1, active))
            src, tgt = stats["source"], stats["target"]
            if level == 0:
                for name, side_stats in (("source", src), ("target", tgt)):
                    nulls = side_stats.pop(-1, None)
                    total = sum(c.rows for c in side_stats.values()) + (nulls.rows if nulls else 0)
                    if name == "source":
                        result.source_rows, result.null_keys_source = (
                            total,
                            nulls.rows if nulls else 0,
                        )
                    else:
                        result.target_rows, result.null_keys_target = (
                            total,
                            nulls.rows if nulls else 0,
                        )
            cells = sorted(set(src) | set(tgt))
            mismatched = [c for c in cells if not cells_match(self.plan, src.get(c), tgt.get(c))]
            rows_in_mismatched = sum(
                (src[c].rows if c in src else 0) + (tgt[c].rows if c in tgt else 0)
                for c in mismatched
            )
            result.levels.append(
                {
                    "level": level,
                    "grid": self.grid.describe(level),
                    "buckets_compared": len(cells),
                    "buckets_differing": len(mismatched),
                    "rows_in_differing_buckets": rows_in_mismatched,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            if len(mismatched) > MAX_CELLS_IN_PREDICATE:
                # Too many to name; diff the parent buckets (or everything) instead.
                result.levels[-1]["note"] = "too many differing buckets to drill further"
                if active is None:
                    result.unrestricted = True
                    result.leaf_level, result.leaf_cells = None, None
                else:
                    result.leaf_level, result.leaf_cells = level - 1, active
                return result
            for c in cells:
                if c not in mismatched:
                    s = src[c]
                    t = tgt[c]
                    result.matched += s.keys
                    result.duplicates_source += s.rows - s.keys
                    result.duplicates_target += t.rows - t.keys
            if not mismatched:
                return result
            narrowing = active is None or len(mismatched) < 0.9 * len(cells)
            per_child = rows_in_mismatched / (2 * len(mismatched) * self.grid.fanout)
            if (
                rows_in_mismatched <= 2 * self.threshold
                or level + 1 >= self.grid.depth
                or not narrowing
                or per_child < MIN_ROWS_PER_CHILD
            ):
                result.leaf_level, result.leaf_cells = level, mismatched
                return result
            active, level = mismatched, level + 1

    def leaf_relation(self, side: Side_, search: SearchResult) -> Relation:
        """The side's relation restricted to the rows of the differing buckets (keys not NULL)."""
        rel = self.sides[side].relation
        d = _dialect(self.plan, side)
        conditions = [keys_not_null_sql(self.plan, side)]
        if not search.unrestricted:
            assert search.leaf_level is not None and search.leaf_cells is not None
            if self.grid.kind == "key":
                assert self.key_values is not None
                value = self.key_values[0 if side == "source" else 1]
            else:
                value = key_int_sql(self.plan, side)
            conditions.append(
                f"({self.grid.restrict(d, search.leaf_level, search.leaf_cells, value)})"
            )
        return restrict_relation(rel, " AND ".join(conditions), d)


def restrict_relation(
    rel: Relation, predicate: str, dialect: Dialect, alias: str = "dvf_part"
) -> Relation:
    """``rel`` with an extra row filter (its own columns), usable wherever ``rel`` was."""
    sql = dialect.subquery(f"SELECT * FROM {rel.sql} WHERE {predicate}", alias)
    combined = f"({rel.filter}) AND ({predicate})" if rel.filter else predicate
    return Relation(
        ref=rel.ref,
        sql=sql,
        base_sql=rel.base_sql,
        table=rel.table,
        filter=combined,
        connection=rel.connection,
    )


def integer_key(plan: ComparisonPlan) -> tuple[str, str]:
    """The normalized integer key expressions ``bisect`` segments by (one per side)."""
    if len(plan.keys) != 1:
        raise PlanError(
            "Strategy 'bisect' needs exactly one key column.",
            hint="Use 'hash_bucket' for composite keys.",
        )
    key = plan.keys[0]
    kinds = {key.pair_spec.source.kind, key.pair_spec.target.kind}
    if kinds != {Kind.INTEGER}:
        raise PlanError(
            f"Strategy 'bisect' needs an integer key; '{key.label}' is "
            f"{' and '.join(sorted(k.value for k in kinds))}.",
            hint="Use 'hash_bucket', which works with keys of any type.",
        )
    return key.source.normalized, key.target.normalized


def key_bounds(
    plan: ComparisonPlan, source: DiffSide, target: DiffSide, recorder: QueryRecorder
) -> tuple[int, int] | None:
    """Smallest and largest integer key over both sides, or ``None`` when both are empty."""
    values = integer_key(plan)
    lows: list[int] = []
    highs: list[int] = []
    for side, value, label in (
        (source, values[0], Side.SOURCE),
        (target, values[1], Side.TARGET),
    ):
        row = recorder.run(
            side.connector,
            f"SELECT MIN({value}) AS lo, MAX({value}) AS hi FROM {side.relation.sql}",
            label,
        ).to_pylist()[0]
        if row["lo"] is not None:
            lows.append(int(row["lo"]))
            highs.append(int(row["hi"]))
    if not lows:
        return None
    low, high = min(lows), max(highs)
    if high - low > 2**62:
        raise PlanError(
            "The key range is too wide for 'bisect' (more than 2**62 values).",
            hint="Use 'hash_bucket' instead.",
        )
    return low, high


def search_and_diff(
    plan: ComparisonPlan,
    source: DiffSide,
    target: DiffSide,
    grid_kind: GridKind,
    *,
    buckets: int,
    fanout: int,
    threshold: int,
    engine: Engine,
    job_template: DiffJob,
) -> DiffResult:
    """Run the checksum search, diff the remaining rows with ``engine``, and combine the two."""
    recorder = job_template.recorder
    started = time.perf_counter()
    key_values: tuple[str, str] | None = None
    if grid_kind == "key":
        key_values = integer_key(plan)
        bounds = key_bounds(plan, source, target, recorder)
        grid: Grid = (
            KeyGrid(bounds[0], bounds[1], buckets, fanout) if bounds else KeyGrid(0, 0, 1, fanout)
        )
    else:
        grid = HashGrid(buckets, fanout)
    search = ChecksumSearch(
        plan, source, target, grid, recorder, threshold=threshold, key_values=key_values
    )
    found = search.run()
    search_rows = sum(r.rows_returned or 0 for r in recorder.records)
    leaf: DiffResult | None = None
    if found.unrestricted or found.leaf_cells:
        job = DiffJob(
            plan=plan,
            source=DiffSide(source.connector, search.leaf_relation("source", found)),
            target=DiffSide(target.connector, search.leaf_relation("target", found)),
            mode=job_template.mode,
            sample_size=job_template.sample_size,
            max_mismatch_rows=job_template.max_mismatch_rows,
            recorder=recorder,
            resources=job_template.resources,
        )
        leaf = engine.diff(job)
    leaf_summary = leaf.summary if leaf is not None else ReconciliationSummary()
    summary = ReconciliationSummary(
        matched=found.matched + leaf_summary.matched,
        mismatched=leaf_summary.mismatched,
        missing_in_target=leaf_summary.missing_in_target,
        extra_in_target=leaf_summary.extra_in_target,
        duplicate_keys_source=found.duplicates_source + leaf_summary.duplicate_keys_source,
        duplicate_keys_target=found.duplicates_target + leaf_summary.duplicate_keys_target,
        null_keys_source=found.null_keys_source,
        null_keys_target=found.null_keys_target,
    )
    result = DiffResult(
        engine=leaf.engine if leaf is not None else engine.name or "auto",
        source_rows=found.source_rows,
        target_rows=found.target_rows,
        summary=summary,
    )
    if leaf is not None:
        result.column_counts = leaf.column_counts
        result.mismatches = leaf.mismatches
        result.key_samples = leaf.key_samples
        result.timing = leaf.timing
        result.notes = list(leaf.notes)
    if summary.null_keys_source or summary.null_keys_target:
        _null_key_samples(plan, source, target, recorder, job_template.sample_size, result)
    rows_transferred = sum(r.rows_returned or 0 for r in recorder.records)
    total = max(found.source_rows + found.target_rows, 1)
    result.details = {
        "strategy": "hash_bucket" if grid_kind == "hash" else "bisect",
        "levels": found.levels,
        "leaf_rows_source": (leaf.source_rows if leaf else 0),
        "leaf_rows_target": (leaf.target_rows if leaf else 0),
        "summary_rows_transferred": search_rows,
        "rows_transferred": rows_transferred,
        "transfer_pct": round(100.0 * rows_transferred / total, 6),
        "search_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    if found.unrestricted:
        result.notes.append(
            "Differences are spread over most buckets, so every row was diffed; "
            "row_hash or full_diff is as efficient here."
        )
    unhashed = [c.label for c in plan.columns if not c.pair_spec.hashable]
    if unhashed:
        result.notes.append(
            "Floating-point columns are compared per bucket by sums within tolerance, then exactly "
            f"per row in differing buckets: {', '.join(unhashed[:10])}."
        )
    return result


def _null_key_samples(
    plan: ComparisonPlan,
    source: DiffSide,
    target: DiffSide,
    recorder: QueryRecorder,
    limit: int,
    result: DiffResult,
) -> None:
    if limit <= 0:
        return
    for side, kind, label, count in (
        ("source", "null_key_in_source", Side.SOURCE, result.summary.null_keys_source),
        ("target", "null_key_in_target", Side.TARGET, result.summary.null_keys_target),
    ):
        if not count:
            continue
        s: Side_ = side  # type: ignore[assignment]
        d = _dialect(plan, s)
        rel = source if s == "source" else target
        raw = ", ".join(f"{d.to_text(_side(k, s).raw)} AS key{i}" for i, k in enumerate(plan.keys))
        sql = d.limit(f"SELECT {raw} FROM {rel.relation.sql} WHERE {keys_null_sql(plan, s)}", limit)
        rows = recorder.run(rel.connector, sql, label).to_pylist()
        result.key_samples[kind] = [
            [r[f"key{i}"] for i in range(len(plan.keys))] + [1] for r in rows
        ]
