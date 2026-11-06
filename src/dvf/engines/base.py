"""Row-level comparison: the job, the result, and the SQL that both SQL engines share.

A diff pairs rows by key and reports key reconciliation (REC-03), rows whose canonical content
differs (REC-04), and, in ``full`` mode, per-column mismatches with side-by-side values (REC-05).

Each side is first *projected*: keys and columns are normalized and canonicalized in that side's
own database (pushdown), producing these columns:

====== =====================================================================
k{i}   canonical key text (used to join)
rk{i}  raw key text (for display)
h      row hash over hashable canonical columns (``hash`` mode)
c{j}   canonical text of compared column j (hashable columns, ``full`` mode)
n{j}   numeric value of tolerance-compared column j
r{j}   raw text of column j (``full`` mode)
====== =====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from dvf.compare.plan import ComparisonPlan, PlannedColumn
from dvf.execution.context import QueryRecorder
from dvf.models.results import ReconciliationSummary, TimingBreakdown
from dvf.normalize.canonical import row_hash_sql
from dvf.plugins.base import Component, Connector, Relation
from dvf.sql.dialects import Dialect

DiffMode = Literal["hash", "full"]


@dataclass
class DiffSide:
    connector: Connector
    relation: Relation


@dataclass
class DiffJob:
    plan: ComparisonPlan
    source: DiffSide
    target: DiffSide
    mode: DiffMode
    sample_size: int
    max_mismatch_rows: int
    recorder: QueryRecorder = field(default_factory=QueryRecorder)

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

    @property
    def rows_with_differences(self) -> int:
        s = self.summary
        return s.mismatched + s.missing_in_target + s.extra_in_target


class Engine(Component):
    """Base class for row-level comparison engines."""

    name: str = ""

    def diff(self, job: DiffJob) -> DiffResult:  # pragma: no cover - interface
        raise NotImplementedError


# ====================================================================== projection


def projection_sql(
    plan: ComparisonPlan, side: Literal["source", "target"], relation_sql: str, mode: DiffMode
) -> str:
    """SELECT producing a side's canonical projection, in that side's dialect."""
    dialect = plan.source_dialect if side == "source" else plan.target_dialect
    items: list[str] = []
    for i, key in enumerate(plan.keys):
        expr = key.source if side == "source" else key.target
        items.append(f"{expr.canonical} AS k{i}")
        items.append(f"{dialect.to_text(expr.raw)} AS rk{i}")
    if mode == "hash":
        hashed = [
            (c.source if side == "source" else c.target).canonical
            for c in plan.columns
            if c.pair_spec.hashable
        ]
        items.append(f"{row_hash_sql(hashed, dialect)} AS h")
    for j, col in enumerate(plan.columns):
        expr = col.source if side == "source" else col.target
        if col.pair_spec.hashable:
            if mode == "full":
                items.append(f"{expr.canonical} AS c{j}")
        else:
            assert expr.numeric is not None
            items.append(f"{expr.numeric} AS n{j}")
        if mode == "full":
            items.append(f"{dialect.to_text(expr.raw)} AS r{j}")
    return "SELECT\n  " + ",\n  ".join(items) + f"\nFROM {relation_sql}"


def projected_columns(plan: ComparisonPlan, mode: DiffMode) -> list[tuple[str, str]]:
    """(name, kind) of projection columns, where kind is ``text`` or ``double``."""
    cols: list[tuple[str, str]] = []
    for i in range(len(plan.keys)):
        cols += [(f"k{i}", "text"), (f"rk{i}", "text")]
    if mode == "hash":
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


# ====================================================================== diff SQL


def _tolerance_mismatch(col: PlannedColumn, j: int, dialect: Dialect) -> str:
    numeric = col.pair_spec.numeric
    assert numeric is not None
    s, t = f"s1.n{j}", f"t1.n{j}"
    allowed = dialect.greatest(
        repr(float(numeric.abs_tol)),
        f"{float(numeric.rel_tol)!r} * {dialect.greatest(f'ABS({s})', f'ABS({t})')}",
    )
    return (
        f"(CASE WHEN {s} IS NULL AND {t} IS NULL THEN 0 WHEN {s} IS NULL OR {t} IS NULL THEN 1 "
        f"WHEN ABS({s} - {t}) > {allowed} THEN 1 ELSE 0 END)"
    )


class SqlDiff:
    """Builds the diff queries over two projected relations in one dialect."""

    def __init__(
        self,
        plan: ComparisonPlan,
        dialect: Dialect,
        mode: DiffMode,
        source_rel: str,
        target_rel: str,
    ) -> None:
        self.plan = plan
        self.dialect = dialect
        self.mode = mode
        self.source_rel = source_rel
        self.target_rel = target_rel
        self.columns = projected_columns(plan, mode)
        self.key_names = [f"k{i}" for i in range(len(plan.keys))]

    def _order(self) -> str:
        names = [n for n, _ in self.columns if n == "h" or n[0] in "cn"]
        return ", ".join(names or self.key_names)

    def _flags(self) -> list[str]:
        flags: list[str] = []
        for j, col in enumerate(self.plan.columns):
            if col.pair_spec.hashable:
                if self.mode == "full":
                    cond = self.dialect.is_distinct(f"s1.c{j}", f"t1.c{j}")
                    flags.append(f"(CASE WHEN {cond} THEN 1 ELSE 0 END) AS m{j}")
            else:
                flags.append(f"{_tolerance_mismatch(col, j, self.dialect)} AS m{j}")
        return flags

    def _row_flag(self) -> str:
        parts = []
        if self.mode == "hash":
            parts.append(self.dialect.is_distinct("s1.h", "t1.h"))
        for j, col in enumerate(self.plan.columns):
            if self.mode == "full" and col.pair_spec.hashable:
                parts.append(self.dialect.is_distinct(f"s1.c{j}", f"t1.c{j}"))
            elif not col.pair_spec.hashable:
                parts.append(f"{_tolerance_mismatch(col, j, self.dialect)} = 1")
        if not parts:
            return "0"
        return f"(CASE WHEN {' OR '.join(parts)} THEN 1 ELSE 0 END)"

    def ctes(self) -> str:
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
        order = self._order()
        return (
            "WITH\n"
            f"s_all AS (SELECT * FROM {self.source_rel}),\n"
            f"t_all AS (SELECT * FROM {self.target_rel}),\n"
            f"s_nn AS (SELECT * FROM s_all WHERE {not_null}),\n"
            f"t_nn AS (SELECT * FROM t_all WHERE {not_null}),\n"
            f"s_rn AS (SELECT s_nn.*, ROW_NUMBER() OVER (PARTITION BY {keys} ORDER BY {order}) AS dvf_rn FROM s_nn),\n"
            f"t_rn AS (SELECT t_nn.*, ROW_NUMBER() OVER (PARTITION BY {keys} ORDER BY {order}) AS dvf_rn FROM t_nn),\n"
            "s1 AS (SELECT * FROM s_rn WHERE dvf_rn = 1),\n"
            "t1 AS (SELECT * FROM t_rn WHERE dvf_rn = 1),\n"
            f"j AS (\n  SELECT {', '.join(selected)}\n  FROM s1 FULL OUTER JOIN t1 ON {join}\n)\n"
        )

    def stats_sql(self) -> str:
        both = "in_s = 1 AND in_t = 1"
        items = [
            "(SELECT COUNT(*) FROM s_all) AS source_rows",
            "(SELECT COUNT(*) FROM t_all) AS target_rows",
            "(SELECT COUNT(*) FROM s_nn) AS source_nn",
            "(SELECT COUNT(*) FROM t_nn) AS target_nn",
            "(SELECT COUNT(*) FROM s1) AS source_unique",
            "(SELECT COUNT(*) FROM t1) AS target_unique",
            "COALESCE(SUM(CASE WHEN in_s = 1 AND in_t = 0 THEN 1 ELSE 0 END), 0) AS missing",
            "COALESCE(SUM(CASE WHEN in_s = 0 AND in_t = 1 THEN 1 ELSE 0 END), 0) AS extra",
            f"COALESCE(SUM(CASE WHEN {both} AND mrow = 1 THEN 1 ELSE 0 END), 0) AS mismatched",
            f"COALESCE(SUM(CASE WHEN {both} AND mrow = 0 THEN 1 ELSE 0 END), 0) AS matched",
        ]
        for name in self._flag_names():
            items.append(
                f"COALESCE(SUM(CASE WHEN {both} THEN {name} ELSE 0 END), 0) AS {name}_count"
            )
        return self.ctes() + "SELECT " + ",\n  ".join(items) + "\nFROM j"

    def _flag_names(self) -> list[str]:
        return [
            f"m{j}"
            for j, col in enumerate(self.plan.columns)
            if self.mode == "full" or not col.pair_spec.hashable
        ]

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
                f"FROM j WHERE in_s = 1 AND in_t = 1 AND m{j} = 1 ORDER BY {order}"
            )
            parts.append(f"SELECT * FROM ({q.limit(inner, limit)}) AS q{j}")
        return self.ctes() + "\nUNION ALL\n".join(parts)

    def keys_sql(self, limit: int) -> str:
        """Samples of missing, extra, duplicate, and NULL keys (raw key text plus a count)."""
        q = self.dialect
        n = len(self.plan.keys)
        side_keys = {s: ", ".join(f"{s}_rk{i} AS key{i}" for i in range(n)) for s in ("s", "t")}
        order = {s: ", ".join(f"{s}_k{i}" for i in range(n)) for s in ("s", "t")}
        raw_min = ", ".join(f"MIN(rk{i}) AS key{i}" for i in range(n))
        raw = ", ".join(f"rk{i} AS key{i}" for i in range(n))
        keys = ", ".join(self.key_names)
        null_any = " OR ".join(f"{k} IS NULL" for k in self.key_names)
        selects = [
            (
                "missing_in_target",
                f"SELECT {side_keys['s']}, 1 AS dvf_count FROM j WHERE in_s = 1 AND in_t = 0 ORDER BY {order['s']}",
            ),
            (
                "extra_in_target",
                f"SELECT {side_keys['t']}, 1 AS dvf_count FROM j WHERE in_s = 0 AND in_t = 1 ORDER BY {order['t']}",
            ),
            (
                "duplicate_in_source",
                f"SELECT {raw_min}, COUNT(*) AS dvf_count FROM s_nn GROUP BY {keys} HAVING COUNT(*) > 1 ORDER BY {keys}",
            ),
            (
                "duplicate_in_target",
                f"SELECT {raw_min}, COUNT(*) AS dvf_count FROM t_nn GROUP BY {keys} HAVING COUNT(*) > 1 ORDER BY {keys}",
            ),
            ("null_key_in_source", f"SELECT {raw}, 1 AS dvf_count FROM s_all WHERE {null_any}"),
            ("null_key_in_target", f"SELECT {raw}, 1 AS dvf_count FROM t_all WHERE {null_any}"),
        ]
        parts = [
            f"SELECT {q.literal(kind)} AS dvf_kind, x.* FROM ({q.limit(sql, limit)}) AS x"
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


def run_sql_diff(
    builder: SqlDiff,
    execute: Any,
    job: DiffJob,
    engine: str,
) -> DiffResult:
    """Run the stats, mismatch, and key-sample queries with ``execute(sql) -> list[dict]``."""
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
    if job.mode == "full":
        flagged = [j for j, c in enumerate(job.plan.columns) if result.column_counts.get(c.label)]
        if flagged:
            per_column = max(1, job.max_mismatch_rows // len(flagged))
            for row in execute(builder.mismatch_sql(flagged, per_column)):
                result.mismatches.append(_mismatch_row(row, len(job.plan.keys)))
    has_key_issues = any(
        (
            summary.missing_in_target,
            summary.extra_in_target,
            summary.duplicate_keys_source,
            summary.duplicate_keys_target,
            summary.null_keys_source,
            summary.null_keys_target,
        )
    )
    if has_key_issues and job.sample_size > 0:
        for row in execute(builder.keys_sql(job.sample_size)):
            values = [row[f"key{i}"] for i in range(len(job.plan.keys))]
            result.key_samples.setdefault(row["dvf_kind"], []).append([*values, row["dvf_count"]])
    return result


def _mismatch_row(row: dict[str, Any], n_keys: int) -> MismatchRow:
    return MismatchRow(
        keys=[row[f"key{i}"] for i in range(n_keys)],
        column=row["dvf_column"],
        source_value=row["source_value"],
        target_value=row["target_value"],
        source_canonical=row["source_canonical"],
        target_canonical=row["target_canonical"],
    )
