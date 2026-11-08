"""Unit tests for Phase 2 building blocks: grids, partitions, waivers, retries, checkpoints,
history, merging, and log context."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from dvf.compare.buckets import CellStats, HashGrid, KeyGrid, _in_lists, cells_match
from dvf.config.models import PartitionConfig, WaiverConfig
from dvf.core.enums import Dimension, Severity, Side, Status
from dvf.core.errors import ConfigError, ConnectorError
from dvf.engines.base import DiffResult, MismatchRow, merge_results
from dvf.execution.checkpoint import (
    CheckpointStore,
    PartitionStore,
    _safe,
    load_manifest,
    load_results,
    prune_runs,
    write_atomic,
)
from dvf.execution.context import QueryRecorder, RetryPolicy
from dvf.execution.history import HistoryStore, parse_store
from dvf.execution.selectors import Selectors
from dvf.execution.waivers import WaiverBook
from dvf.models.results import CheckResult, ColumnMismatch, ReconciliationSummary
from dvf.observability.logging import configure_logging, get_logger, log_context
from dvf.plugins.base import Connector
from dvf.sql.dialects import HASH_SPACE, get_dialect

# ====================================================================== grids


def test_hash_grid_levels_nest_and_stay_in_bigint() -> None:
    grid = HashGrid(256, 16)
    assert grid.granularities[0] == 256
    assert all(
        b == a * 16 for a, b in zip(grid.granularities, grid.granularities[1:], strict=False)
    )
    assert grid.granularities[-1] * (HASH_SPACE - 1) < 2**63
    duck = get_dialect("duckdb")
    assert grid.group(duck, 0, "x") == "(((x) * 256) // 4294967296)"
    assert "IN (1, 5)" in grid.restrict(duck, 1, [5, 1], "x")
    assert grid.describe(1) == "4,096 hash buckets"


def test_hash_buckets_nest_exactly() -> None:
    for x in (0, 1, 123456789, HASH_SPACE - 1):
        for g in (256, 4096):
            assert (x * g * 16 // HASH_SPACE) // 16 == x * g // HASH_SPACE


def test_key_grid_widths_and_ranges() -> None:
    grid = KeyGrid(100, 10_099, buckets=16, fanout=4)
    assert grid.widths[-1] == 1 and all(
        a == b * 4 for a, b in zip(grid.widths, grid.widths[1:], strict=False)
    )
    assert -(-10_000 // grid.widths[0]) <= 16
    level = len(grid.widths) - 2
    assert grid.ranges(level, [0, 1, 3]) == [
        (100, 100 + 2 * grid.widths[level]),
        (100 + 3 * grid.widths[level], 100 + 4 * grid.widths[level]),
    ]
    duck = get_dialect("duckdb")
    assert "v >= 100" in grid.restrict(duck, level, [0], "v")
    assert grid.group(duck, len(grid.widths) - 1, "v") == "(v - 100)"
    assert grid.describe(len(grid.widths) - 1) == "single keys"
    many = list(range(0, 4000, 2))
    assert " IN (" in grid.restrict(duck, len(grid.widths) - 1, many, "v")
    zero = KeyGrid(0, 0, 1, 2)
    assert zero.group(duck, 0, "v") == "v"


def test_in_lists_chunk() -> None:
    sql = _in_lists("g", list(range(2500)))
    assert sql.count(" IN (") == 3


def test_cells_match_uses_float_tolerance() -> None:
    from dvf.compare.plan import ComparisonPlan

    plan = ComparisonPlan(
        keys=[],
        columns=[],
        source_dialect=get_dialect("duckdb"),
        target_dialect=get_dialect("duckdb"),
    )
    a = CellStats(rows=3, keys=3, s1=10, s2=20)
    assert cells_match(plan, a, CellStats(rows=3, keys=3, s1=10, s2=20))
    assert not cells_match(plan, a, CellStats(rows=3, keys=2, s1=10, s2=20))
    assert not cells_match(plan, a, None)


# ====================================================================== merging and serialization


def test_merge_results_and_round_trip() -> None:
    first = DiffResult(
        engine="duckdb",
        source_rows=3,
        target_rows=3,
        summary=ReconciliationSummary(matched=2, mismatched=1),
        column_counts={"a": 1},
        mismatches=[MismatchRow([1], "a", "x", "y", "x", "y")],
        key_samples={"missing_in_target": [[1, 1]]},
        notes=["note"],
    )
    second = DiffResult.from_dict(json.loads(json.dumps(first.to_dict())))
    assert second.summary == first.summary and second.mismatches == first.mismatches
    merged = merge_results([first, second], "duckdb", sample_size=1, cap=1)
    assert merged.summary.matched == 4 and merged.column_counts == {"a": 2}
    assert len(merged.mismatches) == 1 and merged.key_samples["missing_in_target"] == [[1, 1]]
    assert merged.notes == ["note"] and merged.source_rows == 6
    assert merge_results([], "x", 1, 1).summary == ReconciliationSummary()


# ====================================================================== partitions config


def test_partition_config_requires_one_kind() -> None:
    with pytest.raises(ValueError, match="only one of"):
        PartitionConfig(values="auto", hash_buckets=2)
    with pytest.raises(ValueError, match="need 'column'"):
        PartitionConfig(ranges=[1, 2])
    assert PartitionConfig(hash_buckets=4).max_partitions == 1000


# ====================================================================== waivers


def _result(status: Status = Status.FAILED, **kwargs: Any) -> CheckResult:
    return CheckResult(
        check_id="s.t.compare.column_mismatch",
        name="x",
        type="compare.column_mismatch",
        dimension=Dimension.RECONCILIATION,
        severity=Severity.ERROR,
        status=status,
        **kwargs,
    )


def test_waiver_book_cases() -> None:
    today = date(2026, 6, 1)
    book = WaiverBook(
        [
            WaiverConfig(check="s.t.*", column="name", reason="r1"),
            WaiverConfig(
                check="s.t.compare.column_mismatch", reason="old", expires=date(2026, 1, 1)
            ),
            WaiverConfig(check="other.*", reason="unused"),
        ],
        today,
    )
    passed = _result(Status.PASSED)
    assert book.apply(passed) is passed
    error = _result(Status.ERROR)
    assert book.apply(error) is error
    mismatches = [
        ColumnMismatch(column="name", mismatch_count=1, mismatch_pct=1.0),
        ColumnMismatch(column="city", mismatch_count=1, mismatch_pct=1.0),
    ]
    partial = book.apply(_result(column_mismatches=mismatches))
    assert partial.status is Status.FAILED
    assert partial.waiver is not None and partial.waiver.expired
    assert any("expired" in h for h in partial.hints) and any(
        "Waived columns" in h for h in partial.hints
    )
    only_name = book.apply(_result(column_mismatches=mismatches[:1]))
    assert only_name.status is Status.WAIVED and only_name.waiver is not None
    dq = book.apply(_result(columns=["name"]).model_copy(update={"check_id": "s.t.not_null.name"}))
    assert dq.status is Status.WAIVED
    statuses = {w.selector: w.status for w in book.statuses()}
    assert statuses == {
        "s.t.*": "applied",
        "s.t.compare.column_mismatch": "expired",
        "other.*": "unused",
    }
    assert WaiverBook([]).apply(partial) is partial


# ====================================================================== retries


class _Flaky(Connector):
    dialect = "duckdb"

    def __init__(self, failures: int, message: str) -> None:
        self.name = "flaky"
        self.failures = failures
        self.message = message
        self.closed = 0
        self.retry_policy = RetryPolicy(retries=2, backoff_s=0.0)

    def query(self, sql: str) -> pa.Table:
        if self.failures:
            self.failures -= 1
            raise ConnectorError("query failed") from RuntimeError(self.message)
        return pa.table({"n": [1]})

    def close(self) -> None:
        self.closed += 1


def test_transient_errors_are_retried() -> None:
    recorder = QueryRecorder()
    flaky = _Flaky(2, "server closed the connection unexpectedly")
    assert recorder.run(flaky, "SELECT 1", Side.SOURCE).num_rows == 1
    assert flaky.closed == 2
    assert recorder.records[-1].parameters == {"attempts": 3}
    hopeless = _Flaky(5, "connection reset by peer")
    with pytest.raises(ConnectorError):
        recorder.run(hopeless, "SELECT 1", Side.SOURCE)
    assert hopeless.failures == 2  # three attempts made
    bad_sql = _Flaky(1, "syntax error at or near SELECT")
    with pytest.raises(ConnectorError):
        recorder.run(bad_sql, "SELECT", Side.SOURCE)
    assert bad_sql.closed == 0
    assert RetryPolicy(3, 1.5).delay(2) == 6.0


# ====================================================================== checkpoints


def test_checkpoint_store_round_trip(tmp_path: Path) -> None:
    from dvf.models.results import DatasetPairResult

    store = CheckpointStore(tmp_path, "run-1")
    store.start(
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        config_hash="abc",
        config_files=["dvf.yaml"],
        selectors=["table:x"],
        profile=None,
    )
    assert load_manifest(tmp_path, "run-1")["selectors"] == ["table:x"]
    key = CheckpointStore.table_key("suite", "table with spaces/ä")
    assert key.startswith("suite.table_with_spaces") and len(key) <= 91
    store.save_table(key, DatasetPairResult(name="t", status=Status.PASSED))
    loaded = store.load_table(key)
    assert loaded is not None and loaded.status is Status.PASSED
    (tmp_path / "runs" / "run-1" / "tables" / "broken.json").write_text("{", encoding="utf-8")
    assert store.load_table("broken") is None
    parts = store.partitions(key)
    assert parts is not None and parts.load("p") is None
    parts.save("p", {"a": 1})
    assert parts.load("p") == {"a": 1}
    disabled = CheckpointStore(tmp_path, "run-2", enabled=False)
    assert disabled.load_table(key) is None and disabled.partitions(key) is None
    assert disabled.finish(None) is None  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="No checkpoints"):
        load_manifest(tmp_path, "missing")
    with pytest.raises(ConfigError, match="No saved results"):
        load_results(tmp_path, "run-1")
    for name in ("run-0", "run-3"):
        (tmp_path / "runs" / name).mkdir(parents=True)
    assert prune_runs(tmp_path, 2) == ["run-0"]
    assert prune_runs(tmp_path / "nothing", 1) == []
    assert _safe("plain") == "plain"
    broken = PartitionStore(tmp_path / "p")
    write_atomic(tmp_path / "p" / "x.json", "[1]")
    assert broken.load("x") is None


# ====================================================================== history


def test_history_store_urls_and_empty_reads(tmp_path: Path) -> None:
    assert parse_store("duckdb:///./h.duckdb", tmp_path) == ("duckdb", tmp_path / "h.duckdb")
    absolute = (tmp_path / "x.sqlite").as_posix()
    kind, path = parse_store(f"sqlite:///{absolute}", tmp_path)
    assert kind == "sqlite" and path.name == "x.sqlite"
    with pytest.raises(ConfigError, match="Unsupported history store"):
        parse_store("postgres://x", tmp_path)
    store = HistoryStore("duckdb:///missing/history.duckdb", tmp_path)
    assert store.runs() == [] and store.run("x") is None and store.check_statuses("x") == {}
    assert store.find_baseline("p", "last_run", "x") is None
    assert store.find_baseline("p", "none", "x") is None
    assert store.trend("p", "x") == []


# ====================================================================== selectors and logging


def test_selectors_keep_raw_items() -> None:
    assert Selectors.parse(["table:a", "check:b"]).raw == ["table:a", "check:b"]
    assert Selectors.parse(None).raw == []


def test_log_context_reaches_json_logs() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    try:
        with log_context(run_id="r1", table="t1"):
            get_logger("dvf.test").info("inside")
        get_logger("dvf.test").info("outside")
    finally:
        configure_logging("warning")
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert records[0]["run_id"] == "r1" and records[0]["table"] == "t1"
    assert "run_id" not in records[1]
    logging.getLogger("dvf").handlers.clear()
