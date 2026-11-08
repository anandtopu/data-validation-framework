"""MIG-04: SCD Type 2 history checks, in portable SQL with window functions.

The SQL uses ``ROW_NUMBER``, ``LEAD``, and windowed ``SUM``/``COUNT`` in a derived table and
aggregates outside it, which DuckDB, SQLite (3.25+), PostgreSQL, MySQL 8, SQL Server, Oracle,
Snowflake, BigQuery, and Spark all accept. Conditions are written as ``CASE`` expressions so no
boolean values appear in select lists.
"""

from __future__ import annotations

from typing import Any

from dvf.checks.context import CheckContext, CheckOutcome
from dvf.checks.pair import EnvironmentAware, Workspace, WorkspaceInput
from dvf.config.base import parse_duration
from dvf.core.enums import Dimension
from dvf.core.errors import PlanError
from dvf.normalize.canonical import single_spec
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import (
    as_int,
    flag_condition,
    real_columns,
    resolve_locator,
    rules_outcome,
)
from dvf.packs.migration.windows import bound_literal, parse_bound
from dvf.plugins.base import Check

LABELS = {
    "null_valid_from": "rows without valid_from",
    "invalid_range": "rows with valid_from not before valid_to",
    "no_current": "business keys without a current row",
    "multiple_current": "business keys with more than one current row",
    "overlap": "overlapping validity ranges",
    "gap": "gaps between validity ranges",
    "flag_inconsistent": "rows whose current flag contradicts valid_to",
    "history_count": "business keys whose version count differs from the history",
}


def scd2_sql(ctx: CheckContext, config: cfg.Scd2Check) -> tuple[str, list[str]]:
    """The per-row SQL: business key, validity, current marker, and one ``r_<rule>`` column per
    rule (1 when the row violates it). Returns the SQL and the rules it evaluates."""
    d = ctx.dialect
    keys = [ctx.col(k) for k in config.business_key]
    vf, vt = ctx.col(config.valid_from), ctx.col(config.valid_to)
    open_end = f"{vt} IS NULL"
    if config.open_end is not None:
        open_end = (
            f"({vt} IS NULL OR {vt} >= {bound_literal(parse_bound(config.open_end, ctx.run), d)})"
        )
    open_int = f"(CASE WHEN {open_end} THEN 1 ELSE 0 END)"
    if config.current_flag is not None:
        current = flag_condition(config.current_flag, ctx.col, d, ctx.render)
        current_int = f"(CASE WHEN {current} THEN 1 ELSE 0 END)"
    else:
        current_int = open_int
    base_items = [f"{k} AS b{i}" for i, k in enumerate(keys)]
    base_items += [f"{vf} AS vf", f"{vt} AS vt", f"{open_int} AS is_open", f"{current_int} AS cur"]
    base = f"SELECT {', '.join(base_items)}\n{ctx.from_where()}"
    bparts = ", ".join(f"b{i}" for i in range(len(keys)))
    # NULLs sort first in MySQL and SQL Server and last elsewhere: order them explicitly, with
    # open-ended versions after closed ones that start on the same day.
    order = "(CASE WHEN vf IS NULL THEN 1 ELSE 0 END), vf, is_open, vt"
    windowed = (
        f"SELECT dvf_b.*, "
        f"ROW_NUMBER() OVER (PARTITION BY {bparts} ORDER BY {order}) AS dvf_rn, "
        f"LEAD(vf) OVER (PARTITION BY {bparts} ORDER BY {order}) AS next_from, "
        f"SUM(cur) OVER (PARTITION BY {bparts}) AS currents\n"
        f"FROM ({base}) dvf_b"
    )
    inclusive = config.end == "inclusive"
    closed = "is_open = 0 AND vt IS NOT NULL"
    rules = {
        "null_valid_from": "vf IS NULL",
        "invalid_range": f"{closed} AND vf IS NOT NULL AND vf {'>' if inclusive else '>='} vt",
        "no_current": "dvf_rn = 1 AND currents = 0",
        "multiple_current": "dvf_rn = 1 AND currents > 1",
        "overlap": (
            f"next_from IS NOT NULL AND (is_open = 1 OR next_from {'<=' if inclusive else '<'} vt)"
        ),
    }
    if not config.allow_gaps:
        if inclusive:
            step = parse_duration(config.granularity).total_seconds() * 1_000_000
            gap = f"{_epoch(ctx, config.valid_from, 'next_from')} - {_epoch(ctx, config.valid_to, 'vt')} > {step!r}"
        else:
            gap = "next_from > vt"
        rules["gap"] = f"next_from IS NOT NULL AND {closed} AND {gap}"
    if config.current_flag is not None:
        rules["flag_inconsistent"] = "cur <> is_open"
    flags = ", ".join(
        f"(CASE WHEN {cond} THEN 1 ELSE 0 END) AS r_{name}" for name, cond in rules.items()
    )
    sql = f"SELECT dvf_w.*, {flags}\nFROM ({windowed}) dvf_w"
    return sql, list(rules)


def _epoch(ctx: CheckContext, column: str, expr: str) -> str:
    assert ctx.schema is not None
    found = ctx.schema.column(column)
    tz_aware = found is not None and single_spec(found.type, ctx.normalization).tz_aware
    return ctx.dialect.epoch_us(expr, tz_aware=tz_aware, timezone="UTC")


class Scd2(EnvironmentAware, Check):
    """SCD Type 2 history is well formed: one current row per business key, valid and
    non-overlapping validity ranges, optionally no gaps, and reconciled version counts (MIG-04)."""

    dimension = Dimension.CONSISTENCY
    Config = cfg.Scd2Check

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.Scd2Check)
        sql, rules = scd2_sql(ctx, config)
        sums = ", ".join(f"COALESCE(SUM(r_{r}), 0) AS {r}" for r in rules)
        row = ctx.row(
            f"SELECT COUNT(*) AS total_count, "
            f"COALESCE(SUM(CASE WHEN dvf_rn = 1 THEN 1 ELSE 0 END), 0) AS key_count, {sums}\n"
            f"FROM ({sql}) dvf_r"
        )
        total = as_int(row.get("total_count"))
        counts: dict[str, int] = {r: as_int(row.get(r)) for r in rules}
        samples = extract = None
        if any(counts.values()):
            d = ctx.dialect
            label = " ".join(f"WHEN r_{r} = 1 THEN {d.literal(r)}" for r in rules)
            any_rule = " OR ".join(f"r_{r} = 1" for r in rules)
            keys = ", ".join(f"b{i} AS {d.quote(k)}" for i, k in enumerate(config.business_key))
            samples, extract = ctx.evidence(
                f"SELECT {keys}, vf AS {d.quote(config.valid_from)}, vt AS {d.quote(config.valid_to)}, "
                f"CASE {label} END AS dvf_rule\nFROM ({sql}) dvf_r\nWHERE {any_rule}"
            )
        details: dict[str, Any] = {"business_keys": as_int(row.get("key_count"))}
        if config.history is not None:
            counts["history_count"], details["history_keys"] = self._history(ctx, config)
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

    def _history(self, ctx: CheckContext, config: cfg.Scd2Check) -> tuple[int, int]:
        """Business keys whose number of versions differs from the reference history."""
        assert config.history is not None
        ref = config.history
        conn, rel, schema = resolve_locator(
            ctx.connectors, ctx.relation.connection, ref, ctx.render
        )
        ref_keys = real_columns(schema, ref.keys, "the history dataset")
        if len(ref_keys) != len(config.business_key):
            raise PlanError(
                "'history.keys' and 'business_key' must have the same number of columns."
            )
        rd, d = conn.sql_dialect, ctx.dialect
        n = len(ref_keys)
        rk = ", ".join(f"{rd.to_text(rd.quote(c))} AS k{i}" for i, c in enumerate(ref_keys))
        rg = ", ".join(rd.to_text(rd.quote(c)) for c in ref_keys)
        ref_sql = f"SELECT {rk}, COUNT(*) AS n FROM {rel.sql} GROUP BY {rg}"
        own = [ctx.col(k) for k in config.business_key]
        ok = ", ".join(f"{d.to_text(c)} AS k{i}" for i, c in enumerate(own))
        og = ", ".join(d.to_text(c) for c in own)
        own_sql = f"SELECT {ok}, COUNT(*) AS n\n{ctx.from_where()}\nGROUP BY {og}"
        environment = self.environment
        inputs = {
            "hist_ref": WorkspaceInput(conn, ref_sql, ctx.side),
            "hist_ver": WorkspaceInput(ctx.connector, own_sql, ctx.side),
        }
        resources = environment.resources if environment is not None else None
        with Workspace(inputs, recorder=ctx.recorder, resources=resources) as ws:
            join = " AND ".join(f"r.k{i} = v.k{i}" for i in range(n))
            back = " AND ".join(f"v2.k{i} = r2.k{i}" for i in range(n))
            # Keys in both with different counts or only in the checked data, then keys only in
            # the reference (no FULL OUTER JOIN: MySQL has none).
            differing = (
                f"SELECT COUNT(*) AS n FROM {ws.table('hist_ver', 'v')} "
                f"LEFT JOIN {ws.table('hist_ref', 'r')} ON {join} "
                f"WHERE r.k0 IS NULL OR r.n <> v.n"
            )
            missing = (
                f"SELECT COUNT(*) AS n FROM {ws.table('hist_ref', 'r2')} WHERE NOT EXISTS "
                f"(SELECT 1 FROM {ws.table('hist_ver', 'v2')} WHERE {back})"
            )
            count = as_int(ws.scalar(differing)) + as_int(ws.scalar(missing))
            keys = as_int(ws.scalar(f"SELECT COUNT(*) AS n FROM {ws.table('hist_ref')}"))
        return count, keys


__all__ = ["LABELS", "Scd2", "scd2_sql"]
