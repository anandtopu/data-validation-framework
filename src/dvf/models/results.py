"""The canonical, versioned result model (ADR 0004).

``RunResult`` -> ``SuiteResult`` -> ``DatasetPairResult`` -> ``CheckResult``. Every report format
is rendered from this model, and nothing else.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dvf.core.datasets import DatasetRef
from dvf.core.enums import Dimension, Severity, Side, Status
from dvf.core.types import CanonicalType, Compatibility

RESULTS_SCHEMA_VERSION = "1.0.0"
"""SemVer of the results document. Independent of the package version."""

_ID_SEGMENT = re.compile(r"[^A-Za-z0-9_+\-]+")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryRecord(_Model):
    """A query executed for a check, so failures can be reproduced exactly."""

    side: Side
    dialect: str | None = None
    sql: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    engine: str | None = None
    duration_ms: float | None = None
    rows_returned: int | None = None


class TimingBreakdown(_Model):
    """Where time went, in milliseconds."""

    source_query_ms: float = 0.0
    target_query_ms: float = 0.0
    transfer_ms: float = 0.0
    compare_ms: float = 0.0
    render_ms: float = 0.0
    total_ms: float = 0.0


class ErrorInfo(_Model):
    """An execution error, with a remediation hint when one is known."""

    type: str
    message: str
    traceback: str | None = None
    hint: str | None = None


class WaiverApplied(_Model):
    """A waiver that turned a failure into an accepted difference."""

    selector: str
    reason: str
    ticket: str | None = None
    expires: str | None = None
    approved_by: str | None = None


class SampleSet(_Model):
    """Sample rows captured as evidence. Values are masked according to privacy settings."""

    columns: list[str]
    rows: list[list[Any]]
    masked: bool = False
    truncated: bool = False


class ArtifactRef(_Model):
    """A file produced alongside the report, such as a mismatch extract or repro SQL."""

    kind: str
    path: str
    format: str | None = None
    rows: int | None = None


class ColumnMismatch(_Model):
    """Per-column mismatch summary for a source-vs-target comparison."""

    column: str
    source_column: str | None = None
    mismatch_count: int
    mismatch_pct: float
    classifications: dict[str, int] = Field(
        default_factory=dict,
        description="Mismatch class (for example 'whitespace_only') to number of rows.",
    )
    hints: list[str] = Field(default_factory=list)
    samples: SampleSet | None = None


class ColumnSchemaDiff(_Model):
    """One row of a schema comparison."""

    source_column: str | None
    target_column: str | None
    status: str = Field(description="'matched', 'missing_in_target', or 'extra_in_target'.")
    source_type: CanonicalType | None = None
    target_type: CanonicalType | None = None
    source_native_type: str | None = None
    target_native_type: str | None = None
    compatibility: Compatibility | None = None
    reasons: list[str] = Field(default_factory=list)
    nullable_change: str | None = None


class CheckResult(_Model):
    """The outcome of one check, comparison, or schema validation."""

    check_id: str
    name: str
    type: str
    dimension: Dimension
    severity: Severity
    status: Status
    side: Side | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    columns: list[str] = Field(default_factory=list)
    message: str | None = None
    observed: Any = None
    expected: Any = None
    unexpected_count: int | None = None
    total_count: int | None = None
    unexpected_pct: float | None = None
    mostly: float | None = None
    samples: SampleSet | None = None
    column_mismatches: list[ColumnMismatch] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    queries: list[QueryRecord] = Field(default_factory=list)
    normalization: list[str] = Field(default_factory=list)
    engine: str | None = None
    partition: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    timing: TimingBreakdown | None = None
    error: ErrorInfo | None = None
    waiver: WaiverApplied | None = None
    details: dict[str, Any] = Field(
        default_factory=dict, description="Custom details from configuration or plugins."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class RowCounts(_Model):
    source: int | None = None
    target: int | None = None

    @property
    def difference(self) -> int | None:
        if self.source is None or self.target is None:
            return None
        return self.target - self.source


class ReconciliationSummary(_Model):
    """Key-level reconciliation totals for a dataset pair."""

    matched: int = 0
    mismatched: int = 0
    missing_in_target: int = 0
    extra_in_target: int = 0
    duplicate_keys_source: int = 0
    duplicate_keys_target: int = 0
    null_keys_source: int = 0
    null_keys_target: int = 0

    @property
    def match_pct(self) -> float | None:
        total = self.matched + self.mismatched + self.missing_in_target + self.extra_in_target
        return None if total == 0 else round(100.0 * self.matched / total, 6)


class DatasetPairResult(_Model):
    """Results for one table (or dataset pair) in a suite."""

    name: str
    source: DatasetRef | None = None
    target: DatasetRef | None = None
    status: Status
    row_counts: RowCounts = Field(default_factory=RowCounts)
    reconciliation: ReconciliationSummary | None = None
    schema_diff: list[ColumnSchemaDiff] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    duration_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SuiteResult(_Model):
    name: str
    status: Status
    tags: list[str] = Field(default_factory=list)
    datasets: list[DatasetPairResult] = Field(default_factory=list)
    duration_ms: float | None = None


class RunEnvironment(_Model):
    """Everything needed to reproduce a run."""

    dvf_version: str
    python_version: str
    platform: str
    hostname: str | None = None
    profile: str | None = None
    git_sha: str | None = None
    config_files: list[str] = Field(default_factory=list)
    config_hash: str | None = None
    cli_args: list[str] = Field(default_factory=list)
    plugins: dict[str, str] = Field(
        default_factory=dict, description="Plugin distribution name to version."
    )
    engines: dict[str, str] = Field(
        default_factory=dict, description="Engine or driver name to version."
    )


class RunSummary(_Model):
    checks_by_status: dict[Status, int] = Field(default_factory=dict)
    suites: int = 0
    datasets: int = 0
    rows_compared: int = 0
    quality_score: float | None = Field(
        default=None, description="Share of evaluated checks that passed, from 0 to 100."
    )


class RunResult(_Model):
    """The complete, canonical output of a run."""

    schema_version: str = RESULTS_SCHEMA_VERSION
    run_id: str
    project: str
    status: Status
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    environment: RunEnvironment
    summary: RunSummary = Field(default_factory=RunSummary)
    suites: list[SuiteResult] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(
        default_factory=dict, description="Resolved configuration with secrets redacted."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    def iter_checks(self) -> Iterable[CheckResult]:
        for suite in self.suites:
            for dataset in suite.datasets:
                yield from dataset.checks

    def summarize(self) -> RunSummary:
        """Compute summary counts and quality score from the check results."""
        counts = Counter(check.status for check in self.iter_checks())
        evaluated = sum(counts[s] for s in (Status.PASSED, Status.WARNING, Status.FAILED))
        evaluated += counts[Status.WAIVED]
        passed = counts[Status.PASSED] + counts[Status.WAIVED]
        return RunSummary(
            checks_by_status=dict(counts),
            suites=len(self.suites),
            datasets=sum(len(s.datasets) for s in self.suites),
            rows_compared=self.summary.rows_compared,
            quality_score=None if evaluated == 0 else round(100.0 * passed / evaluated, 2),
        )


# ---------------------------------------------------------------------- helpers

_STATUS_PRECEDENCE = (Status.ERROR, Status.FAILED, Status.WARNING)


def aggregate_status(statuses: Iterable[Status]) -> Status:
    """Combine child statuses into a parent status.

    ``error`` beats ``failed`` beats ``warning``. Waived checks count as passed. A parent whose
    children were all skipped (or that has no children) is ``skipped``.
    """
    seen = set(statuses)
    for status in _STATUS_PRECEDENCE:
        if status in seen:
            return status
    if seen & {Status.PASSED, Status.WAIVED}:
        return Status.PASSED
    return Status.SKIPPED


def make_check_id(
    suite: str,
    table: str,
    check_type: str,
    *,
    columns: Iterable[str] = (),
    name: str | None = None,
) -> str:
    """Build the stable identifier ``suite.table.type[.columns][.name]`` used in results and waivers.

    Dotted plugin types (``acme.vat_valid``) are kept as is. Column names are joined with ``+``.
    """
    parts = [suite, table, check_type]
    cols = [_ID_SEGMENT.sub("_", c) for c in columns]
    if cols:
        parts.append("+".join(cols))
    if name:
        parts.append(_ID_SEGMENT.sub("_", name))
    return ".".join(parts)
