"""Shared helpers for the orchestrator integrations.

:func:`run_validation` runs a configuration through :func:`dvf.api.run` and returns a
:class:`DvfRunSummary`: a small, JSON-safe digest (status, quality score, exit code, failed check
IDs, report paths) that fits in an Airflow XCom, a Prefect result, a Dagster metadata entry, or a
Databricks task value. The exit code and the quality gates are computed exactly as ``dvf run``
computes them (``--fail-on``, ``--min-score``, ``--strict``), with :func:`dvf.api.exit_code`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dvf import api
from dvf.core.enums import Severity, Status
from dvf.core.errors import ConfigError, DvfError
from dvf.models.results import CheckResult, RunResult

EXIT_OUTCOMES = {0: "passed", 1: "validation_failed", 2: "execution_error", 3: "config_error"}
"""What each ``dvf run`` exit code means."""

MARKDOWN_FILE = "summary.md"
RESULTS_FILE = "results.json"

_SPLIT = re.compile(r"[\s,]+")
_PROBLEMS = (Status.ERROR, Status.FAILED, Status.WARNING)


@dataclass(frozen=True)
class DvfRunSummary:
    """The outcome of one run, reduced to what an orchestrator needs."""

    run_id: str
    project: str
    status: str
    exit_code: int
    """0 passed, 1 validation failures or a score below the minimum, 2 execution errors."""
    quality_score: float | None
    checks_by_status: dict[str, int]
    failed_checks: tuple[str, ...] = ()
    error_checks: tuple[str, ...] = ()
    warning_checks: tuple[str, ...] = ()
    results_path: str | None = None
    report_dir: str | None = None
    reports: tuple[str, ...] = ()
    fail_on: str = Severity.ERROR.value
    min_score: float | None = None
    strict: bool = False
    duration_s: float | None = None

    @property
    def passed(self) -> bool:
        """True when the run passes its quality gates (exit code 0)."""
        return self.exit_code == 0

    @property
    def outcome(self) -> str:
        """``passed``, ``validation_failed``, or ``execution_error``."""
        return EXIT_OUTCOMES.get(self.exit_code, "execution_error")

    def message(self) -> str:
        """One line for logs and exception messages."""
        counts = ", ".join(
            f"{self.checks_by_status[s.value]} {s.value}"
            for s in Status
            if self.checks_by_status.get(s.value)
        )
        text = f"dvf run {self.run_id} ({self.project}): {self.status.upper()}"
        text += f", {counts}" if counts else ", no checks"
        if self.quality_score is not None:
            text += f"; quality score {self.quality_score:.1f}"
        if self.passed:
            return text + "; quality gates passed"
        gate = f"--fail-on {self.fail_on}"
        if self.min_score is not None:
            gate += f" --min-score {self.min_score:g}"
        if self.strict:
            gate += " --strict"
        reason = "execution errors" if self.exit_code == 2 else f"quality gates failed ({gate})"
        text += f"; {reason}, exit code {self.exit_code}"
        shown = (*self.error_checks, *self.failed_checks)[:5]
        if shown:
            more = len(self.error_checks) + len(self.failed_checks) - len(shown)
            text += ": " + ", ".join(shown) + (f" and {more} more" if more > 0 else "")
        return text

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dictionary (for XComs, task values, and flow results)."""
        return {
            "run_id": self.run_id,
            "project": self.project,
            "status": self.status,
            "outcome": self.outcome,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "quality_score": self.quality_score,
            "checks_by_status": dict(self.checks_by_status),
            "failed_checks": list(self.failed_checks),
            "error_checks": list(self.error_checks),
            "warning_checks": list(self.warning_checks),
            "results_path": self.results_path,
            "report_dir": self.report_dir,
            "reports": list(self.reports),
            "fail_on": self.fail_on,
            "min_score": self.min_score,
            "strict": self.strict,
            "duration_s": self.duration_s,
        }

    def markdown(self) -> str:
        """The run's ``summary.md`` when it was written, else a short Markdown digest."""
        if self.report_dir is not None:
            path = Path(self.report_dir) / MARKDOWN_FILE
            if path.is_file():
                return path.read_text(encoding="utf-8")
        lines = [
            f"## dvf run `{self.run_id}`",
            "",
            f"- Project: {self.project}",
            f"- Status: **{self.status.upper()}** ({self.outcome.replace('_', ' ')})",
        ]
        if self.quality_score is not None:
            lines.append(f"- Quality score: {self.quality_score:.1f}")
        for label, items in (
            ("Errors", self.error_checks),
            ("Failed", self.failed_checks),
            ("Warnings", self.warning_checks),
        ):
            if items:
                lines.append(f"- {label}: " + ", ".join(f"`{i}`" for i in items[:20]))
        if self.results_path:
            lines.append(f"- Results: `{self.results_path}`")
        return "\n".join(lines) + "\n"


class QualityGateError(DvfError):
    """A run did not pass its quality gates. :attr:`summary` holds the run's summary."""

    def __init__(self, summary: DvfRunSummary) -> None:
        super().__init__(summary.message())
        self.summary = summary
        self.exit_code = summary.exit_code


def parse_severity(value: Severity | str) -> Severity:
    """``value`` as a :class:`Severity`, or a :class:`ConfigError` naming the valid values."""
    try:
        return Severity(str(value).strip().lower())
    except ValueError:
        valid = ", ".join(s.value for s in Severity)
        raise ConfigError(f"'fail_on' must be one of {valid}, got '{value}'.") from None


def split_selectors(value: str | Iterable[str] | None) -> list[str]:
    """Selectors from a list, or from one string separated by commas, spaces, or newlines.

    Orchestrator parameters and CI variables are often single strings, so
    ``"tag:pr, table:orders"`` and ``["tag:pr", "table:orders"]`` mean the same.
    """
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [part for item in items for part in _SPLIT.split(str(item)) if part]


def _check_ids(result: RunResult, status: Status) -> tuple[str, ...]:
    return tuple(c.check_id for c in result.iter_checks() if c.status is status)


def summarize(
    output: api.RunOutput | RunResult,
    *,
    fail_on: Severity | str = Severity.ERROR,
    min_score: float | None = None,
    strict: bool = False,
) -> DvfRunSummary:
    """Summarize a finished run and compute its exit code with the given gates."""
    severity = parse_severity(fail_on)
    report_dir: Path | None = None
    reports: tuple[str, ...] = ()
    if isinstance(output, RunResult):
        result = output
    else:
        result = output.result
        report_dir = output.output_dir
        reports = tuple(str(_artifact_path(a.path, report_dir)) for a in output.artifacts)
    results_path: str | None = None
    if report_dir is not None and (report_dir / RESULTS_FILE).is_file():
        results_path = str(report_dir / RESULTS_FILE)
    counts = {status.value: n for status, n in result.summary.checks_by_status.items() if n}
    code = api.exit_code(result, fail_on=severity, strict=strict, min_score=min_score)
    return DvfRunSummary(
        run_id=result.run_id,
        project=result.project,
        status=result.status.value,
        exit_code=code,
        quality_score=result.summary.quality_score,
        checks_by_status=counts,
        failed_checks=_check_ids(result, Status.FAILED),
        error_checks=_check_ids(result, Status.ERROR),
        warning_checks=_check_ids(result, Status.WARNING),
        results_path=results_path,
        report_dir=str(report_dir) if report_dir is not None else None,
        reports=reports,
        fail_on=severity.value,
        min_score=min_score,
        strict=strict,
        duration_s=round(result.duration_ms / 1000, 3) if result.duration_ms is not None else None,
    )


def _artifact_path(path: str, report_dir: Path | None) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or report_dir is None:
        return candidate
    return report_dir / candidate


def run_validation(
    config: str | os.PathLike[str] | api.LoadedConfig | api.DvfConfig,
    *,
    profile: str | None = None,
    only: str | Sequence[str] | None = None,
    overrides: Sequence[str] = (),
    variables: Mapping[str, str] | None = None,
    env_file: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    formats: Sequence[str] | None = None,
    write_reports: bool = True,
    run_id: str | None = None,
    history: bool = True,
    baseline: str | None = None,
    parallel: int | None = None,
    plugins: Sequence[str] = (),
    fail_on: Severity | str = Severity.ERROR,
    min_score: float | None = None,
    strict: bool = False,
) -> tuple[api.RunOutput, DvfRunSummary]:
    """Run ``config`` with :func:`dvf.api.run` and summarize it with the given gates.

    Configuration and connection problems raise :class:`~dvf.core.errors.ConfigError` and
    :class:`~dvf.core.errors.ExecutionError` as in :func:`dvf.api.run`; a run that completes but
    fails its gates is returned with ``summary.passed`` false (see :func:`enforce_gate`).
    """
    severity = parse_severity(fail_on)
    output = api.run(
        config,
        profile=profile,
        overrides=list(overrides),
        variables=dict(variables) if variables else None,
        env_file=env_file,
        only=split_selectors(only),
        run_id=run_id,
        output_dir=output_dir,
        formats=list(formats) if formats is not None else None,
        write_reports=write_reports,
        plugins=list(plugins),
        parallel=parallel,
        history=history,
        baseline=baseline,
    )
    return output, summarize(output, fail_on=severity, min_score=min_score, strict=strict)


def enforce_gate(summary: DvfRunSummary) -> DvfRunSummary:
    """Return ``summary``, or raise :class:`QualityGateError` when the run failed its gates."""
    if not summary.passed:
        raise QualityGateError(summary)
    return summary


def problem_rows(result: RunResult, limit: int = 100) -> list[dict[str, Any]]:
    """Errored, failed, and warning checks as table rows, worst first (for UI artifacts)."""
    rank = {status: i for i, status in enumerate(_PROBLEMS)}
    problems = sorted(
        (c for c in result.iter_checks() if c.status in rank),
        key=lambda c: (rank[c.status], -c.severity.rank, c.check_id),
    )
    return [_row(check) for check in problems[:limit]]


def _row(check: CheckResult) -> dict[str, Any]:
    message = check.message or ""
    return {
        "check_id": check.check_id,
        "status": check.status.value,
        "severity": check.severity.value,
        "type": check.type,
        "message": message if len(message) <= 300 else message[:297] + "...",
    }


__all__ = [
    "EXIT_OUTCOMES",
    "DvfRunSummary",
    "QualityGateError",
    "enforce_gate",
    "parse_severity",
    "problem_rows",
    "run_validation",
    "split_selectors",
    "summarize",
]
