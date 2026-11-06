"""The JUnit XML reporter (``junit.xml``) for CI test tabs.

Each dvf suite becomes a ``<testsuite>`` and each check a ``<testcase>``. Failed checks map to
``<failure>``, errored checks to ``<error>``, and skipped checks to ``<skipped>``. Warnings and
waived checks pass, with a note in ``<system-out>``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from dvf.core.enums import Status
from dvf.models.results import ArtifactRef, CheckResult, RunResult, SuiteResult
from dvf.plugins.base import Reporter
from dvf.reporting._common import (
    CheckEntry,
    format_count,
    format_group,
    format_pct,
    format_value,
    headline,
    iter_entries,
    quality_score,
    report_artifact,
    report_title,
    sorted_classifications,
    truncate,
    write_text,
)

if TYPE_CHECKING:
    from dvf.reporting.base import ReportOptions

JUNIT_FILE = "junit.xml"

_INVALID_XML_CHARS = re.compile(r"[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")
"""Characters XML 1.0 does not allow, including lone surrogates."""

_MAX_VALUE = 1000
_MAX_SQL = 4000
_MAX_COLUMNS = 10
_MAX_METRICS = 20
_MAX_MESSAGE = 1000


def xml_text(value: object) -> str:
    """``value`` as text that is valid in XML 1.0 content and attributes."""
    text = value if isinstance(value, str) else str(value)
    return _INVALID_XML_CHARS.sub("", text)


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


def _outcome_lines(entry: CheckEntry) -> list[str]:
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
    lines.extend(_mismatch_lines(check))
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
    return lines


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
        lines.append(
            f"  - {mismatch.column}: {format_count(mismatch.mismatch_count)} rows "
            f"({format_pct(mismatch.mismatch_pct)}){detail}"
        )
        lines.extend(f"      hint: {hint}" for hint in mismatch.hints)
    if len(ranked) > _MAX_COLUMNS:
        lines.append(f"  ... {len(ranked) - _MAX_COLUMNS} more columns")
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
    parts.append(f"selector {waiver.selector}")
    return "WAIVED: " + ", ".join(parts)


def _add_testcase(suite_element: ET.Element, entry: CheckEntry) -> None:
    check = entry.check
    case = _sub(
        suite_element,
        "testcase",
        {"classname": entry.location, "name": check.check_id, "time": _seconds(check.duration_ms)},
    )
    status = check.status
    if status is Status.FAILED:
        _text_element(
            case,
            "failure",
            "\n".join(_outcome_lines(entry)),
            {"message": truncate(headline(check), _MAX_MESSAGE), "type": check.type},
        )
    elif status is Status.ERROR:
        error = check.error
        body = [f"Check: {check.check_id}", f"Table: {entry.location}"]
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
        text = "\n".join([f"WARNING: {headline(check)}", *_outcome_lines(entry)])
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
        }
        for name, value in props.items():
            if value is not None:
                _sub(properties, "property", {"name": name, "value": value})
        for entry in entries_by_suite.get(id(suite), []):
            _add_testcase(suite_element, entry)
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
