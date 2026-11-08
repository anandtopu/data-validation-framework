"""Run history: every run's summary and check statuses, for trends, baselines, and run-to-run
comparison (BUILD_PROMPT.md section 9.7).

The store is a local DuckDB file by default (``duckdb:///./.dvf/history.duckdb``); SQLite works
too (``sqlite:///path``). History is best effort: a store that cannot be opened (for example,
locked by another run) is reported as a warning and never fails the run.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------- named baselines (MIG-14)

BASELINES_DIR = "baselines"
"""Folder under the state folder holding named metric baselines (``<name>.json``)."""

_BASELINE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def baseline_path(state_dir: Path, name: str) -> Path:
    """Where the named baseline ``name`` is stored."""
    if not _BASELINE_NAME.match(name):
        raise ConfigError(
            f"Invalid baseline name '{name}'.",
            hint="Use letters, digits, underscores, and hyphens, for example pre_cutover.",
        )
    return state_dir / BASELINES_DIR / f"{name}.json"


def save_named_baseline(
    state_dir: Path, name: str, document: dict[str, Any], *, overwrite: bool = False
) -> Path:
    """Store a named baseline document atomically; refuse to replace one unless ``overwrite``."""
    from dvf.execution.checkpoint import write_atomic

    path = baseline_path(state_dir, name)
    if path.exists() and not overwrite:
        raise ConfigError(
            f"Baseline '{name}' already exists ({path}).",
            hint="Choose another name, or pass --overwrite to replace it.",
        )
    write_atomic(path, json.dumps(document, indent=2, default=str))
    return path


def load_named_baseline(state_dir: Path, name: str) -> dict[str, Any]:
    """Read the named baseline ``name``."""
    path = baseline_path(state_dir, name)
    if not path.exists():
        available = [b["name"] for b in list_named_baselines(state_dir)]
        hint = (
            f"Available baselines: {', '.join(available)}."
            if available
            else "Capture one with 'dvf baseline capture CONFIG --name NAME'."
        )
        raise ConfigError(f"Baseline '{name}' not found in {path.parent}.", hint=hint)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ConfigError(f"Baseline file {path} is not a baseline document.")
    return document


def list_named_baselines(state_dir: Path) -> list[dict[str, Any]]:
    """Name, capture time, side, and table count of every named baseline, newest first."""
    folder = state_dir / BASELINES_DIR
    if not folder.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in folder.glob("*.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        entries.append(
            {
                "name": path.stem,
                "captured_at": document.get("captured_at"),
                "side": document.get("side"),
                "tables": len(document.get("tables", {})),
                "path": str(path),
            }
        )
    return sorted(entries, key=lambda e: str(e["captured_at"] or ""), reverse=True)


# ---------------------------------------------------------------------- comparing two runs (MIG-08)


@dataclass
class RunDiff:
    """How a later run (``b``) differs from an earlier one (``a``), for mock cutover rehearsals."""

    run_a: str
    run_b: str
    status_a: str
    status_b: str
    started_a: str | None = None
    started_b: str | None = None
    new_failures: list[str] = field(default_factory=list)
    """Checks failing (or in error) in ``b`` but not in ``a``."""
    resolved: list[str] = field(default_factory=list)
    """Checks failing in ``a`` that ran in ``b`` and no longer fail."""
    persisting: list[str] = field(default_factory=list)
    added_checks: list[str] = field(default_factory=list)
    removed_checks: list[str] = field(default_factory=list)
    status_changes: list[dict[str, Any]] = field(default_factory=list)
    """Every check whose status changed: ``check_id``, ``before``, ``after``."""
    summary_deltas: list[dict[str, Any]] = field(default_factory=list)
    """Run metrics (quality score, checks per status, rows compared, duration) before and after."""
    dataset_deltas: list[dict[str, Any]] = field(default_factory=list)
    """Per table: status, row counts, and key reconciliation totals that changed."""
    check_deltas: list[dict[str, Any]] = field(default_factory=list)
    """Checks whose unexpected count changed."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_a": self.run_a,
            "run_b": self.run_b,
            "status_a": self.status_a,
            "status_b": self.status_b,
            "started_a": self.started_a,
            "started_b": self.started_b,
            "new_failures": list(self.new_failures),
            "resolved": list(self.resolved),
            "persisting": list(self.persisting),
            "added_checks": list(self.added_checks),
            "removed_checks": list(self.removed_checks),
            "status_changes": list(self.status_changes),
            "summary_deltas": list(self.summary_deltas),
            "dataset_deltas": list(self.dataset_deltas),
            "check_deltas": list(self.check_deltas),
        }


def _delta(name: str, before: Any, after: Any, **extra: Any) -> dict[str, Any]:
    change = None
    numeric = (int, float)
    if (
        isinstance(before, numeric)
        and isinstance(after, numeric)
        and not isinstance(before, bool)
        and not isinstance(after, bool)
    ):
        change = round(after - before, 6)
    return {**extra, "metric": name, "before": before, "after": after, "change": change}


def compare_runs(a: RunResult, b: RunResult) -> RunDiff:
    """New, resolved, and persisting failures and metric deltas from run ``a`` to run ``b``."""
    checks_a = {c.check_id: c for c in a.iter_checks()}
    checks_b = {c.check_id: c for c in b.iter_checks()}
    failing_a = {k for k, c in checks_a.items() if c.status.value in _FAILING}
    failing_b = {k for k, c in checks_b.items() if c.status.value in _FAILING}
    diff = RunDiff(
        run_a=a.run_id,
        run_b=b.run_id,
        status_a=a.status.value,
        status_b=b.status.value,
        started_a=a.started_at.isoformat(),
        started_b=b.started_at.isoformat(),
        new_failures=sorted(failing_b - failing_a),
        resolved=sorted(k for k in failing_a - failing_b if k in checks_b),
        persisting=sorted(failing_a & failing_b),
        added_checks=sorted(set(checks_b) - set(checks_a)),
        removed_checks=sorted(set(checks_a) - set(checks_b)),
    )
    for check_id in sorted(set(checks_a) & set(checks_b)):
        before, after = checks_a[check_id], checks_b[check_id]
        if before.status != after.status:
            diff.status_changes.append(
                {"check_id": check_id, "before": before.status.value, "after": after.status.value}
            )
        if before.unexpected_count != after.unexpected_count:
            diff.check_deltas.append(
                _delta(
                    "unexpected_count",
                    before.unexpected_count,
                    after.unexpected_count,
                    check_id=check_id,
                )
            )
    sa, sb = a.summary, b.summary
    diff.summary_deltas.append(_delta("quality_score", sa.quality_score, sb.quality_score))
    for status in Status:
        before_n = sa.checks_by_status.get(status, 0)
        after_n = sb.checks_by_status.get(status, 0)
        if before_n or after_n:
            diff.summary_deltas.append(_delta(f"checks_{status.value}", before_n, after_n))
    diff.summary_deltas.append(_delta("rows_compared", sa.rows_compared, sb.rows_compared))
    diff.summary_deltas.append(_delta("duration_ms", a.duration_ms, b.duration_ms))
    datasets_a = {(s.name, d.name): d for s in a.suites for d in s.datasets}
    datasets_b = {(s.name, d.name): d for s in b.suites for d in s.datasets}
    for key in sorted(set(datasets_a) | set(datasets_b)):
        da, db = datasets_a.get(key), datasets_b.get(key)
        table = f"{key[0]}.{key[1]}"
        values: list[tuple[str, Any, Any]] = [
            ("status", da.status.value if da else None, db.status.value if db else None),
            (
                "source_rows",
                da.row_counts.source if da else None,
                db.row_counts.source if db else None,
            ),
            (
                "target_rows",
                da.row_counts.target if da else None,
                db.row_counts.target if db else None,
            ),
        ]
        ra = da.reconciliation if da else None
        rb = db.reconciliation if db else None
        for name in ("matched", "mismatched", "missing_in_target", "extra_in_target"):
            values.append(
                (name, getattr(ra, name) if ra else None, getattr(rb, name) if rb else None)
            )
        for name, before_value, after_value in values:
            if before_value != after_value:
                diff.dataset_deltas.append(_delta(name, before_value, after_value, table=table))
    return diff
