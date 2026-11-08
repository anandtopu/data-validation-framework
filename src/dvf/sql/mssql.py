"""Microsoft SQL Server (and Azure SQL) dialect.

Canonicalization needs SQL Server 2019 or later (UTF-8 collations are used to hash UTF-8 bytes);
the ``truncate_timestamp`` normalizer needs SQL Server 2022 (``DATETRUNC``).

Semantic differences from the Python reference, and how this dialect handles them:

* **Collations.** Default collations are case- and accent-insensitive, and ``=`` ignores trailing
  spaces under every collation. Text produced by this dialect (``to_text``, ``trim``, ``lower``,
  ``collapse_whitespace``) carries the binary collation ``Latin1_General_100_BIN2``, and
  :meth:`MSSQLDialect.is_distinct` compares the UTF-16 bytes and their length, so equality of
  canonical text is exact. SQL that dvf does not route through the dialect (``NULLIF(x, '')``
  for ``null_equals_empty``, ``x = ''`` in the ``strip_leading_zeros`` normalizer, and
  ``COUNT(DISTINCT ...)`` in profiles) still compares with trailing-space padding, so a value made
  only of spaces equals the empty string there.
* **Length.** ``LEN`` ignores trailing spaces and counts UTF-16 code units; :meth:`length`
  appends a sentinel and uses a supplementary-character (``_SC``) collation, so it counts code
  points including trailing spaces.
* **MD5.** ``HASHBYTES`` on ``NVARCHAR`` hashes UTF-16; :meth:`md5` converts to ``VARCHAR`` under
  a UTF-8 collation first, so digests match the reference.
* **No Unicode normalization.** SQL Server has no ``NORMALIZE`` function;
  :meth:`supports_unicode_form` returns ``False`` and canonicalization notes that it was skipped.
* **No regular expressions** before SQL Server 2025; :meth:`regex_match` raises.
* **Case mapping** follows SQL Server's own tables (version-140 collations, the most recent):
  characters whose case pairs were added in later Unicode versions are left unchanged. ``ß``
  upper-cases to ``ẞ`` as in the reference.
* **DATETIME** stores 1/300-second ticks (``.003`` is stored as ``.0033333``). Drivers and SQL
  Server itself display such values rounded to milliseconds, so canonical timestamps round
  ``datetime``/``smalldatetime`` values to milliseconds first and then truncate as configured.
* **Time zones.** ``AT TIME ZONE`` takes Windows zone names and Windows rules: IANA names from
  ``normalization.timezone`` are mapped (see :data:`WINDOWS_ZONES`), and dates before the zone's
  current rules took effect may convert differently from IANA tzdata.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from dvf.sql.dialects import (
    WHITESPACE_CLASS,
    Dialect,
    UnsupportedFeatureError,
)

BINARY_COLLATION = "Latin1_General_100_BIN2"
"""Code-point comparison; used for every text value this dialect produces."""
UTF8_COLLATION = "Latin1_General_100_CS_AS_KS_WS_SC_UTF8"
"""Used to convert ``NVARCHAR`` to UTF-8 ``VARCHAR`` before hashing (SQL Server 2019+).

SQL Server ignores ``COLLATE`` between an ``NVARCHAR`` expression's collation and its ``_UTF8``
twin (they compare alike), and the conversion to ``VARCHAR`` then uses the twin's code page.
:meth:`MSSQLDialect.md5` therefore collates to :data:`BINARY_COLLATION` first, whose twin this
is not."""
SC_COLLATION = "Latin1_General_100_CI_AS_SC"
"""Supplementary-character aware: ``LEN`` counts a surrogate pair as one character."""
CASE_COLLATION = "Japanese_XJIS_140_CI_AS"
"""Version-140 collations carry SQL Server's most complete Unicode case tables."""

_MAX_LENGTH = 2_147_483_647
_COLLAPSE_PASSES = 31
"""``REPLACE(s, '  ', ' ')`` halves every run of spaces; 31 passes collapse any NVARCHAR(MAX)."""

GROWING_LOWER = "ȺȾ"
"""Characters whose lower-case form is longer in UTF-8 (``Ⱥ`` -> ``ⱥ``)."""
GROWING_UPPER = "ƛȿɀɐɑɒɜɡɤɥɦɪɫɬɱɽʂʇʝʞ"
"""Characters whose upper-case form is longer in UTF-8 (``ɐ`` -> ``Ɐ``); ``ß`` is handled apart."""


def case_fixups(chars: str, upper: bool) -> tuple[tuple[str, str], ...]:
    """(character, mapped character) pairs applied with ``REPLACE`` before ``LOWER``/``UPPER``.

    Databases leave these characters unchanged (or, in MySQL, truncate the string), while the
    reference maps them. The mapping comes from Python, like the reference itself; ``ß``
    upper-cases to ``ẞ`` as in DuckDB and PostgreSQL.
    """
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
_TRUNC_UNITS = {
    "second": "second",
    "minute": "minute",
    "hour": "hour",
    "day": "day",
    "millisecond": "millisecond",
}

WINDOWS_ZONES: dict[str, str] = {
    "UTC": "UTC",
    "Etc/UTC": "UTC",
    "Etc/GMT": "UTC",
    "GMT": "UTC",
    "Africa/Abidjan": "Greenwich Standard Time",
    "Africa/Accra": "Greenwich Standard Time",
    "Africa/Algiers": "W. Central Africa Standard Time",
    "Africa/Cairo": "Egypt Standard Time",
    "Africa/Casablanca": "Morocco Standard Time",
    "Africa/Johannesburg": "South Africa Standard Time",
    "Africa/Lagos": "W. Central Africa Standard Time",
    "Africa/Nairobi": "E. Africa Standard Time",
    "Africa/Tripoli": "Libya Standard Time",
    "Africa/Tunis": "W. Central Africa Standard Time",
    "Africa/Windhoek": "Namibia Standard Time",
    "America/Anchorage": "Alaskan Standard Time",
    "America/Argentina/Buenos_Aires": "Argentina Standard Time",
    "America/Asuncion": "Paraguay Standard Time",
    "America/Bogota": "SA Pacific Standard Time",
    "America/Buenos_Aires": "Argentina Standard Time",
    "America/Caracas": "Venezuela Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Detroit": "Eastern Standard Time",
    "America/Edmonton": "Mountain Standard Time",
    "America/Halifax": "Atlantic Standard Time",
    "America/Havana": "Cuba Standard Time",
    "America/Indiana/Indianapolis": "US Eastern Standard Time",
    "America/Indianapolis": "US Eastern Standard Time",
    "America/La_Paz": "SA Western Standard Time",
    "America/Lima": "SA Pacific Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Mexico_City": "Central Standard Time (Mexico)",
    "America/Montevideo": "Montevideo Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Panama": "SA Pacific Standard Time",
    "America/Phoenix": "US Mountain Standard Time",
    "America/Regina": "Canada Central Standard Time",
    "America/Santiago": "Pacific SA Standard Time",
    "America/Sao_Paulo": "E. South America Standard Time",
    "America/St_Johns": "Newfoundland Standard Time",
    "America/Toronto": "Eastern Standard Time",
    "America/Vancouver": "Pacific Standard Time",
    "America/Winnipeg": "Central Standard Time",
    "Asia/Almaty": "Central Asia Standard Time",
    "Asia/Amman": "Jordan Standard Time",
    "Asia/Baghdad": "Arabic Standard Time",
    "Asia/Baku": "Azerbaijan Standard Time",
    "Asia/Bangkok": "SE Asia Standard Time",
    "Asia/Beirut": "Middle East Standard Time",
    "Asia/Calcutta": "India Standard Time",
    "Asia/Colombo": "Sri Lanka Standard Time",
    "Asia/Damascus": "Syria Standard Time",
    "Asia/Dhaka": "Bangladesh Standard Time",
    "Asia/Dubai": "Arabian Standard Time",
    "Asia/Ho_Chi_Minh": "SE Asia Standard Time",
    "Asia/Hong_Kong": "China Standard Time",
    "Asia/Irkutsk": "North Asia East Standard Time",
    "Asia/Jakarta": "SE Asia Standard Time",
    "Asia/Jerusalem": "Israel Standard Time",
    "Asia/Kabul": "Afghanistan Standard Time",
    "Asia/Karachi": "Pakistan Standard Time",
    "Asia/Kathmandu": "Nepal Standard Time",
    "Asia/Katmandu": "Nepal Standard Time",
    "Asia/Kolkata": "India Standard Time",
    "Asia/Krasnoyarsk": "North Asia Standard Time",
    "Asia/Kuala_Lumpur": "Singapore Standard Time",
    "Asia/Kuwait": "Arab Standard Time",
    "Asia/Manila": "Singapore Standard Time",
    "Asia/Novosibirsk": "N. Central Asia Standard Time",
    "Asia/Qatar": "Arab Standard Time",
    "Asia/Riyadh": "Arab Standard Time",
    "Asia/Seoul": "Korea Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "Asia/Tashkent": "West Asia Standard Time",
    "Asia/Tbilisi": "Georgian Standard Time",
    "Asia/Tehran": "Iran Standard Time",
    "Asia/Tokyo": "Tokyo Standard Time",
    "Asia/Ulaanbaatar": "Ulaanbaatar Standard Time",
    "Asia/Vladivostok": "Vladivostok Standard Time",
    "Asia/Yakutsk": "Yakutsk Standard Time",
    "Asia/Yangon": "Myanmar Standard Time",
    "Asia/Yekaterinburg": "Ekaterinburg Standard Time",
    "Asia/Yerevan": "Caucasus Standard Time",
    "Atlantic/Azores": "Azores Standard Time",
    "Atlantic/Cape_Verde": "Cape Verde Standard Time",
    "Atlantic/Reykjavik": "Greenwich Standard Time",
    "Australia/Adelaide": "Cen. Australia Standard Time",
    "Australia/Brisbane": "E. Australia Standard Time",
    "Australia/Darwin": "AUS Central Standard Time",
    "Australia/Hobart": "Tasmania Standard Time",
    "Australia/Melbourne": "AUS Eastern Standard Time",
    "Australia/Perth": "W. Australia Standard Time",
    "Australia/Sydney": "AUS Eastern Standard Time",
    "Europe/Amsterdam": "W. Europe Standard Time",
    "Europe/Athens": "GTB Standard Time",
    "Europe/Belgrade": "Central Europe Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Brussels": "Romance Standard Time",
    "Europe/Bucharest": "GTB Standard Time",
    "Europe/Budapest": "Central Europe Standard Time",
    "Europe/Chisinau": "E. Europe Standard Time",
    "Europe/Copenhagen": "Romance Standard Time",
    "Europe/Dublin": "GMT Standard Time",
    "Europe/Helsinki": "FLE Standard Time",
    "Europe/Istanbul": "Turkey Standard Time",
    "Europe/Kaliningrad": "Kaliningrad Standard Time",
    "Europe/Kiev": "FLE Standard Time",
    "Europe/Kyiv": "FLE Standard Time",
    "Europe/Lisbon": "GMT Standard Time",
    "Europe/London": "GMT Standard Time",
    "Europe/Luxembourg": "W. Europe Standard Time",
    "Europe/Madrid": "Romance Standard Time",
    "Europe/Minsk": "Belarus Standard Time",
    "Europe/Moscow": "Russian Standard Time",
    "Europe/Oslo": "W. Europe Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "Europe/Prague": "Central Europe Standard Time",
    "Europe/Riga": "FLE Standard Time",
    "Europe/Rome": "W. Europe Standard Time",
    "Europe/Samara": "Russia Time Zone 3",
    "Europe/Sofia": "FLE Standard Time",
    "Europe/Stockholm": "W. Europe Standard Time",
    "Europe/Tallinn": "FLE Standard Time",
    "Europe/Vienna": "W. Europe Standard Time",
    "Europe/Vilnius": "FLE Standard Time",
    "Europe/Warsaw": "Central European Standard Time",
    "Europe/Zagreb": "Central European Standard Time",
    "Europe/Zurich": "W. Europe Standard Time",
    "Pacific/Auckland": "New Zealand Standard Time",
    "Pacific/Fiji": "Fiji Standard Time",
    "Pacific/Guam": "West Pacific Standard Time",
    "Pacific/Honolulu": "Hawaiian Standard Time",
    "Pacific/Port_Moresby": "West Pacific Standard Time",
    "Pacific/Tongatapu": "Tonga Standard Time",
}
"""IANA zone names and the Windows zones SQL Server's ``AT TIME ZONE`` knows them by (from the
Unicode CLDR mapping). Extend it with :func:`register_windows_zone`."""


def register_windows_zone(iana: str, windows: str) -> None:
    """Teach the dialect the Windows name (``sys.time_zone_info``) of an IANA time zone."""
    WINDOWS_ZONES[iana] = windows


def windows_zone(iana: str) -> str:
    """The Windows zone name for an IANA name, or raise :class:`UnsupportedFeatureError`."""
    found = WINDOWS_ZONES.get(iana)
    if found is None:
        raise UnsupportedFeatureError(
            f"SQL Server converts time zones by Windows name, and dvf does not know the Windows "
            f"name of '{iana}'. Register it with "
            "dvf.sql.mssql.register_windows_zone(iana, windows_name) (see sys.time_zone_info)."
        )
    return found


# ---------------------------------------------------------------------- SQL scanning

_TOKEN = re.compile(
    r"""
    (?P<string>N?'(?:[^']|'')*')
  | (?P<ident>\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*")
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<word>[A-Za-z_@#][A-Za-z0-9_@#$]*)
  | (?P<open>\()
  | (?P<close>\))
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


class _TopLevel:
    """Words at parenthesis depth zero of a statement (strings and comments skipped)."""

    def __init__(self, sql: str) -> None:
        self.words: list[tuple[str, int, int]] = []
        depth = 0
        for match in _TOKEN.finditer(sql):
            kind = match.lastgroup
            if kind == "open":
                depth += 1
            elif kind == "close":
                depth = max(0, depth - 1)
            elif kind == "word" and depth == 0:
                self.words.append((match.group().upper(), match.start(), match.end()))

    def first(self) -> str | None:
        return self.words[0][0] if self.words else None

    def has(self, *names: str) -> bool:
        return any(w in names for w, _, _ in self.words)

    def has_order_by(self) -> bool:
        return any(
            w == "ORDER" and i + 1 < len(self.words) and self.words[i + 1][0] == "BY"
            for i, (w, _, _) in enumerate(self.words)
        )

    def main_select(self) -> int | None:
        """Index (in :attr:`words`) of the SELECT that starts the main query."""
        for i, (word, _, _) in enumerate(self.words):
            if word == "SELECT":
                return i
        return None


class MSSQLDialect(Dialect):
    """SQL Server 2019+ (and Azure SQL Database / Managed Instance)."""

    name = "mssql"
    text_type = "NVARCHAR(MAX)"
    double_type = "FLOAT"

    # ------------------------------------------------------------------ basics
    def quote(self, identifier: str) -> str:
        return "[" + identifier.replace("]", "]]") + "]"

    def literal(self, value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, datetime):
            return "'" + value.isoformat() + "'"
        if isinstance(value, date | time):
            return "'" + value.isoformat() + "'"
        if isinstance(value, str):
            return "N'" + value.replace("'", "''") + "'"
        if value is None or isinstance(value, int | float | Decimal):
            return super().literal(value)
        return "N'" + str(value).replace("'", "''") + "'"

    def to_text(self, expr: str) -> str:
        return f"(CAST({expr} AS NVARCHAR(MAX)) COLLATE {BINARY_COLLATION})"

    def to_double(self, expr: str) -> str:
        return f"CAST({expr} AS FLOAT)"

    def length(self, expr: str) -> str:
        # LEN ignores trailing spaces (the sentinel keeps them) and counts surrogate pairs as one
        # character only under an _SC collation.
        return f"(LEN((CAST({expr} AS NVARCHAR(MAX)) + N'.') COLLATE {SC_COLLATION}) - 1)"

    def _bytes(self, expr: str) -> str:
        return f"CAST(CAST({expr} AS NVARCHAR(MAX)) AS VARBINARY(MAX))"

    def is_distinct(self, left: str, right: str) -> str:
        # Binary comparison of the UTF-16 text: no collation, no trailing-space padding. VARBINARY
        # comparison pads with zero bytes, so the byte lengths are compared as well.
        lb, rb = self._bytes(left), self._bytes(right)
        return (
            f"(CASE WHEN {left} IS NULL AND {right} IS NULL THEN 0 "
            f"WHEN {left} IS NULL OR {right} IS NULL THEN 1 "
            f"WHEN {lb} = {rb} AND DATALENGTH({lb}) = DATALENGTH({rb}) THEN 0 ELSE 1 END = 1)"
        )

    def greatest(self, *exprs: str) -> str:
        # GREATEST exists only from SQL Server 2022; MAX over VALUES works everywhere and, like
        # PostgreSQL's GREATEST, ignores NULLs.
        rows = ", ".join(f"({e})" for e in exprs)
        return f"(SELECT MAX(dvf_v) FROM (VALUES {rows}) AS dvf_g(dvf_v))"

    def concat(self, *exprs: str) -> str:
        if len(exprs) == 1:
            return exprs[0]
        # CONCAT truncates to 8000 bytes unless an argument is a MAX type.
        first = f"CAST({exprs[0]} AS NVARCHAR(MAX))"
        return f"CONCAT({', '.join([first, *exprs[1:]])})"

    def md5(self, expr: str) -> str:
        text = f"(CAST({expr} AS NVARCHAR(MAX)) COLLATE {BINARY_COLLATION})"
        utf8 = f"CAST({text} COLLATE {UTF8_COLLATION} AS VARCHAR(MAX))"
        return f"LOWER(CONVERT(VARCHAR(32), HASHBYTES('MD5', {utf8}), 2))"

    # ------------------------------------------------------------------ hashing and buckets
    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"CAST(CONVERT(BINARY(4), SUBSTRING({hex_expr}, {int(offset) + 1}, 8), 2) AS BIGINT)"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"(CAST({numerator} AS BIGINT) / {int(denominator)})"

    def supports_unicode_form(self, form: str) -> bool:
        return False

    def boolean(self, condition: str) -> str:
        return f"(CASE WHEN {condition} THEN 1 WHEN NOT ({condition}) THEN 0 END)"

    def limit(self, sql: str, rows: int) -> str:
        """Limit ``sql`` to ``rows`` rows (``TOP`` inserted into the main SELECT when possible).

        Inserting ``TOP`` keeps an ``ORDER BY`` legal inside derived tables, which SQL Server
        otherwise rejects.
        """
        n = int(rows)
        scan = _TopLevel(sql)
        has_set_op = scan.has("UNION", "INTERSECT", "EXCEPT")
        index = scan.main_select()
        if index is not None and not has_set_op:
            following = [w for w, _, _ in scan.words[index + 1 : index + 3]]
            insert_at = scan.words[index][2]
            if following[:1] in (["DISTINCT"], ["ALL"]):
                insert_at = scan.words[index + 1][2]
                following = following[1:]
            if following[:1] != ["TOP"]:
                return f"{sql[:insert_at]} TOP ({n}){sql[insert_at:]}"
        starts_with_cte = scan.first() == "WITH"
        if scan.has_order_by():
            if n > 0:
                return f"{sql}\nOFFSET 0 ROWS FETCH NEXT {n} ROWS ONLY"
            if not starts_with_cte:
                return f"SELECT TOP (0) * FROM (\n{sql}\nOFFSET 0 ROWS\n) AS [dvf_limit]"
        elif not starts_with_cte:
            return f"SELECT TOP ({n}) * FROM (\n{sql}\n) AS [dvf_limit]"
        elif n > 0:
            return f"{sql}\nORDER BY 1 OFFSET 0 ROWS FETCH NEXT {n} ROWS ONLY"
        raise UnsupportedFeatureError(
            "SQL Server cannot return zero rows of a WITH ... UNION query without rewriting it"
        )

    # ------------------------------------------------------------------ strings
    def _case(self, func: str, expr: str, fixups: tuple[tuple[str, str], ...]) -> str:
        out = f"(CAST({expr} AS NVARCHAR(MAX)) COLLATE {BINARY_COLLATION})"
        for old, new in fixups:
            out = f"REPLACE({out}, N'{old}', N'{new}')"
        return f"({func}({out} COLLATE {CASE_COLLATION}) COLLATE {BINARY_COLLATION})"

    def lower(self, expr: str) -> str:
        return self._case("LOWER", expr, _LOWER_FIXUPS)

    def upper(self, expr: str) -> str:
        return self._case("UPPER", expr, _UPPER_FIXUPS)

    def trim(self, expr: str, mode: str = "both", chars: str = " ") -> str:
        """Remove ``chars`` (a set of characters) from one or both ends.

        ``LTRIM``/``RTRIM`` remove only U+0020, which is what dvf trims; other characters use
        ``PATINDEX`` (the two-argument ``LTRIM`` needs SQL Server 2022).
        """
        if mode not in ("both", "ltrim", "rtrim"):
            raise UnsupportedFeatureError(f"unknown trim mode '{mode}'")
        text = f"(CAST({expr} AS NVARCHAR(MAX)) COLLATE {BINARY_COLLATION})"
        if chars == " ":
            if mode == "ltrim":
                return f"LTRIM({text})"
            if mode == "rtrim":
                return f"RTRIM({text})"
            return f"LTRIM(RTRIM({text}))"
        if not chars or any(c in chars for c in "[]^-"):
            raise UnsupportedFeatureError(
                f"SQL Server dialect cannot trim the characters {chars!r} (use letters, digits, "
                "or punctuation other than []^-)"
            )
        sentinel = next(c for c in ".x#~" if c not in chars)
        pattern = self.literal(f"%[^{chars}]%")
        stop = self.literal(sentinel)

        def strip_left(value: str) -> str:
            return f"SUBSTRING({value}, PATINDEX({pattern}, {value} + {stop}), {_MAX_LENGTH})"

        def strip_right(value: str) -> str:
            return f"REVERSE({strip_left(f'REVERSE({value})')})"

        if mode == "ltrim":
            return strip_left(text)
        if mode == "rtrim":
            return strip_right(text)
        return strip_left(strip_right(text))

    def unicode_normalize(self, expr: str, form: str) -> str:
        raise UnsupportedFeatureError(
            f"SQL Server has no Unicode normalization function, so {form} is not available; "
            "set normalization.unicode_form to none, or normalize the data at the source"
        )

    def collapse_whitespace(self, expr: str) -> str:
        assert WHITESPACE_CLASS == "[ \t\n\r]+"
        out = f"(CAST({expr} AS NVARCHAR(MAX)) COLLATE {BINARY_COLLATION})"
        for code in (9, 10, 13):
            out = f"REPLACE({out}, NCHAR({code}), N' ')"
        for _ in range(_COLLAPSE_PASSES):
            out = f"REPLACE({out}, N'  ', N' ')"
        return out

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        raise UnsupportedFeatureError(
            "SQL Server has no regular expressions before SQL Server 2025; use an 'expression' "
            "or 'custom_sql' check with LIKE patterns instead of 'regex'"
        )

    # ------------------------------------------------------------------ canonical forms
    def integer_text(self, expr: str) -> str:
        return f"CAST({expr} AS NVARCHAR(MAX))"

    def round_decimal(self, expr: str, digits: int) -> str:
        # Casting a decimal to a smaller scale rounds half away from zero.
        return f"CAST({expr} AS DECIMAL(38, {int(digits)}))"

    def decimal_text(self, expr: str, scale: int) -> str:
        # Decimal casts round half away from zero and never produce '-0.00'.
        return f"CAST(CAST({expr} AS DECIMAL(38, {int(scale)})) AS NVARCHAR(MAX))"

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        return f"DATETRUNC({_TRUNC_UNITS[unit]}, {expr})"

    def _datetime2(self, expr: str) -> str:
        """``expr`` as DATETIME2(7); DATETIME ticks (1/300 s) round to milliseconds first."""
        base = f"SQL_VARIANT_PROPERTY({expr}, 'BaseType')"
        return (
            f"(CASE WHEN {base} IN ('datetime', 'smalldatetime') "
            f"THEN CAST(CAST({expr} AS DATETIME2(3)) AS DATETIME2(7)) "
            f"ELSE CAST({expr} AS DATETIME2(7)) END)"
        )

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"CAST(SWITCHOFFSET({expr}, '+00:00') AS DATETIME2(7))"
        local = self._datetime2(expr)
        if timezone == "UTC":
            return local
        zone = self.literal(windows_zone(timezone))
        return f"CAST(({local} AT TIME ZONE {zone}) AT TIME ZONE 'UTC' AS DATETIME2(7))"

    def date_text(self, expr: str) -> str:
        return f"CONVERT(NVARCHAR(10), CAST({expr} AS DATE), 23)"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        if precision == "s":
            return f"CONVERT(NVARCHAR(19), {utc}, 126)"
        micros = f"RIGHT(N'00000' + CAST(DATEPART(MICROSECOND, {utc}) AS NVARCHAR(6)), 6)"
        text = f"CONVERT(NVARCHAR(19), {utc}, 126) + N'.' + {micros}"
        return f"LEFT({text}, {_PRECISION_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        value = f"CAST({expr} AS TIME(7))"
        if precision == "s":
            return f"CONVERT(NVARCHAR(8), {value}, 108)"
        micros = f"RIGHT(N'00000' + CAST(DATEPART(MICROSECOND, {value}) AS NVARCHAR(6)), 6)"
        text = f"CONVERT(NVARCHAR(8), {value}, 108) + N'.' + {micros}"
        return f"LEFT({text}, {_TIME_WIDTH[precision]})"

    def boolean_text(self, expr: str) -> str:
        return f"(CASE WHEN {expr} = 0 THEN N'0' WHEN {expr} IS NOT NULL THEN N'1' END)"

    def binary_hex(self, expr: str) -> str:
        return f"LOWER(CONVERT(VARCHAR(MAX), CAST({expr} AS VARBINARY(MAX)), 2))"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        utc = self.to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        return (
            f"CAST(DATEDIFF_BIG(MICROSECOND, CAST('1970-01-01' AS DATETIME2(7)), {utc}) AS FLOAT)"
        )

    def current_timestamp_utc(self) -> str:
        return "SYSUTCDATETIME()"


DIALECT = MSSQLDialect()
