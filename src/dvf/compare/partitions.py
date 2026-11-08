"""Partitioned comparisons: split row-level strategies into independent slices.

Each partition is a pair of predicates (source and target) over the datasets' own columns. The
partitions of one configuration are disjoint and, except for ``predicates`` (which are the user's
responsibility), together cover every row, so partition results add up to the table's result.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dvf.compare.buckets import key_int_sql, keys_not_null_sql, keys_null_sql
from dvf.config.models import PartitionConfig
from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.plugins.base import Relation
from dvf.sql.dialects import Dialect

if TYPE_CHECKING:
    from dvf.compare.context import ComparisonContext
    from dvf.compare.plan import ComparisonPlan

_UNSAFE = re.compile(r"[^A-Za-z0-9_.=<>-]+")


@dataclass(frozen=True)
class Partition:
    """One slice: predicates per side (``None`` means no restriction)."""

    name: str
    source: str | None
    target: str | None


def _label(value: Any) -> str:
    return _UNSAFE.sub("_", str(value))[:48]


def _column_exprs(ctx: ComparisonContext, column: str) -> tuple[str, str, str]:
    pair = ctx.pairing.pair_for_target(column) or ctx.pairing.target_for_source(column)
    if pair is None or pair.source is None:
        raise PlanError(
            f"Partition column '{column}' is not a paired column of table '{ctx.table.label}'.",
            hint="Use a column present on both sides (or mapped under columns.mappings).",
        )
    sd, td = ctx.source.connector.sql_dialect, ctx.target.connector.sql_dialect
    return pair.target.name, sd.quote(pair.source.name), td.quote(pair.target.name)


def _value_partitions(
    ctx: ComparisonContext, config: PartitionConfig, column: str
) -> list[Partition]:
    label, s_col, t_col = _column_exprs(ctx, column)
    sd, td = ctx.source.connector.sql_dialect, ctx.target.connector.sql_dialect
    values: list[Any]
    if config.values == "auto":
        sql = sd.limit(
            f"SELECT DISTINCT {s_col} AS v FROM {ctx.source.relation.sql} WHERE {s_col} IS NOT NULL",
            config.max_partitions + 1,
        )
        rows = ctx.source.connector.query(sql).to_pylist()
        values = sorted((r["v"] for r in rows), key=lambda v: (str(type(v)), v))
        if len(values) > config.max_partitions:
            raise PlanError(
                f"Column '{column}' has more than {config.max_partitions:,} distinct values, "
                "too many partitions.",
                hint="Use 'ranges' or 'hash_buckets', or raise 'max_partitions'.",
            )
    else:
        assert isinstance(config.values, list)
        values = list(config.values)
    parts = [
        Partition(
            f"{label}={_label(v)}", f"{s_col} = {sd.literal(v)}", f"{t_col} = {td.literal(v)}"
        )
        for v in values
    ]
    if values:
        s_list = ", ".join(sd.literal(v) for v in values)
        t_list = ", ".join(td.literal(v) for v in values)
        parts.append(
            Partition(f"{label}=other", f"{s_col} NOT IN ({s_list})", f"{t_col} NOT IN ({t_list})")
        )
    parts.append(Partition(f"{label}=NULL", f"{s_col} IS NULL", f"{t_col} IS NULL"))
    return parts


def _range_partitions(
    config: PartitionConfig, ctx: ComparisonContext, column: str
) -> list[Partition]:
    label, s_col, t_col = _column_exprs(ctx, column)
    sd, td = ctx.source.connector.sql_dialect, ctx.target.connector.sql_dialect
    bounds = list(config.ranges or [])
    try:
        ordered = bounds == sorted(bounds)
    except TypeError:
        ordered = False
    if not ordered or len(set(map(str, bounds))) != len(bounds):
        raise PlanError(
            f"Partition 'ranges' for '{column}' must be strictly ascending.",
            hint="List each boundary once, smallest first.",
        )
    parts = [
        Partition(
            f"{label}<{_label(bounds[0])}",
            f"{s_col} < {sd.literal(bounds[0])}",
            f"{t_col} < {td.literal(bounds[0])}",
        )
    ]
    for lo, hi in itertools.pairwise(bounds):
        parts.append(
            Partition(
                f"{_label(lo)}<={label}<{_label(hi)}",
                f"{s_col} >= {sd.literal(lo)} AND {s_col} < {sd.literal(hi)}",
                f"{t_col} >= {td.literal(lo)} AND {t_col} < {td.literal(hi)}",
            )
        )
    parts.append(
        Partition(
            f"{label}>={_label(bounds[-1])}",
            f"{s_col} >= {sd.literal(bounds[-1])}",
            f"{t_col} >= {td.literal(bounds[-1])}",
        )
    )
    parts.append(Partition(f"{label}=NULL", f"{s_col} IS NULL", f"{t_col} IS NULL"))
    return parts


def _hash_partitions(plan: ComparisonPlan, count: int) -> list[Partition]:
    parts = []
    sd, td = plan.source_dialect, plan.target_dialect
    s_bucket = sd.bucket(key_int_sql(plan, "source"), count)
    t_bucket = td.bucket(key_int_sql(plan, "target"), count)
    s_nn, t_nn = keys_not_null_sql(plan, "source"), keys_not_null_sql(plan, "target")
    for i in range(count):
        parts.append(
            Partition(
                f"hash{i + 1}of{count}",
                f"{s_nn} AND {s_bucket} = {i}",
                f"{t_nn} AND {t_bucket} = {i}",
            )
        )
    parts.append(
        Partition("null_keys", keys_null_sql(plan, "source"), keys_null_sql(plan, "target"))
    )
    return parts


def plan_partitions(ctx: ComparisonContext, plan: ComparisonPlan) -> list[Partition]:
    """The partitions configured for ``ctx.table`` (one unrestricted partition when none are)."""
    config = ctx.table.comparison.partitions
    if config is None:
        return [Partition("all", None, None)]
    if config.predicates is not None:
        parts = [Partition(p.name, p.source, p.target or p.source) for p in config.predicates]
    elif config.hash_buckets is not None:
        parts = _hash_partitions(plan, config.hash_buckets)
    elif config.ranges is not None:
        assert config.column is not None
        parts = _range_partitions(config, ctx, config.column)
    else:
        assert config.column is not None
        parts = _value_partitions(ctx, config, config.column)
    if len(parts) > config.max_partitions + 2:
        raise PlanError(
            f"{len(parts):,} partitions exceed max_partitions ({config.max_partitions:,}).",
            hint="Use fewer values or ranges, or raise 'max_partitions'.",
        )
    names = [p.name for p in parts]
    if len(set(names)) != len(names):
        raise PlanError("Partition names must be unique.", hint="Rename the duplicated partitions.")
    return parts


def describe_partitions(ctx: ComparisonContext, plan: ComparisonPlan) -> list[dict[str, Any]]:
    """Partition names and predicates, for ``dvf explain``."""
    return [
        {"name": p.name, Side.SOURCE.value: p.source, Side.TARGET.value: p.target}
        for p in plan_partitions(ctx, plan)
    ]


def restricted(rel: Relation, predicate: str | None, dialect: Dialect) -> Relation:
    from dvf.compare.buckets import restrict_relation

    return rel if predicate is None else restrict_relation(rel, predicate, dialect, "dvf_slice")
