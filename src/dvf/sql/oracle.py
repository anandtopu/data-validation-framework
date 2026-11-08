"""Oracle Database dialect (12.2 or later; tested with 23ai).

Semantic differences from the Python reference, and how this dialect handles them:

* **Empty strings are NULL.** Oracle stores ``''`` as NULL, so an empty canonical string (an
  empty value, or one trimmed to nothing) is NULL on Oracle: it hashes as a NULL field and
  equals another side's NULL, not its empty string. The same holds for empty ``RAW`` values.
  ``null_equals_empty`` is therefore always in effect on Oracle, the ``strip_leading_zeros``
  normalizer turns all-zero strings (``'000'``) into NULL instead of ``'0'``, and
  ``empty_if_null`` leaves NULL as NULL. ``STANDARD_HASH(NULL)`` is the digest of the empty
  string, so :meth:`OracleDialect.md5` of NULL is not NULL (row hashes never hash NULL).
* **Text length.** Canonical text is ``VARCHAR2(4000)``, so values and serialized rows longer
  than 4000 bytes (32767 with ``MAX_STRING_SIZE = EXTENDED``) raise ORA-01489 / ORA-22835.
* **Unicode normalization** uses ``COMPOSE``/``DECOMPOSE``. Oracle composes only adjacent
  characters: when a non-composing combining mark of a lower combining class sits between a base
  letter and a mark that would compose with it (``e`` + U+0316 + U+0301), the sequence stays
  decomposed where NFC would compose it.
* **Case mapping** follows Oracle's Unicode tables; characters whose case pairs were added in the
  newest Unicode versions are left unchanged. ``ß`` upper-cases to ``ẞ`` as in the reference.
* **DATE has a time part.** Oracle ``DATE`` carries hours, minutes, and seconds;
  :mod:`dvf.core.types` maps it to a timestamp.
* **Time zones.** Naive values are converted with Oracle's time zone file and two lookups that
  never fail; local times inside a DST gap or overlap may convert differently from the reference
  (``FROM_TZ`` would raise ORA-01878 for them). Oracle offsets have whole minutes, so historical
  local mean time offsets with seconds (before about 1906, depending on the zone) are rounded,
  and the time zone file lists daylight saving transitions only up to 2080 (standard time after).
* **Identifiers** are case-sensitive when quoted; unquoted aliases come back upper-case (the
  connector lower-cases them). Table aliases take no ``AS``: :meth:`subquery` omits it and
  :func:`strip_subquery_alias_as` rewrites other SQL before it runs.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Any

from dvf.sql.dialects import WHITESPACE_CLASS, Dialect, UnsupportedFeatureError

MAX_TEXT = "VARCHAR2(4000)"
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
"""MD5 of the empty string (Oracle cannot hash ``''``, which is NULL)."""

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
_NORMALIZE = {
    "NFC": "COMPOSE(DECOMPOSE({x}, 'CANONICAL'))",
    "NFD": "DECOMPOSE({x}, 'CANONICAL')",
    "NFKC": "COMPOSE(DECOMPOSE({x}, 'COMPATIBILITY'))",
    "NFKD": "DECOMPOSE({x}, 'COMPATIBILITY')",
}
_DECIMAL_DIGITS = 38


# ---------------------------------------------------------------------- SQL rewriting

_TOKEN = re.compile(
    r"""
    (?P<string>[Nn]?'(?:[^']|'')*')
  | (?P<ident>"(?:[^"]|"")*")
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$#]*)
  | (?P<open>\()
  | (?P<close>\))
  | (?P<space>\s+)
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)
_QUERY_WORDS = frozenset({"SELECT", "WITH"})


def strip_subquery_alias_as(sql: str) -> str:
    """Remove ``AS`` between a subquery and its alias (``(SELECT ...) AS x`` -> ``(SELECT ...) x``).

    Oracle rejects ``AS`` before table aliases. Only a parenthesized query directly inside another
    query (or at the top level) loses its ``AS``, so ``CAST((SELECT ...) AS NUMBER)`` is left
    alone. Strings, quoted identifiers, and comments are skipped.
    """
    tokens = [(m.lastgroup or "other", m.group()) for m in _TOKEN.finditer(sql)]
    out: list[str] = []
    # One entry per open parenthesis: is it a query paren, and is its parent a query context?
    stack: list[tuple[bool, bool]] = []
    pending: tuple[bool, bool] | None = None
    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        if pending is not None and kind not in ("space", "comment"):
            is_query = kind == "word" and text.upper() in _QUERY_WORDS
            stack[-1] = (is_query, pending[1])
            pending = None
        if kind == "open":
            parent_is_query = not stack or stack[-1][0]
            stack.append((False, parent_is_query))
            pending = (False, parent_is_query)
            out.append(text)
        elif kind == "close":
            closed = stack.pop() if stack else (False, False)
            out.append(text)
            is_query, in_query_context = closed
            if is_query and in_query_context:
                j = i + 1
                while j < len(tokens) and tokens[j][0] == "space":
                    j += 1
                k = j + 1
                while k < len(tokens) and tokens[k][0] == "space":
                    k += 1
                if (
                    j < len(tokens)
                    and tokens[j][0] == "word"
                    and tokens[j][1].upper() == "AS"
                    and k < len(tokens)
                    and tokens[k][0] in ("word", "ident")
                ):
                    out.append(" ")
                    i = k
                    continue
        else:
            out.append(text)
        i += 1
    return "".join(out)


class OracleDialect(Dialect):
    """Oracle Database 12.2+ with an AL32UTF8 database character set."""

    name = "oracle"
    text_type = MAX_TEXT
    double_type = "BINARY_DOUBLE"

    # ------------------------------------------------------------------ basics
    def literal(self, value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(UTC).replace(tzinfo=None)
                return f"TIMESTAMP '{value.isoformat(sep=' ')} +00:00'"
            return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
        if isinstance(value, date):
            return f"DATE '{value.isoformat()}'"
        if isinstance(value, time):
            return "'" + value.isoformat() + "'"
        return super().literal(value)

    def to_text(self, expr: str) -> str:
        return f"CAST({expr} AS {MAX_TEXT})"

    def to_double(self, expr: str) -> str:
        return f"CAST({expr} AS BINARY_DOUBLE)"

    def length(self, expr: str) -> str:
        # LENGTH4 counts code points (LENGTH counts UTF-16 code units for NVARCHAR2).
        return f"LENGTH4({expr})"

    def is_distinct(self, left: str, right: str) -> str:
        # DECODE treats two NULLs as equal; VARCHAR2 comparison is binary and not blank-padded
        # (the connector sets NLS_COMP = BINARY).
        return f"(DECODE(CAST({left} AS {MAX_TEXT}), CAST({right} AS {MAX_TEXT}), 0, 1) = 1)"

    def concat(self, *exprs: str) -> str:
        return "(" + " || ".join(exprs) + ")"

    def md5(self, expr: str) -> str:
        if expr.strip() == "''":
            return self.literal(EMPTY_MD5)
        # TO_CHAR converts NVARCHAR2 to the (AL32UTF8) database character set, so the digest is
        # taken over UTF-8 bytes.
        return f"LOWER(RAWTOHEX(STANDARD_HASH(TO_CHAR({expr}), 'MD5')))"

    # ------------------------------------------------------------------ hashing and buckets
    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"TO_NUMBER(SUBSTR({hex_expr}, {int(offset) + 1}, 8), 'XXXXXXXX')"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"FLOOR(({numerator}) / {int(denominator)})"

    def supports_unicode_form(self, form: str) -> bool:
        return form in _NORMALIZE

    def boolean(self, condition: str) -> str:
        return f"(CASE WHEN {condition} THEN 1 WHEN NOT ({condition}) THEN 0 END)"

    def limit(self, sql: str, rows: int) -> str:
        return f"{sql}\nFETCH FIRST {int(rows)} ROWS ONLY"

    def subquery(self, sql: str, alias: str) -> str:
        return f"(\n{sql}\n) {self.quote(alias)}"

    # ------------------------------------------------------------------ strings
    def _case(self, func: str, expr: str, fixups: tuple[tuple[str, str], ...]) -> str:
        out = expr
        for old, new in fixups:
            out = f"REPLACE({out}, {self.literal(old)}, {self.literal(new)})"
        return f"{func}({out})"

    def lower(self, expr: str) -> str:
        return self._case("LOWER", expr, _LOWER_FIXUPS)

    def upper(self, expr: str) -> str:
        return self._case("UPPER", expr, _UPPER_FIXUPS)

    def trim(self, expr: str, mode: str = "both", chars: str = " ") -> str:
        """Remove ``chars`` (a set of characters) from one or both ends.

        A value trimmed to nothing is NULL on Oracle.
        """
        chars_sql = self.literal(chars)
        if mode == "ltrim":
            return f"LTRIM({expr}, {chars_sql})"
        if mode == "rtrim":
            return f"RTRIM({expr}, {chars_sql})"
        return f"LTRIM(RTRIM({expr}, {chars_sql}), {chars_sql})"

    def unicode_normalize(self, expr: str, form: str) -> str:
        template = _NORMALIZE.get(form)
        if template is None:
            raise UnsupportedFeatureError(f"Oracle cannot apply Unicode {form} normalization")
        return template.format(x=expr)

    def collapse_whitespace(self, expr: str) -> str:
        return f"REGEXP_REPLACE({expr}, {self.literal(WHITESPACE_CLASS)}, ' ')"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        if not full:
            return f"REGEXP_LIKE({expr}, {self.literal(pattern)})"
        # Oracle's $ also matches before a final line break, so the match must end at the end.
        anchored = self.literal(f"^({pattern})$")
        return f"(REGEXP_INSTR({expr}, {anchored}, 1, 1, 1) = LENGTH({expr}) + 1)"

    # ------------------------------------------------------------------ canonical forms
    def integer_text(self, expr: str) -> str:
        return f"TO_CHAR({expr})"

    def round_decimal(self, expr: str, digits: int) -> str:
        # ROUND on NUMBER rounds half away from zero.
        return f"ROUND(CAST({expr} AS NUMBER), {int(digits)})"

    def decimal_text(self, expr: str, scale: int) -> str:
        digits = "9" * (_DECIMAL_DIGITS - 1) + "0"
        fmt = f"FM{digits}" + (f".{'0' * int(scale)}" if scale > 0 else "")
        return f"TO_CHAR(ROUND(CAST({expr} AS NUMBER), {int(scale)}), '{fmt}')"

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        """Truncate by subtracting the smaller fields, which keeps a value's time zone.

        Fields of ``TIMESTAMP WITH TIME ZONE`` values are read in UTC, so hour and day truncation
        of values with a non-zero offset happens in UTC.
        """
        ts = f"CAST({expr} AS TIMESTAMP(9) WITH TIME ZONE)"
        second = f"EXTRACT(SECOND FROM {ts})"
        if unit == "millisecond":
            drop = f"MOD({second} * 1000, 1) / 1000"
        elif unit == "second":
            drop = f"MOD({second}, 1)"
        elif unit == "minute":
            drop = second
        elif unit == "hour":
            drop = f"(EXTRACT(MINUTE FROM {ts}) * 60 + {second})"
        else:
            drop = f"(EXTRACT(HOUR FROM {ts}) * 3600 + EXTRACT(MINUTE FROM {ts}) * 60 + {second})"
        return f"({ts} - NUMTODSINTERVAL({drop}, 'SECOND'))"

    def _naive_to_utc(self, ts: str, timezone: str) -> str:
        """UTC of a naive local time without FROM_TZ, which fails inside DST gaps."""
        zone = self.literal(timezone)

        def offset(utc: str) -> str:
            return f"(CAST(FROM_TZ({utc}, 'UTC') AT TIME ZONE {zone} AS TIMESTAMP(9)) - {utc})"

        first = f"({ts} - {offset(ts)})"
        return f"({ts} - {offset(first)})"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"CAST(SYS_EXTRACT_UTC({expr}) AS TIMESTAMP(9))"
        local = f"CAST({expr} AS TIMESTAMP(9))"
        if timezone == "UTC":
            return local
        return self._naive_to_utc(local, timezone)

    def date_text(self, expr: str) -> str:
        return f"TO_CHAR({expr}, 'YYYY-MM-DD')"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        # Nine digits, then cut: truncation, exactly like the reference.
        text = f"TO_CHAR({utc}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF9')"
        return f"SUBSTR({text}, 1, {_PRECISION_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        """Time of day of a DATE or TIMESTAMP (Oracle has no TIME type)."""
        text = f"TO_CHAR(CAST({expr} AS TIMESTAMP(9)), 'HH24:MI:SS.FF9')"
        return f"SUBSTR({text}, 1, {_TIME_WIDTH[precision]})"

    def boolean_text(self, expr: str) -> str:
        # TO_NUMBER accepts BOOLEAN (23ai) and numbers alike.
        return f"(CASE WHEN TO_NUMBER({expr}) = 0 THEN '0' WHEN {expr} IS NOT NULL THEN '1' END)"

    def binary_hex(self, expr: str) -> str:
        return f"LOWER(RAWTOHEX({expr}))"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        delta = f"({utc} - TIMESTAMP '1970-01-01 00:00:00')"
        micros = (
            f"EXTRACT(DAY FROM {delta}) * 86400000000 + EXTRACT(HOUR FROM {delta}) * 3600000000"
            f" + EXTRACT(MINUTE FROM {delta}) * 60000000 + EXTRACT(SECOND FROM {delta}) * 1000000"
        )
        return f"CAST(FLOOR({micros}) AS BINARY_DOUBLE)"

    def current_timestamp_utc(self) -> str:
        return "SYS_EXTRACT_UTC(SYSTIMESTAMP)"


DIALECT = OracleDialect()
