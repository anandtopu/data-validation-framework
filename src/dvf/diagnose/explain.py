"""``dvf explain``: the run plan and the SQL each step would run, without running validations.

Only catalog lookups (does the dataset exist, what are its columns) touch the connections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from dvf.checks.context import CheckContext
from dvf.compare.buckets import HashGrid, base_sql, integer_key, stats_sql
from dvf.compare.comparators import _grouped_select, group_columns
from dvf.compare.context import ComparisonContext
from dvf.compare.partitions import plan_partitions
from dvf.config.models import TableConfig
from dvf.core.enums import Side
from dvf.core.errors import DvfError
from dvf.diagnose.planning import PreparedTable, find_tasks, prepare
from dvf.engines.base import DiffSide, projection_sql
from dvf.execution.runner import Runner
from dvf.models.results import make_check_id
from dvf.plugins.base import RowCheck
from dvf.plugins.registry import PluginKind
from dvf.security.masking import Privacy

ROW_LEVEL = ("row_hash", "hash_bucket", "bisect", "full_diff")


@dataclass
class PlannedQuery:
    purpose: str
    side: str
    dialect: str
    sql: str


@dataclass
class PlannedCheck:
    check_id: str
    type: str
    side: str
    severity: str
    sql: str | None = None


@dataclass
class ExplainedTable:
    suite: str
    table: str
    source: str | None
    target: str | None
    engine: str
    row_engine: str | None = None
    keys: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    strategies: list[str] = field(default_factory=list)
    on_mismatch: str | None = None
    partitions: list[dict[str, Any]] = field(default_factory=list)
    queries: list[PlannedQuery] = field(default_factory=list)
    checks: list[PlannedCheck] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class ExplainReport:
    project: str
    parallel_tables: int
    connection_caps: dict[str, int]
    tables: list[ExplainedTable] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_engine(prepared: PreparedTable) -> str:
    state = prepared.state
    engine = prepared.task.table.engine
    if engine != "auto":
        return engine
    assert state.source_conn is not None and state.target_conn is not None
    local = state.source_conn.dialect == "duckdb" and state.target_conn.dialect == "duckdb"
    return "pushdown" if state.source_conn is state.target_conn and not local else "duckdb"


def _context(runner: Runner, prepared: PreparedTable) -> ComparisonContext:
    state = prepared.state
    assert state.source_conn and state.target_conn and state.source_rel and state.target_rel
    assert state.source_schema is not None and state.target_schema is not None
    assert prepared.pairing is not None
    table = prepared.task.table
    return ComparisonContext(
        suite=prepared.task.suite,
        table=table,
        source=DiffSide(state.source_conn, state.source_rel),
        target=DiffSide(state.target_conn, state.target_rel),
        source_schema=state.source_schema,
        target_schema=state.target_schema,
        pairing=prepared.pairing,
        columns_plan=prepared.plan,
        plan_error=None,
        engine=table.engine,
        registry=runner.registry,
        privacy=Privacy.from_config(prepared.task.suite.defaults.privacy, []),
        sample_size=table.sample_size,
        normalization=table.normalization,
        dataset=state.dataset,
        run=runner.info,
    )


def _comparison_queries(runner: Runner, prepared: PreparedTable, out: ExplainedTable) -> None:
    plan = prepared.plan
    table = prepared.task.table
    ctx = _context(runner, prepared)
    sd, td = ctx.source.connector.sql_dialect, ctx.target.connector.sql_dialect
    strategies = [*table.comparison.strategies]
    if table.comparison.on_mismatch and table.comparison.on_mismatch not in strategies:
        strategies.append(table.comparison.on_mismatch)
    if "row_count" in strategies:
        groups = group_columns(ctx)
        out.queries += [
            PlannedQuery(
                "row_count",
                "source",
                sd.name,
                _grouped_select(
                    ["COUNT(*) AS n"], [g.source_sql for g in groups], ctx.source.relation.sql, sd
                ),
            ),
            PlannedQuery(
                "row_count",
                "target",
                td.name,
                _grouped_select(
                    ["COUNT(*) AS n"], [g.target_sql for g in groups], ctx.target.relation.sql, td
                ),
            ),
        ]
    if plan is None or not plan.keys or not any(s in ROW_LEVEL for s in strategies):
        return
    config = table.comparison.partitions
    if config is not None and config.values == "auto":
        out.partitions = [
            {"name": f"one per value of {config.column} (read from the source when the run starts)"}
        ]
    else:
        out.partitions = [
            {"name": p.name, "source": p.source, "target": p.target}
            for p in plan_partitions(ctx, plan)
        ]
    for side, rel, dialect in (
        ("source", ctx.source.relation.sql, sd),
        ("target", ctx.target.relation.sql, td),
    ):
        out.queries.append(
            PlannedQuery(
                "row-level projection (keys, row hash, tolerance columns); full values are fetched "
                "afterwards for differing rows only",
                side,
                dialect.name,
                projection_sql(plan, side, rel, "hash"),  # type: ignore[arg-type]
            )
        )
        if "hash_bucket" in strategies:
            grid = HashGrid(table.comparison.buckets, table.comparison.drilldown_fanout)
            base = base_sql(plan, side, rel, None)  # type: ignore[arg-type]
            out.queries.append(
                PlannedQuery(
                    f"hash_bucket level 0 ({grid.describe(0)})",
                    side,
                    dialect.name,
                    stats_sql(plan, side, base, grid.group(dialect, 0, "x"), None, with_nulls=True),  # type: ignore[arg-type]
                )
            )
        if "bisect" in strategies:
            try:
                values = integer_key(plan)
            except DvfError as exc:
                out.problems.append(str(exc))
            else:
                value = values[0 if side == "source" else 1]
                out.queries.append(
                    PlannedQuery(
                        "bisect key range (segments follow from it)",
                        side,
                        dialect.name,
                        f"SELECT MIN({value}) AS lo, MAX({value}) AS hi FROM {rel}",
                    )
                )


def _check_sql(runner: Runner, prepared: PreparedTable, config: Any, side: Side) -> str | None:
    state = prepared.state
    conn, rel, schema = (
        (state.target_conn, state.target_rel, state.target_schema)
        if side is Side.TARGET
        else (state.source_conn, state.source_rel, state.source_schema)
    )
    if conn is None or rel is None:
        return None
    entry = runner.registry.find(PluginKind.CHECK, config.type)
    if entry is None or not entry.ready:
        return None
    check = entry.implementation(config)
    table: TableConfig = prepared.task.table
    ctx = CheckContext(
        config=config,
        side=side,
        connector=conn,
        relation=rel,
        schema=schema,
        sample_size=table.sample_size,
        key_columns=[k.source if side is Side.SOURCE else k.target_name for k in table.keys],
        run=runner.info,
        connectors=runner.pool,
        privacy=Privacy.from_config(prepared.task.suite.defaults.privacy, []),
        normalization=table.normalization,
    )
    if isinstance(check, RowCheck):
        predicate = check.predicate(ctx)
        return (
            "SELECT COUNT(*) AS total_count, "
            f"SUM(CASE WHEN ({predicate}) THEN 0 ELSE 1 END) AS unexpected_count\n{ctx.from_where()}"
        )
    sql = getattr(config, "sql", None)
    if isinstance(sql, str):
        return ctx.render(sql).replace("{table}", ctx.table_reference())
    return None


def explain(runner: Runner, *, table: str | None = None, suite: str | None = None) -> ExplainReport:
    config = runner.config
    report = ExplainReport(
        project=config.project.name,
        parallel_tables=runner._workers(10**6),
        connection_caps={name: runner.pool.cap(name) for name in config.connections},
    )
    try:
        for task in find_tasks(runner, table, suite):
            out = ExplainedTable(
                suite=task.suite.name,
                table=task.table.label,
                source=None,
                target=None,
                engine=task.table.engine,
                strategies=list(task.table.comparison.strategies)
                if task.suite.is_comparison
                else [],
                on_mismatch=task.table.comparison.on_mismatch if task.suite.is_comparison else None,
            )
            report.tables.append(out)
            try:
                prepared = prepare(runner, task.suite, task.table)
            except DvfError as exc:
                out.problems.append(str(exc))
                continue
            state = prepared.state
            out.source = state.dataset.source.display() if state.dataset.source else None
            out.target = state.dataset.target.display() if state.dataset.target else None
            if prepared.plan_error:
                out.problems.append(prepared.plan_error)
            if task.suite.is_comparison and prepared.plan is not None:
                out.row_engine = _row_engine(prepared)
                out.keys = [k.describe() for k in prepared.plan.keys]
                out.columns = [c.describe() for c in prepared.plan.columns] + prepared.plan.notes
                if not prepared.plan.keys:
                    out.problems.append("no key columns: row-level strategies will be skipped")
                try:
                    _comparison_queries(runner, prepared, out)
                except DvfError as exc:
                    out.problems.append(str(exc))
            for check in task.table.checks:
                if not check.enabled:
                    continue
                for side in runner._sides(task.suite, check):
                    check_id = make_check_id(
                        task.suite.name,
                        task.table.label,
                        check.type,
                        columns=check.target_columns(),
                        name=check.name,
                    )
                    try:
                        sql = _check_sql(runner, prepared, check, side)
                    except DvfError as exc:
                        sql = None
                        out.problems.append(f"{check_id}: {exc}")
                    out.checks.append(
                        PlannedCheck(
                            check_id=check_id,
                            type=check.type,
                            side=side.value,
                            severity=(check.severity or task.table.severity).value,
                            sql=sql,
                        )
                    )
    finally:
        runner.pool.close_all()
    return report
