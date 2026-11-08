"""The Excel reporter (``report.xlsx``) for business sign-off.

Sheets: Summary, Tables, Checks, Schema diff, Mismatches, Samples, Waivers, Trend (only when the
result has a trend), and Sign-off. The workbook is written in openpyxl's write-only (streaming)
mode and every sheet has a row cap, so very large results still produce a bounded file.

Privacy: everything comes from the result document, and only the parts a business reviewer needs.
Sample values appear exactly as the result holds them (already masked by the run's privacy
settings); the configuration snapshot, SQL text, and tracebacks are left out. Text is always
written as text, never as a formula, so a value such as ``=HYPERLINK(...)`` cannot run on open.

Needs the ``excel`` extra (``openpyxl``).
"""

from __future__ import annotations

import heapq
import importlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from dvf.core.enums import Status
from dvf.core.errors import DependencyError
from dvf.models.results import ArtifactRef, CheckResult, RunResult, SampleSet, WaiverApplied
from dvf.plugins.base import Reporter
from dvf.reporting._common import (
    STATUS_RANK,
    CheckEntry,
    artifact_label,
    baseline_marks,
    dataset_count,
    dimension_scores,
    format_duration,
    format_pct,
    format_value,
    headline,
    iter_entries,
    quality_score,
    report_artifact,
    report_title,
    rows_per_second,
    sorted_classifications,
    status_counts,
    top_classification,
    top_issues,
    trend_series,
    waiver_counts,
    waiver_statuses,
)

if TYPE_CHECKING:
    from dvf.reporting.base import ReportOptions

EXCEL_FILE = "report.xlsx"

MAX_CHECK_ROWS = 20_000
"""Checks listed on the Checks sheet. Above it, the most severe outcomes are kept."""
MAX_SCHEMA_ROWS = 50_000
MAX_MISMATCH_ROWS = 50_000
MAX_SAMPLE_ROWS = 20_000
"""Sample rows on the Samples sheet in total (each sample is also capped per check)."""
MAX_WAIVER_ROWS = 10_000
MAX_TREND_ROWS = 1_000
MAX_LISTED_IDS = 200
"""Check IDs listed in one waiver's cell."""
TEXT_LIMIT = 2_000
"""Characters per text cell (Excel allows 32,767; long values are cut with an ellipsis)."""

SIGN_OFF_DECISIONS = ("Approved", "Rejected", "Conditional")
SIGN_OFF_ROLES = ("Business owner", "Data owner", "QA lead", "", "")

_HEADER_COLOR = "1F4E79"
_TONES: dict[str, tuple[str, str]] = {
    "passed": ("1A7F37", "DAFBE1"),
    "warning": ("9A6700", "FFF8C5"),
    "failed": ("CF222E", "FFEBE9"),
    "error": ("8B1A6B", "F9E3F1"),
    "skipped": ("6E7781", "EAEEF2"),
    "waived": ("0969DA", "DDF4FF"),
}
"""Status tone to (font color, fill color), matching the HTML report."""
_TONE_OF: dict[str, str] = {
    **{status.value: status.value for status in Status},
    "applied": "waived",
    "expired": "failed",
    "unused": "skipped",
    "new": "failed",
    "persisting": "warning",
    "resolved": "passed",
    "matched": "passed",
    "missing_in_target": "failed",
    "extra_in_target": "failed",
    "exact": "passed",
    "compatible_widening": "passed",
    "lossy": "warning",
    "incompatible": "failed",
    "unknown": "skipped",
}

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
"""Characters Excel files cannot hold (control characters, lone surrogates)."""
_ERROR_LITERALS = frozenset({"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A"})
_MAX_EXACT_INT = 10**15
"""Excel stores numbers as doubles; larger integers are written as text to keep every digit."""

_INT = "#,##0"
_PCT = "0.00##%"
_DATETIME = "yyyy-mm-dd hh:mm:ss"
_DATE = "yyyy-mm-dd"


def _import_openpyxl() -> Any:
    try:
        return importlib.import_module("openpyxl")
    except ImportError as exc:
        raise DependencyError("openpyxl", "excel", "The Excel report") from exc


# ---------------------------------------------------------------------- values


def _clean(text: str, limit: int = TEXT_LIMIT) -> str:
    text = _ILLEGAL.sub("", text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def excel_value(value: Any, limit: int = TEXT_LIMIT) -> Any:
    """``value`` as something openpyxl can store: numbers, booleans, and naive UTC datetimes
    stay as they are, everything else becomes clean, length-capped text."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, int):
        return value if abs(value) < _MAX_EXACT_INT else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else format_value(value)
    if isinstance(value, datetime):
        return _naive_utc(value)
    if isinstance(value, date):
        return value
    if isinstance(value, time):
        return value.isoformat() if value.tzinfo is not None else value
    if isinstance(value, str):
        return _clean(value, limit)
    return _clean(format_value(value), limit)


def _pct(value: float | None) -> float | None:
    """A 0 to 100 percentage as the fraction Excel's percent format expects."""
    if value is None or not math.isfinite(value):
        return None
    return value / 100.0


def _yes(flag: bool) -> str:
    return "Yes" if flag else "No"


def _text(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    """Any result value as display text (structured values as JSON), or None when unset."""
    if value is None:
        return None
    return _clean(format_value(value), limit)


def _lines(items: Iterable[str], limit: int = TEXT_LIMIT) -> str | None:
    text = "\n".join(item for item in items if item)
    return _clean(text, limit) if text else None


# ---------------------------------------------------------------------- workbook plumbing


@dataclass(frozen=True, slots=True)
class Column:
    """A sheet column: heading, width in characters, and how values are shown."""

    name: str
    width: float
    kind: str = "text"
    """``text``, ``int``, ``pct`` (0 to 100 values), ``ms``, ``status``, ``datetime``, or ``yes``."""


class _Book:
    """A write-only workbook with the shared styles every sheet uses."""

    def __init__(self, openpyxl: Any) -> None:
        styles = importlib.import_module("openpyxl.styles")
        self._cell_factory = importlib.import_module("openpyxl.cell").WriteOnlyCell
        self.get_column_letter = importlib.import_module("openpyxl.utils").get_column_letter
        self.DataValidation = importlib.import_module(
            "openpyxl.worksheet.datavalidation"
        ).DataValidation
        self.chart = importlib.import_module("openpyxl.chart")
        self.wb = openpyxl.Workbook(write_only=True)

        thin = styles.Side(style="thin", color="B7C3D0")
        self.header_font = styles.Font(bold=True, color="FFFFFF")
        self.header_fill = styles.PatternFill("solid", fgColor=_HEADER_COLOR)
        self.title_font = styles.Font(bold=True, size=14, color=_HEADER_COLOR)
        self.section_font = styles.Font(bold=True, size=12, color=_HEADER_COLOR)
        self.bold = styles.Font(bold=True)
        self.note_font = styles.Font(italic=True, color="59636E")
        self.null_font = styles.Font(italic=True, color="8C959F")
        self.wrap = styles.Alignment(wrap_text=True, vertical="top")
        self.input_fill = styles.PatternFill("solid", fgColor="FFFBE6")
        self.border = styles.Border(left=thin, right=thin, top=thin, bottom=thin)
        self.tone_font = {
            tone: styles.Font(bold=True, color=fg) for tone, (fg, _bg) in _TONES.items()
        }
        self.tone_fill = {
            tone: styles.PatternFill("solid", fgColor=bg) for tone, (_fg, bg) in _TONES.items()
        }

    def cell(
        self,
        ws: Any,
        value: Any,
        *,
        number_format: str | None = None,
        tone: str | None = None,
        font: Any = None,
        fill: Any = None,
        alignment: Any = None,
        border: Any = None,
    ) -> Any:
        """A styled write-only cell. Text is forced to be text (never a formula or error)."""
        cell = self._cell_factory(ws, value=excel_value(value))
        if isinstance(cell.value, str):
            cell.data_type = "s"
        if number_format:
            cell.number_format = number_format
        if tone in self.tone_font:
            cell.font = self.tone_font[tone]
            cell.fill = self.tone_fill[tone]
        if font is not None:
            cell.font = font
        if fill is not None:
            cell.fill = fill
        if alignment is not None:
            cell.alignment = alignment
        if border is not None:
            cell.border = border
        return cell

    def plain(self, ws: Any, value: Any) -> Any:
        """An unstyled value, as a cell only when that is needed to keep text from being parsed."""
        converted = excel_value(value)
        if isinstance(converted, str) and (
            converted.startswith("=") or converted in _ERROR_LITERALS
        ):
            return self.cell(ws, converted)
        return converted

    def save(self, path: Path) -> None:
        self.wb.save(str(path))


def _tone(value: Any) -> str | None:
    if value is None:
        return None
    key = value.value if isinstance(value, Enum) else str(value)
    return _TONE_OF.get(key)


class _Sheet:
    """One worksheet: column widths, a frozen, filterable header row, and typed data rows."""

    def __init__(
        self,
        book: _Book,
        title: str,
        columns: Sequence[Column],
        *,
        freeze: str | None = "A2",
        header: bool = True,
    ) -> None:
        self.book = book
        self.ws = book.wb.create_sheet(title)
        self.columns = list(columns)
        for index, column in enumerate(self.columns, start=1):
            self.ws.column_dimensions[book.get_column_letter(index)].width = column.width
        if freeze:
            self.ws.freeze_panes = freeze
        self.rows = 0
        self.data_rows = 0
        if header:
            self.append_raw(
                [
                    book.cell(self.ws, column.name, font=book.header_font, fill=book.header_fill)
                    for column in self.columns
                ]
            )

    def append_raw(self, cells: Sequence[Any]) -> None:
        self.ws.append(list(cells))
        self.rows += 1

    def blank(self) -> None:
        self.append_raw([])

    def row(
        self,
        values: Sequence[Any],
        *,
        fill: Any = None,
        tones: Mapping[str, str | None] | None = None,
    ) -> None:
        """A data row typed by the sheet's columns. ``tones`` sets a tone by column name."""
        book, ws = self.book, self.ws
        cells: list[Any] = []
        for column, value in zip(self.columns, values, strict=False):
            tone = tones.get(column.name) if tones else None
            kind = column.kind
            if kind == "status" or tone is not None:
                cells.append(book.cell(ws, value, tone=tone or _tone(value), fill=fill))
            elif kind in ("int", "ms"):
                cells.append(book.cell(ws, value, number_format=_INT, fill=fill))
            elif kind == "pct":
                cells.append(book.cell(ws, _pct(value), number_format=_PCT, fill=fill))
            elif kind == "datetime":
                cells.append(book.cell(ws, value, number_format=_DATETIME, fill=fill))
            elif kind == "yes":
                flag = bool(value)
                cells.append(book.cell(ws, _yes(flag), tone="waived" if flag else None, fill=fill))
            elif fill is not None:
                cells.append(book.cell(ws, value, fill=fill))
            else:
                cells.append(book.plain(ws, value))
        self.append_raw(cells)
        self.data_rows += 1

    def note(self, text: str) -> None:
        self.append_raw([self.book.cell(self.ws, text, font=self.book.note_font)])

    def filter(self) -> None:
        """Add an autofilter over the header and data rows written so far."""
        if self.data_rows:
            last = self.book.get_column_letter(len(self.columns))
            self.ws.auto_filter.ref = f"A1:{last}{self.data_rows + 1}"


# ---------------------------------------------------------------------- sheets


def _select_checks(entries: list[CheckEntry], cap: int) -> tuple[list[CheckEntry], int]:
    """At most ``cap`` checks in run order, keeping the most severe outcomes when cutting."""
    if len(entries) <= cap:
        return entries, 0
    keep = heapq.nsmallest(
        cap,
        entries,
        key=lambda e: (STATUS_RANK[e.check.status], -e.check.severity.rank, e.index),
    )
    keep.sort(key=lambda e: e.index)
    return keep, len(entries) - cap


def _waiver_text(waiver: WaiverApplied | None) -> str | None:
    if waiver is None:
        return None
    parts = [waiver.reason]
    if waiver.column:
        parts.append(f"column {waiver.column}")
    if waiver.ticket:
        parts.append(f"ticket {waiver.ticket}")
    if waiver.approved_by:
        parts.append(f"approved by {waiver.approved_by}")
    parts.append(f"selector {waiver.selector}")
    if waiver.expired:
        when = f" on {waiver.expires}" if waiver.expires else ""
        return f"EXPIRED{when}: " + "; ".join(parts)
    if waiver.expires:
        parts.append(f"expires {waiver.expires}")
    return "; ".join(parts)


def _check_hints(check: CheckResult) -> str | None:
    hints = list(check.hints)
    if check.error is not None and check.error.hint:
        hints.append(check.error.hint)
    return _lines(hints)


class _Writer:
    """Writes every sheet of the workbook for one run."""

    def __init__(self, book: _Book, run: RunResult, options: ReportOptions) -> None:
        self.book = book
        self.run = run
        self.options = options
        self.entries = list(iter_entries(run))
        listed = [
            e for e in self.entries if options.include_passed or e.check.status is not Status.PASSED
        ]
        self.omitted_passed = len(self.entries) - len(listed)
        self.checks, self.truncated_checks = _select_checks(listed, MAX_CHECK_ROWS)
        self.marks = baseline_marks(run)
        self.waivers = waiver_statuses(run)
        self.trend = trend_series(run)

    def write(self) -> None:
        self.summary()
        self.tables()
        self.check_sheet()
        self.schema_diff()
        self.mismatches()
        self.samples()
        self.waiver_sheet()
        if self.trend:
            self.trend_sheet()
        self.sign_off()

    # ------------------------------------------------------------------ Summary
    def _pair(self, sheet: _Sheet, label: str, value: Any, *, kind: str = "text") -> None:
        book, ws = self.book, sheet.ws
        tone = None
        number_format = None
        if kind == "status":
            tone = _tone(value)
        elif kind == "pct":
            value, number_format = _pct(value), _PCT
        elif kind == "int":
            number_format = _INT
        elif kind == "datetime":
            number_format = _DATETIME
        sheet.append_raw(
            [
                book.cell(ws, label, font=book.bold),
                book.cell(ws, value, tone=tone, number_format=number_format),
            ]
        )

    def _section(self, sheet: _Sheet, title: str) -> None:
        sheet.blank()
        sheet.append_raw([self.book.cell(sheet.ws, title, font=self.book.section_font)])

    def summary(self) -> None:
        run, book = self.run, self.book
        sheet = _Sheet(
            book,
            "Summary",
            [
                Column("Item", 30),
                Column("Value", 48),
                Column("", 14),
                Column("", 46),
                Column("", 30),
                Column("", 50),
                Column("", 16),
            ],
            freeze=None,
            header=False,
        )
        ws = sheet.ws
        title = f"{report_title(run, self.options)}: data validation sign-off report"
        sheet.append_raw([book.cell(ws, title, font=book.title_font)])
        sheet.blank()
        self._pair(sheet, "Overall status", run.status, kind="status")
        self._pair(sheet, "Quality score", quality_score(run), kind="pct")
        self._pair(sheet, "Project", run.project)
        self._pair(sheet, "Run ID", run.run_id)
        self._pair(sheet, "Started (UTC)", run.started_at, kind="datetime")
        self._pair(sheet, "Ended (UTC)", run.ended_at, kind="datetime")
        self._pair(sheet, "Duration", format_duration(run.duration_ms))
        self._pair(sheet, "Suites", len(run.suites), kind="int")
        self._pair(sheet, "Tables", dataset_count(run), kind="int")
        self._pair(sheet, "Rows compared", run.summary.rows_compared, kind="int")
        throughput = rows_per_second(run)
        self._pair(
            sheet,
            "Throughput (rows/s)",
            None if throughput is None else round(throughput),
            kind="int",
        )
        self._pair(sheet, "Checks", len(self.entries), kind="int")

        self._section(sheet, "Checks by status")
        for status, count in status_counts(e.check for e in self.entries).items():
            sheet.append_raw(
                [
                    book.cell(ws, status.value, tone=_tone(status)),
                    book.cell(ws, count, number_format=_INT),
                ]
            )

        self._section(sheet, "Run state")
        interrupted_tone = "warning" if run.interrupted else None
        sheet.append_raw(
            [
                book.cell(ws, "Interrupted", font=book.bold),
                book.cell(ws, _yes(run.interrupted), tone=interrupted_tone),
            ]
        )
        sheet.append_raw(
            [
                book.cell(ws, "Resumed from checkpoint", font=book.bold),
                book.cell(ws, _yes(run.resumed_from_checkpoint)),
            ]
        )
        if run.interrupted:
            sheet.note(
                "The run stopped before every table finished; resume it with 'dvf run --resume'."
            )

        baseline = run.baseline
        if baseline is not None:
            self._section(sheet, "Baseline comparison")
            self._pair(sheet, "Baseline run", baseline.baseline_run_id)
            self._pair(sheet, "Policy", baseline.policy)
            self._pair(
                sheet, "Baseline started (UTC)", baseline.baseline_started_at, kind="datetime"
            )
            for label, ids, tone in (
                ("New failures", baseline.new_failures, "failed"),
                ("Resolved", baseline.resolved, "passed"),
                ("Persisting failures", baseline.persisting, "warning"),
            ):
                sheet.append_raw(
                    [
                        book.cell(ws, label, font=book.bold),
                        book.cell(ws, len(ids), tone=tone if ids else None, number_format=_INT),
                    ]
                )

        if self.waivers:
            self._section(sheet, "Waivers")
            for waiver_status, count in waiver_counts(self.waivers).items():
                expired = waiver_status == "expired" and count > 0
                sheet.append_raw(
                    [
                        book.cell(ws, waiver_status, font=book.bold),
                        book.cell(
                            ws, count, tone="failed" if expired else None, number_format=_INT
                        ),
                    ]
                )
            if waiver_counts(self.waivers).get("expired"):
                sheet.note("Expired waivers no longer excuse failures; see the Waivers sheet.")

        self._section(sheet, "Reproducibility")
        env = run.environment
        for label, value in (
            ("dvf version", env.dvf_version),
            ("Results schema", run.schema_version),
            ("Profile", env.profile),
            ("Git SHA", env.git_sha),
            ("Config hash", env.config_hash),
            ("Host", env.hostname),
        ):
            self._pair(sheet, label, value)

        metadata = {**self.options.metadata, **run.metadata}
        if metadata:
            self._section(sheet, "Project metadata")
            for key, value in metadata.items():
                self._pair(sheet, str(key), _text(value))

        notes = self._notes()
        if notes:
            self._section(sheet, "Notes")
            for text in notes:
                sheet.note(text)

        self._top_issues(sheet)

    def _notes(self) -> list[str]:
        notes = []
        if self.omitted_passed:
            notes.append(
                f"{self.omitted_passed:,} passed checks are counted but not listed on the Checks sheet."
            )
        if self.truncated_checks:
            notes.append(
                f"The Checks sheet lists {len(self.checks):,} checks; {self.truncated_checks:,} "
                "more (the least severe outcomes) are in results.json."
            )
        return notes

    def _top_issues(self, sheet: _Sheet) -> None:
        book, ws = self.book, sheet.ws
        self._section(sheet, "Top issues")
        top = top_issues(self.entries)
        if not top:
            sheet.note("No failed, errored, or warning checks.")
            return
        sheet.append_raw(
            [
                book.cell(ws, name, font=book.header_font, fill=book.header_fill)
                for name in ("#", "Status", "Severity", "Check", "Table", "Message", "Unexpected")
            ]
        )
        for number, entry in enumerate(top, start=1):
            check = entry.check
            sheet.append_raw(
                [
                    number,
                    book.cell(ws, check.status, tone=_tone(check.status)),
                    check.severity.value,
                    book.plain(ws, check.check_id),
                    book.plain(ws, entry.location),
                    book.plain(ws, headline(check)),
                    book.cell(ws, check.unexpected_count, number_format=_INT),
                ]
            )

    # ------------------------------------------------------------------ Tables
    def tables(self) -> None:
        sheet = _Sheet(
            self.book,
            "Tables",
            [
                Column("Suite", 18),
                Column("Table", 24),
                Column("Status", 11, "status"),
                Column("Source", 32),
                Column("Target", 32),
                Column("Source rows", 13, "int"),
                Column("Target rows", 13, "int"),
                Column("Difference", 12, "int"),
                Column("Matched", 12, "int"),
                Column("Missing in target", 12, "int"),
                Column("Extra in target", 12, "int"),
                Column("Mismatched", 12, "int"),
                Column("Duplicate keys (source)", 13, "int"),
                Column("Duplicate keys (target)", 13, "int"),
                Column("Null keys (source)", 12, "int"),
                Column("Null keys (target)", 12, "int"),
                Column("Match %", 10, "pct"),
                Column("Checks", 9, "int"),
                Column("Failed", 9, "int"),
                Column("Errors", 9, "int"),
                Column("Warnings", 9, "int"),
                Column("Waived", 9, "int"),
                Column("Quality dimensions", 60),
                Column("Duration (ms)", 12, "ms"),
            ],
            freeze="C2",
        )
        for suite in self.run.suites:
            for dataset in suite.datasets:
                rec = dataset.reconciliation
                counts = status_counts(dataset.checks)
                dims = ", ".join(
                    f"{ds.dimension.value} {format_pct(ds.score)}"
                    for ds in dimension_scores(dataset.checks)
                )
                sheet.row(
                    [
                        suite.name,
                        dataset.name,
                        dataset.status,
                        dataset.source.display() if dataset.source else None,
                        dataset.target.display() if dataset.target else None,
                        dataset.row_counts.source,
                        dataset.row_counts.target,
                        dataset.row_counts.difference,
                        rec.matched if rec else None,
                        rec.missing_in_target if rec else None,
                        rec.extra_in_target if rec else None,
                        rec.mismatched if rec else None,
                        rec.duplicate_keys_source if rec else None,
                        rec.duplicate_keys_target if rec else None,
                        rec.null_keys_source if rec else None,
                        rec.null_keys_target if rec else None,
                        rec.match_pct if rec else None,
                        len(dataset.checks),
                        counts[Status.FAILED],
                        counts[Status.ERROR],
                        counts[Status.WARNING],
                        counts[Status.WAIVED],
                        dims or None,
                        dataset.duration_ms,
                    ]
                )
        sheet.filter()
        if not sheet.data_rows:
            sheet.note("No tables in this run.")

    # ------------------------------------------------------------------ Checks
    def check_sheet(self) -> None:
        sheet = _Sheet(
            self.book,
            "Checks",
            [
                Column("Check ID", 48),
                Column("Suite", 16),
                Column("Table", 20),
                Column("Name", 24),
                Column("Type", 18),
                Column("Dimension", 15),
                Column("Severity", 10),
                Column("Status", 11, "status"),
                Column("Baseline", 11, "status"),
                Column("Side", 8),
                Column("Columns", 24),
                Column("Partition", 16),
                Column("Message", 60),
                Column("Observed", 30),
                Column("Expected", 30),
                Column("Unexpected count", 13, "int"),
                Column("Total count", 13, "int"),
                Column("Unexpected %", 12, "pct"),
                Column("Waiver", 48),
                Column("Hints", 60),
                Column("Error", 40),
                Column("Artifacts", 48),
                Column("Owner", 20),
                Column("Tags", 20),
                Column("Duration (ms)", 12, "ms"),
            ],
            freeze="B2",
        )
        for entry in self.checks:
            check = entry.check
            waiver = check.waiver
            waiver_tone = None
            if waiver is not None:
                waiver_tone = "failed" if waiver.expired else "waived"
            error = check.error
            sheet.row(
                [
                    check.check_id,
                    entry.suite.name,
                    entry.dataset.name,
                    check.name,
                    check.type,
                    check.dimension.value,
                    check.severity.value,
                    check.status,
                    self.marks.get(check.check_id),
                    check.side.value if check.side else None,
                    ", ".join(check.columns) or None,
                    check.partition,
                    check.message,
                    _text(check.observed),
                    _text(check.expected),
                    check.unexpected_count,
                    check.total_count,
                    check.unexpected_pct,
                    _waiver_text(waiver),
                    _check_hints(check),
                    f"{error.type}: {error.message}" if error is not None else None,
                    _lines(f"{artifact_label(a.kind)}: {a.path}" for a in check.artifacts),
                    check.owner,
                    ", ".join(check.tags) or None,
                    check.duration_ms,
                ],
                tones={"Waiver": waiver_tone},
            )
        sheet.filter()
        if self.truncated_checks:
            sheet.note(
                f"Showing {len(self.checks):,} of {len(self.checks) + self.truncated_checks:,} "
                "listed checks (failures first); results.json has every check."
            )
        if self.omitted_passed:
            sheet.note(
                f"{self.omitted_passed:,} passed checks are not listed (include_passed is off)."
            )
        if not self.entries:
            sheet.note("No checks in this run.")

    # ------------------------------------------------------------------ Schema diff
    def schema_diff(self) -> None:
        sheet = _Sheet(
            self.book,
            "Schema diff",
            [
                Column("Suite", 16),
                Column("Table", 22),
                Column("Status", 18, "status"),
                Column("Source column", 22),
                Column("Source type", 20),
                Column("Source canonical", 22),
                Column("Target column", 22),
                Column("Target type", 20),
                Column("Target canonical", 22),
                Column("Compatibility", 20, "status"),
                Column("Reasons", 50),
                Column("Nullability change", 20),
            ],
            freeze="C2",
        )
        total = 0
        for suite in self.run.suites:
            for dataset in suite.datasets:
                for diff in dataset.schema_diff:
                    total += 1
                    if sheet.data_rows >= MAX_SCHEMA_ROWS:
                        continue
                    sheet.row(
                        [
                            suite.name,
                            dataset.name,
                            diff.status,
                            diff.source_column,
                            diff.source_native_type,
                            _text(diff.source_type),
                            diff.target_column,
                            diff.target_native_type,
                            _text(diff.target_type),
                            diff.compatibility,
                            _lines(diff.reasons),
                            diff.nullable_change,
                        ]
                    )
        sheet.filter()
        if total > sheet.data_rows:
            sheet.note(
                f"Showing {sheet.data_rows:,} of {total:,} schema rows; results.json has all of them."
            )
        if not total:
            sheet.note("No schema comparisons in this run.")

    # ------------------------------------------------------------------ Mismatches
    def mismatches(self) -> None:
        sheet = _Sheet(
            self.book,
            "Mismatches",
            [
                Column("Suite", 16),
                Column("Table", 20),
                Column("Check ID", 44),
                Column("Check status", 12, "status"),
                Column("Column", 22),
                Column("Source column", 22),
                Column("Mismatches", 12, "int"),
                Column("Mismatch %", 11, "pct"),
                Column("Top classification", 22),
                Column("Classification breakdown", 44),
                Column("Hints", 60),
                Column("Waived", 9, "yes"),
            ],
            freeze="C2",
        )
        total = 0
        for entry in self.entries:
            for mismatch in entry.check.column_mismatches:
                total += 1
                if sheet.data_rows >= MAX_MISMATCH_ROWS:
                    continue
                breakdown = ", ".join(
                    f"{name}: {count:,}"
                    for name, count in sorted_classifications(mismatch.classifications)
                )
                sheet.row(
                    [
                        entry.suite.name,
                        entry.dataset.name,
                        entry.check.check_id,
                        entry.check.status,
                        mismatch.column,
                        mismatch.source_column,
                        mismatch.mismatch_count,
                        mismatch.mismatch_pct,
                        top_classification(mismatch.classifications),
                        breakdown or None,
                        _lines(mismatch.hints),
                        mismatch.waived,
                    ]
                )
        sheet.filter()
        if total > sheet.data_rows:
            sheet.note(
                f"Showing {sheet.data_rows:,} of {total:,} column mismatches; results.json has all of them."
            )
        if not total:
            sheet.note("No column mismatches in this run.")

    # ------------------------------------------------------------------ Samples
    def _sample_sets(self) -> Iterable[tuple[CheckEntry, str | None, SampleSet]]:
        for entry in self.entries:
            check = entry.check
            if check.samples is not None:
                yield entry, None, check.samples
            for mismatch in check.column_mismatches:
                if mismatch.samples is not None:
                    yield entry, mismatch.column, mismatch.samples

    def samples(self) -> None:
        book = self.book
        sheet = _Sheet(
            book, "Samples", [Column("", 24) for _ in range(12)], freeze=None, header=False
        )
        ws = sheet.ws
        cap = max(self.options.max_samples_per_check, 0)
        sheet.note(
            "Sample rows exactly as captured in the results (masked according to the run's privacy "
            f"settings), at most {cap:,} per check or column."
        )
        written = 0
        found = False
        for entry, column, sample in self._sample_sets():
            found = True
            if written >= MAX_SAMPLE_ROWS:
                sheet.blank()
                sheet.note(
                    f"Sample listing stopped at {MAX_SAMPLE_ROWS:,} rows; the HTML and JSON reports have the rest."
                )
                break
            rows = sample.rows[: min(cap, MAX_SAMPLE_ROWS - written)]
            sheet.blank()
            where = f" · column {column}" if column else ""
            sheet.append_raw(
                [book.cell(ws, f"{entry.check.check_id}{where}", font=book.section_font)]
            )
            flags = [f"showing {len(rows):,} of {len(sample.rows):,} rows"]
            if sample.truncated:
                flags.append("more rows existed when the sample was captured")
            if sample.masked:
                flags.append("values are masked")
            sheet.note("; ".join(flags).capitalize() + ".")
            if not rows:
                continue
            sheet.append_raw(
                [
                    book.cell(ws, name, font=book.header_font, fill=book.header_fill)
                    for name in sample.columns
                ]
            )
            for row in rows:
                sheet.append_raw(
                    [
                        book.cell(ws, "NULL", font=book.null_font)
                        if value is None
                        else book.plain(ws, value)
                        for value in row
                    ]
                )
            written += len(rows)
        if not found:
            sheet.note("No sample rows in this result.")

    # ------------------------------------------------------------------ Waivers
    def waiver_sheet(self) -> None:
        book = self.book
        sheet = _Sheet(
            book,
            "Waivers",
            [
                Column("Status", 11, "status"),
                Column("Selector", 44),
                Column("Column", 18),
                Column("Reason", 50),
                Column("Ticket", 14),
                Column("Approved by", 18),
                Column("Expires", 12),
                Column("Matched checks", 10, "int"),
                Column("Check IDs", 60),
            ],
        )
        expired_fill = book.tone_fill["failed"]
        for waiver in self.waivers[:MAX_WAIVER_ROWS]:
            ids = waiver.checks[:MAX_LISTED_IDS]
            listed = "\n".join(ids)
            if len(waiver.checks) > len(ids):
                listed += f"\n… {len(waiver.checks) - len(ids):,} more"
            sheet.row(
                [
                    waiver.status,
                    waiver.selector,
                    waiver.column,
                    waiver.reason,
                    waiver.ticket,
                    waiver.approved_by,
                    waiver.expires,
                    len(waiver.checks),
                    listed or None,
                ],
                fill=expired_fill if waiver.status == "expired" else None,
            )
        sheet.filter()
        if len(self.waivers) > MAX_WAIVER_ROWS:
            sheet.note(f"Showing {MAX_WAIVER_ROWS:,} of {len(self.waivers):,} waivers.")
        if any(w.status == "expired" for w in self.waivers):
            sheet.note(
                "Expired waivers are flagged as failures: the checks they covered fail again until "
                "the waivers are renewed or removed."
            )
        if not self.waivers:
            sheet.note("No waivers matched or were configured for this run.")

    # ------------------------------------------------------------------ Trend
    def trend_sheet(self) -> None:
        sheet = _Sheet(
            self.book,
            "Trend",
            [
                Column("Run ID", 34),
                Column("Started (UTC)", 20, "datetime"),
                Column("Status", 11, "status"),
                Column("Quality score", 13, "pct"),
                Column("Checks", 10, "int"),
                Column("Failed", 10, "int"),
                Column("Errors", 10, "int"),
                Column("This run", 9, "yes"),
            ],
        )
        points = self.trend[-MAX_TREND_ROWS:]
        for point, current in points:
            sheet.row(
                [
                    point.run_id,
                    point.started_at,
                    point.status,
                    point.quality_score,
                    point.checks,
                    point.failed,
                    point.errors,
                    current,
                ]
            )
        sheet.filter()
        if len(self.trend) > len(points):
            sheet.note(f"Showing the last {len(points):,} of {len(self.trend):,} runs.")
        scored = sum(1 for point, _ in points if point.quality_score is not None)
        if scored >= 2:
            self._trend_chart(sheet, len(points))

    def _trend_chart(self, sheet: _Sheet, count: int) -> None:
        chart_module = self.book.chart
        chart = chart_module.LineChart()
        chart.title = "Quality score by run"
        chart.y_axis.title = "Quality score"
        chart.y_axis.number_format = "0%"
        chart.y_axis.scaling.min = 0
        chart.y_axis.scaling.max = 1
        chart.height = 7.5
        chart.width = 18
        data = chart_module.Reference(sheet.ws, min_col=4, min_row=1, max_row=count + 1)
        labels = chart_module.Reference(sheet.ws, min_col=1, min_row=2, max_row=count + 1)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(labels)
        chart.legend = None
        sheet.ws.add_chart(chart, "J2")

    # ------------------------------------------------------------------ Sign-off
    def sign_off(self) -> None:
        run, book = self.run, self.book
        sheet = _Sheet(
            book,
            "Sign-off",
            [
                Column("Role", 24),
                Column("Approver", 28),
                Column("Date", 14),
                Column("Decision", 16),
                Column("Comments and conditions", 70),
            ],
            freeze=None,
            header=False,
        )
        ws = sheet.ws
        sheet.append_raw([book.cell(ws, "Sign-off", font=book.title_font)])
        sheet.blank()
        self._pair(sheet, "Project", run.project)
        self._pair(sheet, "Run ID", run.run_id)
        self._pair(sheet, "Overall status", run.status, kind="status")
        self._pair(sheet, "Quality score", quality_score(run), kind="pct")
        self._pair(sheet, "Run started (UTC)", run.started_at, kind="datetime")
        for key, value in {**self.options.metadata, **run.metadata}.items():
            self._pair(sheet, str(key), _text(value))
        sheet.blank()
        sheet.append_raw(
            [
                book.cell(
                    ws,
                    "Record each reviewer's decision: Approved, Rejected, or Conditional "
                    "(list the conditions in the comments).",
                    font=book.note_font,
                )
            ]
        )
        sheet.append_raw(
            [
                book.cell(ws, name, font=book.header_font, fill=book.header_fill)
                for name in ("Role", "Approver", "Date", "Decision", "Comments and conditions")
            ]
        )
        first = sheet.rows + 1
        for role in SIGN_OFF_ROLES:
            sheet.append_raw(
                [
                    book.cell(ws, role or None, font=book.bold, border=book.border),
                    book.cell(ws, None, fill=book.input_fill, border=book.border),
                    book.cell(
                        ws, None, fill=book.input_fill, border=book.border, number_format=_DATE
                    ),
                    book.cell(ws, None, fill=book.input_fill, border=book.border),
                    book.cell(
                        ws, None, fill=book.input_fill, border=book.border, alignment=book.wrap
                    ),
                ]
            )
        last = sheet.rows
        decision = book.DataValidation(
            type="list",
            formula1='"' + ",".join(SIGN_OFF_DECISIONS) + '"',
            allow_blank=True,
            showErrorMessage=True,
            errorTitle="Decision",
            error="Choose Approved, Rejected, or Conditional.",
            promptTitle="Decision",
            prompt="Approved, Rejected, or Conditional",
            showInputMessage=True,
        )
        decision.add(f"D{first}:D{last}")
        when = book.DataValidation(
            type="date",
            operator="greaterThan",
            formula1="36526",  # 2000-01-01 as an Excel date serial
            allow_blank=True,
            showErrorMessage=True,
            errorTitle="Date",
            error="Enter a date (for example 2026-11-30).",
        )
        when.add(f"C{first}:C{last}")
        ws.data_validations.append(decision)
        ws.data_validations.append(when)


# ---------------------------------------------------------------------- reporter


def render_excel(run: RunResult, path: Path, options: ReportOptions) -> None:
    """Write the Excel workbook for ``run`` to ``path``."""
    openpyxl = _import_openpyxl()
    book = _Book(openpyxl)
    book.wb.properties.creator = "dvf"
    book.wb.properties.title = f"{report_title(run, options)} {run.run_id}"
    book.wb.properties.created = _naive_utc(run.started_at)
    _Writer(book, run, options).write()
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


class ExcelReporter(Reporter):
    """Excel workbook for business sign-off: summary, tables, checks, schema diff, mismatches,
    samples, waivers, trend, and a sign-off sheet (needs the ``excel`` extra)."""

    format: ClassVar[str] = "excel"

    def __init__(self) -> None:
        _import_openpyxl()  # fail before any report is written when the extra is missing

    def render(self, run: RunResult, out_dir: Path, options: ReportOptions) -> list[ArtifactRef]:
        """Write ``report.xlsx`` into ``out_dir``."""
        path = out_dir / EXCEL_FILE
        render_excel(run, path, options)
        return [report_artifact(path, self.format)]


__all__ = ["EXCEL_FILE", "ExcelReporter", "excel_value", "render_excel"]
