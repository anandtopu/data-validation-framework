"""Snowflake SQL dialect (beta).

Implemented from Snowflake's SQL reference and verified by parsing with sqlglot; it has not been
run against a live account. Canonical forms follow :mod:`dvf.normalize.canonical`:

* String constants treat the backslash as an escape character, so literals double it.
* ``REGEXP_LIKE`` is implicitly anchored at both ends (full match); partial matches use
  ``REGEXP_INSTR(...) > 0``. Patterns are POSIX ERE with Perl extensions, not Python syntax.
* Snowflake has no Unicode normalization function, so canonicalization skips it and the plan
  notes say so (:meth:`SnowflakeDialect.supports_unicode_form` is false).
* Integer division uses ``MOD`` because ``/`` on integers yields a decimal with six fractional
  digits: ``FLOOR(n / d)`` would round up quotients within 5e-7 of the next integer.
* Timestamps are rendered with nine fractional digits (Snowflake's maximum) and then cut to the
  configured precision, which truncates exactly like the Python reference.

Known differences from the Python reference: ``LOWER``/``UPPER`` may apply full Unicode case
mappings; dvf pre-maps capital sigma, dotted capital I, and sharp s, but other characters whose
upper case is several characters (ligatures such as U+FB01) can still differ. Ambiguous or
nonexistent local times around DST transitions follow ``CONVERT_TIMEZONE``, not Python's
``fold=0``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from dvf.sql.dialects import WHITESPACE_CLASS, Dialect, UnsupportedFeatureError

_TS_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}

CAPITAL_SIGMA, SMALL_SIGMA = chr(0x03A3), chr(0x03C3)
DOTTED_CAPITAL_I = chr(0x0130)
SHARP_S, CAPITAL_SHARP_S = chr(0x00DF), chr(0x1E9E)


class SnowflakeDialect(Dialect):
    name = "snowflake"
    text_type = "VARCHAR"
    double_type = "DOUBLE"

    # ------------------------------------------------------------------ basics
    def literal(self, value: Any) -> str:
        if value is None or isinstance(value, bool | int | float | Decimal):
            return super().literal(value)
        if isinstance(value, datetime | date | time):
            value = value.isoformat()
        text = str(value).replace("\\", "\\\\").replace("'", "''")
        return f"'{text}'"

    def concat(self, *exprs: str) -> str:
        return f"CONCAT({', '.join(exprs)})"

    # MD5 returns the lower-case hexadecimal digest of the UTF-8 bytes (base implementation).

    def hash_int(self, hex_expr: str, offset: int) -> str:
        # Format-model parsing is case-insensitive, so lower-case digests parse with 'XXXXXXXX'.
        return f"TO_NUMBER(SUBSTR({hex_expr}, {int(offset) + 1}, 8), 'XXXXXXXX')"

    def int_div(self, numerator: str, denominator: int) -> str:
        d = int(denominator)
        return f"CAST((({numerator}) - MOD(({numerator}), {d})) / {d} AS BIGINT)"

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
            f"Snowflake has no Unicode normalization function, so {form} is not available; "
            "set normalization.unicode_form to none or drop the unicode_nfc normalizer"
        )

    def collapse_whitespace(self, expr: str) -> str:
        return f"REGEXP_REPLACE({expr}, {self.literal(WHITESPACE_CLASS)}, ' ')"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        if full:
            # REGEXP_LIKE anchors the whole pattern; the group keeps alternations anchored too.
            return f"REGEXP_LIKE({expr}, {self.literal('(' + pattern + ')')})"
        return f"(REGEXP_INSTR({expr}, {self.literal(pattern)}) > 0)"

    # ------------------------------------------------------------------ canonical forms
    def round_decimal(self, expr: str, digits: int) -> str:
        d = int(digits)
        return f"CAST(ROUND({expr}, {d}) AS NUMBER(38, {d}))"

    def decimal_text(self, expr: str, scale: int) -> str:
        # ROUND is half away from zero; TO_VARCHAR keeps the scale's trailing zeros.
        s = int(scale)
        return f"TO_VARCHAR(CAST(ROUND({expr}, {s}) AS NUMBER(38, {s})))"

    def date_text(self, expr: str) -> str:
        return f"TO_VARCHAR(CAST({expr} AS DATE), 'YYYY-MM-DD')"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"CAST(CONVERT_TIMEZONE('UTC', {expr}) AS TIMESTAMP_NTZ)"
        if timezone == "UTC":
            return f"CAST({expr} AS TIMESTAMP_NTZ)"
        return f"CONVERT_TIMEZONE({self.literal(timezone)}, 'UTC', CAST({expr} AS TIMESTAMP_NTZ))"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        text = f"REPLACE(TO_VARCHAR({utc}, 'YYYY-MM-DD HH24:MI:SS.FF9'), ' ', 'T')"
        return f"LEFT({text}, {_TS_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        text = f"TO_VARCHAR(CAST({expr} AS TIME), 'HH24:MI:SS.FF9')"
        return f"LEFT({text}, {_TIME_WIDTH[precision]})"

    def binary_hex(self, expr: str) -> str:
        return f"HEX_ENCODE({expr}, 0)"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        return f"CAST(DATE_PART(EPOCH_MICROSECOND, {utc}) AS DOUBLE)"

    def current_timestamp_utc(self) -> str:
        return "SYSDATE()"


DIALECT = SnowflakeDialect()
