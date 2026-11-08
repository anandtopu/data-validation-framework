"""MIG-02: delta and incremental loads.

The watermark window itself is a table option (``window``, see :mod:`.windows`). This module adds
``cdc_applied`` (the latest change of every key is reflected in the dataset) and
``late_arriving`` (rows that arrived after their period closed, or later than allowed).
"""

from __future__ import annotations

from dvf.checks.context import CheckContext, CheckOutcome
from dvf.checks.pair import EnvironmentAware, Workspace, WorkspaceInput
from dvf.config.base import parse_duration
from dvf.core.enums import Dimension
from dvf.core.errors import PlanError
from dvf.normalize.canonical import single_spec
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import (
    as_int,
    default_keys,
    flag_condition,
    flag_int,
    real_columns,
    resolve_locator,
    rules_outcome,
)
from dvf.packs.migration.windows import bound_literal, parse_bound
from dvf.plugins.base import Check

_CDC_LABELS = {
    "upsert_missing": "inserted or updated keys missing from the dataset",
    "upsert_deleted": "inserted or updated keys flagged as deleted",
    "delete_present": "deleted keys still present (and not flagged as deleted)",
    "value_mismatch": "keys whose values differ from the latest change",
    "unknown_operation": "changes with an unknown operation code",
}


def _differs(a: str, b: str) -> str:
    """Portable "values differ" for text columns, NULL-safe."""
    return f"({a} <> {b} OR ({a} IS NULL AND {b} IS NOT NULL) OR ({a} IS NOT NULL AND {b} IS NULL))"


class CdcApplied(EnvironmentAware, Check):
    """The latest change of every key in a CDC change table is reflected in the dataset (MIG-02).

    The latest change per key is found in the change table's database with ``ROW_NUMBER()``
    ordered by the sequence columns. Inserts and updates must be present (and, with
    ``compare_columns``, carry the after-image values); deletes must be absent, or flagged when
    ``soft_delete`` is set. Keys and values are compared as text.
    """

    dimension = Dimension.RECONCILIATION
    Config = cfg.CdcAppliedCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.CdcAppliedCheck)
        changes = config.changes
        chg_conn, chg_rel, chg_schema = resolve_locator(
            ctx.connectors, ctx.relation.connection, changes, ctx.render
        )
        where = "the change table"
        chg_keys = real_columns(chg_schema, changes.key, where)
        target_keys = default_keys(ctx, config.key_columns, "cdc_applied")
        if len(chg_keys) != len(target_keys):
            raise PlanError(
                f"The change table has {len(chg_keys)} key column(s) but the dataset has "
                f"{len(target_keys)}.",
                hint="List the same number of columns in 'changes.key' and 'key_columns'.",
            )
        (op_col,) = real_columns(chg_schema, [changes.operation], where)
        seq_cols = real_columns(chg_schema, changes.sequence, where)
        pairs = [
            (real_columns(chg_schema, [c.change], where)[0], ctx.column(c.target or c.change))
            for c in config.compare_columns
        ]

        cd = chg_conn.sql_dialect
        partition = ", ".join(cd.quote(c) for c in chg_keys)
        order = ", ".join(f"{cd.quote(c)} DESC" for c in seq_cols)
        inner = [f"{cd.to_text(cd.quote(c))} AS k{i}" for i, c in enumerate(chg_keys)]
        inner.append(f"{cd.lower(cd.trim(cd.to_text(cd.quote(op_col))))} AS op")
        inner += [f"{cd.to_text(cd.quote(c))} AS v{j}" for j, (c, _) in enumerate(pairs)]
        inner.append(f"ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {order}) AS dvf_rn")
        not_null = " AND ".join(f"{cd.quote(c)} IS NOT NULL" for c in chg_keys)
        outer = [f"k{i}" for i in range(len(chg_keys))] + ["op"]
        outer += [f"v{j}" for j in range(len(pairs))]
        chg_sql = (
            f"SELECT {', '.join(outer)} FROM (\n"
            f"SELECT {', '.join(inner)}\nFROM {chg_rel.sql}\nWHERE {not_null}\n) dvf_c\n"
            "WHERE dvf_rn = 1"
        )

        d = ctx.dialect
        items = [f"{d.to_text(d.quote(c))} AS k{i}" for i, c in enumerate(target_keys)]
        if config.soft_delete is not None:
            deleted = flag_condition(config.soft_delete, ctx.col, d, ctx.render)
            items.append(f"{flag_int(deleted)} AS dvf_del")
        else:
            items.append("0 AS dvf_del")
        items += [f"{d.to_text(d.quote(t))} AS v{j}" for j, (_, t) in enumerate(pairs)]
        tgt_sql = f"SELECT {', '.join(items)}\n{ctx.from_where()}"

        environment = self.environment
        resources = environment.resources if environment is not None else None
        inputs = {
            "chg": WorkspaceInput(chg_conn, chg_sql, ctx.side),
            "tgt": WorkspaceInput(ctx.connector, tgt_sql, ctx.side),
        }
        with Workspace(inputs, recorder=ctx.recorder, resources=resources) as ws:
            return self._rules(ctx, ws, config, len(chg_keys), len(pairs))

    @staticmethod
    def _rules(
        ctx: CheckContext, ws: Workspace, config: cfg.CdcAppliedCheck, nkeys: int, nvalues: int
    ) -> CheckOutcome:
        d = ws.dialect

        def codes(values: list[str]) -> str:
            return ", ".join(d.literal(v.strip().lower()) for v in values) or "NULL"

        upserts = codes([*config.insert_values, *config.update_values])
        deletes = codes(config.delete_values)
        known = codes([*config.insert_values, *config.update_values, *config.delete_values])
        join = " AND ".join(f"t.k{i} = c.k{i}" for i in range(nkeys))
        mismatch = " OR ".join(_differs(f"c.v{j}", f"t.v{j}") for j in range(nvalues))
        whens = [
            f"WHEN c.op IS NULL OR c.op NOT IN ({known}) THEN 'unknown_operation'",
            f"WHEN c.op IN ({upserts}) AND t.k0 IS NULL THEN 'upsert_missing'",
            f"WHEN c.op IN ({upserts}) AND t.dvf_del = 1 THEN 'upsert_deleted'",
            f"WHEN c.op IN ({deletes}) AND t.k0 IS NOT NULL AND COALESCE(t.dvf_del, 0) = 0 "
            "THEN 'delete_present'",
        ]
        if mismatch:
            whens.append(f"WHEN c.op IN ({upserts}) AND ({mismatch}) THEN 'value_mismatch'")
        keys = ", ".join(f"c.k{i} AS k{i}" for i in range(nkeys))
        classified = (
            f"SELECT {keys}, c.op AS op, CASE {' '.join(whens)} END AS dvf_rule\n"
            f"FROM {ws.table('chg', 'c')} LEFT JOIN {ws.table('tgt', 't')} ON {join}"
        )
        counts = dict.fromkeys(_CDC_LABELS, 0)
        for row in ws.rows(
            f"SELECT dvf_rule, COUNT(*) AS n FROM ({classified}) dvf_x "
            "WHERE dvf_rule IS NOT NULL GROUP BY dvf_rule"
        ):
            counts[str(row["dvf_rule"])] = as_int(row["n"])
        ops = {
            str(r["op"]): as_int(r["n"])
            for r in ws.rows(f"SELECT op, COUNT(*) AS n FROM {ws.table('chg')} GROUP BY op")
        }
        total = sum(ops.values())
        samples = extract = None
        if any(counts.values()):
            labels = ", ".join(
                [*(f"k{i} AS {d.quote(name)}" for i, name in enumerate(_key_labels(ctx, config)))]
            )
            samples, extract = ws.evidence(
                f"SELECT {labels}, op, dvf_rule FROM ({classified}) dvf_x "
                "WHERE dvf_rule IS NOT NULL",
                sample_size=ctx.sample_size,
                extract_rows=ctx.extract_rows,
                privacy=ctx.privacy,
            )
        outcome = rules_outcome(
            counts,
            _CDC_LABELS,
            total=total,
            mostly=config.mostly,
            unit="changed keys",
            samples=samples,
            extract=extract,
            details={"latest_operations": ops},
        )
        return outcome


def _key_labels(ctx: CheckContext, config: cfg.CdcAppliedCheck) -> list[str]:
    return list(config.key_columns) or list(ctx.key_columns) or list(config.changes.key)


class LateArriving(Check):
    """Rows that arrived after their period closed, or later than allowed (MIG-02).

    With ``period_start``, a row is late when its event is before the period start but it was
    loaded on or after ``loaded_since``. With ``max_lateness``, a row is late when it was loaded
    more than that long after its event. Rows missing either timestamp are not judged.
    """

    dimension = Dimension.TIMELINESS
    Config = cfg.LateArrivingCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.LateArrivingCheck)
        d = ctx.dialect
        event, load = ctx.col(config.event_column), ctx.col(config.load_column)
        late: list[str] = []
        if config.period_start is not None:
            start = bound_literal(parse_bound(config.period_start, ctx.run), d)
            since = bound_literal(
                parse_bound(config.loaded_since or config.period_start, ctx.run), d
            )
            late.append(f"({event} < {start} AND {load} >= {since})")
        if config.max_lateness is not None:
            micros = parse_duration(config.max_lateness).total_seconds() * 1_000_000
            late.append(
                f"({self._epoch(ctx, config.load_column)} - "
                f"{self._epoch(ctx, config.event_column)} > {micros!r})"
            )
        valid = (
            f"(CASE WHEN {event} IS NULL OR {load} IS NULL THEN 1 "
            f"WHEN {' OR '.join(late)} THEN 0 ELSE 1 END) = 1"
        )
        outcome = ctx.evaluate_predicate(
            valid, columns=[config.event_column, config.load_column], null_passes=False
        )
        total, unexpected = outcome.total_count or 0, outcome.unexpected_count or 0
        rules = []
        if config.period_start is not None:
            rules.append(f"event before {parse_bound(config.period_start, ctx.run)}")
        if config.max_lateness is not None:
            rules.append(f"loaded more than {config.max_lateness} after the event")
        outcome.message = (
            f"{unexpected:,} of {total:,} rows arrived late ({' or '.join(rules)})"
            if unexpected
            else f"no late-arriving rows among {total:,}"
        )
        return outcome

    @staticmethod
    def _epoch(ctx: CheckContext, column: str) -> str:
        found = ctx.schema.column(column) if ctx.schema is not None else None
        tz_aware = False
        if found is not None:
            tz_aware = single_spec(found.type, ctx.normalization).tz_aware
        return ctx.dialect.epoch_us(ctx.col(column), tz_aware=tz_aware, timezone="UTC")


__all__ = ["CdcApplied", "LateArriving"]
