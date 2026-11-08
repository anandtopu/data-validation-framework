"""The HTML reporter (``report.html``): one self-contained, offline, interactive file.

The page is rendered with Jinja2 from the templates in ``dvf/reporting/templates``. Every section
is its own template, so ``ReportOptions.templates_dir`` can override one section (same file name)
without copying the rest. Autoescaping is always on: every value comes from user data.
"""

from __future__ import annotations

import base64
import heapq
import logging
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote

from jinja2 import (
    BaseLoader,
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    StrictUndefined,
    Template,
)

from dvf import __version__
from dvf.core.enums import Status
from dvf.models.results import (
    ArtifactRef,
    CheckResult,
    ColumnMismatch,
    ColumnProfile,
    DatasetPairResult,
    ProfileComparison,
    RunResult,
    SuiteResult,
    TrendPoint,
    WaiverStatus,
)
from dvf.plugins.base import Reporter
from dvf.reporting._common import (
    INTERRUPTED_NOTICE,
    RESUMED_NOTICE,
    STATUS_ORDER,
    STATUS_RANK,
    CheckEntry,
    DimensionScore,
    PartitionTable,
    artifact_label,
    baseline_marks,
    baseline_summary,
    dataset_count,
    details_without_partitions,
    dimension_scores,
    format_count,
    format_datetime,
    format_duration,
    format_group,
    format_pct,
    format_signed,
    format_value,
    headline,
    partition_table,
    quality_score,
    report_artifact,
    report_title,
    rows_per_second,
    sorted_classifications,
    status_counts,
    sum_timings,
    to_json,
    top_issues,
    trend_series,
    truncate,
    waiver_counts,
    waiver_statuses,
)

if TYPE_CHECKING:
    from dvf.reporting.base import ReportOptions

log = logging.getLogger(__name__)

HTML_FILE = "report.html"
TEMPLATE_NAME = "report.html.j2"
DEFAULT_ACCENT = "#1f4e79"
MAX_LOGO_BYTES = 2 * 1024 * 1024
"""Larger logos are skipped so the report stays small."""

_VALUE_LIMIT = 5000
_SHORT_LIMIT = 160
_CELL_LIMIT = 500
_SEARCH_MESSAGE_LIMIT = 300
MAX_PARTITION_ROWS = 500
"""Partition rows shown per check; the rest stay in the JSON document."""
MAX_LISTED_CHECKS = 50
"""Check IDs listed per waiver and per baseline category before '... n more'."""

_COLOR = re.compile(r"^#(?:[0-9A-Fa-f]{3}){1,2}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")

_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
}
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

PROFILE_STATS: tuple[tuple[str, str], ...] = (
    ("count", "Count"),
    ("null_count", "Nulls"),
    ("null_pct", "Null %"),
    ("distinct_count", "Distinct"),
    ("min", "Min"),
    ("max", "Max"),
    ("mean", "Mean"),
    ("min_length", "Min len"),
    ("max_length", "Max len"),
    ("avg_length", "Avg len"),
)
"""Profile statistics shown side by side, as (attribute, column heading)."""

TIMING_FIELDS: tuple[tuple[str, str], ...] = (
    ("source_query_ms", "Source query"),
    ("target_query_ms", "Target query"),
    ("transfer_ms", "Transfer"),
    ("compare_ms", "Compare"),
    ("render_ms", "Render"),
    ("total_ms", "Total"),
)


# ---------------------------------------------------------------------- view models


@dataclass(frozen=True, slots=True)
class CheckView:
    """A check prepared for the template: its anchor, search text, and visibility."""

    entry: CheckEntry
    anchor: str
    included: bool
    """False for passed checks left out of the detail list (``include_passed=False``)."""
    search: str
    baseline: str | None = None
    """``new`` or ``persisting`` when the check fails relative to the baseline run."""

    @property
    def check(self) -> CheckResult:
        return self.entry.check

    @property
    def partitions(self) -> PartitionTable | None:
        return partition_table(self.entry.check)

    @property
    def extra_details(self) -> dict[str, Any]:
        """Custom details, without ``partitions`` when it is shown as its own table."""
        return details_without_partitions(self.entry.check)

    @property
    def index(self) -> int:
        return self.entry.index

    @property
    def location(self) -> str:
        return self.entry.location

    @property
    def headline(self) -> str:
        return headline(self.entry.check)

    @property
    def rank(self) -> int:
        return STATUS_RANK[self.entry.check.status]


@dataclass(frozen=True, slots=True)
class ProfileCell:
    """One statistic of a profile comparison, formatted for both sides."""

    source: str
    target: str
    differs: bool


@dataclass(frozen=True, slots=True)
class ProfileView:
    comparison: ProfileComparison
    cells: list[ProfileCell]


@dataclass(frozen=True, slots=True)
class MismatchRow:
    """A column mismatch with the check it belongs to, for the per-table matrix."""

    view: CheckView
    mismatch: ColumnMismatch


@dataclass(frozen=True, slots=True)
class DatasetView:
    suite: SuiteResult
    dataset: DatasetPairResult
    anchor: str
    counts: list[tuple[Status, int]]
    dimensions: list[DimensionScore]
    profiles: list[ProfileView]
    mismatches: list[MismatchRow]


@dataclass(frozen=True, slots=True)
class SuiteView:
    suite: SuiteResult
    datasets: list[DatasetView]

    @property
    def check_count(self) -> int:
        return sum(len(view.dataset.checks) for view in self.datasets)


@dataclass(frozen=True, slots=True)
class Bar:
    status: Status
    count: int
    y: int
    width: float
    text_x: float


@dataclass(frozen=True, slots=True)
class Chart:
    """An inline SVG bar chart of checks by status."""

    width: int
    height: int
    label_width: int
    bars: list[Bar]


@dataclass(frozen=True, slots=True)
class SparkPoint:
    x: float
    y: float
    label: str
    current: bool


@dataclass(frozen=True, slots=True)
class Sparkline:
    """An inline SVG sparkline of the quality score over earlier runs and this one."""

    width: int
    height: int
    top: float
    bottom: float
    points: list[SparkPoint]

    @property
    def polyline(self) -> str:
        return " ".join(f"{point.x:g},{point.y:g}" for point in self.points)


@dataclass(frozen=True, slots=True)
class CheckLink:
    """A check ID, with the anchor of its detail entry when the report lists it."""

    check_id: str
    anchor: str | None


@dataclass(frozen=True, slots=True)
class LinkList:
    links: list[CheckLink]
    total: int

    @property
    def more(self) -> int:
        return self.total - len(self.links)


@dataclass(frozen=True, slots=True)
class WaiverView:
    waiver: WaiverStatus
    checks: LinkList

    @property
    def badge(self) -> str:
        """Status style for the waiver's own status: expired waivers look like failures."""
        return _WAIVER_BADGES.get(self.waiver.status, "skipped")


@dataclass(frozen=True, slots=True)
class BaselineView:
    text: str
    new_failures: LinkList
    resolved: LinkList
    persisting: LinkList


@dataclass(frozen=True, slots=True)
class Notice:
    kind: str
    label: str
    text: str


_WAIVER_BADGES = {"applied": "waived", "expired": "failed", "unused": "skipped"}


# ---------------------------------------------------------------------- helpers


def accent_color(value: str | None) -> str:
    """The accent color if it is a valid ``#RGB`` or ``#RRGGBB`` color, else the default.

    Validation matters: the value is placed inside a ``<style>`` block, where HTML escaping
    does not protect against CSS injection.
    """
    if value and _COLOR.match(value):
        return value
    if value:
        log.warning("Ignoring invalid primary_color %r; expected #RRGGBB.", value)
    return DEFAULT_ACCENT


def text_color_on(color: str) -> str:
    """Black or white, whichever has more contrast on ``color`` (WCAG relative luminance)."""
    digits = color[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    channels = [int(digits[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    on_white = 1.05 / (luminance + 0.05)
    on_black = (luminance + 0.05) / 0.05
    return "#ffffff" if on_white >= on_black else "#111827"


def _sniff_image(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return None


def logo_data_uri(path: Path | None) -> str | None:
    """The logo as a base64 ``data:`` URI, or None when it is missing, too large, or unknown."""
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        log.debug("Report logo %s could not be read; rendering without it.", path)
        return None
    if len(data) > MAX_LOGO_BYTES:
        log.warning("Report logo %s is larger than %d bytes; skipped.", path, MAX_LOGO_BYTES)
        return None
    mime = _IMAGE_TYPES.get(path.suffix.lower()) or _sniff_image(data)
    if mime is None:
        log.warning("Report logo %s is not a recognized image type; skipped.", path)
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def artifact_href(path: str) -> str | None:
    """A link target for an artifact path, or None when the path should not be linked.

    Relative paths stay relative, absolute paths become ``file:`` URIs, and anything with
    another scheme (``javascript:``, ``http:``, ...) is shown as text only.
    """
    if not path:
        return None
    if _WINDOWS_ABSOLUTE.match(path):
        posix = path.replace("\\", "/")
        if posix.startswith("//"):  # UNC path: //server/share/file
            return "file:" + quote(posix, safe="/._-~")
        return "file:///" + quote(posix, safe="/:._-~")
    if _SCHEME.match(path):
        return None
    if path.startswith("/"):
        return "file://" + quote(path, safe="/._-~")
    return quote(path.replace("\\", "/"), safe="/._-~")


def _is_structured(value: Any) -> bool:
    return isinstance(value, Mapping | list | tuple)


def _fmt(value: Any, limit: int | None = _VALUE_LIMIT) -> str:
    return format_value(value, limit=limit)


def _short(value: Any) -> str:
    return format_value(value, limit=_SHORT_LIMIT)


def _cell(value: Any) -> str:
    return format_value(value, limit=_CELL_LIMIT, none="NULL")


def _pretty(value: Any, limit: int | None = _VALUE_LIMIT) -> str:
    return truncate(to_json(value, indent=2), limit)


def _search_text(entry: CheckEntry) -> str:
    check = entry.check
    parts = [
        check.check_id,
        check.name,
        check.type,
        entry.suite.name,
        entry.dataset.name,
        check.dimension.value,
        check.severity.value,
        check.status.value,
        check.owner or "",
        check.side.value if check.side else "",
        " ".join(check.tags),
        " ".join(check.columns),
        truncate(headline(check), _SEARCH_MESSAGE_LIMIT),
    ]
    return " ".join(part for part in parts if part)


def _profile_value(profile: ColumnProfile | None, attribute: str) -> Any:
    return None if profile is None else getattr(profile, attribute)


def _profile_view(comparison: ProfileComparison) -> ProfileView:
    cells = []
    for attribute, _label in PROFILE_STATS:
        source = _profile_value(comparison.source, attribute)
        target = _profile_value(comparison.target, attribute)
        cells.append(
            ProfileCell(
                source=_cell(source) if comparison.source is not None else "",
                target=_cell(target) if comparison.target is not None else "",
                differs=source != target,
            )
        )
    return ProfileView(comparison, cells)


def _chart(counts: dict[Status, int]) -> Chart:
    label_width, bar_area, row_height = 78, 300, 26
    peak = max(counts.values(), default=0) or 1
    bars = []
    for row, (status, count) in enumerate(counts.items()):
        width = round(bar_area * count / peak, 1)
        bars.append(Bar(status, count, row * row_height, width, label_width + width + 6))
    return Chart(label_width + bar_area + 60, row_height * len(bars) + 4, label_width, bars)


def _sparkline(series: list[tuple[TrendPoint, bool]]) -> Sparkline | None:
    """Quality score from 0 to 100 on a fixed scale, one point per run; None without scores."""
    width, height, pad = 320, 64, 6
    top, bottom = float(pad), float(height - pad)
    step = (width - 2 * pad) / max(len(series) - 1, 1)
    points = []
    for position, (point, current) in enumerate(series):
        if point.quality_score is None:
            continue
        score = min(max(point.quality_score, 0.0), 100.0)
        x = width / 2 if len(series) == 1 else pad + position * step
        y = top + (100.0 - score) / 100.0 * (bottom - top)
        label = f"{point.run_id}: {format_pct(point.quality_score)} ({point.status.value})"
        if current:
            label += ", this run"
        points.append(SparkPoint(round(x, 1), round(y, 1), label, current))
    if not points:
        return None
    return Sparkline(width, height, top, bottom, points)


def _link_list(check_ids: list[str], anchors: Mapping[str, str]) -> LinkList:
    shown = check_ids[:MAX_LISTED_CHECKS]
    return LinkList([CheckLink(cid, anchors.get(cid)) for cid in shown], len(check_ids))


def _notices(run: RunResult) -> list[Notice]:
    notices = []
    if run.interrupted:
        notices.append(Notice("warning", "Interrupted run.", INTERRUPTED_NOTICE))
    if run.resumed_from_checkpoint:
        notices.append(Notice("info", "Resumed run.", RESUMED_NOTICE))
    return notices


def _history_context(run: RunResult, views: list[CheckView]) -> dict[str, Any]:
    """Baseline, waiver, and trend sections."""
    anchors: dict[str, str] = {}
    for view in views:
        if view.included:
            anchors.setdefault(view.check.check_id, view.anchor)
    baseline = run.baseline
    baseline_view = None
    if baseline is not None:
        baseline_view = BaselineView(
            text=baseline_summary(baseline),
            new_failures=_link_list(baseline.new_failures, anchors),
            resolved=_link_list(baseline.resolved, anchors),
            persisting=_link_list(baseline.persisting, anchors),
        )
    waivers = waiver_statuses(run)
    series = trend_series(run)
    return {
        "notices": _notices(run),
        "baseline": baseline,
        "baseline_view": baseline_view,
        "waivers": [WaiverView(w, _link_list(w.checks, anchors)) for w in waivers],
        "waiver_counts": waiver_counts(waivers),
        "trend": series,
        "sparkline": _sparkline(series),
    }


# ---------------------------------------------------------------------- context


def build_context(run: RunResult, options: ReportOptions) -> dict[str, Any]:
    """Everything the templates need, computed in one pass over the checks."""
    views: list[CheckView] = []
    suites: list[SuiteView] = []
    marks = baseline_marks(run)
    table_number = 0
    for suite in run.suites:
        dataset_views: list[DatasetView] = []
        for dataset in suite.datasets:
            table_number += 1
            own: list[CheckView] = []
            for check in dataset.checks:
                entry = CheckEntry(len(views) + len(own) + 1, suite, dataset, check)
                included = options.include_passed or check.status is not Status.PASSED
                own.append(
                    CheckView(
                        entry,
                        f"c{entry.index}",
                        included,
                        _search_text(entry),
                        marks.get(check.check_id),
                    )
                )
            views.extend(own)
            dataset_views.append(
                DatasetView(
                    suite=suite,
                    dataset=dataset,
                    anchor=f"t{table_number}",
                    counts=[
                        (status, n) for status, n in status_counts(dataset.checks).items() if n
                    ],
                    dimensions=dimension_scores(dataset.checks),
                    profiles=[_profile_view(p) for p in dataset.profiles],
                    mismatches=[
                        MismatchRow(view, mismatch)
                        for view in own
                        for mismatch in view.check.column_mismatches
                    ],
                )
            )
        suites.append(SuiteView(suite, dataset_views))

    all_checks = [view.check for view in views]
    counts = status_counts(all_checks)
    shown = [view for view in views if view.included]
    by_index = {view.index: view for view in views}
    top = [by_index[entry.index] for entry in top_issues(view.entry for view in views)]
    timed = (view for view in views if view.check.duration_ms is not None)
    slowest = heapq.nlargest(10, timed, key=lambda v: v.check.duration_ms or 0.0)
    accent = accent_color(options.primary_color)
    return {
        "run": run,
        "env": run.environment,
        "options": options,
        "title": report_title(run, options),
        "accent": accent,
        "accent_text": text_color_on(accent),
        "logo": logo_data_uri(options.logo),
        "header_meta": [
            (str(key), value) for key, value in {**options.metadata, **run.metadata}.items()
        ],
        "statuses": STATUS_ORDER,
        "counts": counts,
        "total_checks": len(views),
        "quality_score": quality_score(run),
        "suite_count": len(run.suites),
        "dataset_count": dataset_count(run),
        "rows_compared": run.summary.rows_compared,
        "throughput": rows_per_second(run),
        "chart": _chart(counts),
        "top_issues": top,
        "suites": suites,
        "checks": shown,
        "detail_counts": status_counts(view.check for view in shown),
        "omitted_passed": len(views) - len(shown),
        "max_samples": options.max_samples_per_check,
        "extra_sections": options.extra_sections,
        "timing": sum_timings(all_checks),
        "timing_fields": TIMING_FIELDS,
        "profile_stats": PROFILE_STATS,
        "slowest": slowest,
        "cli": shlex.join(run.environment.cli_args),
        "config_json": to_json(run.config_snapshot, indent=2),
        "generator": f"dvf {__version__}",
        "max_partitions": MAX_PARTITION_ROWS,
        **_history_context(run, views),
    }


def create_environment(options: ReportOptions) -> Environment:
    """A Jinja2 environment that prefers ``options.templates_dir`` over the packaged templates."""
    loaders: list[BaseLoader] = []
    if options.templates_dir is not None:
        loaders.append(FileSystemLoader(str(options.templates_dir)))
    loaders.append(PackageLoader("dvf.reporting", "templates"))
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters.update(
        fmt=_fmt,
        short=_short,
        cell=_cell,
        pretty=_pretty,
        count=format_count,
        signed=format_signed,
        pct=format_pct,
        duration=format_duration,
        dt=format_datetime,
        group=format_group,
        classes=sorted_classifications,
        href=artifact_href,
        artifact_label=artifact_label,
    )
    env.tests["structured"] = _is_structured
    return env


def _template(options: ReportOptions) -> Template:
    return create_environment(options).get_template(TEMPLATE_NAME)


def render_html(run: RunResult, options: ReportOptions) -> str:
    """The HTML report as text."""
    return _template(options).render(build_context(run, options))


class HtmlReporter(Reporter):
    """Self-contained interactive HTML report for engineers and QA (no external resources)."""

    format: ClassVar[str] = "html"

    def render(self, run: RunResult, out_dir: Path, options: ReportOptions) -> list[ArtifactRef]:
        """Write ``report.html`` into ``out_dir``, streaming the template output to disk."""
        template = _template(options)
        context = build_context(run, options)
        path = out_dir / HTML_FILE
        out_dir.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for chunk in template.generate(context):
                handle.write(chunk)
        return [report_artifact(path, self.format)]
