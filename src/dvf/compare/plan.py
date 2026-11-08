"""Column pairing and comparison plans.

:func:`pair_columns` decides which source column corresponds to which target column, honoring
key mappings, explicit column mappings, renames, ``include`` and ``exclude`` patterns, and the
name matching mode. Schema checks and comparisons share the pairing, so a renamed column is never
reported as missing.

:func:`build_plan` turns a pairing into a :class:`ComparisonPlan`: per column, the SQL expression
on each side (with normalizers applied) and the canonicalization that makes both sides comparable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from dvf.config.base import NormalizerRef
from dvf.config.models import (
    ColumnMapping,
    KeyMapping,
    NormalizationConfig,
    TableConfig,
    ToleranceConfig,
)
from dvf.core.datasets import ColumnSchema, DatasetSchema
from dvf.core.errors import PlanError
from dvf.core.types import CanonicalType
from dvf.normalize.canonical import (
    CanonSpec,
    Kind,
    PairSpec,
    canonical_sql,
    numeric_sql,
    resolve_pair,
)
from dvf.plugins.base import Normalizer
from dvf.sql.dialects import Dialect


@dataclass(frozen=True)
class ColumnPair:
    """A source column (or expression) and the target column it maps to."""

    target: ColumnSchema
    source: ColumnSchema | None
    """``None`` when the target is derived from a ``source_expr``."""
    source_expr: str | None = None
    mapping: ColumnMapping | None = None
    key: KeyMapping | None = None
    compare: bool = True

    @property
    def label(self) -> str:
        return self.target.name

    @property
    def source_label(self) -> str:
        return self.source.name if self.source is not None else f"({self.source_expr})"

    @property
    def is_key(self) -> bool:
        return self.key is not None


@dataclass
class ColumnPairing:
    """The outcome of :func:`pair_columns`."""

    pairs: list[ColumnPair] = field(default_factory=list)
    missing_in_target: list[ColumnSchema] = field(default_factory=list)
    """In-scope source columns with no target column."""
    extra_in_target: list[ColumnSchema] = field(default_factory=list)
    """In-scope target columns with no source column."""
    problems: list[str] = field(default_factory=list)
    """Mapping errors, such as a mapped column that does not exist."""

    @property
    def keys(self) -> list[ColumnPair]:
        return [p for p in self.pairs if p.is_key]

    @property
    def compared(self) -> list[ColumnPair]:
        return [p for p in self.pairs if not p.is_key and p.compare]

    def target_for_source(self, source_name: str) -> ColumnPair | None:
        lowered = source_name.lower()
        return next(
            (p for p in self.pairs if p.source is not None and p.source.name.lower() == lowered),
            None,
        )

    def pair_for_target(self, target_name: str) -> ColumnPair | None:
        lowered = target_name.lower()
        return next((p for p in self.pairs if p.target.name.lower() == lowered), None)


def _matches(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatchcase(lowered, p.lower()) for p in patterns)


def pair_columns(
    table: TableConfig,
    source: DatasetSchema,
    target: DatasetSchema,
    *,
    key_mappings: list[KeyMapping] | None = None,
) -> ColumnPairing:
    """Pair source and target columns for ``table``."""
    cols = table.columns
    case_sensitive = cols.match == "exact"
    result = ColumnPairing()
    used_source: set[str] = set()
    used_target: set[str] = set()

    def find(schema: DatasetSchema, name: str) -> ColumnSchema | None:
        return schema.column(name, case_sensitive=case_sensitive)

    def in_scope(*names: str) -> bool:
        if any(_matches(n, cols.exclude) for n in names):
            return False
        return any(_matches(n, cols.include) for n in names)

    for key in key_mappings if key_mappings is not None else table.keys:
        src, tgt = find(source, key.source), find(target, key.target_name)
        if src is None:
            result.problems.append(f"key column '{key.source}' not found in the source")
        if tgt is None:
            result.problems.append(f"key column '{key.target_name}' not found in the target")
        if src is None or tgt is None:
            continue
        result.pairs.append(ColumnPair(target=tgt, source=src, key=key))
        used_source.add(src.name)
        used_target.add(tgt.name)

    for mapping in cols.mappings:
        tgt = find(target, mapping.target)
        if tgt is None:
            result.problems.append(f"mapped target column '{mapping.target}' not found")
            continue
        if tgt.name in used_target:
            continue
        if mapping.source is not None:
            src = find(source, mapping.source)
            if src is None:
                result.problems.append(f"mapped source column '{mapping.source}' not found")
                continue
            used_source.add(src.name)
            pair = ColumnPair(target=tgt, source=src, mapping=mapping, compare=mapping.compare)
        else:
            pair = ColumnPair(
                target=tgt,
                source=None,
                source_expr=mapping.source_expr,
                mapping=mapping,
                compare=mapping.compare,
            )
        used_target.add(tgt.name)
        result.pairs.append(pair)

    targets_left = {c.name: c for c in target.columns if c.name not in used_target}
    for src in source.columns:
        if src.name in used_source:
            continue
        tgt = find(target, src.name)
        if tgt is not None and tgt.name in targets_left:
            del targets_left[tgt.name]
            if in_scope(src.name, tgt.name):
                result.pairs.append(ColumnPair(target=tgt, source=src))
        elif in_scope(src.name):
            result.missing_in_target.append(src)
    result.extra_in_target = [c for c in targets_left.values() if in_scope(c.name)]
    return result


# ====================================================================== comparison plans


@dataclass(frozen=True)
class SideExpr:
    """One side of a planned column."""

    raw: str
    """SQL for the raw value, before normalizers."""
    normalized: str
    """SQL after normalizers, before canonicalization."""
    canonical: str
    """SQL for the canonical string."""
    numeric: str | None
    """SQL for the numeric value used by tolerance comparisons, if any."""
    type: CanonicalType
    """Type after normalizers."""
    raw_type: CanonicalType
    normalizers: tuple[Normalizer, ...]
    spec: CanonSpec


@dataclass(frozen=True)
class PlannedColumn:
    """A key or compared column with both sides planned."""

    label: str
    source_label: str
    source: SideExpr
    target: SideExpr
    pair_spec: PairSpec
    is_key: bool
    pii: bool = False
    normalizer_names: tuple[str, ...] = ()

    def describe(self) -> str:
        steps = [*self.normalizer_names, self.pair_spec.describe()]
        return f"{self.label}: " + " -> ".join(steps)


@dataclass
class ComparisonPlan:
    """Everything an engine needs to compare two datasets row by row."""

    keys: list[PlannedColumn]
    columns: list[PlannedColumn]
    source_dialect: Dialect
    target_dialect: Dialect
    notes: list[str] = field(default_factory=list)
    """Caveats about the canonical forms, such as normalization a dialect cannot apply."""

    @property
    def hashed(self) -> list[PlannedColumn]:
        return [c for c in self.columns if c.pair_spec.hashable]

    @property
    def tolerant(self) -> list[PlannedColumn]:
        return [c for c in self.columns if not c.pair_spec.hashable]

    def describe(self) -> list[str]:
        return [c.describe() for c in self.keys + self.columns] + list(self.notes)


NormalizerFactory = Callable[[NormalizerRef], Normalizer]


def _side(
    raw: str,
    ctype: CanonicalType,
    refs: list[NormalizerRef],
    factory: NormalizerFactory,
    dialect: Dialect,
) -> tuple[str, CanonicalType, tuple[Normalizer, ...]]:
    expr, current = raw, ctype
    applied: list[Normalizer] = []
    for ref in refs:
        normalizer = factory(ref)
        if not normalizer.applies_to(current):
            continue
        expr = normalizer.to_sql(expr, dialect, current)
        current = normalizer.output_type(current)
        applied.append(normalizer)
    return expr, current, tuple(applied)


def _finish(
    raw: str,
    raw_type: CanonicalType,
    normalized: str,
    ctype: CanonicalType,
    normalizers: tuple[Normalizer, ...],
    spec: CanonSpec,
    pair: PairSpec,
    dialect: Dialect,
) -> SideExpr:
    numeric = None
    if pair.numeric is not None:
        numeric = numeric_sql(normalized, spec, pair.numeric, dialect)
    return SideExpr(
        raw=raw,
        normalized=normalized,
        canonical=canonical_sql(normalized, spec, dialect),
        numeric=numeric,
        type=ctype,
        raw_type=raw_type,
        normalizers=normalizers,
        spec=spec,
    )


def build_plan(
    table: TableConfig,
    pairing: ColumnPairing,
    *,
    normalization: NormalizationConfig,
    source_dialect: Dialect,
    target_dialect: Dialect,
    factory: NormalizerFactory,
    source_expr_types: Mapping[str, CanonicalType] | None = None,
    pii_columns: list[str] | None = None,
    require_keys: bool = True,
) -> ComparisonPlan:
    """Plan the comparison of ``table``. Row-level comparisons need keys (``require_keys``)."""
    if pairing.problems:
        raise PlanError(
            f"Table '{table.label}' cannot be compared: " + "; ".join(pairing.problems),
            hint="Fix the column names in 'keys' or 'columns.mappings'.",
        )
    expr_types = source_expr_types or {}
    pii_patterns = pii_columns or []
    keys: list[PlannedColumn] = []
    columns: list[PlannedColumn] = []
    tolerance = table.comparison.tolerance
    for pair in pairing.pairs:
        if not pair.is_key and not pair.compare:
            continue
        refs: list[NormalizerRef] = []
        column_tolerance: ToleranceConfig | None = tolerance
        if pair.key is not None:
            refs = list(pair.key.normalize)
            column_tolerance = None  # keys always match exactly
        elif pair.mapping is not None:
            refs = list(pair.mapping.normalize)
            column_tolerance = pair.mapping.tolerance or tolerance
        if pair.source is not None:
            src_raw = source_dialect.quote(pair.source.name)
            src_type = pair.source.type
        else:
            assert pair.source_expr is not None
            src_raw = f"({pair.source_expr})"
            src_type = expr_types.get(pair.source_expr, CanonicalType.unknown())
        tgt_raw = target_dialect.quote(pair.target.name)
        src_norm, src_ctype, src_applied = _side(src_raw, src_type, refs, factory, source_dialect)
        tgt_norm, tgt_ctype, tgt_applied = _side(
            tgt_raw, pair.target.type, refs, factory, target_dialect
        )
        pair_spec = resolve_pair(src_ctype, tgt_ctype, normalization, column_tolerance)
        if pair.is_key and pair_spec.numeric is not None:
            pair_spec = PairSpec(pair_spec.source, pair_spec.target, None)
        planned = PlannedColumn(
            label=pair.label,
            source_label=pair.source_label,
            source=_finish(
                src_raw,
                src_type,
                src_norm,
                src_ctype,
                src_applied,
                pair_spec.source,
                pair_spec,
                source_dialect,
            ),
            target=_finish(
                tgt_raw,
                pair.target.type,
                tgt_norm,
                tgt_ctype,
                tgt_applied,
                pair_spec.target,
                pair_spec,
                target_dialect,
            ),
            pair_spec=pair_spec,
            is_key=pair.is_key,
            pii=bool(pair.mapping and pair.mapping.pii)
            or _matches(pair.label, pii_patterns)
            or (pair.source is not None and _matches(pair.source.name, pii_patterns)),
            normalizer_names=tuple(str(r) for r in refs),
        )
        (keys if pair.is_key else columns).append(planned)
    if not keys and require_keys:
        raise PlanError(
            f"Table '{table.label}' has no key columns, so rows cannot be paired.",
            hint="Set 'keys' (for example keys: [id]) or 'keys: auto' to use the primary key.",
        )
    notes: list[str] = []
    form = normalization.unicode_form
    has_text = any(
        Kind.TEXT in (c.pair_spec.source.kind, c.pair_spec.target.kind) for c in keys + columns
    )
    if form != "none" and has_text:
        for side, dialect in (("source", source_dialect), ("target", target_dialect)):
            if not dialect.supports_unicode_form(form):
                notes.append(
                    f"Unicode {form} normalization is not available in {dialect.name}; "
                    f"{side} text is compared without it"
                )
    return ComparisonPlan(
        keys=keys,
        columns=columns,
        source_dialect=source_dialect,
        target_dialect=target_dialect,
        notes=notes,
    )
