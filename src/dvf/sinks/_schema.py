"""The tables result sinks write, and the rows for one run.

Three tables, each named ``<table_prefix><name>``:

``runs``
    One row per run: status, timing, quality score, counts per status, and reproducibility keys.
``checks``
    One row per check result. ``check_index`` is the check's position in the run (from 1), which
    keeps rows distinct when one check reports several partitions.
``column_mismatches``
    One row per column mismatch of a comparison, with its most frequent mismatch class.

Rows hold plain Python values (text, integers, floats, booleans, timezone-aware UTC datetimes,
and None). Each sink adapts them to its database. Only summary fields are written: no samples,
SQL text, or configuration.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from dvf.core.enums import Status
from dvf.models.results import CheckResult, RunResult

MAX_TEXT = 8_000
"""Characters kept per text value (messages can be long)."""


class ColumnType(StrEnum):
    TEXT = "text"
    BIGINT = "bigint"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"
    """UTC timestamp."""


@dataclass(frozen=True, slots=True)
class SinkColumn:
    name: str
    type: ColumnType


@dataclass(frozen=True, slots=True)
class SinkTable:
    name: str
    """Table name without the prefix."""
    columns: tuple[SinkColumn, ...]
    primary_key: tuple[str, ...] = ()

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]


def _cols(*specs: tuple[str, ColumnType]) -> tuple[SinkColumn, ...]:
    return tuple(SinkColumn(name, kind) for name, kind in specs)


_T, _I, _D, _B, _TS = (
    ColumnType.TEXT,
    ColumnType.BIGINT,
    ColumnType.DOUBLE,
    ColumnType.BOOLEAN,
    ColumnType.TIMESTAMP,
)

COUNTED_STATUSES: tuple[Status, ...] = (
    Status.PASSED,
    Status.WARNING,
    Status.FAILED,
    Status.ERROR,
    Status.SKIPPED,
    Status.WAIVED,
)

RUNS = SinkTable(
    "runs",
    _cols(
        ("run_id", _T),
        ("project", _T),
        ("status", _T),
        ("started_at", _TS),
        ("ended_at", _TS),
        ("duration_ms", _D),
        ("quality_score", _D),
        ("checks_total", _I),
        *((f"checks_{status.value}", _I) for status in COUNTED_STATUSES),
        ("rows_compared", _I),
        ("profile", _T),
        ("git_sha", _T),
        ("config_hash", _T),
        ("dvf_version", _T),
        ("schema_version", _T),
        ("interrupted", _B),
        ("resumed_from_checkpoint", _B),
        ("baseline_run_id", _T),
        ("new_failures", _I),
    ),
    primary_key=("run_id",),
)

CHECKS = SinkTable(
    "checks",
    _cols(
        ("run_id", _T),
        ("check_index", _I),
        ("check_id", _T),
        ("suite", _T),
        ("dataset", _T),
        ("name", _T),
        ("type", _T),
        ("dimension", _T),
        ("severity", _T),
        ("status", _T),
        ("side", _T),
        ("columns", _T),
        ("message", _T),
        ("unexpected_count", _I),
        ("total_count", _I),
        ("unexpected_pct", _D),
        ("duration_ms", _D),
        ("waived", _B),
        ("waiver_expired", _B),
        ("partition", _T),
        ("started_at", _TS),
    ),
    primary_key=("run_id", "check_index"),
)

COLUMN_MISMATCHES = SinkTable(
    "column_mismatches",
    _cols(
        ("run_id", _T),
        ("check_index", _I),
        ("check_id", _T),
        ("column", _T),
        ("source_column", _T),
        ("mismatch_count", _I),
        ("mismatch_pct", _D),
        ("top_classification", _T),
        ("waived", _B),
    ),
)

TABLES: tuple[SinkTable, ...] = (RUNS, CHECKS, COLUMN_MISMATCHES)
"""Every sink table, in write order."""


# ---------------------------------------------------------------------- values


def clean(value: Any) -> Any:
    """Normalize one value: enums to text, NUL characters removed, long text cut, NaN to None."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        text = value.replace("\x00", "")
        return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return value


def _row(*values: Any) -> tuple[Any, ...]:
    return tuple(clean(value) for value in values)


# ---------------------------------------------------------------------- rows


def _quality_score(run: RunResult) -> float | None:
    if run.summary.quality_score is not None:
        return run.summary.quality_score
    return run.summarize().quality_score


def run_row(run: RunResult) -> tuple[Any, ...]:
    """The ``runs`` row for ``run``, in :data:`RUNS` column order."""
    counts = Counter(check.status for check in run.iter_checks())
    env = run.environment
    baseline = run.baseline
    return _row(
        run.run_id,
        run.project,
        run.status,
        run.started_at,
        run.ended_at,
        run.duration_ms,
        _quality_score(run),
        sum(counts.values()),
        *(counts[status] for status in COUNTED_STATUSES),
        run.summary.rows_compared,
        env.profile,
        env.git_sha,
        env.config_hash,
        env.dvf_version,
        run.schema_version,
        run.interrupted,
        run.resumed_from_checkpoint,
        baseline.baseline_run_id if baseline else None,
        len(baseline.new_failures) if baseline else None,
    )


def _check_row(
    run_id: str, index: int, suite: str, dataset: str, check: CheckResult
) -> tuple[Any, ...]:
    return _row(
        run_id,
        index,
        check.check_id,
        suite,
        dataset,
        check.name,
        check.type,
        check.dimension,
        check.severity,
        check.status,
        check.side,
        ", ".join(check.columns) or None,
        check.message,
        check.unexpected_count,
        check.total_count,
        check.unexpected_pct,
        check.duration_ms,
        check.status is Status.WAIVED,
        check.waiver is not None and check.waiver.expired,
        check.partition,
        check.started_at,
    )


def check_rows(run: RunResult) -> Iterator[tuple[Any, ...]]:
    """One ``checks`` row per check, in run order."""
    index = 0
    for suite in run.suites:
        for dataset in suite.datasets:
            for check in dataset.checks:
                index += 1
                yield _check_row(run.run_id, index, suite.name, dataset.name, check)


def mismatch_rows(run: RunResult) -> Iterator[tuple[Any, ...]]:
    """One ``column_mismatches`` row per column mismatch, in run order."""
    for index, check in enumerate(run.iter_checks(), start=1):
        for mismatch in check.column_mismatches:
            ranked = sorted(mismatch.classifications.items(), key=lambda item: (-item[1], item[0]))
            yield _row(
                run.run_id,
                index,
                check.check_id,
                mismatch.column,
                mismatch.source_column,
                mismatch.mismatch_count,
                mismatch.mismatch_pct,
                ranked[0][0] if ranked else None,
                mismatch.waived,
            )


def table_rows(run: RunResult, table: SinkTable) -> Iterator[tuple[Any, ...]]:
    """The rows of ``table`` for ``run``."""
    if table is RUNS:
        yield run_row(run)
    elif table is CHECKS:
        yield from check_rows(run)
    elif table is COLUMN_MISMATCHES:
        yield from mismatch_rows(run)
    else:  # pragma: no cover - TABLES is closed
        raise ValueError(f"unknown sink table {table.name}")
