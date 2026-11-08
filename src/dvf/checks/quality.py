"""Built-in data quality checks (BUILD_PROMPT.md section 7.2), all pushed down as SQL."""

from __future__ import annotations

import operator
from datetime import UTC, datetime
from typing import Any

import duckdb

from dvf.checks.context import CheckContext, CheckOutcome, mostly_passes, pct
from dvf.config import checks as cfg
from dvf.config.base import parse_duration
from dvf.config.models import DatasetSpec
from dvf.core.enums import Dimension, Side
from dvf.core.errors import PlanError
from dvf.models.results import SampleSet
from dvf.normalize.canonical import to_utc_naive
from dvf.plugins.base import Check, RowCheck


class NotNull(RowCheck):
    """Columns must not contain NULL."""

    dimension = Dimension.COMPLETENESS
    Config = cfg.NotNullCheck
    null_passes = False

    def predicate(self, ctx: CheckContext) -> str:
        return " AND ".join(f"{ctx.col(c)} IS NOT NULL" for c in self.config.target_columns())


class NotBlank(RowCheck):
    """Columns must not contain NULL, empty, or space-only strings."""

    dimension = Dimension.COMPLETENESS
    Config = cfg.NotBlankCheck
    null_passes = False

    def predicate(self, ctx: CheckContext) -> str:
        parts = [
            f"({ctx.col(c)} IS NOT NULL AND "
            f"{ctx.dialect.length(ctx.dialect.trim(ctx.text(ctx.col(c))))} > 0)"
            for c in self.config.target_columns()
        ]
        return " AND ".join(parts)


class NullRatio(Check):
    """The share of NULL values must not exceed a threshold."""

    dimension = Dimension.COMPLETENESS
    Config = cfg.NullRatioCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.NullRatioCheck)
        col = ctx.col(config.column)
        row = ctx.row(
            f"SELECT COUNT(*) AS total_count, "
            f"COALESCE(SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END), 0) AS null_count\n"
            f"{ctx.from_where()}"
        )
        total, nulls = int(row["total_count"] or 0), int(row["null_count"] or 0)
        ratio = 0.0 if total == 0 else nulls / total
        return CheckOutcome(
            passed=ratio <= config.max_ratio,
            message=f"{nulls:,} of {total:,} values are NULL ({ratio:.4%}); maximum {config.max_ratio:.4%}",
            observed=round(ratio, 6),
            expected=config.max_ratio,
            unexpected_count=nulls,
            total_count=total,
        )


class Unique(Check):
    """Values (or combinations of values) must be unique."""

    dimension = Dimension.UNIQUENESS
    Config = cfg.UniqueCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.UniqueCheck)
        cols = [ctx.col(c) for c in config.columns]
        col_list = ", ".join(cols)
        not_null = " AND ".join(f"{c} IS NOT NULL" for c in cols) if config.ignore_nulls else None
        groups_sql = (
            f"SELECT {col_list}, COUNT(*) AS duplicate_count\n{ctx.from_where(not_null)}\n"
            f"GROUP BY {col_list}\nHAVING COUNT(*) > 1"
        )
        row = ctx.row(
            "SELECT COUNT(*) AS duplicate_values, COALESCE(SUM(duplicate_count), 0) AS duplicate_rows\n"
            f"FROM (\n{groups_sql}\n) AS dvf_dups"
        )
        total = int(ctx.scalar(f"SELECT COUNT(*)\n{ctx.from_where()}") or 0)
        values, rows = int(row["duplicate_values"] or 0), int(row["duplicate_rows"] or 0)
        extra = rows - values
        passed = mostly_passes(extra, total, config.mostly)
        samples = None
        if values:
            samples = ctx.sample(
                ctx.dialect.limit(
                    f"{groups_sql}\nORDER BY duplicate_count DESC, {col_list}", ctx.sample_size
                )
            )
        message = (
            f"{values:,} duplicated value(s) across {rows:,} rows ({extra:,} rows beyond the first)"
            if values
            else f"all {total:,} rows are unique"
        )
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=values,
            expected=0,
            unexpected_count=extra,
            total_count=total,
            samples=samples,
            details={"duplicate_values": values, "rows_in_duplicate_groups": rows},
        )


class Regex(RowCheck):
    """Values must match a regular expression."""

    dimension = Dimension.VALIDITY
    Config = cfg.RegexCheck

    def predicate(self, ctx: CheckContext) -> str:
        config = self.config
        assert isinstance(config, cfg.RegexCheck)
        return ctx.regex_match(
            ctx.text(ctx.col(config.column)), config.pattern, full=config.full_match
        )


class AllowedValues(RowCheck):
    """Values must belong to a fixed set."""

    dimension = Dimension.VALIDITY
    Config = cfg.AllowedValuesCheck

    def predicate(self, ctx: CheckContext) -> str:
        config = self.config
        assert isinstance(config, cfg.AllowedValuesCheck)
        col = ctx.col(config.column)
        values = config.values
        if all(isinstance(v, bool) for v in values) and ctx.dialect.name != "sqlite":
            return f"{col} IN ({', '.join(ctx.literal(v) for v in values)})"
        if all(isinstance(v, int | float) and not isinstance(v, bool) for v in values):
            return f"{col} IN ({', '.join(ctx.literal(v) for v in values)})"
        texts = [str(v).lower() if isinstance(v, bool) else str(v) for v in values]
        expr = ctx.text(col)
        if not config.case_sensitive:
            expr = ctx.dialect.lower(expr)
            texts = [t.lower() for t in texts]
        return f"{expr} IN ({', '.join(ctx.literal(t) for t in texts)})"


class Range(RowCheck):
    """Values must fall within a range."""

    dimension = Dimension.VALIDITY
    Config = cfg.RangeCheck

    def predicate(self, ctx: CheckContext) -> str:
        config = self.config
        assert isinstance(config, cfg.RangeCheck)
        col = ctx.col(config.column)
        low, high = (">=", "<=") if config.inclusive else (">", "<")
        parts = []
        if config.min is not None:
            parts.append(f"{col} {low} {ctx.literal(config.min)}")
        if config.max is not None:
            parts.append(f"{col} {high} {ctx.literal(config.max)}")
        return " AND ".join(parts)


class Length(RowCheck):
    """String lengths must fall within a range."""

    dimension = Dimension.VALIDITY
    Config = cfg.LengthCheck

    def predicate(self, ctx: CheckContext) -> str:
        config = self.config
        assert isinstance(config, cfg.LengthCheck)
        length = ctx.dialect.length(ctx.text(ctx.col(config.column)))
        parts = []
        if config.min is not None:
            parts.append(f"{length} >= {config.min}")
        if config.max is not None:
            parts.append(f"{length} <= {config.max}")
        return " AND ".join(parts)


class ReferentialIntegrity(Check):
    """Every value must exist in a parent table (no orphans)."""

    dimension = Dimension.REFERENTIAL_INTEGRITY
    Config = cfg.ReferentialIntegrityCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.ReferentialIntegrityCheck)
        parent_conn_name = config.parent.connection or ctx.relation.connection
        parent_conn = ctx.connectors.get(parent_conn_name)
        parent_rel = parent_conn.resolve(DatasetSpec(table=config.parent.table))
        if not parent_conn.exists(parent_rel):
            raise PlanError(
                f"Parent table '{config.parent.table}' not found on connection '{parent_conn_name}'.",
                hint="Check 'parent.table' and 'parent.connection'.",
            )
        parent_schema = parent_conn.get_schema(parent_rel)
        parent_cols = []
        for name in config.parent.columns:
            found = parent_schema.column(name)
            if found is None:
                raise PlanError(f"Parent column '{name}' not found in '{config.parent.table}'.")
            parent_cols.append(found.name)
        child_cols = [ctx.column(c) for c in config.columns]
        if parent_conn is ctx.connector:
            return self._same_connection(ctx, parent_rel.sql, child_cols, parent_cols)
        return self._cross_connection(ctx, parent_conn, parent_rel.sql, child_cols, parent_cols)

    def _same_connection(
        self, ctx: CheckContext, parent_sql: str, child_cols: list[str], parent_cols: list[str]
    ) -> CheckOutcome:
        q = ctx.dialect.quote
        not_null = " AND ".join(f"dvf_child.{q(c)} IS NOT NULL" for c in child_cols)
        join = " AND ".join(
            f"dvf_parent.{q(p)} = dvf_child.{q(c)}"
            for c, p in zip(child_cols, parent_cols, strict=True)
        )
        child = f"(SELECT * {ctx.from_where()}) AS dvf_child"
        orphan = (
            f"NOT EXISTS (SELECT 1 FROM (SELECT * FROM {parent_sql}) AS dvf_parent WHERE {join})"
        )
        flagged = (
            f"SELECT CASE WHEN {orphan} THEN 1 ELSE 0 END AS dvf_orphan\n"
            f"FROM {child}\nWHERE {not_null}"
        )
        row = ctx.row(
            f"SELECT COUNT(*) AS total_count, COALESCE(SUM(dvf_orphan), 0) AS orphan_count\n"
            f"FROM ({flagged}) AS dvf_flags"
        )
        total, orphans = int(row["total_count"] or 0), int(row["orphan_count"] or 0)
        samples = None
        if orphans:
            columns = ", ".join(
                f"dvf_child.{q(c)}" for c in dict.fromkeys([*ctx.key_columns_real(), *child_cols])
            )
            samples = ctx.sample(
                ctx.dialect.limit(
                    f"SELECT {columns}\nFROM {child}\nWHERE {not_null} AND {orphan}",
                    ctx.sample_size,
                )
            )
        return self._outcome(ctx, orphans, total, samples)

    def _cross_connection(
        self,
        ctx: CheckContext,
        parent_conn: Any,
        parent_sql: str,
        child_cols: list[str],
        parent_cols: list[str],
    ) -> CheckOutcome:
        cq, pq = ctx.dialect.quote, parent_conn.sql_dialect.quote
        child_keys = ", ".join(f"{ctx.text(cq(c))} AS k{i}" for i, c in enumerate(child_cols))
        child_not_null = " AND ".join(f"{cq(c)} IS NOT NULL" for c in child_cols)
        group = ", ".join(f"k{i}" for i in range(len(child_cols)))
        child_table = ctx.query(
            f"SELECT {group}, COUNT(*) AS n FROM (SELECT {child_keys} {ctx.from_where(child_not_null)}) AS c "
            f"GROUP BY {group}"
        )
        pd = parent_conn.sql_dialect
        parent_keys = ", ".join(f"{pd.to_text(pq(p))} AS k{i}" for i, p in enumerate(parent_cols))
        parent_table = ctx.recorder.run(
            parent_conn, f"SELECT DISTINCT {parent_keys} FROM {parent_sql}", Side.SOURCE
        )
        con = duckdb.connect()
        try:
            con.register("child_keys", child_table)
            con.register("parent_keys", parent_table)
            join = " AND ".join(f"p.k{i} = c.k{i}" for i in range(len(child_cols)))
            orphan_sql = (
                f"SELECT c.* FROM child_keys c WHERE NOT EXISTS "
                f"(SELECT 1 FROM parent_keys p WHERE {join})"
            )
            total = int(con.execute("SELECT COALESCE(SUM(n), 0) FROM child_keys").fetchone()[0])  # type: ignore[index]
            orphans = int(
                con.execute(f"SELECT COALESCE(SUM(n), 0) FROM ({orphan_sql})").fetchone()[0]  # type: ignore[index]
            )
            sample_rows = con.execute(
                f"{orphan_sql} ORDER BY n DESC LIMIT {int(ctx.sample_size)}"
            ).fetchall()
        finally:
            con.close()
        samples = None
        if orphans and ctx.sample_size and not ctx.privacy.strict:
            columns = [*child_cols, "row_count"]
            samples = SampleSet(
                columns=columns,
                rows=ctx.privacy.mask_rows(columns, sample_rows),
                masked=ctx.privacy.mask_pii,
                truncated=len(sample_rows) >= ctx.sample_size,
            )
        return self._outcome(ctx, orphans, total, samples)

    def _outcome(
        self, ctx: CheckContext, orphans: int, total: int, samples: SampleSet | None
    ) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.ReferentialIntegrityCheck)
        parent = f"{config.parent.table}({', '.join(config.parent.columns)})"
        share = pct(orphans, total)
        return CheckOutcome(
            passed=mostly_passes(orphans, total, config.mostly),
            message=(
                f"{orphans:,} of {total:,} rows ({share:.4g}%) have no match in {parent}"
                if orphans and share is not None
                else f"all {total:,} rows have a match in {parent}"
            ),
            observed=orphans,
            expected=0,
            unexpected_count=orphans,
            total_count=total,
            samples=samples,
        )


_OPERATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


class CustomSql(Check):
    """A SQL query returning failing rows, or a scalar compared with an expectation."""

    dimension = Dimension.CUSTOM
    Config = cfg.CustomSqlCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.CustomSqlCheck)
        sql = ctx.render(config.sql).replace("{table}", ctx.table_reference()).strip().rstrip(";")
        if config.returns == "scalar":
            assert config.expect is not None
            value = ctx.scalar(sql)
            try:
                number = float(value)
            except (TypeError, ValueError):
                return CheckOutcome(
                    passed=False,
                    message=f"the query returned {value!r}, which is not a number",
                    observed=value,
                    expected=f"{config.expect.operator} {config.expect.value:g}",
                )
            passed = _OPERATORS[config.expect.operator](number, config.expect.value)
            return CheckOutcome(
                passed=passed,
                message=f"observed {number:g}, expected {config.expect.operator} {config.expect.value:g}",
                observed=value,
                expected=f"{config.expect.operator} {config.expect.value:g}",
            )
        failing = int(ctx.scalar(f"SELECT COUNT(*) FROM (\n{sql}\n) AS dvf_custom") or 0)
        total = int(ctx.scalar(f"SELECT COUNT(*)\n{ctx.from_where()}") or 0)
        samples = None
        if failing:
            samples = ctx.sample(
                ctx.dialect.limit(f"SELECT * FROM (\n{sql}\n) AS dvf_custom", ctx.sample_size)
            )
        passed = (
            failing == 0 if config.mostly >= 1 else mostly_passes(failing, total, config.mostly)
        )
        return CheckOutcome(
            passed=passed,
            message=f"the query returned {failing:,} failing row(s)"
            if failing
            else "the query returned no rows",
            observed=failing,
            expected=0,
            unexpected_count=failing,
            total_count=total,
            samples=samples,
        )


class Expression(RowCheck):
    """Every row must satisfy a boolean SQL expression (rows where it is NULL fail)."""

    dimension = Dimension.CONSISTENCY
    Config = cfg.ExpressionCheck
    null_passes = False

    def predicate(self, ctx: CheckContext) -> str:
        config = self.config
        assert isinstance(config, cfg.ExpressionCheck)
        return ctx.render(config.expression)


class RowCountRange(Check):
    """The number of rows must fall within a range."""

    dimension = Dimension.VOLUME
    Config = cfg.RowCountRangeCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.RowCountRangeCheck)
        count = int(ctx.scalar(f"SELECT COUNT(*)\n{ctx.from_where()}") or 0)
        low_ok = config.min is None or count >= config.min
        high_ok = config.max is None or count <= config.max
        bounds = " and ".join(
            p
            for p in (
                f">= {config.min:,}" if config.min is not None else "",
                f"<= {config.max:,}" if config.max is not None else "",
            )
            if p
        )
        return CheckOutcome(
            passed=low_ok and high_ok,
            message=f"{count:,} rows; expected {bounds}",
            observed=count,
            expected=bounds,
            total_count=count,
        )


class Freshness(Check):
    """The newest value of a timestamp column must be younger than ``max_age``."""

    dimension = Dimension.TIMELINESS
    Config = cfg.FreshnessCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.FreshnessCheck)
        latest = ctx.scalar(f"SELECT MAX({ctx.col(config.column)})\n{ctx.from_where()}")
        max_age = parse_duration(config.max_age)
        if latest is None:
            return CheckOutcome(
                passed=False,
                message=f"no values in {config.column}",
                expected=f"newer than {config.max_age}",
            )
        newest = to_utc_naive(latest, ctx.normalization.timezone)
        now = datetime.now(UTC).replace(tzinfo=None)
        age = now - newest
        return CheckOutcome(
            passed=age <= max_age,
            message=f"newest {config.column} is {_human(age.total_seconds())} old; maximum {config.max_age}",
            observed=newest.isoformat(),
            expected=f"newer than {config.max_age}",
            details={"age_seconds": round(age.total_seconds(), 3)},
        )


def _human(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if abs(seconds) >= size:
            return f"{seconds / size:.1f}{unit}"
    return f"{seconds:.0f}s"


BUILTIN_CHECKS: dict[str, type[Check]] = {
    "not_null": NotNull,
    "not_blank": NotBlank,
    "null_ratio": NullRatio,
    "unique": Unique,
    "regex": Regex,
    "allowed_values": AllowedValues,
    "range": Range,
    "length": Length,
    "referential_integrity": ReferentialIntegrity,
    "custom_sql": CustomSql,
    "expression": Expression,
    "row_count_range": RowCountRange,
    "freshness": Freshness,
}
