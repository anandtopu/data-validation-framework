"""MySQL, SQL Server, and Oracle connectors and dialects without a database.

SQL generation, URL parsing, connection settings, error wrapping, missing drivers, and catalog
schema mapping (with fake drivers). The live behavior is covered by
``tests/parity/test_server_dialect_parity.py`` and ``tests/e2e/test_seeded_servers.py``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from dvf.config.connections import DatabaseConnection
from dvf.config.models import DatasetSpec
from dvf.connectors import mssql as mssql_conn
from dvf.connectors import mysql as mysql_conn
from dvf.connectors import oracle as oracle_conn
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import LogicalType
from dvf.normalize.canonical import CanonSpec, Kind, StringOptions, canonical_sql, row_hash_sql
from dvf.sql import mssql as mssql_sql
from dvf.sql import mysql as mysql_sql
from dvf.sql import oracle as oracle_sql
from dvf.sql.dialects import UnsupportedFeatureError, get_dialect

MYSQL = mysql_sql.DIALECT
MSSQL = mssql_sql.DIALECT
ORACLE = oracle_sql.DIALECT


def _config(kind: str, **values: Any) -> DatabaseConnection:
    return DatabaseConnection.model_validate({"type": kind, **values})


# ====================================================================== dialect registry


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("mysql", mysql_sql.MySQLDialect),
        ("mssql", mssql_sql.MSSQLDialect),
        ("oracle", oracle_sql.OracleDialect),
    ],
)
def test_get_dialect_loads_server_dialects(name: str, cls: type) -> None:
    dialect = get_dialect(name)
    assert isinstance(dialect, cls)
    assert dialect.name == name


# ====================================================================== quoting and literals


def test_quoting() -> None:
    assert MYSQL.quote("a`b") == "`a``b`"
    assert MSSQL.quote("a]b") == "[a]]b]"
    assert ORACLE.quote('a"b') == '"a""b"'


def test_mysql_literals() -> None:
    assert MYSQL.literal("it's") == "'it''s'"
    # Backslashes would be escapes unless NO_BACKSLASH_ESCAPES is set: write hex instead.
    assert MYSQL.literal("C:\\temp") == "_utf8mb4 X'433a5c74656d70'"
    assert MYSQL.literal(datetime(2024, 1, 2, 3, 4, 5)) == "'2024-01-02 03:04:05'"
    assert MYSQL.literal(True) == "TRUE"


def test_mssql_literals() -> None:
    assert MSSQL.literal("Müller's") == "N'Müller''s'"
    assert MSSQL.literal(True) == "1"
    assert MSSQL.literal(False) == "0"
    assert MSSQL.literal(datetime(2024, 1, 2, 3, 4, 5)) == "'2024-01-02T03:04:05'"
    assert MSSQL.literal(None) == "NULL"
    assert MSSQL.literal(12) == "12"


def test_oracle_literals() -> None:
    assert ORACLE.literal(True) == "1"
    assert ORACLE.literal(date(2024, 2, 29)) == "DATE '2024-02-29'"
    assert (
        ORACLE.literal(datetime(2024, 1, 2, 3, 4, 5, 6)) == "TIMESTAMP '2024-01-02 03:04:05.000006'"
    )
    aware = datetime.fromisoformat("2024-01-02T05:04:05+02:00")
    assert ORACLE.literal(aware) == "TIMESTAMP '2024-01-02 03:04:05 +00:00'"


# ====================================================================== limit and subqueries


def test_mssql_limit_inserts_top() -> None:
    assert MSSQL.limit("SELECT a FROM t ORDER BY a", 5) == "SELECT TOP (5) a FROM t ORDER BY a"
    assert MSSQL.limit("select distinct a from t", 2) == "select distinct TOP (2) a from t"
    cte = "WITH x AS (SELECT a FROM t) SELECT a FROM x ORDER BY a"
    assert MSSQL.limit(cte, 3) == "WITH x AS (SELECT a FROM t) SELECT TOP (3) a FROM x ORDER BY a"
    # Strings, brackets, and comments do not confuse the scanner.
    tricky = "SELECT 'UNION SELECT' AS [order by], a /* UNION */ FROM t"
    assert MSSQL.limit(tricky, 1).startswith("SELECT TOP (1) 'UNION SELECT'")


def test_mssql_limit_falls_back_for_set_operations_and_top() -> None:
    union = "SELECT a FROM t UNION ALL SELECT a FROM u"
    assert MSSQL.limit(union, 4) == f"SELECT TOP (4) * FROM (\n{union}\n) AS [dvf_limit]"
    ordered = f"{union} ORDER BY a"
    assert MSSQL.limit(ordered, 4) == f"{ordered}\nOFFSET 0 ROWS FETCH NEXT 4 ROWS ONLY"
    assert MSSQL.limit(ordered, 0).startswith("SELECT TOP (0) * FROM (")
    already = "SELECT TOP (10) a FROM t"
    assert MSSQL.limit(already, 2) == f"SELECT TOP (2) * FROM (\n{already}\n) AS [dvf_limit]"
    cte_union = "WITH x AS (SELECT 1 AS a) SELECT a FROM x UNION SELECT a FROM x"
    assert MSSQL.limit(cte_union, 2).endswith("ORDER BY 1 OFFSET 0 ROWS FETCH NEXT 2 ROWS ONLY")
    with pytest.raises(UnsupportedFeatureError):
        MSSQL.limit(cte_union, 0)


def test_limits_and_subqueries_per_dialect() -> None:
    assert MYSQL.limit("SELECT 1", 3) == "SELECT 1\nLIMIT 3"
    assert ORACLE.limit("SELECT 1 FROM DUAL", 3) == "SELECT 1 FROM DUAL\nFETCH FIRST 3 ROWS ONLY"
    assert ORACLE.subquery("SELECT 1 FROM DUAL", "q") == '(\nSELECT 1 FROM DUAL\n) "q"'
    assert MSSQL.subquery("SELECT 1", "q") == "(\nSELECT 1\n) AS [q]"
    assert MYSQL.subquery("SELECT 1", "q") == "(\nSELECT 1\n) AS `q`"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM (SELECT 1 FROM DUAL) AS q", "SELECT * FROM (SELECT 1 FROM DUAL) q"),
        (
            "SELECT COUNT(*) AS n FROM (\nSELECT a FROM t\n) as dvf_dups",
            "SELECT COUNT(*) AS n FROM (\nSELECT a FROM t\n) dvf_dups",
        ),
        (
            "WITH x AS (SELECT * FROM (SELECT 1 AS a FROM DUAL) AS i) SELECT * FROM x",
            "WITH x AS (SELECT * FROM (SELECT 1 AS a FROM DUAL) i) SELECT * FROM x",
        ),
        (
            "SELECT (SELECT COUNT(*) FROM t) AS total FROM DUAL",
            "SELECT (SELECT COUNT(*) FROM t) total FROM DUAL",
        ),
        (
            'SELECT * FROM (SELECT 1 FROM DUAL) AS "Quoted"',
            'SELECT * FROM (SELECT 1 FROM DUAL) "Quoted"',
        ),
        # Untouched: CAST's AS, strings, comments, quoted identifiers, non-query parentheses.
        (
            "SELECT CAST((SELECT MAX(a) FROM t) AS NUMBER) AS m FROM DUAL",
            "SELECT CAST((SELECT MAX(a) FROM t) AS NUMBER) AS m FROM DUAL",
        ),
        ("SELECT ') AS x' AS s FROM DUAL", "SELECT ') AS x' AS s FROM DUAL"),
        (
            "SELECT 1 FROM t -- (SELECT 1) AS x\nWHERE a = 1",
            "SELECT 1 FROM t -- (SELECT 1) AS x\nWHERE a = 1",
        ),
        ("SELECT COALESCE((SELECT 1 FROM DUAL), 0) AS c FROM DUAL", None),
        ("SELECT (a + b) AS c FROM t", None),
    ],
)
def test_strip_subquery_alias_as(sql: str, expected: str | None) -> None:
    rewritten = oracle_sql.strip_subquery_alias_as(sql)
    assert rewritten == (sql if expected is None else expected)
    assert oracle_sql.strip_subquery_alias_as(rewritten) == rewritten


# ====================================================================== text functions


def test_text_functions_use_binary_collations() -> None:
    assert MYSQL.to_text("c") == "(CONVERT(c USING utf8mb4) COLLATE utf8mb4_0900_bin)"
    assert "COLLATE Latin1_General_100_BIN2" in MSSQL.to_text("c")
    assert MSSQL.length("c").startswith("(LEN((CAST(c AS NVARCHAR(MAX)) + N'.')")
    assert MYSQL.length("c") == "CHAR_LENGTH(c)"
    assert ORACLE.length("c") == "LENGTH4(c)"


def test_case_fixups() -> None:
    upper = dict(mssql_sql.case_fixups(mssql_sql.GROWING_UPPER, upper=True))
    assert upper["ß"] == "ẞ"
    assert upper["ɐ"] == "Ɐ"
    lower = dict(mysql_sql.case_fixups(mysql_sql.GROWING_LOWER, upper=False))
    assert lower == {"Ⱥ": "ⱥ", "Ⱦ": "ⱦ"}
    assert (
        "REPLACE(" in MYSQL.upper("c") and "N'ẞ'" in MSSQL.upper("c") and "'ẞ'" in ORACLE.upper("c")
    )


def test_trim_variants() -> None:
    assert (
        MYSQL.trim("c", "rtrim")
        == "TRIM(TRAILING ' ' FROM (CONVERT(c USING utf8mb4) COLLATE utf8mb4_0900_bin))"
    )
    assert "REGEXP_REPLACE" in MYSQL.trim("c", "both", "0x")
    assert MSSQL.trim("c", "ltrim").startswith("LTRIM(")
    assert "PATINDEX(N'%[^0]%'" in MSSQL.trim("c", "ltrim", "0")
    assert "REVERSE(" in MSSQL.trim("c", "rtrim", "0")
    assert ORACLE.trim("c", "both", "0") == "LTRIM(RTRIM(c, '0'), '0')"
    with pytest.raises(UnsupportedFeatureError):
        MSSQL.trim("c", "both", "]")
    with pytest.raises(UnsupportedFeatureError):
        MSSQL.trim("c", "middle")


def test_unicode_and_regex_support() -> None:
    for form in ("NFC", "NFD", "NFKC", "NFKD"):
        assert not MYSQL.supports_unicode_form(form)
        assert not MSSQL.supports_unicode_form(form)
        assert ORACLE.supports_unicode_form(form)
    assert ORACLE.unicode_normalize("c", "NFC") == "COMPOSE(DECOMPOSE(c, 'CANONICAL'))"
    with pytest.raises(UnsupportedFeatureError):
        MYSQL.unicode_normalize("c", "NFC")
    with pytest.raises(UnsupportedFeatureError, match="2025"):
        MSSQL.regex_match("c", "a+", full=True)
    pattern = rb"\A(?:a+)\z".hex()  # backslashes make it a hexadecimal literal
    assert MYSQL.regex_match("c", "a+", full=True).endswith(f"_utf8mb4 X'{pattern}', 'c')")
    assert "REGEXP_INSTR" in ORACLE.regex_match("c", "a+", full=True)
    assert ORACLE.regex_match("c", "a+", full=False) == "REGEXP_LIKE(c, 'a+')"


def test_canonical_text_skips_unicode_normalization_without_support() -> None:
    spec = CanonSpec(kind=Kind.TEXT, logical=LogicalType.STRING, strings=StringOptions())
    assert canonical_sql("c", spec, MYSQL) == "c"
    assert canonical_sql("c", spec, MSSQL) == "c"
    assert canonical_sql("c", spec, ORACLE) == "COMPOSE(DECOMPOSE(c, 'CANONICAL'))"


def test_hashing_sql() -> None:
    assert ORACLE.md5("''") == "'d41d8cd98f00b204e9800998ecf8427e'"
    assert "STANDARD_HASH(TO_CHAR(c), 'MD5')" in ORACLE.md5("c")
    assert "HASHBYTES('MD5'" in MSSQL.md5("c") and mssql_sql.UTF8_COLLATION in MSSQL.md5("c")
    assert MYSQL.md5("c") == "MD5(CONVERT(c USING utf8mb4))"
    assert MYSQL.hash_int("h", 8) == "CAST(CONV(SUBSTRING(h, 9, 8), 16, 10) AS UNSIGNED)"
    assert MSSQL.hash_int("h", 0) == "CAST(CONVERT(BINARY(4), SUBSTRING(h, 1, 8), 2) AS BIGINT)"
    assert ORACLE.hash_int("h", 24) == "TO_NUMBER(SUBSTR(h, 25, 8), 'XXXXXXXX')"
    assert MYSQL.bucket("x", 16) == "(((x) * 16) DIV 4294967296)"
    assert MSSQL.bucket("x", 16) == "(CAST((x) * 16 AS BIGINT) / 4294967296)"
    assert ORACLE.bucket("x", 16) == "FLOOR(((x) * 16) / 4294967296)"
    assert MSSQL.concat("a") == "a"
    assert MSSQL.concat("a", "b").startswith("CONCAT(CAST(a AS NVARCHAR(MAX)), b")
    assert MYSQL.concat("a", "b") == "CONCAT(a, b)"
    assert row_hash_sql(["c"], ORACLE).startswith("LOWER(RAWTOHEX(STANDARD_HASH(TO_CHAR((")


def test_comparison_helpers() -> None:
    assert MYSQL.is_distinct("a", "b").startswith("(NOT (CAST(CONVERT(a USING utf8mb4) AS BINARY)")
    assert "DATALENGTH" in MSSQL.is_distinct("a", "b")
    assert ORACLE.is_distinct("a", "b").startswith("(DECODE(")
    assert MSSQL.greatest("a", "b") == "(SELECT MAX(dvf_v) FROM (VALUES (a), (b)) AS dvf_g(dvf_v))"
    for dialect in (MYSQL, MSSQL, ORACLE):
        assert dialect.boolean("x > 1").startswith("(CASE WHEN x > 1 THEN 1")


def test_temporal_sql() -> None:
    assert MYSQL.to_utc("c", tz_aware=False, timezone="Europe/Berlin") == (
        "CONVERT_TZ(CAST(c AS DATETIME(6)), 'Europe/Berlin', '+00:00')"
    )
    assert "AT TIME ZONE N'W. Europe Standard Time'" in MSSQL.to_utc(
        "c", tz_aware=False, timezone="Europe/Berlin"
    )
    assert "SWITCHOFFSET" in MSSQL.to_utc("c", tz_aware=True, timezone="UTC")
    assert "SQL_VARIANT_PROPERTY" in MSSQL.to_utc("c", tz_aware=False, timezone="UTC")
    assert "FROM_TZ" in ORACLE.to_utc("c", tz_aware=False, timezone="Asia/Kolkata")
    assert ORACLE.to_utc("c", tz_aware=True, timezone="UTC") == (
        "CAST(SYS_EXTRACT_UTC(c) AS TIMESTAMP(9))"
    )
    assert MSSQL.truncate_timestamp("c", "minute") == "DATETRUNC(minute, c)"
    assert MYSQL.truncate_timestamp("c", "hour") == (
        "CAST(DATE_FORMAT(c, '%Y-%m-%d %H:00:00') AS DATETIME(6))"
    )
    assert "NUMTODSINTERVAL" in ORACLE.truncate_timestamp("c", "second")
    assert ORACLE.decimal_text("c", 2).endswith("'FM" + "9" * 37 + "0.00')")
    assert ORACLE.decimal_text("c", 0).endswith("'FM" + "9" * 37 + "0')")


def test_windows_zone_mapping() -> None:
    assert mssql_sql.windows_zone("America/New_York") == "Eastern Standard Time"
    assert mssql_sql.windows_zone("UTC") == "UTC"
    with pytest.raises(UnsupportedFeatureError, match="register_windows_zone"):
        mssql_sql.windows_zone("Mars/Olympus_Mons")
    mssql_sql.register_windows_zone("Test/Zone", "UTC")
    try:
        assert mssql_sql.windows_zone("Test/Zone") == "UTC"
    finally:
        del mssql_sql.WINDOWS_ZONES["Test/Zone"]


# ====================================================================== URLs and settings


def test_mysql_url_parsing() -> None:
    kwargs = mysql_conn.parse_url(
        "mysql+pymysql://app%40corp:p%3Ass@db.example:3307/sales?ssl_disabled=true&read_timeout=30&x=y"
    )
    assert kwargs == {
        "host": "db.example",
        "port": 3307,
        "user": "app@corp",
        "password": "p:ss",
        "database": "sales",
        "ssl_disabled": True,
        "read_timeout": 30,
        "x": "y",
    }
    assert mysql_conn.parse_url("mariadb://h/db")["port"] == 3306
    with pytest.raises(ConnectorError, match="scheme"):
        mysql_conn.parse_url("postgres://h/db")
    with pytest.raises(ConnectorError, match="port"):
        mysql_conn.parse_url("mysql://h:notaport/db")


def test_mysql_connect_args() -> None:
    connector = mysql_conn.MySQLConnector(
        "m",
        _config(
            "mysql", host="h", user="u", password="pw", schema="s", options={"ssl_disabled": True}
        ),
        Path(),
    )
    args = connector.connect_args()
    assert args == {
        "host": "h",
        "port": 3306,
        "database": "s",
        "user": "u",
        "password": "pw",
        "connect_timeout": 15,
        "ssl_disabled": True,
        "charset": "utf8mb4",
        "autocommit": True,
    }
    from_url = mysql_conn.MySQLConnector(
        "m", _config("mysql", url="mysql://a:b@h:1/d", database="other"), Path()
    ).connect_args()
    assert from_url["database"] == "other" and from_url["password"] == "b"


def test_mysql_split_table() -> None:
    assert mysql_conn.split_table("t") == (None, "t")
    assert mysql_conn.split_table("db.t") == ("db", "t")
    assert mysql_conn.split_table("`my db`.`t``x`") == ("my db", "t`x")
    with pytest.raises(ConnectorError):
        mysql_conn.split_table("a.b.c")


def test_mssql_url_and_connection_string() -> None:
    settings = mssql_conn.parse_url(
        "mssql+pyodbc://sa:p%3Bw%7D@127.0.0.1:1433/master?TrustServerCertificate=yes&Encrypt=yes"
    )
    assert settings["password"] == "p;w}"
    assert settings["options"] == {"TrustServerCertificate": "yes", "Encrypt": "yes"}
    text = mssql_conn.build_connection_string(
        settings["host"],
        settings["port"],
        settings["database"],
        settings["user"],
        settings["password"],
        {**settings["options"], "login_timeout": 5, "ApplicationIntent": "Read Only"},
    )
    assert text == (
        "Server=tcp:127.0.0.1,1433;Database=master;UID=sa;PWD={p;w}}};"
        "TrustServerCertificate=yes;Encrypt=yes;ApplicationIntent={Read Only}"
    )
    assert mssql_conn.build_connection_string("srv\\inst", None, None, None, None, {}) == (
        "Server=tcp:srv\\inst"
    )
    assert mssql_conn.quote_value(True) == "yes"
    with pytest.raises(ConnectorError, match="scheme"):
        mssql_conn.parse_url("mysql://h/db")


def test_mssql_login_timeout_option() -> None:
    connector = mssql_conn.MSSQLConnector(
        "s", _config("mssql", host="h", options={"login_timeout": "7"}), Path()
    )
    assert connector._login_timeout() == 7
    assert "login_timeout" not in connector.connection_string()


def test_oracle_url_and_connect_args() -> None:
    settings = oracle_conn.parse_url(
        "oracle+oracledb://scott:t%40ger@db:1522/ORCLPDB1?expire_time=2"
    )
    assert settings == {
        "host": "db",
        "port": 1522,
        "user": "scott",
        "password": "t@ger",
        "service_name": "ORCLPDB1",
        "expire_time": 2,
    }
    connector = oracle_conn.OracleConnector(
        "o", _config("oracle", url="oracle://u:p@h/XEPDB1", options={"sid": "XE"}), Path()
    )
    args = connector.connect_args()
    assert args["sid"] == "XE" and "service_name" not in args
    with pytest.raises(ConnectorError, match="scheme"):
        oracle_conn.parse_url("mssql://h/db")


def test_oracle_names() -> None:
    assert oracle_conn.split_name("customers", "APP") == ("APP", "CUSTOMERS")
    assert oracle_conn.split_name('hr."Mixed"', "APP") == ("HR", "Mixed")
    assert oracle_conn.result_name("K0") == "k0"
    assert oracle_conn.result_name("customer_id") == "customer_id"
    assert oracle_conn.result_name("Mixed") == "Mixed"
    with pytest.raises(ConnectorError):
        oracle_conn.split_name("a.b.c", None)


def test_as_of_is_utc() -> None:
    assert mssql_conn.as_of_utc("2024-06-30T23:59:59Z") == "2024-06-30T23:59:59.000000"
    assert mssql_conn.as_of_utc("2024-07-01T01:00:00+02:00") == "2024-06-30T23:00:00.000000"
    with pytest.raises(ConnectorError, match="ISO 8601"):
        mssql_conn.as_of_utc("yesterday")


# ====================================================================== resolve


def test_resolve_relations() -> None:
    mysql = mysql_conn.MySQLConnector("m", _config("mysql", host="h"), Path())
    rel = mysql.resolve(DatasetSpec(table="db.t", filter="a > 1"))
    assert rel.sql == "(SELECT * FROM db.t WHERE a > 1) AS dvf_filtered" and rel.table == "db.t"
    with pytest.raises(ConnectorError, match="time travel"):
        mysql.resolve(DatasetSpec(table="t", as_of="2024-01-01"))
    with pytest.raises(ConnectorError, match="path"):
        mysql.resolve(DatasetSpec(path="x.csv"))

    mssql = mssql_conn.MSSQLConnector("s", _config("mssql", host="h"), Path())
    temporal = mssql.resolve(DatasetSpec(table="dbo.t", as_of="2024-01-01T00:00:00Z"))
    assert temporal.base_sql == "dbo.t FOR SYSTEM_TIME AS OF '2024-01-01T00:00:00.000000'"
    query = mssql.resolve(DatasetSpec(query="SELECT 1 AS a"))
    assert query.sql == "(\nSELECT 1 AS a\n) AS dvf_query" and query.table is None
    with pytest.raises(ConnectorError, match="tables"):
        mssql.resolve(DatasetSpec(query="SELECT 1", as_of="2024-01-01"))

    oracle = oracle_conn.OracleConnector("o", _config("oracle", host="h"), Path())
    flashback = oracle.resolve(DatasetSpec(table="T", as_of="2024-01-01T00:00:00", filter="x = 1"))
    assert flashback.base_sql == "T AS OF TIMESTAMP TIMESTAMP '2024-01-01 00:00:00.000000 +00:00'"
    assert flashback.sql.endswith('"dvf_filtered"') and " AS dvf" not in flashback.sql
    assert oracle.resolve(DatasetSpec(query="SELECT 1 FROM DUAL")).sql.endswith('"dvf_query"')


# ====================================================================== missing drivers


@pytest.mark.parametrize(
    ("module", "connector", "config", "extra"),
    [
        ("pymysql", mysql_conn.MySQLConnector, {"type": "mysql", "host": "h"}, "mysql"),
        ("mssql_python", mssql_conn.MSSQLConnector, {"type": "mssql", "host": "h"}, "mssql"),
        ("oracledb", oracle_conn.OracleConnector, {"type": "oracle", "host": "h"}, "oracle"),
    ],
)
def test_missing_driver_raises_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    connector: type,
    config: dict[str, Any],
    extra: str,
) -> None:
    monkeypatch.setitem(sys.modules, module, None)
    instance = connector("c", DatabaseConnection.model_validate(config), Path())
    with pytest.raises(DependencyError) as info:
        instance.open()
    assert info.value.extra == extra
    assert f"dvf-framework[{extra}]" in (info.value.hint or "")


# ====================================================================== fake drivers


class FakeError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description: list[tuple[Any, ...]] | None = None
        self._rows: list[tuple[Any, ...]] = []
        self.arraysize = 100
        self.prefetchrows = 2

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.executed.append(sql)
        for needle, response in self.connection.responses:
            if needle in sql:
                if isinstance(response, Exception):
                    raise response
                names, rows = response
                self.description = [(n, None, None, None, None, None, True) for n in names] or None
                self._rows = list(rows)
                return
        self.description = None
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, responses: list[tuple[str, Any]]) -> None:
        self.responses = responses
        self.executed: list[str] = []
        self.version = "23.4.0.0.0"
        self.outputtypehandler: Callable[..., Any] | None = None

    def cursor(self, *args: Any) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        pass


def _fake_module(name: str, connect: Callable[..., Any], **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.connect = connect  # type: ignore[attr-defined]
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_connect_errors_are_wrapped_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "S3cr3t-Pw"

    def fail(*args: Any, **kwargs: Any) -> None:
        raise FakeError(f"Login failed; connection string PWD={secret};")

    monkeypatch.setitem(
        sys.modules, "mssql_python", _fake_module("mssql_python", fail, Error=FakeError)
    )
    mssql = mssql_conn.MSSQLConnector("s", _config("mssql", host="h", password=secret), Path())
    with pytest.raises(ConnectorError) as info:
        mssql.open()
    assert secret not in str(info.value) and "***" in str(info.value)
    assert "TrustServerCertificate" in (info.value.hint or "")

    monkeypatch.setitem(
        sys.modules,
        "oracledb",
        _fake_module("oracledb", fail, Error=FakeError, DatabaseError=FakeError),
    )
    oracle = oracle_conn.OracleConnector("o", _config("oracle", host="h", password=secret), Path())
    with pytest.raises(ConnectorError) as info:
        oracle.open()
    assert secret not in str(info.value)

    def mysql_fail(**kwargs: Any) -> None:
        raise FakeError(1045, "Access denied for user 'u'@'h' (using password: YES)")

    monkeypatch.setitem(
        sys.modules, "pymysql", _fake_module("pymysql", mysql_fail, MySQLError=FakeError)
    )
    mysql = mysql_conn.MySQLConnector("m", _config("mysql", host="h", password=secret), Path())
    with pytest.raises(ConnectorError, match=r"\(1045\) Access denied") as info:
        mysql.open()
    assert secret not in str(info.value)


def test_query_errors_are_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "mssql_python",
        _fake_module("mssql_python", lambda *a, **k: None, Error=FakeError),
    )
    connector = mssql_conn.MSSQLConnector("s", _config("mssql", host="h"), Path())
    connector._con = FakeConnection([("SELECT", FakeError("Invalid object name 'x'"))])
    with pytest.raises(ConnectorError, match="Query failed on connection 's': Invalid object"):
        connector.query("SELECT * FROM x")
    with pytest.raises(ConnectorError):
        list(connector.iter_batches("SELECT * FROM x"))


def test_mssql_schema_from_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "mssql_python",
        _fake_module("mssql_python", lambda *a, **k: None, Error=FakeError),
    )
    connector = mssql_conn.MSSQLConnector("s", _config("mssql", host="h"), Path())
    connector._con = FakeConnection(
        [
            (
                "FROM sys.columns",
                (
                    ["name"] * 9,
                    [
                        ("id", "bigint", 8, 19, 0, False, None, 1, "Key"),
                        ("name", "nvarchar", 100, 0, 0, True, "(N'x')", 2, None),
                        ("notes", "nvarchar", -1, 0, 0, True, None, 3, None),
                        ("amount", "decimal", 9, 12, 3, True, None, 4, None),
                        ("at", "datetime2", 8, 27, 6, True, None, 5, None),
                        ("ratio", "float", 8, 53, 0, True, None, 6, None),
                    ],
                ),
            ),
            ("is_primary_key", (["name"], [("id",)])),
        ]
    )
    relation = connector.resolve(DatasetSpec(table="dbo.t"))
    schema = connector.get_schema(relation)
    assert [c.native_type for c in schema.columns] == [
        "bigint",
        "nvarchar(50)",
        "nvarchar(max)",
        "decimal(12,3)",
        "datetime2(6)",
        "float(53)",
    ]
    assert [c.type.logical for c in schema.columns] == [
        LogicalType.INTEGER,
        LogicalType.STRING,
        LogicalType.STRING,
        LogicalType.DECIMAL,
        LogicalType.TIMESTAMP,
        LogicalType.FLOAT,
    ]
    assert schema.columns[5].type.bits == 64
    assert schema.columns[0].nullable is False and schema.columns[0].comment == "Key"
    assert schema.columns[1].default == "(N'x')"
    assert schema.primary_key == ("id",)


def test_oracle_schema_from_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = oracle_conn.OracleConnector("o", _config("oracle", host="h"), Path())
    connector._con = FakeConnection(
        [
            (
                "FROM all_tab_columns",
                (
                    ["c"] * 11,
                    [
                        ("ID", "NUMBER", 22, 0, None, 18, 0, "N", None, 1, None),
                        ("NAME", "VARCHAR2", 400, 100, "C", None, None, "Y", "'x' ", 2, "Name"),
                        ("AMOUNT", "NUMBER", 22, 0, None, 12, 3, "Y", None, 3, None),
                        ("ANY_NUM", "NUMBER", 22, 0, None, None, None, "Y", None, 4, None),
                        ("CREATED", "DATE", 7, 0, None, None, None, "Y", None, 5, None),
                        ("BLOB_ID", "RAW", 16, 0, None, None, None, "Y", None, 6, None),
                    ],
                ),
            ),
            ("constraint_type = 'P'", (["c"], [("ID",)])),
        ]
    )
    connector._schema = "APP"
    schema = connector.get_schema(connector.resolve(DatasetSpec(table="t")))
    assert [c.native_type for c in schema.columns] == [
        "NUMBER(18,0)",
        "VARCHAR2(100 CHAR)",
        "NUMBER(12,3)",
        "NUMBER",
        "DATE",
        "RAW(16)",
    ]
    assert schema.columns[4].type.logical is LogicalType.TIMESTAMP  # Oracle DATE has a time part
    assert schema.columns[1].default == "'x'" and schema.columns[1].comment == "Name"
    assert schema.columns[0].nullable is False
    assert schema.primary_key == ("ID",)


def test_oracle_rewrites_and_lowercases_results() -> None:
    connector = oracle_conn.OracleConnector("o", _config("oracle", host="h"), Path())
    fake = FakeConnection([("dvf_q", (["K0", "Mixed", "c1"], [(1, 2, 3)]))])
    connector._con = fake
    table = connector.query("SELECT * FROM (SELECT 1 AS k0 FROM DUAL) AS dvf_q")
    assert table.column_names == ["k0", "Mixed", "c1"]
    assert fake.executed[-1] == "SELECT * FROM (SELECT 1 AS k0 FROM DUAL) dvf_q"
    batches = list(connector.iter_batches("SELECT * FROM (SELECT 1 FROM DUAL) AS dvf_q", 10))
    assert batches[0].schema.names == ["k0", "Mixed", "c1"]


def test_oracle_description_types() -> None:
    def desc(
        code: str, size: int | None = None, precision: int | None = None, scale: int | None = None
    ) -> tuple[Any, ...]:
        return (
            "c",
            types.SimpleNamespace(name=f"DB_TYPE_{code}"),
            None,
            size,
            precision,
            scale,
            True,
        )

    assert oracle_conn.native_type_from_description(desc("NUMBER", None, 0, -127)) == "NUMBER"
    assert oracle_conn.native_type_from_description(desc("NUMBER", None, 12, 3)) == "NUMBER(12,3)"
    assert oracle_conn.native_type_from_description(desc("VARCHAR", 40)) == "VARCHAR2(40)"
    assert oracle_conn.native_type_from_description(desc("TIMESTAMP_TZ", None, 0, 6)) == (
        "TIMESTAMP(6) WITH TIME ZONE"
    )
    assert oracle_conn.native_type_from_description(desc("BINARY_DOUBLE")) == "BINARY DOUBLE"


def test_mysql_description_types() -> None:
    assert (
        mysql_conn.native_type_from_description(("c", 246, None, 14, 14, 3, True))
        == "decimal(12,3)"
    )
    assert mysql_conn.native_type_from_description(("c", 253, None, 40, 40, 0, True)) == "varchar"
    assert (
        mysql_conn.native_type_from_description(("c", 253, None, 16, 16, 0, True), 63)
        == "varbinary"
    )
    assert (
        mysql_conn.native_type_from_description(("c", 12, None, 26, 26, 6, True)) == "datetime(6)"
    )


def test_mysql_schema_and_version_with_fake_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules, "pymysql", _fake_module("pymysql", lambda **k: None, MySQLError=FakeError)
    )
    connector = mysql_conn.MySQLConnector("m", _config("mysql", host="h"), Path())
    connector._con = FakeConnection(
        [
            ("VERSION()", (["v"], [("8.4.3-log",)])),
            (
                "information_schema.COLUMNS",
                (
                    ["c"] * 6,
                    [
                        ("id", "bigint", "NO", None, 1, ""),
                        ("amount", "decimal(12,3)", "YES", "0.000", 2, "money"),
                        ("at", "timestamp(6)", "YES", None, 3, ""),
                    ],
                ),
            ),
            ("PRIMARY KEY", (["c"], [("id",)])),
        ]
    )
    assert connector.server_version() == "MySQL 8.4.3"
    schema = connector.get_schema(connector.resolve(DatasetSpec(table="db.t")))
    assert [c.type.logical for c in schema.columns] == [
        LogicalType.INTEGER,
        LogicalType.DECIMAL,
        LogicalType.TIMESTAMP_TZ,
    ]
    assert schema.columns[1].default == "0.000" and schema.columns[1].comment == "money"
    assert schema.columns[0].nullable is False and schema.columns[0].comment is None
    assert schema.primary_key == ("id",)
