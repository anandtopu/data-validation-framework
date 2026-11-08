"""Performance benchmarks (BUILD_PROMPT.md section 8.5). Run with ``uv run nox -s benchmark``.

They are skipped unless ``DVF_BENCHMARK=1``. ``DVF_BENCH_ROWS`` sets the size (10 million by
default, the Phase 2 acceptance size). Each benchmark appends its metrics to
``.benchmarks/results.json``; ``tests/benchmarks/compare.py`` fails when a metric is more than 15%
slower than the stored baseline for this machine.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

import pytest

from dvf import api
from dvf.core.enums import Status
from dvf.plugins.registry import new_registry
from tests.benchmarks.generate import differences, generate, to_duckdb

pytestmark = pytest.mark.skipif(
    os.environ.get("DVF_BENCHMARK") != "1", reason="benchmarks run with DVF_BENCHMARK=1"
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".benchmarks"
ROWS = int(os.environ.get("DVF_BENCH_ROWS", "10000000"))
EVERY = 10_000  # 0.01% of rows differ
LOCAL_TARGET_S = float(os.environ.get("DVF_BENCH_TARGET_S", "120"))


@pytest.fixture(scope="module")
def data() -> tuple[Path, Path]:
    return generate(OUT / "data", ROWS, EVERY)


def _record(name: str, **metrics: Any) -> None:
    OUT.mkdir(exist_ok=True)
    path = OUT / "results.json"
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    results[name] = {"rows": ROWS, **metrics}
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")


def _config(
    source: dict[str, Any], target: dict[str, Any], comparison: dict[str, Any], **defaults: Any
) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "benchmark"},
        "defaults": defaults,
        "connections": {"src": source["connection"], "tgt": target["connection"]},
        "suites": [
            {
                "name": "bench",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "wide",
                        "source": source["dataset"],
                        "target": target["dataset"],
                        "keys": ["id"],
                        "comparison": comparison,
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }


def _run(config: dict[str, Any], base: Path) -> tuple[Any, float]:
    loaded = api.load_config_data(config, base_dir=base, strict=False, registry=new_registry())
    started = time.perf_counter()
    result = api.run(loaded, write_reports=False).result
    return result, time.perf_counter() - started


def _check(result: Any, check_type: str) -> Any:
    return next(c for c in result.iter_checks() if c.type == check_type)


def _assert_exact(result: Any) -> None:
    errors = [c for c in result.iter_checks() if c.status is Status.ERROR]
    assert not errors, [(c.check_id, c.message) for c in errors]
    dataset = result.suites[0].datasets[0]
    expected = differences(ROWS, EVERY)
    assert dataset.reconciliation.mismatched == expected
    assert dataset.reconciliation.matched == ROWS - expected
    mismatch = _check(result, "compare.column_mismatch")
    assert [(m.column, m.mismatch_count) for m in mismatch.column_mismatches] == [("s0", expected)]


def test_local_full_comparison(data: tuple[Path, Path], tmp_path: Path) -> None:
    """10M rows x 30 columns, Parquet to Parquet, full row-level comparison: under 2 minutes."""
    source, target = data
    files = {"type": "files", "path": source.parent.as_posix()}
    config = _config(
        {"connection": files, "dataset": {"path": source.name}},
        {"connection": files, "dataset": {"path": target.name}},
        {"strategies": ["row_count", "row_hash"], "on_mismatch": "full_diff"},
    )
    result, seconds = _run(config, tmp_path)
    _assert_exact(result)
    _record("local_full_comparison", seconds=round(seconds, 3), rows_per_s=round(ROWS / seconds))
    assert seconds < LOCAL_TARGET_S, f"{seconds:.1f}s for {ROWS:,} rows"


def test_local_comparison_with_memory_limit(data: tuple[Path, Path], tmp_path: Path) -> None:
    """The same comparison with the local engine capped at 1 GB: it spills to disk and finishes."""
    source, target = data
    files = {"type": "files", "path": source.parent.as_posix()}
    config = _config(
        {"connection": files, "dataset": {"path": source.name}},
        {"connection": files, "dataset": {"path": target.name}},
        {"strategies": ["row_hash"], "on_mismatch": "full_diff"},
        execution={"memory_limit": "1GB", "temp_dir": (tmp_path / "spill").as_posix()},
    )
    result, seconds = _run(config, tmp_path)
    _assert_exact(result)
    _record("local_comparison_1gb", seconds=round(seconds, 3))


@pytest.mark.parametrize("strategy", ["hash_bucket", "bisect"])
def test_cross_database_transfer_is_proportional(
    strategy: str, data: tuple[Path, Path], tmp_path: Path
) -> None:
    """A DuckDB database against Parquet files with 0.01% differences: found exactly, moving rows
    in proportion to the differences (about d log n), not to the table size."""
    source, target = data
    database = to_duckdb(source, OUT / "data" / f"source_{ROWS}.duckdb")
    config = _config(
        {
            "connection": {"type": "duckdb", "database": database.as_posix()},
            "dataset": {"table": "wide"},
        },
        {
            "connection": {"type": "files", "path": target.parent.as_posix()},
            "dataset": {"path": target.name},
        },
        {"strategies": ["row_count", strategy], "on_mismatch": "full_diff", "buckets": 1024},
    )
    result, seconds = _run(config, tmp_path)
    _assert_exact(result)
    search = _check(result, f"compare.{strategy}")
    moved = search.details["rows_transferred"]
    d = differences(ROWS, EVERY)
    bound = (
        2_000 * d * math.log2(ROWS) / math.log2(1024)
    )  # generous constant; table size never appears
    _record(
        f"cross_database_{strategy}",
        seconds=round(seconds, 3),
        rows_transferred=moved,
        differences=d,
        transfer_pct=search.details["transfer_pct"],
    )
    assert moved < bound, search.details
    assert moved < 0.05 * 2 * ROWS, search.details
