"""Resolve one table the way a run does (metadata only) and build its comparison plan."""

from __future__ import annotations

from dataclasses import dataclass

from dvf.compare.plan import ColumnPairing, ComparisonPlan, build_plan, pair_columns
from dvf.config.models import SuiteConfig, TableConfig
from dvf.core.enums import Status
from dvf.core.errors import DvfError, PlanError
from dvf.execution.runner import Runner, TableTask, _TableState
from dvf.models.results import DatasetPairResult
from dvf.security.masking import Privacy


@dataclass
class PreparedTable:
    """A table's resolved datasets, schemas, column pairing, and comparison plan."""

    task: TableTask
    state: _TableState
    pairing: ColumnPairing | None = None
    plan: ComparisonPlan | None = None
    plan_error: str | None = None


def prepare(runner: Runner, suite: SuiteConfig, table: TableConfig) -> PreparedTable:
    """Resolve ``table`` (catalog lookups only) and plan its comparison, as a run would."""
    state = _TableState(
        suite=suite,
        table=table,
        dataset=DatasetPairResult(name=table.label, status=Status.SKIPPED),
        checks=[],
    )
    prepared = PreparedTable(TableTask(suite, table), state)
    runner._resolve(state)
    if not suite.is_comparison:
        return prepared
    if state.source_schema is None or state.target_schema is None:
        missing = "source" if state.source_schema is None else "target"
        prepared.plan_error = f"the {missing} dataset does not exist"
        return prepared
    keys = runner._key_mappings(state)
    pairing = pair_columns(table, state.source_schema, state.target_schema, key_mappings=keys)
    prepared.pairing = pairing
    state.pairing = pairing
    assert state.source_conn is not None and state.target_conn is not None
    privacy = Privacy.from_config(
        suite.defaults.privacy, [m.target for m in table.columns.mappings if m.pii]
    )
    try:
        prepared.plan = build_plan(
            table,
            pairing,
            normalization=table.normalization,
            source_dialect=state.source_conn.sql_dialect,
            target_dialect=state.target_conn.sql_dialect,
            factory=runner._normalizer,
            source_expr_types=runner._source_expr_types(state, pairing),
            pii_columns=list(privacy.pii_columns),
            require_keys=False,
        )
    except (PlanError, DvfError) as exc:
        prepared.plan_error = str(exc)
    return prepared


def find_tasks(runner: Runner, table: str | None, suite: str | None) -> list[TableTask]:
    """The run's tables, optionally narrowed to one table name (and suite)."""
    tasks = runner._expand()
    if table is not None:
        tasks = [
            t
            for t in tasks
            if t.table.label.lower() == table.lower()
            and (suite is None or t.suite.name.lower() == suite.lower())
        ]
    return tasks
