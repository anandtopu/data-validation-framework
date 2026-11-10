"""Check configuration models.

Every check type registers a model that subclasses :class:`CheckConfig`. The built-in models here
define the configuration contract; their implementations arrive in Phase 1.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)

from dvf.config.base import Duration, Identifier, StrictModel
from dvf.core.enums import Severity, Side


class CheckConfig(StrictModel):
    """Settings shared by every check."""

    type: str
    """Check type, for example ``not_null`` or a plugin type such as ``acme.vat_number_valid``."""
    name: Identifier | None = None
    """Optional name; becomes part of the check ID. Required when a table has two checks of the same type and columns."""
    description: str | None = None
    """Free-text description of what the check verifies."""
    severity: Severity | None = None
    """Inherited from the table when omitted."""
    side: Side | None = None
    """Dataset to check: ``source``, ``target``, or ``both``. Defaults to ``target`` for comparison suites and ``source`` otherwise."""
    mostly: float = Field(default=1.0, gt=0, le=1)
    """Pass when at least this share of rows satisfies the check (1.0 means every row)."""
    where: str | None = None
    """SQL predicate restricting the rows checked."""
    tags: list[str] = Field(default_factory=list)
    """Labels for selecting checks with ``dvf run --only tag:NAME``. Results carry them together
    with the table's and the suite's tags."""
    owner: str | None = None
    """Team or person responsible for the check, reported with its results. Defaults to the table's
    owner, then the suite's."""
    enabled: bool = True
    """Set to false to skip this check without removing it."""
    details: dict[str, Any] = Field(default_factory=dict)
    """Custom details copied into every result of this check (for example requirement IDs)."""
    sample_size: NonNegativeInt | None = Field(default=None, le=10_000)
    """Failing rows this check captures as evidence; ``0`` disables its samples. Defaults to the
    table's ``sample_size``."""
    action: Literal["fail", "warn", "drop", "quarantine"] | None = None
    """In-pipeline mode (``dvf.validate_dataframe``): what happens to rows failing this check.
    ``quarantine`` moves them to the quarantine frame, ``drop`` removes them, ``warn`` keeps them
    and caps the result at a warning, and ``fail`` keeps them and raises when the check fails.
    Defaults to the call's ``on_fail``. ``dvf run`` reports failures by severity and ignores it."""

    def target_columns(self) -> list[str]:
        """Columns this check is about, used to build its check ID."""
        columns = getattr(self, "columns", None)
        if isinstance(columns, list):
            return [str(c) for c in columns]
        column = getattr(self, "column", None)
        return [str(column)] if isinstance(column, str) else []


class ColumnsCheckConfig(CheckConfig):
    """A check over one or more columns."""

    columns: list[str] = Field(min_length=1)
    """Columns to check (at least one)."""


class ColumnCheckConfig(CheckConfig):
    """A check over a single column."""

    column: str
    """Column to check."""


class NotNullCheck(ColumnsCheckConfig):
    """Columns must not contain NULL."""


class NotBlankCheck(ColumnsCheckConfig):
    """Columns must not contain NULL, empty, or whitespace-only strings."""


class NullRatioCheck(ColumnCheckConfig):
    """The share of NULL values must not exceed a threshold."""

    max_ratio: float = Field(ge=0, le=1)
    """Largest accepted share of NULL values, from 0 to 1 (``0.2`` allows 20 percent)."""


class UniqueCheck(ColumnsCheckConfig):
    """Values (or combinations of values) must be unique."""

    ignore_nulls: bool = True
    """Rows where any of the columns is NULL are not counted as duplicates."""


class RegexCheck(ColumnCheckConfig):
    """Values must match a regular expression."""

    pattern: str
    """Regular expression the values must match. It runs in the checked database, so use syntax that
    database supports."""
    full_match: bool = True
    """Require the whole value to match, rather than any substring."""

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value


class AllowedValuesCheck(ColumnCheckConfig):
    """Values must belong to a fixed set."""

    values: list[str | int | float | bool] = Field(min_length=1)
    """Accepted values. They are compared as text unless all of them are numbers or all are
    booleans."""
    case_sensitive: bool = True
    """Set to false to compare text values case-insensitively."""


class RangeCheck(ColumnCheckConfig):
    """Values must fall within a range. Dates and timestamps may be given as ISO strings."""

    min: float | int | str | None = None
    """Lowest accepted value: a number, or a date or timestamp as an ISO string. Omit for no lower
    bound."""
    max: float | int | str | None = None
    """Highest accepted value: a number, or a date or timestamp as an ISO string. Omit for no upper
    bound."""
    inclusive: bool = True
    """Accept values equal to ``min`` or ``max``. Set to false for strict bounds."""

    @model_validator(mode="after")
    def _bounds(self) -> RangeCheck:
        if self.min is None and self.max is None:
            raise ValueError("set 'min', 'max', or both")
        return self


class LengthCheck(ColumnCheckConfig):
    """String lengths must fall within a range."""

    min: NonNegativeInt | None = None
    """Shortest accepted length in characters; omit for no lower bound."""
    max: PositiveInt | None = None
    """Longest accepted length in characters; omit for no upper bound."""

    @model_validator(mode="after")
    def _bounds(self) -> LengthCheck:
        if self.min is None and self.max is None:
            raise ValueError("set 'min', 'max', or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("'min' must not exceed 'max'")
        return self


def _merge_column_shorthand(model: Any) -> None:
    """Fold a ``column`` shorthand into ``columns`` and require exactly one form."""
    if model.column is not None:
        if model.columns:
            raise ValueError("set 'column' or 'columns', not both")
        model.columns = [model.column]
        model.column = None
    if not model.columns:
        raise ValueError("set 'column' or 'columns'")


class ParentRef(StrictModel):
    """The parent side of a referential integrity check."""

    connection: Identifier | None = None
    """Connection holding the parent table; defaults to the checked dataset's connection."""
    table: str
    """Parent table holding the referenced key, optionally qualified (``schema.table``)."""
    columns: list[str] = Field(default_factory=list)
    """Key columns of the parent table, paired by position with the check's ``columns``."""
    column: str | None = Field(default=None, exclude=True)
    """Shorthand for a single-column key; becomes ``columns``."""

    @model_validator(mode="after")
    def _column_shorthand(self) -> ParentRef:
        _merge_column_shorthand(self)
        return self


class ReferentialIntegrityCheck(CheckConfig):
    """Every value must exist in a parent table (no orphans)."""

    columns: list[str] = Field(default_factory=list)
    """Columns of the checked dataset whose values must exist in the parent, paired by position with
    ``parent.columns``."""
    column: str | None = Field(default=None, exclude=True)
    """Shorthand for a single-column key; becomes ``columns``."""
    parent: ParentRef
    """The parent table and its key columns."""

    @model_validator(mode="after")
    def _same_arity(self) -> ReferentialIntegrityCheck:
        _merge_column_shorthand(self)
        if len(self.columns) != len(self.parent.columns):
            raise ValueError("'columns' and 'parent.columns' must have the same length")
        return self


class ScalarExpectation(StrictModel):
    """An assertion on a scalar query result."""

    operator: Literal["==", "!=", "<", "<=", ">", ">="]
    """How the query result is compared with ``value``: ``<result> <operator> <value>``."""
    value: float
    """Number the query result is compared with."""


class CustomSqlCheck(CheckConfig):
    """A SQL query that returns failing rows, or a scalar compared with an expectation.

    ``{table}`` in the SQL is replaced with the checked dataset.
    """

    name: Identifier
    """Name of the check, required for this type; it becomes part of the check ID."""
    sql: str = Field(min_length=1)
    """SQL query to run. ``{table}`` stands for the checked dataset, with its filter applied and
    already aliased."""
    returns: Literal["failing_rows", "scalar"] = "failing_rows"
    """``failing_rows``: every row the query returns is a failure. ``scalar``: the query returns one
    number, which is compared with ``expect``."""
    expect: ScalarExpectation | None = None
    """The expectation the scalar result must meet; required when ``returns`` is ``scalar`` and not
    allowed otherwise."""

    @model_validator(mode="after")
    def _expectation(self) -> CustomSqlCheck:
        if self.returns == "scalar" and self.expect is None:
            raise ValueError("'expect' is required when 'returns' is 'scalar'")
        if self.returns == "failing_rows" and self.expect is not None:
            raise ValueError("'expect' only applies when 'returns' is 'scalar'")
        return self


class ExpressionCheck(CheckConfig):
    """A boolean SQL expression every row must satisfy, such as ``end_date >= start_date``."""

    name: Identifier
    """Name of the check, required for this type; it becomes part of the check ID."""
    expression: str = Field(min_length=1)
    """Boolean SQL expression every row must satisfy; rows where it is false or NULL fail."""


class RowCountRangeCheck(CheckConfig):
    """The number of rows must fall within a range."""

    min: NonNegativeInt | None = None
    """Fewest rows accepted; omit for no lower bound."""
    max: NonNegativeInt | None = None
    """Most rows accepted; omit for no upper bound."""

    @model_validator(mode="after")
    def _bounds(self) -> RowCountRangeCheck:
        if self.min is None and self.max is None:
            raise ValueError("set 'min', 'max', or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("'min' must not exceed 'max'")
        return self


class FreshnessCheck(ColumnCheckConfig):
    """The most recent value of a timestamp column must be newer than ``max_age``."""

    max_age: Duration
    """Largest accepted age of the column's newest value when the check runs, such as ``6h`` or
    ``1d``."""
