"""MIG-07: audit and lineage columns."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from dvf.checks.context import CheckContext, CheckOutcome
from dvf.config.base import parse_duration
from dvf.core.enums import Dimension
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int, rules_outcome, text_in
from dvf.packs.migration.windows import bound_literal, parse_bound
from dvf.plugins.base import Check
from dvf.security.masking import to_json_safe

LABELS = {
    "load_timestamp_null": "rows without a load timestamp",
    "load_timestamp_future": "rows with a load timestamp in the future",
    "load_timestamp_too_early": "rows loaded before the allowed start",
    "batch_id_null": "rows without a batch ID",
    "batch_not_allowed": "rows with a batch ID that is not allowed",
    "too_many_batches": "batch IDs beyond the allowed number",
    "source_system_null": "rows without a source system",
    "source_system_not_allowed": "rows with a source system that is not allowed",
}

MAX_LISTED_BATCHES = 20


class AuditColumns(Check):
    """Audit and lineage columns are populated and consistent (MIG-07).

    Load timestamps must be set, not in the future (beyond ``max_future``), and not before
    ``not_before``; batch IDs set, within ``max_batches`` distinct values and ``allowed_batches``;
    source systems set and in ``allowed_source_systems``. "Now" is taken in
    ``normalization.timezone``, the time zone of timestamps without an offset.
    """

    dimension = Dimension.CONSISTENCY
    Config = cfg.AuditColumnsCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.AuditColumnsCheck)
        d = ctx.dialect
        rules: dict[str, str] = {}
        if config.load_timestamp:
            lt = ctx.col(config.load_timestamp)
            zone = ctx.normalization.timezone
            limit = datetime.now(UTC) + parse_duration(config.max_future)
            local = limit.astimezone(UTC if zone == "UTC" else ZoneInfo(zone)).replace(tzinfo=None)
            rules["load_timestamp_null"] = f"{lt} IS NULL"
            rules["load_timestamp_future"] = f"{lt} > {bound_literal(local, d)}"
            if config.not_before is not None:
                start = bound_literal(parse_bound(config.not_before, ctx.run), d)
                rules["load_timestamp_too_early"] = f"{lt} < {start}"
        batch = ctx.col(config.batch_id) if config.batch_id else None
        if batch is not None:
            rules["batch_id_null"] = f"{batch} IS NULL"
            if config.allowed_batches:
                allowed = text_in(batch, config.allowed_batches, d)
                rules["batch_not_allowed"] = f"{batch} IS NOT NULL AND NOT ({allowed})"
        if config.source_system:
            system = ctx.col(config.source_system)
            rules["source_system_null"] = f"{system} IS NULL"
            if config.allowed_source_systems:
                allowed = text_in(system, config.allowed_source_systems, d)
                rules["source_system_not_allowed"] = f"{system} IS NOT NULL AND NOT ({allowed})"
        items = ["COUNT(*) AS total_count"]
        items += [f"{d.count_if(cond)} AS {name}" for name, cond in rules.items()]
        if batch is not None:
            items.append(f"COUNT(DISTINCT {batch}) AS batch_count")
        row = ctx.row(f"SELECT {', '.join(items)}\n{ctx.from_where()}")
        total = as_int(row.get("total_count"))
        counts = {name: as_int(row.get(name)) for name in rules}
        details: dict[str, Any] = {}
        if batch is not None:
            batch_count = as_int(row.get("batch_count"))
            details["batch_count"] = batch_count
            if config.max_batches is not None:
                counts["too_many_batches"] = max(0, batch_count - config.max_batches)
            top = ctx.query(
                d.limit(
                    f"SELECT {batch} AS batch_id, COUNT(*) AS row_count\n{ctx.from_where()}\n"
                    f"GROUP BY {batch}\nORDER BY COUNT(*) DESC",
                    MAX_LISTED_BATCHES,
                )
            ).to_pylist()
            details["batches"] = [
                {str(k).lower(): to_json_safe(v) for k, v in r.items()} for r in top
            ]
        samples = extract = None
        failing = [cond for name, cond in rules.items() if counts.get(name)]
        if failing:
            label = " ".join(f"WHEN {cond} THEN {d.literal(name)}" for name, cond in rules.items())
            columns = [
                c for c in (config.load_timestamp, config.batch_id, config.source_system) if c
            ]
            samples, extract = ctx.evidence(
                f"SELECT {ctx.sample_columns(columns)}, CASE {label} END AS dvf_rule\n"
                f"{ctx.from_where(' OR '.join(f'({c})' for c in failing))}"
            )
        return rules_outcome(
            counts,
            LABELS,
            total=total,
            mostly=config.mostly,
            unit="rows",
            samples=samples,
            extract=extract,
            details=details,
        )


__all__ = ["AuditColumns"]
