"""The notification message: a privacy-filtered summary of a run that every channel renders.

Notifiers never see a :class:`~dvf.models.results.RunResult`; they get a
:class:`NotificationMessage` built by :func:`build_message`. It holds statuses, counts, the worst
failures, and a report link. Messages and hints are dropped in strict privacy mode and with
``counts_only``; masked sample rows are included only with ``include_samples``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvf import __version__
from dvf.core.enums import Severity, Status
from dvf.models.results import CheckResult, RunResult

if TYPE_CHECKING:
    from dvf.config.models import DvfConfig
    from dvf.config.notifications import NotificationConfig
    from dvf.execution.context import RunInfo
    from dvf.models.results import ArtifactRef

MESSAGE_SCHEMA = "dvf.notification/v1"
"""Identifies the generic webhook payload format (:meth:`NotificationMessage.to_dict`)."""

_MAX_TEXT = 300
_STATUS_RANK = {Status.ERROR: 0, Status.FAILED: 1, Status.WARNING: 2}
_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFO: 3}


class NotificationError(Exception):
    """A delivery failed. ``transient=True`` asks the dispatcher to retry with backoff.

    Notifier plugins may raise it (or any exception with a boolean ``transient`` attribute).
    Messages must not contain webhook URLs, passwords, or tokens; dvf redacts known secrets, but
    the safest message is one that never had them.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        retry_after: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after
        self.status = status


@dataclass(frozen=True)
class ColumnSummary:
    """Mismatch counts of one column, with the classifier's labels (no values)."""

    column: str
    mismatch_count: int
    mismatch_pct: float
    classifications: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FailureSummary:
    """One failed, errored, or warning check in a message."""

    check_id: str
    status: Status
    severity: Severity
    type: str
    suite: str | None = None
    table: str | None = None
    columns: tuple[str, ...] = ()
    unexpected_count: int | None = None
    total_count: int | None = None
    unexpected_pct: float | None = None
    message: str | None = None
    """dvf's message, or ``None`` with ``counts_only`` and in strict privacy mode."""
    hints: tuple[str, ...] = ()
    column_mismatches: tuple[ColumnSummary, ...] = ()
    sample_columns: tuple[str, ...] = ()
    sample_rows: tuple[tuple[Any, ...], ...] = ()
    """Masked sample rows (only with ``include_samples``)."""

    @property
    def counts_text(self) -> str:
        """``"38 of 9,988 rows (0.38%)"``, or an empty string when the check has no counts."""
        if self.unexpected_count is None:
            return ""
        text = f"{self.unexpected_count:,}"
        if self.total_count:
            text += f" of {self.total_count:,} rows"
            if self.unexpected_pct is not None:
                text += f" ({self.unexpected_pct:.2f}%)"
        else:
            text += " rows"
        return text


@dataclass(frozen=True)
class NotificationMessage:
    """Everything a channel may show about a run.

    Notifier plugins read these fields, or :meth:`to_dict` for a JSON payload.
    """

    run_id: str
    project: str
    status: Status
    title: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    checks_by_status: dict[str, int] = field(default_factory=dict)
    quality_score: float | None = None
    rows_compared: int = 0
    suites: int = 0
    datasets: int = 0
    failures: tuple[FailureSummary, ...] = ()
    """The worst failures, at most ``max_failures``."""
    more_failures: int = 0
    """Failures not listed."""
    report_url: str | None = None
    report_path: str | None = None
    """Local path of the HTML report (or the results JSON), when reports were written."""
    summary_path: str | None = None
    """Local path of ``summary.md``, when the Markdown report was written."""
    baseline: dict[str, Any] | None = None
    """``baseline_run_id``, ``new_failures``, ``resolved``, ``persisting`` (counts)."""
    profile: str | None = None
    interrupted: bool = False
    error: str | None = None
    """For a run that stopped with an error before producing results: the (redacted) error."""
    counts_only: bool = False
    test: bool = False
    """A ``dvf notify test`` message, not a real run."""
    summary_markdown: str | None = None
    """The Markdown summary, for channels that attach it (email with ``attach_summary``)."""
    dvf_version: str = __version__

    @property
    def status_label(self) -> str:
        if self.test:
            return "TEST"
        return "INTERRUPTED" if self.interrupted else self.status.value.upper()

    @property
    def counts_text(self) -> str:
        """``"12 passed, 2 failed"``, in status order."""
        parts = [
            f"{self.checks_by_status[s.value]} {s.value}"
            for s in Status
            if self.checks_by_status.get(s.value)
        ]
        return ", ".join(parts) or "no checks"

    @property
    def link(self) -> str | None:
        """The report URL if configured, else the local report path."""
        return self.report_url or self.report_path

    def to_dict(self) -> dict[str, Any]:
        """The generic JSON payload (``schema: dvf.notification/v1``)."""
        return {
            "schema": MESSAGE_SCHEMA,
            "event": "test" if self.test else "run_finished",
            "title": self.title,
            "run_id": self.run_id,
            "project": self.project,
            "status": self.status.value,
            "interrupted": self.interrupted,
            "profile": self.profile,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration_ms": self.duration_ms,
            "summary": {
                "checks_by_status": dict(self.checks_by_status),
                "quality_score": self.quality_score,
                "rows_compared": self.rows_compared,
                "suites": self.suites,
                "datasets": self.datasets,
            },
            "top_failures": [_failure_dict(f) for f in self.failures],
            "more_failures": self.more_failures,
            "baseline": self.baseline,
            "report": {"url": self.report_url, "path": self.report_path},
            "error": self.error,
            "counts_only": self.counts_only,
            "dvf_version": self.dvf_version,
        }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _failure_dict(failure: FailureSummary) -> dict[str, Any]:
    data = asdict(failure)
    data["status"] = failure.status.value
    data["severity"] = failure.severity.value
    data["columns"] = list(failure.columns)
    data["hints"] = list(failure.hints)
    data["column_mismatches"] = [asdict(c) for c in failure.column_mismatches]
    if failure.sample_rows:
        data["samples"] = {
            "columns": list(failure.sample_columns),
            "rows": [list(r) for r in failure.sample_rows],
        }
    del data["sample_columns"], data["sample_rows"]
    return data


def shorten(text: str | None, limit: int = _MAX_TEXT) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------- building


@dataclass(frozen=True)
class MessageContext:
    """What a message is built from besides the result: the run's files and privacy settings."""

    output_dir: Path | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    strict_privacy: bool = False
    strict_suites: frozenset[str] = frozenset()
    """Suites whose effective privacy mode is strict."""
    info: RunInfo | None = None

    @classmethod
    def for_run(
        cls,
        config: DvfConfig,
        info: RunInfo | None,
        output_dir: Path | None = None,
        artifacts: Iterable[ArtifactRef] = (),
    ) -> MessageContext:
        return cls(
            output_dir=output_dir,
            artifacts=tuple(artifacts),
            strict_privacy=config.defaults.privacy.mode == "strict",
            strict_suites=frozenset(
                s.name for s in config.suites if s.defaults.privacy.mode == "strict"
            ),
            info=info,
        )

    def artifact_path(self, *formats: str) -> Path | None:
        for fmt in formats:
            for artifact in self.artifacts:
                if artifact.format == fmt:
                    path = Path(artifact.path)
                    if not path.is_absolute() and self.output_dir is not None:
                        path = self.output_dir / path
                    return path
        return None


def trigger_status(result: RunResult | None) -> Status:
    """The status notification triggers compare with ``on``: an interrupted run is ``error``."""
    if result is None or result.interrupted:
        return Status.ERROR
    return result.status


def _sort_key(check: CheckResult) -> tuple[int, int, str]:
    return (
        _STATUS_RANK.get(check.status, 9),
        _SEVERITY_RANK.get(check.severity, 9),
        check.check_id,
    )


def _split_id(check_id: str) -> tuple[str | None, str | None]:
    parts = check_id.split(".")
    return (parts[0], parts[1]) if len(parts) >= 3 else (None, None)


def build_message(
    result: RunResult,
    config: NotificationConfig,
    context: MessageContext,
    *,
    test: bool = False,
) -> NotificationMessage:
    """The message for ``result`` as one channel (``config``) may show it."""
    counts_only = config.counts_only or context.strict_privacy
    problems = sorted(
        (
            c
            for c in result.iter_checks()
            if c.status in (Status.FAILED, Status.ERROR, Status.WARNING)
        ),
        key=_sort_key,
    )
    listed = problems[: config.max_failures]
    failures = tuple(
        _failure(check, config, counts_only or _suite_strict(check, context)) for check in listed
    )
    baseline = None
    if result.baseline is not None:
        b = result.baseline
        baseline = {
            "baseline_run_id": b.baseline_run_id,
            "new_failures": len(b.new_failures),
            "resolved": len(b.resolved),
            "persisting": len(b.persisting),
        }
    report = context.artifact_path("html", "json")
    summary = context.artifact_path("markdown")
    status = trigger_status(result) if result.interrupted else result.status
    title = f"{result.project}: {'INTERRUPTED' if result.interrupted else status.value.upper()}"
    if test:
        title = f"dvf test notification for {result.project}"
    return NotificationMessage(
        run_id=result.run_id,
        project=result.project,
        status=status,
        title=title,
        started_at=result.started_at,
        ended_at=result.ended_at,
        duration_ms=result.duration_ms,
        checks_by_status={s.value: n for s, n in result.summary.checks_by_status.items()},
        quality_score=result.summary.quality_score,
        rows_compared=result.summary.rows_compared,
        suites=result.summary.suites,
        datasets=result.summary.datasets,
        failures=failures,
        more_failures=max(0, len(problems) - len(listed)),
        report_url=_render_url(config.report_url, context),
        report_path=str(report) if report is not None else None,
        summary_path=str(summary) if summary is not None else None,
        baseline=baseline,
        profile=result.environment.profile,
        interrupted=result.interrupted,
        counts_only=counts_only,
        test=test,
    )


def crash_message(
    run_id: str,
    project: str,
    error: str,
    config: NotificationConfig,
    context: MessageContext,
    *,
    started_at: datetime | None = None,
) -> NotificationMessage:
    """The message for a run that stopped with an error before it produced results."""
    counts_only = config.counts_only or context.strict_privacy
    return NotificationMessage(
        run_id=run_id,
        project=project,
        status=Status.ERROR,
        title=f"{project}: ERROR",
        started_at=started_at,
        ended_at=datetime.now(UTC),
        report_url=_render_url(config.report_url, context),
        error="The run stopped with an error." if counts_only else shorten(error, 1000),
        counts_only=counts_only,
    )


def _suite_strict(check: CheckResult, context: MessageContext) -> bool:
    suite, _ = _split_id(check.check_id)
    return suite is not None and suite in context.strict_suites


def _render_url(template: str | None, context: MessageContext) -> str | None:
    if not template:
        return None
    return context.info.render(template) if context.info is not None else template


def _failure(check: CheckResult, config: NotificationConfig, counts_only: bool) -> FailureSummary:
    suite, table = _split_id(check.check_id)
    columns = tuple(
        ColumnSummary(
            column=m.column,
            mismatch_count=m.mismatch_count,
            mismatch_pct=m.mismatch_pct,
            classifications={} if counts_only else dict(m.classifications),
        )
        for m in check.column_mismatches[:10]
    )
    sample_columns: tuple[str, ...] = ()
    sample_rows: tuple[tuple[Any, ...], ...] = ()
    if config.include_samples and not counts_only and check.samples is not None:
        sample_columns = tuple(check.samples.columns)
        sample_rows = tuple(
            tuple(_cell(v) for v in row) for row in check.samples.rows[: config.max_sample_rows]
        )
    message = check.message
    if check.error is not None and not message:
        message = check.error.message
    return FailureSummary(
        check_id=check.check_id,
        status=check.status,
        severity=check.severity,
        type=check.type,
        suite=suite,
        table=table,
        columns=tuple(check.columns),
        unexpected_count=check.unexpected_count,
        total_count=check.total_count,
        unexpected_pct=check.unexpected_pct,
        message=None if counts_only else shorten(message),
        hints=() if counts_only else tuple(h for h in (shorten(h) for h in check.hints[:3]) if h),
        column_mismatches=columns,
        sample_columns=sample_columns,
        sample_rows=sample_rows,
    )


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return shorten(value, 80)
    if isinstance(value, Mapping | list | tuple):
        return shorten(str(value), 80)
    return value


__all__ = [
    "MESSAGE_SCHEMA",
    "ColumnSummary",
    "FailureSummary",
    "MessageContext",
    "NotificationError",
    "NotificationMessage",
    "build_message",
    "crash_message",
    "shorten",
    "trigger_status",
]
