"""MIG-12: default and sentinel substitutions verified explicitly, not masked by normalization."""

from __future__ import annotations

from dataclasses import dataclass

from dvf.checks.context import CheckOutcome
from dvf.checks.pair import PairColumn, PairContext
from dvf.core.enums import Dimension
from dvf.models.results import MetricComparison
from dvf.packs.migration import config as cfg
from dvf.packs.migration._common import as_int, rules_outcome
from dvf.plugins.base import PairCheck
from dvf.sql.dialects import Dialect


@dataclass(frozen=True)
class _Planned:
    index: int
    rule: cfg.SubstitutionRule
    key: str
    target_column: str


def _source_condition(rule: cfg.SubstitutionRule, value: str, d: Dialect) -> str:
    parts: list[str] = []
    if rule.source_null:
        parts.append(f"{value} IS NULL")
    if rule.source_blank:
        parts.append(f"({value} IS NOT NULL AND {d.length(d.trim(value))} = 0)")
    if rule.source_values:
        parts.append(f"{value} IN ({', '.join(d.literal(str(v)) for v in rule.source_values)})")
    return "(" + " OR ".join(parts) + ")"


def _target_condition(rule: cfg.SubstitutionRule, value: str, d: Dialect) -> str:
    if rule.target_null:
        return f"{value} IS NULL"
    return f"{value} = {d.literal(str(rule.target_value))}"


def _natural(rule: cfg.SubstitutionRule, source: str, target: str) -> str:
    """The target value is the source value itself (a NULL kept, or a source 'N/A' as is)."""
    if rule.target_null:
        return f"{source} IS NULL"
    return f"{source} = {target}"


class SubstitutionRules(PairCheck):
    """Default and sentinel substitutions are applied exactly as specified (MIG-12).

    For each rule, rows whose source matches the rule's condition must hold the rule's target
    value (or NULL). With ``unmatched_sentinels``, target rows holding a rule's sentinel value
    whose source matches no rule for that outcome, and is not the sentinel itself, are reported
    too: they reveal substitutions nobody specified. Values are compared as text, on rows paired
    by key.
    """

    dimension = Dimension.ACCURACY
    Config = cfg.SubstitutionRulesCheck
    requires_keys = True

    def evaluate_pair(self, ctx: PairContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.SubstitutionRulesCheck)
        sd, td = ctx.source.dialect, ctx.target.dialect
        planned: list[_Planned] = []
        columns: list[PairColumn] = []
        for i, rule in enumerate(config.rules):
            source_col = ctx.source.col(rule.column)
            target_name = rule.target_column
            if target_name is None:
                pair = ctx.comparison.pairing.target_for_source(rule.column)
                target_name = pair.target.name if pair is not None else rule.column
            target_col = ctx.target.col(target_name)
            s_expr, t_expr = sd.to_text(source_col), td.to_text(target_col)
            if config.trim:
                s_expr, t_expr = sd.trim(s_expr), td.trim(t_expr)
            columns.append(PairColumn(f"v{i}", source=s_expr, target=t_expr))
            key = f"rule_{rule.name or i + 1}"
            planned.append(_Planned(i, rule, key, ctx.target.column(target_name)))
        names = ctx.key_names()
        with ctx.keyed(columns) as ws:
            d = ws.dialect
            join = " AND ".join(f"t.{k} = s.{k}" for k in names)
            items = ["COUNT(*) AS total_count"]
            # Rule key to (SQL that is 1 for a failing row, index of its value columns).
            flags: dict[str, tuple[str, int]] = {}
            for p in planned:
                s, t = f"s.v{p.index}", f"t.v{p.index}"
                cond_s = _source_condition(p.rule, s, d)
                cond_t = _target_condition(p.rule, t, d)
                flags[p.key] = (
                    f"(CASE WHEN {cond_s} THEN CASE WHEN {cond_t} THEN 0 ELSE 1 END ELSE 0 END)",
                    p.index,
                )
                items.append(f"{d.count_if(cond_s)} AS a_{p.index}")
                items.append(f"SUM({flags[p.key][0]}) AS x_{p.index}")
            unmatched: dict[str, str] = {}
            if config.unmatched_sentinels:
                groups: dict[tuple[str, str], list[_Planned]] = {}
                for p in planned:
                    outcome = "NULL" if p.rule.target_null else repr(str(p.rule.target_value))
                    groups.setdefault((p.target_column, outcome), []).append(p)
                for g, ((column, outcome), members) in enumerate(groups.items(), start=1):
                    first = members[0]
                    t = f"t.v{first.index}"
                    cond_t = _target_condition(first.rule, t, d)
                    covered = " OR ".join(
                        _source_condition(m.rule, f"s.v{m.index}", d) for m in members
                    )
                    natural = _natural(first.rule, f"s.v{first.index}", t)
                    key = f"unmatched_{g}"
                    flags[key] = (
                        f"(CASE WHEN {cond_t} THEN CASE WHEN ({covered}) OR ({natural}) "
                        "THEN 0 ELSE 1 END ELSE 0 END)",
                        first.index,
                    )
                    unmatched[key] = f"{column} = {outcome}"
                    items.append(f"SUM({flags[key][0]}) AS {key}")
            joined = f"FROM {ws.table('src', 's')} JOIN {ws.table('tgt', 't')} ON {join}"
            row = ws.row(f"SELECT {', '.join(items)}\n{joined}")
            total = as_int(row.get("total_count"))
            counts: dict[str, int] = {}
            labels: dict[str, str] = {}
            metrics: list[MetricComparison] = []
            per_rule: dict[str, dict[str, int]] = {}
            for p in planned:
                applicable = as_int(row.get(f"a_{p.index}"))
                wrong = as_int(row.get(f"x_{p.index}"))
                counts[p.key] = wrong
                labels[p.key] = f"rows violating {p.rule.describe()}"
                per_rule[p.key] = {"applicable": applicable, "violations": wrong}
                metrics.append(
                    MetricComparison(
                        metric="substitution",
                        column=p.target_column,
                        source_column=p.rule.column,
                        source=applicable,
                        target=applicable - wrong,
                        difference=float(-wrong),
                        passed=wrong == 0,
                        note=f"{p.rule.describe()}: source rows matching, target rows correct",
                    )
                )
            for key, text in unmatched.items():
                n = as_int(row.get(key))
                counts[key] = n
                labels[key] = f"target rows with {text} and no matching source rule"
                metrics.append(
                    MetricComparison(
                        metric="unmatched_sentinel",
                        column=text.split(" = ")[0],
                        target=n,
                        passed=n == 0,
                        note=f"{text} without a matching source condition",
                    )
                )
            samples = extract = None
            failing = {k: flag for k, flag in flags.items() if counts.get(k)}
            if failing:
                label = " ".join(
                    f"WHEN {f} = 1 THEN {d.literal(k)}" for k, (f, _) in failing.items()
                )
                source_value = " ".join(f"WHEN {f} = 1 THEN s.v{i}" for f, i in failing.values())
                target_value = " ".join(f"WHEN {f} = 1 THEN t.v{i}" for f, i in failing.values())
                keys = ", ".join(
                    f"s.{k} AS {d.quote(key.label)}"
                    for k, key in zip(names, ctx.plan.keys, strict=True)
                )
                any_failing = " OR ".join(f"{f} = 1" for f, _ in failing.values())
                samples, extract = ctx.evidence(
                    ws,
                    f"SELECT {keys}, CASE {label} END AS dvf_rule, "
                    f"CASE {source_value} END AS source_value, "
                    f"CASE {target_value} END AS target_value\n{joined}\nWHERE {any_failing}",
                )
        return rules_outcome(
            counts,
            labels,
            total=total,
            mostly=config.mostly,
            unit="paired rows",
            samples=samples,
            extract=extract,
            metrics=metrics,
            details={"rules": per_rule},
        )


__all__ = ["SubstitutionRules"]
