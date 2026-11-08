"""Helpers shared by the migration validations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

import pyarrow as pa

from dvf.checks.context import CheckContext, CheckOutcome, mostly_passes, pct
from dvf.config.models import DatasetSpec
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.models.results import MetricComparison, SampleSet
from dvf.packs.migration.config import DatasetLocator, FlagConfig
from dvf.plugins.base import Connector, Relation
from dvf.sql.dialects import Dialect

_TRUE_TEXTS = ("true", "1", "t", "y", "yes")
_FALSE_TEXTS = ("false", "0", "f", "n", "no")


def as_int(value: Any) -> int:
    """An integer from a count or sum returned by any driver (None counts as 0)."""
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


def flag_texts(values: Sequence[Any]) -> list[str]:
    """Lower-case texts a flag column may hold for ``values`` (booleans expand to 1/0, t/f...)."""
    texts: list[str] = []
    for value in values:
        if isinstance(value, bool):
            texts.extend(_TRUE_TEXTS if value else _FALSE_TEXTS)
        else:
            texts.append(str(value).strip().lower())
    return list(dict.fromkeys(texts))


def text_in(expr: str, values: Sequence[Any], dialect: Dialect) -> str:
    """``expr`` (as trimmed, lower-case text) is one of ``values``."""
    texts = flag_texts(values)
    normalized = dialect.lower(dialect.trim(dialect.to_text(expr)))
    return f"{normalized} IN ({', '.join(dialect.literal(t) for t in texts)})"


def flag_condition(
    flag: FlagConfig,
    column: Callable[[str], str],
    dialect: Dialect,
    render: Callable[[str], str],
) -> str:
    """SQL condition that is true when ``flag`` is set; ``column`` quotes a real column name."""
    if flag.expression is not None:
        return f"({render(flag.expression)})"
    assert flag.column is not None
    return f"({text_in(column(flag.column), flag.values, dialect)})"


def flag_int(condition: str) -> str:
    """1 when ``condition`` holds, else 0 (NULL counts as not set)."""
    return f"(CASE WHEN {condition} THEN 1 ELSE 0 END)"


def resolve_locator(
    ctx_connectors: Any,
    default_connection: str,
    locator: DatasetLocator,
    render: Callable[[str], str],
) -> tuple[Connector, Relation, DatasetSchema]:
    """Open the dataset ``locator`` names and return its connector, relation, and schema."""
    name = locator.connection or default_connection
    connector: Connector = ctx_connectors.get(name)
    spec = DatasetSpec(
        table=render(locator.table) if locator.table else None,
        query=render(locator.query) if locator.query else None,
        filter=render(locator.filter) if locator.filter else None,
    )
    relation = connector.resolve(spec)
    if not connector.exists(relation):
        what = f"table '{locator.table}'" if locator.table else "query"
        raise PlanError(
            f"The {what} on connection '{name}' could not be read.",
            hint="Check its name, the connection, and the query.",
        )
    return connector, relation, connector.get_schema(relation)


def real_columns(schema: DatasetSchema, names: Sequence[str], where: str) -> list[str]:
    """Real names of ``names`` in ``schema`` (case-insensitive), or a PlanError."""
    out: list[str] = []
    for name in names:
        found = schema.column(name)
        if found is None:
            available = ", ".join(c.name for c in schema.columns[:30])
            raise PlanError(
                f"Column '{name}' not found in {where}.", hint=f"Available columns: {available}"
            )
        out.append(found.name)
    return out


def default_keys(ctx: CheckContext, configured: Sequence[str], check: str) -> list[str]:
    """Configured key columns, else the table's keys on the checked side."""
    names = list(configured) or list(ctx.key_columns)
    if not names:
        raise PlanError(
            f"Check '{check}' needs key columns.",
            hint="Set 'key_columns' on the check, or 'keys' on the table.",
        )
    return [ctx.column(n) for n in names]


def rules_outcome(
    counts: Mapping[str, int],
    labels: Mapping[str, str],
    *,
    total: int,
    mostly: float = 1.0,
    unit: str = "rows",
    samples: SampleSet | None = None,
    extract: pa.Table | None = None,
    details: Mapping[str, Any] | None = None,
    metrics: list[MetricComparison] | None = None,
    hints: list[str] | None = None,
    passed: bool | None = None,
) -> CheckOutcome:
    """An outcome for a check made of several rules: ``counts`` maps each rule to its violations."""
    failing = {rule: n for rule, n in counts.items() if n}
    unexpected = sum(failing.values())
    ok = mostly_passes(unexpected, total, mostly) if passed is None else passed
    if failing:
        parts = [f"{n:,} {labels.get(rule, rule)}" for rule, n in failing.items()]
        share = pct(unexpected, total)
        message = "; ".join(parts)
        if share is not None and unit:
            message += f" (of {total:,} {unit})"
        if ok and mostly < 1:
            message += f", within mostly={mostly}"
    else:
        message = f"all {total:,} {unit} pass" if unit else "all rules pass"
    return CheckOutcome(
        passed=ok,
        message=message,
        observed=dict(counts),
        expected=0 if mostly >= 1 else f"at least {mostly:.4%} valid",
        unexpected_count=unexpected,
        total_count=total,
        samples=samples,
        extract=extract,
        metrics=list(metrics or []),
        hints=list(hints or []),
        details={"violations": dict(counts), **(details or {})},
    )


def side_label(side: Side) -> str:
    return "source" if side is Side.SOURCE else "target"


__all__ = [
    "as_int",
    "default_keys",
    "flag_condition",
    "flag_int",
    "flag_texts",
    "real_columns",
    "resolve_locator",
    "rules_outcome",
    "text_in",
]
