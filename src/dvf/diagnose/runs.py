"""Load earlier runs for ``dvf debug``, ``dvf rerun``, and notebooks.

A run is found by results file, or by run ID in a state folder (``.dvf`` next to the
configuration): ``runs/<run_id>/results.json`` and its manifest, which also records where the
reports (and extracts) were written.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

from dvf.core.enums import Status
from dvf.core.errors import ConfigError
from dvf.execution.checkpoint import MANIFEST, RESULTS, runs_dir
from dvf.execution.selectors import Selectors
from dvf.models.results import CheckResult, RunResult

Format = Literal["arrow", "pandas", "polars"]


def _convert(table: pa.Table, fmt: Format) -> Any:
    if fmt == "pandas":
        return table.to_pandas()
    if fmt == "polars":
        import polars as pl

        return pl.from_arrow(table)
    return table


@dataclass
class CheckHandle:
    """One check of a loaded run."""

    result: CheckResult
    output_dir: Path | None

    def mismatches(self, format: Format = "arrow") -> Any:
        """Mismatched values or failing rows: the extract file when one was written, otherwise
        the samples kept in the results. Returns an Arrow table, or a pandas or Polars frame."""
        for artifact in self.result.artifacts:
            if artifact.kind == "extract" and self.output_dir is not None:
                path = self.output_dir / artifact.path
                if path.exists():
                    table = (
                        pa_csv.read_csv(path) if artifact.format == "csv" else pq.read_table(path)
                    )
                    return _convert(table, format)
        rows: list[dict[str, Any]] = []
        for mismatch in self.result.column_mismatches:
            if mismatch.samples is None:
                continue
            for values in mismatch.samples.rows:
                row = dict(zip(mismatch.samples.columns, values, strict=False))
                row["column"] = mismatch.column
                rows.append({k: None if v is None else str(v) for k, v in row.items()})
        if not rows and self.result.samples is not None:
            sample = self.result.samples
            rows = [
                {k: None if v is None else str(v) for k, v in zip(sample.columns, r, strict=False)}
                for r in sample.rows
            ]
        names = sorted({k for r in rows for k in r}, key=lambda k: (k == "column", k))
        table = pa.table(
            {n: pa.array([r.get(n) for r in rows], pa.string()) for n in names} if rows else {}
        )
        return _convert(table, format)

    def queries(self) -> list[str]:
        return [q.sql for q in self.result.queries]


@dataclass
class RunHandle:
    """An earlier run: its results, and where its reports were written."""

    result: RunResult
    output_dir: Path | None = None
    config_files: tuple[str, ...] = ()

    @property
    def checks(self) -> list[CheckResult]:
        return list(self.result.iter_checks())

    def failed(self) -> list[CheckResult]:
        """Checks that failed or errored (rerun candidates)."""
        return [c for c in self.checks if c.status in (Status.FAILED, Status.ERROR)]

    def check(self, check_id: str) -> CheckHandle:
        """The check with this ID (or the only one ending with it)."""
        checks = self.checks
        exact = [c for c in checks if c.check_id == check_id]
        found = exact or [c for c in checks if c.check_id.endswith(check_id)]
        if len(found) == 1:
            return CheckHandle(found[0], self.output_dir)
        ids = [c.check_id for c in checks]
        if found:
            raise ConfigError(
                f"'{check_id}' matches {len(found)} checks in run {self.result.run_id}.",
                hint="Use the full check ID: " + ", ".join(c.check_id for c in found[:5]),
            )
        close = difflib.get_close_matches(check_id, ids, n=3, cutoff=0.4)
        raise ConfigError(
            f"No check '{check_id}' in run {self.result.run_id}.",
            hint=("Did you mean " + ", ".join(close) + "?")
            if close
            else "Run 'dvf debug RUN_ID' to list checks.",
        )

    def rerun_selectors(self) -> Selectors:
        """Selectors that run exactly the failed and errored checks again."""
        failed = self.failed()
        if not failed:
            raise ConfigError(f"Run {self.result.run_id} has no failed or errored checks to rerun.")
        items: list[str] = []
        for suite in self.result.suites:
            for dataset in suite.datasets:
                if any(c.status in (Status.FAILED, Status.ERROR) for c in dataset.checks):
                    items.append(f"table:{suite.name}.{dataset.name}")
        items += [f"check:{c.check_id.split('#')[0]}" for c in failed]
        return Selectors.parse(list(dict.fromkeys(items)))


def load_run(
    run: str | Path,
    *,
    state_dir: Path | None = None,
) -> RunHandle:
    """A run from a ``results.json`` path, or from its run ID in ``state_dir`` (default ``.dvf``
    in the current folder)."""
    path = Path(run)
    if path.suffix == ".json" and path.is_file():
        result = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
        return RunHandle(result, output_dir=path.parent)
    root = state_dir if state_dir is not None else Path(".dvf")
    folder = runs_dir(root) / str(run)
    results = folder / RESULTS
    if not results.is_file():
        known = sorted(p.name for p in runs_dir(root).iterdir()) if runs_dir(root).is_dir() else []
        raise ConfigError(
            f"No saved results for run '{run}' in {runs_dir(root)}.",
            hint=(
                f"Known runs: {', '.join(known[-5:])}"
                if known
                else "Pass --config (to use its state folder) or --results PATH."
            ),
        )
    result = RunResult.model_validate_json(results.read_text(encoding="utf-8"))
    output_dir: Path | None = None
    config_files: tuple[str, ...] = ()
    manifest_path = folder / MANIFEST
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("output_dir"):
            output_dir = Path(manifest["output_dir"])
        config_files = tuple(manifest.get("config_files") or ())
    return RunHandle(result, output_dir=output_dir, config_files=config_files)
