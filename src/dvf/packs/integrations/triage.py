"""The AI-assisted triage plugin interface (BUILD_PROMPT.md section 9, item 9): off by default.

dvf ships the interface only: no implementation, and nothing that calls an external service.
When a configuration opts in with ``triage.enabled: true``, plugins implementing the
``triage_failures`` hook receive a :class:`TriageSummary` after the run, before reports render,
and may return suggestions that dvf adds to the reports as a "Triage suggestions" section.

A summary is masked and aggregated by construction: check IDs, types, dimensions, severities,
statuses, row counts, column names, and per-column counts of mismatch classes (such as
``whitespace_only``). It never holds values, samples, queries, messages, hints, or error texts
(only the error type), so nothing it contains can reveal a data value.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from dvf.core.enums import Severity, Status
from dvf.reporting.base import ReportSection

if TYPE_CHECKING:
    from dvf.config.models import DvfConfig, TriageConfig
    from dvf.models.results import CheckResult, RunResult
    from dvf.plugins.registry import Registry

log = logging.getLogger(__name__)

MAX_SUGGESTIONS = 50
_MAX_SUGGESTION_TEXT = 1000
_STATUS_RANK = {Status.ERROR: 0, Status.FAILED: 1}
_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFO: 3}


@dataclass(frozen=True)
class ColumnMismatchSummary:
    """How many rows of one column differ, by mismatch class. No values."""

    column: str
    mismatch_count: int
    mismatch_pct: float
    classifications: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckSummary:
    """One failed or errored check, without values or free text."""

    check_id: str
    type: str
    dimension: str
    severity: str
    status: str
    columns: tuple[str, ...] = ()
    unexpected_count: int | None = None
    total_count: int | None = None
    unexpected_pct: float | None = None
    engine: str | None = None
    error_type: str | None = None
    """The exception class of an errored check (its message is never included)."""
    column_mismatches: tuple[ColumnMismatchSummary, ...] = ()
    reconciliation: dict[str, int] | None = None
    """Key-level totals of the check's table (matched, mismatched, missing, extra, ...)."""


@dataclass(frozen=True)
class TriageSummary:
    """What a triage plugin receives: masked, aggregated summaries of a run's failures."""

    run_id: str
    project: str
    status: str
    quality_score: float | None
    checks_by_status: dict[str, int]
    checks: tuple[CheckSummary, ...]
    omitted_checks: int = 0
    """Failed or errored checks left out by ``triage.max_checks``."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TriageSuggestion:
    """A suggestion returned by a triage plugin."""

    summary: str
    check_id: str | None = None
    source: str | None = None


def _sort_key(check: CheckResult) -> tuple[int, int, str]:
    return (
        _STATUS_RANK.get(check.status, 9),
        _SEVERITY_RANK.get(check.severity, 9),
        check.check_id,
    )


def build_summary(result: RunResult, settings: TriageConfig) -> TriageSummary:
    """The masked, aggregated summary of ``result``'s failed and errored checks."""
    reconciliations: dict[str, dict[str, int]] = {}
    failing: list[CheckResult] = []
    for suite in result.suites:
        for dataset in suite.datasets:
            for check in dataset.checks:
                if check.status in (Status.FAILED, Status.ERROR):
                    failing.append(check)
                    if dataset.reconciliation is not None:
                        reconciliations[check.check_id] = dataset.reconciliation.model_dump()
    failing.sort(key=_sort_key)
    listed = failing[: settings.max_checks]
    checks = tuple(
        CheckSummary(
            check_id=c.check_id,
            type=c.type,
            dimension=c.dimension.value,
            severity=c.severity.value,
            status=c.status.value,
            columns=tuple(c.columns),
            unexpected_count=c.unexpected_count,
            total_count=c.total_count,
            unexpected_pct=c.unexpected_pct,
            engine=c.engine,
            error_type=c.error.type if c.error is not None else None,
            column_mismatches=tuple(
                ColumnMismatchSummary(
                    column=m.column,
                    mismatch_count=m.mismatch_count,
                    mismatch_pct=m.mismatch_pct,
                    classifications=dict(m.classifications),
                )
                for m in c.column_mismatches[: settings.max_columns_per_check]
            ),
            reconciliation=reconciliations.get(c.check_id),
        )
        for c in listed
    )
    return TriageSummary(
        run_id=result.run_id,
        project=result.project,
        status=result.status.value,
        quality_score=result.summary.quality_score,
        checks_by_status={s.value: n for s, n in result.summary.checks_by_status.items()},
        checks=checks,
        omitted_checks=len(failing) - len(listed),
    )


def _suggestion(item: Any) -> TriageSuggestion | None:
    if isinstance(item, TriageSuggestion):
        return item
    if isinstance(item, str):
        return TriageSuggestion(summary=item[:_MAX_SUGGESTION_TEXT])
    if isinstance(item, Mapping) and isinstance(item.get("summary"), str):
        check_id = item.get("check_id")
        source = item.get("source")
        return TriageSuggestion(
            summary=item["summary"][:_MAX_SUGGESTION_TEXT],
            check_id=str(check_id) if check_id is not None else None,
            source=str(source) if source is not None else None,
        )
    return None


def run_triage(result: RunResult, config: DvfConfig, registry: Registry) -> list[TriageSuggestion]:
    """Call ``triage_failures`` hooks when ``triage.enabled`` and the run has failures.

    A failing plugin is logged and ignored; triage never fails a run.
    """
    if not config.triage.enabled:
        return []
    summary = build_summary(result, config.triage)
    if not summary.checks:
        return []
    try:
        returned = registry.hooks.hook.triage_failures(summary=summary)
    except Exception as exc:
        log.warning("a triage plugin failed: %s: %s", type(exc).__name__, exc)
        return []
    suggestions: list[TriageSuggestion] = []
    for batch in returned:
        items = batch if isinstance(batch, list | tuple) else [batch]
        for item in items:
            suggestion = _suggestion(item)
            if suggestion is not None and len(suggestions) < MAX_SUGGESTIONS:
                suggestions.append(suggestion)
    return suggestions


def report_section(suggestions: list[TriageSuggestion]) -> ReportSection | None:
    """The "Triage suggestions" report section, or ``None`` without suggestions."""
    if not suggestions:
        return None
    lines = [
        "Suggestions from triage plugins, based on masked, aggregated summaries of the failures. "
        "Review them before acting."
    ]
    for s in suggestions:
        where = f"{s.check_id}: " if s.check_id else ""
        source = f" ({s.source})" if s.source else ""
        lines.append(f"- {where}{s.summary}{source}")
    return ReportSection("Triage suggestions", "\n".join(lines))


__all__ = [
    "CheckSummary",
    "ColumnMismatchSummary",
    "TriageSuggestion",
    "TriageSummary",
    "build_summary",
    "report_section",
    "run_triage",
]
