"""Helpers shared by the built-in reporters: ordering, top issues, scores, and value formatting.

Everything here is derived from the :class:`~dvf.models.results.RunResult` alone, so every format
shows the same numbers.
"""

from __future__ import annotations

import heapq
import json
import math
import re
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
    BaselineComparison,
    CheckResult,
    DatasetPairResult,
    RunResult,
    SuiteResult,
    TimingBreakdown,
    TrendPoint,
    WaiverStatus,
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


def top_classification(classifications: Mapping[str, int]) -> str | None:
    """The most frequent mismatch class, or None when there are none."""
    ranked = sorted_classifications(classifications)
    return ranked[0][0] if ranked else None


# ---------------------------------------------------------------------- run history and waivers

WAIVER_STATUS_ORDER: tuple[str, ...] = ("applied", "expired", "unused")
"""Display order of :attr:`WaiverStatus.status` values."""

INTERRUPTED_NOTICE = (
    "This run was interrupted before every table finished; resume it with 'dvf run --resume'."
)
RESUMED_NOTICE = "Some tables were loaded from this run's checkpoints instead of being re-run."


def run_notices(run: RunResult) -> list[str]:
    """Banner messages for runs that were interrupted or resumed from checkpoints."""
    notices = []
    if run.interrupted:
        notices.append(INTERRUPTED_NOTICE)
    if run.resumed_from_checkpoint:
        notices.append(RESUMED_NOTICE)
    return notices


def waiver_expired(check: CheckResult) -> bool:
    """Whether the check matched a waiver that has expired (so its failure stands)."""
    return check.waiver is not None and check.waiver.expired


def waiver_statuses(run: RunResult) -> list[WaiverStatus]:
    """How each waiver was used in the run.

    Uses :attr:`RunResult.waivers`. Results written before it existed (schema 1.0) are handled by
    deriving the applied and expired waivers from the checks' own waiver records.
    """
    if run.waivers:
        return list(run.waivers)
    derived: dict[tuple[str, str | None, bool], WaiverStatus] = {}
    for check in run.iter_checks():
        waiver = check.waiver
        if waiver is None:
            continue
        key = (waiver.selector, waiver.column, waiver.expired)
        status = derived.get(key)
        if status is None:
            status = WaiverStatus(
                selector=waiver.selector,
                column=waiver.column,
                reason=waiver.reason,
                ticket=waiver.ticket,
                approved_by=waiver.approved_by,
                expires=waiver.expires,
                status="expired" if waiver.expired else "applied",
            )
            derived[key] = status
        if check.check_id not in status.checks:
            status.checks.append(check.check_id)
    return list(derived.values())


def waiver_counts(waivers: Iterable[WaiverStatus]) -> dict[str, int]:
    """Number of waivers per status: applied, expired, unused, then any other status seen."""
    counter = Counter(waiver.status for waiver in waivers)
    counts = {status: counter.pop(status, 0) for status in WAIVER_STATUS_ORDER}
    counts.update(sorted(counter.items()))
    return counts


def baseline_summary(baseline: BaselineComparison) -> str:
    """For example ``3 new, 1 resolved, 5 persisting failures vs run 20261105T...``."""
    return (
        f"{len(baseline.new_failures)} new, {len(baseline.resolved)} resolved, "
        f"{len(baseline.persisting)} persisting failures vs run {baseline.baseline_run_id}"
    )


def baseline_marks(run: RunResult) -> dict[str, str]:
    """Check ID to ``new`` or ``persisting``, for checks failing relative to the baseline."""
    baseline = run.baseline
    if baseline is None:
        return {}
    marks = dict.fromkeys(baseline.persisting, "persisting")
    marks.update(dict.fromkeys(baseline.new_failures, "new"))
    return marks


def trend_series(run: RunResult) -> list[tuple[TrendPoint, bool]]:
    """Earlier runs (oldest first) followed by this run, each flagged ``True`` if it is this run.

    Empty when the result has no trend.
    """
    if not run.trend:
        return []
    counts = Counter(check.status for check in run.iter_checks())
    current = TrendPoint(
        run_id=run.run_id,
        started_at=run.started_at,
        status=run.status,
        quality_score=quality_score(run),
        checks=sum(counts.values()),
        failed=counts[Status.FAILED],
        errors=counts[Status.ERROR],
    )
    return [*((point, False) for point in run.trend), (current, True)]


# ---------------------------------------------------------------------- artifacts and partitions

ARTIFACT_LABELS = {
    "extract": "Mismatch extract",
    "repro_sql": "Repro SQL",
    "repro_config": "Repro config",
    "report": "Report",
}


def artifact_label(kind: str) -> str:
    """A readable label for an artifact kind (``repro_sql`` is shown as ``Repro SQL``)."""
    return ARTIFACT_LABELS.get(kind, kind)


PARTITION_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "Partition"),
    ("status", "Status"),
    ("source_rows", "Source rows"),
    ("target_rows", "Target rows"),
    ("mismatched", "Mismatched"),
    ("missing_in_target", "Missing in target"),
    ("extra_in_target", "Extra in target"),
)
"""Known keys of ``details["partitions"]`` entries, as (key, heading), in display order."""

_PASSING_PARTITION = frozenset({"passed", "skipped", "waived"})
_MAX_PARTITION_EXTRA_KEYS = 8


@dataclass(frozen=True, slots=True)
class PartitionTable:
    """Per-partition outcomes of a comparison, taken from ``details["partitions"]``."""

    columns: list[tuple[str, str]]
    """(key, heading) pairs: the known keys present in any row, then up to 8 other keys."""
    rows: list[Mapping[str, Any]]

    def failing(self) -> list[Mapping[str, Any]]:
        """Rows whose status is set and is not passed, skipped, or waived."""
        return [
            row
            for row in self.rows
            if row.get("status") is not None and str(row["status"]) not in _PASSING_PARTITION
        ]


def partition_table(check: CheckResult) -> PartitionTable | None:
    """The check's per-partition details, or None when ``details["partitions"]`` is absent or
    not a list of mappings."""
    raw = check.details.get("partitions")
    if not isinstance(raw, list):
        return None
    rows: list[Mapping[str, Any]] = [row for row in raw if isinstance(row, Mapping)]
    if not rows:
        return None
    present: dict[str, None] = {}
    for row in rows:
        present.update(dict.fromkeys(str(key) for key in row))
    known = [(key, label) for key, label in PARTITION_FIELDS if key in present]
    known_keys = {key for key, _ in PARTITION_FIELDS}
    extra = [(key, key) for key in present if key not in known_keys]
    return PartitionTable(known + extra[:_MAX_PARTITION_EXTRA_KEYS], rows)


def details_without_partitions(check: CheckResult) -> dict[str, Any]:
    """``check.details`` minus ``partitions`` when that entry is shown as its own table."""
    if partition_table(check) is None:
        return dict(check.details)
    return {key: value for key, value in check.details.items() if key != "partitions"}


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


_INVALID_XML_CHARS = re.compile(r"[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")
"""Characters XML 1.0 does not allow, including lone surrogates."""


def xml_text(value: object) -> str:
    """``value`` as text that is valid in XML 1.0 content and attributes."""
    text = value if isinstance(value, str) else str(value)
    return _INVALID_XML_CHARS.sub("", text)


# ---------------------------------------------------------------------- output


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with Unix newlines, creating the parent folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def report_artifact(path: Path, fmt: str) -> ArtifactRef:
    return ArtifactRef(kind="report", path=str(path), format=fmt)
