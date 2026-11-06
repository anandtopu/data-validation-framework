"""Canonicalization: one comparable representation per value, identical in every engine.

Before values are compared or hashed, each side's column is converted to a canonical string
(BUILD_PROMPT.md section 8.1). This module holds three implementations of the same rules:

* :func:`canonical_text` and :func:`numeric_value`: the pure-Python **reference**. It defines the
  rules; the other implementations are tested against it (``tests/parity``).
* :func:`canonical_sql` and :func:`numeric_sql`: SQL for each dialect, pushed down to databases.
* :func:`canonical_polars` and :func:`numeric_polars`: Polars expressions for the Polars engine.

Rules, by comparison kind:

========== ==================================================================================
TEXT       Unicode normalization, space trimming, whitespace collapsing, per-character lower
           case, and empty-to-NULL, each as configured (in that order).
INTEGER    Base-10 digits.
DECIMAL    Fixed scale, rounded half away from zero, no exponent, never ``-0``.
FLOAT      Compared numerically with a tolerance; never hashed.
BOOLEAN    ``1`` or ``0``.
DATE       ``YYYY-MM-DD``.
TIMESTAMP  ``YYYY-MM-DDTHH:MM:SS[.fff|.ffffff]`` in UTC, truncated to the configured precision.
           Timestamps without an offset are assumed to be in ``normalization.timezone``.
TIME       ``HH:MM:SS[.fff|.ffffff]``, truncated to the configured precision.
BINARY     Lower-case hexadecimal.
========== ==================================================================================

NULL stays NULL and is never equal to an empty string unless ``null_equals_empty`` is set.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from dvf.config.base import parse_duration
from dvf.config.models import NormalizationConfig, ToleranceConfig
from dvf.core.types import CanonicalType, LogicalType
from dvf.sql.dialects import Dialect

if TYPE_CHECKING:
    import polars as pl

UNKNOWN_DECIMAL_SCALE = 10
"""Scale used for decimals whose scale is unknown (for example PostgreSQL ``numeric``)."""

NULL_FIELD = "~"
"""Encoding of a NULL field in the row serialization used for row hashes."""


class Kind(StrEnum):
    """How a column's values are canonicalized and compared."""

    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIME = "time"
    TIMESTAMP = "timestamp"
    BINARY = "binary"


@dataclass(frozen=True)
class StringOptions:
    """String canonicalization settings (from ``normalization``)."""

    trim: str = "none"
    case_sensitive: bool = True
    unicode_form: str = "NFC"
    collapse_whitespace: bool = False
    null_equals_empty: bool = False

    @classmethod
    def from_config(cls, config: NormalizationConfig) -> StringOptions:
        return cls(
            trim=config.trim,
            case_sensitive=config.case_sensitive,
            unicode_form=config.unicode_form,
            collapse_whitespace=config.collapse_whitespace,
            null_equals_empty=config.null_equals_empty,
        )

    def describe(self) -> list[str]:
        steps: list[str] = []
        if self.unicode_form != "none":
            steps.append(f"unicode {self.unicode_form}")
        if self.trim != "none":
            steps.append(f"trim {self.trim}")
        if self.collapse_whitespace:
            steps.append("collapse whitespace")
        if not self.case_sensitive:
            steps.append("lower case")
        if self.null_equals_empty:
            steps.append("empty string as NULL")
        return steps


@dataclass(frozen=True)
class CanonSpec:
    """How one side of a column is canonicalized."""

    kind: Kind
    logical: LogicalType
    """The side's own logical type (after normalizers)."""
    scale: int = 0
    precision: str = "us"
    timezone: str = "UTC"
    tz_aware: bool = False
    strings: StringOptions = field(default_factory=StringOptions)

    @property
    def from_date(self) -> bool:
        return self.logical is LogicalType.DATE

    def describe(self) -> str:
        if self.kind is Kind.TEXT:
            steps = self.strings.describe()
            return "text" + (f" ({', '.join(steps)})" if steps else "")
        if self.kind is Kind.DECIMAL:
            return f"decimal (scale {self.scale})"
        if self.kind is Kind.TIMESTAMP:
            zone = "" if self.tz_aware or self.timezone == "UTC" else f", from {self.timezone}"
            return f"timestamp (UTC, {self.precision}{zone})"
        if self.kind is Kind.TIME:
            return f"time ({self.precision})"
        return self.kind.value


@dataclass(frozen=True)
class NumericCompare:
    """Compare numerically with a tolerance instead of comparing canonical strings."""

    abs_tol: float = 0.0
    rel_tol: float = 0.0
    epoch: bool = False
    """Compare timestamps as microseconds since the epoch (``tolerance.timestamp``)."""

    def describe(self) -> str:
        parts = []
        if self.abs_tol:
            unit = " us" if self.epoch else ""
            parts.append(f"abs {self.abs_tol:g}{unit}")
        if self.rel_tol:
            parts.append(f"rel {self.rel_tol:g}")
        return "numeric" + (f" ({', '.join(parts)})" if parts else "")

    def within(self, source: float | None, target: float | None) -> bool:
        """Whether two numeric values are equal within this tolerance (NULLs equal only NULLs)."""
        if source is None or target is None:
            return source is None and target is None
        allowed = max(self.abs_tol, self.rel_tol * max(abs(source), abs(target)))
        return abs(source - target) <= allowed


@dataclass(frozen=True)
class PairSpec:
    """Canonicalization of both sides of a compared column."""

    source: CanonSpec
    target: CanonSpec
    numeric: NumericCompare | None = None

    @property
    def hashable(self) -> bool:
        """Whether the column takes part in row hashes (tolerance columns are compared apart)."""
        return self.numeric is None

    def describe(self) -> str:
        if self.numeric is not None:
            return self.numeric.describe()
        if self.source == self.target or self.source.describe() == self.target.describe():
            return self.source.describe()
        return f"{self.source.describe()} / {self.target.describe()}"


# ====================================================================== kind resolution

_NUMERIC = (LogicalType.INTEGER, LogicalType.DECIMAL, LogicalType.FLOAT)
_TEMPORAL = (LogicalType.DATE, LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ)


def _own_kind(ctype: CanonicalType) -> Kind:
    logical = ctype.logical
    return {
        LogicalType.INTEGER: Kind.INTEGER,
        LogicalType.DECIMAL: Kind.DECIMAL,
        LogicalType.FLOAT: Kind.FLOAT,
        LogicalType.BOOLEAN: Kind.BOOLEAN,
        LogicalType.DATE: Kind.DATE,
        LogicalType.TIME: Kind.TIME,
        LogicalType.TIMESTAMP: Kind.TIMESTAMP,
        LogicalType.TIMESTAMP_TZ: Kind.TIMESTAMP,
        LogicalType.BINARY: Kind.BINARY,
    }.get(logical, Kind.TEXT)


def _decimal_scale(ctype: CanonicalType) -> int:
    if ctype.logical is LogicalType.INTEGER:
        return 0
    if ctype.logical is LogicalType.DECIMAL and ctype.scale is not None:
        return ctype.scale
    return UNKNOWN_DECIMAL_SCALE


def side_spec(
    ctype: CanonicalType, kind: Kind, config: NormalizationConfig, scale: int = 0
) -> CanonSpec:
    """Build one side's spec for ``kind``."""
    return CanonSpec(
        kind=kind,
        logical=ctype.logical,
        scale=scale,
        precision=config.timestamp_precision,
        timezone=config.timezone,
        tz_aware=ctype.logical is LogicalType.TIMESTAMP_TZ,
        strings=StringOptions.from_config(config),
    )


def resolve_pair(
    source: CanonicalType,
    target: CanonicalType,
    config: NormalizationConfig,
    tolerance: ToleranceConfig | None = None,
) -> PairSpec:
    """Choose how a source and target column are canonicalized so that they are comparable."""
    sl, tl = source.logical, target.logical
    tol = tolerance or ToleranceConfig()

    if sl in _NUMERIC and tl in _NUMERIC:
        if LogicalType.FLOAT in (sl, tl):
            fc = config.float_compare
            numeric = NumericCompare(
                abs_tol=max(tol.numeric_abs or 0.0, fc.abs_tol),
                rel_tol=tol.numeric_rel if tol.numeric_rel is not None else fc.rel_tol,
            )
            return PairSpec(
                side_spec(source, Kind.FLOAT, config),
                side_spec(target, Kind.FLOAT, config),
                numeric,
            )
        if isinstance(config.decimal_scale, int):
            scale = config.decimal_scale
        else:
            scale = max(_decimal_scale(source), _decimal_scale(target))
        kind = Kind.INTEGER if scale == 0 and sl is tl is LogicalType.INTEGER else Kind.DECIMAL
        numeric_tol = None
        if tol.numeric_abs is not None or tol.numeric_rel is not None:
            numeric_tol = NumericCompare(
                abs_tol=tol.numeric_abs or 0.0, rel_tol=tol.numeric_rel or 0.0
            )
        return PairSpec(
            side_spec(source, kind, config, scale),
            side_spec(target, kind, config, scale),
            numeric_tol,
        )

    if sl in _TEMPORAL and tl in _TEMPORAL:
        kind = Kind.DATE if sl is tl is LogicalType.DATE else Kind.TIMESTAMP
        numeric_ts = None
        if tol.timestamp is not None:
            micros = parse_duration(tol.timestamp) / timedelta(microseconds=1)
            numeric_ts = NumericCompare(abs_tol=micros, epoch=True)
        return PairSpec(
            side_spec(source, kind, config), side_spec(target, kind, config), numeric_ts
        )

    return PairSpec(
        side_spec(source, _own_kind(source), config, _decimal_scale(source)),
        side_spec(target, _own_kind(target), config, _decimal_scale(target)),
    )


def single_spec(ctype: CanonicalType, config: NormalizationConfig) -> CanonSpec:
    """Canonicalization of a column on its own (keys of one side, profiles, traces)."""
    return side_spec(ctype, _own_kind(ctype), config, _decimal_scale(ctype))


# ====================================================================== Python reference

_WS_RE = re.compile(r"[ \t\n\r]+")
_SPECIAL_LOWER = {"İ": "i"}
"""Characters whose Python lower-case form is not a single character (see :func:`simple_lower`)."""
_SPECIAL_UPPER = {"ß": "ẞ"}


def simple_lower(text: str) -> str:
    """Lower-case one character at a time, as DuckDB and PostgreSQL do.

    Python's ``str.lower`` applies context rules (a final capital sigma becomes ``ς``) and
    multi-character mappings (``İ`` becomes ``i̇``); databases map each character on its own.
    """
    out = []
    for ch in text:
        low = _SPECIAL_LOWER.get(ch)
        if low is None:
            low = ch.lower()
            if len(low) != 1:
                low = ch
        out.append(low)
    return "".join(out)


def simple_upper(text: str) -> str:
    """Upper-case one character at a time (``ß`` becomes ``ẞ``, as in DuckDB and PostgreSQL)."""
    out = []
    for ch in text:
        up = _SPECIAL_UPPER.get(ch)
        if up is None:
            up = ch.upper()
            if len(up) != 1:
                up = ch
        out.append(up)
    return "".join(out)


def trim_spaces(text: str, mode: str) -> str:
    if mode == "both":
        return text.strip(" ")
    if mode == "rtrim":
        return text.rstrip(" ")
    if mode == "ltrim":
        return text.lstrip(" ")
    return text


def _text(value: Any, opts: StringOptions) -> str | None:
    if isinstance(value, bytes | bytearray | memoryview):
        text = bytes(value).decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if opts.unicode_form != "none":
        text = unicodedata.normalize(opts.unicode_form, text)  # type: ignore[arg-type]
    text = trim_spaces(text, opts.trim)
    if opts.collapse_whitespace:
        text = _WS_RE.sub(" ", text)
    if not opts.case_sensitive:
        text = simple_lower(text)
    if opts.null_equals_empty and text == "":
        return None
    return text


def to_decimal(value: Any) -> Decimal:
    """Convert a database value to :class:`~decimal.Decimal` (floats via their shortest repr)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, bytes | bytearray):
        value = value.decode()
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def decimal_text(value: Any, scale: int) -> str:
    quantum = Decimal(1).scaleb(-scale)
    rounded = to_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    return format(rounded, "f")


def _integer(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return decimal_text(value, 0)


def _boolean(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float | Decimal):
        return "1" if value else "0"
    text = str(value).strip().lower()
    if text in ("1", "true", "t", "yes", "y"):
        return "1"
    if text in ("0", "false", "f", "no", "n"):
        return "0"
    return None


def parse_datetime(value: Any) -> datetime:
    """Parse a timestamp from a database value (datetime, date, or ISO-like string)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, timedelta):
        total = int(value / timedelta(microseconds=1))
        seconds, micros = divmod(total, 1_000_000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return time(hours % 24, minute, sec, micros)
    return time.fromisoformat(str(value).strip())


_TRUNC = {"s": 1_000_000, "ms": 1_000, "us": 1, "ns": 1}
_TS_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}


def to_utc_naive(value: Any, timezone: str) -> datetime:
    """Convert to a naive UTC datetime; naive inputs are assumed to be in ``timezone``."""
    dt = parse_datetime(value)
    if dt.tzinfo is None:
        if timezone == "UTC":
            return dt
        dt = dt.replace(tzinfo=ZoneInfo(timezone))
    return dt.astimezone(UTC).replace(tzinfo=None)


def _timestamp(value: Any, spec: CanonSpec) -> str:
    dt = to_utc_naive(value, spec.timezone)
    step = _TRUNC[spec.precision]
    dt = dt.replace(microsecond=dt.microsecond - dt.microsecond % step)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond:06d}"
    return text[: _TS_WIDTH[spec.precision]]


def _time(value: Any, precision: str) -> str:
    t = parse_time(value)
    step = _TRUNC[precision]
    micros = t.microsecond - t.microsecond % step
    text = f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}.{micros:06d}"
    return text[: _TIME_WIDTH[precision]]


def _binary(value: Any) -> str:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return str(value).encode("utf-8").hex()


def canonical_text(value: Any, spec: CanonSpec) -> str | None:
    """The reference canonical string of ``value`` (``None`` for NULL)."""
    if value is None:
        return None
    kind = spec.kind
    if kind is Kind.TEXT:
        return _text(value, spec.strings)
    if kind is Kind.INTEGER:
        return _integer(value)
    if kind is Kind.DECIMAL:
        return decimal_text(value, spec.scale)
    if kind is Kind.FLOAT:
        return repr(float(value))
    if kind is Kind.BOOLEAN:
        return _boolean(value)
    if kind is Kind.DATE:
        return parse_date(value).isoformat()
    if kind is Kind.TIMESTAMP:
        return _timestamp(value, spec)
    if kind is Kind.TIME:
        return _time(value, spec.precision)
    return _binary(value)


def numeric_value(value: Any, spec: CanonSpec, compare: NumericCompare) -> float | None:
    """The reference numeric value used for tolerance comparisons."""
    if value is None:
        return None
    if compare.epoch:
        dt = to_utc_naive(value, spec.timezone)
        return float((dt - datetime(1970, 1, 1)) // timedelta(microseconds=1))
    if isinstance(value, bool):
        return float(value)
    return float(to_decimal(value)) if not isinstance(value, float) else value


def encode_field(text: str | None) -> str:
    """Length-prefixed field encoding, so ``("ab", "c")`` never equals ``("a", "bc")``."""
    return NULL_FIELD if text is None else f"{len(text)}:{text}"


def row_hash(fields: Sequence[str | None]) -> str:
    """The reference row hash: MD5 of the length-prefixed canonical fields joined with ``|``."""
    serialized = "|".join(encode_field(f) for f in fields)
    return hashlib.md5(serialized.encode("utf-8"), usedforsecurity=False).hexdigest()


# ====================================================================== SQL


def canonical_sql(expr: str, spec: CanonSpec, dialect: Dialect) -> str:
    """SQL producing the canonical string of ``expr`` in ``dialect``."""
    kind = spec.kind
    if kind is Kind.TEXT:
        out = expr if spec.logical is LogicalType.STRING else dialect.to_text(expr)
        opts = spec.strings
        if opts.unicode_form != "none":
            out = dialect.unicode_normalize(out, opts.unicode_form)
        if opts.trim != "none":
            out = dialect.trim(out, opts.trim)
        if opts.collapse_whitespace:
            out = dialect.collapse_whitespace(out)
        if not opts.case_sensitive:
            out = dialect.lower(out)
        if opts.null_equals_empty:
            out = f"NULLIF({out}, '')"
        return out
    if kind is Kind.INTEGER:
        return dialect.integer_text(expr)
    if kind is Kind.DECIMAL:
        return dialect.decimal_text(expr, spec.scale)
    if kind is Kind.FLOAT:
        return dialect.to_text(dialect.to_double(expr))
    if kind is Kind.BOOLEAN:
        return dialect.boolean_text(expr)
    if kind is Kind.DATE:
        return dialect.date_text(expr)
    if kind is Kind.TIMESTAMP:
        return dialect.timestamp_text(
            expr,
            precision=spec.precision,
            tz_aware=spec.tz_aware,
            timezone=spec.timezone,
            from_date=spec.from_date,
        )
    if kind is Kind.TIME:
        return dialect.time_text(expr, precision=spec.precision)
    return dialect.binary_hex(expr)


def numeric_sql(expr: str, spec: CanonSpec, compare: NumericCompare, dialect: Dialect) -> str:
    """SQL producing the numeric value of ``expr`` for tolerance comparisons."""
    if compare.epoch:
        return dialect.epoch_us(expr, tz_aware=spec.tz_aware, timezone=spec.timezone)
    return dialect.to_double(expr)


def field_sql(text_expr: str, dialect: Dialect) -> str:
    """SQL for :func:`encode_field`."""
    length = dialect.to_text(dialect.length(text_expr))
    encoded = dialect.concat(length, "':'", text_expr)
    return f"(CASE WHEN {text_expr} IS NULL THEN {dialect.literal(NULL_FIELD)} ELSE {encoded} END)"


def row_hash_sql(text_exprs: Sequence[str], dialect: Dialect) -> str:
    """SQL for :func:`row_hash` over already-canonical expressions."""
    if not text_exprs:
        return dialect.md5("''")
    parts: list[str] = []
    for i, expr in enumerate(text_exprs):
        if i:
            parts.append("'|'")
        parts.append(field_sql(expr, dialect))
    return dialect.md5(dialect.concat(*parts))


# ====================================================================== Polars


def _python_map(expr: pl.Expr, func: Callable[[Any], Any], dtype: Any) -> pl.Expr:
    """Apply a Python function element-wise (used where Polars has no exact native equivalent)."""
    import polars as pl

    def apply(series: pl.Series) -> pl.Series:
        return pl.Series(series.name, [func(v) for v in series.to_list()], dtype=dtype)

    return expr.map_batches(apply, return_dtype=dtype)


def polars_lower(expr: pl.Expr) -> pl.Expr:
    """Per-character lower case matching :func:`simple_lower`."""
    for special, plain in ((chr(0x03A3), chr(0x03C3)), (chr(0x0130), "i")):
        expr = expr.str.replace_all(special, plain, literal=True)
    return expr.str.to_lowercase()


def canonical_polars(expr: pl.Expr, spec: CanonSpec, dtype: pl.DataType) -> pl.Expr:
    """A Polars expression producing the canonical string of ``expr`` (of type ``dtype``)."""
    import polars as pl

    kind = spec.kind
    if kind is Kind.TEXT:
        if dtype == pl.String:
            out = expr
        elif dtype in (pl.Binary,) or isinstance(dtype, pl.Decimal | pl.Datetime | pl.Date):
            return _python_map(expr, lambda v: canonical_text(v, spec), pl.String)
        else:
            out = expr.cast(pl.String)
        opts = spec.strings
        if opts.unicode_form != "none":
            out = out.str.normalize(opts.unicode_form)  # type: ignore[arg-type]
        if opts.trim == "both":
            out = out.str.strip_chars(" ")
        elif opts.trim == "rtrim":
            out = out.str.strip_chars_end(" ")
        elif opts.trim == "ltrim":
            out = out.str.strip_chars_start(" ")
        if opts.collapse_whitespace:
            out = out.str.replace_all(_WS_RE.pattern, " ")
        if not opts.case_sensitive:
            out = polars_lower(out)
        if opts.null_equals_empty:
            out = pl.when(out == "").then(None).otherwise(out)
        return out
    if kind is Kind.INTEGER and dtype.is_integer():
        return expr.cast(pl.String)
    if kind is Kind.BOOLEAN and dtype == pl.Boolean:
        return pl.when(expr).then(pl.lit("1")).when(~expr).then(pl.lit("0")).otherwise(None)
    if kind is Kind.DATE and isinstance(dtype, pl.Date | pl.Datetime):
        date_expr = expr if isinstance(dtype, pl.Date) else expr.dt.date()
        return date_expr.dt.strftime("%Y-%m-%d")
    if kind is Kind.TIMESTAMP and isinstance(dtype, pl.Date | pl.Datetime):
        aware = isinstance(dtype, pl.Datetime) and dtype.time_zone is not None
        if not aware and spec.timezone != "UTC":
            return _python_map(expr, lambda v: canonical_text(v, spec), pl.String)
        ts = expr.cast(pl.Datetime("us")) if isinstance(dtype, pl.Date) else expr
        if aware:
            ts = ts.dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        ts = ts.cast(pl.Datetime("us"))
        unit = {"s": "1s", "ms": "1ms"}.get(spec.precision)
        if unit is not None:
            ts = ts.dt.truncate(unit)
        return ts.dt.strftime("%Y-%m-%dT%H:%M:%S%.6f").str.slice(0, _TS_WIDTH[spec.precision])
    if kind is Kind.BINARY and dtype == pl.Binary:
        return expr.bin.encode("hex")
    return _python_map(expr, lambda v: canonical_text(v, spec), pl.String)


def numeric_polars(
    expr: pl.Expr, spec: CanonSpec, compare: NumericCompare, dtype: pl.DataType
) -> pl.Expr:
    """A Polars expression producing the numeric value used for tolerance comparisons."""
    import polars as pl

    if not compare.epoch and dtype.is_numeric():
        return expr.cast(pl.Float64)
    return _python_map(expr, lambda v: numeric_value(v, spec, compare), pl.Float64)


def polars_row_hash(exprs: Sequence[pl.Expr]) -> pl.Expr:
    """A Polars expression for :func:`row_hash` over already-canonical string expressions."""
    import polars as pl

    if not exprs:
        return pl.lit(row_hash([]))
    encoded = [
        pl.when(e.is_null())
        .then(pl.lit(NULL_FIELD))
        .otherwise(pl.concat_str([e.str.len_chars().cast(pl.String), pl.lit(":"), e]))
        for e in exprs
    ]
    serialized = pl.concat_str(encoded, separator="|")

    def md5(series: pl.Series) -> pl.Series:
        return pl.Series(
            series.name,
            [
                hashlib.md5(v.encode("utf-8"), usedforsecurity=False).hexdigest()
                for v in series.to_list()
            ],
            dtype=pl.String,
        )

    return serialized.map_batches(md5, return_dtype=pl.String)


def with_strings(spec: CanonSpec, **changes: Any) -> CanonSpec:
    """A copy of ``spec`` with different string options (used by traces and tests)."""
    return replace(spec, strings=replace(spec.strings, **changes))
