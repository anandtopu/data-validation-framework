"""Parity of the SAP normalizers with their Python reference.

Every SAP normalizer runs on SAP-shaped values (leading zeros, blanks, ``00000000``, invalid
dates and times, lowercase flags, unknown currencies) in:

* DuckDB, SQLite, and Polars (always);
* PostgreSQL (``integration``; needs ``DVF_TEST_POSTGRES_URL``);
* a local Spark session with the ``spark`` and ``databricks`` dialects (``spark``);
* MySQL, SQL Server, and Oracle (``integration``; skipped unless ``DVF_TEST_MYSQL_URL``,
  ``DVF_TEST_MSSQL_URL``, or ``DVF_TEST_ORACLE_URL`` is set).

Each result goes through the canonical form of the normalizer's output type, as in a comparison,
and must equal the canonical form of the Python reference's output. Documented database
behavior is modelled, not hidden: Oracle stores ``''`` as NULL, and SQLite multiplies amounts in
floating point, so its currency cases use amounts of at most 15 significant digits after the
shift.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import Any

import duckdb
import polars as pl
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dvf.connectors.sqlite import register_functions
from dvf.core.types import CanonicalType, LogicalType
from dvf.normalize.canonical import CanonSpec, Kind, StringOptions, canonical_sql, canonical_text
from dvf.packs.sap.normalizers import SAP_NORMALIZERS, SapCurr, SapQuan
from dvf.packs.sap.tables import T002_DEFAULTS, TCURX_DEFAULTS
from dvf.plugins.base import INPUT_COLUMN_PREFIX, Normalizer
from dvf.sql.dialects import Dialect, get_dialect
from tests.server_backends import SERVERS, Backend, make_backend
from tests.unit.test_spark_support import PANDAS_WARNING, local_spark

PARITY = settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
LIVE = settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

STRING = CanonicalType.string()
INTEGER = CanonicalType.integer()
AMOUNT = CanonicalType.decimal(15, 2)
QUANTITY = CanonicalType.decimal(15, 3)

# ---------------------------------------------------------------------- SAP-shaped values

_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Co", "Cn"), blacklist_characters="\x00", max_codepoint=0xFFFF
    ),
    max_size=10,
)
_SPACES = st.sampled_from(["", " ", "  "])
_DIGITS = st.text(alphabet="0123456789", min_size=1, max_size=22)


def _padded(core: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.builds(lambda a, b, c: a + b + c, _SPACES, core, _SPACES)


KEY_EDGES = [
    "",
    " ",
    "0",
    "000",
    "0000012345",
    " 12345",
    "12345 ",
    "000000000000000000000000000000000000012345",
    "ABC-1",
    "12 34",
    "abc",
    "00A1",
    chr(0x0663),  # ARABIC-INDIC DIGIT THREE is a digit to Python, not to SAP
    chr(0xFF11) + chr(0xFF12),  # fullwidth digits
    "+12",
    "-12",
    "1234567890123456789012",
]
keys = st.one_of(st.sampled_from(KEY_EDGES), _padded(_DIGITS), _TEXT)


def _ymd(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"


DATS_EDGES = [
    "00000000",
    "",
    " ",
    "        ",
    "20240229",
    "20230229",
    "19000229",
    "20000229",
    "21000229",
    "99991231",
    "00010101",
    "00000101",
    "20231301",
    "20230001",
    "20230100",
    "20230431",
    "20230631",
    "2023011",
    "202301150",
    "2023-01-",
    "abcdefgh",
    "20230115  ",
    " 20230115",
    "2023011a",
]
dats = st.one_of(
    st.sampled_from(DATS_EDGES),
    st.dates(date(1, 1, 1), date(9999, 12, 31)).map(_ymd),
    st.text(alphabet="0123456789", min_size=8, max_size=8),
    _TEXT,
)
dats_int = st.one_of(
    st.sampled_from([0, 20240229, 19000229, -1, 101, 123456789, 99991231]),
    st.dates(date(1, 1, 1), date(9999, 12, 31)).map(lambda d: int(_ymd(d))),
    st.integers(-(10**9), 10**9),
)
TIMS_EDGES = [
    "000000",
    "240000",
    "235959",
    "236000",
    "125960",
    "",
    " ",
    "      ",
    "1234",
    "1234567",
    "12345a",
    " 123456",
    "123456 ",
]
tims = st.one_of(
    st.sampled_from(TIMS_EDGES),
    st.times().map(lambda t: f"{t.hour:02d}{t.minute:02d}{t.second:02d}"),
    st.text(alphabet="0123456789", min_size=6, max_size=6),
    _TEXT,
)
tims_int = st.one_of(
    st.sampled_from([0, 235959, -5, 1000000, 90000, 60]), st.integers(-(10**7), 10**7)
)
FLAG_EDGES = ["X", "x", " ", "", "  ", "X ", " X", "Y", "N", "1", "0", "true"]
flags = st.one_of(st.sampled_from(FLAG_EDGES), _TEXT)
_ISO = sorted(set(T002_DEFAULTS.values()))
LANGUAGE_EDGES = [*T002_DEFAULTS, *_ISO, *(c.lower() for c in _ISO), "Z", "Z1", "z1", "E ", ""]
languages = st.one_of(st.sampled_from(LANGUAGE_EDGES), _TEXT.filter(lambda t: len(t) <= 3))

_AMOUNTS = st.decimals(
    min_value=Decimal("-99999999999.99"),
    max_value=Decimal("99999999999.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
CURRENCY_EDGES = [*TCURX_DEFAULTS, "EUR", "USD", "", " ", "JPY ", "jpy", "XYZ", "CLF", "KWD  "]
currencies = st.one_of(
    st.none(), st.sampled_from(CURRENCY_EDGES), _TEXT.filter(lambda t: len(t) <= 5)
)
_QUANTITIES = st.decimals(
    min_value=Decimal("-9999999999.999"),
    max_value=Decimal("9999999999.999"),
    places=3,
    allow_nan=False,
    allow_infinity=False,
)
UNIT_EDGES = ["ST", "KG", "M3", "G", "", " ", "st", "XX", "KG "]
units = st.one_of(st.none(), st.sampled_from(UNIT_EDGES))


# ---------------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    """One parameterization of an SAP normalizer and the values it is tested on."""

    name: str
    params: dict[str, Any]
    ctype: CanonicalType
    values: st.SearchStrategy[Any]
    edges: tuple[Any, ...] = ()
    other: st.SearchStrategy[Any] | None = None
    """Values of the second column (currency or unit) the normalizer reads."""
    label: str = ""
    small_for_sqlite: bool = False
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def id(self) -> str:
        return f"{self.name}[{self.label or 'default'}]"

    @property
    def paired(self) -> bool:
        return self.other is not None


def _cases() -> dict[str, list[Case]]:
    cases: list[Case] = [
        Case("sap_alpha", {}, STRING, keys, tuple(KEY_EDGES), label="strip"),
        Case(
            "sap_alpha",
            {"mode": "pad", "length": 10},
            STRING,
            keys,
            tuple(KEY_EDGES),
            label="pad10",
        ),
        Case("sap_matnr_length", {}, STRING, keys, tuple(KEY_EDGES), label="18"),
        Case("sap_matnr_length", {"length": 40}, STRING, keys, tuple(KEY_EDGES), label="40"),
        Case("sap_matnr_length", {"mode": "strip"}, STRING, keys, tuple(KEY_EDGES), label="strip"),
        Case("sap_numc", {}, STRING, keys, tuple(KEY_EDGES), label="strip"),
        Case(
            "sap_numc",
            {"length": 6, "invalid": "null"},
            STRING,
            keys,
            tuple(KEY_EDGES),
            label="pad6",
        ),
        Case("sap_dats", {}, STRING, dats, tuple(DATS_EDGES), label="null"),
        Case(
            "sap_dats",
            {"invalid": "sentinel", "sentinel": "1900-01-01"},
            STRING,
            dats,
            tuple(DATS_EDGES),
            label="sentinel",
        ),
        Case("sap_dats", {}, INTEGER, dats_int, (0, 20240229, 19000229, -1), label="integer"),
        Case("sap_tims", {}, STRING, tims, tuple(TIMS_EDGES), label="zero_null"),
        Case(
            "sap_tims", {"zero_as_null": False}, STRING, tims, tuple(TIMS_EDGES), label="midnight"
        ),
        Case("sap_tims", {}, INTEGER, tims_int, (0, 235959, -5, 90000), label="integer"),
        Case("sap_bool", {}, STRING, flags, tuple(FLAG_EDGES), label="default"),
        Case(
            "sap_bool",
            {"other": "false", "null_as_false": True},
            STRING,
            flags,
            tuple(FLAG_EDGES),
            label="abap",
        ),
        Case(
            "sap_bool",
            {"true_values": ["X", "x", "Y"], "false_values": ["", "N"]},
            STRING,
            flags,
            tuple(FLAG_EDGES),
            label="lists",
        ),
        Case("sap_spras", {}, STRING, languages, tuple(LANGUAGE_EDGES), label="to_iso"),
        Case(
            "sap_spras",
            {"direction": "to_sap", "overrides": {"Z": "Z1"}},
            STRING,
            languages,
            tuple(LANGUAGE_EDGES),
            label="to_sap",
        ),
        Case(
            "sap_spras",
            {"unknown": "null", "overrides": {"Z": "Z1"}},
            STRING,
            languages,
            tuple(LANGUAGE_EDGES),
            label="unknown_null",
        ),
        Case(
            "sap_curr",
            {"currency_column": "c"},
            AMOUNT,
            _AMOUNTS,
            (Decimal("12.34"), Decimal("-0.05"), Decimal("1.00")),
            other=currencies,
            label="column",
            small_for_sqlite=True,
        ),
        Case(
            "sap_curr",
            {"currency_column": "c", "decimals": {"CLF": 4, "EUR": 2, "HUF": 0}},
            AMOUNT,
            _AMOUNTS,
            (Decimal("12.34"), Decimal("99999999999.99")),
            other=currencies,
            label="overrides",
            small_for_sqlite=True,
        ),
        Case(
            "sap_curr",
            {"currency": "JPY"},
            AMOUNT,
            _AMOUNTS,
            (Decimal("12.34"), Decimal("-0.01")),
            label="constant_jpy",
            small_for_sqlite=True,
        ),
        Case(
            "sap_curr",
            {"currency": "KWD"},
            AMOUNT,
            _AMOUNTS,
            (Decimal("12.34"), Decimal("-0.05")),
            label="constant_kwd",
            small_for_sqlite=True,
        ),
        Case(
            "sap_quan",
            {"unit_column": "c", "decimals": {"ST": 0, "KG": 3, "M3": 2, "G": 1}},
            QUANTITY,
            _QUANTITIES,
            (Decimal("1.5"), Decimal("-2.5"), Decimal("0.049")),
            other=units,
            label="column",
        ),
        Case(
            "sap_quan",
            {"unit_column": "c", "decimals": {"ST": 0, "KG": 2}, "default_decimals": 2},
            QUANTITY,
            _QUANTITIES,
            (Decimal("1.005"), Decimal("-1.005")),
            other=units,
            label="default",
        ),
        Case(
            "sap_quan",
            {"unit": "ST", "decimals": {"ST": 0}},
            QUANTITY,
            _QUANTITIES,
            (Decimal("0.5"), Decimal("-0.5")),
            label="constant",
        ),
    ]
    grouped: dict[str, list[Case]] = {}
    for case in cases:
        grouped.setdefault(case.name, []).append(case)
    return grouped


SAP_PARITY_CASES: dict[str, list[Case]] = _cases()
"""Parity cases per SAP normalizer (``test_every_builtin_normalizer_has_a_parity_case`` reads
the names)."""
ALL_CASES = [c for group in SAP_PARITY_CASES.values() for c in group]


def test_every_sap_normalizer_has_cases() -> None:
    assert set(SAP_PARITY_CASES) == set(SAP_NORMALIZERS)


# ---------------------------------------------------------------------- helpers


def make(case: Case, other_column: str = "c") -> Normalizer:
    params = dict(case.params)
    for key in ("currency_column", "unit_column"):
        if key in params:
            params[key] = other_column
    normalizer = SAP_NORMALIZERS[case.name](params)
    normalizer.plugin_name = case.name
    bound = normalizer.for_side("source")
    assert bound is not None
    return bound


def out_spec(normalizer: Normalizer, ctype: CanonicalType) -> CanonSpec:
    out = normalizer.output_type(ctype)
    kind = {
        LogicalType.DATE: Kind.DATE,
        LogicalType.TIME: Kind.TIME,
        LogicalType.BOOLEAN: Kind.BOOLEAN,
        LogicalType.DECIMAL: Kind.DECIMAL,
    }.get(out.logical, Kind.TEXT)
    return CanonSpec(
        kind=kind,
        logical=out.logical,
        scale=out.scale or 0,
        precision="s",
        strings=StringOptions(unicode_form="none"),
    )


Row = tuple[Any, Any]


def reference(case: Case, rows: Sequence[Row]) -> list[str | None]:
    normalizer = make(case)
    spec = out_spec(normalizer, case.ctype)
    out: list[str | None] = []
    for value, other in rows:
        if case.paired:
            assert isinstance(normalizer, SapCurr | SapQuan)
            result: Any = normalizer.convert(value, other, case.ctype)
        else:
            result = normalizer.to_python(value, case.ctype)
        out.append(canonical_text(result, spec))
    return out


def sql_for(case: Case, dialect: Dialect, value: str = "v", other: str = "c") -> str:
    normalizer = make(case, other)
    spec = out_spec(normalizer, case.ctype)
    return canonical_sql(normalizer.to_sql(value, dialect, case.ctype), spec, dialect)


def _arrow_type(ctype: CanonicalType) -> pa.DataType:
    if ctype.logical is LogicalType.INTEGER:
        return pa.int64()
    if ctype.logical is LogicalType.DECIMAL:
        return pa.decimal128(ctype.precision or 15, ctype.scale or 0)
    return pa.string()


def draw_rows(case: Case, data: st.DataObject, max_size: int = 25) -> list[Row]:
    single = st.one_of(st.none(), case.values)
    other = case.other if case.other is not None else st.none()
    drawn = data.draw(st.lists(st.tuples(single, other), max_size=max_size))
    edge_other = [None, *CURRENCY_EDGES[:3]] if case.name == "sap_curr" else [None, "ST", "KG"]
    edges: list[Row] = []
    for i, value in enumerate(case.edges):
        edges.append((value, edge_other[i % len(edge_other)] if case.paired else None))
    return [*edges, (None, None), *drawn]


def duckdb_eval(case: Case, rows: Sequence[Row]) -> list[Any]:
    table = pa.table(
        {
            "i": pa.array(range(len(rows)), pa.int64()),
            "v": pa.array([r[0] for r in rows], _arrow_type(case.ctype)),
            "c": pa.array([r[1] for r in rows], pa.string()),
        }
    )
    con = duckdb.connect()
    try:
        con.register("t", table)
        sql = sql_for(case, get_dialect("duckdb"), '"v"')
        return [r[0] for r in con.execute(f"SELECT {sql} FROM t ORDER BY i").fetchall()]
    finally:
        con.close()


def _sqlite_value(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


def sqlite_eval(case: Case, rows: Sequence[Row]) -> list[Any]:
    con = sqlite3.connect(":memory:")
    try:
        register_functions(con)
        con.execute("CREATE TABLE t (i INTEGER, v, c)")
        con.executemany(
            "INSERT INTO t VALUES (?, ?, ?)",
            [(i, _sqlite_value(v), c) for i, (v, c) in enumerate(rows)],
        )
        sql = sql_for(case, get_dialect("sqlite"), '"v"')
        return [r[0] for r in con.execute(f"SELECT {sql} FROM t ORDER BY i").fetchall()]
    finally:
        con.close()


def polars_eval(case: Case, rows: Sequence[Row]) -> list[Any]:
    from dvf.normalize.canonical import canonical_polars

    frame = pl.from_arrow(
        pa.table(
            {
                "v": pa.array([r[0] for r in rows], _arrow_type(case.ctype)),
                INPUT_COLUMN_PREFIX + "c": pa.array([r[1] for r in rows], pa.string()),
            }
        )
    )
    assert isinstance(frame, pl.DataFrame)
    normalizer = make(case)
    staged = frame.select(normalizer.to_polars(pl.col("v"), case.ctype).alias("z"))
    spec = out_spec(normalizer, case.ctype)
    canon = canonical_polars(pl.col("z"), spec, staged.schema["z"])
    return staged.select(canon.alias("c"))["c"].to_list()


def _sqlite_rows(case: Case, rows: list[Row]) -> list[Row]:
    if not case.small_for_sqlite:
        return rows
    # SQLite multiplies in binary floating point: keep amounts small enough to stay exact.
    return [r for r in rows if r[0] is None or abs(r[0]) < Decimal("10000000")]


# ---------------------------------------------------------------------- local engines


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
@PARITY
@given(data=st.data())
def test_local_parity(case: Case, data: st.DataObject) -> None:
    rows = draw_rows(case, data)
    expected = reference(case, rows)
    assert duckdb_eval(case, rows) == expected, "DuckDB differs from the reference"
    assert polars_eval(case, rows) == expected, "Polars differs from the reference"
    small = _sqlite_rows(case, rows)
    assert sqlite_eval(case, small) == reference(case, small), "SQLite differs from the reference"


def test_reference_examples() -> None:
    """Spot checks of the reference itself, in SAP terms."""
    alpha = SAP_NORMALIZERS["sap_alpha"]({})
    assert alpha.to_python("0000012345", STRING) == "12345"
    assert alpha.to_python("0000000000", STRING) == "0"
    assert alpha.to_python("ABC-1", STRING) == "ABC-1"
    pad = SAP_NORMALIZERS["sap_alpha"]({"value": "pad"})
    assert pad.to_python("12345", STRING) == "0000012345"
    matnr = SAP_NORMALIZERS["sap_matnr_length"]({})
    assert matnr.to_python("0" * 35 + "12345", STRING) == "000000000000012345"
    assert SAP_NORMALIZERS["sap_numc"]({"value": 6}).to_python(" ", STRING) == "000000"
    dats_n = SAP_NORMALIZERS["sap_dats"]({})
    assert dats_n.to_python("20240229", STRING) == date(2024, 2, 29)
    assert dats_n.to_python("00000000", STRING) is None
    assert dats_n.to_python("20230229", STRING) is None
    assert dats_n.to_python(20240131, INTEGER) == date(2024, 1, 31)
    assert SAP_NORMALIZERS["sap_tims"]({}).to_python("235959", STRING) == time(23, 59, 59)
    assert SAP_NORMALIZERS["sap_tims"]({}).to_python("000000", STRING) is None
    flag = SAP_NORMALIZERS["sap_bool"]({})
    assert [flag.to_python(v, STRING) for v in ("X", " ", "x", None)] == [True, False, None, None]
    spras = SAP_NORMALIZERS["sap_spras"]({})
    assert [spras.to_python(v, STRING) for v in ("E", "a", "A", "EN")] == ["EN", "AF", "AR", "EN"]
    to_sap = SAP_NORMALIZERS["sap_spras"]({"value": "to_sap"})
    assert [to_sap.to_python(v, STRING) for v in ("EN", "en", "ZF", "E")] == ["E", "E", "M", "E"]
    curr = SAP_NORMALIZERS["sap_curr"]({"currency_column": "WAERS"})
    assert isinstance(curr, SapCurr)
    assert curr.convert(Decimal("1.00"), "JPY", AMOUNT) == Decimal("100.000")
    assert curr.convert(Decimal("12.34"), "KWD", AMOUNT) == Decimal("1.234")
    assert curr.convert(Decimal("12.34"), "EUR", AMOUNT) == Decimal("12.340")
    assert curr.convert(Decimal("12.34"), None, AMOUNT) == Decimal("12.340")
    quan = SAP_NORMALIZERS["sap_quan"]({"unit": "ST", "decimals": {"ST": 0}})
    assert quan.to_python(Decimal("2.500"), QUANTITY) == Decimal("3.000")
    assert quan.to_python(Decimal("-2.500"), QUANTITY) == Decimal("-3.000")


# ---------------------------------------------------------------------- PostgreSQL

_PG_TYPES = {LogicalType.STRING: "TEXT", LogicalType.INTEGER: "BIGINT"}


def _pg_type(ctype: CanonicalType) -> str:
    if ctype.logical is LogicalType.DECIMAL:
        return f"NUMERIC({ctype.precision}, {ctype.scale})"
    return _PG_TYPES[ctype.logical]


@pytest.fixture(scope="module")
def pg(postgres_url: str) -> Iterator[Any]:
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute("SET TIME ZONE 'UTC'")
        yield con


def postgres_eval(con: Any, case: Case, rows: Sequence[Row]) -> list[Any]:
    con.execute("DROP TABLE IF EXISTS dvf_sap_parity")
    con.execute(f"CREATE TEMP TABLE dvf_sap_parity (i INT, v {_pg_type(case.ctype)}, c TEXT)")
    with con.cursor() as cur:
        cur.executemany(
            f"INSERT INTO dvf_sap_parity VALUES (%s, %s::{_pg_type(case.ctype)}, %s::TEXT)",
            [(i, v, c) for i, (v, c) in enumerate(rows)],
        )
    sql = sql_for(case, get_dialect("postgres"), '"v"')
    return [r[0] for r in con.execute(f"SELECT {sql} FROM dvf_sap_parity ORDER BY i").fetchall()]


@pytest.mark.integration
@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
@LIVE
@given(data=st.data())
def test_postgres_parity(pg: Any, case: Case, data: st.DataObject) -> None:
    # PostgreSQL text cannot hold NUL, which the strategies already exclude.
    rows = draw_rows(case, data)
    assert postgres_eval(pg, case, rows) == reference(case, rows)


# ---------------------------------------------------------------------- Spark


@pytest.fixture(scope="module")
def spark() -> Iterator[Any]:
    # The process's shared session, as in the other Spark modules. Starting a separate one
    # here (with its own app name) hung on some Windows machines: Spark's launcher could not
    # read back its temp file, exited 0 without a JVM, and PySpark waits for that forever.
    session = local_spark()
    previous_zone = session.conf.get("spark.sql.session.timeZone")
    session.conf.set("spark.sql.session.timeZone", "UTC")
    try:
        yield session
    finally:
        session.conf.set("spark.sql.session.timeZone", previous_zone)


def _spark_dialects() -> list[str]:
    names = ["databricks"]
    try:
        get_dialect("spark")
    except Exception:  # the Spark dialect may not exist yet in this checkout
        return names
    return ["spark", *names]


def spark_eval(session: Any, dialect: str, case: Case, rows: Sequence[Row]) -> list[Any]:
    from pyspark.sql import types as spark_types

    value_type: Any
    if case.ctype.logical is LogicalType.INTEGER:
        value_type = spark_types.LongType()
    elif case.ctype.logical is LogicalType.DECIMAL:
        value_type = spark_types.DecimalType(case.ctype.precision or 15, case.ctype.scale or 0)
    else:
        value_type = spark_types.StringType()
    schema = spark_types.StructType(
        [
            spark_types.StructField("i", spark_types.LongType()),
            spark_types.StructField("v", value_type),
            spark_types.StructField("c", spark_types.StringType()),
        ]
    )
    frame = session.createDataFrame([(i, v, c) for i, (v, c) in enumerate(rows)], schema)
    frame.createOrReplaceTempView("dvf_sap_parity")
    sql = sql_for(case, get_dialect(dialect), "`v`", "c")
    try:
        return [
            r[0] for r in session.sql(f"SELECT {sql} AS c FROM dvf_sap_parity ORDER BY i").collect()
        ]
    finally:
        # The session is shared by every Spark test in the process; leave no views behind.
        session.catalog.dropTempView("dvf_sap_parity")


SPARK = settings(
    max_examples=4,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


@pytest.mark.spark
@pytest.mark.filterwarnings(PANDAS_WARNING)
@pytest.mark.parametrize("dialect", _spark_dialects())
@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
@SPARK
@given(data=st.data())
def test_spark_parity(spark: Any, dialect: str, case: Case, data: st.DataObject) -> None:
    rows = draw_rows(case, data, max_size=40)
    assert spark_eval(spark, dialect, case, rows) == reference(case, rows)


# ---------------------------------------------------------------------- MySQL, SQL Server, Oracle

_SERVER_TYPES: dict[str, dict[str, str]] = {
    "mysql": {"text": "LONGTEXT CHARACTER SET utf8mb4", "integer": "BIGINT"},
    "mssql": {"text": "NVARCHAR(MAX)", "integer": "BIGINT"},
    "oracle": {"text": "VARCHAR2(4000 CHAR)", "integer": "NUMBER(19)"},
}


def _server_type(server: str, ctype: CanonicalType) -> str:
    if ctype.logical is LogicalType.DECIMAL:
        name = "NUMBER" if server == "oracle" else "DECIMAL"
        return f"{name}({ctype.precision}, {ctype.scale})"
    key = "integer" if ctype.logical is LogicalType.INTEGER else "text"
    return _SERVER_TYPES[server][key]


@pytest.fixture(scope="module", params=SERVERS)
def server(request: pytest.FixtureRequest) -> Iterator[Backend]:
    backend = make_backend(request.param)
    try:
        yield backend
    finally:
        backend.close()


def stored(server: Backend, value: Any) -> Any:
    """Oracle stores ``''`` as NULL."""
    return None if server.name == "oracle" and value == "" else value


def server_reference(server: Backend, case: Case, rows: Sequence[Row]) -> list[str | None]:
    stored_rows = [(stored(server, v), stored(server, c)) for v, c in rows]
    out = reference(case, stored_rows)
    return [None if server.name == "oracle" and v == "" else v for v in out]


def server_eval(server: Backend, case: Case, rows: Sequence[Row]) -> list[Any]:
    key = f"sap_{case.ctype.logical.value.lower()}_{case.ctype.scale or 0}"
    table = server.load_rows(
        key,
        [_server_type(server.name, case.ctype), _server_type(server.name, STRING)],
        list(rows),
    )
    upper = server.name == "oracle"  # unquoted names are upper-case on Oracle
    value, other = ("C0", "C1") if upper else ("c0", "c1")
    sql = sql_for(case, server.dialect, server.dialect.quote(value), other)
    result = server.connector.query(f"SELECT {sql} AS c FROM {table} ORDER BY i")
    return [v.decode("utf-8") if isinstance(v, bytes) else v for v in result.column(0).to_pylist()]


def _server_rows(server: Backend, case: Case, rows: list[Row]) -> list[Row]:
    if server.name == "mysql" and case.name == "sap_dats":
        # MySQL documents DATE values from year 1000 only.
        def early(v: Any) -> bool:
            text = str(v).strip().rjust(8, "0")
            return len(text) >= 4 and text[:4].isdigit() and "0000" < text[:4] < "1000"

        return [r for r in rows if r[0] is None or not early(r[0])]
    return rows


@pytest.mark.integration
@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
@LIVE
@given(data=st.data())
def test_server_parity(server: Backend, case: Case, data: st.DataObject) -> None:
    rows = _server_rows(server, case, draw_rows(case, data))
    got = server_eval(server, case, rows)
    expected = server_reference(server, case, rows)
    mismatches = [(r, g, e) for r, g, e in zip(rows, got, expected, strict=True) if g != e]
    assert not mismatches, f"{server.name} {case.id}: {mismatches[:5]}"
