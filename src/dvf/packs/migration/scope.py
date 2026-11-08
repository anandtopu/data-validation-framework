"""MIG-01: scope and exclusion reconciliation."""

from __future__ import annotations

from dvf.checks.context import CheckOutcome
from dvf.checks.pair import PairContext, WorkspaceInput
from dvf.core.enums import Dimension, Side
from dvf.models.results import MetricComparison, SampleSet
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int
from dvf.plugins.base import PairCheck


class ScopeReconciliation(PairCheck):
    """Source rows reconcile to migrated rows plus rows excluded by design (MIG-01).

    ``source total = migrated (target rows) + excluded (source rows matching excluded_where)``,
    within a tolerance; the excluded count can be held to an exact value or a range, and with
    ``excluded_absent`` no excluded row may have reached the target.
    """

    dimension = Dimension.RECONCILIATION
    Config = cfg.ScopeReconciliationCheck

    def evaluate_pair(self, ctx: PairContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.ScopeReconciliationCheck)
        src = ctx.source
        dialect = src.dialect
        excluded_where = ctx.render(config.excluded_where)
        relation_sql = src.relation.sql
        if config.population == "unfiltered":
            filters = ctx.row_filters(Side.SOURCE)
            relation_sql = src.relation.base_sql
            if filters:
                predicate = " AND ".join(f"({f})" for f in filters)
                relation_sql = dialect.subquery(
                    f"SELECT * FROM {relation_sql} WHERE {predicate}", "dvf_scope"
                )
        row = ctx.row(
            Side.SOURCE,
            f"SELECT COUNT(*) AS total_count, "
            f"{dialect.count_if(f'({excluded_where})')} AS excluded_count\n"
            f"{ctx.from_where(Side.SOURCE, relation_sql=relation_sql)}",
        )
        total = as_int(row.get("total_count"))
        excluded = as_int(row.get("excluded_count"))
        migrated = ctx.count(Side.TARGET)
        unaccounted = total - excluded - migrated
        allowed = max(float(config.tolerance_rows), total * config.tolerance_pct / 100.0)
        balance_ok = abs(unaccounted) <= allowed

        expected = config.expected_excluded
        expected_text: str | None = None
        excluded_ok = True
        if isinstance(expected, int):
            expected_text = f"{expected:,}"
            excluded_ok = excluded == expected
        elif expected is not None:
            bounds = [
                f">= {expected.min:,}" if expected.min is not None else "",
                f"<= {expected.max:,}" if expected.max is not None else "",
            ]
            expected_text = " and ".join(b for b in bounds if b)
            excluded_ok = (expected.min is None or excluded >= expected.min) and (
                expected.max is None or excluded <= expected.max
            )

        metrics = [
            MetricComparison(
                metric="scope_balance",
                source=total,
                target=migrated + excluded,
                difference=float(-unaccounted),
                tolerance=allowed or None,
                passed=balance_ok,
                note="source rows = migrated (target) rows + excluded rows",
            ),
            MetricComparison(
                metric="excluded_rows",
                source=excluded,
                passed=excluded_ok,
                note=f"expected {expected_text}" if expected_text else None,
            ),
            MetricComparison(metric="migrated_rows", target=migrated, passed=True),
        ]
        present = 0
        samples: SampleSet | None = None
        extract = None
        if config.excluded_absent:
            present, samples, extract = self._excluded_in_target(ctx, relation_sql, excluded_where)
            metrics.append(
                MetricComparison(
                    metric="excluded_rows_in_target",
                    target=present,
                    passed=present == 0,
                    note="excluded source keys that exist in the target",
                )
            )

        parts = [
            f"source {total:,} rows = migrated {migrated:,} + excluded {excluded:,}"
            if unaccounted == 0
            else f"source {total:,} rows, migrated {migrated:,} + excluded {excluded:,} = "
            f"{migrated + excluded:,}: {abs(unaccounted):,} rows "
            + ("unaccounted for" if unaccounted > 0 else "too many in the target")
        ]
        if unaccounted and balance_ok:
            parts[0] += f" (within tolerance {allowed:g})"
        if expected_text and not excluded_ok:
            parts.append(f"excluded {excluded:,} rows, expected {expected_text}")
        if present:
            parts.append(f"{present:,} excluded rows exist in the target")
        hints: list[str] = []
        if unaccounted > 0 and not balance_ok:
            hints.append(
                "Rows in scope did not reach the target: compare with full_diff, or check that "
                "'excluded_where' covers every exclusion rule."
            )
        elif unaccounted < 0 and not balance_ok:
            hints.append(
                "The target holds more rows than the in-scope source: look for duplicates or rows "
                "migrated although excluded."
            )
        return CheckOutcome(
            passed=balance_ok and excluded_ok and present == 0,
            message="; ".join(parts),
            observed=unaccounted,
            expected=0 if not allowed else f"0 ± {allowed:g}",
            unexpected_count=abs(unaccounted) + present,
            total_count=total,
            metrics=metrics,
            samples=samples,
            extract=extract,
            hints=hints,
            details={
                "source_rows": total,
                "excluded_rows": excluded,
                "migrated_rows": migrated,
                "unaccounted_rows": unaccounted,
                "expected_excluded": expected_text,
                "excluded_rows_in_target": present if config.excluded_absent else None,
                "population": config.population,
            },
        )

    @staticmethod
    def _excluded_in_target(
        ctx: PairContext, relation_sql: str, excluded_where: str
    ) -> tuple[int, SampleSet | None, object]:
        plan = ctx.plan
        names = ctx.key_names()
        src_keys = ", ".join(f"{e} AS k{i}" for i, e in enumerate(ctx.key_exprs(Side.SOURCE)))
        tgt_keys = ", ".join(f"{e} AS k{i}" for i, e in enumerate(ctx.key_exprs(Side.TARGET)))
        inputs = {
            "src": WorkspaceInput(
                ctx.source.connector,
                f"SELECT DISTINCT {src_keys}\n"
                f"{ctx.from_where(Side.SOURCE, f'({excluded_where})', relation_sql=relation_sql)}",
                Side.SOURCE,
            ),
            "tgt": WorkspaceInput(
                ctx.target.connector,
                f"SELECT DISTINCT {tgt_keys}\n{ctx.from_where(Side.TARGET)}",
                Side.TARGET,
            ),
        }
        with ctx.workspace(inputs) as ws:
            join = " AND ".join(f"t.{k} = s.{k}" for k in names)
            present_sql = (
                f"SELECT {', '.join(f's.{k} AS {k}' for k in names)} FROM {ws.table('src', 's')}\n"
                f"WHERE EXISTS (SELECT 1 FROM {ws.table('tgt', 't')} WHERE {join})"
            )
            present = as_int(ws.scalar(f"SELECT COUNT(*) AS n FROM ({present_sql}) dvf_present"))
            samples = extract = None
            if present:
                labels = ", ".join(
                    f"{k} AS {ws.dialect.quote(key.label)}"
                    for k, key in zip(names, plan.keys, strict=True)
                )
                samples, extract = ctx.evidence(
                    ws, f"SELECT {labels} FROM ({present_sql}) dvf_present"
                )
        return present, samples, extract
