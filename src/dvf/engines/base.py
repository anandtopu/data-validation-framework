"""Row-level comparison: the job, the result, and the SQL that the SQL engines share.

A diff pairs rows by key and reports key reconciliation (REC-03), rows whose canonical content
differs (REC-04), and, in ``full`` mode, per-column mismatches with side-by-side values (REC-05).

Each side is first *projected*: keys and columns are normalized and canonicalized in that side's
own database (pushdown), producing these columns:

====== =====================================================================
k{i}   canonical key text (used to join)
rk{i}  raw key text (for display)
h      row hash over hashable canonical columns
c{j}   canonical text of compared column j (hashable columns, ``full`` projection)
n{j}   numeric value of tolerance-compared column j
r{j}   raw text of column j (``full`` projection)
====== =====================================================================

Full diffs run in two phases. Phase A compares keys, row hashes, and tolerance columns for every
row. Phase B fetches the canonical and raw values of only the rows phase A found different, and
counts mismatches per column. Wide tables with few differences therefore never move or compare
every value.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from dvf.compare.plan import ComparisonPlan, PlannedColumn
from dvf.execution.context import QueryRecorder
from dvf.models.results import ReconciliationSummary, TimingBreakdown
from dvf.normalize.canonical import row_hash_sql
from dvf.plugins.base import Component, Connector, Relation
from dvf.sql.dialects import Dialect

DiffMode = Literal["hash", "full"]
ProjectionMode = Literal["keys", "hash", "full"]
Side_ = Literal["source", "target"]

KEY_LIST_LIMIT = 2_000
"""Most mismatched keys pushed to a remote database as a literal ``IN`` list in phase B. Beyond
it, connectors that cannot receive a key table return every row's values for local filtering."""


@dataclass
class DiffSide:
    connector: Connector
    relation: Relation


@dataclass(frozen=True)
class EngineResources:
    """Local engine settings from ``defaults.execution`` and the command line."""

    memory_limit: str | None = None
    threads: int | None = None
    temp_dir: str | None = None
    keep_temp: bool = False


@dataclass
class DiffJob:
    plan: ComparisonPlan
    source: DiffSide
    target: DiffSide
    mode: DiffMode
    sample_size: int
    max_mismatch_rows: int
    recorder: QueryRecorder = field(default_factory=QueryRecorder)
    resources: EngineResources = field(default_factory=EngineResources)

    @property
    def same_connection(self) -> bool:
        return self.source.connector is self.target.connector


@dataclass
class MismatchRow:
    keys: list[Any]
    column: str
    source_value: Any
    target_value: Any
    source_canonical: Any
    target_canonical: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys": self.keys,
            "column": self.column,
            "source_value": self.source_value,
            "target_value": self.target_value,
            "source_canonical": self.source_canonical,
            "target_canonical": self.target_canonical,
        }


@dataclass
class DiffResult:
    engine: str
    source_rows: int
    target_rows: int
    summary: ReconciliationSummary
    column_counts: dict[str, int] = field(default_factory=dict)
    mismatches: list[MismatchRow] = field(default_factory=list)
    key_samples: dict[str, list[list[Any]]] = field(default_factory=dict)
    timing: TimingBreakdown = field(default_factory=TimingBreakdown)
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    """Strategy-specific facts (buckets compared, rows transferred, partitions)."""

    @property
    def rows_with_differences(self) -> int:
        s = self.summary
        return s.mismatched + s.missing_in_target + s.extra_in_target

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe form, used for checkpoints."""
        from dvf.security.masking import to_json_safe

        def safe(value: Any) -> Any:
            if isinstance(value, list):
                return [safe(v) for v in value]
            if isinstance(value, dict):
                return {str(k): safe(v) for k, v in value.items()}
            return to_json_safe(value)

        return {
            "engine": self.engine,
            "source_rows": self.source_rows,
            "target_rows": self.target_rows,
            "summary": self.summary.model_dump(),
            "column_counts": dict(self.column_counts),
            "mismatches": [safe(m.to_dict()) for m in self.mismatches],
            "key_samples": safe(self.key_samples),
            "timing": self.timing.model_dump(),
            "notes": list(self.notes),
            "details": safe(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffResult:
        return cls(
            engine=data["engine"],
            source_rows=int(data["source_rows"]),
            target_rows=int(data["target_rows"]),
            summary=ReconciliationSummary.model_validate(data["summary"]),
            column_counts={k: int(v) for k, v in data.get("column_counts", {}).items()},
            mismatches=[MismatchRow(**m) for m in data.get("mismatches", [])],
            key_samples=dict(data.get("key_samples", {})),
            timing=TimingBreakdown.model_validate(data.get("timing", {})),
            notes=list(data.get("notes", [])),
            details=dict(data.get("details", {})),
        )


class Engine(Component):
    """Base class for row-level comparison engines."""

    name: str = ""

    def diff(self, job: DiffJob) -> DiffResult:  # pragma: no cover - interface
        raise NotImplementedError


# ====================================================================== projection


def _side_expr(col: PlannedColumn, side: Side_) -> Any:
    return col.source if side == "source" else col.target


def projection_sql(
    plan: ComparisonPlan,
    side: Side_,
    relation_sql: str,
    mode: ProjectionMode,
    *,
    where: str | None = None,
) -> str:
    """SELECT producing a side's canonical projection, in that side's dialect.

    Canonical values are computed once in an inner query and referenced by the row hash, so
    each value is canonicalized a single time. ``where`` filters the relation's rows (in terms of
    the relation's own columns) before anything is canonicalized.
    """
    dialect = plan.source_dialect if side == "source" else plan.target_dialect
    inner: list[str] = []
    key_outer: list[str] = []
    col_outer: list[str] = []
    for i, key in enumerate(plan.keys):
        expr = _side_expr(key, side)
        inner.append(f"{expr.canonical} AS k{i}")
        key_outer.append(f"k{i}")
        if mode != "keys":
            inner.append(f"{dialect.to_text(expr.raw)} AS rk{i}")
            key_outer.append(f"rk{i}")
    hashed: list[str] = []
    if mode != "keys":
        for j, col in enumerate(plan.columns):
            expr = _side_expr(col, side)
            if col.pair_spec.hashable:
                inner.append(f"{expr.canonical} AS c{j}")
                hashed.append(f"c{j}")
                if mode == "full":
                    col_outer.append(f"c{j}")
            else:
                assert expr.numeric is not None
                inner.append(f"{expr.numeric} AS n{j}")
                col_outer.append(f"n{j}")
            if mode == "full":
                inner.append(f"{dialect.to_text(expr.raw)} AS r{j}")
                col_outer.append(f"r{j}")
    where_sql = f"\nWHERE {where}" if where else ""
    inner_sql = "SELECT\n  " + ",\n  ".join(inner) + f"\nFROM {relation_sql}{where_sql}"
    if mode == "keys":
        return inner_sql
    outer = [*key_outer, f"{row_hash_sql(hashed, dialect)} AS h", *col_outer]
    return "SELECT " + ", ".join(outer) + f"\nFROM {dialect.subquery(inner_sql, 'dvf_p')}"


def projected_columns(plan: ComparisonPlan, mode: ProjectionMode) -> list[tuple[str, str]]:
    """(name, kind) of projection columns in order, where kind is ``text`` or ``double``."""
    cols: list[tuple[str, str]] = []
    for i in range(len(plan.keys)):
        cols.append((f"k{i}", "text"))
        if mode != "keys":
            cols.append((f"rk{i}", "text"))
    if mode == "keys":
        return cols
    cols.append(("h", "text"))
    for j, col in enumerate(plan.columns):
        if col.pair_spec.hashable:
            if mode == "full":
                cols.append((f"c{j}", "text"))
        else:
            cols.append((f"n{j}", "double"))
        if mode == "full":
            cols.append((f"r{j}", "text"))
    return cols


def key_names(plan: ComparisonPlan) -> list[str]:
    return [f"k{i}" for i in range(len(plan.keys))]


def duplicate_probe_sql(names: Sequence[str], relation_sql: str, dialect: Dialect) -> str:
    """A query returning ``dvf_dups`` > 0 when any non-NULL key occurs more than once."""
    cols = ", ".join(names)
    not_null = " AND ".join(f"{k} IS NOT NULL" for k in names)
    inner = (
        f"SELECT {cols} FROM {relation_sql} WHERE {not_null} GROUP BY {cols} HAVING COUNT(*) > 1"
    )
    return f"SELECT COUNT(*) AS dvf_dups FROM {dialect.subquery(dialect.limit(inner, 1), 'dvf_d')}"


def key_match_predicate(
    plan: ComparisonPlan, side: Side_, table: str, columns: Sequence[str]
) -> str:
    """``EXISTS`` predicate: the row's canonical key is in ``table`` (columns ``columns``)."""
    dialect = plan.source_dialect if side == "source" else plan.target_dialect
    alias = dialect.quote("dvf_mk")
    conds = " AND ".join(
        f"{alias}.{col} = {_side_expr(key, side).canonical}"
        for key, col in zip(plan.keys, columns, strict=True)
    )
    source = dialect.subquery(f"SELECT * FROM {table}", "dvf_mk")
    return f"EXISTS (SELECT 1 FROM {source} WHERE {conds})"


def key_list_predicate(plan: ComparisonPlan, side: Side_, keys: Sequence[Sequence[Any]]) -> str:
    """Predicate matching rows whose canonical key is one of ``keys`` (literal lists)."""
    dialect = plan.source_dialect if side == "source" else plan.target_dialect
    exprs = [_side_expr(k, side).canonical for k in plan.keys]
    if not keys:
        return "1 = 0"
    if len(exprs) == 1:
        chunks = [keys[i : i + 500] for i in range(0, len(keys), 500)]
        parts = [
            f"{exprs[0]} IN ({', '.join(dialect.literal(k[0]) for k in chunk)})" for chunk in chunks
        ]
    else:
        parts = [
            "("
            + " AND ".join(
                f"{expr} = {dialect.literal(value)}" for expr, value in zip(exprs, key, strict=True)
            )
            + ")"
            for key in keys
        ]
    return "(" + " OR ".join(parts) + ")"


# ====================================================================== diff SQL


def _tolerance_mismatch(col: PlannedColumn, j: int, dialect: Dialect, s: str, t: str) -> str:
    numeric = col.pair_spec.numeric
    assert numeric is not None
    allowed = dialect.greatest(
        repr(float(numeric.abs_tol)),
        f"{float(numeric.rel_tol)!r} * {dialect.greatest(f'ABS({s})', f'ABS({t})')}",
    )
    return (
        f"(CASE WHEN {s} IS NULL AND {t} IS NULL THEN 0 WHEN {s} IS NULL OR {t} IS NULL THEN 1 "
        f"WHEN ABS({s} - {t}) > {allowed} THEN 1 ELSE 0 END)"
    )


class SqlDiff:
    """Builds the diff queries over two projected relations in one dialect.

    ``dedupe`` keeps one row per key (the one with the smallest row hash) when keys repeat; skip
    it when a probe found no duplicates, because the window function is the costliest step.
    ``prefix`` names this diff's CTEs so two diffs can share one statement, and ``preamble`` holds
    CTEs that must come first.
    """

    def __init__(
        self,
        plan: ComparisonPlan,
        dialect: Dialect,
        mode: ProjectionMode,
        source_rel: str,
        target_rel: str,
        *,
        dedupe: bool = True,
        prefix: str = "",
        preamble: Sequence[str] = (),
    ) -> None:
        self.plan = plan
        self.dialect = dialect
        self.mode = mode
        self.source_rel = source_rel
        self.target_rel = target_rel
        self.dedupe = dedupe
        self.prefix = prefix
        self.preamble = list(preamble)
        self.columns = projected_columns(plan, mode)
        self.key_names = key_names(plan)
        self.has_hash = any(name == "h" for name, _ in self.columns)

    def n(self, name: str) -> str:
        """The CTE called ``name`` in this diff."""
        return f"{self.prefix}{name}"

    def _order(self) -> str:
        names = [n for n, _ in self.columns if n == "h" or n[0] == "n"]
        if not self.has_hash:
            names = [n for n, _ in self.columns if n[0] in "cn"]
        return ", ".join(names or self.key_names)

    def _flags(self) -> list[str]:
        flags: list[str] = []
        for j, col in enumerate(self.plan.columns):
            if col.pair_spec.hashable:
                if self.mode == "full":
                    cond = self.dialect.is_distinct(f"s1.c{j}", f"t1.c{j}")
                    flags.append(f"(CASE WHEN {cond} THEN 1 ELSE 0 END) AS m{j}")
            else:
                flags.append(
                    f"{_tolerance_mismatch(col, j, self.dialect, f's1.n{j}', f't1.n{j}')} AS m{j}"
                )
        return flags

    def _row_flag(self) -> str:
        parts = []
        if self.has_hash:
            parts.append(self.dialect.is_distinct("s1.h", "t1.h"))
        for j, col in enumerate(self.plan.columns):
            if not col.pair_spec.hashable:
                parts.append(
                    f"{_tolerance_mismatch(col, j, self.dialect, f's1.n{j}', f't1.n{j}')} = 1"
                )
            elif not self.has_hash:
                parts.append(self.dialect.is_distinct(f"s1.c{j}", f"t1.c{j}"))
        if not parts:
            return "0"
        return f"(CASE WHEN {' OR '.join(parts)} THEN 1 ELSE 0 END)"

    def cte_items(self) -> list[str]:
        """This diff's CTEs (``name AS (...)``), preamble first."""
        n = self.n
        keys = ", ".join(self.key_names)
        not_null = " AND ".join(f"{k} IS NOT NULL" for k in self.key_names)
        join = " AND ".join(f"s1.{k} = t1.{k}" for k in self.key_names)
        selected = []
        for name, _ in self.columns:
            selected += [f"s1.{name} AS s_{name}", f"t1.{name} AS t_{name}"]
        k0 = self.key_names[0]
        selected += [
            f"CASE WHEN s1.{k0} IS NULL THEN 0 ELSE 1 END AS in_s",
            f"CASE WHEN t1.{k0} IS NULL THEN 0 ELSE 1 END AS in_t",
            *self._flags(),
            f"{self._row_flag()} AS mrow",
        ]
        items = [
            *self.preamble,
            f"{n('s_all')} AS (SELECT * FROM {self.source_rel})",
            f"{n('t_all')} AS (SELECT * FROM {self.target_rel})",
            f"{n('s_nn')} AS (SELECT * FROM {n('s_all')} WHERE {not_null})",
            f"{n('t_nn')} AS (SELECT * FROM {n('t_all')} WHERE {not_null})",
        ]
        if self.dedupe:
            order = self._order()
            for s in ("s", "t"):
                items.append(
                    f"{n(s + '_rn')} AS (SELECT {n(s + '_nn')}.*, ROW_NUMBER() OVER "
                    f"(PARTITION BY {keys} ORDER BY {order}) AS dvf_rn FROM {n(s + '_nn')})"
                )
                items.append(f"{n(s + '1')} AS (SELECT * FROM {n(s + '_rn')} WHERE dvf_rn = 1)")
        else:
            items.append(f"{n('s1')} AS (SELECT * FROM {n('s_nn')})")
            items.append(f"{n('t1')} AS (SELECT * FROM {n('t_nn')})")
        select = ", ".join(selected)
        if self.dialect.full_outer_join:
            body = f"  SELECT {select}\n  FROM {n('s1')} s1 FULL OUTER JOIN {n('t1')} t1 ON {join}"
        else:  # MySQL: left join, plus target rows without a source row
            body = (
                f"  SELECT {select}\n  FROM {n('s1')} s1 LEFT JOIN {n('t1')} t1 ON {join}\n"
                f"  UNION ALL\n  SELECT {select}\n  FROM {n('t1')} t1 LEFT JOIN {n('s1')} s1 ON {join}\n"
                f"  WHERE s1.{k0} IS NULL"
            )
        items.append(f"{n('j')} AS (\n{body}\n)")
        return items

    def ctes(self) -> str:
        return "WITH\n" + ",\n".join(self.cte_items()) + "\n"

    def _flag_names(self) -> list[str]:
        return [
            f"m{j}"
            for j, col in enumerate(self.plan.columns)
            if self.mode == "full" or not col.pair_spec.hashable
        ]

    def stats_sql(self) -> str:
        n = self.n
        both = "in_s = 1 AND in_t = 1"
        # Counts come from cross-joined derived tables: Oracle rejects scalar subqueries next to
        # aggregates in one SELECT list.
        counts = [
            ("source_rows", "s_all"),
            ("target_rows", "t_all"),
            ("source_nn", "s_nn"),
            ("target_nn", "t_nn"),
            ("source_unique", "s1"),
            ("target_unique", "t1"),
        ]
        items = [
            "COALESCE(SUM(CASE WHEN in_s = 1 AND in_t = 0 THEN 1 ELSE 0 END), 0) AS missing",
            "COALESCE(SUM(CASE WHEN in_s = 0 AND in_t = 1 THEN 1 ELSE 0 END), 0) AS extra",
            f"COALESCE(SUM(CASE WHEN {both} AND mrow = 1 THEN 1 ELSE 0 END), 0) AS mismatched",
            f"COALESCE(SUM(CASE WHEN {both} AND mrow = 0 THEN 1 ELSE 0 END), 0) AS matched",
        ]
        for name in self._flag_names():
            items.append(
                f"COALESCE(SUM(CASE WHEN {both} THEN {name} ELSE 0 END), 0) AS {name}_count"
            )
        q = self.dialect
        sources = [
            q.subquery(f"SELECT COUNT(*) AS {alias} FROM {n(cte)}", f"dvf_{alias}")
            for alias, cte in counts
        ]
        sources.append(q.subquery("SELECT " + ",\n  ".join(items) + f"\nFROM {n('j')}", "dvf_agg"))
        return self.ctes() + "SELECT *\nFROM " + "\nCROSS JOIN ".join(sources)

    def mismatched_keys_select(self) -> str:
        """Canonical keys (``dvf_mk{i}``) of paired rows whose content differs."""
        keys = ", ".join(f"s_k{i} AS dvf_mk{i}" for i in range(len(self.plan.keys)))
        return f"SELECT {keys} FROM {self.n('j')} WHERE in_s = 1 AND in_t = 1 AND mrow = 1"

    def mismatch_sql(self, columns: list[int], limit: int) -> str:
        """Long-format mismatched values, at most ``limit`` rows per column."""
        q = self.dialect
        key_out = ", ".join(f"s_rk{i} AS key{i}" for i in range(len(self.plan.keys)))
        order = ", ".join(f"s_k{i}" for i in range(len(self.plan.keys)))
        parts = []
        for j in columns:
            col = self.plan.columns[j]
            if col.pair_spec.hashable:
                s_canon, t_canon = f"s_c{j}", f"t_c{j}"
            else:
                s_canon, t_canon = q.to_text(f"s_n{j}"), q.to_text(f"t_n{j}")
            inner = (
                f"SELECT {key_out}, {q.literal(col.label)} AS dvf_column, "
                f"s_r{j} AS source_value, t_r{j} AS target_value, "
                f"{s_canon} AS source_canonical, {t_canon} AS target_canonical "
                f"FROM {self.n('j')} WHERE in_s = 1 AND in_t = 1 AND m{j} = 1 ORDER BY {order}"
            )
            parts.append(f"SELECT * FROM {q.subquery(q.limit(inner, limit), f'q{j}')}")
        return self.ctes() + "\nUNION ALL\n".join(parts)

    def keys_sql(self, limit: int) -> str:
        """Samples of missing, extra, duplicate, and NULL keys (raw key text plus a count)."""
        q = self.dialect
        n_ = self.n
        n = len(self.plan.keys)
        side_keys = {s: ", ".join(f"{s}_rk{i} AS key{i}" for i in range(n)) for s in ("s", "t")}
        order = {s: ", ".join(f"{s}_k{i}" for i in range(n)) for s in ("s", "t")}
        raw_min = ", ".join(f"MIN(rk{i}) AS key{i}" for i in range(n))
        raw = ", ".join(f"rk{i} AS key{i}" for i in range(n))
        keys = ", ".join(self.key_names)
        null_any = " OR ".join(f"{k} IS NULL" for k in self.key_names)
        j = n_("j")
        selects = [
            (
                "missing_in_target",
                f"SELECT {side_keys['s']}, 1 AS dvf_count FROM {j} WHERE in_s = 1 AND in_t = 0 ORDER BY {order['s']}",
            ),
            (
                "extra_in_target",
                f"SELECT {side_keys['t']}, 1 AS dvf_count FROM {j} WHERE in_s = 0 AND in_t = 1 ORDER BY {order['t']}",
            ),
            (
                "duplicate_in_source",
                f"SELECT {raw_min}, COUNT(*) AS dvf_count FROM {n_('s_nn')} GROUP BY {keys} HAVING COUNT(*) > 1 ORDER BY {keys}",
            ),
            (
                "duplicate_in_target",
                f"SELECT {raw_min}, COUNT(*) AS dvf_count FROM {n_('t_nn')} GROUP BY {keys} HAVING COUNT(*) > 1 ORDER BY {keys}",
            ),
            (
                "null_key_in_source",
                f"SELECT {raw}, 1 AS dvf_count FROM {n_('s_all')} WHERE {null_any}",
            ),
            (
                "null_key_in_target",
                f"SELECT {raw}, 1 AS dvf_count FROM {n_('t_all')} WHERE {null_any}",
            ),
        ]
        parts = [
            f"SELECT {q.literal(kind)} AS dvf_kind, {q.quote('x')}.* "
            f"FROM {q.subquery(q.limit(sql, limit), 'x')}"
            for kind, sql in selects
        ]
        return self.ctes() + "\nUNION ALL\n".join(parts)


def summary_from_stats(stats: dict[str, Any]) -> ReconciliationSummary:
    def num(name: str) -> int:
        return int(stats.get(name) or 0)

    return ReconciliationSummary(
        matched=num("matched"),
        mismatched=num("mismatched"),
        missing_in_target=num("missing"),
        extra_in_target=num("extra"),
        duplicate_keys_source=num("source_nn") - num("source_unique"),
        duplicate_keys_target=num("target_nn") - num("target_unique"),
        null_keys_source=num("source_rows") - num("source_nn"),
        null_keys_target=num("target_rows") - num("target_nn"),
    )


Execute = Callable[[str], list[dict[str, Any]]]


def has_key_issues(summary: ReconciliationSummary) -> bool:
    return any(
        (
            summary.missing_in_target,
            summary.extra_in_target,
            summary.duplicate_keys_source,
            summary.duplicate_keys_target,
            summary.null_keys_source,
            summary.null_keys_target,
        )
    )


def collect_phase_a(builder: SqlDiff, execute: Execute, job: DiffJob, engine: str) -> DiffResult:
    """Run phase A (keys, hashes, tolerance columns): the summary, tolerance-column counts, and
    key samples."""
    stats = execute(builder.stats_sql())[0]
    summary = summary_from_stats(stats)
    result = DiffResult(
        engine=engine,
        source_rows=int(stats.get("source_rows") or 0),
        target_rows=int(stats.get("target_rows") or 0),
        summary=summary,
    )
    for j, col in enumerate(job.plan.columns):
        count = stats.get(f"m{j}_count")
        if count is not None:
            result.column_counts[col.label] = int(count)
    if has_key_issues(summary) and job.sample_size > 0:
        for row in execute(builder.keys_sql(job.sample_size)):
            values = [row[f"key{i}"] for i in range(len(job.plan.keys))]
            result.key_samples.setdefault(row["dvf_kind"], []).append([*values, row["dvf_count"]])
    return result


def collect_phase_b(builder: SqlDiff, execute: Execute, job: DiffJob, result: DiffResult) -> None:
    """Run phase B over the mismatched rows only: per-column counts and mismatched values."""
    stats = execute(builder.stats_sql())[0]
    for j, col in enumerate(job.plan.columns):
        count = stats.get(f"m{j}_count")
        if count is not None:
            result.column_counts[col.label] = int(count)
    flagged = [j for j, c in enumerate(job.plan.columns) if result.column_counts.get(c.label)]
    if flagged:
        per_column = max(1, job.max_mismatch_rows // len(flagged))
        for row in execute(builder.mismatch_sql(flagged, per_column)):
            result.mismatches.append(_mismatch_row(row, len(job.plan.keys)))


def _mismatch_row(row: dict[str, Any], n_keys: int) -> MismatchRow:
    return MismatchRow(
        keys=[row[f"key{i}"] for i in range(n_keys)],
        column=row["dvf_column"],
        source_value=row["source_value"],
        target_value=row["target_value"],
        source_canonical=row["source_canonical"],
        target_canonical=row["target_canonical"],
    )


def merge_results(
    results: Sequence[DiffResult], engine: str, sample_size: int, cap: int
) -> DiffResult:
    """Combine diffs of disjoint row subsets (partitions or bucket ranges) into one."""
    summary = ReconciliationSummary()
    merged = DiffResult(engine=engine, source_rows=0, target_rows=0, summary=summary)
    totals: dict[str, int] = {}
    for part in results:
        merged.source_rows += part.source_rows
        merged.target_rows += part.target_rows
        for name, value in part.summary.model_dump().items():
            totals[name] = totals.get(name, 0) + int(value)
        for column, count in part.column_counts.items():
            merged.column_counts[column] = merged.column_counts.get(column, 0) + count
        room = cap - len(merged.mismatches)
        if room > 0:
            merged.mismatches.extend(part.mismatches[:room])
        for kind, rows in part.key_samples.items():
            bucket = merged.key_samples.setdefault(kind, [])
            bucket.extend(rows[: max(0, sample_size - len(bucket))])
        for name in ("source_query_ms", "target_query_ms", "transfer_ms", "compare_ms"):
            setattr(
                merged.timing,
                name,
                round(getattr(merged.timing, name) + getattr(part.timing, name), 3),
            )
        merged.notes.extend(n for n in part.notes if n not in merged.notes)
    merged.summary = ReconciliationSummary(**totals) if totals else summary
    return merged
