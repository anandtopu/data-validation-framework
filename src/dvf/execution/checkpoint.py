"""Checkpoints: finished tables and partitions are saved as they complete, so an interrupted or
killed run can continue with ``dvf run --resume RUN_ID``.

Layout under the state folder (``defaults.execution.state_dir``, ``.dvf`` by default)::

    runs/<run_id>/manifest.json          run identity, config hash, selectors, status
    runs/<run_id>/tables/<table>.json    a finished table's results (DatasetPairResult)
    runs/<run_id>/partitions/<table>/<key>.json   a finished partition's diff
    runs/<run_id>/results.json           the final results (also used by debug and rerun)

Every file is written to a temporary name and renamed, so a kill never leaves a half-written
checkpoint behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dvf.core.errors import ConfigError
from dvf.models.results import DatasetPairResult, RunResult

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
MANIFEST = "manifest.json"
RESULTS = "results.json"


def _safe(text: str, limit: int = 80) -> str:
    cleaned = _UNSAFE.sub("_", text).strip("._") or "x"
    if len(cleaned) <= limit and cleaned == text:
        return cleaned
    digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{cleaned[:limit]}-{digest}"


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` without ever leaving a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def runs_dir(state_dir: Path) -> Path:
    return state_dir / "runs"


class PartitionStore:
    """Saved partition diffs of one table (implements ``PartitionCheckpoints``)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / f"{_safe(key)}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, key: str, data: dict[str, Any]) -> None:
        write_atomic(self._path(key), json.dumps(data, default=str))


class CheckpointStore:
    """The checkpoint folder of one run."""

    def __init__(self, state_dir: Path, run_id: str, *, enabled: bool = True) -> None:
        self.state_dir = state_dir
        self.run_id = run_id
        self.enabled = enabled
        self.directory = runs_dir(state_dir) / run_id

    # ------------------------------------------------------------------ manifest
    def start(
        self,
        *,
        started_at: datetime,
        config_hash: str,
        config_files: list[str],
        selectors: list[str],
        profile: str | None,
    ) -> None:
        if not self.enabled:
            return
        manifest = self.manifest() or {}
        manifest.update(
            {
                "run_id": self.run_id,
                "started_at": started_at.isoformat(),
                "config_hash": config_hash,
                "config_files": config_files,
                "selectors": selectors,
                "profile": profile,
                "status": "running",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        write_atomic(self.directory / MANIFEST, json.dumps(manifest, indent=2))

    def manifest(self) -> dict[str, Any] | None:
        path = self.directory / MANIFEST
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    def finish(self, result: RunResult, output_dir: Path | None = None) -> Path | None:
        """Save the final results and mark the run finished (or interrupted)."""
        if not self.enabled:
            return None
        path = self.directory / RESULTS
        write_atomic(path, result.model_dump_json(indent=None))
        manifest = self.manifest() or {"run_id": self.run_id}
        manifest["status"] = "interrupted" if result.interrupted else "completed"
        if output_dir is not None:
            manifest["output_dir"] = str(output_dir.resolve())
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        write_atomic(self.directory / MANIFEST, json.dumps(manifest, indent=2))
        return path

    # ------------------------------------------------------------------ tables
    @staticmethod
    def table_key(suite: str, table: str) -> str:
        return _safe(f"{suite}.{table}")

    def load_table(self, key: str) -> DatasetPairResult | None:
        if not self.enabled:
            return None
        path = self.directory / "tables" / f"{key}.json"
        if not path.exists():
            return None
        try:
            return DatasetPairResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_table(self, key: str, dataset: DatasetPairResult) -> None:
        if self.enabled:
            write_atomic(self.directory / "tables" / f"{key}.json", dataset.model_dump_json())

    def partitions(self, key: str) -> PartitionStore | None:
        if not self.enabled:
            return None
        return PartitionStore(self.directory / "partitions" / key)


def load_manifest(state_dir: Path, run_id: str) -> dict[str, Any]:
    """The manifest of an earlier run, or a configuration error saying how to find run IDs."""
    store = CheckpointStore(state_dir, run_id)
    manifest = store.manifest() if store.directory.is_dir() else None
    if manifest is None:
        raise ConfigError(
            f"No checkpoints for run '{run_id}' in {runs_dir(state_dir)}.",
            hint="Run 'dvf history' to list runs, and pass the same configuration file.",
        )
    return manifest


def load_results(state_dir: Path, run_id: str) -> RunResult:
    """The saved results of an earlier run."""
    path = runs_dir(state_dir) / run_id / RESULTS
    if not path.exists():
        raise ConfigError(
            f"No saved results for run '{run_id}' in {runs_dir(state_dir)}.",
            hint="Run 'dvf history' to list runs, or pass the results file with --results.",
        )
    return RunResult.model_validate_json(path.read_text(encoding="utf-8"))


def prune_runs(state_dir: Path, keep: int) -> list[str]:
    """Delete the checkpoint folders of all but the ``keep`` newest runs; return their IDs."""
    root = runs_dir(state_dir)
    if not root.is_dir():
        return []
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    removed = []
    for path in runs[: max(0, len(runs) - keep)]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed
