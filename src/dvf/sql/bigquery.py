"""Google BigQuery (GoogleSQL) dialect (beta).

Implemented from the GoogleSQL reference and verified by parsing with sqlglot; it has not been
run against a live project. Canonical forms follow :mod:`dvf.normalize.canonical`:

* Identifiers are quoted with backticks. String literals escape quotes and backslashes with a
  backslash (``''`` is not an escape in GoogleSQL) and may not contain raw line breaks.
* ``MD5`` returns BYTES, so the digest is ``TO_HEX(MD5(x))``.
* Decimals are rounded as ``BIGNUMERIC`` (half away from zero) and padded to the fixed scale,
  because ``CAST(NUMERIC AS STRING)`` drops trailing zeros.
* ``TIMESTAMP`` is an absolute instant and ``DATETIME`` is a wall-clock time; years are rendered
  with ``%E4Y`` so years before 1000 keep their leading zeros.
* Regular expressions use RE2: partial matches use ``REGEXP_CONTAINS``, full matches wrap the
  pattern in ``^(?:...)$``.

Known differences from the Python reference: ``LOWER``/``UPPER`` use full Unicode case mappings;
dvf pre-maps capital sigma, dotted capital I, and sharp s, but other characters whose upper case
is several characters (ligatures such as U+FB01) can still differ. ``SUM`` of 32-bit hash
integers overflows ``INT64`` only beyond about two billion rows in one bucket.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from dvf.sql.dialects import WHITESPACE_CLASS, Dialect, UnsupportedFeatureError

_TS_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}
_UNICODE_FORMS = frozenset({"NFC", "NFD", "NFKC", "NFKD"})
_TRUNC_UNITS = {
    "millisecond": "MILLISECOND",
    "second": "SECOND",
    "minute": "MINUTE",
    "hour": "HOUR",
    "day": "DAY",
}
_TS_FORMAT = "'%E4Y-%m-%dT%H:%M:%E6S'"

CAPITAL_SIGMA, SMALL_SIGMA = chr(0x03A3), chr(0x03C3)
DOTTED_CAPITAL_I = chr(0x0130)
SHARP_S, CAPITAL_SHARP_S = chr(0x00DF), chr(0x1E9E)

_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(text: str) -> str:
    out = []
    for ch in text:
        escaped = _ESCAPES.get(ch)
        if escaped is None and ord(ch) < 0x20:
            escaped = f"\\x{ord(ch):02x}"
        out.append(escaped if escaped is not None else ch)
    return "".join(out)


class BigQueryDialect(Dialect):
    name = "bigquery"
    text_type = "STRING"
    double_type = "FLOAT64"

    # ------------------------------------------------------------------ basics
    def quote(self, identifier: str) -> str:
        return "`" + identifier.replace("\\", "\\\\").replace("`", "\\`") + "`"

    def literal(self, value: Any) -> str:
        if value is None or isinstance(value, bool | int | float | Decimal):
            return super().literal(value)
        if isinstance(value, datetime | date | time):
            value = value.isoformat()
        return f"'{_escape(str(value))}'"

    def concat(self, *exprs: str) -> str:
        return f"CONCAT({', '.join(exprs)})"

    def md5(self, expr: str) -> str:
        return f"TO_HEX(MD5({expr}))"

    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"CAST(CONCAT('0x', SUBSTR({hex_expr}, {int(offset) + 1}, 8)) AS INT64)"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"DIV({numerator}, {int(denominator)})"

    # ------------------------------------------------------------------ strings
    def lower(self, expr: str) -> str:
        sigma = f"REPLACE({expr}, {self.literal(CAPITAL_SIGMA)}, {self.literal(SMALL_SIGMA)})"
        return f"LOWER(REPLACE({sigma}, {self.literal(DOTTED_CAPITAL_I)}, 'i'))"

    def upper(self, expr: str) -> str:
        return f"UPPER(REPLACE({expr}, {self.literal(SHARP_S)}, {self.literal(CAPITAL_SHARP_S)}))"

    def supports_unicode_form(self, form: str) -> bool:
        return form in _UNICODE_FORMS

    def unicode_normalize(self, expr: str, form: str) -> str:
        if form not in _UNICODE_FORMS:
            raise UnsupportedFeatureError(f"unknown Unicode normalization form '{form}'")
        return f"NORMALIZE({expr}, {form})"

    def collapse_whitespace(self, expr: str) -> str:
        return f"REGEXP_REPLACE({expr}, {self.literal(WHITESPACE_CLASS)}, ' ')"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        if full:
            pattern = f"^(?:{pattern})$"
        return f"REGEXP_CONTAINS({expr}, {self.literal(pattern)})"

    # ------------------------------------------------------------------ canonical forms
    def round_decimal(self, expr: str, digits: int) -> str:
        return f"ROUND(CAST({expr} AS BIGNUMERIC), {int(digits)})"

    def decimal_text(self, expr: str, scale: int) -> str:
        s = int(scale)
        text = f"CAST(ROUND(CAST({expr} AS BIGNUMERIC), {s}) AS STRING)"
        if s == 0:
            return text
        # The cast drops trailing zeros ("1.5", "12"): add the point if missing, then pad.
        with_point = f"IF(STRPOS({text}, '.') = 0, CONCAT({text}, '.'), {text})"
        return f"RPAD({with_point}, STRPOS(CONCAT({text}, '.'), '.') + {s}, '0')"

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        return f"TIMESTAMP_TRUNC({expr}, {_TRUNC_UNITS[unit]})"

    def date_text(self, expr: str) -> str:
        return f"FORMAT_DATE('%E4Y-%m-%d', CAST({expr} AS DATE))"

    def _instant(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        """A TIMESTAMP (absolute instant) for ``expr``."""
        if tz_aware:
            return expr
        if timezone == "UTC":
            return f"TIMESTAMP(CAST({expr} AS DATETIME))"
        return f"TIMESTAMP(CAST({expr} AS DATETIME), {self.literal(timezone)})"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"DATETIME({expr}, 'UTC')"
        if timezone == "UTC":
            return f"CAST({expr} AS DATETIME)"
        return f"DATETIME({self._instant(expr, tz_aware=False, timezone=timezone)}, 'UTC')"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        if not tz_aware and timezone == "UTC":
            text = f"FORMAT_DATETIME({_TS_FORMAT}, CAST({expr} AS DATETIME))"
        else:
            instant = self._instant(expr, tz_aware=tz_aware, timezone=timezone)
            text = f"FORMAT_TIMESTAMP({_TS_FORMAT}, {instant}, 'UTC')"
        return f"LEFT({text}, {_TS_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        text = f"FORMAT_TIME('%H:%M:%E6S', CAST({expr} AS TIME))"
        return f"LEFT({text}, {_TIME_WIDTH[precision]})"

    def binary_hex(self, expr: str) -> str:
        return f"TO_HEX({expr})"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        instant = self._instant(expr, tz_aware=tz_aware, timezone=timezone)
        return f"CAST(UNIX_MICROS({instant}) AS FLOAT64)"

    def current_timestamp_utc(self) -> str:
        return "CURRENT_DATETIME('UTC')"


DIALECT = BigQueryDialect()
