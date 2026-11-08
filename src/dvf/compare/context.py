"""What comparators see: both datasets, their schemas, the column pairing, and the plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from dvf.compare.plan import ColumnPairing, ComparisonPlan
from dvf.config.models import NormalizationConfig, SuiteConfig, TableConfig
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.engines.base import DiffSide, EngineResources
from dvf.execution.context import QueryRecorder, RunInfo
from dvf.models.results import DatasetPairResult
from dvf.plugins.registry import Registry
from dvf.security.masking import Privacy


class PartitionCheckpoints(Protocol):
    """Saved partition results of one table, so a resumed run skips finished partitions."""

    def load(self, key: str) -> dict[str, Any] | None: ...

    def save(self, key: str, data: dict[str, Any]) -> None: ...


@dataclass
class ComparisonContext:
    """Inputs shared by every comparison strategy of one table."""

    suite: SuiteConfig
    table: TableConfig
    source: DiffSide
    target: DiffSide
    source_schema: DatasetSchema
    target_schema: DatasetSchema
    pairing: ColumnPairing
    columns_plan: ComparisonPlan | None
    """All paired columns planned for comparison (keys may be empty)."""
    plan_error: PlanError | None
    engine: str
    registry: Registry
    privacy: Privacy
    sample_size: int
    normalization: NormalizationConfig
    dataset: DatasetPairResult
    run: RunInfo
    state: dict[str, Any] = field(default_factory=dict)
    """Scratch space shared between strategies (for example a finished diff)."""
    resources: EngineResources = field(default_factory=EngineResources)
    checkpoints: PartitionCheckpoints | None = None

    @property
    def plan(self) -> ComparisonPlan | None:
        """The row-level plan, or ``None`` when no keys are known."""
        if self.columns_plan is None or not self.columns_plan.keys:
            return None
        return self.columns_plan

    def recorder(self) -> QueryRecorder:
        return QueryRecorder()

    def run_both(
        self, recorder: QueryRecorder, source_sql: str, target_sql: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Run one query per side and return the rows as dictionaries."""
        source = recorder.run(self.source.connector, source_sql, Side.SOURCE).to_pylist()
        target = recorder.run(self.target.connector, target_sql, Side.TARGET).to_pylist()
        return source, target
