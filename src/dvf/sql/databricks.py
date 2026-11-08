"""Databricks SQL dialect (beta).

Implemented from the Databricks SQL language reference and verified by parsing with sqlglot; it
has not been run against a live workspace. Canonical forms follow
:mod:`dvf.normalize.canonical`:

* Identifiers are quoted with backticks. String literals escape quotes and backslashes with a
  backslash: Spark concatenates adjacent literals, so ``'it''s'`` would silently read ``its``.
* ``TIMESTAMP`` is an instant rendered in the session time zone; the connector sets it to UTC
  (``SET TIME ZONE 'UTC'``), and timestamp canonicalization relies on that. ``TIMESTAMP_NTZ``
  is a wall-clock time. ``convert_timezone`` needs Databricks Runtime 13.1 or later.
* Regular expressions use Java syntax. ``RLIKE`` finds a match anywhere; full matches wrap the
  pattern in ``^(?:...)\\z`` because Java's ``$`` also matches before a final line break.
* Decimal-to-string casts are plain (no exponent) since Spark 3.3, and casts round half up.
* There is no Unicode normalization function, so canonicalization skips it and the plan notes
  say so (:meth:`DatabricksDialect.supports_unicode_form` is false).
* Databricks has no ``TIME`` type; time canonicalization parses the value's text.

Known differences from the Python reference: ``lower``/``upper`` use Java's full case mappings;
dvf pre-maps capital sigma, dotted capital I, and sharp s, but other characters whose upper case
is several characters (ligatures such as U+FB01) can still differ. ``SUM`` of 32-bit hash
integers overflows ``BIGINT`` only beyond about two billion rows in one bucket.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from dvf.sql.dialects import WHITESPACE_CLASS, Dialect, UnsupportedFeatureError

_TS_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}
_TRIM_SIDES = {"both": "BOTH", "ltrim": "LEADING", "rtrim": "TRAILING"}

CAPITAL_SIGMA, SMALL_SIGMA = chr(0x03A3), chr(0x03C3)
DOTTED_CAPITAL_I = chr(0x0130)
SHARP_S, CAPITAL_SHARP_S = chr(0x00DF), chr(0x1E9E)

_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class DatabricksDialect(Dialect):
    name = "databricks"
    text_type = "STRING"
    double_type = "DOUBLE"

    # ------------------------------------------------------------------ basics
    def quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def literal(self, value: Any) -> str:
        if value is None or isinstance(value, bool | int | float | Decimal):
            return super().literal(value)
        if isinstance(value, datetime | date | time):
            value = value.isoformat()
        return "'" + "".join(_ESCAPES.get(ch, ch) for ch in str(value)) + "'"

    def concat(self, *exprs: str) -> str:
        return f"CONCAT({', '.join(exprs)})"

    # MD5 of a STRING hashes its UTF-8 bytes and returns lower-case hex (base implementation).

    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"CAST(CONV(SUBSTR({hex_expr}, {int(offset) + 1}, 8), 16, 10) AS BIGINT)"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"(({numerator}) DIV {int(denominator)})"

    # ------------------------------------------------------------------ strings
    def lower(self, expr: str) -> str:
        sigma = f"REPLACE({expr}, {self.literal(CAPITAL_SIGMA)}, {self.literal(SMALL_SIGMA)})"
        return f"LOWER(REPLACE({sigma}, {self.literal(DOTTED_CAPITAL_I)}, 'i'))"

    def upper(self, expr: str) -> str:
        return f"UPPER(REPLACE({expr}, {self.literal(SHARP_S)}, {self.literal(CAPITAL_SHARP_S)}))"

    def trim(self, expr: str, mode: str = "both", chars: str = " ") -> str:
        return f"TRIM({_TRIM_SIDES[mode]} {self.literal(chars)} FROM {expr})"

    def supports_unicode_form(self, form: str) -> bool:
        return False

    def unicode_normalize(self, expr: str, form: str) -> str:
        raise UnsupportedFeatureError(
            f"Databricks SQL has no Unicode normalization function, so {form} is not "
            "available; set normalization.unicode_form to none or drop the unicode_nfc normalizer"
        )

    def collapse_whitespace(self, expr: str) -> str:
        return f"REGEXP_REPLACE({expr}, {self.literal(WHITESPACE_CLASS)}, ' ')"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        if full:
            pattern = f"^(?:{pattern})\\z"
        return f"({expr} RLIKE {self.literal(pattern)})"

    # ------------------------------------------------------------------ canonical forms
    def round_decimal(self, expr: str, digits: int) -> str:
        d = int(digits)
        return f"CAST(ROUND({expr}, {d}) AS DECIMAL(38, {d}))"

    def decimal_text(self, expr: str, scale: int) -> str:
        s = int(scale)
        return f"CAST(CAST(ROUND({expr}, {s}) AS DECIMAL(38, {s})) AS STRING)"

    def date_text(self, expr: str) -> str:
        return f"DATE_FORMAT(CAST({expr} AS DATE), 'yyyy-MM-dd')"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware or timezone == "UTC":
            # TIMESTAMP -> TIMESTAMP_NTZ uses the session time zone, which the connector sets to UTC.
            return f"CAST({expr} AS TIMESTAMP_NTZ)"
        return f"CONVERT_TIMEZONE({self.literal(timezone)}, 'UTC', CAST({expr} AS TIMESTAMP_NTZ))"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        text = f"REPLACE(DATE_FORMAT({utc}, 'yyyy-MM-dd HH:mm:ss.SSSSSS'), ' ', 'T')"
        return f"LEFT({text}, {_TS_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        ts = f"CAST(CONCAT('1970-01-01 ', CAST({expr} AS STRING)) AS TIMESTAMP_NTZ)"
        return f"LEFT(DATE_FORMAT({ts}, 'HH:mm:ss.SSSSSS'), {_TIME_WIDTH[precision]})"

    def binary_hex(self, expr: str) -> str:
        return f"LOWER(HEX({expr}))"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"CAST(UNIX_MICROS({expr}) AS DOUBLE)"
        utc = self.to_utc(expr, tz_aware=False, timezone=timezone)
        return f"CAST(UNIX_MICROS(CAST({utc} AS TIMESTAMP)) AS DOUBLE)"

    def current_timestamp_utc(self) -> str:
        return "CAST(CURRENT_TIMESTAMP() AS TIMESTAMP_NTZ)"


DIALECT = DatabricksDialect()
