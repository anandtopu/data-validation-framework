"""The sap_hana connector and the HANA dialect.

There is no HANA here: a fake ``hdbcli`` module runs the connector's statements in DuckDB, with a
``SYS`` schema holding HANA-style catalog rows (``SYS.TABLE_COLUMNS``, ``SYS.CONSTRAINTS``, ...)
and SAP dictionary tables. Catalog and data statements are plain SQL that DuckDB also runs; the
HANA-specific SQL the dialect generates is checked by its shape. All data is synthetic.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf.config.connections import SapHanaConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.sap_hana import SapHanaConnector, hana_type, split_name
from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, LogicalType
from dvf.normalize.canonical import CanonSpec, Kind, canonical_sql
from dvf.packs.sap.drivers import SapDriverMissingError
from dvf.packs.sap.normalizers import SAP_NORMALIZERS
from dvf.plugins.registry import PluginKind, new_registry
from dvf.sql.dialects import UnsupportedFeatureError, get_dialect

_SETUP = """
CREATE SCHEMA SAPSR3;
CREATE TABLE SAPSR3.KNA1 (MANDT VARCHAR, KUNNR VARCHAR, NAME1 VARCHAR, ERDAT VARCHAR);
INSERT INTO SAPSR3.KNA1 VALUES
  ('100', '0000000001', 'One', '20240101'), ('100', '0000000002', 'Two', '00000000'),
  ('200', '0000000001', 'Other client', '20240101');
CREATE TABLE SAPSR3.TCURX (CURRKEY VARCHAR, CURRDEC INTEGER);
INSERT INTO SAPSR3.TCURX VALUES ('JPY', 0), ('KWD', 3);
CREATE TABLE SAPSR3.ZRCLNT (RCLNT VARCHAR, BUKRS VARCHAR, HSL DECIMAL(23, 2));
INSERT INTO SAPSR3.ZRCLNT VALUES ('100', '1000', 10.50), ('300', '1000', 99.00);
CREATE TABLE DUMMY (DUMMY VARCHAR);
INSERT INTO DUMMY VALUES ('X');
CREATE SCHEMA SYS;
CREATE TABLE SYS.M_DATABASE (VERSION VARCHAR);
INSERT INTO SYS.M_DATABASE VALUES ('2.00.075.00');
CREATE TABLE SYS.TABLE_COLUMNS (SCHEMA_NAME VARCHAR, TABLE_NAME VARCHAR, COLUMN_NAME VARCHAR,
  POSITION INTEGER, DATA_TYPE_NAME VARCHAR, LENGTH INTEGER, SCALE INTEGER, IS_NULLABLE VARCHAR,
  DEFAULT_VALUE VARCHAR, COMMENTS VARCHAR);
CREATE TABLE SYS.VIEW_COLUMNS (SCHEMA_NAME VARCHAR, VIEW_NAME VARCHAR, COLUMN_NAME VARCHAR,
  POSITION INTEGER, DATA_TYPE_NAME VARCHAR, LENGTH INTEGER, SCALE INTEGER, IS_NULLABLE VARCHAR,
  DEFAULT_VALUE VARCHAR, COMMENTS VARCHAR);
INSERT INTO SYS.TABLE_COLUMNS VALUES
  ('SAPSR3', 'KNA1', 'MANDT', 1, 'NVARCHAR', 3, NULL, 'FALSE', '000', NULL),
  ('SAPSR3', 'KNA1', 'KUNNR', 2, 'NVARCHAR', 10, NULL, 'FALSE', ' ', NULL),
  ('SAPSR3', 'KNA1', 'NAME1', 3, 'NVARCHAR', 35, NULL, 'FALSE', ' ', NULL),
  ('SAPSR3', 'KNA1', 'ERDAT', 4, 'NVARCHAR', 8, NULL, 'FALSE', '00000000', NULL),
  ('SAPSR3', 'TCURX', 'CURRKEY', 1, 'NVARCHAR', 5, NULL, 'FALSE', ' ', NULL),
  ('SAPSR3', 'TCURX', 'CURRDEC', 2, 'TINYINT', 3, NULL, 'FALSE', '0', NULL),
  ('SAPSR3', 'ZRCLNT', 'RCLNT', 1, 'NVARCHAR', 3, NULL, 'FALSE', '000', NULL),
  ('SAPSR3', 'ZRCLNT', 'BUKRS', 2, 'NVARCHAR', 4, NULL, 'FALSE', ' ', NULL),
  ('SAPSR3', 'ZRCLNT', 'HSL', 3, 'DECIMAL', 23, 2, 'FALSE', '0', NULL);
INSERT INTO SYS.VIEW_COLUMNS VALUES
  ('SAPSR3', 'ZV_KNA1', 'KUNNR', 1, 'NVARCHAR', 10, NULL, 'TRUE', NULL, 'Customer');
CREATE TABLE SYS.CONSTRAINTS (SCHEMA_NAME VARCHAR, TABLE_NAME VARCHAR, COLUMN_NAME VARCHAR,
  POSITION INTEGER, IS_PRIMARY_KEY VARCHAR);
INSERT INTO SYS.CONSTRAINTS VALUES
  ('SAPSR3', 'KNA1', 'MANDT', 1, 'TRUE'), ('SAPSR3', 'KNA1', 'KUNNR', 2, 'TRUE');
CREATE TABLE SYS.TABLES (SCHEMA_NAME VARCHAR, TABLE_NAME VARCHAR);
INSERT INTO SYS.TABLES VALUES ('SAPSR3', 'KNA1'), ('SAPSR3', 'TCURX'), ('SAPSR3', 'ZRCLNT');
CREATE TABLE SYS.VIEWS (SCHEMA_NAME VARCHAR, VIEW_NAME VARCHAR);
INSERT INTO SYS.VIEWS VALUES ('SAPSR3', 'ZV_KNA1');
CREATE TABLE SAPSR3.DD02L (TABNAME VARCHAR, AS4LOCAL VARCHAR, TABCLASS VARCHAR, CLIDEP VARCHAR,
  CONTFLAG VARCHAR);
INSERT INTO SAPSR3.DD02L VALUES ('KNA1', 'A', 'TRANSP', 'X', 'A'), ('TCURX', 'A', 'TRANSP', '', 'C');
CREATE TABLE SAPSR3.DD02T (TABNAME VARCHAR, AS4LOCAL VARCHAR, DDLANGUAGE VARCHAR, DDTEXT VARCHAR);
INSERT INTO SAPSR3.DD02T VALUES ('KNA1', 'A', 'E', 'General Data in Customer Master');
CREATE TABLE SAPSR3.DD03L (TABNAME VARCHAR, FIELDNAME VARCHAR, AS4LOCAL VARCHAR, AS4VERS VARCHAR,
  POSITION VARCHAR, KEYFLAG VARCHAR, DATATYPE VARCHAR, LENG VARCHAR, DECIMALS VARCHAR,
  ROLLNAME VARCHAR, DOMNAME VARCHAR, "NOTNULL" VARCHAR, REFTABLE VARCHAR, REFFIELD VARCHAR,
  CHECKTABLE VARCHAR);
INSERT INTO SAPSR3.DD03L VALUES
  ('KNA1', 'MANDT', 'A', '0000', '0001', 'X', 'CLNT', '000003', '000000', 'MANDT', 'MANDT', 'X', '', '', 'T000'),
  ('KNA1', 'KUNNR', 'A', '0000', '0002', 'X', 'CHAR', '000010', '000000', 'KUNNR', 'KUNNR', 'X', '', '', ''),
  ('KNA1', 'NAME1', 'A', '0000', '0003', '', '', '000000', '000000', 'NAME1_GP', '', '', '', '', ''),
  ('KNA1', '.INCLUDE', 'A', '0000', '0004', '', '', '000000', '000000', '', '', '', '', '', ''),
  ('KNA1', 'ERDAT', 'A', '0000', '0005', '', 'DATS', '000008', '000000', 'ERDAT', 'DATUM', '', '', '', ''),
  ('TCURX', 'CURRKEY', 'A', '0000', '0001', 'X', 'CUKY', '000005', '000000', 'CURRKEY', '', 'X', '', '', ''),
  ('TCURX', 'CURRDEC', 'A', '0000', '0002', '', 'INT1', '000003', '000000', 'CURRDEC', '', '', '', '', '');
CREATE TABLE SAPSR3.DD04L (ROLLNAME VARCHAR, AS4LOCAL VARCHAR, DOMNAME VARCHAR, DATATYPE VARCHAR,
  LENG VARCHAR, DECIMALS VARCHAR);
INSERT INTO SAPSR3.DD04L VALUES ('NAME1_GP', 'A', 'NAME', 'CHAR', '000035', '000000');
CREATE TABLE SAPSR3.DD04T (ROLLNAME VARCHAR, DDLANGUAGE VARCHAR, AS4LOCAL VARCHAR, DDTEXT VARCHAR);
INSERT INTO SAPSR3.DD04T VALUES ('KUNNR', 'E', 'A', 'Customer Number'), ('NAME1_GP', 'E', 'A', 'Name'),
  ('KUNNR', 'D', 'A', 'Kundennummer');
CREATE TABLE SAPSR3.DD08L (TABNAME VARCHAR, FIELDNAME VARCHAR, AS4LOCAL VARCHAR, CHECKTABLE VARCHAR,
  FRKART VARCHAR, CARD VARCHAR, CARDLEFT VARCHAR);
INSERT INTO SAPSR3.DD08L VALUES ('KNA1', 'MANDT', 'A', 'T000', 'KEY', 'CN', '1');
CREATE TABLE SAPSR3.DD05S (TABNAME VARCHAR, FIELDNAME VARCHAR, AS4LOCAL VARCHAR, FORTABLE VARCHAR,
  FORKEY VARCHAR, CHECKTABLE VARCHAR, CHECKFIELD VARCHAR, PRIMPOS VARCHAR);
INSERT INTO SAPSR3.DD05S VALUES ('KNA1', 'MANDT', 'A', 'KNA1', 'MANDT', 'T000', 'MANDT', '0001');
"""


class FakeError(Exception):
    def __init__(self, message: str, errorcode: int = 0) -> None:
        super().__init__(message)
        self.errorcode = errorcode


class FakeCursor:
    def __init__(self, con: FakeConnection) -> None:
        self._con = con
        self._result: Any = None
        self.timeout: int | None = None

    def setquerytimeout(self, seconds: int) -> None:
        self.timeout = seconds
        self._con.timeouts.append(seconds)

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._con.statements.append(sql)
        try:
            self._result = self._con.db.execute(sql, list(params) if params else None)
        except duckdb.Error as exc:
            raise FakeError(str(exc), -10807 if "lost" in sql else 260) from exc

    @property
    def description(self) -> Any:
        return self._result.description if self._result is not None else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result.fetchall())

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return list(self._result.fetchmany(size))

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, db: duckdb.DuckDBPyConnection, kwargs: dict[str, Any]) -> None:
        self.db = db
        self.kwargs = kwargs
        self.statements: list[str] = []
        self.timeouts: list[int] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_hana(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[FakeConnection]]:
    db = duckdb.connect()
    db.execute(_SETUP)
    connections: list[FakeConnection] = []
    package = types.ModuleType("hdbcli")
    dbapi = types.ModuleType("hdbcli.dbapi")

    def connect(**kwargs: Any) -> FakeConnection:
        if kwargs.get("password") == "wrong":
            raise FakeError("authentication failed", 10)
        con = FakeConnection(db.cursor(), kwargs)
        connections.append(con)
        return con

    dbapi.connect = connect  # type: ignore[attr-defined]
    dbapi.Error = FakeError  # type: ignore[attr-defined]
    package.dbapi = dbapi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hdbcli", package)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", dbapi)
    yield connections
    db.close()


def hana(tmp_path: Path, **overrides: Any) -> SapHanaConnector:
    config = SapHanaConnection.model_validate(
        {
            "type": "sap_hana",
            "host": "hana.example.test",
            "port": 30215,
            "user": "DVF_READER",
            "password": "synthetic-not-a-secret",
            "schema": "SAPSR3",
            "client": "100",
            **overrides,
        }
    )
    return SapHanaConnector("hana", config, tmp_path)


# ---------------------------------------------------------------------- connector


def test_connects_with_hdbcli_parameters(fake_hana: list[FakeConnection], tmp_path: Path) -> None:
    con = hana(tmp_path, database_name="S4D", options={"compress": True}, query_timeout_s=30)
    report = con.test_connection()
    assert report.ok, report.message
    assert report.server_version == "SAP HANA 2.00.075.00"
    kwargs = fake_hana[0].kwargs
    assert kwargs["address"] == "hana.example.test" and kwargs["port"] == 30215
    assert kwargs["databaseName"] == "S4D" and kwargs["currentSchema"] == "SAPSR3"
    assert kwargs["encrypt"] is True and kwargs["compress"] is True
    assert any("FROM DUMMY" in s for s in fake_hana[0].statements)
    assert fake_hana[0].timeouts and set(fake_hana[0].timeouts) == {30}
    con.close()
    assert fake_hana[0].closed


def test_wrong_credentials_and_missing_driver(
    fake_hana: list[FakeConnection], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConnectorError, match="Could not connect to SAP HANA"):
        hana(tmp_path, password="wrong").open()
    monkeypatch.setitem(sys.modules, "hdbcli", None)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", None)
    with pytest.raises(SapDriverMissingError) as raised:
        hana(tmp_path).open()
    assert "pip install hdbcli" in (raised.value.hint or "")


def test_client_column_is_detected_and_filtered(
    fake_hana: list[FakeConnection], tmp_path: Path
) -> None:
    con = hana(tmp_path)
    try:
        kna1 = con.resolve(DatasetSpec(table="KNA1"))
        assert "\"MANDT\" = '100'" in kna1.base_sql and kna1.table == '"SAPSR3"."KNA1"'
        assert con.query(f"SELECT COUNT(*) AS n FROM {kna1.sql}").to_pylist() == [{"n": 2}]
        rclnt = con.resolve(DatasetSpec(table="SAPSR3.zrclnt", filter="BUKRS = '1000'"))
        assert "\"RCLNT\" = '100'" in rclnt.sql
        assert con.query(f"SELECT SUM(HSL) AS s FROM {rclnt.sql}").to_pylist()[0]["s"] == 10.5
        tcurx = con.resolve(DatasetSpec(table="TCURX"))
        assert tcurx.base_sql == '"SAPSR3"."TCURX"'  # client-independent
        off = con.resolve(DatasetSpec(table="KNA1", options={"client_filter": False}))
        assert con.query(f"SELECT COUNT(*) AS n FROM {off.sql}").to_pylist() == [{"n": 3}]
        query = con.resolve(DatasetSpec(query="SELECT * FROM SAPSR3.KNA1"))
        assert "MANDT" not in query.sql  # queries are not rewritten
    finally:
        con.close()
    explicit = hana(tmp_path, client_column="RCLNT")
    try:
        assert "MANDT" not in explicit.resolve(DatasetSpec(table="KNA1")).sql
        assert "RCLNT" in explicit.resolve(DatasetSpec(table="ZRCLNT")).sql
    finally:
        explicit.close()


def test_schema_and_primary_key_from_the_catalog(
    fake_hana: list[FakeConnection], tmp_path: Path
) -> None:
    con = hana(tmp_path)
    try:
        rel = con.resolve(DatasetSpec(table="KNA1"))
        schema = con.get_schema(rel)
        assert schema.primary_key == ("MANDT", "KUNNR")
        assert [c.native_type for c in schema.columns] == [
            "NVARCHAR(3)",
            "NVARCHAR(10)",
            "NVARCHAR(35)",
            "NVARCHAR(8)",
        ]
        assert schema.columns[1].type == CanonicalType.string(10)
        assert con.exists(rel)
        assert not con.exists(con.resolve(DatasetSpec(table="NOPE")))
        with pytest.raises(ConnectorError, match="not found"):
            con.get_schema(con.resolve(DatasetSpec(table="NOPE")))
        view = con.get_schema(con.resolve(DatasetSpec(table="ZV_KNA1")))
        assert view.columns[0].comment == "Customer"
        assert con.list_datasets("Z*") == ["ZRCLNT", "ZV_KNA1"]
        assert con.list_datasets("SAPSR3.T*") == ["SAPSR3.TCURX"]
        query = con.get_schema(con.resolve(DatasetSpec(query="SELECT KUNNR FROM SAPSR3.KNA1")))
        assert [c.name for c in query.columns] == ["KUNNR"]
    finally:
        con.close()


def test_ddic_schema_types_texts_and_client(
    fake_hana: list[FakeConnection], tmp_path: Path
) -> None:
    con = hana(tmp_path, ddic=True, client_column=None)
    try:
        rel = con.resolve(DatasetSpec(table="KNA1"))
        assert "\"MANDT\" = '100'" in rel.sql
        schema = con.get_schema(rel)
        native = {c.name: c.native_type for c in schema.columns}
        assert native == {
            "MANDT": "CLNT(3)",
            "KUNNR": "CHAR(10)",
            "NAME1": "CHAR(35)",  # type from DD04L: DD03L leaves it empty
            "ERDAT": "DATS(8)",
        }
        comments = {c.name: c.comment for c in schema.columns}
        assert comments["KUNNR"] == "Customer Number" and comments["NAME1"] == "Name"
        ddic = con.ddic_table("KNA1")
        assert ddic is not None and ddic.description == "General Data in Customer Master"
        assert ddic.foreign_keys[0].check_table == "T000"
        assert con.ddic_table("TCURX") is not None
        assert con.ddic_table("TCURX").client_column is None  # type: ignore[union-attr]
        assert con.ddic_table("NOPE") is None
    finally:
        con.close()


def test_transient_errors(tmp_path: Path) -> None:
    con = hana(tmp_path)
    lost = ConnectorError("query failed")
    lost.__cause__ = FakeError("connection lost", -10807)
    assert con.is_transient(lost)
    assert not con.is_transient(ConnectorError("syntax error"))


def test_split_name() -> None:
    assert split_name("kna1") == (None, "KNA1")
    assert split_name("sapsr3.KNA1") == ("SAPSR3", "KNA1")
    assert split_name('SAPSR3."/BIC/AZSALES00"') == ("SAPSR3", "/BIC/AZSALES00")
    assert split_name('"a""b"') == (None, 'a"b')
    with pytest.raises(ConnectorError):
        split_name("a.b.c")


def test_hana_types() -> None:
    assert hana_type("DECIMAL", 13, 2) == CanonicalType.decimal(13, 2)
    assert hana_type("DECIMAL", 34, None).scale is None
    assert hana_type("NVARCHAR", 10) == CanonicalType.string(10)
    assert hana_type("TINYINT").unsigned
    assert hana_type("SECONDDATE") == CanonicalType.timestamp(0)
    assert hana_type("TIMESTAMP").precision == 7
    assert hana_type("ST_GEOMETRY").logical is LogicalType.SPATIAL
    assert hana_type("BOOLEAN").logical is LogicalType.BOOLEAN
    assert hana_type("SOMETHING").logical is LogicalType.UNKNOWN


def test_registered_as_beta() -> None:
    registry = new_registry()
    for name in ("sap_hana", "sap_rfc"):
        entry = registry.get(PluginKind.CONNECTOR, name)
        assert entry.status == "beta", name


# ---------------------------------------------------------------------- dialect

D = get_dialect("hana")


def test_dialect_basics() -> None:
    assert D.name == "hana"
    assert D.quote('a"b') == '"a""b"'
    assert D.literal("abc") == "'abc'" and D.literal("é") == "N'é'"
    assert D.literal("it's") == "'it''s'"
    assert D.md5("x") == "LOWER(BINTOHEX(HASH_MD5(TO_BINARY(x))))"
    assert D.hash_int("h", 8) == "HEXTONUM(SUBSTRING(h, 9, 8))"
    assert D.limit("SELECT 1 FROM DUMMY", 5).endswith("LIMIT 5")
    assert D.trim("x", "both", "0123456789") == "LTRIM(RTRIM(x, '0123456789'), '0123456789')"
    assert D.trim("x", "rtrim") == "RTRIM(x, ' ')"
    assert D.regex_match("x", "[0-9]+", full=True) == "(x LIKE_REGEXPR '^(?:[0-9]+)\\z')"
    assert D.boolean("a = b").startswith("(CASE WHEN a = b THEN TRUE")
    assert D.boolean_text("b") == "(CASE WHEN b = TRUE THEN '1' WHEN b = FALSE THEN '0' END)"
    assert D.is_distinct("a", "b").endswith("= 1)")
    assert D.date_from_iso("t") == "TO_DATE(t, 'YYYY-MM-DD')"
    assert D.time_from_iso("t") == "TO_TIME(t, 'HH24:MI:SS')"
    assert D.substring("t", 1, 4) == "SUBSTRING(t, 1, 4)"
    assert "REPLACE_REGEXPR" in D.collapse_whitespace("x")
    assert not D.supports_unicode_form("NFC")
    with pytest.raises(UnsupportedFeatureError):
        D.unicode_normalize("x", "NFC")
    assert "ROUND_HALF_UP" in D.round_decimal("x", 2)
    assert "LOCALTOUTC" in D.to_utc("ts", tz_aware=False, timezone="Europe/Berlin")
    assert "NANO100_BETWEEN" in D.epoch_us("ts", tz_aware=False, timezone="UTC")
    assert D.current_timestamp_utc() == "CURRENT_UTCTIMESTAMP"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (Kind.DATE, "TO_NVARCHAR(CAST(v AS DATE), 'YYYY-MM-DD')"),
        (Kind.TIME, "LEFT(TO_NVARCHAR(CAST(v AS TIME), 'HH24:MI:SS') || '.000000', 15)"),
        (Kind.BINARY, "LOWER(BINTOHEX(v))"),
    ],
)
def test_dialect_canonical_forms(kind: Kind, expected: str) -> None:
    spec = CanonSpec(kind=kind, logical=LogicalType.STRING)
    assert canonical_sql("v", spec, D) == expected


def test_dialect_timestamp_and_decimal_text() -> None:
    ts = D.timestamp_text("v", precision="ms", tz_aware=False, timezone="UTC", from_date=False)
    assert (
        ts
        == "LEFT(REPLACE(TO_NVARCHAR(CAST(v AS TIMESTAMP), 'YYYY-MM-DD HH24:MI:SS.FF6'), ' ', 'T'), 23)"
    )
    assert "ABS(" in D.decimal_text("v", 2)
    assert D.truncate_timestamp("v", "minute") == (
        "TO_TIMESTAMP(TO_NVARCHAR(v, 'YYYY-MM-DD HH24:MI'), 'YYYY-MM-DD HH24:MI')"
    )


def test_sap_normalizers_on_hana() -> None:
    string = CanonicalType.string()
    dats = SAP_NORMALIZERS["sap_dats"]({}).to_sql('"ERDAT"', D, string)
    assert "TO_DATE(" in dats and "SUBSTRING(" in dats
    assert "LTRIM(RTRIM(" in dats  # digits check through a trim set
    alpha = SAP_NORMALIZERS["sap_alpha"]({}).to_sql('"KUNNR"', D, string)
    assert "LTRIM(" in alpha and "'0123456789'" in alpha
    flag = SAP_NORMALIZERS["sap_bool"]({}).to_sql('"XFLAG"', D, string)
    assert "THEN TRUE" in flag and "THEN FALSE" in flag
    curr = SAP_NORMALIZERS["sap_curr"]({"currency_column": "WAERS"})
    bound = curr.for_side("source")
    assert bound is not None
    sql = bound.to_sql('"WRBTR"', D, CanonicalType.decimal(13, 2))
    assert '"WAERS"' in sql and "* 100" in sql and "ROUND_HALF_UP" in sql
