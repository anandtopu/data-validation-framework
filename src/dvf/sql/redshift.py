"""Amazon Redshift dialect (beta).

Implemented from the Amazon Redshift SQL reference and verified by parsing with sqlglot; it has
not been run against a live cluster. Redshift descends from PostgreSQL 8, so it lacks several
functions the PostgreSQL dialect uses:

* ``VARCHAR`` without a length means ``VARCHAR(256)``, so text casts use ``VARCHAR(MAX)``.
* ``CONCAT`` takes exactly two arguments; rows are serialized with ``||``.
* Hexadecimal digits become integers with ``STRTOL(..., 16)``; integer division truncates.
* There is no Unicode normalization function, so canonicalization skips it and the plan notes
  say so (:meth:`RedshiftDialect.supports_unicode_form` is false).
* ``IS DISTINCT FROM`` is not documented for Redshift, so a ``CASE`` expression replaces it.
* String constants treat the backslash as an escape character, so literals double it.
* ``TO_CHAR`` has no format for a bare ``TIME``; times are formatted after adding them to a date.

Known limits: ``VARCHAR`` holds at most 65,535 bytes, so rows whose serialized canonical values
exceed that cannot be hashed in Redshift. ``LOWER``/``UPPER`` behavior on characters with
special case mappings is not documented; dvf pre-maps capital sigma, dotted capital I, and sharp
s. Ambiguous or nonexistent local times around DST transitions follow ``CONVERT_TIMEZONE``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, ClassVar

from dvf.sql.dialects import WHITESPACE_CLASS, Dialect, UnsupportedFeatureError

_TS_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}

CAPITAL_SIGMA, SMALL_SIGMA = chr(0x03A3), chr(0x03C3)
DOTTED_CAPITAL_I = chr(0x0130)
SHARP_S, CAPITAL_SHARP_S = chr(0x00DF), chr(0x1E9E)


class RedshiftDialect(Dialect):
    name = "redshift"
    text_type = "VARCHAR(MAX)"
    double_type = "DOUBLE PRECISION"
    trim_functions: ClassVar[dict[str, str]] = {"both": "BTRIM", "rtrim": "RTRIM", "ltrim": "LTRIM"}

    # ------------------------------------------------------------------ basics
    def literal(self, value: Any) -> str:
        if value is None or isinstance(value, bool | int | float | Decimal):
            return super().literal(value)
        if isinstance(value, datetime | date | time):
            value = value.isoformat()
        text = str(value).replace("\\", "\\\\").replace("'", "''")
        return f"'{text}'"

    def is_distinct(self, left: str, right: str) -> str:
        return (
            f"(CASE WHEN {left} = {right} OR ({left} IS NULL AND {right} IS NULL) "
            "THEN FALSE ELSE TRUE END)"
        )

    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"STRTOL(SUBSTRING({hex_expr}, {int(offset) + 1}, 8), 16)"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"(CAST({numerator} AS BIGINT) / {int(denominator)})"

    # ------------------------------------------------------------------ strings
    def lower(self, expr: str) -> str:
        sigma = f"REPLACE({expr}, {self.literal(CAPITAL_SIGMA)}, {self.literal(SMALL_SIGMA)})"
        return f"LOWER(REPLACE({sigma}, {self.literal(DOTTED_CAPITAL_I)}, 'i'))"

    def upper(self, expr: str) -> str:
        return f"UPPER(REPLACE({expr}, {self.literal(SHARP_S)}, {self.literal(CAPITAL_SHARP_S)}))"

    def supports_unicode_form(self, form: str) -> bool:
        return False

    def unicode_normalize(self, expr: str, form: str) -> str:
        raise UnsupportedFeatureError(
            f"Amazon Redshift has no Unicode normalization function, so {form} is not "
            "available; set normalization.unicode_form to none or drop the unicode_nfc normalizer"
        )

    def collapse_whitespace(self, expr: str) -> str:
        return f"REGEXP_REPLACE({expr}, {self.literal(WHITESPACE_CLASS)}, ' ')"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        if full:
            pattern = f"^({pattern})$"
        return f"({expr} ~ {self.literal(pattern)})"

    # ------------------------------------------------------------------ canonical forms
    def round_decimal(self, expr: str, digits: int) -> str:
        d = int(digits)
        return f"CAST(ROUND({expr}, {d}) AS DECIMAL(38, {d}))"

    def decimal_text(self, expr: str, scale: int) -> str:
        # ROUND keeps the input's type, so cast to the target scale afterwards (exactly).
        s = int(scale)
        return f"CAST(CAST(ROUND({expr}, {s}) AS DECIMAL(38, {s})) AS {self.text_type})"

    def date_text(self, expr: str) -> str:
        return f"TO_CHAR(CAST({expr} AS DATE), 'YYYY-MM-DD')"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"({expr} AT TIME ZONE 'UTC')"
        if timezone == "UTC":
            return f"CAST({expr} AS TIMESTAMP)"
        return f"CONVERT_TIMEZONE({self.literal(timezone)}, 'UTC', CAST({expr} AS TIMESTAMP))"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        text = f"REPLACE(TO_CHAR({utc}, 'YYYY-MM-DD HH24:MI:SS.US'), ' ', 'T')"
        return f"LEFT({text}, {_TS_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        ts = f"CAST('1970-01-01' AS DATE) + CAST({expr} AS TIME)"
        return f"LEFT(TO_CHAR({ts}, 'HH24:MI:SS.US'), {_TIME_WIDTH[precision]})"

    def binary_hex(self, expr: str) -> str:
        return f"LOWER(TO_HEX({expr}))"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        epoch = "CAST('1970-01-01 00:00:00' AS TIMESTAMP)"
        return f"CAST(DATEDIFF(microsecond, {epoch}, {utc}) AS DOUBLE PRECISION)"

    def current_timestamp_utc(self) -> str:
        # The connector sets the session time zone to UTC.
        return "GETDATE()"


DIALECT = RedshiftDialect()
