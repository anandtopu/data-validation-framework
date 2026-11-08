"""MIG-08: the cutover sign-off report (``signoff`` format: ``signoff.md`` and ``signoff.html``).

A short document for the go/no-go meeting of a mock or final cutover: run identity, a
recommendation, scope, results by table, open failures (new or persisting against the
baseline), waivers, the run-to-run comparison, and an approvals block to fill in. Like every
reporter it is rendered from the results document alone, so ``dvf report --format signoff`` can
produce it later from any ``results.json``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from dvf.core.enums import Severity, Status
from dvf.models.results import ArtifactRef, CheckResult, RunResult
from dvf.plugins.base import Reporter
from dvf.reporting._common import (
    DASH,
    FAILURE_STATUSES,
    STATUS_ORDER,
    baseline_marks,
    baseline_summary,
    format_count,
    format_datetime,
    format_pct,
    headline,
    iter_entries,
    quality_score,
    report_artifact,
    report_title,
    status_counts,
    truncate,
    waiver_statuses,
    write_text,
)
from dvf.reporting.base import ReportOptions

MARKDOWN_FILE = "signoff.md"
HTML_FILE = "signoff.html"
SIGN_OFF_ROLES = ("Business owner", "Data owner", "QA lead", "Cutover manager")
"""Default approver roles; add your own rows when filling in the document."""
MAX_LISTED = 200


@dataclass
class Recommendation:
    decision: str
    """``GO``, ``CONDITIONAL``, or ``NO-GO``."""
    reasons: list[str] = field(default_factory=list)


def recommend(run: RunResult) -> Recommendation:
    """NO-GO on execution errors, failures at error severity or above, or an interrupted run;
    CONDITIONAL on lower-severity failures or warnings; otherwise GO."""
    checks = list(run.iter_checks())
    errors = [c for c in checks if c.status is Status.ERROR]
    blocking = [
        c for c in checks if c.status is Status.FAILED and c.severity.at_least(Severity.ERROR)
    ]
    minor = [c for c in checks if c.status is Status.FAILED and c not in blocking]
    warnings = [c for c in checks if c.status is Status.WARNING]
    expired = [w for w in waiver_statuses(run) if w.status == "expired"]
    reasons: list[str] = []
    if run.interrupted:
        reasons.append("the run was interrupted before every table finished")
    if errors:
        reasons.append(f"{len(errors)} check(s) could not run (errors)")
    if blocking:
        reasons.append(f"{len(blocking)} failure(s) at error severity or above")
    if expired:
        reasons.append(f"{len(expired)} expired waiver(s)")
    if reasons:
        return Recommendation("NO-GO", reasons)
    if minor:
        reasons.append(f"{len(minor)} failure(s) below error severity")
    if warnings:
        reasons.append(f"{len(warnings)} warning(s)")
    if reasons:
        return Recommendation("CONDITIONAL", reasons)
    return Recommendation("GO", ["every check passed or is covered by an active waiver"])


@dataclass
class _Table:
    headers: list[str]
    rows: list[list[str]]


@dataclass
class _Section:
    title: str
    paragraphs: list[str] = field(default_factory=list)
    table: _Table | None = None
    notes: list[str] = field(default_factory=list)


def _sections(
    run: RunResult, options: ReportOptions
) -> tuple[list[tuple[str, str]], list[_Section]]:
    """The identity block and the sections, shared by both formats."""
    recommendation = recommend(run)
    env = run.environment
    identity: list[tuple[str, str]] = [
        ("Project", run.project),
        ("Run ID", run.run_id),
        ("Run started (UTC)", format_datetime(run.started_at)),
        ("Overall status", run.status.value.upper()),
        (
            "Recommendation",
            f"{recommendation.decision}: {'; '.join(recommendation.reasons)}",
        ),
        ("Quality score", format_pct(quality_score(run))),
        ("Profile", env.profile or DASH),
        ("Configuration hash", (env.config_hash or DASH)[:16]),
        ("Git commit", (env.git_sha or DASH)[:12]),
        ("dvf version", env.dvf_version),
    ]
    for key, value in {**options.metadata, **run.metadata}.items():
        identity.append((str(key), str(value)))

    datasets = [(s, d) for s in run.suites for d in s.datasets]
    scope = _Section(
        "Scope",
        [
            f"{len(run.suites)} suite(s) and {len(datasets)} table(s); "
            f"{format_count(run.summary.rows_compared)} source rows compared."
        ],
        _Table(
            ["Suite", "Table", "Source", "Target", "Source rows", "Target rows"],
            [
                [
                    s.name,
                    d.name,
                    d.source.display() if d.source else DASH,
                    d.target.display() if d.target else DASH,
                    format_count(d.row_counts.source),
                    format_count(d.row_counts.target),
                ]
                for s, d in datasets[:MAX_LISTED]
            ],
        ),
    )
    statuses = list(STATUS_ORDER)
    results = _Section(
        "Results by table",
        table=_Table(
            ["Suite", "Table", "Status", *[st.value.capitalize() for st in statuses], "Match %"],
            [
                [
                    s.name,
                    d.name,
                    d.status.value.upper(),
                    *[str(n) for n in status_counts(d.checks).values()],
                    format_pct(d.reconciliation.match_pct) if d.reconciliation else DASH,
                ]
                for s, d in datasets[:MAX_LISTED]
            ],
        ),
    )
    marks = baseline_marks(run)
    open_checks: list[CheckResult] = [
        e.check for e in iter_entries(run) if e.check.status in FAILURE_STATUSES
    ]
    open_checks.sort(key=lambda c: (-c.severity.rank, c.check_id))
    failures = _Section(
        "Open failures",
        [] if open_checks else ["None."],
        _Table(
            ["Check", "Severity", "Status", "Since", "Finding"],
            [
                [
                    c.check_id,
                    c.severity.value,
                    c.status.value,
                    marks.get(c.check_id, "new" if run.baseline else DASH),
                    truncate(headline(c), 300),
                ]
                for c in open_checks[:MAX_LISTED]
            ],
        )
        if open_checks
        else None,
    )
    if len(open_checks) > MAX_LISTED:
        failures.notes.append(f"{len(open_checks) - MAX_LISTED} more are in the full report.")
    waivers = waiver_statuses(run)
    waiver_section = _Section(
        "Waivers (accepted differences)",
        [] if waivers else ["None."],
        _Table(
            ["Check selector", "Column", "Status", "Reason", "Ticket", "Approved by", "Expires"],
            [
                [
                    w.selector,
                    w.column or DASH,
                    w.status,
                    w.reason,
                    w.ticket or DASH,
                    w.approved_by or DASH,
                    w.expires or DASH,
                ]
                for w in waivers
            ],
        )
        if waivers
        else None,
    )
    comparison = _Section("Run-to-run comparison")
    if run.baseline is not None:
        b = run.baseline
        comparison.paragraphs.append(baseline_summary(b) + f" (policy: {b.policy}).")
        if b.new_failures:
            comparison.notes.append("New failures: " + ", ".join(b.new_failures[:50]))
        if b.resolved:
            comparison.notes.append("Resolved: " + ", ".join(b.resolved[:50]))
    else:
        comparison.paragraphs.append(
            "No baseline run was available. Compare two rehearsals with "
            "'dvf history compare RUN_A RUN_B'."
        )
    approvals = _Section(
        "Approvals",
        [
            "Record each approver's decision: Approved, Rejected, or Conditional (list the "
            "conditions)."
        ],
        _Table(
            ["Role", "Name", "Decision", "Date", "Signature and conditions"],
            [[role, "", "", "", ""] for role in SIGN_OFF_ROLES],
        ),
    )
    return identity, [scope, results, failures, waiver_section, comparison, approvals]


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ") if text else " "


def render_markdown(run: RunResult, options: ReportOptions) -> str:
    identity, sections = _sections(run, options)
    lines = [f"# Cutover sign-off: {report_title(run, options)}", "", "| | |", "|---|---|"]
    lines += [f"| {_md_cell(k)} | {_md_cell(v)} |" for k, v in identity]
    for section in sections:
        lines += ["", f"## {section.title}", ""]
        for paragraph in section.paragraphs:
            lines += [paragraph, ""]
        if section.table is not None:
            lines.append("| " + " | ".join(_md_cell(h) for h in section.table.headers) + " |")
            lines.append("|" + "---|" * len(section.table.headers))
            lines += [
                "| " + " | ".join(_md_cell(c) for c in row) + " |" for row in section.table.rows
            ]
            lines.append("")
        lines += [f"- {note}" for note in section.notes]
    lines += ["", f"Generated by dvf {run.environment.dvf_version} from run {run.run_id}.", ""]
    return "\n".join(lines)


_CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:2rem;color:#1f2328;max-width:1100px}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #d0d7de}
table{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:.9rem}
th,td{border:1px solid #d0d7de;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:#f6f8fa}.go{color:#1a7f37}.no-go{color:#cf222e}.conditional{color:#9a6700}
.approvals td{height:2.2rem}@media print{body{margin:0}}
"""


def render_html(run: RunResult, options: ReportOptions) -> str:
    identity, sections = _sections(run, options)
    esc = html.escape
    decision = recommend(run).decision
    css_class = {"GO": "go", "NO-GO": "no-go"}.get(decision, "conditional")
    color = f"h2{{border-color:{options.primary_color}}}" if options.primary_color else ""
    out = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>Cutover sign-off: {esc(report_title(run, options))}</title>",
        f"<style>{_CSS}{color}</style></head><body>",
        f"<h1>Cutover sign-off: {esc(report_title(run, options))}</h1>",
        "<table>",
    ]
    for key, value in identity:
        cell = esc(value)
        if key == "Recommendation":
            cell = f'<strong class="{css_class}">{cell}</strong>'
        out.append(f"<tr><th>{esc(key)}</th><td>{cell}</td></tr>")
    out.append("</table>")
    for section in sections:
        out.append(f"<h2>{esc(section.title)}</h2>")
        out += [f"<p>{esc(p)}</p>" for p in section.paragraphs]
        if section.table is not None:
            css = ' class="approvals"' if section.title == "Approvals" else ""
            out.append(f"<table{css}><tr>")
            out += [f"<th>{esc(h)}</th>" for h in section.table.headers]
            out.append("</tr>")
            for row in section.table.rows:
                out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>")
            out.append("</table>")
        if section.notes:
            out.append("<ul>" + "".join(f"<li>{esc(n)}</li>" for n in section.notes) + "</ul>")
    out.append(
        f"<p><small>Generated by dvf {esc(run.environment.dvf_version)} from run "
        f"{esc(run.run_id)}.</small></p></body></html>"
    )
    return "\n".join(out)


class SignoffReporter(Reporter):
    """Cutover sign-off document (Markdown and printable HTML) with a go/no-go recommendation,
    open failures, waivers, the run-to-run comparison, and an approvals block."""

    format: ClassVar[str] = "signoff"

    def render(self, run: RunResult, out_dir: Path, options: ReportOptions) -> list[ArtifactRef]:
        md_path, html_path = out_dir / MARKDOWN_FILE, out_dir / HTML_FILE
        write_text(md_path, render_markdown(run, options))
        write_text(html_path, render_html(run, options))
        return [report_artifact(md_path, self.format), report_artifact(html_path, self.format)]


__all__ = ["SignoffReporter", "recommend", "render_html", "render_markdown"]
