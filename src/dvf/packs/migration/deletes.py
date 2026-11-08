"""MIG-05: soft and hard delete propagation."""

from __future__ import annotations

from dvf.checks.context import CheckOutcome
from dvf.checks.pair import PairColumn, PairContext
from dvf.core.enums import Dimension
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int, flag_condition, flag_int, rules_outcome
from dvf.plugins.base import PairCheck

LABELS = {
    "hard_delete_not_propagated": "target rows whose key no longer exists in the source",
    "soft_delete_not_propagated": "soft-deleted source rows not flagged as deleted in the target",
    "wrongly_deleted": "active source rows flagged as deleted in the target",
    "soft_deleted_present": "soft-deleted source rows present in the target",
    "soft_deleted_missing": "soft-deleted source rows missing from the target",
}


class DeletePropagation(PairCheck):
    """Source deletes are reflected in the target (MIG-05).

    * Hard deletes: target rows whose key is gone from the source must not stay active (rows
      flagged deleted by ``target_flag`` count as propagated).
    * Soft deletes: a source row flagged by ``source_flag`` must be flagged in the target, and
      an active source row must not be; ``soft_deleted_rows`` says whether soft-deleted rows
      must be ``flagged`` in the target, ``absent`` from it, or ``either``.
    """

    dimension = Dimension.CONSISTENCY
    Config = cfg.DeletePropagationCheck
    requires_keys = True

    def evaluate_pair(self, ctx: PairContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.DeletePropagationCheck)
        src, tgt = ctx.source, ctx.target
        s_del = (
            flag_int(flag_condition(config.source_flag, src.col, src.dialect, ctx.render))
            if config.source_flag is not None
            else "0"
        )
        t_del = (
            flag_int(flag_condition(config.target_flag, tgt.col, tgt.dialect, ctx.render))
            if config.target_flag is not None
            else "0"
        )
        names = ctx.key_names()
        with ctx.keyed([PairColumn("v_del", source=s_del, target=t_del)]) as ws:
            join = " AND ".join(f"t.{k} = s.{k}" for k in names)
            back = " AND ".join(f"s2.{k} = t.{k}" for k in names)
            whens: list[str] = []
            policy = config.soft_deleted_rows
            if policy == "absent":
                whens.append("WHEN s.v_del = 1 AND t.k0 IS NOT NULL THEN 'soft_deleted_present'")
            if policy == "flagged":
                whens.append("WHEN s.v_del = 1 AND t.k0 IS NULL THEN 'soft_deleted_missing'")
            if config.target_flag is not None:
                whens.append(
                    "WHEN s.v_del = 1 AND t.k0 IS NOT NULL AND t.v_del = 0 "
                    "THEN 'soft_delete_not_propagated'"
                )
                whens.append("WHEN s.v_del = 0 AND t.v_del = 1 THEN 'wrongly_deleted'")
            rule = f"CASE {' '.join(whens)} END" if whens else "CAST(NULL AS VARCHAR(40))"
            keys = ", ".join(f"s.{k} AS {k}" for k in names)
            parts = [
                f"SELECT {keys}, {rule} AS dvf_rule\n"
                f"FROM {ws.table('src', 's')} LEFT JOIN {ws.table('tgt', 't')} ON {join}"
            ]
            if config.hard_deletes:
                tkeys = ", ".join(f"t.{k} AS {k}" for k in names)
                parts.append(
                    f"SELECT {tkeys}, CASE WHEN COALESCE(t.v_del, 0) = 0 "
                    "THEN 'hard_delete_not_propagated' END AS dvf_rule\n"
                    f"FROM {ws.table('tgt', 't')}\n"
                    f"WHERE NOT EXISTS (SELECT 1 FROM {ws.table('src', 's2')} WHERE {back})"
                )
            classified = "\nUNION ALL\n".join(parts)
            counts = {r: 0 for r in LABELS if self._applies(r, config)}
            total = 0
            for row in ws.rows(
                f"SELECT dvf_rule, COUNT(*) AS n FROM ({classified}) dvf_x GROUP BY dvf_rule"
            ):
                n = as_int(row["n"])
                total += n
                if row["dvf_rule"] is not None:
                    counts[str(row["dvf_rule"])] = n
            samples = extract = None
            if any(counts.values()):
                labels = ", ".join(
                    f"{k} AS {ws.dialect.quote(key.label)}"
                    for k, key in zip(names, ctx.plan.keys, strict=True)
                )
                samples, extract = ctx.evidence(
                    ws,
                    f"SELECT {labels}, dvf_rule FROM ({classified}) dvf_x "
                    "WHERE dvf_rule IS NOT NULL",
                )
        return rules_outcome(
            counts,
            LABELS,
            total=total,
            mostly=config.mostly,
            unit="keys",
            samples=samples,
            extract=extract,
            details={"soft_deleted_rows": policy},
        )

    @staticmethod
    def _applies(rule: str, config: cfg.DeletePropagationCheck) -> bool:
        if rule == "hard_delete_not_propagated":
            return config.hard_deletes
        if rule in ("soft_delete_not_propagated", "wrongly_deleted"):
            return config.target_flag is not None
        if rule == "soft_deleted_present":
            return config.soft_deleted_rows == "absent"
        return config.soft_deleted_rows == "flagged"


__all__ = ["DeletePropagation"]
