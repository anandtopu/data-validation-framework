"""The Markdown reporter (``summary.md``) for pull request comments and ``$GITHUB_STEP_SUMMARY``.

The summary stays below :data:`MAX_CHARS` characters (GitHub limits comments to 65,536). When
there are too many checks, the detail listing is cut with a note pointing to the full reports.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from dvf import __version__
from dvf.core.enums import Status
from dvf.models.results import ArtifactRef, CheckResult, RunResult
from dvf.plugins.base import Reporter
from dvf.reporting._common import (
    DASH,
    ELLIPSIS,
    FAILURE_STATUSES,
    STATUS_RANK,
    CheckEntry,
    dataset_count,
    format_count,
    format_datetime,
    format_duration,
    format_group,
    format_pct,
    format_value,
    headline,
    issue_sort_key,
    iter_entries,
    quality_score,
    report_artifact,
    report_title,
    sorted_classifications,
    status_counts,
    top_issues,
    truncate,
    write_text,
)

if TYPE_CHECKING:
    from dvf.reporting.base import ReportOptions

MARKDOWN_FILE = "summary.md"
MAX_CHARS = 60_000
"""Upper bound on the length of the summary, in characters."""

_MAX_TABLE_ROWS = 100
_MAX_TOP = 10
_MAX_SECTION_CHARS = 8_000
_MAX_CELL = 200
_MAX_TEXT = 500
_MAX_COLUMNS = 5
_MAX_HINTS = 3
_OMITTED_NOTE = "{ellipsis} {count} more checks omitted; see the HTML or JSON report."

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|&~])")
_NEWLINES = re.compile(r"\s*[\r\n]+\s*")
_BACKTICKS = re.compile(r"`+")


def md_escape(value: Any, limit: int | None = _MAX_CELL) -> str:
    """Inline Markdown text: one line, with Markdown and HTML syntax characters escaped.

    Escaping ``|`` makes the text safe inside table cells.
    """
    text = value if isinstance(value, str) else format_value(value)
    text = truncate(_NEWLINES.sub(" ", text), limit)
    return _MD_SPECIAL.sub(r"\\\1", text)


def md_code(value: str, *, table: bool = False) -> str:
    """An inline code span that survives backticks (and pipes, inside tables) in ``value``."""
    text = _NEWLINES.sub(" ", value)
    if not text:
        return ""
    longest = max((len(run) for run in _BACKTICKS.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    if table:
        text = text.replace("|", "\\|")
    return f"{fence}{pad}{text}{pad}{fence}"


def _status(status: Status) -> str:
    label = status.value.upper()
    return f"**{label}**" if status in FAILURE_STATUSES else label


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _unexpected(check: CheckResult) -> str:
    if check.unexpected_count is None:
        return DASH
    text = format_count(check.unexpected_count)
    if check.total_count is not None:
        text += f" of {format_count(check.total_count)}"
    if check.unexpected_pct is not None:
        text += f" ({format_pct(check.unexpected_pct)})"
    return text


# ---------------------------------------------------------------------- sections


def _header(run: RunResult, options: ReportOptions, counts: dict[Status, int]) -> list[str]:
    lines = [f"# {md_escape(report_title(run, options))}: {run.status.value.upper()}", ""]
    lines.append(
        f"Run {md_code(run.run_id)} · project {md_escape(run.project)} · started "
        f"{format_datetime(run.started_at)} · duration {format_duration(run.duration_ms)}"
    )
    meta = {**options.metadata, **run.metadata}
    if meta:
        lines.append("")
        lines.append(
            " · ".join(
                f"**{md_escape(str(key), 80)}:** {md_escape(value)}" for key, value in meta.items()
            )
        )
    score = quality_score(run)
    by_status = ", ".join(f"{n} {status.value}" for status, n in counts.items() if n) or "none"
    lines += [
        "",
        f"**Quality score:** {format_pct(score)} · **Checks:** {by_status} · "
        f"**Suites:** {len(run.suites)} · **Tables:** {dataset_count(run)} · "
        f"**Rows compared:** {format_count(run.summary.rows_compared)} · "
        f"**Duration:** {format_duration(run.duration_ms)}",
        "",
        "## Checks by status",
        "",
        "| Status | Checks |",
        "|:--|--:|",
    ]
    lines += [_row([status.value, str(n)]) for status, n in counts.items()]
    lines += [_row(["**total**", f"**{sum(counts.values())}**"]), ""]
    return lines


def _tables(run: RunResult) -> list[str]:
    pairs = [(suite, dataset) for suite in run.suites for dataset in suite.datasets]
    if not pairs:
        return []
    omitted = 0
    if len(pairs) > _MAX_TABLE_ROWS:
        omitted = len(pairs) - _MAX_TABLE_ROWS
        pairs = sorted(pairs, key=lambda pair: STATUS_RANK[pair[1].status])[:_MAX_TABLE_ROWS]
    lines = [
        "## Tables",
        "",
        "| Suite | Table | Status | Source rows | Target rows | Missing | Extra | Mismatched "
        "| Match % |",
        "|:--|:--|:--|--:|--:|--:|--:|--:|--:|",
    ]
    for suite, dataset in pairs:
        rec = dataset.reconciliation
        lines.append(
            _row(
                [
                    md_escape(suite.name),
                    md_escape(dataset.name),
                    _status(dataset.status),
                    format_count(dataset.row_counts.source),
                    format_count(dataset.row_counts.target),
                    format_count(rec.missing_in_target) if rec else DASH,
                    format_count(rec.extra_in_target) if rec else DASH,
                    format_count(rec.mismatched) if rec else DASH,
                    format_pct(rec.match_pct) if rec else DASH,
                ]
            )
        )
    if omitted:
        lines += ["", f"{ELLIPSIS} {omitted} more tables omitted; see the HTML or JSON report."]
    lines.append("")
    return lines


def _top_failure(number: int, entry: CheckEntry) -> list[str]:
    check = entry.check
    indent = " " * len(f"{number}. ")
    lines = [
        f"{number}. {_status(check.status)} · {check.severity.value} · {md_code(check.check_id)} "
        f"({md_escape(entry.location)}) {DASH} {md_escape(headline(check), _MAX_TEXT)}"
    ]
    outcome: list[str] = []
    if check.observed is not None or check.expected is not None:
        outcome.append(
            f"Observed {md_escape(check.observed)}, expected {md_escape(check.expected)}"
        )
    if check.unexpected_count is not None:
        outcome.append(f"Unexpected {_unexpected(check)}")
    if outcome:
        lines.append(f"{indent}- " + "; ".join(outcome))
    ranked = sorted(check.column_mismatches, key=lambda m: (-m.mismatch_count, m.column))
    for mismatch in ranked[:_MAX_COLUMNS]:
        classes = ", ".join(
            f"{md_escape(name)} {format_count(n)}"
            for name, n in sorted_classifications(mismatch.classifications)
        )
        lines.append(
            f"{indent}- {md_code(mismatch.column)}: {format_count(mismatch.mismatch_count)} "
            f"mismatches ({format_pct(mismatch.mismatch_pct)})"
            + (f": {classes}" if classes else "")
        )
        lines += [
            f"{indent}  - Hint: {md_escape(h, _MAX_TEXT)}" for h in mismatch.hints[:_MAX_HINTS]
        ]
    if len(ranked) > _MAX_COLUMNS:
        lines.append(f"{indent}- {ELLIPSIS} {len(ranked) - _MAX_COLUMNS} more columns")
    failing = [metric for metric in check.metrics if not metric.passed]
    for metric in failing[:_MAX_COLUMNS]:
        name = metric.metric + (f"({metric.column})" if metric.column else "")
        group = f" [{format_group(metric.group)}]" if metric.group else ""
        lines.append(
            f"{indent}- Metric {md_escape(name + group)}: source {md_escape(metric.source)}, "
            f"target {md_escape(metric.target)}, difference {md_escape(metric.difference)}"
        )
    if len(failing) > _MAX_COLUMNS:
        lines.append(f"{indent}- {ELLIPSIS} {len(failing) - _MAX_COLUMNS} more failing metrics")
    if check.error is not None:
        error = f"{check.error.type}: {check.error.message}"
        hint = f" · Hint: {md_escape(check.error.hint, _MAX_TEXT)}" if check.error.hint else ""
        lines.append(f"{indent}- Error: {md_escape(error, _MAX_TEXT)}{hint}")
    lines += [f"{indent}- Hint: {md_escape(h, _MAX_TEXT)}" for h in check.hints[:_MAX_HINTS]]
    return lines


def _top_failures(entries: list[CheckEntry]) -> list[str]:
    lines = ["## Top failures", ""]
    top = top_issues(entries, _MAX_TOP, FAILURE_STATUSES)
    if not top:
        lines += ["No failed or errored checks.", ""]
        return lines
    for number, entry in enumerate(top, start=1):
        lines += _top_failure(number, entry)
    lines.append("")
    return lines


def _extra_sections(options: ReportOptions) -> list[str]:
    lines: list[str] = []
    for section in options.extra_sections:
        content = section.content.rstrip()
        if len(content) > _MAX_SECTION_CHARS:
            content = (
                content[:_MAX_SECTION_CHARS]
                + f"\n\n{ELLIPSIS} section truncated; see the HTML report."
            )
        lines += [f"## {md_escape(section.title)}", "", content, ""]
    return lines


def _detail_row(entry: CheckEntry) -> str:
    check = entry.check
    message = headline(check)
    if check.status is Status.WAIVED and check.waiver is not None:
        ticket = f" ({check.waiver.ticket})" if check.waiver.ticket else ""
        message = f"Waived: {check.waiver.reason}{ticket}"
    return _row(
        [
            _status(check.status),
            check.severity.value,
            md_code(check.check_id, table=True),
            md_escape(entry.location),
            md_escape(message),
            _unexpected(check),
        ]
    )


def _details(entries: list[CheckEntry], budget: int) -> list[str]:
    """All non-passed checks in a collapsible block, cut to fit ``budget`` characters."""
    if not entries:
        return []
    opening = [
        "<details>",
        f"<summary>All non-passed checks ({len(entries)})</summary>",
        "",
        "| Status | Severity | Check | Table | Message | Unexpected |",
        "|:--|:--|:--|:--|:--|--:|",
    ]
    closing = ["", "</details>", ""]
    note = _OMITTED_NOTE.format(ellipsis=ELLIPSIS, count=len(entries))
    used = len("\n".join(opening + closing)) + len(note) + 4
    if used > budget:
        return [note, ""]
    rows: list[str] = []
    omitted = 0
    for position, entry in enumerate(entries):
        row = _detail_row(entry)
        if used + len(row) + 1 > budget:
            omitted = len(entries) - position
            break
        rows.append(row)
        used += len(row) + 1
    lines = opening + rows
    if omitted:
        lines += ["", _OMITTED_NOTE.format(ellipsis=ELLIPSIS, count=omitted)]
    return lines + closing


def render_markdown(run: RunResult, options: ReportOptions) -> str:
    """The Markdown summary as text, at most :data:`MAX_CHARS` characters long."""
    entries = list(iter_entries(run))
    counts = status_counts(entry.check for entry in entries)
    head = [
        *_header(run, options, counts),
        *_tables(run),
        *_top_failures(entries),
        *_extra_sections(options),
    ]
    footer = (
        f"<sub>Generated by dvf {md_escape(__version__)} from results schema "
        f"{md_escape(run.schema_version)}. The HTML and JSON reports have every detail.</sub>\n"
    )
    head_text = "\n".join(head) + "\n"
    non_passed = sorted(
        (entry for entry in entries if entry.check.status is not Status.PASSED),
        key=issue_sort_key,
    )
    budget = MAX_CHARS - len(head_text) - len(footer)
    details = _details(non_passed, budget)
    text = head_text + ("\n".join(details) + "\n" if details else "") + footer
    if len(text) > MAX_CHARS:
        cut = f"\n\n{ELLIPSIS} summary truncated; see the HTML or JSON report.\n"
        text = text[: MAX_CHARS - len(cut)] + cut
    return text


class MarkdownReporter(Reporter):
    """Markdown summary for pull request comments and CI job summaries."""

    format: ClassVar[str] = "markdown"

    def render(self, run: RunResult, out_dir: Path, options: ReportOptions) -> list[ArtifactRef]:
        """Write ``summary.md`` into ``out_dir``."""
        path = out_dir / MARKDOWN_FILE
        write_text(path, render_markdown(run, options))
        return [report_artifact(path, self.format)]
