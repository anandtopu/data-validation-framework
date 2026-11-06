"""The root configuration model and its sections (ADR 0006).

Models here describe the *resolved* configuration: the loader has already applied layers,
interpolation, and inheritance, so every table carries its effective settings.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BeforeValidator,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
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
from dvf.core.enums import SchemaCheck, Severity

CONFIG_VERSION = 1
"""The configuration format version this dvf reads."""


# ====================================================================== defaults


class FloatCompare(StrictModel):
    """Tolerance for comparing floating-point values (they are never hashed directly)."""

    abs_tol: NonNegativeFloat = 0.0
    rel_tol: NonNegativeFloat = 1e-9


class NormalizationConfig(StrictModel):
    """How values are canonicalized before comparison or hashing."""

    trim: Literal["none", "rtrim", "ltrim", "both"] = "none"
    """Whitespace trimming. ``rtrim`` suits blank-padded CHAR columns."""
    case_sensitive: bool = True
    null_equals_empty: bool = False
    """Treat NULL and empty string as equal."""
    unicode_form: Literal["NFC", "NFD", "NFKC", "NFKD", "none"] = "NFC"
    collapse_whitespace: bool = False
    timestamp_precision: Literal["s", "ms", "us", "ns"] = "us"
    """Timestamps are truncated to this precision on both sides."""
    timezone: str = "UTC"
    """Time zone that timestamps without an offset are assumed to be in."""
    decimal_scale: Literal["auto"] | NonNegativeInt = "auto"
    """Fixed scale for decimals, or ``auto`` to use the larger scale of the two sides."""
    float_compare: FloatCompare = Field(default_factory=FloatCompare)

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
    """``strict`` keeps every raw value out of reports and extracts."""
    pii_columns: list[str] = Field(default_factory=list)
    """Column names (glob patterns allowed) always treated as personal data."""


class ExecutionConfig(StrictModel):
    """Concurrency, timeouts, and retries."""

    max_parallel_tables: PositiveInt = 4
    max_queries_per_connection: PositiveInt = 2
    query_timeout_s: PositiveInt = 1800
    retries: NonNegativeInt = 2
    retry_backoff_s: NonNegativeFloat = 2.0


class ToleranceConfig(StrictModel):
    """Differences that are accepted as equal."""

    numeric_abs: NonNegativeFloat | None = None
    numeric_rel: NonNegativeFloat | None = None
    timestamp: Duration | None = None
    row_count_pct: float | None = Field(default=None, ge=0, le=100)
    """Accepted row count difference, as a percentage of the source count."""


class SampleConfig(StrictModel):
    """Statistical sampling for comparisons too large to run in full."""

    fraction: float | None = Field(default=None, gt=0, le=1)
    rows: PositiveInt | None = None
    seed: int = 42
    stratify_by: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_size(self) -> SampleConfig:
        ExactlyOneOf.check(self, ("fraction", "rows"))
        return self


Aggregate = Literal[
    "count", "sum", "min", "max", "avg", "count_distinct", "null_count", "sum_length"
]


_DEFAULT_AGGREGATES: list[Aggregate] = ["count", "sum", "min", "max", "null_count"]


class ComparisonConfig(StrictModel):
    """Source-vs-target comparison settings."""

    enabled: bool = True
    strategies: list[ComponentName] = Field(
        default_factory=lambda: ["row_count", "aggregate"], min_length=1
    )
    """Comparison strategies, cheapest first. They escalate: count, aggregate, then hash."""
    on_mismatch: ComponentName | None = "full_diff"
    """Strategy used to explain mismatches found by the strategies above (or none)."""
    buckets: PositiveInt = Field(default=256, le=1_000_000)
    """Number of hash buckets for ``hash_bucket``."""
    group_by: list[str] = Field(default_factory=list)
    """Columns for grouped counts and control totals (for example company code, period)."""
    aggregates: list[Aggregate] = Field(default_factory=lambda: list(_DEFAULT_AGGREGATES))
    tolerance: ToleranceConfig = Field(default_factory=ToleranceConfig)
    sample: SampleConfig | None = None
    max_mismatch_rows: PositiveInt = 100_000
    """Upper bound on mismatched rows fetched for diagnosis."""


_DEFAULT_SCHEMA_CHECKS = [
    SchemaCheck.OBJECT_EXISTS,
    SchemaCheck.COLUMNS_PRESENT,
    SchemaCheck.TYPE_COMPATIBILITY,
    SchemaCheck.LENGTH_PRECISION,
    SchemaCheck.NULLABILITY,
]


class DefaultsConfig(StrictModel):
    """Settings inherited by suites and tables."""

    engine: ComponentName = "auto"
    severity: Severity = Severity.ERROR
    sample_size: NonNegativeInt = Field(default=50, le=10_000)
    """Failing rows captured per check as evidence (masked by privacy settings)."""
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    comparison: ComparisonConfig = Field(default_factory=ComparisonConfig)
    schema_checks: list[SchemaCheck] = Field(default_factory=lambda: list(_DEFAULT_SCHEMA_CHECKS))


INHERITED_TABLE_FIELDS = (
    "engine",
    "severity",
    "sample_size",
    "normalization",
    "comparison",
    "schema_checks",
)
"""Fields a table inherits from its suite's effective defaults."""


# ====================================================================== datasets and tables


class ProjectConfig(StrictModel):
    name: str = Field(min_length=1)
    description: str | None = None
    owner: str | None = None
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
    target: str | None = None


class KeyMapping(StrictModel):
    """A key column, optionally renamed and normalized on the target side."""

    source: str
    target: str | None = None
    """Target column name; defaults to the source name."""
    normalize: list[NormalizerInput] = Field(default_factory=list)

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
    source_expr: str | None = None
    """A source-side SQL expression the target column is derived from."""
    target: str
    normalize: list[NormalizerInput] = Field(default_factory=list)
    compare: bool = True
    """Set to false to exclude this column from value comparison."""
    tolerance: ToleranceConfig | None = None
    pii: bool = False

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
    mappings: list[ColumnMapping] = Field(default_factory=list)
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


def _keys_auto(value: Any) -> Any:
    return [] if value == "auto" else value


class TableConfig(StrictModel):
    """One table (or dataset pair) to validate, or a pattern selecting many."""

    name: Identifier | None = None
    pattern: PatternSpec | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    enabled: bool = True
    source: DatasetSpec | None = None
    target: DatasetSpec | None = None
    keys: Annotated[
        list[KeyInput],
        BeforeValidator(_keys_auto, json_schema_input_type=Literal["auto"] | list[KeyInput]),
    ] = Field(default_factory=list)
    """Key columns used to pair rows, or ``auto`` to discover primary keys."""
    discover_keys: bool = False
    columns: ColumnsConfig = Field(default_factory=ColumnsConfig)
    engine: ComponentName = "auto"
    severity: Severity = Severity.ERROR
    sample_size: NonNegativeInt = Field(default=50, le=10_000)
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)
    comparison: ComparisonConfig = Field(default_factory=ComparisonConfig)
    schema_checks: list[SchemaCheck] = Field(default_factory=lambda: list(_DEFAULT_SCHEMA_CHECKS))
    checks: list[SerializeAsAny[CheckConfig]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

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
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    enabled: bool = True
    source: Identifier
    """Connection name of the source (or the only) dataset."""
    target: Identifier | None = None
    """Connection name of the target; omit for single-dataset data quality suites."""
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    """Effective defaults for this suite's tables (project defaults with suite overrides)."""
    tables: list[TableConfig] = Field(default_factory=list)

    @property
    def is_comparison(self) -> bool:
        return self.target is not None


# ====================================================================== other sections


class WaiverConfig(StrictModel):
    """An accepted, documented difference that should not fail the run until it expires."""

    check: str = Field(min_length=1)
    """Check ID selector; glob patterns allowed (``suite.table.*``)."""
    column: str | None = None
    reason: str = Field(min_length=1)
    ticket: str | None = None
    approved_by: str | None = None
    expires: date | None = None
    """Expired waivers fail the run so they are reviewed."""

    def is_expired(self, today: date | None = None) -> bool:
        return self.expires is not None and self.expires < (today or date.today())


class BrandingConfig(StrictModel):
    title: str | None = None
    logo: str | None = None
    primary_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class ExtractsConfig(StrictModel):
    """Full mismatch extracts written next to the reports."""

    enabled: bool = True
    format: Literal["parquet", "csv"] = "parquet"
    max_rows_per_check: PositiveInt = 100_000


class ReportsConfig(StrictModel):
    formats: list[ComponentName] = Field(default_factory=lambda: ["json", "html"], min_length=1)
    output_dir: str = "./reports/${run.date}/${run.id}"
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    extracts: ExtractsConfig = Field(default_factory=ExtractsConfig)
    templates_dir: str | None = None
    """Folder of Jinja2 templates overriding the built-in report templates."""
    include_passed: bool = True
    max_samples_per_check: NonNegativeInt = 50


class HistoryConfig(StrictModel):
    """Run history used for trends, baselines, and run-to-run comparison."""

    enabled: bool = True
    store: str = "duckdb:///./.dvf/history.duckdb"
    baseline: str = "last_passed"
    """``last_run``, ``last_passed``, ``none``, or a specific run ID."""
    retention_runs: PositiveInt | None = None


class PluginsConfig(StrictModel):
    """Project-local plugins. Loading them executes their code."""

    paths: list[str] = Field(default_factory=list)
    """Python files or folders, relative to the config file."""
    modules: list[str] = Field(default_factory=list)
    """Importable module names."""


class DvfConfig(StrictModel):
    """The root of a dvf configuration."""

    version: int
    """Configuration format version. Use ``1``."""
    project: ProjectConfig
    vars: dict[str, Any] = Field(default_factory=dict)
    """Variables available as ``${var:name}``."""
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    connections: dict[Identifier, SerializeAsAny[ConnectionConfig]] = Field(default_factory=dict)
    suites: list[SuiteConfig] = Field(default_factory=list)
    waivers: list[WaiverConfig] = Field(default_factory=list)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    notifications: list[SerializeAsAny[NotificationConfig]] = Field(default_factory=list)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
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
