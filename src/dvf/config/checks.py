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
    severity: Severity | None = None
    """Inherited from the table when omitted."""
    side: Side | None = None
    """Dataset to check: ``source``, ``target``, or ``both``. Defaults to ``target`` for comparison suites and ``source`` otherwise."""
    mostly: float = Field(default=1.0, gt=0, le=1)
    """Pass when at least this share of rows satisfies the check (1.0 means every row)."""
    where: str | None = None
    """SQL predicate restricting the rows checked."""
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    enabled: bool = True
    details: dict[str, Any] = Field(default_factory=dict)
    """Custom details copied into every result of this check (for example requirement IDs)."""

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


class ColumnCheckConfig(CheckConfig):
    """A check over a single column."""

    column: str


class NotNullCheck(ColumnsCheckConfig):
    """Columns must not contain NULL."""


class NotBlankCheck(ColumnsCheckConfig):
    """Columns must not contain NULL, empty, or whitespace-only strings."""


class NullRatioCheck(ColumnCheckConfig):
    """The share of NULL values must not exceed a threshold."""

    max_ratio: float = Field(ge=0, le=1)


class UniqueCheck(ColumnsCheckConfig):
    """Values (or combinations of values) must be unique."""

    ignore_nulls: bool = True
    """Rows where any of the columns is NULL are not counted as duplicates."""


class RegexCheck(ColumnCheckConfig):
    """Values must match a regular expression."""

    pattern: str
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
    case_sensitive: bool = True


class RangeCheck(ColumnCheckConfig):
    """Values must fall within a range. Dates and timestamps may be given as ISO strings."""

    min: float | int | str | None = None
    max: float | int | str | None = None
    inclusive: bool = True

    @model_validator(mode="after")
    def _bounds(self) -> RangeCheck:
        if self.min is None and self.max is None:
            raise ValueError("set 'min', 'max', or both")
        return self


class LengthCheck(ColumnCheckConfig):
    """String lengths must fall within a range."""

    min: NonNegativeInt | None = None
    max: PositiveInt | None = None

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
    columns: list[str] = Field(default_factory=list)
    column: str | None = Field(default=None, exclude=True)
    """Shorthand for a single-column key; becomes ``columns``."""

    @model_validator(mode="after")
    def _column_shorthand(self) -> ParentRef:
        _merge_column_shorthand(self)
        return self


class ReferentialIntegrityCheck(CheckConfig):
    """Every value must exist in a parent table (no orphans)."""

    columns: list[str] = Field(default_factory=list)
    column: str | None = Field(default=None, exclude=True)
    """Shorthand for a single-column key; becomes ``columns``."""
    parent: ParentRef

    @model_validator(mode="after")
    def _same_arity(self) -> ReferentialIntegrityCheck:
        _merge_column_shorthand(self)
        if len(self.columns) != len(self.parent.columns):
            raise ValueError("'columns' and 'parent.columns' must have the same length")
        return self


class ScalarExpectation(StrictModel):
    """An assertion on a scalar query result."""

    operator: Literal["==", "!=", "<", "<=", ">", ">="]
    value: float


class CustomSqlCheck(CheckConfig):
    """A SQL query that returns failing rows, or a scalar compared with an expectation.

    ``{table}`` in the SQL is replaced with the checked dataset.
    """

    name: Identifier
    sql: str = Field(min_length=1)
    returns: Literal["failing_rows", "scalar"] = "failing_rows"
    expect: ScalarExpectation | None = None

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
    expression: str = Field(min_length=1)


class RowCountRangeCheck(CheckConfig):
    """The number of rows must fall within a range."""

    min: NonNegativeInt | None = None
    max: NonNegativeInt | None = None

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
