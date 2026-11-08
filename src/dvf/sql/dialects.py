"""SQL dialect primitives used to generate pushdown SQL.

Each dialect knows how to quote identifiers, write literals, and express the handful of functions
canonicalization and checks need. User-supplied SQL fragments (filters, ``where``, custom SQL) are
trusted configuration and are passed through unchanged; identifiers dvf discovers from catalogs
are always quoted.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, ClassVar

from dvf.core.errors import DvfError

HASH_SPACE = 2**32
"""Size of the integer space :meth:`Dialect.hash_int` maps 8 hexadecimal digits into."""

WHITESPACE_CLASS = "[ \t\n\r]+"
"""Characters that ``collapse_whitespace`` replaces with one space (ASCII whitespace only)."""


class UnsupportedFeatureError(DvfError):
    """A dialect cannot express something a check or comparison needs."""


class Dialect:
    """Base dialect: ANSI-flavored SQL with double-quoted identifiers."""

    name: ClassVar[str] = "ansi"
    text_type: ClassVar[str] = "VARCHAR"
    double_type: ClassVar[str] = "DOUBLE PRECISION"
    full_outer_join: ClassVar[bool] = True
    """Whether ``FULL OUTER JOIN`` is supported (MySQL has none; diffs emulate it)."""

    # ------------------------------------------------------------------ basics
    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise UnsupportedFeatureError(f"cannot write the non-finite number {value} as SQL")
            return repr(value)
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, datetime | date | time):
            value = value.isoformat()
        text = str(value)
        return "'" + text.replace("'", "''") + "'"

    def to_text(self, expr: str) -> str:
        return f"CAST({expr} AS {self.text_type})"

    def to_double(self, expr: str) -> str:
        return f"CAST({expr} AS {self.double_type})"

    def length(self, expr: str) -> str:
        return f"LENGTH({expr})"

    def is_distinct(self, left: str, right: str) -> str:
        return f"({left} IS DISTINCT FROM {right})"

    def greatest(self, *exprs: str) -> str:
        return f"GREATEST({', '.join(exprs)})"

    def concat(self, *exprs: str) -> str:
        """Concatenate expressions that are never NULL (used to serialize canonical rows)."""
        return "(" + " || ".join(exprs) + ")"

    def md5(self, expr: str) -> str:
        """Lower-case hexadecimal MD5 digest of the UTF-8 bytes of ``expr``."""
        return f"MD5({expr})"

    # ------------------------------------------------------------------ hashing and buckets
    def hash_int(self, hex_expr: str, offset: int) -> str:
        """The integer value (0 to 2**32 - 1) of the 8 hexadecimal digits of ``hex_expr`` that
        start at 0-based ``offset``. ``hex_expr`` is a lower-case MD5 digest from :meth:`md5`."""
        raise UnsupportedFeatureError(f"{self.name} cannot convert hexadecimal digits to integers")

    def int_div(self, numerator: str, denominator: int) -> str:
        """Floor division of a non-negative integer expression by a positive integer constant,
        returning an integer."""
        # Divide an exact multiple, so engines that round quotients to a few decimals stay exact.
        d = int(denominator)
        return f"CAST((({numerator}) - MOD({numerator}, {d})) / {d} AS BIGINT)"

    def bucket(self, int_expr: str, granularity: int) -> str:
        """The bucket (0 to ``granularity - 1``) of a 32-bit hash integer from :meth:`hash_int`."""
        return self.int_div(f"({int_expr}) * {int(granularity)}", HASH_SPACE)

    def supports_unicode_form(self, form: str) -> bool:
        """Whether :meth:`unicode_normalize` supports ``form`` (``NFC``, ``NFD``, ``NFKC``, ``NFKD``).

        When it does not, canonicalization skips Unicode normalization on this side and says so
        in the normalization notes.
        """
        return True

    def boolean(self, condition: str) -> str:
        """A boolean expression usable in SELECT lists (SQLite has no boolean type)."""
        return f"({condition})"

    def count_if(self, condition: str) -> str:
        return f"SUM(CASE WHEN {condition} THEN 1 ELSE 0 END)"

    def limit(self, sql: str, rows: int) -> str:
        return f"{sql}\nLIMIT {int(rows)}"

    def subquery(self, sql: str, alias: str) -> str:
        return f"(\n{sql}\n) AS {self.quote(alias)}"

    # ------------------------------------------------------------------ strings
    def lower(self, expr: str) -> str:
        return f"LOWER({expr})"

    def upper(self, expr: str) -> str:
        return f"UPPER({expr})"

    trim_functions: ClassVar[dict[str, str]] = {"both": "TRIM", "rtrim": "RTRIM", "ltrim": "LTRIM"}

    def trim(self, expr: str, mode: str = "both", chars: str = " ") -> str:
        # Always pass the characters: DuckDB's one-argument TRIM strips all Unicode whitespace.
        return f"{self.trim_functions[mode]}({expr}, {self.literal(chars)})"

    def unicode_normalize(self, expr: str, form: str) -> str:  # pragma: no cover - abstract
        raise UnsupportedFeatureError(f"{self.name} cannot apply Unicode {form} normalization")

    def collapse_whitespace(self, expr: str) -> str:
        return f"REGEXP_REPLACE({expr}, {self.literal(WHITESPACE_CLASS)}, ' ', 'g')"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:  # pragma: no cover
        raise UnsupportedFeatureError(f"{self.name} has no regular expression support")

    # ------------------------------------------------------------------ canonical forms
    def integer_text(self, expr: str) -> str:
        return self.to_text(expr)

    def round_decimal(self, expr: str, digits: int) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        return f"DATE_TRUNC('{unit}', {expr})"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def decimal_text(self, expr: str, scale: int) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def date_text(self, expr: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def time_text(self, expr: str, *, precision: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def boolean_text(self, expr: str) -> str:
        return f"(CASE WHEN {expr} THEN '1' WHEN NOT ({expr}) THEN '0' END)"

    def binary_hex(self, expr: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def current_timestamp_utc(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


_PRECISION_UNITS = {"s": "second", "ms": "millisecond", "us": "microsecond", "ns": "microsecond"}
_PRECISION_WIDTH = {"s": 19, "ms": 23, "us": 26, "ns": 26}
"""Characters kept of ``YYYY-MM-DDTHH:MM:SS.ffffff`` for each precision."""
_TIME_WIDTH = {"s": 8, "ms": 12, "us": 15, "ns": 15}


class DuckDBDialect(Dialect):
    name = "duckdb"
    text_type = "VARCHAR"
    double_type = "DOUBLE"

    def concat(self, *exprs: str) -> str:
        # A chain of || is quadratic in DuckDB; concat() is linear (and operands are never NULL).
        return f"concat({', '.join(exprs)})"

    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"CAST('0x' || SUBSTRING({hex_expr}, {int(offset) + 1}, 8) AS BIGINT)"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"(({numerator}) // {int(denominator)})"

    def supports_unicode_form(self, form: str) -> bool:
        return form == "NFC"

    def unicode_normalize(self, expr: str, form: str) -> str:
        if form != "NFC":
            raise UnsupportedFeatureError(
                f"DuckDB supports only NFC Unicode normalization, not {form}; "
                "set normalization.unicode_form to NFC or none"
            )
        return f"NFC_NORMALIZE({expr})"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        func = "REGEXP_FULL_MATCH" if full else "REGEXP_MATCHES"
        return f"{func}({expr}, {self.literal(pattern)})"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"CAST(CAST({expr} AS DECIMAL(38, {int(scale)})) AS VARCHAR)"

    def round_decimal(self, expr: str, digits: int) -> str:
        return f"CAST({expr} AS DECIMAL(38, {int(digits)}))"

    def date_text(self, expr: str) -> str:
        return f"STRFTIME(CAST({expr} AS DATE), '%Y-%m-%d')"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        return self._to_utc(expr, tz_aware=tz_aware, timezone=timezone)

    def _to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"TIMEZONE('UTC', {expr})"
        if timezone == "UTC":
            return f"CAST({expr} AS TIMESTAMP)"
        return f"TIMEZONE('UTC', TIMEZONE({self.literal(timezone)}, CAST({expr} AS TIMESTAMP)))"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self._to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        unit = _PRECISION_UNITS[precision]
        text = f"STRFTIME(DATE_TRUNC('{unit}', {utc}), '%Y-%m-%dT%H:%M:%S.%f')"
        return f"LEFT({text}, {_PRECISION_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        unit = _PRECISION_UNITS[precision]
        ts = f"DATE_TRUNC('{unit}', DATE '1970-01-01' + CAST({expr} AS TIME))"
        return f"LEFT(STRFTIME({ts}, '%H:%M:%S.%f'), {_TIME_WIDTH[precision]})"

    def binary_hex(self, expr: str) -> str:
        return f"LOWER(HEX({expr}))"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        return (
            f"CAST(EPOCH_US({self._to_utc(expr, tz_aware=tz_aware, timezone=timezone)}) AS DOUBLE)"
        )

    def current_timestamp_utc(self) -> str:
        return "TIMEZONE('UTC', CURRENT_TIMESTAMP)"


class PostgresDialect(Dialect):
    name = "postgres"
    text_type = "TEXT"
    double_type = "DOUBLE PRECISION"
    trim_functions: ClassVar[dict[str, str]] = {"both": "BTRIM", "rtrim": "RTRIM", "ltrim": "LTRIM"}

    def unicode_normalize(self, expr: str, form: str) -> str:
        return f"NORMALIZE({expr}, {form})"

    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"(('x' || SUBSTRING({hex_expr} FROM {int(offset) + 1} FOR 8))::BIT(32)::BIGINT)"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"(CAST({numerator} AS BIGINT) / {int(denominator)})"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        if full:
            pattern = f"^(?:{pattern})$"
        return f"({self.to_text(expr)} ~ {self.literal(pattern)})"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"CAST(ROUND(CAST({expr} AS NUMERIC), {int(scale)}) AS TEXT)"

    def round_decimal(self, expr: str, digits: int) -> str:
        return f"ROUND(CAST({expr} AS NUMERIC), {int(digits)})"

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        return f"DATE_TRUNC('{unit}s', {expr})"

    def date_text(self, expr: str) -> str:
        return f"TO_CHAR(CAST({expr} AS DATE), 'YYYY-MM-DD')"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        return self._to_utc(expr, tz_aware=tz_aware, timezone=timezone)

    def _to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        if tz_aware:
            return f"({expr} AT TIME ZONE 'UTC')"
        if timezone == "UTC":
            return f"CAST({expr} AS TIMESTAMP)"
        return f"((CAST({expr} AS TIMESTAMP) AT TIME ZONE {self.literal(timezone)}) AT TIME ZONE 'UTC')"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        utc = self._to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        unit = _PRECISION_UNITS[precision] + "s"
        text = f"TO_CHAR(DATE_TRUNC('{unit}', {utc}), 'YYYY-MM-DD\"T\"HH24:MI:SS.US')"
        return f"LEFT({text}, {_PRECISION_WIDTH[precision]})"

    def time_text(self, expr: str, *, precision: str) -> str:
        unit = _PRECISION_UNITS[precision] + "s"
        ts = f"DATE_TRUNC('{unit}', DATE '1970-01-01' + CAST({expr} AS TIME))"
        return f"LEFT(TO_CHAR({ts}, 'HH24:MI:SS.US'), {_TIME_WIDTH[precision]})"

    def binary_hex(self, expr: str) -> str:
        return f"ENCODE({expr}, 'hex')"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        utc = self._to_utc(expr, tz_aware=tz_aware, timezone=timezone)
        return f"(EXTRACT(EPOCH FROM {utc}) * 1000000)::DOUBLE PRECISION"

    def current_timestamp_utc(self) -> str:
        return "(NOW() AT TIME ZONE 'UTC')"


class SQLiteDialect(Dialect):
    """SQLite, with dvf's Python functions registered on every connection (see ``connectors.sqlite``).

    SQLite has no decimal, date, or Unicode functions, so canonicalization calls the Python
    reference implementation through user-defined functions. This keeps SQLite results identical
    to the reference by construction.
    """

    name = "sqlite"
    text_type = "TEXT"
    double_type = "REAL"

    def is_distinct(self, left: str, right: str) -> str:
        return f"({left} IS NOT {right})"

    def greatest(self, *exprs: str) -> str:
        return f"MAX({', '.join(exprs)})"

    def boolean(self, condition: str) -> str:
        return f"(CASE WHEN {condition} THEN 1 ELSE 0 END)"

    def md5(self, expr: str) -> str:
        return f"dvf_md5({expr})"

    def hash_int(self, hex_expr: str, offset: int) -> str:
        return f"dvf_hash_int({hex_expr}, {int(offset)})"

    def int_div(self, numerator: str, denominator: int) -> str:
        return f"(({numerator}) / {int(denominator)})"

    def lower(self, expr: str) -> str:
        return f"dvf_lower({expr})"

    def upper(self, expr: str) -> str:
        return f"dvf_upper({expr})"

    def unicode_normalize(self, expr: str, form: str) -> str:
        return f"dvf_unicode_normalize({expr}, {self.literal(form)})"

    def collapse_whitespace(self, expr: str) -> str:
        return f"dvf_collapse_whitespace({expr})"

    def regex_match(self, expr: str, pattern: str, *, full: bool) -> str:
        return f"(dvf_regexp({self.literal(pattern)}, {expr}, {1 if full else 0}) = 1)"

    def integer_text(self, expr: str) -> str:
        return f"dvf_canon_integer({expr})"

    def round_decimal(self, expr: str, digits: int) -> str:
        return f"dvf_canon_decimal({expr}, {int(digits)})"

    def truncate_timestamp(self, expr: str, unit: str) -> str:
        return f"dvf_truncate_timestamp({expr}, {self.literal(unit)})"

    def to_utc(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        return f"dvf_to_utc({expr}, {self.literal(timezone)})"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"dvf_canon_decimal({expr}, {int(scale)})"

    def date_text(self, expr: str) -> str:
        return f"dvf_canon_date({expr})"

    def timestamp_text(
        self, expr: str, *, precision: str, tz_aware: bool, timezone: str, from_date: bool
    ) -> str:
        return f"dvf_canon_timestamp({expr}, {self.literal(precision)}, {self.literal(timezone)})"

    def time_text(self, expr: str, *, precision: str) -> str:
        return f"dvf_canon_time({expr}, {self.literal(precision)})"

    def boolean_text(self, expr: str) -> str:
        return f"dvf_canon_boolean({expr})"

    def binary_hex(self, expr: str) -> str:
        # HEX(NULL) is '' in SQLite, not NULL.
        return f"(CASE WHEN {expr} IS NULL THEN NULL ELSE LOWER(HEX({expr})) END)"

    def epoch_us(self, expr: str, *, tz_aware: bool, timezone: str) -> str:
        return f"dvf_epoch_us({expr}, {self.literal(timezone)})"

    def current_timestamp_utc(self) -> str:
        return "DATETIME('now')"


_DIALECTS: dict[str, Dialect] = {
    d.name: d for d in (DuckDBDialect(), PostgresDialect(), SQLiteDialect())
}

_DIALECT_MODULES = {
    "mysql": "dvf.sql.mysql",
    "mssql": "dvf.sql.mssql",
    "oracle": "dvf.sql.oracle",
    "snowflake": "dvf.sql.snowflake",
    "bigquery": "dvf.sql.bigquery",
    "redshift": "dvf.sql.redshift",
    "databricks": "dvf.sql.databricks",
}
"""Dialects defined in their own modules, imported on first use. Each module defines ``DIALECT``."""


def register_dialect(dialect: Dialect) -> None:
    """Make ``dialect`` available to :func:`get_dialect` (plugins may add their own)."""
    _DIALECTS[dialect.name] = dialect


def get_dialect(name: str) -> Dialect:
    """Return the dialect called ``name`` (for example ``duckdb``, ``postgres``, or ``mssql``)."""
    found = _DIALECTS.get(name)
    if found is not None:
        return found
    module_name = _DIALECT_MODULES.get(name)
    if module_name is not None:
        import importlib

        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
        else:
            dialect: Dialect = module.DIALECT
            register_dialect(dialect)
            return dialect
    known = ", ".join(sorted({*_DIALECTS, *_DIALECT_MODULES}))
    raise UnsupportedFeatureError(
        f"SQL generation for dialect '{name}' is not available; known dialects: {known}"
    )
