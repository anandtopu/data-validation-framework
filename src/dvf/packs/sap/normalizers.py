"""SAP normalizers: ABAP storage conventions turned into comparable values.

Each normalizer has three implementations that must agree, as the built-in normalizers do: SQL
for every dialect (composed from :class:`~dvf.sql.dialects.Dialect` primitives), a Polars
expression, and the Python reference (:meth:`to_python`). ``tests/parity/test_sap_parity.py``
holds them to each other on DuckDB, SQLite, PostgreSQL, Polars, Spark, and (when configured) MySQL,
SQL Server, and Oracle.

Conventions shared by all of them:

* Text is matched exactly (case-sensitively), and "digits" means the ASCII digits ``0``-``9``.
* SAP ``CHAR`` values are blank-padded on some databases. The normalizers remove trailing spaces
  (``U+0020``) before they interpret a value; values they do not convert pass through unchanged.
* Empty strings are tested by length, because Oracle stores ``''`` as NULL.
* Conversions that could fail (text to DATE or TIME) run only on values that were validated
  first, inside ``CASE``, so an invalid value never aborts a query: it becomes NULL (or, for
  ``sap_dats``, an optional sentinel date).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping
from datetime import date, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from dvf.config.base import StrictModel
from dvf.core.errors import PluginError
from dvf.core.types import CanonicalType, LogicalType
from dvf.normalize.canonical import UNKNOWN_DECIMAL_SCALE, decimal_text, to_decimal
from dvf.packs.sap.tables import DEFAULT_CURRENCY_DECIMALS, T002_DEFAULTS, TCURX_DEFAULTS
from dvf.plugins.base import Normalizer, polars_input
from dvf.sql.dialects import Dialect

if TYPE_CHECKING:
    import polars as pl

DIGITS = "0123456789"
_STRINGISH = frozenset({LogicalType.STRING, LogicalType.UNKNOWN})
_DECIMALS = frozenset({LogicalType.DECIMAL, LogicalType.INTEGER})


def is_digits(text: str) -> bool:
    """Whether ``text`` is non-empty and holds only the ASCII digits ``0``-``9``."""
    return text != "" and all(c in DIGITS for c in text)


# ====================================================================== SQL building blocks


def sql_is_empty(d: Dialect, expr: str) -> str:
    """True when the non-NULL text ``expr`` is empty (also on Oracle, where ``''`` is NULL)."""
    return f"COALESCE({d.length(expr)}, 0) = 0"


def sql_is_digits(d: Dialect, expr: str) -> str:
    """True when ``expr`` is non-empty and holds only ASCII digits (False for NULL)."""
    rest = d.trim(expr, "both", DIGITS)
    return f"(COALESCE({d.length(expr)}, 0) > 0 AND COALESCE({d.length(rest)}, 0) = 0)"


def sql_zero_pad(d: Dialect, expr: str, width: int) -> str:
    """The non-NULL digits ``expr`` (at most ``width`` of them) left-padded with zeros."""
    zeros = d.literal("0" * width)
    size = f"COALESCE({d.length(expr)}, 0)"
    padded = d.substring(d.concat(zeros, expr), f"{size} + 1", width)
    return f"(CASE WHEN {size} = 0 THEN {zeros} ELSE {padded} END)"


def sql_pad_short(d: Dialect, expr: str, width: int) -> str:
    """``expr`` left-padded with zeros when shorter than ``width`` (integers read as DATS/TIMS)."""
    size = f"COALESCE({d.length(expr)}, 0)"
    return f"(CASE WHEN {size} < {width} THEN {sql_zero_pad(d, expr, width)} ELSE {expr} END)"


def sql_in(expr: str, literals: Iterable[str]) -> str:
    return f"{expr} IN ({', '.join(literals)})"


def _text_sql(d: Dialect, expr: str, ctype: CanonicalType) -> str:
    if ctype.logical is LogicalType.INTEGER:
        return d.integer_text(expr)
    return d.to_text(expr)


def _pl_text(expr: pl.Expr) -> pl.Expr:
    import polars as pl

    return expr.cast(pl.String)


def _python_text(value: Any) -> str:
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return value if isinstance(value, str) else str(value)


# ====================================================================== ALPHA and NUMC


def alpha_convert(digits: str, mode: str, width: int) -> str:
    """The ALPHA form of a digits-only value: leading zeros stripped, or padded to ``width``."""
    stripped = digits.lstrip("0")
    if mode == "strip":
        return stripped or "0"
    return stripped.rjust(width, "0") if len(stripped) <= width else stripped


def _alpha_sql(d: Dialect, trimmed: str, mode: str, width: int) -> str:
    """SQL for :func:`alpha_convert` of the digits-only expression ``trimmed``."""
    stripped = d.trim(trimmed, "ltrim", "0")
    size = f"COALESCE({d.length(stripped)}, 0)"
    if mode == "strip":
        return f"(CASE WHEN {size} = 0 THEN {d.literal('0')} ELSE {stripped} END)"
    return (
        f"(CASE WHEN {size} <= {width} THEN {sql_zero_pad(d, stripped, width)} ELSE {stripped} END)"
    )


def _alpha_polars(trimmed: pl.Expr, mode: str, width: int) -> pl.Expr:
    import polars as pl

    stripped = trimmed.str.strip_chars_start("0")
    if mode == "strip":
        return pl.when(stripped == "").then(pl.lit("0")).otherwise(stripped)
    return (
        pl.when(stripped.str.len_chars() <= width)
        .then(stripped.str.zfill(width))
        .otherwise(stripped)
    )


_DIGITS_RE = r"^[0-9]+$"


class AlphaParams(StrictModel):
    mode: Literal["strip", "pad"] = Field(default="strip", alias="value")
    """``strip`` removes leading zeros from digits-only values (``0000012345`` becomes
    ``12345``, all zeros become ``0``); ``pad`` adds leading zeros up to ``length``, like
    CONVERSION_EXIT_ALPHA_INPUT. ``{sap_alpha: pad}`` is a shorthand."""
    length: int = Field(default=10, ge=1, le=255)
    """Field length for ``pad`` (KUNNR and LIFNR 10, AUFNR 12, MATNR 18 or 40)."""


class SapAlpha(Normalizer):
    """SAP ALPHA conversion exit for keys such as KUNNR, LIFNR, MATNR, and AUFNR.

    Only digits-only values convert (spaces around them are ignored); anything else, such as
    ``ABC-1`` or ``12 34``, passes through unchanged, as in SAP.
    """

    params_model: ClassVar[type[BaseModel] | None] = AlphaParams

    @property
    def mode(self) -> str:
        return str(getattr(self.params, "mode", "strip"))

    @property
    def width(self) -> int:
        return int(getattr(self.params, "length", 10))

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _STRINGISH

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        text = d.to_text(expr)
        trimmed = d.trim(text, "both")
        converted = _alpha_sql(d, trimmed, self.mode, self.width)
        return f"(CASE WHEN {sql_is_digits(d, trimmed)} THEN {converted} ELSE {text} END)"

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        text = _pl_text(expr)
        trimmed = text.str.strip_chars(" ")
        converted = _alpha_polars(trimmed, self.mode, self.width)
        return pl.when(trimmed.str.contains(_DIGITS_RE)).then(converted).otherwise(text)

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        text = _python_text(value)
        trimmed = text.strip(" ")
        if not is_digits(trimmed):
            return text
        return alpha_convert(trimmed, self.mode, self.width)


class MatnrParams(StrictModel):
    length: Literal[18, 40] = Field(default=18, alias="value")
    """Length that digits-only material numbers are padded to. S/4HANA pads them to 18 even in
    its 40-character MATNR field unless the lexicographical setting is on, so 18 matches both ECC
    and a default S/4HANA. ``{sap_matnr_length: 40}`` is a shorthand."""
    mode: Literal["pad", "strip"] = "pad"
    """``pad`` to ``length``, or ``strip`` leading zeros (the length-independent form)."""


class SapMatnrLength(SapAlpha):
    """SAP material numbers (MATNR) from ECC (CHAR 18) and S/4HANA (CHAR 40) in one form.

    Digits-only material numbers lose their leading zeros and are padded to ``length`` (18 by
    default), so ``000000000000012345`` (ECC), a 40-character zero-padded value, and ``12345``
    all become ``000000000000012345``. Digits-only values longer than ``length`` keep their
    significant digits; other values pass through unchanged.
    """

    params_model = MatnrParams

    @property
    def width(self) -> int:
        return int(getattr(self.params, "length", 18))

    @property
    def mode(self) -> str:
        return str(getattr(self.params, "mode", "pad"))


class NumcParams(StrictModel):
    length: int | None = Field(default=None, ge=1, le=255, alias="value")
    """Field length. With a length, values are padded with zeros to it (``{sap_numc: 6}`` turns
    ``10`` into ``000010``); without one, leading zeros are stripped (``000010`` becomes
    ``10``), which compares NUMC text with numbers stored as text."""
    invalid: Literal["keep", "null"] = "keep"
    """What happens to values with characters other than digits: ``keep`` them unchanged (so
    they show up as differences), or turn them into ``null``."""


class SapNumc(Normalizer):
    """SAP NUMC (numeric text): digits only, zero-padded to the field length.

    Spaces around the digits are ignored, and a blank value is SAP's initial value, all zeros.
    """

    params_model = NumcParams

    @property
    def p(self) -> NumcParams:
        assert isinstance(self.params, NumcParams)
        return self.params

    @property
    def mode(self) -> str:
        return "strip" if self.p.length is None else "pad"

    @property
    def width(self) -> int:
        return self.p.length or 0

    def _zero(self) -> str:
        return "0" if self.p.length is None else "0" * self.p.length

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _STRINGISH

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        text = d.to_text(expr)
        trimmed = d.trim(text, "both")
        converted = _alpha_sql(d, trimmed, self.mode, self.width)
        fallback = text if self.p.invalid == "keep" else "NULL"
        return (
            f"(CASE WHEN {text} IS NULL THEN NULL "
            f"WHEN {sql_is_empty(d, trimmed)} THEN {d.literal(self._zero())} "
            f"WHEN {sql_is_digits(d, trimmed)} THEN {converted} ELSE {fallback} END)"
        )

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        text = _pl_text(expr)
        trimmed = text.str.strip_chars(" ")
        converted = _alpha_polars(trimmed, self.mode, self.width)
        fallback = text if self.p.invalid == "keep" else pl.lit(None, dtype=pl.String)
        return (
            pl.when(text.is_null())
            .then(pl.lit(None, dtype=pl.String))
            .when(trimmed == "")
            .then(pl.lit(self._zero()))
            .when(trimmed.str.contains(_DIGITS_RE))
            .then(converted)
            .otherwise(fallback)
        )

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        text = _python_text(value)
        trimmed = text.strip(" ")
        if trimmed == "":
            return self._zero()
        if is_digits(trimmed):
            return alpha_convert(trimmed, self.mode, self.width)
        return text if self.p.invalid == "keep" else None


# ====================================================================== DATS and TIMS

_MULTIPLES_OF_4 = [f"{n:02d}" for n in range(0, 100, 4)]
_DATE_RE = r"^[0-9]{4}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$"
_TIME_RE = r"^([01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]$"


def _date_or_none(text: str) -> date | None:
    if len(text) != 8 or not is_digits(text):
        return None
    year, month, day = int(text[:4]), int(text[4:6]), int(text[6:])
    if year < 1:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


class DatsParams(StrictModel):
    invalid: Literal["null", "sentinel"] = "null"
    """What an invalid value (``20230230``, ``2023AB01``, a wrong length) becomes: ``null``, or
    the ``sentinel`` date, so that invalid source dates differ from a NULL target."""
    sentinel: date = date(1, 1, 1)
    """Date used for invalid values when ``invalid`` is ``sentinel``."""


class SapDats(Normalizer):
    """SAP DATS (``YYYYMMDD`` text) to DATE.

    ``00000000``, blank, and NULL become NULL (SAP's initial date). Valid ``YYYYMMDD`` values
    (years 0001 to 9999, real calendar days, leap years included) become dates. Anything else is
    invalid and becomes NULL, or the sentinel date (``invalid: sentinel``). Trailing spaces are
    ignored. Integer columns (``20240131`` stored as a number) are read as 8-digit text.

    Invalid values never abort a query: the SQL validates the digits, month, and day with string
    comparisons first, and converts only values that passed. Use a data quality check (``regex``
    on the raw column) to list them.
    """

    params_model = DatsParams

    @property
    def p(self) -> DatsParams:
        assert isinstance(self.params, DatsParams)
        return self.params

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _STRINGISH or ctype.logical is LogicalType.INTEGER

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.date()

    def _invalid_python(self) -> date | None:
        return self.p.sentinel if self.p.invalid == "sentinel" else None

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        text = _python_text(value)
        if ctype.logical is LogicalType.INTEGER and len(text) < 8:
            text = text.rjust(8, "0")
        trimmed = text.rstrip(" ")
        if trimmed in ("", "00000000"):
            return None
        parsed = _date_or_none(trimmed)
        return parsed if parsed is not None else self._invalid_python()

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        text = _text_sql(d, expr, ctype)
        if ctype.logical is LogicalType.INTEGER:
            text = sql_pad_short(d, text, 8)
        t = d.trim(text, "rtrim")

        def part(start: int, length: int) -> str:
            return d.substring(t, start, length)

        def lit(value: str) -> str:
            return d.literal(value)

        year, month, day = part(1, 4), part(5, 2), part(7, 2)
        century, yy = part(1, 2), part(3, 2)
        fours = [lit(v) for v in _MULTIPLES_OF_4]
        leap = (
            f"(({yy} <> {lit('00')} AND {sql_in(yy, fours[1:])}) "
            f"OR ({yy} = {lit('00')} AND {sql_in(century, fours)}))"
        )
        last_day = (
            f"(CASE WHEN {sql_in(month, [lit(m) for m in ('04', '06', '09', '11')])} "
            f"THEN {lit('30')} WHEN {month} = {lit('02')} "
            f"THEN (CASE WHEN {leap} THEN {lit('29')} ELSE {lit('28')} END) ELSE {lit('31')} END)"
        )
        shape = f"(COALESCE({d.length(t)}, 0) = 8 AND {sql_is_digits(d, t)})"
        valid = (
            f"({shape} AND {year} <> {lit('0000')} AND {month} >= {lit('01')} "
            f"AND {month} <= {lit('12')} AND {day} >= {lit('01')} AND {day} <= {last_day})"
        )
        initial = f"({sql_is_empty(d, t)} OR {t} = {lit('00000000')})"
        iso = d.concat(year, lit("-"), month, lit("-"), day)
        invalid = (
            d.date_from_iso(lit(self.p.sentinel.isoformat()))
            if self.p.invalid == "sentinel"
            else "NULL"
        )
        return (
            f"(CASE WHEN {text} IS NULL THEN NULL WHEN {initial} THEN NULL "
            f"WHEN {valid} THEN {d.date_from_iso(iso)} ELSE {invalid} END)"
        )

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        text = _pl_text(expr)
        if ctype.logical is LogicalType.INTEGER:
            text = pl.when(text.str.len_chars() < 8).then(text.str.zfill(8)).otherwise(text)
        t = text.str.strip_chars_end(" ")
        iso = pl.concat_str(
            [t.str.slice(0, 4), pl.lit("-"), t.str.slice(4, 2), pl.lit("-"), t.str.slice(6, 2)]
        )
        parsed = iso.str.to_date("%Y-%m-%d", strict=False)
        shape_ok = t.str.contains(_DATE_RE) & (t.str.slice(0, 4) != "0000")
        invalid = (
            pl.lit(self.p.sentinel, dtype=pl.Date)
            if self.p.invalid == "sentinel"
            else pl.lit(None, dtype=pl.Date)
        )
        return (
            pl.when(text.is_null())
            .then(pl.lit(None, dtype=pl.Date))
            .when((t == "") | (t == "00000000"))
            .then(pl.lit(None, dtype=pl.Date))
            .when(shape_ok & parsed.is_not_null())
            .then(parsed)
            .otherwise(invalid)
        )


class TimsParams(StrictModel):
    zero_as_null: bool = True
    """Treat ``000000`` (SAP's initial time) as NULL. It is also midnight: set ``false`` when
    the field holds real midnight times."""


class SapTims(Normalizer):
    """SAP TIMS (``HHMMSS`` text) to TIME.

    Blank and NULL become NULL, and so does ``000000`` unless ``zero_as_null`` is false. Valid
    times (``000000`` to ``235959``) become times; invalid values (``240000``, ``12AB00``, a wrong
    length) become NULL without aborting the query. Trailing spaces are ignored, and integer
    columns are read as 6-digit text.
    """

    params_model = TimsParams

    @property
    def zero_as_null(self) -> bool:
        return bool(getattr(self.params, "zero_as_null", True))

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _STRINGISH or ctype.logical is LogicalType.INTEGER

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.time(0)

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        text = _python_text(value)
        if ctype.logical is LogicalType.INTEGER and len(text) < 6:
            text = text.rjust(6, "0")
        trimmed = text.rstrip(" ")
        if trimmed == "" or (self.zero_as_null and trimmed == "000000"):
            return None
        if len(trimmed) != 6 or not is_digits(trimmed):
            return None
        hour, minute, second = int(trimmed[:2]), int(trimmed[2:4]), int(trimmed[4:])
        if hour > 23 or minute > 59 or second > 59:
            return None
        return time(hour, minute, second)

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        text = _text_sql(d, expr, ctype)
        if ctype.logical is LogicalType.INTEGER:
            text = sql_pad_short(d, text, 6)
        t = d.trim(text, "rtrim")
        hour, minute, second = d.substring(t, 1, 2), d.substring(t, 3, 2), d.substring(t, 5, 2)
        shape = f"(COALESCE({d.length(t)}, 0) = 6 AND {sql_is_digits(d, t)})"
        valid = (
            f"({shape} AND {hour} <= {d.literal('23')} AND {minute} <= {d.literal('59')} "
            f"AND {second} <= {d.literal('59')})"
        )
        initial = sql_is_empty(d, t)
        if self.zero_as_null:
            initial = f"({initial} OR {t} = {d.literal('000000')})"
        iso = d.concat(hour, d.literal(":"), minute, d.literal(":"), second)
        return (
            f"(CASE WHEN {text} IS NULL THEN NULL WHEN {initial} THEN NULL "
            f"WHEN {valid} THEN {d.time_from_iso(iso)} ELSE NULL END)"
        )

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        text = _pl_text(expr)
        if ctype.logical is LogicalType.INTEGER:
            text = pl.when(text.str.len_chars() < 6).then(text.str.zfill(6)).otherwise(text)
        t = text.str.strip_chars_end(" ")
        iso = pl.concat_str(
            [t.str.slice(0, 2), pl.lit(":"), t.str.slice(2, 2), pl.lit(":"), t.str.slice(4, 2)]
        )
        initial = t == ""
        if self.zero_as_null:
            initial = initial | (t == "000000")
        none = pl.lit(None, dtype=pl.Time)
        return (
            pl.when(text.is_null() | initial)
            .then(none)
            .when(t.str.contains(_TIME_RE))
            .then(iso.str.to_time("%H:%M:%S", strict=False))
            .otherwise(none)
        )


# ====================================================================== flags


class BoolParams(StrictModel):
    true_values: list[str] = Field(default_factory=lambda: ["X"], min_length=1)
    """Values that mean true, compared exactly after removing trailing spaces."""
    false_values: list[str] = Field(default_factory=lambda: [""])
    """Values that mean false; ``""`` stands for blank (empty or spaces only)."""
    other: Literal["null", "false"] = "null"
    """What other values (``x``, ``Y``, ``1``) become: ``null`` (so they differ from a boolean
    target), or ``false`` (ABAP's ``flag = abap_true`` test)."""
    null_as_false: bool = False
    """Treat NULL as false. SAP writes blanks, not NULL, but HANA holds NULL in columns added to
    a table after rows existed, and Oracle stores empty strings as NULL."""

    @field_validator("true_values", "false_values")
    @classmethod
    def _strip(cls, values: list[str]) -> list[str]:
        return [v.rstrip(" ") for v in values]

    @model_validator(mode="after")
    def _disjoint(self) -> BoolParams:
        if "" in self.true_values:
            raise ValueError("blank cannot be a true value")
        both = set(self.true_values) & set(self.false_values)
        if both:
            raise ValueError(f"values listed as both true and false: {sorted(both)}")
        return self


class SapBool(Normalizer):
    """SAP flags (CHAR 1): ``X`` is true and blank is false.

    Other values become NULL by default (``other: false`` makes them false), and NULL stays NULL
    unless ``null_as_false`` is set. Matching is exact: a lowercase ``x`` is not true unless it
    is listed in ``true_values``.
    """

    params_model = BoolParams

    @property
    def p(self) -> BoolParams:
        assert isinstance(self.params, BoolParams)
        return self.params

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _STRINGISH

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.boolean()

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return False if self.p.null_as_false else None
        trimmed = _python_text(value).rstrip(" ")
        if trimmed in self.p.true_values:
            return True
        if trimmed in self.p.false_values:
            return False
        return False if self.p.other == "false" else None

    def _condition(self, d: Dialect, t: str, values: list[str]) -> str:
        parts = []
        exact = [d.literal(v) for v in values if v != ""]
        if exact:
            parts.append(sql_in(t, exact))
        if "" in values:
            parts.append(sql_is_empty(d, t))
        return "(" + " OR ".join(parts) + ")" if parts else "(1 = 0)"

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        text = d.to_text(expr)
        t = d.trim(text, "rtrim")
        null = d.literal(False) if self.p.null_as_false else "NULL"
        other = d.literal(False) if self.p.other == "false" else "NULL"
        false_branch = ""
        if self.p.false_values:
            false_branch = (
                f"WHEN {self._condition(d, t, self.p.false_values)} THEN {d.literal(False)} "
            )
        return (
            f"(CASE WHEN {text} IS NULL THEN {null} "
            f"WHEN {self._condition(d, t, self.p.true_values)} THEN {d.literal(True)} "
            f"{false_branch}ELSE {other} END)"
        )

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        text = _pl_text(expr)
        t = text.str.strip_chars_end(" ")
        null = pl.lit(False) if self.p.null_as_false else pl.lit(None, dtype=pl.Boolean)
        other = pl.lit(False) if self.p.other == "false" else pl.lit(None, dtype=pl.Boolean)
        chain = (
            pl.when(text.is_null()).then(null).when(t.is_in(self.p.true_values)).then(pl.lit(True))
        )
        if self.p.false_values:
            chain = chain.when(t.is_in(self.p.false_values)).then(pl.lit(False))
        return chain.otherwise(other)


# ====================================================================== language keys


class SprasParams(StrictModel):
    direction: Literal["to_iso", "to_sap"] = Field(default="to_iso", alias="value")
    """``to_iso`` turns internal one-character keys (``E``) into two-character codes (``EN``);
    ``to_sap`` does the reverse. ``{sap_spras: to_sap}`` is a shorthand."""
    overrides: dict[str, str] = Field(default_factory=dict)
    """Entries that replace or extend the built-in T002 table, as ``{internal: code}``, for
    example ``{Z: Z1}``. Read your system's table with ``SELECT SPRAS, LAISO FROM T002``."""
    unknown: Literal["keep", "null"] = "keep"
    """What values that are not in the table become: ``keep`` them unchanged, or ``null``."""

    @field_validator("overrides")
    @classmethod
    def _shape(cls, overrides: dict[str, str]) -> dict[str, str]:
        for key, code in overrides.items():
            if len(key) != 1:
                raise ValueError(f"internal language keys have one character, got {key!r}")
            if len(code) != 2 or not code.isascii():
                raise ValueError(
                    f"language codes have two ASCII characters, got {code!r} for {key!r}"
                )
        return overrides


class SapSpras(Normalizer):
    """SAP language keys: one-character internal keys (SPRAS) to two-character codes (T002).

    The mapping is case-sensitive (``a`` is Afrikaans, ``A`` Arabic). In the ``to_sap``
    direction, codes are matched case-insensitively for ASCII letters (``en`` and ``EN`` both
    become ``E``). Values already in the other form pass through unchanged, so the normalizer can
    be applied to both sides of a comparison.
    """

    params_model = SprasParams

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        assert isinstance(self.params, SprasParams)
        table = {**T002_DEFAULTS, **self.params.overrides}
        if self.params.direction == "to_iso":
            self.mapping = table
        else:
            inverse: dict[str, str] = {}
            for key, code in table.items():
                upper = code.upper()
                if upper in inverse and inverse[upper] != key:
                    raise PluginError(
                        f"Invalid parameters for normalizer 'sap_spras': code {code!r} maps to "
                        f"both {inverse[upper]!r} and {key!r}; fix 'overrides'"
                    )
                inverse[upper] = key
            self.mapping = inverse

    @property
    def p(self) -> SprasParams:
        assert isinstance(self.params, SprasParams)
        return self.params

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _STRINGISH

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.string()

    def _lookup_key(self, trimmed: str) -> str:
        return trimmed.upper() if self.p.direction == "to_sap" and trimmed.isascii() else trimmed

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        text = _python_text(value)
        found = self.mapping.get(self._lookup_key(text.rstrip(" ")))
        if found is not None:
            return found
        return text if self.p.unknown == "keep" else None

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        text = d.to_text(expr)
        t = d.trim(text, "rtrim")
        branches = []
        for key, code in self.mapping.items():
            if self.p.direction == "to_sap":
                # Only ASCII letters fold: each case variant of the code is listed.
                condition = sql_in(t, [d.literal(v) for v in _ascii_case_variants(key)])
            else:
                condition = f"{t} = {d.literal(key)}"
            branches.append(f"WHEN {condition} THEN {d.literal(code)}")
        fallback = text if self.p.unknown == "keep" else "NULL"
        return f"(CASE {' '.join(branches)} ELSE {fallback} END)"

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        text = _pl_text(expr)
        t = text.str.strip_chars_end(" ")
        if self.p.direction == "to_sap":
            mapping = {
                v: code for key, code in self.mapping.items() for v in _ascii_case_variants(key)
            }
        else:
            mapping = dict(self.mapping)
        mapped = t.replace_strict(mapping, default=None, return_dtype=pl.String)
        fallback = text if self.p.unknown == "keep" else pl.lit(None, dtype=pl.String)
        return pl.when(mapped.is_not_null()).then(mapped).otherwise(fallback)


def _ascii_case_variants(code: str) -> list[str]:
    """Every spelling of ``code`` that differs only in the case of ASCII letters."""
    variants = [""]
    for ch in code:
        options = {ch, ch.lower(), ch.upper()} if ch.isascii() and ch.isalpha() else {ch}
        variants = [v + o for v in variants for o in sorted(options)]
    return variants


# ====================================================================== amounts and quantities


class _SideBound(Normalizer):
    """A normalizer that applies on configured sides and may read another column of the row."""

    _bound_side: str | None = None

    @property
    def side_param(self) -> str:
        return str(getattr(self.params, "side", "both"))

    def for_side(self, side: str) -> Normalizer | None:
        if self.side_param not in ("both", side):
            return None
        bound = copy.copy(self)
        bound._bound_side = side
        return bound

    def _column(self) -> str | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def input_columns(self) -> tuple[str, ...]:
        column = self._column()
        return (column,) if column else ()


def _shift_factor_sql(d: Dialect, exponent: int) -> str:
    """SQL for the exact decimal ``10 ** exponent``."""
    if exponent >= 0:
        return str(10**exponent)
    return d.round_decimal(d.literal(Decimal(1).scaleb(exponent)), -exponent)


def _decimal_scale(ctype: CanonicalType, fallback: int) -> int:
    if ctype.logical is LogicalType.INTEGER:
        return 0
    if ctype.scale is not None:
        return ctype.scale
    return fallback


def _keys_by_value(mapping: Mapping[str, int], skip: int | None) -> dict[int, list[str]]:
    groups: dict[int, list[str]] = {}
    for key, value in sorted(mapping.items()):
        if value != skip:
            groups.setdefault(value, []).append(key)
    return groups


class CurrParams(StrictModel):
    currency: str | None = None
    """A constant currency key for every row, for example ``JPY``."""
    currency_column: str | None = None
    """The column holding each row's currency key on the source side (for example ``WAERS``).
    It must be in the same dataset: use a ``query`` dataset to join it from a header table."""
    target_currency_column: str | None = None
    """The currency column on the target side, when it has another name."""
    side: Literal["source", "target", "both"] = "source"
    """Which side stores SAP-internal amounts and is shifted: ``source`` (default), ``target``,
    or ``both``."""
    decimals: dict[str, int] = Field(default_factory=dict)
    """Decimals per currency key (TCURX.CURRDEC) that replace or extend the built-in table, for
    example ``{CLF: 4}``. Read yours with ``SELECT CURRKEY, CURRDEC FROM TCURX``."""
    default_decimals: int = Field(default=DEFAULT_CURRENCY_DECIMALS, ge=0, le=14)
    """Decimals of currencies that are not in the table (SAP: 2)."""
    field_decimals: int = Field(default=2, ge=0, le=14)
    """Decimals of the amount field's domain (2 for almost every CURR field)."""

    @field_validator("decimals")
    @classmethod
    def _range(cls, decimals: dict[str, int]) -> dict[str, int]:
        bad = {k: v for k, v in decimals.items() if not 0 <= v <= 14}
        if bad:
            raise ValueError(f"currency decimals must be between 0 and 14: {bad}")
        return decimals

    @model_validator(mode="after")
    def _one_source(self) -> CurrParams:
        if (self.currency is None) == (self.currency_column is None):
            raise ValueError("set exactly one of 'currency' or 'currency_column'")
        if self.target_currency_column is not None and self.currency_column is None:
            raise ValueError("'target_currency_column' needs 'currency_column'")
        return self


class SapCurr(_SideBound):
    """SAP currency amounts (CURR): the decimal shift of currencies without two decimals.

    SAP stores amounts with the decimals of the field (2), whatever the currency: 100 JPY is
    stored as ``1.00``, and 1.234 KWD as ``12.34``. This normalizer multiplies the stored value by
    ``10 ** (field_decimals - currency_decimals)`` on the side that holds SAP-internal amounts,
    with the currency's decimals from TCURX (built-in table plus ``decimals``). The currency comes
    from a column of the same row or a constant, and is matched exactly after removing trailing
    spaces; unknown, blank, and NULL currencies have ``default_decimals``.
    """

    params_model = CurrParams

    @property
    def p(self) -> CurrParams:
        assert isinstance(self.params, CurrParams)
        return self.params

    @property
    def table(self) -> dict[str, int]:
        return {**TCURX_DEFAULTS, **self.p.decimals}

    def _column(self) -> str | None:
        if self.p.currency_column is None:
            return None
        if self._bound_side == "target" and self.p.target_currency_column:
            return self.p.target_currency_column
        return self.p.currency_column

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _DECIMALS

    def _in_scale(self, ctype: CanonicalType) -> int:
        return _decimal_scale(ctype, self.p.field_decimals)

    def _out_scale(self, ctype: CanonicalType) -> int:
        most = max([self.p.default_decimals, *self.table.values()])
        return self._in_scale(ctype) + max(0, most - self.p.field_decimals)

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.decimal(38, self._out_scale(ctype))

    def decimals_of(self, currency: Any) -> int:
        """Decimals of ``currency`` (a key, blank, or None)."""
        if currency is None:
            return self.p.default_decimals
        return self.table.get(_python_text(currency).rstrip(" "), self.p.default_decimals)

    def convert(self, value: Any, currency: Any, ctype: CanonicalType) -> Decimal | None:
        """The reference: ``value`` (an SAP-internal amount) in ``currency``, shifted."""
        if value is None:
            return None
        shift = self.p.field_decimals - self.decimals_of(currency)
        out = to_decimal(value).scaleb(shift)
        return to_decimal(decimal_text(out, self._out_scale(ctype)))

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if self.p.currency is None:
            raise NotImplementedError(
                "sap_curr reads the currency from column "
                f"'{self.p.currency_column}'; use convert(value, currency, ctype)"
            )
        return self.convert(value, self.p.currency, ctype)

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        out = self._out_scale(ctype)
        value = d.round_decimal(expr, self._in_scale(ctype))

        def shifted(currency_decimals: int) -> str:
            exponent = self.p.field_decimals - currency_decimals
            if exponent == 0:
                return d.round_decimal(value, out)
            return d.round_decimal(f"({value}) * {_shift_factor_sql(d, exponent)}", out)

        if self.p.currency is not None:
            return shifted(self.decimals_of(self.p.currency))
        column = self._column()
        assert column is not None
        key = d.trim(d.to_text(d.quote(column)), "rtrim")
        branches = [
            f"WHEN {sql_in(key, [d.literal(k) for k in keys])} THEN {shifted(decimals)}"
            for decimals, keys in _keys_by_value(self.table, self.p.default_decimals).items()
        ]
        if not branches:
            return shifted(self.p.default_decimals)
        return f"(CASE {' '.join(branches)} ELSE {shifted(self.p.default_decimals)} END)"

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        out = self._out_scale(ctype)

        def text(amount: Decimal | None) -> str | None:
            return None if amount is None else decimal_text(amount, out)

        if self.p.currency is not None:
            currency = self.p.currency
            return _map_values(expr, lambda v: text(self.convert(v, currency, ctype)))
        column = self._column()
        assert column is not None
        return _map_pairs(
            expr, polars_input(column), lambda v, c: text(self.convert(v, c, ctype)), pl.String
        )


class QuanParams(StrictModel):
    unit: str | None = None
    """A constant unit of measure for every row, for example ``KG``."""
    unit_column: str | None = None
    """The column holding each row's unit on the source side (for example ``MEINS``)."""
    target_unit_column: str | None = None
    """The unit column on the target side, when it has another name."""
    side: Literal["source", "target", "both"] = "both"
    """Which sides are rounded (rounding is the same on both, so the default is ``both``)."""
    decimals: dict[str, int] = Field(default_factory=dict)
    """Decimals per unit (T006.DECAN), for example ``{ST: 0, KG: 3}``. Read yours with
    ``SELECT MSEHI, DECAN FROM T006``."""
    default_decimals: int | None = Field(default=None, ge=0, le=14)
    """Decimals for units that are not in ``decimals``; unset leaves those values unrounded."""

    @field_validator("decimals")
    @classmethod
    def _range(cls, decimals: dict[str, int]) -> dict[str, int]:
        bad = {k: v for k, v in decimals.items() if not 0 <= v <= 14}
        if bad:
            raise ValueError(f"unit decimals must be between 0 and 14: {bad}")
        return decimals

    @model_validator(mode="after")
    def _one_source(self) -> QuanParams:
        if (self.unit is None) == (self.unit_column is None):
            raise ValueError("set exactly one of 'unit' or 'unit_column'")
        if self.target_unit_column is not None and self.unit_column is None:
            raise ValueError("'target_unit_column' needs 'unit_column'")
        return self


class SapQuan(_SideBound):
    """SAP quantities (QUAN) rounded to the decimals of their unit of measure.

    The unit comes from a column of the same row or a constant, and is matched exactly after
    removing trailing spaces. Values are rounded half away from zero to the unit's decimals
    (``decimals``, else ``default_decimals``); values of other units are left as they are.
    """

    params_model = QuanParams

    @property
    def p(self) -> QuanParams:
        assert isinstance(self.params, QuanParams)
        return self.params

    def _column(self) -> str | None:
        if self.p.unit_column is None:
            return None
        if self._bound_side == "target" and self.p.target_unit_column:
            return self.p.target_unit_column
        return self.p.unit_column

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical in _DECIMALS

    def _out_scale(self, ctype: CanonicalType) -> int:
        return _decimal_scale(ctype, UNKNOWN_DECIMAL_SCALE)

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        return CanonicalType.decimal(38, self._out_scale(ctype))

    def decimals_of(self, unit: Any) -> int | None:
        if unit is None:
            return self.p.default_decimals
        return self.p.decimals.get(_python_text(unit).rstrip(" "), self.p.default_decimals)

    def convert(self, value: Any, unit: Any, ctype: CanonicalType) -> Decimal | None:
        """The reference: ``value`` rounded to the decimals of ``unit``."""
        if value is None:
            return None
        digits = self.decimals_of(unit)
        amount = to_decimal(value)
        if digits is not None:
            amount = to_decimal(decimal_text(amount, digits))
        return to_decimal(decimal_text(amount, self._out_scale(ctype)))

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if self.p.unit is None:
            raise NotImplementedError(
                f"sap_quan reads the unit from column '{self.p.unit_column}'; "
                "use convert(value, unit, ctype)"
            )
        return self.convert(value, self.p.unit, ctype)

    def to_sql(self, expr: str, dialect: Dialect, ctype: CanonicalType) -> str:
        d = dialect
        out = self._out_scale(ctype)

        def rounded(digits: int | None) -> str:
            if digits is None:
                return d.round_decimal(expr, out)
            return d.round_decimal(d.round_decimal(expr, digits), out)

        if self.p.unit is not None:
            return rounded(self.decimals_of(self.p.unit))
        column = self._column()
        assert column is not None
        key = d.trim(d.to_text(d.quote(column)), "rtrim")
        groups: dict[int, list[str]] = {}
        for unit, digits in sorted(self.p.decimals.items()):
            if digits != self.p.default_decimals:
                groups.setdefault(digits, []).append(unit)
        branches = [
            f"WHEN {sql_in(key, [d.literal(u) for u in units])} THEN {rounded(digits)}"
            for digits, units in groups.items()
        ]
        if not branches:
            return rounded(self.p.default_decimals)
        return f"(CASE {' '.join(branches)} ELSE {rounded(self.p.default_decimals)} END)"

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:
        import polars as pl

        out = self._out_scale(ctype)

        def text(amount: Decimal | None) -> str | None:
            return None if amount is None else decimal_text(amount, out)

        if self.p.unit is not None:
            unit = self.p.unit
            return _map_values(expr, lambda v: text(self.convert(v, unit, ctype)))
        column = self._column()
        assert column is not None
        return _map_pairs(
            expr, polars_input(column), lambda v, u: text(self.convert(v, u, ctype)), pl.String
        )


def _map_values(expr: pl.Expr, func: Callable[[Any], Any]) -> pl.Expr:
    """Apply the Python reference element-wise; the result is decimal text, as ``round`` does."""
    import polars as pl

    def apply(series: pl.Series) -> pl.Series:
        return pl.Series(series.name, [func(v) for v in series.to_list()], dtype=pl.String)

    return expr.map_batches(apply, return_dtype=pl.String)


def _map_pairs(
    expr: pl.Expr, other: pl.Expr, func: Callable[[Any, Any], Any], dtype: Any
) -> pl.Expr:
    import polars as pl

    def apply(series: pl.Series) -> pl.Series:
        rows = series.to_list()
        return pl.Series(series.name, [func(r["v"], r["o"]) for r in rows], dtype=dtype)

    return pl.struct(expr.alias("v"), other.alias("o")).map_batches(apply, return_dtype=dtype)


SAP_NORMALIZERS: dict[str, type[Normalizer]] = {
    "sap_alpha": SapAlpha,
    "sap_matnr_length": SapMatnrLength,
    "sap_numc": SapNumc,
    "sap_dats": SapDats,
    "sap_tims": SapTims,
    "sap_bool": SapBool,
    "sap_spras": SapSpras,
    "sap_curr": SapCurr,
    "sap_quan": SapQuan,
}
"""The SAP normalizers by registered name."""

__all__ = [
    "SAP_NORMALIZERS",
    "SapAlpha",
    "SapBool",
    "SapCurr",
    "SapDats",
    "SapMatnrLength",
    "SapNumc",
    "SapQuan",
    "SapSpras",
    "SapTims",
    "alpha_convert",
    "is_digits",
]
