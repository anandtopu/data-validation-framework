"""What a check sees while it runs (:class:`CheckContext`) and what it returns (:class:`CheckOutcome`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Dimension, Side, Status
from dvf.core.errors import PlanError
from dvf.execution.context import ConnectorPool, QueryRecorder, RunInfo
from dvf.models.results import (
    ColumnMismatch,
    MetricComparison,
    QueryRecord,
    SampleSet,
    TimingBreakdown,
)
from dvf.plugins.base import Connector, Relation
from dvf.security.masking import Privacy

if TYPE_CHECKING:
    import pyarrow as pa

    from dvf.config.checks import CheckConfig
    from dvf.config.models import NormalizationConfig
    from dvf.sql.dialects import Dialect


@dataclass
class CheckOutcome:
    """The result of evaluating one check, before dvf adds identity, timing, and hooks.

    Set :attr:`passed` and let dvf derive the status from the severity, or set :attr:`status`
    explicitly (for example ``skipped``).
    """

    passed: bool | None = None
    status: Status | None = None
    message: str | None = None
    observed: Any = None
    expected: Any = None
    unexpected_count: int | None = None
    total_count: int | None = None
    samples: SampleSet | None = None
    column_mismatches: list[ColumnMismatch] = field(default_factory=list)
    metrics: list[MetricComparison] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    queries: list[QueryRecord] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    normalization: list[str] = field(default_factory=list)
    engine: str | None = None
    timing: TimingBreakdown | None = None
    # Identity overrides, used by comparators and schema checks that emit several results.
    type: str | None = None
    name: str | None = None
    columns: list[str] = field(default_factory=list)
    dimension: Dimension | None = None
    severity_cap: str | None = None
    """Highest severity this outcome may carry (``warning`` for informational comparisons)."""
    extract: pa.Table | None = None
    """Failing rows or mismatched values beyond the samples (masked, text), written as an extract
    file when extracts are enabled. Not part of the results document."""


def pct(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(100.0 * part / whole, 6)


def mostly_passes(unexpected: int, total: int, mostly: float) -> bool:
    """Whether the share of valid rows reaches ``mostly`` (an empty dataset passes)."""
    if total == 0:
        return True
    return (total - unexpected) / total >= mostly - 1e-12


class CheckContext:
    """Access to the checked dataset, SQL helpers, and evidence capture.

    Plugin checks receive one of these in :meth:`~dvf.plugins.Check.evaluate`.
    """

    def __init__(
        self,
        *,
        config: CheckConfig,
        side: Side,
        connector: Connector,
        relation: Relation,
        schema: DatasetSchema | None,
        sample_size: int,
        key_columns: list[str],
        run: RunInfo,
        connectors: ConnectorPool,
        privacy: Privacy,
        normalization: NormalizationConfig,
    ) -> None:
        self.config = config
        self.side = side
        self.connector = connector
        self.relation = relation
        self.schema = schema
        self.sample_size = sample_size
        self.key_columns = key_columns
        self.run = run
        self.connectors = connectors
        self.privacy = privacy
        self.normalization = normalization
        self.recorder = QueryRecorder()
        self.extract_rows = 0
        """Failing rows to capture for an extract file (0: samples only). Set by the runner."""

    # ------------------------------------------------------------------ SQL helpers
    @property
    def dialect(self) -> Dialect:
        return self.connector.sql_dialect

    def column(self, name: str) -> str:
        """The real name of column ``name`` (matched case-insensitively)."""
        if self.schema is None:
            return name
        found = self.schema.column(name)
        if found is None:
            available = ", ".join(c.name for c in self.schema.columns[:30])
            raise PlanError(
                f"Column '{name}' not found in {self.relation.ref.display()}.",
                hint=f"Available columns: {available}",
            )
        return found.name

    def col(self, name: str) -> str:
        """Column ``name`` as a quoted SQL identifier."""
        return self.dialect.quote(self.column(name))

    def key_columns_real(self) -> list[str]:
        """The table's key columns on this side that exist in the dataset."""
        names: list[str] = []
        for name in self.key_columns:
            try:
                names.append(self.column(name))
            except PlanError:
                continue
        return names

    def literal(self, value: Any) -> str:
        return self.dialect.literal(value)

    def text(self, expr: str) -> str:
        return self.dialect.to_text(expr)

    def regex_match(self, expr: str, pattern: str, *, full: bool = True) -> str:
        return self.dialect.regex_match(expr, pattern, full=full)

    def render(self, text: str) -> str:
        """Replace ``${run.*}`` placeholders."""
        return self.run.render(text)

    @property
    def where(self) -> str | None:
        return self.render(self.config.where) if self.config.where else None

    def from_where(self, extra: str | None = None) -> str:
        """``FROM <dataset> [WHERE <where> AND <extra>]``."""
        conditions = [c for c in (self.where, extra) if c]
        clause = f"FROM {self.relation.sql}"
        if conditions:
            clause += "\nWHERE " + " AND ".join(f"({c})" for c in conditions)
        return clause

    def table_reference(self) -> str:
        """What ``{table}`` stands for in custom SQL."""
        if self.relation.table is not None and not self.relation.filter:
            return self.relation.table
        return f"(SELECT * FROM {self.relation.sql}) AS dvf_table"

    # ------------------------------------------------------------------ queries
    def query(self, sql: str) -> pa.Table:
        """Run SQL on the checked dataset's connection, recording it for reproducibility."""
        return self.recorder.run(self.connector, sql, self.side)

    def scalar(self, sql: str) -> Any:
        table = self.query(sql)
        if table.num_rows == 0 or table.num_columns == 0:
            return None
        return table.column(0)[0].as_py()

    def row(self, sql: str) -> dict[str, Any]:
        rows = self.query(sql).to_pylist()
        return rows[0] if rows else {}

    # ------------------------------------------------------------------ evidence
    def sample(self, sql: str, *, mask_columns: bool = True) -> SampleSet | None:
        """Run a sample query (already limited) and return masked rows."""
        if self.sample_size <= 0 or self.privacy.strict:
            return None
        table = self.query(sql)
        columns = list(table.column_names)
        rows = [list(r.values()) for r in table.to_pylist()]
        masked = self.privacy.mask_rows(columns, rows) if mask_columns else rows
        return SampleSet(
            columns=columns,
            rows=masked,
            masked=self.privacy.mask_pii,
            truncated=len(rows) >= self.sample_size,
        )

    def evidence(self, sql: str) -> tuple[SampleSet | None, pa.Table | None]:
        """Masked samples and, when extracts are enabled, a larger masked extract, from one query
        of failing rows (``sql`` is unlimited; dvf adds the limit)."""
        limit = max(self.sample_size, self.extract_rows)
        if limit <= 0 or self.privacy.strict:
            return None, None
        table = self.query(self.dialect.limit(sql, limit))
        columns = list(table.column_names)
        rows = [list(r.values()) for r in table.to_pylist()]
        masked = self.privacy.mask_rows(columns, rows)
        samples = None
        if self.sample_size > 0:
            samples = SampleSet(
                columns=columns,
                rows=masked[: self.sample_size],
                masked=self.privacy.mask_pii,
                truncated=len(rows) >= self.sample_size,
            )
        extract = None
        if self.extract_rows > 0 and len(rows) > self.sample_size:
            from dvf.execution.artifacts import text_table

            extract = text_table(columns, masked)
        return samples, extract

    def sample_columns(self, columns: list[str]) -> str:
        """Key columns followed by ``columns``, quoted, without duplicates (or ``*``)."""
        names: list[str] = []
        for name in [*self.key_columns, *columns]:
            try:
                real = self.column(name)
            except PlanError:
                continue
            if real not in names:
                names.append(real)
        return ", ".join(self.dialect.quote(n) for n in names) if names else "*"

    def evaluate_predicate(
        self, predicate: str, *, columns: list[str], null_passes: bool = True
    ) -> CheckOutcome:
        """Count rows where ``predicate`` is not true, apply ``mostly``, and sample failures."""
        valid = f"({predicate})"
        if null_passes and columns:
            all_null = " AND ".join(f"{self.col(c)} IS NULL" for c in columns)
            valid = f"(({all_null}) OR {valid})"
        failing = f"(CASE WHEN {valid} THEN 0 ELSE 1 END) = 1"
        row = self.row(
            f"SELECT COUNT(*) AS total_count, "
            f"COALESCE(SUM(CASE WHEN {valid} THEN 0 ELSE 1 END), 0) AS unexpected_count\n"
            f"{self.from_where()}"
        )
        total = int(row.get("total_count") or 0)
        unexpected = int(row.get("unexpected_count") or 0)
        mostly = self.config.mostly
        passed = mostly_passes(unexpected, total, mostly)
        samples = None
        extract = None
        if unexpected:
            samples, extract = self.evidence(
                f"SELECT {self.sample_columns(columns)}\n{self.from_where(failing)}"
            )
        share = pct(unexpected, total)
        message = (
            f"{unexpected:,} of {total:,} rows ({share:.4g}%) fail"
            if unexpected and share is not None
            else f"all {total:,} rows pass"
        )
        if unexpected and not passed and mostly < 1:
            message += f"; at least {mostly:.4%} must pass"
        elif unexpected and passed:
            message += f", within mostly={mostly}"
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=None if total == 0 else round((total - unexpected) / total, 6),
            expected=mostly,
            unexpected_count=unexpected,
            total_count=total,
            samples=samples,
            extract=extract,
        )
