"""Run history: every run's summary and check statuses, for trends, baselines, and run-to-run
comparison (BUILD_PROMPT.md section 9.7).

The store is a local DuckDB file by default (``duckdb:///./.dvf/history.duckdb``); SQLite works
too (``sqlite:///path``). History is best effort: a store that cannot be opened (for example,
locked by another run) is reported as a warning and never fails the run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from dvf.core.enums import Status
from dvf.core.errors import ConfigError
from dvf.models.results import BaselineComparison, RunResult, TrendPoint

log = logging.getLogger(__name__)

_FAILING = {Status.FAILED.value, Status.ERROR.value}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS dvf_runs (
        run_id TEXT PRIMARY KEY, project TEXT, status TEXT, started_at TEXT, ended_at TEXT,
        duration_ms DOUBLE, quality_score DOUBLE, checks INTEGER, failed INTEGER, errors INTEGER,
        warnings INTEGER, waived INTEGER, rows_compared BIGINT, config_hash TEXT, profile TEXT,
        git_sha TEXT, results_path TEXT, config_files TEXT, interrupted INTEGER)""",
    """CREATE TABLE IF NOT EXISTS dvf_checks (
        run_id TEXT, check_id TEXT, suite TEXT, dataset TEXT, type TEXT, status TEXT,
        severity TEXT, unexpected_count BIGINT, message TEXT)""",
)


def parse_store(url: str, base_dir: Path) -> tuple[str, Path]:
    """``(kind, path)`` from ``duckdb:///path`` or ``sqlite:///path`` (relative to ``base_dir``)."""
    kind, sep, rest = url.partition("://")
    if not sep or kind not in ("duckdb", "sqlite"):
        raise ConfigError(
            f"Unsupported history store '{url}'.",
            hint="Use duckdb:///path/to/history.duckdb or sqlite:///path/to/history.sqlite.",
        )
    raw = rest[1:] if rest.startswith("/") else rest
    path = Path(raw)
    return kind, path if path.is_absolute() else base_dir / path


class HistoryStore:
    """Read and write run history."""

    def __init__(self, url: str, base_dir: Path) -> None:
        self.kind, self.path = parse_store(url, base_dir)

    @contextmanager
    def _connect(self, *, write: bool) -> Iterator[Any]:
        if not write and not self.path.exists():
            yield None
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con: Any = None
        for attempt in range(5):
            try:
                if self.kind == "duckdb":
                    con = duckdb.connect(str(self.path))
                else:
                    con = sqlite3.connect(str(self.path), timeout=10)
                break
            except (duckdb.IOException, sqlite3.OperationalError):
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
        try:
            for statement in _SCHEMA:
                con.execute(statement)
            yield con
            if self.kind == "sqlite":
                con.commit()
        finally:
            con.close()

    @staticmethod
    def _rows(con: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = con.execute(sql, list(params))
        names = [d[0] for d in cursor.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------ write
    def record(self, result: RunResult, results_path: Path | None, config_files: list[str]) -> None:
        counts = {
            s.value if hasattr(s, "value") else str(s): n
            for s, n in result.summary.checks_by_status.items()
        }
        run_row = [
            result.run_id,
            result.project,
            result.status.value,
            result.started_at.isoformat(),
            result.ended_at.isoformat() if result.ended_at else None,
            result.duration_ms,
            result.summary.quality_score,
            sum(counts.values()),
            counts.get("failed", 0),
            counts.get("error", 0),
            counts.get("warning", 0),
            counts.get("waived", 0),
            result.summary.rows_compared,
            result.environment.config_hash,
            result.environment.profile,
            result.environment.git_sha,
            str(results_path) if results_path else None,
            json.dumps(config_files),
            int(result.interrupted),
        ]
        check_rows = []
        for suite in result.suites:
            for dataset in suite.datasets:
                for check in dataset.checks:
                    check_rows.append(
                        [
                            result.run_id,
                            check.check_id,
                            suite.name,
                            dataset.name,
                            check.type,
                            check.status.value,
                            check.severity.value,
                            check.unexpected_count,
                            (check.message or "")[:1000],
                        ]
                    )
        with self._connect(write=True) as con:
            con.execute("DELETE FROM dvf_checks WHERE run_id = ?", [result.run_id])
            con.execute("DELETE FROM dvf_runs WHERE run_id = ?", [result.run_id])
            con.execute(f"INSERT INTO dvf_runs VALUES ({', '.join('?' * len(run_row))})", run_row)
            if check_rows:
                con.executemany(
                    "INSERT INTO dvf_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", check_rows
                )

    def prune(self, project: str, keep: int) -> list[str]:
        """Forget all but the ``keep`` newest runs of ``project``; return the removed run IDs."""
        with self._connect(write=True) as con:
            rows = self._rows(
                con,
                "SELECT run_id FROM dvf_runs WHERE project = ? ORDER BY started_at DESC",
                [project],
            )
            removed = [r["run_id"] for r in rows[keep:]]
            for run_id in removed:
                con.execute("DELETE FROM dvf_checks WHERE run_id = ?", [run_id])
                con.execute("DELETE FROM dvf_runs WHERE run_id = ?", [run_id])
        return removed

    # ------------------------------------------------------------------ read
    def runs(self, project: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect(write=False) as con:
            if con is None:
                return []
            where, params = ("WHERE project = ?", [project]) if project else ("", [])
            return self._rows(
                con,
                f"SELECT * FROM dvf_runs {where} ORDER BY started_at DESC LIMIT {int(limit)}",
                params,
            )

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect(write=False) as con:
            if con is None:
                return None
            rows = self._rows(con, "SELECT * FROM dvf_runs WHERE run_id = ?", [run_id])
            return rows[0] if rows else None

    def check_statuses(self, run_id: str) -> dict[str, str]:
        with self._connect(write=False) as con:
            if con is None:
                return {}
            rows = self._rows(
                con, "SELECT check_id, status FROM dvf_checks WHERE run_id = ?", [run_id]
            )
            return {r["check_id"]: r["status"] for r in rows}

    def find_baseline(self, project: str, policy: str, exclude: str) -> dict[str, Any] | None:
        """The run ``policy`` names: ``last_run``, ``last_passed``, or a run ID."""
        if policy == "none":
            return None
        with self._connect(write=False) as con:
            if con is None:
                return None
            if policy in ("last_run", "last_passed"):
                status = "AND status = 'passed'" if policy == "last_passed" else ""
                rows = self._rows(
                    con,
                    "SELECT * FROM dvf_runs WHERE project = ? AND run_id <> ? AND interrupted = 0 "
                    f"{status} ORDER BY started_at DESC LIMIT 1",
                    [project, exclude],
                )
            else:
                rows = self._rows(con, "SELECT * FROM dvf_runs WHERE run_id = ?", [policy])
            return rows[0] if rows else None

    def trend(self, project: str, exclude: str, limit: int = 20) -> list[TrendPoint]:
        rows = [r for r in self.runs(project, limit + 1) if r["run_id"] != exclude][:limit]
        points = [
            TrendPoint(
                run_id=r["run_id"],
                started_at=datetime.fromisoformat(r["started_at"]),
                status=Status(r["status"]),
                quality_score=r["quality_score"],
                checks=int(r["checks"] or 0),
                failed=int(r["failed"] or 0),
                errors=int(r["errors"] or 0),
            )
            for r in rows
        ]
        return list(reversed(points))


def compare_to_baseline(
    result: RunResult, baseline: dict[str, Any], previous: dict[str, str], policy: str
) -> BaselineComparison:
    """New, resolved, and persisting failures of ``result`` relative to a baseline run."""
    current = {c.check_id: c.status.value for c in result.iter_checks()}
    failing_now = {k for k, v in current.items() if v in _FAILING}
    failing_before = {k for k, v in previous.items() if v in _FAILING}
    return BaselineComparison(
        baseline_run_id=baseline["run_id"],
        baseline_started_at=datetime.fromisoformat(baseline["started_at"]),
        policy=policy,
        new_failures=sorted(failing_now - failing_before),
        resolved=sorted(k for k in failing_before - failing_now if k in current),
        persisting=sorted(failing_now & failing_before),
    )
