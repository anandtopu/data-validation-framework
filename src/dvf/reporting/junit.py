"""The JUnit XML reporter (``junit.xml``) for CI test tabs.

Each dvf suite becomes a ``<testsuite>`` and each check a ``<testcase>``. Failed checks map to
``<failure>``, errored checks to ``<error>``, and skipped checks to ``<skipped>``. Warnings and
waived checks pass, with a note in ``<system-out>``; waived checks also carry ``waived`` test case
properties. A failure whose waiver has expired stays a failure and says so.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from dvf.core.enums import Status
from dvf.models.results import ArtifactRef, CheckResult, RunResult, SuiteResult
from dvf.plugins.base import Reporter
from dvf.reporting._common import (
    CheckEntry,
    artifact_label,
    baseline_marks,
    format_count,
    format_group,
    format_pct,
    format_value,
    headline,
    iter_entries,
    partition_table,
    quality_score,
    report_artifact,
    report_title,
    sorted_classifications,
    truncate,
    waiver_counts,
    waiver_expired,
    waiver_statuses,
    write_text,
    xml_text,
)

if TYPE_CHECKING:
    from dvf.reporting.base import ReportOptions

__all__ = ["JUNIT_FILE", "JUnitReporter", "build_junit", "render_junit", "xml_text"]

JUNIT_FILE = "junit.xml"

_MAX_VALUE = 1000
_MAX_SQL = 4000
_MAX_COLUMNS = 10
_MAX_METRICS = 20
_MAX_MESSAGE = 1000
_MAX_PARTITIONS = 20
_MAX_ARTIFACTS = 20


def _seconds(ms: float | None) -> str:
    return f"{(ms or 0.0) / 1000.0:.3f}"


def _sub(parent: ET.Element, tag: str, attrib: dict[str, object] | None = None) -> ET.Element:
    """Add a child element, cleaning every attribute value."""
    clean = {key: xml_text(val) for key, val in (attrib or {}).items() if val is not None}
    return ET.SubElement(parent, tag, clean)


def _text_element(
    parent: ET.Element, tag: str, text: str, attrib: dict[str, object] | None = None
) -> ET.Element:
    element = _sub(parent, tag, attrib)
    element.text = xml_text(text)
    return element


def _outcome_lines(entry: CheckEntry, mark: str | None = None) -> list[str]:
    """Plain-text evidence for a check: what was asserted, observed, and suggested."""
    check = entry.check
    lines = [
        f"Check: {check.check_id} ({check.name})",
        f"Table: {entry.location}",
        f"Type: {check.type}; dimension: {check.dimension}; severity: {check.severity}",
    ]
    if check.columns:
        lines.append("Columns: " + ", ".join(check.columns))
    if check.partition:
        lines.append(f"Partition: {check.partition}")
    if mark:
        lines.append(f"Baseline: {mark} failure")
    if check.message:
        lines.append(f"Message: {truncate(check.message, _MAX_MESSAGE)}")
    if check.observed is not None or check.expected is not None:
        lines.append(f"Observed: {format_value(check.observed, limit=_MAX_VALUE)}")
        lines.append(f"Expected: {format_value(check.expected, limit=_MAX_VALUE)}")
    if check.unexpected_count is not None:
        of_total = f" of {format_count(check.total_count)}" if check.total_count is not None else ""
        pct = f" ({format_pct(check.unexpected_pct)})" if check.unexpected_pct is not None else ""
        lines.append(f"Unexpected: {format_count(check.unexpected_count)}{of_total}{pct}")
    if check.mostly is not None:
        lines.append(f"Mostly: {format_value(check.mostly)}")
    lines.extend(_expired_waiver_lines(check))
    lines.extend(_mismatch_lines(check))
    lines.extend(_partition_lines(check))
    lines.extend(_metric_lines(check))
    if check.hints:
        lines.append("Hints:")
        lines.extend(f"  - {hint}" for hint in check.hints)
    for query in check.queries:
        dialect = f" ({query.dialect})" if query.dialect else ""
        lines.append(f"Query [{query.side}]{dialect}:")
        lines.extend("    " + line for line in truncate(query.sql, _MAX_SQL).splitlines())
    if check.engine:
        lines.append(f"Engine: {check.engine}")
    lines.extend(_artifact_lines(check))
    return lines


def _expired_waiver_lines(check: CheckResult) -> list[str]:
    waiver = check.waiver
    if waiver is None or not waiver.expired:
        return []
    when = f" on {waiver.expires}" if waiver.expires else ""
    column = f", column {waiver.column}" if waiver.column else ""
    ticket = f", ticket {waiver.ticket}" if waiver.ticket else ""
    return [
        f"Waiver EXPIRED{when}: {waiver.reason} (selector {waiver.selector}{column}{ticket}). "
        "The failure stands until the waiver is renewed or removed."
    ]


def _mismatch_lines(check: CheckResult) -> list[str]:
    if not check.column_mismatches:
        return []
    ranked = sorted(check.column_mismatches, key=lambda m: (-m.mismatch_count, m.column))
    lines = ["Column mismatches:"]
    for mismatch in ranked[:_MAX_COLUMNS]:
        classes = ", ".join(
            f"{name}: {format_count(n)}"
            for name, n in sorted_classifications(mismatch.classifications)
        )
        detail = f" [{classes}]" if classes else ""
        waived = " (waived)" if mismatch.waived else ""
        lines.append(
            f"  - {mismatch.column}: {format_count(mismatch.mismatch_count)} rows "
            f"({format_pct(mismatch.mismatch_pct)}){detail}{waived}"
        )
        lines.extend(f"      hint: {hint}" for hint in mismatch.hints)
    if len(ranked) > _MAX_COLUMNS:
        lines.append(f"  ... {len(ranked) - _MAX_COLUMNS} more columns")
    return lines


def _partition_lines(check: CheckResult) -> list[str]:
    table = partition_table(check)
    if table is None:
        return []
    failing = table.failing()
    if not failing:
        return [f"Partitions: {len(table.rows)}, none failing"]
    lines = [f"Failing partitions ({len(failing)} of {len(table.rows)}):"]
    for row in failing[:_MAX_PARTITIONS]:
        counts = ", ".join(
            f"{label.lower()} {format_value(row[key], limit=_MAX_VALUE)}"
            for key, label in table.columns
            if key not in ("name", "status") and key in row
        )
        name = format_value(row.get("name"), limit=_MAX_VALUE)
        status = format_value(row.get("status"))
        lines.append(f"  - {name} [{status}]" + (f": {counts}" if counts else ""))
    if len(failing) > _MAX_PARTITIONS:
        lines.append(f"  ... {len(failing) - _MAX_PARTITIONS} more failing partitions")
    return lines


def _metric_lines(check: CheckResult) -> list[str]:
    failing = [metric for metric in check.metrics if not metric.passed]
    if not failing:
        return []
    lines = ["Metric differences:"]
    for metric in failing[:_MAX_METRICS]:
        column = f"({metric.column})" if metric.column else ""
        group = f" [{format_group(metric.group)}]" if metric.group else ""
        tolerance = (
            f", tolerance {format_value(metric.tolerance)}" if metric.tolerance is not None else ""
        )
        lines.append(
            f"  - {metric.metric}{column}{group}: source {format_value(metric.source)}, "
            f"target {format_value(metric.target)}, difference "
            f"{format_value(metric.difference)}{tolerance}"
        )
    if len(failing) > _MAX_METRICS:
        lines.append(f"  ... {len(failing) - _MAX_METRICS} more metrics")
    return lines


def _artifact_lines(check: CheckResult) -> list[str]:
    if not check.artifacts:
        return []
    lines = ["Artifacts (paths relative to the report folder):"]
    for artifact in check.artifacts[:_MAX_ARTIFACTS]:
        lines.append(f"  - {artifact_label(artifact.kind)}: {artifact.path}")
    if len(check.artifacts) > _MAX_ARTIFACTS:
        lines.append(f"  ... {len(check.artifacts) - _MAX_ARTIFACTS} more artifacts")
    return lines


def _waiver_text(check: CheckResult) -> str:
    waiver = check.waiver
    if waiver is None:
        return f"WAIVED: {headline(check)}"
    parts = [waiver.reason]
    if waiver.ticket:
        parts.append(f"ticket {waiver.ticket}")
    if waiver.expires:
        parts.append(f"expires {waiver.expires}")
    if waiver.approved_by:
        parts.append(f"approved by {waiver.approved_by}")
    if waiver.column:
        parts.append(f"column {waiver.column}")
    parts.append(f"selector {waiver.selector}")
    return "WAIVED: " + ", ".join(parts)


def _case_properties(case: ET.Element, check: CheckResult, mark: str | None) -> None:
    """Test case properties for waived checks, expired waivers, and baseline changes."""
    props: dict[str, object | None] = {}
    waiver = check.waiver
    if check.status is Status.WAIVED:
        props["waived"] = "true"
        if waiver is not None:
            props.update(
                {
                    "waiver.reason": truncate(waiver.reason, _MAX_MESSAGE),
                    "waiver.ticket": waiver.ticket,
                    "waiver.expires": waiver.expires,
                    "waiver.approved_by": waiver.approved_by,
                    "waiver.selector": waiver.selector,
                    "waiver.column": waiver.column,
                }
            )
    elif waiver is not None and waiver.expired:
        props.update(
            {
                "waiver.expired": "true",
                "waiver.expires": waiver.expires,
                "waiver.ticket": waiver.ticket,
                "waiver.selector": waiver.selector,
            }
        )
    if mark:
        props["baseline"] = mark
    present = {name: value for name, value in props.items() if value is not None}
    if not present:
        return
    properties = _sub(case, "properties")
    for name, value in present.items():
        _sub(properties, "property", {"name": name, "value": value})


def _add_testcase(suite_element: ET.Element, entry: CheckEntry, mark: str | None = None) -> None:
    check = entry.check
    case = _sub(
        suite_element,
        "testcase",
        {"classname": entry.location, "name": check.check_id, "time": _seconds(check.duration_ms)},
    )
    _case_properties(case, check, mark)
    status = check.status
    if status is Status.FAILED:
        message = headline(check)
        if waiver_expired(check):
            message = f"{message} [waiver expired]"
        _text_element(
            case,
            "failure",
            "\n".join(_outcome_lines(entry, mark)),
            {"message": truncate(message, _MAX_MESSAGE), "type": check.type},
        )
    elif status is Status.ERROR:
        error = check.error
        body = [f"Check: {check.check_id}", f"Table: {entry.location}"]
        if mark:
            body.append(f"Baseline: {mark} failure")
        if error is not None and error.hint:
            body.append(f"Hint: {error.hint}")
        if error is not None and error.traceback:
            body.extend(["Traceback:", error.traceback])
        if error is None and check.message:
            body.append(check.message)
        _text_element(
            case,
            "error",
            "\n".join(body),
            {
                "message": truncate(error.message if error else headline(check), _MAX_MESSAGE),
                "type": error.type if error else "Error",
            },
        )
    elif status is Status.SKIPPED:
        _sub(case, "skipped", {"message": truncate(headline(check), _MAX_MESSAGE)})
    elif status is Status.WARNING:
        text = "\n".join([f"WARNING: {headline(check)}", *_outcome_lines(entry, mark)])
        _text_element(case, "system-out", text)
    elif status is Status.WAIVED:
        _text_element(case, "system-out", _waiver_text(check))


def _totals(checks: Iterable[CheckResult]) -> dict[str, int]:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for check in checks:
        totals["tests"] += 1
        if check.status is Status.FAILED:
            totals["failures"] += 1
        elif check.status is Status.ERROR:
            totals["errors"] += 1
        elif check.status is Status.SKIPPED:
            totals["skipped"] += 1
    return totals


def _suite_checks(suite: SuiteResult) -> Iterable[CheckResult]:
    for dataset in suite.datasets:
        yield from dataset.checks


def _suite_ms(suite: SuiteResult) -> float:
    if suite.duration_ms is not None:
        return suite.duration_ms
    return sum(check.duration_ms or 0.0 for check in _suite_checks(suite))


def _run_properties(run: RunResult) -> dict[str, object | None]:
    """Suite properties describing the run: interruption, baseline comparison, and waivers."""
    props: dict[str, object | None] = {
        "interrupted": "true" if run.interrupted else None,
        "resumed_from_checkpoint": "true" if run.resumed_from_checkpoint else None,
    }
    baseline = run.baseline
    if baseline is not None:
        props.update(
            {
                "baseline_run_id": baseline.baseline_run_id,
                "baseline_policy": baseline.policy,
                "baseline_new_failures": len(baseline.new_failures),
                "baseline_resolved": len(baseline.resolved),
                "baseline_persisting": len(baseline.persisting),
            }
        )
    waivers = waiver_statuses(run)
    if waivers:
        for status, count in waiver_counts(waivers).items():
            props[f"waivers_{status}"] = count
    return props


def build_junit(run: RunResult, options: ReportOptions) -> ET.Element:
    """Build the ``<testsuites>`` element for ``run``."""
    suite_ms = [_suite_ms(suite) for suite in run.suites]
    run_ms = run.duration_ms if run.duration_ms is not None else sum(suite_ms)
    root = ET.Element("testsuites")
    for key, val in {
        "name": report_title(run, options),
        **_totals(run.iter_checks()),
        "time": _seconds(run_ms),
    }.items():
        root.set(key, xml_text(val))

    score = quality_score(run)
    env = run.environment
    marks = baseline_marks(run)
    run_props = _run_properties(run)
    entries_by_suite: dict[int, list[CheckEntry]] = {}
    for entry in iter_entries(run):
        entries_by_suite.setdefault(id(entry.suite), []).append(entry)

    for suite, ms in zip(run.suites, suite_ms, strict=True):
        suite_element = _sub(
            root,
            "testsuite",
            {
                "name": suite.name,
                **_totals(_suite_checks(suite)),
                "time": _seconds(ms),
                "timestamp": run.started_at.isoformat(timespec="seconds"),
                "hostname": env.hostname,
            },
        )
        properties = _sub(suite_element, "properties")
        props: dict[str, object | None] = {
            "run_id": run.run_id,
            "project": run.project,
            "run_status": run.status,
            "suite_status": suite.status,
            "quality_score": None if score is None else f"{score:g}",
            "dvf_version": env.dvf_version,
            "profile": env.profile,
            "git_sha": env.git_sha,
            **run_props,
        }
        for name, value in props.items():
            if value is not None:
                _sub(properties, "property", {"name": name, "value": value})
        for entry in entries_by_suite.get(id(suite), []):
            _add_testcase(suite_element, entry, marks.get(entry.check.check_id))
    return root


def render_junit(run: RunResult, options: ReportOptions) -> str:
    """The JUnit XML document as text."""
    root = build_junit(run, options)
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


class JUnitReporter(Reporter):
    """JUnit XML for CI test tabs: one test suite per dvf suite and one test case per check."""

    format: ClassVar[str] = "junit"

    def render(self, run: RunResult, out_dir: Path, options: ReportOptions) -> list[ArtifactRef]:
        """Write ``junit.xml`` into ``out_dir``."""
        path = out_dir / JUNIT_FILE
        write_text(path, render_junit(run, options))
        return [report_artifact(path, self.format)]
