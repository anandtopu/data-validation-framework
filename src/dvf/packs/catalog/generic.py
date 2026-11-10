"""Helpers shared by the generic ``sqlalchemy`` and ``odbc`` connectors.

A generic connector reaches a database through SQLAlchemy or ODBC but still needs one of dvf's
SQL dialects, so that canonical forms, hashes, and checks are generated correctly. These helpers
choose the dialect from what the driver reports, prepare each session the way dvf's native
connectors do (UTC, read-only where the database has it), and describe query results.

No driver is imported here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from dvf.core.datasets import ColumnSchema
from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, parse_native_type

VERIFIED_DIALECTS: dict[str, tuple[str, ...]] = {
    "sqlalchemy": ("sqlite", "postgres", "mysql", "oracle"),
    "odbc": ("postgres",),
}
"""dvf dialects each generic connector has been verified with against live databases (canonical
parity and the seeded-defect suite for SQLite and PostgreSQL; the seeded-defect suite for MySQL
and Oracle)."""

SQLALCHEMY_DIALECTS: dict[str, str] = {
    "postgresql": "postgres",
    "sqlite": "sqlite",
    "mysql": "mysql",
    "mssql": "mssql",
    "oracle": "oracle",
    "duckdb": "duckdb",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "redshift": "redshift",
    "redshift_connector": "redshift",
    "databricks": "databricks",
    "hana": "hana",
}
"""SQLAlchemy dialect names (``engine.dialect.name``) and the dvf dialect each maps to."""

ODBC_DBMS_NAMES: tuple[tuple[str, str], ...] = (
    ("postgresql", "postgres"),
    ("redshift", "redshift"),
    ("microsoft sql server", "mssql"),
    ("sql server", "mssql"),
    ("mysql", "mysql"),
    ("oracle", "oracle"),
    ("sqlite", "sqlite"),
    ("duckdb", "duckdb"),
    ("snowflake", "snowflake"),
    ("databricks", "databricks"),
    ("spark sql", "databricks"),
    ("hdb", "hana"),
    ("sap hana", "hana"),
    ("bigquery", "bigquery"),
)
"""Substrings of the ODBC ``SQL_DBMS_NAME`` (lower case) and the dvf dialect each maps to."""

UNSUPPORTED = {
    "mariadb": "MariaDB is not supported yet (its functions differ from MySQL 8)",
}


def dialect_for_sqlalchemy(name: str, override: str | None, connection: str) -> str:
    """The dvf dialect of a SQLAlchemy dialect name, or ``override`` when set."""
    if override:
        return _known(override, connection)
    key = name.lower()
    if key in UNSUPPORTED:
        raise ConnectorError(
            f"Connection '{connection}': {UNSUPPORTED[key]}.",
            hint="Set 'dialect:' on the connection to force a dvf dialect at your own risk.",
        )
    found = SQLALCHEMY_DIALECTS.get(key)
    if found is None:
        raise ConnectorError(
            f"Connection '{connection}': dvf has no SQL dialect for the SQLAlchemy dialect "
            f"'{name}'.",
            hint="Set 'dialect:' to the closest dvf dialect (" + ", ".join(_dialect_names()) + ").",
        )
    return found


def dialect_for_dbms(dbms_name: str, override: str | None, connection: str) -> str:
    """The dvf dialect of an ODBC DBMS name, or ``override`` when set."""
    if override:
        return _known(override, connection)
    lowered = dbms_name.lower()
    if "mariadb" in lowered:
        raise ConnectorError(
            f"Connection '{connection}': {UNSUPPORTED['mariadb']}.",
            hint="Set 'dialect:' on the connection to force a dvf dialect at your own risk.",
        )
    for fragment, dialect in ODBC_DBMS_NAMES:
        if fragment in lowered:
            return dialect
    raise ConnectorError(
        f"Connection '{connection}': dvf has no SQL dialect for the ODBC DBMS '{dbms_name}'.",
        hint="Set 'dialect:' to the closest dvf dialect (" + ", ".join(_dialect_names()) + ").",
    )


def _dialect_names() -> list[str]:
    from dvf.sql.dialects import _DIALECT_MODULES, _DIALECTS

    return sorted({*_DIALECTS, *_DIALECT_MODULES})


def _known(name: str, connection: str) -> str:
    from dvf.sql.dialects import UnsupportedFeatureError, get_dialect

    try:
        get_dialect(name)
    except UnsupportedFeatureError as exc:
        raise ConnectorError(f"Connection '{connection}': {exc}") from None
    return name


# ====================================================================== sessions


def session_statements(dialect: str, *, read_only: bool, timeout_s: int | None) -> list[str]:
    """SQL that prepares a new session like dvf's native connector for ``dialect``."""
    statements: list[str] = []
    if dialect in ("postgres", "redshift"):
        statements.append("SET TIME ZONE 'UTC'")
        if read_only and dialect == "postgres":
            statements.append("SET default_transaction_read_only = on")
        if timeout_s:
            statements.append(f"SET statement_timeout = {int(timeout_s) * 1000}")
    elif dialect == "mysql":
        statements.append("SET time_zone = '+00:00'")
        if read_only:
            statements.append("SET SESSION TRANSACTION READ ONLY")
        if timeout_s:
            statements.append(f"SET SESSION max_execution_time = {int(timeout_s) * 1000}")
    elif dialect == "mssql":
        statements.append("SET DATEFORMAT ymd; SET ARITHABORT ON; SET ANSI_WARNINGS ON")
    elif dialect == "oracle":
        from dvf.connectors.oracle import _SESSION_SQL

        statements.extend(_SESSION_SQL)
    elif dialect == "duckdb":
        statements.append("SET TimeZone = 'UTC'")
    elif dialect == "snowflake":
        statements.append("ALTER SESSION SET TIMEZONE = 'UTC'")
    elif dialect == "databricks":
        statements.append("SET TIME ZONE 'UTC'")
    return statements


def prepare_sqlite(raw_connection: Any) -> None:
    """Register dvf's SQL functions on a ``sqlite3`` connection (the SQLite dialect needs them)."""
    import sqlite3

    from dvf.connectors.sqlite import register_functions

    if not isinstance(raw_connection, sqlite3.Connection):
        raise ConnectorError(
            "The SQLite dialect needs dvf's SQL functions, which can only be registered on a "
            "sqlite3 connection (use the 'sqlite' connector or sqlite:/// URLs).",
        )
    register_functions(raw_connection)


# ====================================================================== query schemas


def _python_type(type_code: Any, size: Any, precision: Any, scale: Any) -> CanonicalType | None:
    """A canonical type from a DB-API ``type_code`` that is a Python class (pyodbc and others)."""
    if not isinstance(type_code, type):
        return None
    if issubclass(type_code, bool):
        return CanonicalType.boolean()
    if issubclass(type_code, int):
        return CanonicalType.integer()
    if issubclass(type_code, float):
        return CanonicalType.float()
    if issubclass(type_code, Decimal):
        if isinstance(precision, int) and precision > 0:
            return CanonicalType.decimal(precision, scale if isinstance(scale, int) else 0)
        return CanonicalType.decimal()
    if issubclass(type_code, dt.datetime):
        return CanonicalType.timestamp(6)
    if issubclass(type_code, dt.date):
        return CanonicalType.date()
    if issubclass(type_code, dt.time):
        return CanonicalType.time(6)
    if issubclass(type_code, bytes | bytearray | memoryview):
        return CanonicalType.binary()
    if issubclass(type_code, uuid.UUID):
        return CanonicalType.string(36)
    if issubclass(type_code, str):
        length = size if isinstance(size, int) and 0 < size < 2**31 - 1 else None
        return CanonicalType.string(length)
    return None


def _value_type(values: Sequence[Any]) -> CanonicalType:
    """A canonical type inferred from sample values (the last resort)."""
    sample = next((v for v in values if v is not None), None)
    if sample is None:
        return CanonicalType.string()
    if isinstance(sample, bool):
        return CanonicalType.boolean()
    if isinstance(sample, int):
        return CanonicalType.integer()
    if isinstance(sample, float):
        return CanonicalType.float()
    if isinstance(sample, Decimal):
        scales = [
            max(0, -int(v.as_tuple().exponent))
            for v in values
            if isinstance(v, Decimal) and v.is_finite()
        ]
        return CanonicalType.decimal(38, max(scales) if scales else 0)
    if isinstance(sample, dt.datetime):
        return CanonicalType.timestamp(6, tz=sample.tzinfo is not None)
    if isinstance(sample, dt.date):
        return CanonicalType.date()
    if isinstance(sample, dt.time):
        return CanonicalType.time(6)
    if isinstance(sample, bytes | bytearray | memoryview):
        return CanonicalType.binary()
    return CanonicalType.string()


NativeNamer = Callable[[Sequence[Any]], str | None]
"""Returns a native type name for a description entry, or None when it cannot tell."""


def describe_columns(
    description: Sequence[Sequence[Any]],
    rows: Sequence[Sequence[Any]],
    dialect: str,
    native_namer: NativeNamer | None = None,
) -> tuple[ColumnSchema, ...]:
    """Column schemas of a query result from the DB-API description (and sample rows).

    Each column's type comes from, in order: a native type name (``native_namer``, for drivers
    that report one), a Python class ``type_code`` (pyodbc), or the sample values.
    """
    columns: list[ColumnSchema] = []
    for i, entry in enumerate(description):
        name = str(entry[0])
        size = entry[3] if len(entry) > 3 else None
        precision = entry[4] if len(entry) > 4 else None
        scale = entry[5] if len(entry) > 5 else None
        native = native_namer(entry) if native_namer is not None else None
        if native:
            ctype = parse_native_type(native, dialect)
        else:
            found = _python_type(entry[1], size, precision, scale)
            if found is not None:
                ctype, native = found, getattr(entry[1], "__name__", str(entry[1]))
            else:
                ctype = _value_type([row[i] for row in rows if i < len(row)])
                native = "unknown"
        columns.append(ColumnSchema(name=name, native_type=str(native), type=ctype, ordinal=i + 1))
    return tuple(columns)


def odbc_native_type(type_name: str, size: Any, digits: Any) -> str:
    """A native type string from ODBC catalog metadata (``SQLColumns``)."""
    name = (type_name or "").strip()
    lowered = name.lower()
    if any(part in lowered for part in ("char", "binary", "bpchar")) and "(" not in name:
        if isinstance(size, int) and 0 < size < 2**31 - 1:
            return f"{name}({size})"
        return name
    if lowered in ("numeric", "decimal", "number", "dec") and isinstance(size, int) and size > 0:
        return f"{name}({size},{int(digits or 0)})"
    return name


__all__ = [
    "ODBC_DBMS_NAMES",
    "SQLALCHEMY_DIALECTS",
    "VERIFIED_DIALECTS",
    "describe_columns",
    "dialect_for_dbms",
    "dialect_for_sqlalchemy",
    "odbc_native_type",
    "prepare_sqlite",
    "session_statements",
]
