"""Built-in normalizers.

A normalizer rewrites a column's values before canonicalization, on both sides of a comparison,
wherever it applies to the column's type (a string normalizer leaves a number column alone). Each
one has three implementations that must agree: SQL per dialect, Polars, and the Python reference.

SAP normalizers are declared in the registry and arrive with the SAP pack (Phase 3).
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field, NonNegativeInt

from dvf.config.base import StrictModel
from dvf.core.types import CanonicalType, LogicalType
from dvf.normalize.canonical import (
    _WS_RE,
    decimal_text,
    parse_datetime,
    polars_lower,
    simple_lower,
    simple_upper,
    to_decimal,
    to_utc_naive,
    trim_spaces,
)
from dvf.plugins.base import Normalizer
from dvf.sql.dialects import Dialect

if TYPE_CHECKING:
    import polars as pl

_STRINGISH = frozenset({LogicalType.STRING, LogicalType.UNKNOWN})
_NUMERIC = frozenset({LogicalType.INTEGER, LogicalType.DECIMAL, LogicalType.FLOAT})
_TIMESTAMPS = frozenset({LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ})


class StringNormalizer(Normalizer):
    """A normalizer for text columns."""

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _STRINGISH


class _Trim(StringNormalizer):
    mode: ClassVar[str] = "both"

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.trim(expr, self.mode)

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        if self.mode == "both":
            return expr.str.strip_chars(" ")
        if self.mode == "rtrim":
            return expr.str.strip_chars_end(" ")
        return expr.str.strip_chars_start(" ")

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return None if value is None else trim_spaces(str(value), self.mode)


class Trim(_Trim):
    """Remove leading and trailing spaces."""


class RTrim(_Trim):
    """Remove trailing spaces (blank-padded CHAR columns)."""

    mode = "rtrim"


class LTrim(_Trim):
    """Remove leading spaces."""

    mode = "ltrim"


class Lower(StringNormalizer):
    """Lower-case strings, one character at a time."""

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.lower(expr)

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        return polars_lower(expr)

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return None if value is None else simple_lower(str(value))


class Upper(StringNormalizer):
    """Upper-case strings, one character at a time."""

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.upper(expr)

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        return expr.str.replace_all("ß", "ẞ", literal=True).str.to_uppercase()

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return None if value is None else simple_upper(str(value))


class CollapseWhitespace(StringNormalizer):
    """Replace runs of spaces, tabs, and line breaks with a single space."""

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.collapse_whitespace(expr)

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        return expr.str.replace_all(_WS_RE.pattern, " ")

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return None if value is None else _WS_RE.sub(" ", str(value))


class UnicodeNfc(StringNormalizer):
    """Unicode NFC normalization."""

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.unicode_normalize(expr, "NFC")

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        return expr.str.normalize("NFC")

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return None if value is None else unicodedata.normalize("NFC", str(value))


class NullIfEmpty(StringNormalizer):
    """Treat empty strings as NULL."""

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return f"NULLIF({expr}, '')"

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        return pl.when(expr == "").then(None).otherwise(expr)

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return None if value in (None, "") else value


class EmptyIfNull(StringNormalizer):
    """Treat NULL as an empty string."""

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return f"COALESCE({expr}, '')"

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        return expr.fill_null("")

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return "" if value is None else value


class StripLeadingZeros(StringNormalizer):
    """Remove leading zeros from strings, keeping a single ``0`` for all-zero values."""

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        stripped = dialect.trim(expr, "ltrim", "0")
        return f"(CASE WHEN {stripped} = '' AND {expr} <> '' THEN '0' ELSE {stripped} END)"

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        stripped = expr.str.strip_chars_start("0")
        return pl.when((stripped == "") & (expr != "")).then(pl.lit("0")).otherwise(stripped)

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        text = str(value)
        stripped = text.lstrip("0")
        return "0" if stripped == "" and text != "" else stripped


class RoundParams(StrictModel):
    digits: NonNegativeInt = Field(default=0, le=30, alias="value")
    """Decimal digits to keep (``{round: 2}`` or ``{round: {digits: 2}}``)."""


class Round(Normalizer):
    """Round numbers to a number of decimal digits (half away from zero)."""

    params_model = RoundParams

    @property
    def digits(self) -> int:
        params = self.params
        assert isinstance(params, RoundParams)
        return params.digits

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _NUMERIC

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.decimal(38, self.digits)

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.round_decimal(expr, self.digits)

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        digits = self.digits
        return expr.map_batches(
            lambda s: pl.Series(
                s.name,
                [None if v is None else decimal_text(v, digits) for v in s.to_list()],
                dtype=pl.String,
            ),
            return_dtype=pl.String,
        )

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        return to_decimal(decimal_text(value, self.digits))


_UNITS = {
    "s": "second",
    "second": "second",
    "m": "minute",
    "minute": "minute",
    "h": "hour",
    "hour": "hour",
    "d": "day",
    "day": "day",
    "ms": "millisecond",
    "millisecond": "millisecond",
}
_POLARS_UNITS = {"second": "1s", "minute": "1m", "hour": "1h", "day": "1d", "millisecond": "1ms"}


class TruncateParams(StrictModel):
    unit: Literal["s", "second", "m", "minute", "h", "hour", "d", "day", "ms", "millisecond"] = (
        Field(default="second", alias="value")
    )
    """Truncate timestamps to this unit."""


def truncate_datetime(dt: datetime, unit: str) -> datetime:
    if unit == "millisecond":
        return dt.replace(microsecond=dt.microsecond - dt.microsecond % 1000)
    dt = dt.replace(microsecond=0)
    if unit == "second":
        return dt
    dt = dt.replace(second=0)
    if unit == "minute":
        return dt
    dt = dt.replace(minute=0)
    return dt if unit == "hour" else dt.replace(hour=0)


class TruncateTimestamp(Normalizer):
    """Truncate timestamps to a unit (second, minute, hour, day, or millisecond)."""

    params_model = TruncateParams

    @property
    def unit(self) -> str:
        params = self.params
        assert isinstance(params, TruncateParams)
        return _UNITS[params.unit]

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _TIMESTAMPS

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.truncate_timestamp(expr, self.unit)

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        return expr.dt.truncate(_POLARS_UNITS[self.unit])

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        dt = parse_datetime(value)
        return truncate_datetime(dt, self.unit)


class ToUtcParams(StrictModel):
    from_tz: str = Field(default="UTC", alias="value")
    """Time zone that timestamps without an offset are in."""


class ToUtc(Normalizer):
    """Convert timestamps to UTC (timestamps without an offset are read in ``from_tz``)."""

    params_model = ToUtcParams

    @property
    def from_tz(self) -> str:
        params = self.params
        assert isinstance(params, ToUtcParams)
        return params.from_tz

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _TIMESTAMPS

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.timestamp(ctype.precision)

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        return dialect.to_utc(
            expr, tz_aware=ctype.logical is LogicalType.TIMESTAMP_TZ, timezone=self.from_tz
        )

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        if ctype.logical is LogicalType.TIMESTAMP_TZ:
            return expr.dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        if self.from_tz == "UTC":
            return expr
        return (
            expr.dt.replace_time_zone(self.from_tz)
            .dt.convert_time_zone("UTC")
            .dt.replace_time_zone(None)
        )

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        return None if value is None else to_utc_naive(value, self.from_tz)


BUILTIN_NORMALIZERS: dict[str, type[Normalizer]] = {
    "trim": Trim,
    "rtrim": RTrim,
    "ltrim": LTrim,
    "lower": Lower,
    "upper": Upper,
    "collapse_whitespace": CollapseWhitespace,
    "unicode_nfc": UnicodeNfc,
    "null_if_empty": NullIfEmpty,
    "empty_if_null": EmptyIfNull,
    "round": Round,
    "strip_leading_zeros": StripLeadingZeros,
    "truncate_timestamp": TruncateTimestamp,
    "to_utc": ToUtc,
}

__all__ = ["BUILTIN_NORMALIZERS"]
