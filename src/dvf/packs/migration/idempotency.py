"""MIG-03: idempotency of a load (re-running it changes nothing)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dvf.checks.context import CheckContext, CheckOutcome
from dvf.checks.pair import CheckEnvironment, EnvironmentAware
from dvf.core.enums import Dimension
from dvf.models.results import MetricComparison, RunResult
from dvf.normalize.canonical import canonical_sql, row_hash_sql, single_spec
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int, default_keys
from dvf.plugins.base import Check

log = logging.getLogger(__name__)


def content_checksum_sql(ctx: CheckContext, columns: list[str]) -> str:
    """Row count and two sums of 32-bit row-hash prefixes: an order-independent checksum of the
    canonical content of ``columns`` (duplicates are counted, never cancelled out)."""
    d = ctx.dialect
    assert ctx.schema is not None
    exprs = []
    for name in columns:
        found = ctx.schema.column(name)
        assert found is not None
        spec = single_spec(found.type, ctx.normalization)
        exprs.append(canonical_sql(d.quote(found.name), spec, d))
    hashed = f"SELECT {row_hash_sql(exprs, d)} AS h\n{ctx.from_where()}"
    prefixes = (
        f"SELECT {d.hash_int('h', 0)} AS h1, {d.hash_int('h', 8)} AS h2 FROM ({hashed}) dvf_h"
    )
    return (
        "SELECT COUNT(*) AS row_count, COALESCE(SUM(h1), 0) AS s1, COALESCE(SUM(h2), 0) AS s2 "
        f"FROM ({prefixes}) dvf_s"
    )


def find_baseline_check(
    environment: CheckEnvironment, policy: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The baseline run's history row and this check's details in that run (if found)."""
    history = environment.history()
    if history is None:
        return None, None
    run = history.find_baseline(environment.project, policy, environment.run.run_id)
    if run is None or not run.get("results_path"):
        return run, None
    path = Path(str(run["results_path"]))
    try:
        result = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("baseline results %s could not be read: %s", path, exc)
        return run, None
    for check in result.iter_checks():
        if check.check_id == environment.check_id:
            return run, dict(check.details)
    return run, None


class Idempotency(EnvironmentAware, Check):
    """Re-running a load creates no duplicates and changes neither the row count nor the content
    (MIG-03).

    Duplicate keys are counted in the database. The row count and an order-independent checksum
    of the canonical content (two sums of row-hash prefixes) are recorded in the result and
    compared with the same check in the baseline run from the run history (``last_run`` by
    default). Without a baseline, the check records its metrics and passes.
    """

    dimension = Dimension.CONSISTENCY
    Config = cfg.IdempotencyCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.IdempotencyCheck)
        assert ctx.schema is not None
        keys = default_keys(ctx, config.key_columns, "idempotency")
        d = ctx.dialect
        cols = ", ".join(d.quote(k) for k in keys)
        not_null = " AND ".join(f"{d.quote(k)} IS NOT NULL" for k in keys)
        groups = (
            f"SELECT {cols}, COUNT(*) AS dvf_n\n{ctx.from_where(not_null)}\n"
            f"GROUP BY {cols}\nHAVING COUNT(*) > 1"
        )
        dup = ctx.row(
            "SELECT COUNT(*) AS duplicate_keys, COALESCE(SUM(dvf_n), 0) AS duplicate_rows "
            f"FROM ({groups}) dvf_d"
        )
        duplicate_keys = as_int(dup.get("duplicate_keys"))
        duplicate_rows = as_int(dup.get("duplicate_rows"))

        excluded = {e.lower() for e in config.exclude}
        names = config.checksum_columns or [c.name for c in ctx.schema.columns]
        columns = [ctx.column(n) for n in names if n.lower() not in excluded]
        stats = ctx.row(content_checksum_sql(ctx, columns))
        row_count = as_int(stats.get("row_count"))
        checksum = f"{row_count}-{as_int(stats.get('s1')):x}-{as_int(stats.get('s2')):x}"

        samples = None
        if duplicate_keys:
            samples = ctx.sample(
                d.limit(f"{groups}\nORDER BY dvf_n DESC, {cols}", max(ctx.sample_size, 1))
            )
        details: dict[str, Any] = {
            "row_count": row_count,
            "checksum": checksum,
            "duplicate_keys": duplicate_keys,
            "checksum_columns": columns,
        }
        metrics: list[MetricComparison] = []
        problems: list[str] = []
        if duplicate_keys:
            problems.append(
                f"{duplicate_keys:,} duplicated key(s) across {duplicate_rows:,} rows "
                f"({duplicate_rows - duplicate_keys:,} rows beyond the first)"
            )
        policy = config.baseline or "last_run"
        environment = self.environment
        baseline_run: dict[str, Any] | None = None
        previous: dict[str, Any] | None = None
        if environment is not None:
            baseline_run, previous = find_baseline_check(environment, policy)
        if previous is not None and baseline_run is not None:
            details["baseline_run_id"] = baseline_run["run_id"]
            if "row_count" in config.compare and previous.get("row_count") is not None:
                before = int(previous["row_count"])
                same = before == row_count
                metrics.append(
                    MetricComparison(
                        metric="row_count",
                        source=before,
                        target=row_count,
                        difference=float(row_count - before),
                        passed=same,
                        note=f"baseline run {baseline_run['run_id']}",
                    )
                )
                if not same:
                    problems.append(
                        f"row count changed from {before:,} to {row_count:,} since run "
                        f"{baseline_run['run_id']}"
                    )
            if "checksum" in config.compare and previous.get("checksum") is not None:
                same = previous["checksum"] == checksum
                metrics.append(
                    MetricComparison(
                        metric="checksum",
                        source=previous["checksum"],
                        target=checksum,
                        passed=same,
                        note=f"baseline run {baseline_run['run_id']}",
                    )
                )
                if not same and previous.get("checksum_columns") == columns:
                    problems.append(f"content checksum changed since run {baseline_run['run_id']}")
                elif not same:
                    metrics[-1] = metrics[-1].model_copy(
                        update={"passed": True, "note": "checksum columns changed; not compared"}
                    )
            baseline_note = f"compared with run {baseline_run['run_id']}"
        else:
            baseline_note = (
                "no baseline run with this check yet; row count and checksum recorded"
                if environment is not None and environment.history_store is not None
                else "run history is off, so only duplicates were checked"
            )
        message = "; ".join(problems) if problems else f"{row_count:,} rows, no duplicate keys"
        return CheckOutcome(
            passed=not problems,
            message=f"{message} ({baseline_note})",
            observed={"row_count": row_count, "checksum": checksum},
            expected=(
                {"row_count": previous.get("row_count"), "checksum": previous.get("checksum")}
                if previous
                else None
            ),
            unexpected_count=duplicate_rows - duplicate_keys,
            total_count=row_count,
            samples=samples,
            metrics=metrics,
            details=details,
        )


__all__ = ["Idempotency", "content_checksum_sql", "find_baseline_check"]
