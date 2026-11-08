"""``dvf suggest``: candidate checks derived from column profiles.

Every suggestion is a plain check configuration with a comment giving its evidence, written as
a YAML fragment shaped like the configuration (``suites[].tables[].checks``) so the checks can be
copied into the matching tables. Suggestions describe the data as it is today: review them
before adopting them as rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dvf.core.errors import ConfigError
from dvf.core.types import LogicalType
from dvf.discovery.common import generated_header, write_output
from dvf.discovery.patterns import find_format
from dvf.discovery.profiling import ColumnProfileEntry, ProfileReport, TableProfile, is_masked
from dvf.discovery.yamlwriter import Document, Flow, Map, Seq, dump

_NUMERIC = (LogicalType.INTEGER.value, LogicalType.DECIMAL.value, LogicalType.FLOAT.value)
_TEMPORAL = (LogicalType.DATE.value, LogicalType.TIMESTAMP.value, LogicalType.TIMESTAMP_TZ.value)
_TIMESTAMPS = (LogicalType.TIMESTAMP.value, LogicalType.TIMESTAMP_TZ.value)
_UNIQUE_TYPES = (
    LogicalType.INTEGER.value,
    LogicalType.STRING.value,
    LogicalType.UUID.value,
    LogicalType.DECIMAL.value,
)
_CATEGORY_TYPES = (LogicalType.STRING.value, LogicalType.INTEGER.value)


@dataclass
class SuggestOptions:
    """How suggestions are derived."""

    margin: float = 0.0
    """Widen ranges and length bounds by this share of the observed span (0.1 = 10%)."""
    max_values: int = 10
    """Suggest ``allowed_values`` for columns with at most this many distinct values."""
    freshness: bool = True
    """Suggest a ``freshness`` check on the most recent timestamp column of each table."""
    now: datetime | None = None
    """The current time for freshness ages (default: now, in UTC)."""


@dataclass
class Suggestion:
    """One suggested check and why it was suggested."""

    suite: str
    table: str
    side: str
    check: dict[str, Any]
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "table": self.table,
            "side": self.side,
            "check": self.check,
            "evidence": self.evidence,
        }


@dataclass
class SuggestResult:
    """Suggested checks, as a YAML fragment and as a list."""

    suggestions: list[Suggestion]
    document: Document
    skipped: list[str] = field(default_factory=list)
    """Tables that could not be profiled, with the reason."""
    output: Path | None = None

    @property
    def text(self) -> str:
        return dump(self.document)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output) if self.output else None,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "skipped": self.skipped,
        }


# ---------------------------------------------------------------------- values


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or is_masked(value):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _plain_number(value: Decimal, integer: bool) -> int | float:
    if integer or value == value.to_integral_value():
        return int(value)
    return float(value)


def _widen(
    low: Decimal, high: Decimal, margin: float, integer: bool
) -> tuple[int | float, int | float]:
    span = high - low
    pad = Decimal(str(margin)) * (span if span != 0 else max(abs(low), abs(high)))
    lo, hi = low - pad, high + pad
    if integer:
        return math.floor(lo), math.ceil(hi)
    return _plain_number(lo, False), _plain_number(hi, False)


def _parse_temporal(value: Any) -> datetime | None:
    if not isinstance(value, str) or is_masked(value):
        return None
    text = value.strip().replace("T", " ").removesuffix("Z")
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), datetime.min.time())
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _temporal_text(moment: datetime, logical: str) -> str:
    if logical == LogicalType.DATE.value:
        return moment.date().isoformat()
    return moment.replace(microsecond=0).isoformat(sep=" ")


def _age_text(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.1f}{unit}"
    return f"{seconds:.0f}s"


def _max_age(seconds: float, margin: float) -> str:
    wanted = seconds * (1 + margin)
    if wanted <= 86400:
        return f"{max(1, math.ceil(wanted / 3600))}h"
    return f"{math.ceil(wanted / 86400)}d"


# ---------------------------------------------------------------------- rules


class _TableSuggester:
    def __init__(self, table: TableProfile, options: SuggestOptions) -> None:
        self.table = table
        self.options = options
        self.items: list[tuple[dict[str, Any], str]] = []

    def add(self, check: dict[str, Any], evidence: str) -> None:
        self.items.append((check, evidence))

    def run(self) -> list[tuple[dict[str, Any], str]]:
        table = self.table
        rows = table.row_count or 0
        if rows == 0:
            return []
        complete = [c.name for c in table.columns if c.null_count == 0]
        if complete:
            self.add({"type": "not_null", "columns": complete}, f"no NULLs in {rows:,} rows")
        for column in table.columns:
            self.column(column, rows)
        if self.options.freshness:
            self.freshness()
        return self.items

    def column(self, column: ColumnProfileEntry, rows: int) -> None:
        non_null = column.non_null
        if non_null == 0:
            return
        unique = column.all_distinct and non_null >= 2 and column.logical in _UNIQUE_TYPES
        if unique:
            self.add(
                {"type": "unique", "columns": [column.name]},
                f"{column.distinct_count:,} distinct values in {non_null:,} non-NULL rows",
            )
        categories = self.allowed_values(column)
        if column.logical in _NUMERIC and not (column.all_distinct and column.logical == "INTEGER"):
            self.numeric_range(column)
        elif column.logical in _TEMPORAL:
            self.temporal_range(column)
        if column.logical == LogicalType.STRING.value and not categories:
            fmt_name = column.format_of_all()
            if fmt_name is not None:
                fmt = find_format(fmt_name)
                assert fmt is not None
                self.add(
                    {"type": "regex", "column": column.name, "pattern": fmt.pattern},
                    f"all {non_null:,} non-NULL values look like a {fmt.description}",
                )
            else:
                self.length(column)

    def allowed_values(self, column: ColumnProfileEntry) -> bool:
        distinct = column.distinct_count
        if (
            column.logical not in _CATEGORY_TYPES
            or distinct is None
            or distinct == 0
            or distinct > self.options.max_values
            or distinct * 2 > column.non_null
            or len(column.top_values) != distinct
            or any(is_masked(v) for v, _ in column.top_values)
        ):
            return False
        values = [v for v, _ in column.top_values]
        shown = ", ".join(f"{v} ({n:,})" for v, n in column.top_values)
        self.add(
            {"type": "allowed_values", "column": column.name, "values": values},
            f"{distinct} distinct values in {column.non_null:,} non-NULL rows: {shown}",
        )
        return True

    def numeric_range(self, column: ColumnProfileEntry) -> None:
        low, high = _decimal(column.min), _decimal(column.max)
        if low is None or high is None:
            return
        integer = column.logical == LogicalType.INTEGER.value
        lo, hi = _widen(low, high, self.options.margin, integer)
        margin = f", widened by {self.options.margin:.0%}" if self.options.margin else ""
        self.add(
            {"type": "range", "column": column.name, "min": lo, "max": hi},
            f"observed minimum {column.min} and maximum {column.max}{margin}",
        )

    def temporal_range(self, column: ColumnProfileEntry) -> None:
        low, high = _parse_temporal(column.min), _parse_temporal(column.max)
        if low is None or high is None:
            return
        pad = timedelta(seconds=(high - low).total_seconds() * self.options.margin)
        bound = low - pad
        if column.logical == LogicalType.DATE.value:
            bound = datetime.combine(bound.date(), datetime.min.time())
        margin = f", widened by {self.options.margin:.0%}" if self.options.margin else ""
        self.add(
            {"type": "range", "column": column.name, "min": _temporal_text(bound, column.logical)},
            f"observed minimum {column.min}{margin}; no maximum, as new data keeps arriving",
        )

    def length(self, column: ColumnProfileEntry) -> None:
        low, high = column.min_length, column.max_length
        if high is None or high <= 0 or low is None:
            return
        margin = self.options.margin
        check: dict[str, Any] = {"type": "length", "column": column.name}
        lo = math.floor(low * (1 - margin))
        if lo > 0:
            check["min"] = lo
        check["max"] = max(1, math.ceil(high * (1 + margin)))
        note = f", widened by {margin:.0%}" if margin else ""
        self.add(check, f"observed lengths {low} to {high}{note}")

    def freshness(self) -> None:
        now = (self.options.now or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
        newest: tuple[datetime, ColumnProfileEntry] | None = None
        for column in self.table.columns:
            if column.logical not in _TIMESTAMPS:
                continue
            moment = _parse_temporal(column.max)
            if moment is None or moment > now:  # future values (planned dates) say nothing
                continue
            if newest is None or moment > newest[0]:
                newest = (moment, column)
        if newest is None:
            return
        moment, column = newest
        age = (now - moment).total_seconds()
        self.add(
            {
                "type": "freshness",
                "column": column.name,
                "max_age": _max_age(age, self.options.margin),
                "severity": "warning",
            },
            f"newest {column.name} {column.max} is {_age_text(age)} old",
        )


# ---------------------------------------------------------------------- document


def suggest_from_profiles(
    report: ProfileReport, options: SuggestOptions | None = None
) -> SuggestResult:
    """Suggest checks from a :class:`ProfileReport`."""
    options = options or SuggestOptions()
    if options.margin < 0:
        raise ConfigError("--margin must not be negative.")
    suggestions: list[Suggestion] = []
    skipped: list[str] = []
    sides_per_table: dict[tuple[str, str], set[str]] = {}
    for table in report.tables:
        sides_per_table.setdefault((table.suite, table.table), set()).add(table.side)
    for table in report.tables:
        if table.error:
            skipped.append(f"{table.suite}.{table.table} ({table.side}): {table.error}")
            continue
        both = len(sides_per_table[(table.suite, table.table)]) > 1
        default_side = "target" if table.is_comparison else "source"
        for check, evidence in _TableSuggester(table, options).run():
            if table.side != default_side:
                check["side"] = table.side
            if both:
                check["name"] = table.side
            suggestions.append(Suggestion(table.suite, table.table, table.side, check, evidence))
    document = _document(report, suggestions, skipped, options)
    return SuggestResult(suggestions=suggestions, document=document, skipped=skipped)


def _document(
    report: ProfileReport,
    suggestions: list[Suggestion],
    skipped: list[str],
    options: SuggestOptions,
) -> Document:
    suites = Seq()
    by_suite: dict[str, dict[str, list[Suggestion]]] = {}
    for item in suggestions:
        by_suite.setdefault(item.suite, {}).setdefault(item.table, []).append(item)
    for suite_name, tables in by_suite.items():
        table_seq = Seq()
        for table_name, items in tables.items():
            checks = Seq()
            for item in items:
                checks.add(Flow(item.check), comment=f"evidence ({item.side}): {item.evidence}")
            table_seq.add(Map().add("name", table_name).add("checks", checks))
        suites.add(Map().add("name", suite_name).add("tables", table_seq))
    root = Map().add("suites", suites if len(suites) else [])
    header = generated_header(
        "dvf suggest",
        f"Candidate checks for project '{report.project}', from profiles taken {report.generated_at}.",
        "They describe the data as it is today. Review each one, then copy the checks you",
        "want into the matching tables of your configuration and run 'dvf validate-config'.",
    )
    if options.margin:
        header.append(
            f"Ranges and lengths are widened by {options.margin:.0%} of the observed span."
        )
    footer = [f"Not profiled: {s}" for s in skipped]
    if not suggestions:
        footer.append("No checks could be suggested from these profiles.")
    return Document(root, header=header, footer=footer)


def suggest(
    report: ProfileReport,
    options: SuggestOptions | None = None,
    *,
    output: Path | None = None,
    overwrite: bool = False,
) -> SuggestResult:
    """Suggest checks from ``report`` and, with ``output``, write the YAML fragment there."""
    result = suggest_from_profiles(report, options)
    if output is not None:
        write_output(result.text, output, overwrite=overwrite)
        result.output = output
    return result


__all__ = ["SuggestOptions", "SuggestResult", "Suggestion", "suggest", "suggest_from_profiles"]
