"""Configuration models of the migration validations (MIG-01 to MIG-14)."""

from __future__ import annotations

import codecs
from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BeforeValidator,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)

from dvf.config.base import Duration, ExactlyOneOf, Identifier, StrictModel
from dvf.config.checks import CheckConfig, ColumnsCheckConfig

FlagValue = str | int | bool
"""A flag value; ``true`` and ``false`` also match ``1``/``0``, ``t``/``f``, ``y``/``n``."""

_FLAG_SET: list[FlagValue] = [True]


def _as_list(value: Any) -> Any:
    return [value] if isinstance(value, str) else value


ColumnList = Annotated[list[str], BeforeValidator(_as_list, json_schema_input_type=str | list[str])]
"""One column name or a list of names."""


# ====================================================================== shared


class PairCheckConfig(CheckConfig):
    """Settings shared by checks that compare the source and target datasets of a table."""

    source_where: str | None = None
    """SQL predicate restricting the source rows (``where`` applies to both sides)."""
    target_where: str | None = None
    """SQL predicate restricting the target rows (``where`` applies to both sides)."""


class DatasetLocator(StrictModel):
    """Another dataset a check reads: a table or a query on a connection."""

    connection: Identifier | None = None
    """Connection holding the dataset; defaults to the checked dataset's connection."""
    table: str | None = None
    """Table or view name, optionally qualified (``schema.table``). Set exactly one of ``table`` and
    ``query``."""
    query: str | None = None
    """SQL query used as the dataset. Set exactly one of ``table`` and ``query``."""
    filter: str | None = None
    """SQL predicate applied to this dataset."""

    @model_validator(mode="after")
    def _one_locator(self) -> DatasetLocator:
        ExactlyOneOf.check(self, ("table", "query"))
        return self


class FlagConfig(StrictModel):
    """A marker such as a soft-delete or current-row flag: a column holding one of ``values``,
    or a SQL boolean expression."""

    column: str | None = None
    """Column holding the flag; it is set when the column holds one of ``values``. Set exactly one
    of ``column`` and ``expression``."""
    values: list[FlagValue] = Field(default_factory=lambda: list(_FLAG_SET), min_length=1)
    """Values that mean the flag is set (compared as trimmed, case-insensitive text)."""
    expression: str | None = None
    """A SQL condition that is true when the flag is set, for example ``deleted_at IS NOT NULL``."""

    @model_validator(mode="after")
    def _one_form(self) -> FlagConfig:
        ExactlyOneOf.check(self, ("column", "expression"))
        return self


# ====================================================================== MIG-01


class ExcludedRange(StrictModel):
    """An accepted range of excluded rows."""

    min: NonNegativeInt | None = None
    """Fewest excluded rows accepted; omit for no lower bound."""
    max: NonNegativeInt | None = None
    """Most excluded rows accepted; omit for no upper bound."""

    @model_validator(mode="after")
    def _bounds(self) -> ExcludedRange:
        if self.min is None and self.max is None:
            raise ValueError("set 'min', 'max', or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("'min' must not exceed 'max'")
        return self


class ScopeReconciliationCheck(PairCheckConfig):
    """Source rows reconcile to migrated rows plus rows excluded by design (MIG-01)."""

    excluded_where: str = Field(min_length=1)
    """SQL predicate on the source selecting the rows excluded by design (archived, closed, out
    of scope)."""
    expected_excluded: NonNegativeInt | ExcludedRange | None = None
    """The expected number of excluded rows: an exact count, or ``{min, max}``."""
    population: Literal["unfiltered", "filtered"] = "unfiltered"
    """Count the source before its ``source.filter`` (``unfiltered``, the usual case when that
    filter is the scope) or after it (``filtered``). Window and in-flight filters always apply."""
    tolerance_rows: NonNegativeInt = 0
    """Accepted difference between the source total and migrated plus excluded rows."""
    tolerance_pct: float = Field(default=0.0, ge=0, le=100)
    """The same, as a percentage of the source total (the larger allowance wins)."""
    excluded_absent: bool = False
    """Also require that no excluded source row's key exists in the target (needs keys)."""


# ====================================================================== MIG-02


class ChangeTableRef(DatasetLocator):
    """A CDC change table: one row per change, with key, operation, and ordering columns."""

    key: ColumnList = Field(min_length=1)
    """Key columns of the changed record, in the order of the checked dataset's key."""
    operation: str
    """Column holding the operation code."""
    sequence: ColumnList = Field(min_length=1)
    """Columns ordering the changes of one key (for example commit LSN, then sequence)."""


class ChangeColumn(StrictModel):
    """A column of the change table's after-image compared with the checked dataset."""

    change: str
    """Column of the change table's after-image."""
    target: str | None = None
    """Column of the checked dataset; defaults to the same name."""


class CdcAppliedCheck(CheckConfig):
    """The latest change of every key in a CDC change table is reflected in the dataset (MIG-02)."""

    changes: ChangeTableRef
    """The CDC change table: where it is, and its key, operation, and sequence columns."""
    key_columns: list[str] = Field(default_factory=list)
    """Key columns of the checked dataset; defaults to the table's keys on that side."""
    insert_values: list[str] = Field(
        default_factory=lambda: ["i", "insert", "c", "create", "r", "read"]
    )
    """Operation codes of inserts (case-insensitive)."""
    update_values: list[str] = Field(default_factory=lambda: ["u", "update"])
    """Operation codes of updates (case-insensitive)."""
    delete_values: list[str] = Field(default_factory=lambda: ["d", "delete"])
    """Operation codes of deletes (case-insensitive)."""
    soft_delete: FlagConfig | None = None
    """How the checked dataset marks deleted rows; deleted keys may then remain, flagged."""
    compare_columns: list[ChangeColumn] = Field(default_factory=list)
    """After-image columns whose latest values must equal the dataset's (compared as text)."""


class LateArrivingCheck(CheckConfig):
    """Rows that arrived after their period closed, or later than allowed (MIG-02)."""

    event_column: str
    """When the business event happened (for example a posting date)."""
    load_column: str
    """When the row was loaded."""
    max_lateness: Duration | None = None
    """Rows loaded more than this long after their event are late."""
    period_start: datetime | date | str | None = None
    """Start of the open period: rows whose event is before it but that were loaded on or after
    ``loaded_since`` arrived late, into a closed period."""
    loaded_since: datetime | date | str | None = None
    """Defaults to ``period_start``."""

    @model_validator(mode="after")
    def _one_rule(self) -> LateArrivingCheck:
        if self.max_lateness is None and self.period_start is None:
            raise ValueError("set 'max_lateness', 'period_start', or both")
        return self


# ====================================================================== MIG-03


IdempotencyMetric = Literal["row_count", "checksum"]
_IDEMPOTENCY_METRICS: list[IdempotencyMetric] = ["row_count", "checksum"]


class IdempotencyCheck(CheckConfig):
    """Re-running a load creates no duplicates and changes neither the row count nor the content
    (MIG-03)."""

    key_columns: list[str] = Field(default_factory=list)
    """Columns that must be unique; defaults to the table's keys on the checked side."""
    checksum_columns: list[str] = Field(default_factory=list)
    """Columns in the content checksum; defaults to every column not in ``exclude``."""
    exclude: list[str] = Field(default_factory=list)
    """Columns left out of the checksum, such as load timestamps and batch IDs."""
    baseline: str | None = None
    """Run to compare with: ``last_run`` (default), ``last_passed``, or a run ID."""
    compare: list[IdempotencyMetric] = Field(default_factory=lambda: list(_IDEMPOTENCY_METRICS))
    """What must be unchanged since the baseline run: the row count, the content checksum, or
    both."""


# ====================================================================== MIG-04


class HistoryCountRef(DatasetLocator):
    """A dataset with one row per expected version of each business key."""

    keys: ColumnList = Field(min_length=1)
    """Business key columns in this dataset, in the order of ``business_key``."""


class Scd2Check(CheckConfig):
    """SCD Type 2 history is well formed: one current row per business key, valid and
    non-overlapping validity ranges, optionally no gaps, and reconciled version counts (MIG-04)."""

    business_key: ColumnList = Field(min_length=1)
    """Business key columns identifying one entity across its versions."""
    valid_from: str
    """Column holding the start of each version's validity."""
    valid_to: str
    """Column holding the end of each version's validity; NULL, or ``open_end``, marks a version
    that is still valid."""
    current_flag: FlagConfig | None = None
    """How current rows are marked. Without it, rows with an open end are current."""
    open_end: datetime | date | str | None = None
    """A ``valid_to`` meaning "still valid", such as ``9999-12-31`` (NULL always counts)."""
    end: Literal["exclusive", "inclusive"] = "exclusive"
    """``exclusive``: a version is valid up to, not including, ``valid_to``, so the next one
    starts at ``valid_to``. ``inclusive``: the next one starts ``granularity`` later."""
    granularity: Duration = "1d"
    """For inclusive ends: the step between one version's end and the next one's start."""
    allow_gaps: bool = False
    """Do not report gaps between one version's end and the next version's start."""
    history: HistoryCountRef | None = None
    """Reconcile the number of versions per business key with this dataset."""


# ====================================================================== MIG-05


class DeletePropagationCheck(PairCheckConfig):
    """Source deletes are reflected in the target: hard deletes propagated and soft-delete flags
    consistent (MIG-05)."""

    hard_deletes: bool = True
    """Target keys missing from the source must not remain as active rows."""
    source_flag: FlagConfig | None = None
    """How the source marks soft-deleted rows."""
    target_flag: FlagConfig | None = None
    """How the target marks deleted rows."""
    soft_deleted_rows: Literal["either", "flagged", "absent"] = "either"
    """Where soft-deleted source rows must end up: ``flagged`` in the target, ``absent`` from
    it, or ``either``."""

    @model_validator(mode="after")
    def _something_to_check(self) -> DeletePropagationCheck:
        if not self.hard_deletes and self.source_flag is None:
            raise ValueError("set 'source_flag', or keep 'hard_deletes' on")
        if (
            not self.hard_deletes
            and self.target_flag is None
            and self.soft_deleted_rows == "either"
        ):
            raise ValueError(
                "nothing to check: keep 'hard_deletes' on, set 'target_flag', or set "
                "'soft_deleted_rows' to 'flagged' or 'absent'"
            )
        if self.soft_deleted_rows == "flagged" and self.target_flag is None:
            raise ValueError("'soft_deleted_rows: flagged' needs 'target_flag'")
        return self


# ====================================================================== MIG-06


class KeyMapRef(DatasetLocator):
    """A surrogate key map: legacy key columns and the new key columns they map to."""

    legacy_columns: ColumnList = Field(min_length=1)
    """Legacy key columns in the map, in the order of ``source_key``."""
    new_columns: ColumnList = Field(min_length=1)
    """New key columns in the map, in the order of ``target_key``."""


class KeyMapCheck(PairCheckConfig):
    """Every legacy key maps to exactly one new key (and the reverse, where required), and the
    map covers every source and target key (MIG-06)."""

    map: KeyMapRef
    """The key map: where it is, and its legacy and new key columns."""
    source_key: list[str] = Field(default_factory=list)
    """Legacy key columns in the source dataset; defaults to the table's source keys."""
    target_key: list[str] = Field(default_factory=list)
    """New key columns in the target dataset; defaults to the table's target keys."""
    reverse: bool = True
    """Every new key must map from exactly one legacy key."""
    complete_source: bool = True
    """Every source key must have a mapping."""
    complete_target: bool = True
    """Every target key must appear in the map."""
    dangling: bool = False
    """Map entries whose new key is not in the target count as violations."""

    @model_validator(mode="after")
    def _arity(self) -> KeyMapCheck:
        if self.source_key and len(self.source_key) != len(self.map.legacy_columns):
            raise ValueError("'source_key' and 'map.legacy_columns' must have the same length")
        if self.target_key and len(self.target_key) != len(self.map.new_columns):
            raise ValueError("'target_key' and 'map.new_columns' must have the same length")
        return self


# ====================================================================== MIG-07


class AuditColumnsCheck(CheckConfig):
    """Audit and lineage columns are populated and consistent (MIG-07)."""

    load_timestamp: str | None = None
    """Load timestamp column: it must be set, at most ``max_future`` ahead of now, and not before
    ``not_before``."""
    batch_id: str | None = None
    """Batch ID column: it must be set, and ``max_batches`` and ``allowed_batches`` may limit its
    values."""
    source_system: str | None = None
    """Source system column: it must be set, and ``allowed_source_systems`` may limit its values."""
    not_before: datetime | date | str | None = None
    """Load timestamps must be at or after this (for example the load window start)."""
    max_future: Duration = "5m"
    """Load timestamps may be at most this far in the future (clock skew)."""
    max_batches: PositiveInt | None = None
    """Most distinct batch IDs (``1``: one batch per load)."""
    allowed_batches: list[str | int] = Field(default_factory=list)
    """Accepted batch IDs, compared as trimmed, case-insensitive text; needs ``batch_id``."""
    allowed_source_systems: list[str] = Field(default_factory=list)
    """Accepted source systems, compared as trimmed, case-insensitive text; needs
    ``source_system``."""

    @model_validator(mode="after")
    def _columns(self) -> AuditColumnsCheck:
        if not any((self.load_timestamp, self.batch_id, self.source_system)):
            raise ValueError("set at least one of 'load_timestamp', 'batch_id', 'source_system'")
        if (self.max_batches or self.allowed_batches) and not self.batch_id:
            raise ValueError("'max_batches' and 'allowed_batches' need 'batch_id'")
        if self.allowed_source_systems and not self.source_system:
            raise ValueError("'allowed_source_systems' needs 'source_system'")
        return self

    def target_columns(self) -> list[str]:
        return [c for c in (self.load_timestamp, self.batch_id, self.source_system) if c]


# ====================================================================== MIG-10

CharsetRule = Literal["replacement_char", "mojibake", "control_chars", "round_trip"]


class CharsetConversionCheck(ColumnsCheckConfig):
    """Text survived the character set conversion: no replacement characters, mojibake, or
    control characters, and every value fits the legacy code page (MIG-10)."""

    code_page: str | None = None
    """Legacy code page values must round-trip through, such as ``cp037`` (EBCDIC), ``cp500``,
    ``latin-1``, or ``cp1252``."""
    rules: list[CharsetRule] | None = None
    """Rules to apply; defaults to all, with ``round_trip`` only when ``code_page`` is set."""
    allowed_control: list[str] = Field(default_factory=lambda: ["\t", "\n", "\r"])
    """Control characters that are acceptable."""
    mojibake_patterns: list[str] = Field(default_factory=list)
    """Extra substrings that mark mojibake, in addition to the built-in detection."""
    unicode_form: Literal["none", "NFC", "NFKC"] = "none"
    """Normalize values before the round trip (decomposed accents do not fit most code pages)."""
    max_distinct: PositiveInt = 10_000_000
    """Most distinct values examined per column; the rest are reported as not checked."""

    @field_validator("code_page")
    @classmethod
    def _known_codec(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"unknown code page '{value}'") from exc
        return value

    @model_validator(mode="after")
    def _round_trip_needs_code_page(self) -> CharsetConversionCheck:
        if self.rules is not None and "round_trip" in self.rules and self.code_page is None:
            raise ValueError("rule 'round_trip' needs 'code_page'")
        return self

    @property
    def effective_rules(self) -> list[str]:
        if self.rules is not None:
            return list(self.rules)
        rules = ["replacement_char", "mojibake", "control_chars"]
        return [*rules, "round_trip"] if self.code_page else rules


# ====================================================================== MIG-11


DstRule = Literal["nonexistent", "ambiguous"]
_DST_RULES: list[DstRule] = ["nonexistent", "ambiguous"]


class DstEdgeCasesCheck(ColumnsCheckConfig):
    """Local timestamps that do not exist (spring forward) or are ambiguous (fall back) in a
    time zone (MIG-11)."""

    timezone: str | None = None
    """Time zone of the local values; defaults to ``normalization.timezone``."""
    rules: list[DstRule] = Field(default_factory=lambda: list(_DST_RULES), min_length=1)
    """Rules to apply: ``nonexistent`` finds local times skipped when clocks spring forward,
    ``ambiguous`` finds local times repeated when they fall back."""
    min_year: int = Field(default=1970, ge=1, le=9999)
    """Earliest year whose transitions are examined (sentinel dates are ignored)."""
    max_year: int = Field(default=2100, ge=1, le=9999)
    """Latest year whose transitions are examined (sentinel dates are ignored)."""

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is None or value.upper() in ("UTC", "Z"):
            return None if value is None else "UTC"
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone '{value}'") from exc
        return value


# ====================================================================== MIG-12


class SubstitutionRule(StrictModel):
    """A substitution such as source NULL to target ``'N/A'``, or ``'00000000'`` to NULL."""

    name: Identifier | None = None
    """Name used in the result (``rule_<name>``); defaults to the rule's position."""
    column: str
    """Source column."""
    target_column: str | None = None
    """Target column; defaults to the column paired with ``column``, else the same name."""
    source_null: bool = False
    """The rule applies when the source is NULL."""
    source_blank: bool = False
    """The rule applies when the source is empty or spaces only."""
    source_values: list[str | int | float] = Field(default_factory=list)
    """The rule applies when the source holds one of these values (compared as text)."""
    target_null: bool = False
    """The target must then be NULL."""
    target_value: str | int | float | None = None
    """The target must then hold this value (compared as text)."""

    @model_validator(mode="after")
    def _shape(self) -> SubstitutionRule:
        if not (self.source_null or self.source_blank or self.source_values):
            raise ValueError("set 'source_null', 'source_blank', or 'source_values'")
        if self.target_null == (self.target_value is not None):
            raise ValueError("set exactly one of 'target_null' and 'target_value'")
        return self

    def describe(self) -> str:
        parts = []
        if self.source_null:
            parts.append("NULL")
        if self.source_blank:
            parts.append("blank")
        parts += [repr(str(v)) for v in self.source_values]
        then = "NULL" if self.target_null else repr(str(self.target_value))
        return f"{self.column}: {' or '.join(parts)} -> {then}"


class SubstitutionRulesCheck(PairCheckConfig):
    """Default and sentinel substitutions are applied exactly as specified, and target sentinel
    values do not appear without a matching source condition (MIG-12)."""

    rules: list[SubstitutionRule] = Field(min_length=1)
    """The substitutions to verify."""
    trim: bool = False
    """Compare values after removing leading and trailing spaces."""
    unmatched_sentinels: bool = True
    """Report target sentinel values whose source matches no rule (and is not the sentinel)."""


# ====================================================================== MIG-14

BaselineMetric = Literal["row_count", "null_count", "count_distinct", "sum", "min", "max", "groups"]
_BASELINE_METRICS: list[BaselineMetric] = ["row_count", "null_count", "sum"]


class BaselineTolerance(StrictModel):
    """Accepted differences from the baseline."""

    row_count_pct: float = Field(default=0.0, ge=0, le=100)
    """Accepted row count difference, as a percentage of the baseline (also for groups)."""
    numeric_abs: NonNegativeFloat = 0.0
    """Accepted absolute difference of sums."""
    numeric_rel: NonNegativeFloat = 0.0
    """Accepted relative difference of sums."""


class BaselineMetricsCheck(CheckConfig):
    """Current metrics match a named pre-migration baseline within tolerances (MIG-14)."""

    baseline: Identifier
    """Name given to ``dvf baseline capture --name``."""
    metrics: list[BaselineMetric] = Field(
        default_factory=lambda: list(_BASELINE_METRICS), min_length=1
    )
    """Metrics compared with the baseline; ``groups`` compares the row counts per
    ``comparison.group_by`` group."""
    columns: list[str] = Field(default_factory=list)
    """Baseline columns to compare (default: every captured column)."""
    tolerance: BaselineTolerance = Field(default_factory=BaselineTolerance)
    """Accepted differences between the current metrics and the baseline."""
    baseline_table: str | None = None
    """The ``suite.table`` entry of the baseline to compare with (default: this table)."""


__all__ = [
    "AuditColumnsCheck",
    "BaselineMetricsCheck",
    "BaselineTolerance",
    "CdcAppliedCheck",
    "ChangeColumn",
    "ChangeTableRef",
    "CharsetConversionCheck",
    "DatasetLocator",
    "DeletePropagationCheck",
    "DstEdgeCasesCheck",
    "ExcludedRange",
    "FlagConfig",
    "HistoryCountRef",
    "IdempotencyCheck",
    "KeyMapCheck",
    "KeyMapRef",
    "LateArrivingCheck",
    "PairCheckConfig",
    "Scd2Check",
    "ScopeReconciliationCheck",
    "SubstitutionRule",
    "SubstitutionRulesCheck",
]
