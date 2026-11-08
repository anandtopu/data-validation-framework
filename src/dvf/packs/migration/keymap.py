"""MIG-06: surrogate and key map validation."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from dvf.checks.context import CheckOutcome
from dvf.checks.pair import PairContext, Workspace, WorkspaceInput, masked_evidence
from dvf.core.enums import Dimension, Side
from dvf.core.errors import PlanError
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int, real_columns, resolve_locator, rules_outcome
from dvf.plugins.base import PairCheck

LABELS = {
    "map_null_key": "key map entries with an empty legacy or new key",
    "legacy_multi": "legacy keys mapped to more than one new key",
    "new_multi": "new keys mapped from more than one legacy key",
    "unmapped_source": "source keys without a mapping",
    "unknown_target": "target keys missing from the key map",
    "dangling": "key map entries whose new key is not in the target",
}


class KeyMap(PairCheck):
    """Every legacy key maps to exactly one new key, the reverse where required, and the map
    covers every source key and every target key (MIG-06).

    The key map is any table or query (``map``), on any connection. Keys are compared as text.
    """

    dimension = Dimension.REFERENTIAL_INTEGRITY
    Config = cfg.KeyMapCheck

    def evaluate_pair(self, ctx: PairContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.KeyMapCheck)
        ref = config.map
        map_conn, map_rel, map_schema = resolve_locator(
            ctx.connectors, ctx.target.relation.connection, ref, ctx.render
        )
        legacy = real_columns(map_schema, ref.legacy_columns, "the key map")
        new = real_columns(map_schema, ref.new_columns, "the key map")
        source_key = [ctx.source.column(c) for c in (config.source_key or ctx.source.key_columns)]
        target_key = [ctx.target.column(c) for c in (config.target_key or ctx.target.key_columns)]
        if config.complete_source and len(source_key) != len(legacy):
            raise PlanError(
                f"The key map has {len(legacy)} legacy column(s) but the source key has "
                f"{len(source_key)}.",
                hint="Set 'source_key' to the legacy key columns of the source dataset.",
            )
        if (config.complete_target or config.dangling) and len(target_key) != len(new):
            raise PlanError(
                f"The key map has {len(new)} new key column(s) but the target key has "
                f"{len(target_key)}.",
                hint="Set 'target_key' to the new key columns of the target dataset.",
            )
        md = map_conn.sql_dialect
        items = [f"{md.to_text(md.quote(c))} AS l{i}" for i, c in enumerate(legacy)]
        items += [f"{md.to_text(md.quote(c))} AS n{j}" for j, c in enumerate(new)]
        inputs = {
            "kmap": WorkspaceInput(
                map_conn, f"SELECT DISTINCT {', '.join(items)} FROM {map_rel.sql}"
            )
        }
        if config.complete_source:
            sd = ctx.source.dialect
            cols = ", ".join(f"{sd.to_text(sd.quote(c))} AS l{i}" for i, c in enumerate(source_key))
            nn = " AND ".join(f"{sd.quote(c)} IS NOT NULL" for c in source_key)
            inputs["src"] = WorkspaceInput(
                ctx.source.connector,
                f"SELECT DISTINCT {cols}\n{ctx.from_where(Side.SOURCE, nn)}",
                Side.SOURCE,
            )
        if config.complete_target or config.dangling:
            td = ctx.target.dialect
            cols = ", ".join(f"{td.to_text(td.quote(c))} AS n{j}" for j, c in enumerate(target_key))
            nn = " AND ".join(f"{td.quote(c)} IS NOT NULL" for c in target_key)
            inputs["tgt"] = WorkspaceInput(
                ctx.target.connector,
                f"SELECT DISTINCT {cols}\n{ctx.from_where(Side.TARGET, nn)}",
                Side.TARGET,
            )
        with ctx.workspace(inputs) as ws:
            return self._rules(ctx, ws, config, len(legacy), len(new))

    def _rules(
        self, ctx: PairContext, ws: Workspace, config: cfg.KeyMapCheck, nl: int, nn: int
    ) -> CheckOutcome:
        ls = [f"l{i}" for i in range(nl)]
        ns = [f"n{j}" for j in range(nn)]
        lcols, ncols = ", ".join(ls), ", ".join(ns)
        new_not_null = " AND ".join(f"{n} IS NOT NULL" for n in ns)
        legacy_not_null = " AND ".join(f"{c} IS NOT NULL" for c in ls)
        any_null = " OR ".join(f"{c} IS NULL" for c in [*ls, *ns])
        mp = ws.table("kmap")
        queries: dict[str, str] = {
            "map_null_key": f"SELECT {lcols}, {ncols} FROM {mp} WHERE {any_null}",
            "legacy_multi": (
                f"SELECT {lcols}, COUNT(*) AS new_keys FROM {mp} "
                f"WHERE {legacy_not_null} AND {new_not_null} GROUP BY {lcols} HAVING COUNT(*) > 1"
            ),
        }
        if config.reverse:
            queries["new_multi"] = (
                f"SELECT {ncols}, COUNT(*) AS legacy_keys FROM {mp} "
                f"WHERE {legacy_not_null} AND {new_not_null} GROUP BY {ncols} HAVING COUNT(*) > 1"
            )
        if config.complete_source:
            match = " AND ".join(f"m.{c} = s.{c}" for c in ls)
            queries["unmapped_source"] = (
                f"SELECT {', '.join(f's.{c} AS {c}' for c in ls)} FROM {ws.table('src', 's')} "
                f"WHERE NOT EXISTS (SELECT 1 FROM {ws.table('kmap', 'm')} WHERE {match})"
            )
        if config.complete_target:
            match = " AND ".join(f"m.{c} = t.{c}" for c in ns)
            queries["unknown_target"] = (
                f"SELECT {', '.join(f't.{c} AS {c}' for c in ns)} FROM {ws.table('tgt', 't')} "
                f"WHERE NOT EXISTS (SELECT 1 FROM {ws.table('kmap', 'm')} WHERE {match})"
            )
        if config.dangling:
            match = " AND ".join(f"t.{c} = m.{c}" for c in ns)
            queries["dangling"] = (
                f"SELECT DISTINCT {', '.join(f'm.{c} AS {c}' for c in ns)} "
                f"FROM {ws.table('kmap', 'm')} WHERE {' AND '.join(f'm.{c} IS NOT NULL' for c in ns)} "
                f"AND NOT EXISTS (SELECT 1 FROM {ws.table('tgt', 't')} WHERE {match})"
            )
        counts: dict[str, int] = {}
        sample_rows: list[dict[str, Any]] = []
        per_rule = max(1, ctx.sample_size // max(len(queries), 1))
        for rule, sql in queries.items():
            counts[rule] = as_int(ws.scalar(f"SELECT COUNT(*) AS n FROM ({sql}) dvf_q"))
            if counts[rule] and ctx.sample_size > 0 and not ctx.privacy.strict:
                for row in ws.rows(ws.dialect.limit(sql, per_rule)):
                    sample_rows.append({"rule": rule, **row})
        details: dict[str, Any] = {
            "map_entries": as_int(ws.scalar(f"SELECT COUNT(*) AS n FROM {mp}")),
        }
        if "src" in ws.inputs:
            details["source_keys"] = as_int(
                ws.scalar(f"SELECT COUNT(*) AS n FROM {ws.table('src')}")
            )
        if "tgt" in ws.inputs:
            details["target_keys"] = as_int(
                ws.scalar(f"SELECT COUNT(*) AS n FROM {ws.table('tgt')}")
            )
        samples = None
        if sample_rows:
            legacy_names = [f"legacy.{c}" for c in config.map.legacy_columns]
            new_names = [f"new.{c}" for c in config.map.new_columns]
            names = ["rule", *legacy_names, *new_names, "count"]
            rows = [
                [
                    r["rule"],
                    *(r.get(c) for c in ls),
                    *(r.get(c) for c in ns),
                    r.get("new_keys", r.get("legacy_keys")),
                ]
                for r in sample_rows
            ]
            data = {
                name: pa.array(
                    [None if row[i] is None else str(row[i]) for row in rows], pa.string()
                )
                for i, name in enumerate(names)
            }
            samples, _ = masked_evidence(pa.table(data), ctx.sample_size, 0, ctx.privacy)
        return rules_outcome(
            counts,
            LABELS,
            total=details["map_entries"],
            mostly=config.mostly,
            unit="key map entries",
            samples=samples,
            details=details,
        )


__all__ = ["KeyMap"]
