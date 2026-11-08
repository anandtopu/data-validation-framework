"""``dvf trace``: follow one record through both sides, step by step.

For each paired column it shows the raw value, the value after each normalizer, and the
canonical value on both sides, then the row hash on each side, so a mismatch can be pinned to the
exact step that introduces it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from dvf.compare.plan import ComparisonPlan, PlannedColumn
from dvf.core.errors import ConfigError, PlanError
from dvf.diagnose.planning import find_tasks, prepare
from dvf.execution.runner import Runner
from dvf.normalize.canonical import canonical_text, row_hash_sql
from dvf.plugins.base import Connector, Relation
from dvf.security.masking import to_json_safe

Side_ = Literal["source", "target"]
MAX_ROWS = 5


@dataclass
class TraceColumn:
    column: str
    source_column: str
    is_key: bool
    steps: list[str]
    """Step names: ``raw``, each normalizer, then ``canonical``."""
    source: list[list[Any]] = field(default_factory=list)
    """Per matched source row, the value at each step."""
    target: list[list[Any]] = field(default_factory=list)
    equal: bool | None = None
    comparison: str = "exact"


@dataclass
class TraceReport:
    suite: str
    table: str
    key: list[str]
    canonical_key: list[str | None]
    source_rows: int
    target_rows: int
    columns: list[TraceColumn] = field(default_factory=list)
    source_hashes: list[str | None] = field(default_factory=list)
    target_hashes: list[str | None] = field(default_factory=list)
    queries: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _side(col: PlannedColumn, side: Side_) -> Any:
    return col.source if side == "source" else col.target


def _canonical_key(plan: ComparisonPlan, values: list[str]) -> list[str | None]:
    """The canonical form of the given key values, through the source key's normalizers."""
    out: list[str | None] = []
    for key, value in zip(plan.keys, values, strict=True):
        expr = key.source
        current: Any = value
        ctype = expr.raw_type
        for normalizer in expr.normalizers:
            try:
                current = normalizer.to_python(current, ctype)
            except NotImplementedError:
                break
            ctype = normalizer.output_type(ctype)
        try:
            out.append(canonical_text(current, expr.spec))
        except (ValueError, TypeError, ArithmeticError):
            out.append(str(value))
    return out


def _side_sql(
    plan: ComparisonPlan, side: Side_, relation: Relation, canonical_key: list[str | None]
) -> tuple[str, list[list[str]]]:
    dialect = plan.source_dialect if side == "source" else plan.target_dialect
    items: list[str] = []
    names: list[list[str]] = []
    for i, col in enumerate([*plan.keys, *plan.columns]):
        expr = _side(col, side)
        steps: list[tuple[str, str]] = [("raw", expr.raw)]
        current, ctype = expr.raw, expr.raw_type
        for normalizer in expr.normalizers:
            current = normalizer.to_sql(current, dialect, ctype)
            ctype = normalizer.output_type(ctype)
            steps.append((normalizer.plugin_name or type(normalizer).__name__, current))
        if col.pair_spec.hashable or col.is_key:
            steps.append(("canonical", expr.canonical))
        else:
            assert expr.numeric is not None
            steps.append(("canonical", expr.canonical))
            steps.append(("numeric", expr.numeric))
        column_names = []
        for j, (_, sql) in enumerate(steps):
            name = f"t{i}_{j}"
            items.append(f"{dialect.to_text(sql)} AS {name}")
            column_names.append(name)
        names.append(column_names)
    hashed = [_side(c, side).canonical for c in plan.columns if c.pair_spec.hashable]
    items.append(f"{row_hash_sql(hashed, dialect)} AS dvf_h")
    conditions = [
        f"{_side(k, side).canonical} = {dialect.literal(v)}"
        if v is not None
        else f"{_side(k, side).canonical} IS NULL"
        for k, v in zip(plan.keys, canonical_key, strict=True)
    ]
    sql = dialect.limit(
        f"SELECT {', '.join(items)}\nFROM {relation.sql}\nWHERE {' AND '.join(conditions)}",
        MAX_ROWS,
    )
    return sql, names


def trace(runner: Runner, *, table: str, keys: list[str], suite: str | None = None) -> TraceReport:
    tasks = find_tasks(runner, table, suite)
    if not tasks:
        raise ConfigError(
            f"No table named '{table}'" + (f" in suite '{suite}'" if suite else "") + ".",
            hint="Run 'dvf explain' to list the tables of the configuration.",
        )
    if len(tasks) > 1:
        raise ConfigError(
            f"Table '{table}' is in several suites: {', '.join(t.suite.name for t in tasks)}.",
            hint="Pass --suite to choose one.",
        )
    task = tasks[0]
    try:
        prepared = prepare(runner, task.suite, task.table)
        plan = prepared.plan
        if not task.suite.is_comparison:
            raise PlanError("trace compares a source and a target; this suite has no target.")
        if plan is None or not plan.keys:
            raise PlanError(
                prepared.plan_error or f"Table '{table}' has no key columns.",
                hint="Set 'keys' on the table so records can be found by key.",
            )
        if len(keys) != len(plan.keys):
            raise ConfigError(
                f"Table '{table}' has {len(plan.keys)} key column(s) "
                f"({', '.join(k.label for k in plan.keys)}); got {len(keys)} --key value(s).",
                hint="Pass one --key per key column, in order.",
            )
        canonical = _canonical_key(plan, keys)
        report = TraceReport(
            suite=task.suite.name,
            table=task.table.label,
            key=list(keys),
            canonical_key=canonical,
            source_rows=0,
            target_rows=0,
        )
        state = prepared.state
        rows: dict[str, list[dict[str, Any]]] = {}
        names: dict[str, list[list[str]]] = {}
        for side, conn, rel in (
            ("source", state.source_conn, state.source_rel),
            ("target", state.target_conn, state.target_rel),
        ):
            assert isinstance(conn, Connector) and rel is not None
            sql, names[side] = _side_sql(plan, side, rel, canonical)  # type: ignore[arg-type]
            report.queries[side] = sql
            rows[side] = conn.query(sql).to_pylist()
        report.source_rows = len(rows["source"])
        report.target_rows = len(rows["target"])
        for i, col in enumerate([*plan.keys, *plan.columns]):
            exact = col.pair_spec.hashable or col.is_key
            tail = ["canonical"] if exact else ["canonical", "numeric"]
            s_steps = ["raw", *[n.plugin_name or type(n).__name__ for n in col.source.normalizers]]
            t_steps = ["raw", *[n.plugin_name or type(n).__name__ for n in col.target.normalizers]]
            entry = TraceColumn(
                column=col.label,
                source_column=col.source_label,
                is_key=col.is_key,
                steps=(s_steps if len(s_steps) >= len(t_steps) else t_steps) + tail,
                comparison="exact" if exact else col.pair_spec.describe(),
            )
            entry.source = [
                [to_json_safe(r[n]) for n in names["source"][i]] for r in rows["source"]
            ]
            entry.target = [
                [to_json_safe(r[n]) for n in names["target"][i]] for r in rows["target"]
            ]
            if entry.source and entry.target:
                s_val, t_val = entry.source[0][-1], entry.target[0][-1]
                if exact:
                    entry.equal = s_val == t_val
                else:
                    numeric = col.pair_spec.numeric
                    entry.equal = numeric is not None and numeric.within(
                        None if s_val is None else float(s_val),
                        None if t_val is None else float(t_val),
                    )
            report.columns.append(entry)
        report.source_hashes = [r["dvf_h"] for r in rows["source"]]
        report.target_hashes = [r["dvf_h"] for r in rows["target"]]
        if not report.source_rows:
            report.notes.append("No source row has this key.")
        if not report.target_rows:
            report.notes.append("No target row has this key.")
        if report.source_rows > 1 or report.target_rows > 1:
            report.notes.append("The key is duplicated; up to five rows per side are shown.")
        return report
    finally:
        runner.pool.close_all()
