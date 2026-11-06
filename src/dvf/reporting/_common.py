"""Helpers shared by the built-in reporters: ordering, top issues, scores, and value formatting.

Everything here is derived from the :class:`~dvf.models.results.RunResult` alone, so every format
shows the same numbers.
"""

from __future__ import annotations

import heapq
import json
import math
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvf.core.enums import Dimension, Status
from dvf.models.results import (
    ArtifactRef,
    CheckResult,
    DatasetPairResult,
    RunResult,
    SuiteResult,
    TimingBreakdown,
)

if TYPE_CHECKING:
    from dvf.reporting.base import ReportOptions

DASH = "—"
"""Shown for values that are not set."""

ELLIPSIS = "…"

STATUS_ORDER: tuple[Status, ...] = (
    Status.PASSED,
    Status.WARNING,
    Status.FAILED,
    Status.ERROR,
    Status.SKIPPED,
    Status.WAIVED,
)
"""Display order of statuses in counts, charts, and filters."""

ISSUE_STATUSES = frozenset({Status.ERROR, Status.FAILED, Status.WARNING})
"""Statuses listed as issues in executive summaries."""

FAILURE_STATUSES = frozenset({Status.ERROR, Status.FAILED})

_EVALUATED = frozenset({Status.PASSED, Status.WARNING, Status.FAILED, Status.WAIVED})
_PASSING = frozenset({Status.PASSED, Status.WAIVED})

_ISSUE_TIER = {
    Status.ERROR: 0,
    Status.FAILED: 0,
    Status.WARNING: 1,
    Status.SKIPPED: 2,
    Status.WAIVED: 3,
    Status.PASSED: 4,
}

STATUS_RANK = {
    Status.ERROR: 0,
    Status.FAILED: 1,
    Status.WARNING: 2,
    Status.SKIPPED: 3,
    Status.WAIVED: 4,
    Status.PASSED: 5,
}
"""Worst-first rank of each status, used for sorting check lists."""


# ---------------------------------------------------------------------- traversal


@dataclass(frozen=True, slots=True)
class CheckEntry:
    """A check together with the suite and table it belongs to."""

    index: int
    """Position of the check in run order, starting at 1."""
    suite: SuiteResult
    dataset: DatasetPairResult
    check: CheckResult

    @property
    def location(self) -> str:
        """``suite.table``, used as the JUnit class name and in listings."""
        return f"{self.suite.name}.{self.dataset.name}"


def iter_entries(run: RunResult) -> Iterator[CheckEntry]:
    """Yield every check of the run in order, with its suite and table."""
    index = 0
    for suite in run.suites:
        for dataset in suite.datasets:
            for check in dataset.checks:
                index += 1
                yield CheckEntry(index, suite, dataset, check)


def issue_sort_key(entry: CheckEntry) -> tuple[int, int, int, str, int]:
    """Failed and errored checks first, then warnings; by severity, then unexpected count."""
    check = entry.check
    return (
        _ISSUE_TIER[check.status],
        -check.severity.rank,
        -(check.unexpected_count or 0),
        check.check_id,
        entry.index,
    )


def top_issues(
    entries: Iterable[CheckEntry],
    limit: int = 10,
    statuses: frozenset[Status] = ISSUE_STATUSES,
) -> list[CheckEntry]:
    """The ``limit`` most important checks whose status is in ``statuses``."""
    return heapq.nsmallest(
        limit, (e for e in entries if e.check.status in statuses), key=issue_sort_key
    )


def status_counts(checks: Iterable[CheckResult]) -> dict[Status, int]:
    """Number of checks per status, for every status in display order."""
    counter = Counter(check.status for check in checks)
    return {status: counter.get(status, 0) for status in STATUS_ORDER}


def quality_score(run: RunResult) -> float | None:
    """The run's quality score: the stored one when set, otherwise computed from the checks."""
    if run.summary.quality_score is not None:
        return run.summary.quality_score
    return run.summarize().quality_score


def dataset_count(run: RunResult) -> int:
    return sum(len(suite.datasets) for suite in run.suites)


def rows_per_second(run: RunResult) -> float | None:
    """Rows compared per second of run time, when both are known."""
    rows = run.summary.rows_compared
    if not rows or not run.duration_ms:
        return None
    return rows / (run.duration_ms / 1000.0)


def report_title(run: RunResult, options: ReportOptions) -> str:
    return options.title or run.project


def headline(check: CheckResult) -> str:
    """A one-line description of the outcome: the message, else the error, else the name."""
    if check.message:
        return check.message
    if check.error is not None:
        return f"{check.error.type}: {check.error.message}"
    return check.name


@dataclass(frozen=True, slots=True)
class DimensionScore:
    """Share of evaluated checks of one data quality dimension that passed."""

    dimension: Dimension
    passed: int
    evaluated: int
    total: int

    @property
    def score(self) -> float | None:
        if self.evaluated == 0:
            return None
        return round(100.0 * self.passed / self.evaluated, 2)


def dimension_scores(checks: Iterable[CheckResult]) -> list[DimensionScore]:
    """Scores for every dimension that has at least one check, in :class:`Dimension` order.

    As in :meth:`RunResult.summarize`, passed, warning, failed, and waived checks are evaluated,
    and passed and waived checks count as passing.
    """
    totals: Counter[Dimension] = Counter()
    evaluated: Counter[Dimension] = Counter()
    passed: Counter[Dimension] = Counter()
    for check in checks:
        totals[check.dimension] += 1
        if check.status in _EVALUATED:
            evaluated[check.dimension] += 1
        if check.status in _PASSING:
            passed[check.dimension] += 1
    return [
        DimensionScore(dim, passed[dim], evaluated[dim], totals[dim])
        for dim in Dimension
        if totals[dim]
    ]


def sum_timings(checks: Iterable[CheckResult]) -> TimingBreakdown | None:
    """Add up the timing breakdowns of the checks that have one."""
    fields = ("source_query_ms", "target_query_ms", "transfer_ms", "compare_ms", "render_ms")
    sums = dict.fromkeys((*fields, "total_ms"), 0.0)
    found = False
    for check in checks:
        timing = check.timing
        if timing is None:
            continue
        found = True
        for name in sums:
            sums[name] += getattr(timing, name)
    return TimingBreakdown(**sums) if found else None


def sorted_classifications(classifications: Mapping[str, int]) -> list[tuple[str, int]]:
    """Mismatch classes, most frequent first, ties by name."""
    return sorted(classifications.items(), key=lambda item: (-item[1], item[0]))


# ---------------------------------------------------------------------- formatting


def truncate(text: str, limit: int | None) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking the cut with an ellipsis."""
    if limit is None or len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + ELLIPSIS


def to_json(value: Any, *, indent: int | None = None) -> str:
    """JSON text for display. Values JSON cannot represent are shown with ``str()``."""
    separators = None if indent is not None else (", ", ": ")
    return json.dumps(value, indent=indent, ensure_ascii=False, default=str, separators=separators)


def format_value(value: Any, *, limit: int | None = None, none: str = DASH) -> str:
    """A compact, human-readable rendering of any result value."""
    if value is None:
        return none
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, Enum):
        text = str(value.value)
    elif isinstance(value, str):
        text = value
    elif isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        text = str(int(value))
    elif isinstance(value, int | float):
        text = repr(value)
    elif isinstance(value, datetime | date | time):
        text = value.isoformat()
    elif isinstance(value, Mapping | list | tuple):
        text = to_json(value)
    else:
        text = str(value)
    return truncate(text, limit)


def format_count(value: int | float | None) -> str:
    """A count with thousands separators."""
    if value is None:
        return DASH
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def format_signed(value: int | float | None) -> str:
    """A difference with an explicit sign."""
    if value is None:
        return DASH
    if value > 0:
        return "+" + format_count(value)
    return format_count(value)


def format_pct(value: float | None) -> str:
    """A percentage given on a 0 to 100 scale."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return DASH
    if 0 < value < 0.01:
        return "<0.01%"
    if 99.99 < value < 100:
        return ">99.99%"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def format_duration(ms: float | None) -> str:
    """A duration in milliseconds, in the most readable unit."""
    if ms is None:
        return DASH
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {secs} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return DASH
    return value.isoformat(sep=" ", timespec="seconds")


def format_group(group: Mapping[str, Any] | None) -> str:
    """``key=value`` pairs of a grouped metric."""
    if not group:
        return DASH
    return ", ".join(f"{key}={format_value(val, none='NULL')}" for key, val in group.items())


# ---------------------------------------------------------------------- output


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with Unix newlines, creating the parent folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def report_artifact(path: Path, fmt: str) -> ArtifactRef:
    return ArtifactRef(kind="report", path=str(path), format=fmt)
