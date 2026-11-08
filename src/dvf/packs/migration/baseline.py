"""MIG-14: pre-migration baseline capture and the ``baseline_metrics`` check.

``dvf baseline capture CONFIG --name pre_cutover`` records, per table, the row count, per-column
control totals (NULL count, distinct count, sum, min, max), and grouped row counts (the table's
``comparison.group_by``) of one side, under ``<state_dir>/baselines/<name>.json``. After the
migration, the ``baseline_metrics`` check compares the same metrics of either side with the
named baseline within tolerances: a go/no-go gate, and evidence for a rollback decision.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dvf import __version__
from dvf.checks.context import CheckContext, CheckOutcome
from dvf.checks.pair import EnvironmentAware
from dvf.config.models import NormalizationConfig, TableConfig
from dvf.core.datasets import ColumnSchema, DatasetSchema
from dvf.core.enums import Dimension, Side
from dvf.core.errors import DvfError, PlanError
from dvf.execution.history import load_named_baseline, save_named_baseline
from dvf.models.results import MetricComparison
from dvf.normalize.canonical import Kind, canonical_sql, canonical_text, single_spec
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int
from dvf.plugins.base import Check, Connector
from dvf.security.masking import to_json_safe
from dvf.sql.dialects import Dialect

BASELINE_FORMAT = 1
MAX_GROUPS = 10_000
MAX_GROUP_METRICS = 500
_NUMERIC = (Kind.INTEGER, Kind.DECIMAL, Kind.FLOAT)
_TEMPORAL = (Kind.DATE, Kind.TIMESTAMP, Kind.TIME)

QueryRunner = Callable[[str], list[dict[str, Any]]]


def _lower(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{str(k).lower(): v for k, v in r.items()} for r in rows]


def _number_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return repr(value)
    return str(value)


def collect_metrics(
    run: QueryRunner,
    dialect: Dialect,
    from_sql: str,
    schema: DatasetSchema,
    normalization: NormalizationConfig,
    *,
    columns: Sequence[str] | None = None,
    distinct: bool = True,
    group_by: Sequence[str] = (),
) -> dict[str, Any]:
    """Row count, per-column control totals, and grouped row counts of one dataset.

    ``from_sql`` is ``FROM <relation> [WHERE ...]``. Sums and numeric extremes are kept as exact
    text; temporal extremes as canonical text (UTC, normalization precision).
    """
    selected: list[ColumnSchema] = []
    for name in columns if columns is not None else [c.name for c in schema.columns]:
        found = schema.column(name)
        if found is not None:
            selected.append(found)
    items = ["COUNT(*) AS dvf_rows"]
    specs = {}
    for i, col in enumerate(selected):
        q = dialect.quote(col.name)
        spec = single_spec(col.type, normalization)
        specs[col.name] = spec
        items.append(f"SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS m{i}_null_count")
        if distinct and spec.kind is not Kind.BINARY:
            items.append(
                f"COUNT(DISTINCT {canonical_sql(q, spec, dialect)}) AS m{i}_count_distinct"
            )
        if spec.kind in _NUMERIC:
            value = dialect.to_double(q) if spec.kind is Kind.FLOAT else q
            items.append(f"SUM({value}) AS m{i}_sum")
        if spec.kind in _NUMERIC or spec.kind in _TEMPORAL:
            items += [f"MIN({q}) AS m{i}_min", f"MAX({q}) AS m{i}_max"]
    row = _lower(run(f"SELECT {', '.join(items)}\n{from_sql}"))[0]
    result: dict[str, Any] = {"row_count": as_int(row.get("dvf_rows")), "columns": {}}
    for i, col in enumerate(selected):
        spec = specs[col.name]
        entry: dict[str, Any] = {
            "type": col.type.logical.value,
            "null_count": as_int(row.get(f"m{i}_null_count")),
        }
        if f"m{i}_count_distinct" in row:
            entry["count_distinct"] = as_int(row.get(f"m{i}_count_distinct"))
        if f"m{i}_sum" in row:
            entry["sum"] = _number_text(row.get(f"m{i}_sum"))
        for agg in ("min", "max"):
            if f"m{i}_{agg}" not in row:
                continue
            extreme = row.get(f"m{i}_{agg}")
            if extreme is None:
                entry[agg] = None
            elif spec.kind in _TEMPORAL:
                entry[agg] = canonical_text(extreme, spec)
            else:
                entry[agg] = _number_text(extreme)
        result["columns"][col.name] = entry
    groups = [schema.column(g) for g in group_by]
    if group_by and all(g is not None for g in groups):
        exprs = [
            canonical_sql(dialect.quote(g.name), single_spec(g.type, normalization), dialect)
            for g in groups
            if g is not None
        ]
        select = ", ".join(f"{e} AS g{i}" for i, e in enumerate(exprs))
        rows = _lower(
            run(
                dialect.limit(
                    f"SELECT {select}, COUNT(*) AS n\n{from_sql}\nGROUP BY {', '.join(exprs)}",
                    MAX_GROUPS + 1,
                )
            )
        )
        result["group_by"] = [g.name for g in groups if g is not None]
        result["groups"] = [
            [[r.get(f"g{i}") for i in range(len(exprs))], as_int(r.get("n"))] for r in rows
        ]
        result["groups_truncated"] = len(rows) > MAX_GROUPS
    return result


# ====================================================================== capture


def capture_baseline(
    loaded: Any,
    name: str,
    *,
    side: str = "source",
    only: Sequence[str] = (),
    overwrite: bool = False,
    distinct: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Capture the metrics of ``side`` of every selected table and store them as baseline
    ``name`` in the configuration's state folder. Returns the file and the document."""
    from dvf.diagnose.planning import prepare
    from dvf.execution.runner import Runner, RunOptions
    from dvf.execution.selectors import Selectors

    if side not in ("source", "target"):
        raise PlanError(f"side must be 'source' or 'target', got '{side}'.")
    runner = Runner(
        loaded,
        RunOptions(selectors=Selectors.parse(list(only)), write_reports=False, history=False),
    )
    document: dict[str, Any] = {
        "format": BASELINE_FORMAT,
        "name": name,
        "project": loaded.config.project.name,
        "side": side,
        "captured_at": datetime.now(UTC).isoformat(),
        "config_hash": runner.config_hash,
        "config_files": [str(f) for f in loaded.files],
        "profile": loaded.profile,
        "dvf_version": __version__,
        "tables": {},
    }
    try:
        for task in runner._expand():
            key = f"{task.suite.name}.{task.table.label}"
            try:
                prepared = prepare(runner, task.suite, task.table)
                state = prepared.state
                if side == "target":
                    conn, rel, schema = state.target_conn, state.target_rel, state.target_schema
                else:
                    conn, rel, schema = state.source_conn, state.source_rel, state.source_schema
                if conn is None or rel is None or schema is None:
                    document["tables"][key] = {"error": f"there is no {side} dataset"}
                    continue
                metrics = collect_metrics(
                    _rows_of(conn),
                    conn.sql_dialect,
                    f"FROM {rel.sql}",
                    schema,
                    task.table.normalization,
                    distinct=distinct,
                    group_by=_side_names(task.table, task.table.comparison.group_by, side, schema),
                )
                document["tables"][key] = {"dataset": rel.ref.display(), **metrics}
            except DvfError as exc:
                document["tables"][key] = {"error": str(exc)}
    finally:
        runner.pool.close_all()
    path = save_named_baseline(runner.state_dir, name, document, overwrite=overwrite)
    return path, document


def _rows_of(connector: Connector) -> QueryRunner:
    def run(sql: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = connector.query(sql).to_pylist()
        return rows

    return run


def _side_names(
    table: TableConfig, names: Sequence[str], side: str, schema: DatasetSchema
) -> list[str]:
    """``names`` (source or target column names) as they are called on ``side``."""
    out: list[str] = []
    for name in names:
        if schema.column(name) is not None:
            out.append(name)
            continue
        translated = _to_target(table, name) if side == "target" else _to_source(table, name)
        if translated is not None and schema.column(translated) is not None:
            out.append(translated)
    return out


def _to_target(table: TableConfig, source_name: str) -> str | None:
    lowered = source_name.lower()
    for key in table.keys:
        if key.source.lower() == lowered:
            return key.target_name
    for mapping in table.columns.mappings:
        if mapping.source is not None and mapping.source.lower() == lowered:
            return mapping.target
    return None


def _to_source(table: TableConfig, target_name: str) -> str | None:
    lowered = target_name.lower()
    for key in table.keys:
        if key.target_name.lower() == lowered:
            return key.source
    for mapping in table.columns.mappings:
        if mapping.target.lower() == lowered and mapping.source is not None:
            return mapping.source
    return None


# ====================================================================== check


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class BaselineMetrics(EnvironmentAware, Check):
    """Current metrics match a named pre-migration baseline within tolerances (MIG-14).

    Captured column names are translated to the checked side through the table's keys and
    column mappings, so a source baseline can be compared with the target after cutover.
    """

    dimension = Dimension.RECONCILIATION
    Config = cfg.BaselineMetricsCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.BaselineMetricsCheck)
        assert ctx.schema is not None
        environment = self.require_environment()
        document = load_named_baseline(environment.state_dir, config.baseline)
        key = config.baseline_table or f"{environment.suite.name}.{environment.table.label}"
        tables = document.get("tables", {})
        entry = tables.get(key)
        if entry is None:
            raise PlanError(
                f"Baseline '{config.baseline}' has no table '{key}'.",
                hint=f"Tables in it: {', '.join(sorted(tables)) or 'none'}. "
                "Set 'baseline_table' to compare with another one.",
            )
        if "error" in entry:
            raise PlanError(
                f"Baseline '{config.baseline}' could not capture table '{key}': {entry['error']}"
            )
        captured_side = str(document.get("side", "source"))
        checked_side = "target" if ctx.side is Side.TARGET else "source"
        table = environment.table
        baseline_columns: dict[str, Any] = entry.get("columns", {})
        wanted = config.columns or list(baseline_columns)
        names: dict[str, str] = {}
        missing: list[str] = []
        for name in wanted:
            current_name = name
            if captured_side != checked_side:
                translated = (
                    _to_target(table, name) if checked_side == "target" else _to_source(table, name)
                )
                current_name = translated or name
            found = ctx.schema.column(current_name)
            if found is None or name not in baseline_columns:
                missing.append(name)
            else:
                names[name] = found.name
        group_by = (
            _side_names(table, entry.get("group_by", []), checked_side, ctx.schema)
            if "groups" in config.metrics and entry.get("groups") is not None
            else []
        )
        current = collect_metrics(
            lambda sql: ctx.query(sql).to_pylist(),
            ctx.dialect,
            ctx.from_where(),
            ctx.schema,
            ctx.normalization,
            columns=list(names.values()),
            distinct="count_distinct" in config.metrics,
            group_by=group_by,
        )
        metrics, failures = self._compare(config, entry, current, names, missing)
        captured = str(document.get("captured_at", ""))[:19]
        label = f"baseline '{config.baseline}' (captured {captured})"
        if failures:
            shown = ", ".join(failures[:8]) + (" …" if len(failures) > 8 else "")
            message = f"{len(failures):,} of {len(metrics):,} metrics differ from {label}: {shown}"
        else:
            message = f"all {len(metrics):,} metrics match {label}"
        return CheckOutcome(
            passed=not failures,
            message=message,
            observed={"row_count": current["row_count"]},
            expected={"row_count": entry.get("row_count")},
            unexpected_count=len(failures),
            total_count=len(metrics),
            metrics=metrics[:MAX_GROUP_METRICS] if len(metrics) > MAX_GROUP_METRICS else metrics,
            hints=[f"columns not found now or not in the baseline: {', '.join(missing)}"]
            if missing
            else [],
            details={
                "baseline": config.baseline,
                "baseline_table": key,
                "baseline_side": captured_side,
                "captured_at": document.get("captured_at"),
                "failed_metrics": failures[:100],
            },
        )

    @staticmethod
    def _compare(
        config: cfg.BaselineMetricsCheck,
        entry: dict[str, Any],
        current: dict[str, Any],
        names: dict[str, str],
        missing: list[str],
    ) -> tuple[list[MetricComparison], list[str]]:
        tol = config.tolerance
        metrics: list[MetricComparison] = []
        failures: list[str] = []

        def add(
            metric: str,
            column: str | None,
            before: Any,
            after: Any,
            passed: bool,
            difference: float | None = None,
            tolerance: float | None = None,
            group: dict[str, Any] | None = None,
        ) -> None:
            metrics.append(
                MetricComparison(
                    metric=metric,
                    column=column,
                    group=group,
                    source=to_json_safe(before),
                    target=to_json_safe(after),
                    difference=difference,
                    tolerance=tolerance,
                    passed=passed,
                    note="baseline, current",
                )
            )
            if not passed:
                failures.append(f"{metric}({column})" if column else metric)

        if "row_count" in config.metrics:
            before, after = as_int(entry.get("row_count")), as_int(current["row_count"])
            allowed = abs(before) * tol.row_count_pct / 100.0
            add(
                "row_count",
                None,
                before,
                after,
                abs(after - before) <= allowed,
                float(after - before),
                allowed or None,
            )
        for name in missing:
            add("column_present", name, True, False, False)
        current_columns: dict[str, Any] = current["columns"]
        for name, real in names.items():
            base = entry["columns"].get(name, {})
            now = current_columns.get(real, {})
            for metric in ("null_count", "count_distinct", "sum", "min", "max"):
                if metric not in config.metrics or metric not in base or metric not in now:
                    continue
                b, a = base.get(metric), now.get(metric)
                db, da = _decimal(b), _decimal(a)
                if metric == "sum" and db is not None and da is not None:
                    allowed_sum = max(
                        Decimal(repr(tol.numeric_abs)),
                        Decimal(repr(tol.numeric_rel)) * max(abs(db), abs(da)),
                    )
                    add(
                        metric,
                        real,
                        b,
                        a,
                        abs(da - db) <= allowed_sum,
                        float(da - db),
                        float(allowed_sum) or None,
                    )
                elif db is not None and da is not None:
                    add(metric, real, b, a, da == db, float(da - db))
                else:
                    add(metric, real, b, a, b == a)
        if "groups" in config.metrics and entry.get("groups") is not None and "groups" in current:
            labels = list(current.get("group_by", []))
            before_groups = {tuple(g): n for g, n in entry["groups"]}
            after_groups = {tuple(g): n for g, n in current["groups"]}
            differing = 0
            for group in sorted(set(before_groups) | set(after_groups), key=str):
                b, a = before_groups.get(group, 0), after_groups.get(group, 0)
                allowed = abs(b) * tol.row_count_pct / 100.0
                if abs(a - b) <= allowed:
                    continue
                differing += 1
                if differing <= MAX_GROUP_METRICS:
                    add(
                        "group_row_count",
                        None,
                        b,
                        a,
                        False,
                        float(a - b),
                        allowed or None,
                        dict(zip(labels, group, strict=False)),
                    )
            if not differing:
                add("group_row_count", None, len(before_groups), len(after_groups), True)
        return metrics, failures


__all__ = ["BaselineMetrics", "capture_baseline", "collect_metrics"]
