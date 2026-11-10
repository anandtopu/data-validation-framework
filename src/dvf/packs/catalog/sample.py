"""The ``sample`` comparison strategy (REC-07): a statistically documented sample comparison for
tables too large to compare in full.

Design
------
1. **Population.** Each side counts its rows with non-NULL keys (per stratum when
   ``stratify_by`` is set). The population of a stratum is the larger of the two counts.
2. **Sample size.** ``margin_of_error`` uses Cochran's formula with the finite population
   correction: ``n0 = z^2 p (1 - p) / e^2`` and ``n = n0 / (1 + (n0 - 1) / N)``, where ``z`` is the
   normal quantile of ``confidence`` and ``p`` is ``expected_rate`` (0.5, the most conservative,
   by default). ``rows`` and ``fraction`` give the size directly.
3. **Selection.** A key is in the sample when the 32-bit integer of
   ``MD5('dvf-sample:<seed>' | canonical key)`` (dvf's row hash of the seed and the canonical key
   fields) is below ``f * 2^32``, where ``f`` is its stratum's sampling fraction. The hash is the
   same in every dialect (the parity tests prove the row hash), so both sides select exactly the
   same keys without exchanging them, and a seed always selects the same keys. This is Bernoulli
   sampling: the realized size varies around the target by about its square root, and the
   estimates use the realized size.
4. **Stratification.** Strata (the canonical values of ``stratify_by``) share the sampling
   fraction ``n / N`` (proportional allocation), except that ``min_per_stratum`` raises small
   strata. Raised fractions are rounded up to ``f * 2^k`` so they form a few *classes*; each class
   is compared with one keyed diff, and the estimate is stratified by class.
5. **Comparison.** The sampled rows of each class are diffed by key through the configured engine
   (``full`` mode: column mismatches with classification).
6. **Estimate.** With ``n_c`` sampled keys and ``x_c`` keys with differences (mismatched,
   missing, or extra) in class ``c``:
   ``p = sum(W_c p_c)`` with ``W_c = N_c / N`` and
   ``Var(p) = sum(W_c^2 (1 - f_c) p_c (1 - p_c) / (n_c - 1))``, where ``N_c = n_c / f_c`` is the
   Horvitz-Thompson estimate of the class's keys (both sides). The confidence interval is the
   Wilson score interval with the effective sample size ``p (1 - p) / Var(p)`` (Kish), which stays
   meaningful when no difference is found. A census (``f = 1``) gives an exact result.
   Extrapolated counts are ``sum(x_c / f_c)`` per kind of difference.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any

from dvf.checks.context import CheckOutcome, pct
from dvf.compare.buckets import keys_not_null_sql
from dvf.compare.classify import classify_pair, summarize
from dvf.compare.context import ComparisonContext
from dvf.compare.partitions import restricted
from dvf.compare.plan import ComparisonPlan, PlannedColumn
from dvf.config.models import SampleConfig
from dvf.core.enums import Dimension, Side, Status
from dvf.core.errors import PlanError
from dvf.engines.base import DiffJob, DiffResult, DiffSide, Engine, merge_results
from dvf.execution.context import QueryRecorder
from dvf.models.results import ColumnMismatch, SampleSet
from dvf.normalize.canonical import canonical_sql, resolve_pair, row_hash, row_hash_sql
from dvf.plugins.base import Comparator
from dvf.plugins.registry import PluginKind
from dvf.sql.dialects import HASH_SPACE, Dialect

DEFAULT_SAMPLE = SampleConfig(margin_of_error=0.01)
"""Used when the table selects ``sample`` without ``comparison.sample``."""

SEED_PREFIX = "dvf-sample:"
_MAX_CLASSES = 40


# ====================================================================== statistics


def z_value(confidence: float) -> float:
    """The two-sided standard normal quantile for ``confidence`` (1.96 for 0.95)."""
    return NormalDist().inv_cdf(0.5 + confidence / 2.0)


def cochran_size(
    population: int, margin: float, confidence: float, expected_rate: float = 0.5
) -> int:
    """Cochran's sample size with the finite population correction (at most ``population``)."""
    if population <= 0:
        return 0
    z = z_value(confidence)
    n0 = z * z * expected_rate * (1.0 - expected_rate) / (margin * margin)
    n = n0 / (1.0 + (n0 - 1.0) / population)
    return min(population, max(1, math.ceil(n)))


def wilson_interval(p: float, n_eff: float, confidence: float) -> tuple[float, float]:
    """The Wilson score interval of a proportion ``p`` with effective sample size ``n_eff``."""
    if n_eff <= 0:
        return 0.0, 1.0
    z = z_value(confidence)
    z2 = z * z
    denominator = 1.0 + z2 / n_eff
    center = (p + z2 / (2.0 * n_eff)) / denominator
    half = (z / denominator) * math.sqrt(max(0.0, p * (1.0 - p) / n_eff + z2 / (4.0 * n_eff**2)))
    # At p = 0 (or 1) the lower (upper) bound is exactly 0 (1); computed, it can be off by a
    # rounding error that depends on the platform's math library.
    low = 0.0 if p <= 0.0 else max(0.0, center - half)
    high = 1.0 if p >= 1.0 else min(1.0, center + half)
    return low, high


@dataclass
class ClassStats:
    """One sampling class: strata that share a sampling fraction."""

    fraction: float
    population: int
    sampled: int = 0
    differing: int = 0
    mismatched: int = 0
    missing_in_target: int = 0
    extra_in_target: int = 0


@dataclass
class Estimate:
    rate: float
    low: float
    high: float
    sampled: int
    differing: int
    population: int
    effective_size: float
    census: bool
    extrapolated: dict[str, int] = field(default_factory=dict)


def estimate(classes: list[ClassStats], confidence: float) -> Estimate:
    """The stratified estimate of the mismatch rate, its interval, and extrapolated counts.

    Each class's key population is estimated as ``n_c / f_c`` (Horvitz-Thompson: sampled keys
    of either side over the known inclusion probability), so missing and extra keys count, and a
    census is exact.
    """
    sampled = sum(c.sampled for c in classes)
    differing = sum(c.differing for c in classes)
    used = [c for c in classes if c.sampled > 0 and c.fraction > 0]
    sizes = {id(c): c.sampled / c.fraction for c in used}
    population = round(sum(sizes.values()))
    if not used or population <= 0:
        return Estimate(0.0, 0.0, 1.0, sampled, differing, population, 0.0, False)
    total = sum(sizes.values())
    rate = 0.0
    variance = 0.0
    census = all(c.fraction >= 1.0 for c in used)
    extrapolated: Counter[str] = Counter()
    for c in used:
        weight = sizes[id(c)] / total
        p_c = c.differing / c.sampled
        rate += weight * p_c
        fpc = max(0.0, 1.0 - c.fraction)
        variance += weight * weight * fpc * p_c * (1.0 - p_c) / max(1, c.sampled - 1)
        for kind in ("differing", "mismatched", "missing_in_target", "extra_in_target"):
            extrapolated[kind] += round(getattr(c, kind) / c.fraction)
    if census:
        return Estimate(
            rate,
            rate,
            rate,
            sampled,
            differing,
            population,
            float(sampled),
            True,
            dict(extrapolated),
        )
    if variance > 0:
        n_eff = min(float(sampled), rate * (1.0 - rate) / variance)
    else:  # no difference (or only differences) found: the design effect of the totals
        fpc = max(1e-12, 1.0 - sampled / total)
        n_eff = max(1.0, (sampled - 1) / fpc)
    low, high = wilson_interval(rate, n_eff, confidence)
    return Estimate(
        rate, low, high, sampled, differing, population, n_eff, False, dict(extrapolated)
    )


# ====================================================================== selection


def sample_hash(canonical_keys: list[str | None], seed: int) -> int:
    """The Python reference of the 32-bit sampling hash of a canonical key."""
    return int(row_hash([f"{SEED_PREFIX}{seed}", *canonical_keys])[:8], 16)


def threshold(fraction: float) -> int:
    """Keys whose sampling hash is below this are sampled (``fraction`` of the hash space)."""
    return min(HASH_SPACE, max(0, math.ceil(fraction * HASH_SPACE)))


def sample_hash_sql(keys: list[str], dialect: Dialect, seed: int) -> str:
    """SQL for :func:`sample_hash` over canonical key expressions."""
    return dialect.hash_int(
        row_hash_sql([dialect.literal(f"{SEED_PREFIX}{seed}"), *keys], dialect), 0
    )


def _side(col: PlannedColumn, side: str) -> Any:
    return col.source if side == "source" else col.target


def _dialect(plan: ComparisonPlan, side: str) -> Dialect:
    return plan.source_dialect if side == "source" else plan.target_dialect


@dataclass(frozen=True)
class Stratum:
    values: tuple[Any, ...]
    source_rows: int
    target_rows: int

    @property
    def population(self) -> int:
        return max(self.source_rows, self.target_rows)


@dataclass
class SampleClass:
    fraction: float
    strata: list[Stratum]
    default: bool = False


def _strata_exprs(
    ctx: ComparisonContext, plan: ComparisonPlan, names: list[str]
) -> list[tuple[str, str, str]]:
    """(label, source SQL, target SQL) of the canonical value of each ``stratify_by`` column."""
    found: list[tuple[str, str, str]] = []
    planned = {c.label.lower(): c for c in [*plan.keys, *plan.columns]}
    planned.update({c.source_label.lower(): c for c in [*plan.keys, *plan.columns]})
    for name in names:
        col = planned.get(name.lower())
        if col is not None:
            found.append((col.label, col.source.canonical, col.target.canonical))
            continue
        pair = ctx.pairing.pair_for_target(name) or ctx.pairing.target_for_source(name)
        if pair is None or pair.source is None:
            raise PlanError(
                f"stratify_by column '{name}' is not a paired column of table '{ctx.table.label}'.",
                hint="Use a column present on both sides (or mapped under columns.mappings).",
            )
        spec = resolve_pair(pair.source.type, pair.target.type, ctx.normalization)
        sd, td = plan.source_dialect, plan.target_dialect
        found.append(
            (
                pair.target.name,
                canonical_sql(sd.quote(pair.source.name), spec.source, sd),
                canonical_sql(td.quote(pair.target.name), spec.target, td),
            )
        )
    return found


def _stratum_predicate(exprs: list[str], values: tuple[Any, ...], dialect: Dialect) -> str:
    parts = [
        f"{e} IS NULL" if v is None else f"{e} = {dialect.literal(v)}"
        for e, v in zip(exprs, values, strict=True)
    ]
    return "(" + " AND ".join(parts) + ")"


def allocate(
    strata: list[Stratum], wanted: int, min_per_stratum: int
) -> tuple[float, list[SampleClass]]:
    """Proportional allocation with a per-stratum minimum, as sampling classes."""
    population = sum(s.population for s in strata)
    base = min(1.0, wanted / population) if population else 1.0
    default = SampleClass(base, [], default=True)
    raised: dict[float, SampleClass] = {}
    for stratum in strata:
        if stratum.population == 0:
            default.strata.append(stratum)
            continue
        target = max(base * stratum.population, float(min(min_per_stratum, stratum.population)))
        needed = min(1.0, target / stratum.population)
        if needed <= base * (1 + 1e-9):
            default.strata.append(stratum)
            continue
        # Round raised fractions up to base * 2^k, so a few classes cover every stratum.
        k = math.ceil(math.log2(needed / base)) if base > 0 else 0
        fraction = min(1.0, base * 2**k)
        raised.setdefault(fraction, SampleClass(fraction, [])).strata.append(stratum)
    classes = [default, *sorted(raised.values(), key=lambda c: c.fraction)]
    if len(classes) > _MAX_CLASSES:
        raise PlanError("Too many sampling classes.", hint="Lower min_per_stratum.")
    return base, classes


# ====================================================================== comparator


def _engine(ctx: ComparisonContext) -> Engine:
    entry = ctx.registry.get(PluginKind.ENGINE, ctx.engine)
    if not entry.ready:
        raise PlanError(f"Engine '{ctx.engine}' is not implemented in this version.")
    implementation = entry.implementation
    engine = implementation() if isinstance(implementation, type) else implementation
    assert isinstance(engine, Engine)
    return engine


def _outcome(**kwargs: Any) -> CheckOutcome:
    return CheckOutcome(type="compare.sample", dimension=Dimension.RECONCILIATION, **kwargs)


def _fmt_pct(value: float) -> str:
    return f"{100.0 * value:.4g}%"


class SampleComparator(Comparator):
    """Statistical sample comparison with a documented size, confidence, and fixed seed."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:
        plan = ctx.plan
        if plan is None:
            error = ctx.plan_error
            return [
                _outcome(
                    status=Status.SKIPPED,
                    message=str(error) if error else "sample comparison needs key columns",
                    hints=[error.hint] if error and error.hint else ["Set 'keys' on the table."],
                )
            ]
        config = ctx.table.comparison.sample or DEFAULT_SAMPLE
        recorder = QueryRecorder()
        sd, td = plan.source_dialect, plan.target_dialect
        strata_cols = _strata_exprs(ctx, plan, list(config.stratify_by))
        strata = self._count(ctx, plan, strata_cols, config, recorder)
        population = sum(s.population for s in strata)
        if config.margin_of_error is not None:
            wanted = cochran_size(
                population, config.margin_of_error, config.confidence, config.expected_rate
            )
            sizing = (
                f"Cochran with finite population correction (margin {config.margin_of_error:g}, "
                f"confidence {config.confidence:g}, expected rate {config.expected_rate:g})"
            )
        elif config.rows is not None:
            wanted = min(population, config.rows)
            sizing = f"{config.rows:,} rows"
        else:
            assert config.fraction is not None
            wanted = min(population, math.ceil(config.fraction * population))
            sizing = f"fraction {config.fraction:g}"
        design: dict[str, Any] = {
            "method": "Bernoulli sampling by a seeded hash of the canonical key",
            "sizing": sizing,
            "population": population,
            "sample_size_target": wanted,
            "confidence": config.confidence,
            "margin_of_error": config.margin_of_error,
            "seed": config.seed,
            "stratify_by": list(config.stratify_by),
            "strata": len(strata) if strata_cols else 0,
        }
        if population == 0:
            return [
                _outcome(
                    passed=True,
                    message="no rows with keys on either side; nothing to sample",
                    details={"sample": design},
                    queries=recorder.records,
                )
            ]
        base, classes = allocate(strata, wanted, config.min_per_stratum)
        design["fraction"] = base
        engine = _engine(ctx)
        s_hash = sample_hash_sql([k.source.canonical for k in plan.keys], sd, config.seed)
        t_hash = sample_hash_sql([k.target.canonical for k in plan.keys], td, config.seed)
        s_nn, t_nn = keys_not_null_sql(plan, "source"), keys_not_null_sql(plan, "target")
        raised = [c for c in classes if not c.default]
        parts: list[DiffResult] = []
        stats: list[ClassStats] = []
        class_details: list[dict[str, Any]] = []
        for sample_class in classes:
            if strata_cols and not sample_class.strata:
                continue  # every stratum was raised to another class
            s_pred = f"{s_nn} AND {s_hash} < {threshold(sample_class.fraction)}"
            t_pred = f"{t_nn} AND {t_hash} < {threshold(sample_class.fraction)}"
            if strata_cols:
                s_exprs = [s for _, s, _ in strata_cols]
                t_exprs = [t for _, _, t in strata_cols]
                if sample_class.default:
                    if raised:
                        others = [s for c in raised for s in c.strata]
                        s_in = " OR ".join(
                            _stratum_predicate(s_exprs, s.values, sd) for s in others
                        )
                        t_in = " OR ".join(
                            _stratum_predicate(t_exprs, s.values, td) for s in others
                        )
                        s_pred += f" AND (CASE WHEN {s_in} THEN 1 ELSE 0 END) = 0"
                        t_pred += f" AND (CASE WHEN {t_in} THEN 1 ELSE 0 END) = 0"
                else:
                    s_in = " OR ".join(
                        _stratum_predicate(s_exprs, s.values, sd) for s in sample_class.strata
                    )
                    t_in = " OR ".join(
                        _stratum_predicate(t_exprs, s.values, td) for s in sample_class.strata
                    )
                    s_pred += f" AND ({s_in})"
                    t_pred += f" AND ({t_in})"
            job = DiffJob(
                plan=plan,
                source=DiffSide(ctx.source.connector, restricted(ctx.source.relation, s_pred, sd)),
                target=DiffSide(ctx.target.connector, restricted(ctx.target.relation, t_pred, td)),
                mode="full",
                sample_size=ctx.sample_size,
                max_mismatch_rows=ctx.table.comparison.max_mismatch_rows,
                recorder=recorder,
                resources=ctx.resources,
            )
            result = engine.diff(job)
            parts.append(result)
            s = result.summary
            sampled = s.matched + s.mismatched + s.missing_in_target + s.extra_in_target
            class_population = sum(st.population for st in sample_class.strata)
            stat = ClassStats(
                fraction=sample_class.fraction,
                population=class_population,
                sampled=sampled,
                differing=s.mismatched + s.missing_in_target + s.extra_in_target,
                mismatched=s.mismatched,
                missing_in_target=s.missing_in_target,
                extra_in_target=s.extra_in_target,
            )
            stats.append(stat)
            class_details.append(
                {
                    "fraction": round(sample_class.fraction, 9),
                    "strata": len(sample_class.strata) if strata_cols else None,
                    "population": stat.population,
                    "sampled": stat.sampled,
                    "differing": stat.differing,
                }
            )
        merged = (
            parts[0]
            if len(parts) == 1
            else merge_results(
                parts, parts[0].engine, ctx.sample_size, ctx.table.comparison.max_mismatch_rows
            )
        )
        est = estimate(stats, config.confidence)
        return [self._result(ctx, plan, config, design, class_details, merged, est, recorder)]

    # ------------------------------------------------------------------ steps
    def _count(
        self,
        ctx: ComparisonContext,
        plan: ComparisonPlan,
        strata_cols: list[tuple[str, str, str]],
        config: SampleConfig,
        recorder: QueryRecorder,
    ) -> list[Stratum]:
        """Rows with non-NULL keys per stratum on each side (one stratum when unstratified)."""
        counts: dict[str, dict[tuple[Any, ...], int]] = {}
        for side in ("source", "target"):
            dialect = _dialect(plan, side)
            relation = (ctx.source if side == "source" else ctx.target).relation
            connector = (ctx.source if side == "source" else ctx.target).connector
            exprs = [s if side == "source" else t for _, s, t in strata_cols]
            select = [f"{e} AS g{i}" for i, e in enumerate(exprs)] + ["COUNT(*) AS n"]
            sql = (
                f"SELECT {', '.join(select)}\nFROM {relation.sql}\n"
                f"WHERE {keys_not_null_sql(plan, side)}"
            )
            if exprs:
                sql += "\nGROUP BY " + ", ".join(exprs)
                sql = dialect.limit(sql, config.max_strata + 1)
            rows = recorder.run(connector, sql, Side(side)).to_pylist()
            found: dict[tuple[Any, ...], int] = {}
            for row in rows:
                key = tuple(row.get(f"g{i}") for i in range(len(exprs)))
                found[key] = found.get(key, 0) + int(row["n"] or 0)
            if len(found) > config.max_strata:
                raise PlanError(
                    f"stratify_by gives more than {config.max_strata:,} strata on the {side}.",
                    hint="Stratify by fewer or coarser columns, or raise max_strata.",
                )
            counts[side] = found
        keys = sorted(
            set(counts["source"]) | set(counts["target"]),
            key=lambda k: tuple("" if v is None else str(v) for v in k),
        )
        return [Stratum(k, counts["source"].get(k, 0), counts["target"].get(k, 0)) for k in keys]

    def _result(
        self,
        ctx: ComparisonContext,
        plan: ComparisonPlan,
        config: SampleConfig,
        design: dict[str, Any],
        class_details: list[dict[str, Any]],
        merged: DiffResult,
        est: Estimate,
        recorder: QueryRecorder,
    ) -> CheckOutcome:
        s = merged.summary
        sampled, differing = est.sampled, est.differing
        conf = f"{100 * config.confidence:g}%"
        design["sample_size"] = sampled
        if est.population and sampled:
            # Worst case (p = 0.5) half-width for the realized size, with the finite population
            # correction.
            fpc = max(0.0, 1.0 - sampled / est.population)
            design["achieved_margin_of_error"] = round(
                z_value(config.confidence) * math.sqrt(0.25 * fpc / sampled), 6
            )
        estimate_details = {
            "mismatch_rate": round(est.rate, 9),
            "confidence_interval": [round(est.low, 9), round(est.high, 9)],
            "confidence": config.confidence,
            "effective_sample_size": round(est.effective_size, 1),
            "census": est.census,
            "method": "stratified estimator by sampling class, Wilson score interval",
        }
        rows_low = math.floor(est.low * est.population)
        rows_high = math.ceil(est.high * est.population)
        rows_estimate = est.extrapolated.get("differing", 0)
        extrapolated: dict[str, Any] = {
            "rows_with_differences": rows_estimate,
            "rows_with_differences_interval": [rows_low, rows_high],
            "mismatched": est.extrapolated.get("mismatched", 0),
            "missing_in_target": est.extrapolated.get("missing_in_target", 0),
            "extra_in_target": est.extrapolated.get("extra_in_target", 0),
            "population": est.population,
        }
        sampled_summary = {
            "matched": s.matched,
            "mismatched": s.mismatched,
            "missing_in_target": s.missing_in_target,
            "extra_in_target": s.extra_in_target,
            "duplicate_keys_source": s.duplicate_keys_source,
            "duplicate_keys_target": s.duplicate_keys_target,
        }
        passed = est.rate <= config.max_mismatch_rate
        if est.census:
            message = (
                f"every key was compared ({sampled:,}): {differing:,} differ ({_fmt_pct(est.rate)})"
            )
        else:
            message = (
                f"sample of {sampled:,} of about {est.population:,} keys (seed {config.seed}): "
                f"{differing:,} differ; estimated mismatch rate {_fmt_pct(est.rate)} "
                f"({conf} CI {_fmt_pct(est.low)} to {_fmt_pct(est.high)}), about "
                f"{rows_estimate:,} rows ({rows_low:,} to {rows_high:,})"
            )
        hints = list(merged.notes)
        if not passed:
            hints.append(
                "The sample found differences: run full_diff (or hash_bucket) on the table, or "
                "a partition of it, to list every difference."
            )
        if ctx.table.comparison.partitions is not None:
            hints.append("The sample covers the whole table; comparison.partitions are ignored.")
        compared = s.matched + s.mismatched
        mismatches: list[ColumnMismatch] = []
        by_column: dict[str, list[Any]] = {}
        for row in merged.mismatches:
            by_column.setdefault(row.column, []).append(row)
        for col in plan.columns:
            count = merged.column_counts.get(col.label, 0)
            if not count:
                continue
            labels = Counter(
                classify_pair(r.source_value, r.target_value) for r in by_column.get(col.label, [])
            )
            mismatches.append(
                ColumnMismatch(
                    column=col.label,
                    source_column=col.source_label,
                    mismatch_count=count,
                    mismatch_pct=pct(count, compared) or 0.0,
                    classifications=dict(labels.most_common()),
                    hints=summarize(labels, count),
                )
            )
        mismatches.sort(key=lambda m: -m.mismatch_count)
        samples = None
        if merged.key_samples and not ctx.privacy.strict:
            labels_ = [k.label for k in plan.keys]
            columns = ["issue", *labels_, "rows"]
            rows: list[list[Any]] = []
            for kind, values in merged.key_samples.items():
                rows += [[kind, *v] for v in values]
            samples = SampleSet(
                columns=columns,
                rows=ctx.privacy.mask_rows(columns, rows),
                masked=ctx.privacy.mask_pii,
                truncated=any(len(v) >= ctx.sample_size for v in merged.key_samples.values()),
            )
        return _outcome(
            passed=passed,
            message=message,
            observed=round(est.rate, 9),
            expected=config.max_mismatch_rate,
            unexpected_count=differing,
            total_count=sampled,
            column_mismatches=mismatches,
            columns=[m.column for m in mismatches],
            samples=samples,
            hints=hints,
            engine=merged.engine,
            normalization=plan.describe(),
            queries=recorder.records[:60],
            timing=merged.timing,
            details={
                "sample": design,
                "estimate": estimate_details,
                "extrapolated": extrapolated,
                "sampled": sampled_summary,
                "classes": class_details,
            },
        )


__all__ = [
    "DEFAULT_SAMPLE",
    "SampleComparator",
    "allocate",
    "cochran_size",
    "estimate",
    "sample_hash",
    "sample_hash_sql",
    "threshold",
    "wilson_interval",
]
