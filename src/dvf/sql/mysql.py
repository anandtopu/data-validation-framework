"""MySQL dialect (MySQL 8.0.17 or later).

MariaDB is not covered: it lacks the ``utf8mb4_0900_bin`` collation and ``REGEXP_LIKE`` flags
this dialect relies on.

Semantic differences from the Python reference, and how this dialect handles them:

* **Collations.** The default ``utf8mb4_0900_ai_ci`` collation is case- and accent-insensitive
  and treats some characters (soft hyphen, zero-width joiners) as ignorable. Text produced by
  this dialect is converted to ``utf8mb4`` with the binary, NO PAD collation ``utf8mb4_0900_bin``,
  and :meth:`MySQLDialect.is_distinct` compares bytes, so equality of canonical text is exact.
  SQL that dvf does not route through the dialect (``NULLIF(x, '')`` for ``null_equals_empty``
  on an untouched column, ``COUNT(DISTINCT ...)``) follows the column's collation.
* **No Unicode normalization.** MySQL has no normalization function;
  :meth:`supports_unicode_form` returns ``False`` and canonicalization notes that it was skipped.
* **Case mapping** uses the Unicode 9.0 tables of the ``0900`` collations. MySQL truncates the
  rest of a string after a character whose case mapping is longer in UTF-8 (``Ⱥ``, ``ɐ``); those
  characters are mapped with ``REPLACE`` first. Characters whose case pairs were added after
  Unicode 9.0 (for example Georgian Mtavruli) are left unchanged.
* **Time zones.** ``CONVERT_TZ`` needs the time zone tables (``mysql_tzinfo_to_sql``) and only
  converts values inside the ``TIMESTAMP`` range (1970-01-01 to 2038-01-19 UTC); values outside
  it are returned unconverted. ``TIMESTAMP`` columns are read in the session time zone, which the
  connector sets to UTC.
* **String literals** containing a backslash are written as hexadecimal literals, so they mean
  the same with and without ``NO_BACKSLASH_ESCAPES``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from dvf.sql.dialects import WHITESPACE_CLASS, Dialect, UnsupportedFeatureError

BINARY_COLLATION = "utf8mb4_0900_bin"

GROWING_LOWER = "ȺȾ"
"""Characters whose lower-case form is longer in UTF-8 (``Ⱥ`` -> ``ⱥ``)."""
GROWING_UPPER = "ƛȿɀɐɑɒɜɡɤɥɦɪɫɬɱɽʂʇʝʞ"
"""Characters whose upper-case form is longer in UTF-8 (``ɐ`` -> ``Ɐ``); ``ß`` is handled apart."""


def case_fixups(chars: str, upper: bool) -> tuple[tuple[str, str], ...]:
    """(character, mapped character) pairs applied with ``REPLACE`` before ``LOWER``/``UPPER``,
    taken from Python like the reference (``ß`` upper-cases to ``ẞ``)."""
    pairs: list[tuple[str, str]] = [("ß", "ẞ")] if upper else []
    for ch in chars:
        mapped = ch.upper() if upper else ch.lower()
        if len(mapped) == 1 and mapped != ch:
            pairs.append((ch, mapped))
    return tuple(pairs)


_LOWER_FIXUPS = case_fixups(GROWING_LOWER, upper=False)
_UPPER_FIXUPS = case_fixups(GROWING_UPPER, upper=True)

_PRECISION_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}
_TRUNC_FORMATS = {
    "second": "%Y-%m-%d %H:%i:%s",
    "minute": "%Y-%m-%d %H:%i:00",
    "hour": "%Y-%m-%d %H:00:00",
    "day": "%Y-%m-%d 00:00:00",
}


class MySQLDialect(Dialect):
    """MySQL 8.0.17+ with the ``utf8mb4`` character set."""

    name = "mysql"
    full_outer_join = False
    text_type = "CHAR"
    double_type = "DOUBLE"

    # ------------------------------------------------------------------ basics
    def quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def literal(self, value: Any) -> str:
        if isinstance(value, datetime):
            return "'" + value.isoformat(sep=" ") + "'"
        if isinstance(value, date | time):
            return "'" + value.isoformat() + "'"
        if isinstance(value, str) and "\\" in value:
            # Hexadecimal literals mean the same whether or not NO_BACKSLASH_ESCAPES is set.
            return f"_utf8mb4 X'{value.encode('utf-8').hex()}'"
        return super().literal(value)

    def _utf8(self, expr: str) -> str:
        """``expr`` as utf8mb4 text with the binary, NO PAD collation."""
        return f"(CONVERT({expr} USING utf8mb4) COLLATE {BINARY_COLLATION})"

    def to_text(self, expr: str) -> str:
        return self._utf8(expr)

    def to_double(self, expr: str) -> str:
        return f"CAST({expr} AS DOUBLE)"

    def length(self, expr: str) -> str:
        return f"CHAR_LENGTH({expr})"

    def is_distinct(self, left: str, right: str) -> str:
        # Byte comparison of the UTF-8 text: no collation, no padding (NULL-safe via <=>).
        def as_bytes(expr: str) -> str:
            return f"CAST(CONVERT({expr} USING utf8mb4) AS BINARY)"

        return f"(NOT ({as_bytes(left)} <=> {as_bytes(right)}))"

    def concat(self, *exprs: str) -> str:
        return f"CONCAT({', '.join(exprs)})"

    def md5(self, expr: str) -> str:
        return f"MD5(CONVERT({expr} USING utf8mb4))"

    # ------------------------------------------------------------------ hashing and buckets
    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"CAST(CONV(SUBSTRING({hex_expr}, {int(offset) + 1}, 8), 16, 10) AS UNSIGNED)"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"(({numerator}) DIV {int(denominator)})"

    def supports_unicode_form(self, form: str) -> bool:
        return False

    def boolean(self, condition: str) -> str:
        return f"(CASE WHEN {condition} THEN 1 WHEN NOT ({condition}) THEN 0 END)"

    # ------------------------------------------------------------------ strings
    def _case(self, func: str, expr: str, fixups: tuple[tuple[str, str], ...]) -> str:
        out = self._utf8(expr)
        for old, new in fixups:
            out = f"REPLACE({out}, {self.literal(old)}, {self.literal(new)})"
        return f"{func}({out})"

    def lower(self, expr: str) -> str:
        return self._case("LOWER", expr, _LOWER_FIXUPS)

    def upper(self, expr: str) -> str:
        return self._case("UPPER", expr, _UPPER_FIXUPS)

    def trim(self, expr: str, mode: str = "both", chars: str = " ") -> str:
        """Remove ``chars`` (a set of characters) from one or both ends."""
        text = self._utf8(expr)
        if len(chars) == 1:
            where = {"both": "BOTH", "ltrim": "LEADING", "rtrim": "TRAILING"}[mode]
            return f"TRIM({where} {self.literal(chars)} FROM {text})"
        if not chars or any(c in chars for c in "[]^-\\"):
            raise UnsupportedFeatureError(f"MySQL dialect cannot trim the characters {chars!r}")
        cls = f"[{chars}]+"
        patterns = {"both": f"^{cls}|{cls}$", "ltrim": f"^{cls}", "rtrim": f"{cls}$"}
        return f"REGEXP_REPLACE({text}, {self.literal(patterns[mode])}, '')"

    def unicode_normalize(self, expr: str, form: str) -> str:
        raise UnsupportedFeatureError(
            f"MySQL has no Unicode normalization function, so {form} is not available; "
            "set normalization.unicode_form to none, or normalize the data at the source"
        )

    def collapse_whitespace(self, expr: str) -> str:
        return f"REGEXP_REPLACE({self._utf8(expr)}, {self.literal(WHITESPACE_CLASS)}, ' ')"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        # ICU: 'c' makes matching case-sensitive; \A and \z anchor at the very start and end
        # ($ would also match before a final line break).
        if full:
            pattern = rf"\A(?:{pattern})\z"
        return f"REGEXP_LIKE({self._utf8(expr)}, {self.literal(pattern)}, 'c')"

    # ------------------------------------------------------------------ canonical forms
    def integer_text(self, expr: str) -> str:
        return f"CAST({expr} AS CHAR)"

    def round_decimal(self, expr: str, digits: int) -> str:
        # Casting an exact value to a smaller scale rounds half away from zero.
        return f"CAST({expr} AS DECIMAL(65, {int(digits)}))"

    def decimal_text(self, expr: str, scale: int) -> str:
        # Decimal casts round half away from zero and never produce '-0.00'.
        return f"CAST(CAST({expr} AS DECIMAL(65, {int(scale)})) AS CHAR)"

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        if unit == "millisecond":
            text = f"LEFT(DATE_FORMAT({expr}, '%Y-%m-%d %H:%i:%s.%f'), 23)"
        else:
            text = f"DATE_FORMAT({expr}, '{_TRUNC_FORMATS[unit]}')"
        return f"CAST({text} AS DATETIME(6))"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        value = f"CAST({expr} AS DATETIME(6))"
        if tz_aware or timezone == "UTC":
            # TIMESTAMP columns are returned in the session time zone, which is UTC.
            return value
        return f"CONVERT_TZ({value}, {self.literal(timezone)}, '+00:00')"

    def date_text(self, expr: str) -> str:
        return f"DATE_FORMAT({expr}, '%Y-%m-%d')"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        # Cutting the formatted text truncates the fraction, exactly like the reference.
        text = f"DATE_FORMAT({utc}, '%Y-%m-%dT%H:%i:%s.%f')"
        return f"LEFT({text}, {_PRECISION_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        text = f"TIME_FORMAT(CAST({expr} AS TIME(6)), '%H:%i:%s.%f')"
        return f"LEFT({text}, {_TIME_WIDTH[precision]})"

    def binary_hex(self, expr: str) -> str:
        return f"LOWER(HEX({expr}))"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        return f"CAST(TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', {utc}) AS DOUBLE)"

    def current_timestamp_utc(self) -> str:
        return "UTC_TIMESTAMP(6)"


DIALECT = MySQLDialect()
