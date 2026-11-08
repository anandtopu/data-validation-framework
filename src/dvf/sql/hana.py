"""SAP HANA SQL dialect (beta; HANA 2.0 SPS 05 or later, and HANA Cloud).

Implemented from the SAP HANA SQL reference. sqlglot has no HANA dialect, so the SQL is checked
by unit tests of its shape, not by parsing, and it has not been run against a live HANA system.
Canonical forms follow :mod:`dvf.normalize.canonical`:

* Identifiers are double-quoted and case-sensitive; unquoted names are upper-cased by HANA, which
  suits SAP tables. String literals containing non-ASCII characters are written as ``N'...'``.
* Predicates cannot appear in a SELECT list, so :meth:`HanaDialect.boolean` wraps them in
  ``CASE``, and booleans are tested with ``= TRUE``.
* ``TRIM`` removes a single character only; trimming character sets uses ``LTRIM``/``RTRIM``
  with a removal set.
* ``HASH_MD5`` hashes binary values. Text is converted with ``TO_BINARY``, which uses HANA's
  internal CESU-8 encoding: it equals UTF-8 for every character of the Basic Multilingual Plane,
  so row hashes of text with characters beyond U+FFFF (emoji, some CJK extensions) differ from
  other databases' hashes of the same text.
* There is no Unicode normalization function: canonicalization skips it and says so
  (:meth:`HanaDialect.supports_unicode_form` is false).
* Regular expressions use ``LIKE_REGEXPR`` (PCRE). Full matches wrap the pattern in
  ``^(?:...)\\z``.
* ``TIME`` has whole seconds only, and there is no ``TIMESTAMP WITH TIME ZONE`` type: time zone
  conversions use ``LOCALTOUTC``, which needs HANA's time zone tables.
* ``SELECT`` without ``FROM`` needs ``FROM DUMMY``; the connector's connection test uses it.

SAP data on HANA stores ABAP ``DATS``, ``TIMS``, ``NUMC``, and flags as ``NVARCHAR``; use the SAP
normalizers (``sap_dats``, ``sap_tims``, ``sap_numc``, ``sap_bool``) to compare them with typed
columns.
"""

from __future__ import annotations

from typing import Any

from dvf.sql.dialects import WHITESPACE_CLASS, Dialect, UnsupportedFeatureError

_TS_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}
_TRUNC_FORMATS = {
    "day": "YYYY-MM-DD",
    "hour": "YYYY-MM-DD HH24",
    "minute": "YYYY-MM-DD HH24:MI",
    "second": "YYYY-MM-DD HH24:MI:SS",
    "millisecond": "YYYY-MM-DD HH24:MI:SS.FF3",
}
TEXT_TYPE = "NVARCHAR(5000)"


class HanaDialect(Dialect):
    name = "hana"
    text_type = TEXT_TYPE
    double_type = "DOUBLE"

    # ------------------------------------------------------------------ basics
    def literal(self, value: Any) -> str:
        out = super().literal(value)
        if isinstance(value, str) and not value.isascii():
            return "N" + out
        return out

    def to_text(self, expr: str) -> str:
        return f"CAST({expr} AS {TEXT_TYPE})"

    def is_distinct(self, left: str, right: str) -> str:
        return (
            f"(CASE WHEN {left} IS NULL AND {right} IS NULL THEN 0 "
            f"WHEN {left} IS NULL OR {right} IS NULL THEN 1 "
            f"WHEN {left} = {right} THEN 0 ELSE 1 END = 1)"
        )

    def md5(self, expr: str) -> str:
        return f"LOWER(BINTOHEX(HASH_MD5(TO_BINARY({expr}))))"

    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"HEXTONUM(SUBSTRING({hex_expr}, {int(offset) + 1}, 8))"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"TO_BIGINT(FLOOR(({numerator}) / {int(denominator)}))"

    def supports_unicode_form(self, form: str) -> bool:
        return False

    def boolean(self, condition: str) -> str:
        return f"(CASE WHEN {condition} THEN TRUE WHEN NOT ({condition}) THEN FALSE END)"

    # ------------------------------------------------------------------ strings
    def trim(self, expr: str, mode: str = "both", chars: str = " ") -> str:
        removal = self.literal(chars)
        if mode == "ltrim":
            return f"LTRIM({expr}, {removal})"
        if mode == "rtrim":
            return f"RTRIM({expr}, {removal})"
        if mode != "both":
            raise UnsupportedFeatureError(f"unknown trim mode '{mode}'")
        return f"LTRIM(RTRIM({expr}, {removal}), {removal})"

    def unicode_normalize(self, expr: str, form: str) -> str:
        raise UnsupportedFeatureError(
            f"SAP HANA has no Unicode normalization function, so {form} is not available; "
            "set normalization.unicode_form to none or drop the unicode_nfc normalizer"
        )

    def collapse_whitespace(self, expr: str) -> str:
        pattern = self.literal(WHITESPACE_CLASS)
        return f"REPLACE_REGEXPR({pattern} IN {expr} WITH ' ' OCCURRENCE ALL)"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        if full:
            pattern = f"^(?:{pattern})\\z"
        return f"({expr} LIKE_REGEXPR {self.literal(pattern)})"

    # ------------------------------------------------------------------ canonical forms
    def date_from_iso(self, expr: str) -> str:
        return f"TO_DATE({expr}, 'YYYY-MM-DD')"

    def time_from_iso(self, expr: str) -> str:
        return f"TO_TIME({expr}, 'HH24:MI:SS')"

    def integer_text(self, expr: str) -> str:
        return f"TO_NVARCHAR({expr})"

    def round_decimal(self, expr: str, digits: int) -> str:
        d = int(digits)
        # ROUND_HALF_UP rounds half away from zero, like the reference.
        return f"CAST(ROUND({expr}, {d}, ROUND_HALF_UP) AS DECIMAL(38, {d}))"

    def decimal_text(self, expr: str, scale: int) -> str:
        rounded = self.round_decimal(expr, scale)
        # ABS of zero avoids '-0.00'.
        return f"TO_NVARCHAR(CASE WHEN {rounded} = 0 THEN ABS({rounded}) ELSE {rounded} END)"

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        fmt = _TRUNC_FORMATS[unit]
        return f"TO_TIMESTAMP(TO_NVARCHAR({expr}, '{fmt}'), '{fmt}')"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        ts = f"CAST({expr} AS TIMESTAMP)"
        if tz_aware or timezone == "UTC":
            return ts
        return f"LOCALTOUTC({ts}, {self.literal(timezone)}, 'platform')"

    def date_text(self, expr: str) -> str:
        return f"TO_NVARCHAR(CAST({expr} AS DATE), 'YYYY-MM-DD')"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        text = f"REPLACE(TO_NVARCHAR({utc}, 'YYYY-MM-DD HH24:MI:SS.FF6'), ' ', 'T')"
        return f"LEFT({text}, {_TS_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        text = f"TO_NVARCHAR(CAST({expr} AS TIME), 'HH24:MI:SS') || '.000000'"
        return f"LEFT({text}, {_TIME_WIDTH[precision]})"

    def boolean_text(self, expr: str) -> str:
        return f"(CASE WHEN {expr} = TRUE THEN '1' WHEN {expr} = FALSE THEN '0' END)"

    def binary_hex(self, expr: str) -> str:
        return f"LOWER(BINTOHEX({expr}))"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        # NANO100_BETWEEN counts 100-nanosecond units.
        return f"CAST(NANO100_BETWEEN(TO_TIMESTAMP('1970-01-01 00:00:00'), {utc}) / 10 AS DOUBLE)"

    def current_timestamp_utc(self) -> str:
        return "CURRENT_UTCTIMESTAMP"


DIALECT = HanaDialect()

__all__ = ["DIALECT", "HanaDialect"]
