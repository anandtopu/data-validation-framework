"""The root configuration model and its sections (ADR 0006).

Models here describe the *resolved* configuration: the loader has already applied layers,
interpolation, and inheritance, so every table carries its effective settings.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BeforeValidator,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    SecretStr,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from dvf.config.base import (
    ComponentName,
    Duration,
    ExactlyOneOf,
    Identifier,
    NormalizerInput,
    StrictModel,
)
from dvf.config.checks import CheckConfig
from dvf.config.connections import ConnectionConfig
from dvf.config.notifications import NotificationConfig
from dvf.core.enums import SchemaCheck, Severity, Status

CONFIG_VERSION = 1
"""The configuration format version this dvf reads."""


# ====================================================================== defaults


class FloatCompare(StrictModel):
    """Tolerance for comparing floating-point values (they are never hashed directly)."""

    abs_tol: NonNegativeFloat = 0.0
    """Absolute difference accepted between floating-point values; the larger of this and
    ``tolerance.numeric_abs`` applies."""
    rel_tol: NonNegativeFloat = 1e-9
    """Difference accepted between floating-point values, relative to the larger magnitude.
    ``tolerance.numeric_rel`` replaces it when set."""


class NormalizationConfig(StrictModel):
    """How values are canonicalized before comparison or hashing."""

    trim: Literal["none", "rtrim", "ltrim", "both"] = "none"
    """Whitespace trimming. ``rtrim`` suits blank-padded CHAR columns."""
    case_sensitive: bool = True
    """Set to false to compare text case-insensitively (both sides are lower-cased)."""
    null_equals_empty: bool = False
    """Treat NULL and empty string as equal."""
    unicode_form: Literal["NFC", "NFD", "NFKC", "NFKD", "none"] = "NFC"
    """Unicode normalization applied to text, or ``none``. Databases without a normalization
    function compare text without it, and the result says so."""
    collapse_whitespace: bool = False
    """Replace each run of whitespace in text with a single space."""
    timestamp_precision: Literal["s", "ms", "us", "ns"] = "us"
    """Timestamps are truncated to this precision on both sides."""
    timezone: str = "UTC"
    """Time zone that timestamps without an offset are assumed to be in."""
    decimal_scale: Literal["auto"] | NonNegativeInt = "auto"
    """Fixed scale for decimals, or ``auto`` to use the larger scale of the two sides."""
    float_compare: FloatCompare = Field(default_factory=FloatCompare)
    """Tolerance for floating-point values, which are compared numerically and never hashed."""

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if value.upper() in ("UTC", "Z"):
            return "UTC"
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone '{value}'") from exc
        return value


class PrivacyConfig(StrictModel):
    """Protection of personal data in samples and reports."""

    mask_pii: bool = True
    """Mask values in columns tagged or detected as personal data."""
    mode: Literal["standard", "strict"] = "standard"
    """``strict`` keeps every raw value out of results, reports, extracts, repro files, and
    notifications: no samples or extracts, literals removed from recorded SQL, minimum and
    maximum values hidden."""
    pii_columns: list[str] = Field(default_factory=list)
    """Column names (glob patterns allowed) always treated as personal data."""
    masking: Literal["mask", "hash", "tokenize"] = "mask"
    """How protected sample values are written: ``mask`` (``***``, keeping a hint such as
    ``j***@example.com``), ``hash`` (keyed HMAC-SHA256 digest, equal values give equal digests),
    or ``tokenize`` (``tok_000042``, consistent within a run)."""
    detect_patterns: bool = True
    """Also protect values that look like email addresses, phone numbers, national IDs, payment
    card numbers, or IBANs, in any column."""
    hash_key: SecretStr | None = None
    """Key for ``masking: hash`` (use a ``${secret:...}`` reference). Without it, a random key per
    run is used, so digests cannot be compared across runs."""


class ExecutionConfig(StrictModel):
    """Concurrency, timeouts, retries, local engine resources, and checkpoints."""

    max_parallel_tables: PositiveInt = 4
    """Tables validated at the same time (project-level setting)."""
    max_queries_per_connection: PositiveInt = 2
    """Default cap on tables using one connection at the same time; a connection's own
    ``max_parallel_queries`` overrides it."""
    query_timeout_s: PositiveInt = 1800
    """Query timeout in seconds for connections that set no ``query_timeout_s`` of their own."""
    retries: NonNegativeInt = 2
    """Retries of a query that failed with a transient error (lost connection, deadlock, timeout)."""
    retry_backoff_s: NonNegativeFloat = 2.0
    """First retry delay in seconds; it doubles on each retry."""
    memory_limit: str | None = Field(
        default=None, pattern=r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$"
    )
    """Memory limit of the local DuckDB engine, such as ``4GB``. It spills to disk beyond it."""
    threads: PositiveInt | None = None
    """Threads of the local DuckDB engine (default: all cores)."""
    temp_dir: str | None = None
    """Folder for the local engine's spill files (default: the system temporary folder)."""
    checkpoints: bool = True
    """Save finished tables and partitions so ``dvf run --resume RUN_ID`` can continue a run."""
    state_dir: str = ".dvf"
    """Folder (relative to the configuration) for checkpoints, run results, and history."""


class ToleranceConfig(StrictModel):
    """Differences that are accepted as equal."""

    numeric_abs: NonNegativeFloat | None = None
    """Accepted absolute difference between numeric values; sums may differ by it once per row."""
    numeric_rel: NonNegativeFloat | None = None
    """Accepted difference between numeric values relative to the larger one, such as ``0.001`` for
    0.1 percent."""
    timestamp: Duration | None = None
    """Accepted difference between dates or timestamps, such as ``1s``."""
    row_count_pct: float | None = Field(default=None, ge=0, le=100)
    """Accepted row count difference, as a percentage of the source count."""


class SampleConfig(StrictModel):
    """Statistical sample comparison (the ``sample`` strategy, REC-07) for tables too large to
    compare in full.

    Size the sample in exactly one way: ``margin_of_error`` (with ``confidence``, by Cochran's
    formula with the finite population correction), ``fraction``, or ``rows``. Rows are chosen
    by a seeded hash of the canonical key, so both sides (in any database) select the same keys.
    """

    fraction: float | None = Field(default=None, gt=0, le=1)
    """Share of the keys to compare."""
    rows: PositiveInt | None = None
    """Number of keys to compare (expected; the realized size varies slightly)."""
    margin_of_error: float | None = Field(default=None, gt=0, lt=1)
    """Wanted half-width of the confidence interval of the mismatch rate (``0.01`` is plus or
    minus one percentage point); the sample size follows from Cochran's formula."""
    confidence: float = Field(default=0.95, gt=0, lt=1)
    """Confidence level of the reported interval (and of ``margin_of_error``)."""
    expected_rate: float = Field(default=0.5, gt=0, lt=1)
    """Mismatch rate assumed when sizing by ``margin_of_error``; 0.5 is the most conservative."""
    seed: int = 42
    """Changes which keys are sampled; the same seed always selects the same keys."""
    stratify_by: list[str] = Field(default_factory=list)
    """Paired columns whose values form strata, each sampled in proportion to its size."""
    min_per_stratum: NonNegativeInt = 0
    """Expected sampled keys per stratum at least (small strata are oversampled and reweighted)."""
    max_strata: PositiveInt = Field(default=1000, le=100_000)
    """Most strata allowed; more is an error (stratify by fewer or coarser columns)."""
    max_mismatch_rate: float = Field(default=0.0, ge=0, le=1)
    """The comparison passes when the estimated mismatch rate is at most this (0: no sampled
    row may differ)."""

    @model_validator(mode="after")
    def _one_size(self) -> SampleConfig:
        ExactlyOneOf.check(self, ("fraction", "rows", "margin_of_error"))
        return self


class PartitionPredicate(StrictModel):
    """A named partition given as a SQL predicate per side."""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.=-]+$")
    """Unique name of the partition: letters, digits, and ``_.=-``."""
    source: str = Field(min_length=1)
    """SQL predicate selecting the partition's source rows."""
    target: str | None = None
    """Predicate on the target; defaults to the source predicate."""


class PartitionConfig(StrictModel):
    """Split row-level comparisons into independent partitions.

    Each partition is compared on its own (and checkpointed), and the partition results are
    combined. Use exactly one of ``values``, ``ranges``, ``hash_buckets``, or ``predicates``.
    """

    column: str | None = None
    """Paired column (target or source name) for ``values`` and ``ranges``."""
    values: Literal["auto"] | list[Any] | None = None
    """One partition per value; ``auto`` reads the distinct values from the source. Rows with
    other values or NULL form one more partition."""
    ranges: list[Any] | None = Field(default=None, min_length=1)
    """Ascending boundaries; partitions are below the first, between neighbors, from the last
    upward, and NULL."""
    hash_buckets: PositiveInt | None = Field(default=None, le=4096)
    """Split by a hash of the key columns into this many partitions."""
    predicates: list[PartitionPredicate] | None = Field(default=None, min_length=1)
    """Named partitions given as a SQL predicate per side; make them cover every row."""
    max_partitions: PositiveInt = Field(default=1000, le=100_000)
    """Most partitions allowed, not counting the extra partitions for other values and NULL; more is
    an error."""

    @model_validator(mode="after")
    def _one_kind(self) -> PartitionConfig:
        ExactlyOneOf.check(self, ("values", "ranges", "hash_buckets", "predicates"))
        if (self.values is not None or self.ranges is not None) and not self.column:
            raise ValueError("'values' and 'ranges' partitions need 'column'")
        return self


Aggregate = Literal[
    "count", "sum", "min", "max", "avg", "count_distinct", "null_count", "sum_length"
]


_DEFAULT_AGGREGATES: list[Aggregate] = ["count", "sum", "min", "max", "null_count"]


class ComparisonConfig(StrictModel):
    """Source-vs-target comparison settings."""

    enabled: bool = True
    """Set to false to skip the comparison strategies; schema checks and the table's checks still
    run."""
    strategies: list[ComponentName] = Field(
        default_factory=lambda: ["row_count", "aggregate"], min_length=1
    )
    """Comparison strategies, cheapest first. They escalate: count, aggregate, then hash."""
    on_mismatch: ComponentName | None = "full_diff"
    """Strategy used to explain mismatches found by the strategies above (or none)."""
    buckets: PositiveInt = Field(default=256, le=1_000_000)
    """Number of first-level hash buckets for ``hash_bucket``."""
    drilldown_fanout: int = Field(default=16, ge=2, le=1024)
    """Sub-buckets (``hash_bucket``) or key segments (``bisect``) per mismatched bucket or segment."""
    drilldown_threshold: PositiveInt = 10_000
    """Stop drilling down once the mismatched buckets or segments hold at most this many rows,
    and fetch their row hashes."""
    partitions: PartitionConfig | None = None
    """Compare row-level strategies partition by partition."""
    group_by: list[str] = Field(default_factory=list)
    """Columns for grouped counts and control totals (for example company code, period)."""
    aggregates: list[Aggregate] = Field(default_factory=lambda: list(_DEFAULT_AGGREGATES))
    """Aggregates the ``aggregate`` strategy compares per column, overall or per ``group_by``
    group."""
    tolerance: ToleranceConfig = Field(default_factory=ToleranceConfig)
    """Differences accepted as equal; a column mapping's ``tolerance`` replaces it for that
    column."""
    sample: SampleConfig | None = None
    """Sample size and strata of the ``sample`` strategy (REC-07). Without it, the strategy sizes
    the sample for a margin of error of one percentage point."""
    max_mismatch_rows: PositiveInt = 100_000
    """Upper bound on mismatched rows fetched for diagnosis."""


_DEFAULT_SCHEMA_CHECKS = [
    SchemaCheck.OBJECT_EXISTS,
    SchemaCheck.COLUMNS_PRESENT,
    SchemaCheck.TYPE_COMPATIBILITY,
    SchemaCheck.LENGTH_PRECISION,
    SchemaCheck.NULLABILITY,
]

NAMING_PRESETS: dict[str, str] = {
    "snake_case": r"[a-z][a-z0-9]*(_[a-z0-9]+)*",
    "upper_snake_case": r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*",
    "camel_case": r"[a-z][a-zA-Z0-9]*",
    "pascal_case": r"[A-Z][a-zA-Z0-9]*",
}
"""Regular expressions of the naming rule ``preset`` values."""


class NamingRule(StrictModel):
    """A naming standard for table or column names (schema check ``naming_standards``)."""

    name: str | None = None
    """Label shown in results; defaults to the pattern or preset."""
    pattern: str | None = None
    """Regular expression every name must match in full (Python syntax)."""
    preset: Literal["snake_case", "upper_snake_case", "camel_case", "pascal_case"] | None = None
    """A predefined pattern instead of ``pattern``."""
    max_length: PositiveInt | None = None
    """Longest allowed name, in characters."""
    applies_to: Literal["tables", "columns", "all"] = "all"
    """Which names the rule checks: table names, column names, or ``all``."""
    exclude: list[str] = Field(default_factory=list)
    """Names exempt from the rule (glob patterns, case-insensitive)."""

    @model_validator(mode="after")
    def _shape(self) -> NamingRule:
        ExactlyOneOf.check(self, ("pattern", "preset"), required=self.max_length is None)
        if self.pattern is not None:
            import re

            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression '{self.pattern}': {exc}") from exc
        return self

    @property
    def regex(self) -> str | None:
        return self.pattern if self.pattern is not None else NAMING_PRESETS.get(self.preset or "")

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        parts = [self.preset or self.pattern or ""]
        if self.max_length is not None:
            parts.append(f"max {self.max_length} characters")
        return ", ".join(p for p in parts if p)


class NamingConfig(StrictModel):
    """Naming standards checked by ``naming_standards`` (SCH-14)."""

    rules: list[NamingRule] = Field(default_factory=list)
    """Rules the checked names must follow; without any, ``naming_standards`` is skipped."""
    side: Literal["source", "target", "both"] = "target"
    """Which dataset's names are checked."""


DriftAspect = Literal["columns", "types", "nullability", "primary_key", "order"]
_DEFAULT_DRIFT_ASPECTS: tuple[DriftAspect, ...] = ("columns", "types", "nullability", "primary_key")


class DriftConfig(StrictModel):
    """The baseline of ``schema_drift`` (SCH-11)."""

    baseline: str = "previous_run"
    """``previous_run`` (the schema saved by the last run under the state folder) or the path of
    a committed snapshot file (JSON or YAML, relative to the configuration)."""
    side: Literal["source", "target", "both"] = "both"
    """Which datasets are compared with their baseline."""
    ignore_columns: list[str] = Field(default_factory=list)
    """Columns ignored by the comparison (glob patterns, case-insensitive)."""
    compare: list[DriftAspect] = Field(default_factory=lambda: list(_DEFAULT_DRIFT_ASPECTS))
    """What counts as drift: added or removed columns, type changes, nullability changes,
    primary key changes, and (opt-in) column order changes."""


class SchemaOptionsConfig(StrictModel):
    """Settings of the schema checks that need more than the check name."""

    naming: NamingConfig = Field(default_factory=NamingConfig)
    """Naming standards checked by ``naming_standards`` (SCH-14)."""
    drift: DriftConfig = Field(default_factory=DriftConfig)
    """The baseline compared by ``schema_drift`` (SCH-11)."""


class DefaultsConfig(StrictModel):
    """Settings inherited by suites and tables."""

    engine: ComponentName = "auto"
    """Where row-level comparisons run: ``auto``, ``pushdown``, ``duckdb``, ``polars``, ``spark``,
    or an engine added by a plugin."""
    severity: Severity = Severity.ERROR
    """Severity of checks that set none."""
    sample_size: NonNegativeInt = Field(default=50, le=10_000)
    """Failing rows captured per check as evidence (masked by privacy settings)."""
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)
    """Canonicalization settings inherited by suites and tables."""
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    """Masking of personal data in samples, reports, and extracts."""
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    """Concurrency, timeouts, retries, local engine resources, and checkpoints; read from the
    project-level ``defaults`` only."""
    comparison: ComparisonConfig = Field(default_factory=ComparisonConfig)
    """Source-vs-target comparison settings inherited by suites and tables."""
    schema_checks: list[SchemaCheck] = Field(default_factory=lambda: list(_DEFAULT_SCHEMA_CHECKS))
    """Schema checks run for comparison tables; single-dataset suites report ``object_exists``
    only."""
    schema_options: SchemaOptionsConfig = Field(default_factory=SchemaOptionsConfig)
    """Naming rules (SCH-14) and the drift baseline (SCH-11)."""


INHERITED_TABLE_FIELDS = (
    "engine",
    "severity",
    "sample_size",
    "normalization",
    "comparison",
    "schema_checks",
    "schema_options",
)
"""Fields a table inherits from its suite's effective defaults."""


# ====================================================================== datasets and tables


class ProjectConfig(StrictModel):
    name: str = Field(min_length=1)
    """Project name, recorded with every run; run history is kept per project."""
    description: str | None = None
    """Free-text description of the project."""
    owner: str | None = None
    """Team or person responsible for the project."""
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Free-form metadata copied into every report (release, environment, approver)."""


class DatasetSpec(StrictModel):
    """One side of a table comparison: a table, a query, or a file path."""

    connection: Identifier | None = None
    """Overrides the suite's connection for this side."""
    table: str | None = None
    """Table or view name, optionally qualified (``schema.table``)."""
    query: str | None = None
    """A SQL query used as the dataset."""
    path: str | None = None
    """File or folder path, relative to a file connection's base path."""
    filter: str | None = None
    """SQL predicate applied to this side (scope and exclusions)."""
    as_of: str | None = None
    """Time travel: read the dataset as of this timestamp or version, where supported."""
    options: dict[str, Any] = Field(default_factory=dict)
    """Connector-specific dataset settings, such as file reader options, NoSQL filters and
    projections, or RFC read settings."""

    @model_validator(mode="after")
    def _at_most_one_locator(self) -> DatasetSpec:
        ExactlyOneOf.check(self, ("table", "query", "path"), required=False)
        return self

    @property
    def has_locator(self) -> bool:
        return any(v is not None for v in (self.table, self.query, self.path))


class PatternSpec(StrictModel):
    """Glob patterns selecting many tables at once; tables are paired by name."""

    source: str = Field(min_length=1)
    """Glob pattern (``*``, ``?``) matched case-insensitively against the source connection's table
    names."""
    target: str | None = None
    """Target table name, with each ``*`` or ``?`` replaced by what it matched in the source name;
    defaults to the source table's name."""


class KeyMapping(StrictModel):
    """A key column, optionally renamed and normalized on the target side."""

    source: str
    """Source key column; also the target column unless ``target`` is set."""
    target: str | None = None
    """Target column name; defaults to the source name."""
    normalize: list[NormalizerInput] = Field(default_factory=list)
    """Normalizers applied to the key on both sides before rows are paired, for example
    ``sap_alpha``."""

    @property
    def target_name(self) -> str:
        return self.target or self.source


def _coerce_key(value: Any) -> Any:
    return {"source": value} if isinstance(value, str) else value


KeyInput = Annotated[
    KeyMapping, BeforeValidator(_coerce_key, json_schema_input_type=str | KeyMapping)
]


class ColumnMapping(StrictModel):
    """How a target column relates to the source: renamed, derived, or normalized."""

    source: str | None = None
    """Source column name. Set exactly one of ``source`` and ``source_expr``."""
    source_expr: str | None = None
    """A source-side SQL expression the target column is derived from."""
    target: str
    """Target column name; each target column may be mapped only once."""
    normalize: list[NormalizerInput] = Field(default_factory=list)
    """Normalizers applied to this column on both sides before comparison, in order."""
    compare: bool = True
    """Set to false to exclude this column from value comparison."""
    tolerance: ToleranceConfig | None = None
    """Tolerance for this column; replaces ``comparison.tolerance``."""
    pii: bool = False
    """Treat this column as personal data: its values are masked when ``privacy.mask_pii`` is on."""

    @model_validator(mode="after")
    def _one_source(self) -> ColumnMapping:
        ExactlyOneOf.check(self, ("source", "source_expr"))
        return self


def _coerce_include(value: Any) -> Any:
    return [value] if isinstance(value, str) else value


class ColumnsConfig(StrictModel):
    """Which columns to compare and how they map."""

    include: Annotated[
        list[str],
        BeforeValidator(_coerce_include, json_schema_input_type=str | list[str]),
    ] = Field(default_factory=lambda: ["*"])
    """Columns to compare; ``*`` means all mapped columns. Glob patterns are allowed."""
    exclude: list[str] = Field(default_factory=list)
    """Columns left out of the comparison, by source or target name. Glob patterns are allowed, and
    exclusions win over ``include``."""
    mappings: list[ColumnMapping] = Field(default_factory=list)
    """Renamed, derived, or normalized columns, with optional per-column tolerances and PII tags."""
    match: Literal["exact", "case_insensitive"] = "case_insensitive"
    """How unmapped source and target column names are paired."""

    @model_validator(mode="after")
    def _unique_targets(self) -> ColumnsConfig:
        seen: set[str] = set()
        for mapping in self.mappings:
            key = mapping.target.lower()
            if key in seen:
                raise ValueError(f"target column '{mapping.target}' is mapped more than once")
            seen.add(key)
        return self


WindowBound = datetime | date | int | float | str


class WindowConfig(StrictModel):
    """Compare only rows inside a watermark window, for delta and incremental loads (MIG-02).

    Each side keeps the rows with ``start <= column < end`` (``end_inclusive`` makes the end
    inclusive). Bounds may use ``${run.*}`` and ``${var:*}``; ISO dates and timestamps become
    date and timestamp literals (offsets are converted to UTC), and numbers stay numbers, so
    integer watermarks such as change sequence numbers work too.
    """

    column: str | None = None
    """Watermark column on both sides."""
    source_column: str | None = None
    """Watermark column on the source, if it differs from ``column``."""
    target_column: str | None = None
    """Watermark column on the target; defaults to ``column``, then ``source_column``."""
    start: WindowBound | None = None
    """Inclusive lower bound."""
    end: WindowBound | None = None
    """Upper bound, exclusive unless ``end_inclusive`` is true."""
    end_inclusive: bool = False
    """Make ``end`` inclusive (``start <= column <= end``)."""

    @model_validator(mode="after")
    def _shape(self) -> WindowConfig:
        if self.start is None and self.end is None:
            raise ValueError("set 'start', 'end', or both")
        if self.column is None and self.source_column is None and self.target_column is None:
            raise ValueError("set 'column' (or 'source_column' and 'target_column')")
        return self


class InFlightConfig(StrictModel):
    """Exclude rows that changed shortly before a cutoff, so data still moving through live
    replication does not show as mismatches (MIG-09). Combine it with ``as_of`` time travel on
    sources that support it."""

    column: str | None = None
    """Change timestamp column on both sides."""
    source_column: str | None = None
    """Change timestamp column on the source, if it differs from ``column``."""
    target_column: str | None = None
    """Change timestamp column on the target; defaults to ``column``, then ``source_column``."""
    window: Duration
    """Rows changed within this long before the cutoff, or after it, are excluded (``15m``)."""
    cutoff: datetime | str | None = None
    """The cutoff instant: an ISO timestamp, ``${run.*}``, or ``${var:*}``. Defaults to the
    run's start. Values without an offset are in ``normalization.timezone``; the column's values
    are assumed to be in that time zone too."""
    exclude_keys_on_both_sides: bool = True
    """Also exclude, on the other side, the keys of rows excluded on either side, so a row that
    changed on one side only is not reported as missing or different (needs keys)."""
    max_keys: PositiveInt = Field(default=10_000, le=1_000_000)
    """Most in-flight keys excluded on both sides; more is an error (narrow the window)."""

    @model_validator(mode="after")
    def _shape(self) -> InFlightConfig:
        if self.column is None and self.source_column is None and self.target_column is None:
            raise ValueError("set 'column' (or 'source_column' and 'target_column')")
        return self


def _keys_auto(value: Any) -> Any:
    return [] if value == "auto" else value


class TableConfig(StrictModel):
    """One table (or dataset pair) to validate, or a pattern selecting many."""

    name: Identifier | None = None
    """Table name, part of every check ID: letters, digits, underscores, and hyphens. Set exactly
    one of ``name`` and ``pattern``."""
    pattern: PatternSpec | None = None
    """Glob patterns selecting many tables, each validated with this entry's settings. Set exactly
    one of ``name`` and ``pattern``."""
    description: str | None = None
    """Free-text description of the table."""
    tags: list[str] = Field(default_factory=list)
    """Labels for selecting with ``dvf run --only tag:NAME``, added to every check result of the
    table."""
    owner: str | None = None
    """Team or person responsible for the table; the default owner of its checks."""
    enabled: bool = True
    """Set to false to skip this table."""
    source: DatasetSpec | None = None
    """Source dataset (the only one in single-dataset suites). A named table needs one of ``table``,
    ``query``, or ``path``; a pattern entry takes its tables from the pattern."""
    target: DatasetSpec | None = None
    """Target dataset; required for named tables in comparison suites and not allowed in
    single-dataset suites."""
    keys: Annotated[
        list[KeyInput],
        BeforeValidator(_keys_auto, json_schema_input_type=Literal["auto"] | list[KeyInput]),
    ] = Field(default_factory=list)
    """Key columns used to pair rows, or ``auto`` to discover primary keys."""
    discover_keys: bool = False
    """Use the source dataset's primary key as the keys; ``keys: auto`` sets this. Do not combine it
    with explicit ``keys``."""
    columns: ColumnsConfig = Field(default_factory=ColumnsConfig)
    """Which columns to compare and how source and target columns map."""
    engine: ComponentName = "auto"
    """Where row-level comparisons run; inherited from the suite's defaults when omitted."""
    severity: Severity = Severity.ERROR
    """Severity of checks that set none; inherited from the suite's defaults when omitted."""
    sample_size: NonNegativeInt = Field(default=50, le=10_000)
    """Failing rows captured per check as evidence; inherited from the suite's defaults when
    omitted."""
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)
    """Canonicalization settings, merged key by key over the suite's defaults."""
    comparison: ComparisonConfig = Field(default_factory=ComparisonConfig)
    """Comparison settings, merged key by key over the suite's defaults."""
    schema_checks: list[SchemaCheck] = Field(default_factory=lambda: list(_DEFAULT_SCHEMA_CHECKS))
    """Schema checks to run (comparison suites); inherited from the suite's defaults when
    omitted."""
    schema_options: SchemaOptionsConfig = Field(default_factory=SchemaOptionsConfig)
    """Naming rules (SCH-14) and the drift baseline (SCH-11), merged key by key over the suite's
    defaults."""
    checks: list[SerializeAsAny[CheckConfig]] = Field(default_factory=list)
    """Data quality, file, and migration checks for this table."""
    details: dict[str, Any] = Field(default_factory=dict)
    """Custom details copied into the table's results (for example requirement IDs)."""
    window: WindowConfig | None = None
    """Validate only rows inside a watermark window (delta and incremental loads, MIG-02)."""
    in_flight: InFlightConfig | None = None
    """Exclude rows changed shortly before a cutoff (live replication, MIG-09)."""

    @model_validator(mode="before")
    @classmethod
    def _auto_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("keys") == "auto":
            data = dict(data)
            data["keys"] = []
            data["discover_keys"] = True
        return data

    @model_validator(mode="after")
    def _shape(self) -> TableConfig:
        ExactlyOneOf.check(self, ("name", "pattern"))
        if self.name is not None:
            if self.source is None or not self.source.has_locator:
                raise ValueError("set 'source' with one of 'table', 'query', or 'path'")
            if self.target is not None and not self.target.has_locator:
                raise ValueError("'target' needs one of 'table', 'query', or 'path'")
        else:
            for side in (self.source, self.target):
                if side is not None and side.has_locator:
                    raise ValueError(
                        "pattern tables take 'table', 'query', and 'path' from the pattern; "
                        "use 'source.filter' or 'target.filter' only"
                    )
        if self.keys and self.discover_keys:
            raise ValueError("set explicit 'keys' or 'keys: auto', not both")
        return self

    @property
    def label(self) -> str:
        if self.name is not None:
            return self.name
        return self.pattern.source if self.pattern is not None else "<unnamed>"


class SuiteConfig(StrictModel):
    """A group of tables validated together, usually one source and one target connection."""

    name: Identifier
    """Suite name, part of every check ID: letters, digits, underscores, and hyphens."""
    description: str | None = None
    """Free-text description of the suite."""
    tags: list[str] = Field(default_factory=list)
    """Labels for selecting with ``dvf run --only tag:NAME``, added to every check result of the
    suite."""
    owner: str | None = None
    """Team or person responsible for the suite; the default owner of its tables' checks."""
    enabled: bool = True
    """Set to false to skip every table of this suite."""
    source: Identifier
    """Connection name of the source (or the only) dataset."""
    target: Identifier | None = None
    """Connection name of the target; omit for single-dataset data quality suites."""
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    """Effective defaults for this suite's tables (project defaults with suite overrides)."""
    tables: list[TableConfig] = Field(default_factory=list)
    """Tables, or table patterns, validated by this suite."""

    @property
    def is_comparison(self) -> bool:
        return self.target is not None


# ====================================================================== other sections


class WaiverConfig(StrictModel):
    """An accepted, documented difference that should not fail the run until it expires."""

    check: str = Field(min_length=1)
    """Check ID selector; glob patterns allowed (``suite.table.*``)."""
    column: str | None = None
    """Waive differences in this column only (glob patterns allowed). A column comparison is waived
    only when every differing column is waived."""
    reason: str = Field(min_length=1)
    """Why the difference is accepted; recorded with the waived result."""
    ticket: str | None = None
    """Change request or issue reference, such as ``CR-1042``."""
    approved_by: str | None = None
    """Who approved the waiver."""
    expires: date | None = None
    """Expired waivers fail the run so they are reviewed."""

    def is_expired(self, today: date | None = None) -> bool:
        return self.expires is not None and self.expires < (today or date.today())


class BrandingConfig(StrictModel):
    title: str | None = None
    """Report title; defaults to the project name."""
    logo: str | None = None
    """Logo image file, relative to the configuration; HTML reports embed it."""
    primary_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    """Accent color of the reports, as ``#RRGGBB``."""


class ExtractsConfig(StrictModel):
    """Full mismatch extracts written next to the reports."""

    enabled: bool = True
    """Write extracts for failed checks. Strict privacy mode writes none even when this is on."""
    format: Literal["parquet", "csv"] = "parquet"
    """File format of the extract files."""
    max_rows_per_check: PositiveInt = 100_000
    """Most failing rows written to one check's extract. Fetched mismatched values are capped by
    ``comparison.max_mismatch_rows`` instead."""


class ReportsConfig(StrictModel):
    formats: list[ComponentName] = Field(default_factory=lambda: ["json", "html"], min_length=1)
    """Report formats, such as ``json``, ``html``, ``junit``, ``markdown``, or ``excel``. ``json``
    is always written, and ``dvf run --format`` replaces this list."""
    output_dir: str = "./reports/${run.date}/${run.id}"
    """Folder for reports and extracts, relative to the configuration. ``${run.*}`` placeholders are
    resolved when the run starts; ``--output-dir`` overrides it."""
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    """Title, logo, and accent color of the reports."""
    extracts: ExtractsConfig = Field(default_factory=ExtractsConfig)
    """Full mismatch extracts written next to the reports for failed checks."""
    templates_dir: str | None = None
    """Folder of Jinja2 templates overriding the built-in report templates."""
    include_passed: bool = True
    """List passed checks in the detailed report listings; set to false to list only the other
    checks."""
    max_samples_per_check: NonNegativeInt = 50
    """Most sample rows shown per check or column in the reports."""


class HistoryConfig(StrictModel):
    """Run history used for trends, baselines, and run-to-run comparison."""

    enabled: bool = True
    """Record each run in the history store and compare it with the baseline. ``--no-history`` turns
    history off for one run."""
    store: str = "duckdb:///./.dvf/history.duckdb"
    """History database URL: ``duckdb:///path`` or ``sqlite:///path``. Relative paths are relative
    to the configuration's folder."""
    baseline: str = "last_passed"
    """``last_run``, ``last_passed``, ``none``, or a specific run ID."""
    retention_runs: PositiveInt | None = None
    """Keep only this many newest runs (history rows and checkpoint folders); omit to keep every
    run."""


class SinkConfig(StrictModel):
    """Where run and check results are written as tables, for dashboards."""

    type: ComponentName
    """Sink type, for example ``duckdb``, ``sqlite``, or ``postgres``."""
    enabled: bool = True
    """Set to false to stop writing to this sink without removing it."""
    path: str | None = None
    """Database file for file-based sinks (relative to the configuration)."""
    url: SecretStr | None = None
    """Connection URL for server sinks. Treated as a secret."""
    schema_: str | None = Field(default=None, alias="schema")
    """Schema for the result tables, created when missing; the ``sqlite`` sink does not support
    it."""
    table_prefix: str = Field(default="dvf_", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    """Prefix of the result table names: ``<prefix>runs``, ``<prefix>checks``, and
    ``<prefix>column_mismatches``."""
    options: dict[str, Any] = Field(default_factory=dict)
    """Sink settings: ``batch_size`` (rows per insert, default 1,000) and, for ``postgres``, extra
    ``psycopg.connect`` arguments such as ``sslmode``."""


class PluginsConfig(StrictModel):
    """Project-local plugins. Loading them executes their code."""

    paths: list[str] = Field(default_factory=list)
    """Python files or folders, relative to the config file."""
    modules: list[str] = Field(default_factory=list)
    """Importable module names."""


class LineageTransportConfig(StrictModel):
    """Where OpenLineage events are sent."""

    type: Literal["http", "file", "console"] = "console"
    """``http`` (an OpenLineage backend such as Marquez), ``file`` (JSON Lines), or ``console``
    (standard error, for debugging)."""
    url: str | None = None
    """``http``: the backend's base URL, for example ``http://localhost:5000``."""
    endpoint: str = "api/v1/lineage"
    """``http``: the path events are posted to, relative to ``url``."""
    api_key: SecretStr | None = None
    """``http``: sent as ``Authorization: Bearer <api_key>``. Use a ``${secret:...}`` reference."""
    timeout_s: float = Field(default=5.0, gt=0, le=300)
    """``http``: timeout of one request, in seconds."""
    retries: NonNegativeInt = Field(default=2, le=10)
    """``http``: retries after a network error, HTTP 429, or a 5xx response."""
    path: str | None = None
    """``file``: the JSON Lines file events are appended to (relative to the configuration;
    ``${run.*}`` placeholders allowed)."""

    @model_validator(mode="after")
    def _shape(self) -> LineageTransportConfig:
        if self.type == "http" and not self.url:
            raise ValueError("the 'http' transport needs 'url'")
        if self.type == "file" and not self.path:
            raise ValueError("the 'file' transport needs 'path'")
        return self


class LineageConfig(StrictModel):
    """OpenLineage run events with data quality facets (off by default)."""

    enabled: bool = False
    """Send OpenLineage events for each run."""
    namespace: str = Field(default="dvf", min_length=1)
    """Namespace of dvf's jobs."""
    job_name: str | None = None
    """Name of the run's job; defaults to the project name."""
    granularity: Literal["run", "table"] = "run"
    """``run``: one job per run, with every dataset as an input. ``table``: also one child job
    per table (``<job_name>.<suite>.<table>``), linked to the run with the parent facet."""
    transport: LineageTransportConfig = Field(default_factory=LineageTransportConfig)
    """Where the events are sent: the console (the default), a file, or an HTTP backend."""
    dataset_namespaces: dict[str, str] = Field(default_factory=dict)
    """Dataset namespace per connection name, replacing the one dvf derives from the connection
    (for example ``postgres://db.example.com:5432``)."""
    fail_on: list[Status] = Field(default_factory=lambda: [Status.ERROR])
    """Run statuses sent as ``FAIL`` events; other finished runs are ``COMPLETE`` (the assertions
    carry the check results) and interrupted runs are ``ABORT``."""


class TelemetryConfig(StrictModel):
    """OpenTelemetry traces and metrics of dvf itself (``dvf-framework[otel]``, off by default)."""

    enabled: bool = False
    """Record OpenTelemetry traces and metrics. Needs ``dvf-framework[otel]``; without it, dvf logs
    a warning and records nothing."""
    exporter: Literal["global", "console"] = "global"
    """``global`` uses the tracer and meter providers configured in the process (for example by
    ``opentelemetry-instrument`` from ``OTEL_*`` variables). ``console`` prints spans and metrics
    to standard error, for debugging."""
    service_name: str = "dvf"
    """``service.name`` of the providers dvf creates for the ``console`` exporter."""
    metrics: bool = True
    """Also record metrics (rows per second, query time per side, retries, check counts)."""


class TriageConfig(StrictModel):
    """The AI-assisted triage plugin interface (off by default).

    When enabled, plugins implementing the ``triage_failures`` hook receive masked, aggregated
    summaries of the run's failures: statuses, counts, and mismatch classes, never values.
    """

    enabled: bool = False
    """Opt in to calling triage plugins."""
    max_checks: PositiveInt = Field(default=25, le=500)
    """Failed and errored checks included in the summary (the worst first)."""
    max_columns_per_check: PositiveInt = Field(default=20, le=500)
    """Column mismatch summaries included per check."""


class DvfConfig(StrictModel):
    """The root of a dvf configuration."""

    version: int
    """Configuration format version. Use ``1``."""
    project: ProjectConfig
    """Project name, description, owner, and metadata."""
    vars: dict[str, Any] = Field(default_factory=dict)
    """Variables available as ``${var:name}``."""
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    """Settings inherited by every suite and table."""
    connections: dict[Identifier, SerializeAsAny[ConnectionConfig]] = Field(default_factory=dict)
    """Named connections; each one's ``type`` selects the connector and its fields."""
    suites: list[SuiteConfig] = Field(default_factory=list)
    """Groups of tables to validate."""
    waivers: list[WaiverConfig] = Field(default_factory=list)
    """Accepted, documented differences that do not fail the run until they expire."""
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    """Report formats, output folder, branding, and extracts."""
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    """Run history used for trends, baselines, and run-to-run comparison."""
    sinks: list[SinkConfig] = Field(default_factory=list)
    """Result sinks that receive every run's results as tables."""
    notifications: list[SerializeAsAny[NotificationConfig]] = Field(default_factory=list)
    """Notification channels (Teams, Slack, webhook, or email) and the run statuses that trigger
    them."""
    lineage: LineageConfig = Field(default_factory=LineageConfig)
    """OpenLineage run events with data quality facets."""
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    """OpenTelemetry traces and metrics of dvf itself."""
    triage: TriageConfig = Field(default_factory=TriageConfig)
    """The AI-assisted triage plugin interface."""
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    """Project-local plugin files and modules; loading them executes their code."""
    extends: list[str] = Field(default_factory=list, exclude=True)
    """Files merged beneath this one (applied by the loader)."""
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict, exclude=True)
    """Named overlays selected with ``--profile`` (applied by the loader)."""

    @field_validator("version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != CONFIG_VERSION:
            raise ValueError(
                f"unsupported configuration version {value}; this dvf reads version {CONFIG_VERSION}"
            )
        return value

    def iter_tables(self) -> list[tuple[SuiteConfig, TableConfig]]:
        return [(suite, table) for suite in self.suites for table in suite.tables]
