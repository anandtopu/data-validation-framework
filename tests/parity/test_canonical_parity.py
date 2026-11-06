"""Cross-engine parity: the SQL (DuckDB, SQLite, PostgreSQL) and Polars implementations of
canonicalization, row hashing, and normalizers must equal the Python reference exactly."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
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
from dvf.normalize.canonical import (
    CanonSpec,
    Kind,
    StringOptions,
    canonical_polars,
    canonical_sql,
    canonical_text,
    polars_row_hash,
    row_hash,
    row_hash_sql,
)
from dvf.normalize.normalizers import BUILTIN_NORMALIZERS
from dvf.sql.dialects import get_dialect

PARITY = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# Characters whose case mapping differs between ICU/utf8proc, libc, and Python in ways that are
# not part of dvf's contract (title-case digraphs and characters outside the BMP).
_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Co", "Cn", "Lt"),
        blacklist_characters="\x00",
        max_codepoint=0xFFFF,
    ),
    max_size=12,
)
EDGE_TEXT = [
    "",
    " ",
    "\u00a0x\u00a0",
    "  a  ",
    "\ta b\n",
    "é",
    "ΑΣ",
    "İstanbul",
    "ß",
    "A  B",
    "0012",
]


def _arrow_type(kind: Kind, aware: bool = False) -> pa.DataType:
    return {
        Kind.TEXT: pa.string(),
        Kind.INTEGER: pa.int64(),
        Kind.DECIMAL: pa.decimal128(24, 6),
        Kind.BOOLEAN: pa.bool_(),
        Kind.DATE: pa.date32(),
        Kind.TIMESTAMP: pa.timestamp("us", tz="UTC" if aware else None),
        Kind.TIME: pa.time64("us"),
        Kind.BINARY: pa.binary(),
    }[kind]


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


def duckdb_eval(
    values: Sequence[Any], arrow_type: pa.DataType, build: Callable[[str], str]
) -> list[Any]:
    table = pa.table(
        {"i": pa.array(range(len(values)), pa.int64()), "v": pa.array(values, arrow_type)}
    )
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.register("t", table)
        rows = con.execute(f"SELECT {build('v')} AS c FROM t ORDER BY i").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def sqlite_eval(values: Sequence[Any], build: Callable[[str], str]) -> list[Any]:
    con = sqlite3.connect(":memory:")
    try:
        register_functions(con)
        con.execute("CREATE TABLE t (i INTEGER, v)")
        con.executemany(
            "INSERT INTO t VALUES (?, ?)", [(i, _sqlite_value(v)) for i, v in enumerate(values)]
        )
        return [r[0] for r in con.execute(f"SELECT {build('v')} FROM t ORDER BY i").fetchall()]
    finally:
        con.close()


def polars_eval(
    values: Sequence[Any], arrow_type: pa.DataType, build: Callable[[pl.Expr, pl.DataType], pl.Expr]
) -> list[Any]:
    frame = pl.from_arrow(pa.table({"v": pa.array(values, arrow_type)}))
    assert isinstance(frame, pl.DataFrame)
    return frame.select(build(pl.col("v"), frame.schema["v"]).alias("c"))["c"].to_list()


def _spec(kind: Kind, **kwargs: Any) -> CanonSpec:
    logical = {
        Kind.TEXT: LogicalType.STRING,
        Kind.INTEGER: LogicalType.INTEGER,
        Kind.DECIMAL: LogicalType.DECIMAL,
        Kind.BOOLEAN: LogicalType.BOOLEAN,
        Kind.DATE: LogicalType.DATE,
        Kind.TIMESTAMP: LogicalType.TIMESTAMP,
        Kind.TIME: LogicalType.TIME,
        Kind.BINARY: LogicalType.BINARY,
    }[kind]
    if kwargs.pop("aware", False):
        logical = LogicalType.TIMESTAMP_TZ
        kwargs["tz_aware"] = True
    return CanonSpec(kind=kind, logical=logical, **kwargs)


def assert_parity(
    values: Sequence[Any], spec: CanonSpec, *, sqlite: bool = True, aware: bool = False
) -> None:
    expected = [canonical_text(v, spec) for v in values]
    arrow_type = _arrow_type(spec.kind, aware)
    duck = duckdb_eval(values, arrow_type, lambda c: canonical_sql(c, spec, get_dialect("duckdb")))
    assert duck == expected, "DuckDB differs from the reference"
    polars_out = polars_eval(values, arrow_type, lambda e, dt: canonical_polars(e, spec, dt))
    assert polars_out == expected, "Polars differs from the reference"
    if sqlite:
        lite = sqlite_eval(values, lambda c: canonical_sql(c, spec, get_dialect("sqlite")))
        assert lite == expected, "SQLite differs from the reference"


string_options = st.builds(
    StringOptions,
    trim=st.sampled_from(["none", "rtrim", "ltrim", "both"]),
    case_sensitive=st.booleans(),
    unicode_form=st.sampled_from(["NFC", "none"]),
    collapse_whitespace=st.booleans(),
    null_equals_empty=st.booleans(),
)


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), _TEXT, st.sampled_from(EDGE_TEXT)), max_size=25),
    opts=string_options,
)
def test_text_parity(values: list[str | None], opts: StringOptions) -> None:
    assert_parity(values, _spec(Kind.TEXT, strings=opts))


@PARITY
@given(values=st.lists(st.one_of(st.none(), st.integers(-(2**63), 2**63 - 1)), max_size=25))
def test_integer_parity(values: list[int | None]) -> None:
    assert_parity(values, _spec(Kind.INTEGER))


decimals = st.decimals(
    min_value=Decimal("-999999999999.999999"),
    max_value=Decimal("999999999999.999999"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


@PARITY
@given(
    values=st.lists(
        st.one_of(
            st.none(),
            decimals,
            st.sampled_from([Decimal("-0.0005"), Decimal("1.0005"), Decimal("-1.005")]),
        ),
        max_size=25,
    ),
    scale=st.integers(0, 6),
)
def test_decimal_parity(values: list[Decimal | None], scale: int) -> None:
    assert_parity(values, _spec(Kind.DECIMAL, scale=scale))


@PARITY
@given(values=st.lists(st.one_of(st.none(), st.booleans()), max_size=10))
def test_boolean_parity(values: list[bool | None]) -> None:
    assert_parity(values, _spec(Kind.BOOLEAN))


@PARITY
@given(
    values=st.lists(
        st.one_of(st.none(), st.dates(date(1900, 1, 1), date(2100, 12, 31))), max_size=25
    )
)
def test_date_parity(values: list[date | None]) -> None:
    assert_parity(values, _spec(Kind.DATE))


naive_datetimes = st.datetimes(datetime(1900, 1, 1), datetime(2100, 12, 31))
DST_EDGES = [
    datetime(2024, 3, 31, 1, 59, 59, 999999),
    datetime(2024, 10, 27, 0, 30),
    datetime(1960, 2, 29, 23, 59, 59, 123456),
    datetime(1969, 12, 31, 23, 59, 59, 999999),
]


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), naive_datetimes, st.sampled_from(DST_EDGES)), max_size=25),
    precision=st.sampled_from(["s", "ms", "us"]),
)
def test_timestamp_utc_parity(values: list[datetime | None], precision: str) -> None:
    assert_parity(values, _spec(Kind.TIMESTAMP, precision=precision))


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), naive_datetimes, st.sampled_from(DST_EDGES)), max_size=20),
    zone=st.sampled_from(["Europe/Berlin", "America/New_York", "Asia/Kolkata"]),
)
def test_timestamp_local_zone_parity(values: list[datetime | None], zone: str) -> None:
    # Ambiguous and non-existent local times (DST transitions) are resolved like Python's
    # zoneinfo with fold=0; DuckDB (ICU) agrees outside the transition hour.
    spec = _spec(Kind.TIMESTAMP, precision="us", timezone=zone)
    safe = [v for v in values if v is None or not _near_transition(v, zone)]
    assert_parity(safe, spec)


def _near_transition(value: datetime, zone: str) -> bool:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(zone)
    before = value.replace(tzinfo=tz, fold=0).utcoffset()
    after = value.replace(tzinfo=tz, fold=1).utcoffset()
    roundtrip = value.replace(tzinfo=tz).astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    return before != after or roundtrip != value


@PARITY
@given(
    values=st.lists(
        st.one_of(
            st.none(),
            st.datetimes(datetime(1900, 1, 1), datetime(2100, 12, 31), timezones=st.just(UTC)),
        ),
        max_size=20,
    )
)
def test_timestamp_aware_parity(values: list[datetime | None]) -> None:
    assert_parity(values, _spec(Kind.TIMESTAMP, precision="ms", aware=True), aware=True)


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), st.times()), max_size=20),
    precision=st.sampled_from(["s", "ms", "us"]),
)
def test_time_parity(values: list[time | None], precision: str) -> None:
    assert_parity(values, _spec(Kind.TIME, precision=precision))


@PARITY
@given(values=st.lists(st.one_of(st.none(), st.binary(max_size=16)), max_size=20))
def test_binary_parity(values: list[bytes | None]) -> None:
    assert_parity(values, _spec(Kind.BINARY))


@PARITY
@given(rows=st.lists(st.lists(st.one_of(st.none(), _TEXT), min_size=3, max_size=3), max_size=20))
def test_row_hash_parity(rows: list[list[str | None]]) -> None:
    expected = [row_hash(r) for r in rows]
    table = pa.table({f"c{i}": pa.array([r[i] for r in rows], pa.string()) for i in range(3)})
    cols = ["c0", "c1", "c2"]
    con = duckdb.connect()
    try:
        con.register("t", table)
        duck = [
            r[0]
            for r in con.execute(
                f"SELECT {row_hash_sql(cols, get_dialect('duckdb'))} FROM t"
            ).fetchall()
        ]
    finally:
        con.close()
    assert duck == expected
    lite_con = sqlite3.connect(":memory:")
    register_functions(lite_con)
    lite_con.execute("CREATE TABLE t (c0, c1, c2)")
    lite_con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    lite = [
        r[0]
        for r in lite_con.execute(
            f"SELECT {row_hash_sql(cols, get_dialect('sqlite'))} FROM t"
        ).fetchall()
    ]
    lite_con.close()
    assert lite == expected
    frame = pl.from_arrow(table)
    assert isinstance(frame, pl.DataFrame)
    polars_out = frame.select(polars_row_hash([pl.col(c) for c in cols]).alias("h"))["h"].to_list()
    assert polars_out == expected


def test_row_hash_is_unambiguous() -> None:
    assert row_hash(["ab", "c"]) != row_hash(["a", "bc"])
    assert row_hash([None, ""]) != row_hash(["", None])
    assert row_hash(["~"]) != row_hash([None])


# ---------------------------------------------------------------------- normalizers

_NORMALIZER_INPUTS: dict[str, tuple[CanonicalType, Kind, Any]] = {
    "trim": (CanonicalType.string(), Kind.TEXT, None),
    "rtrim": (CanonicalType.string(), Kind.TEXT, None),
    "ltrim": (CanonicalType.string(), Kind.TEXT, None),
    "lower": (CanonicalType.string(), Kind.TEXT, None),
    "upper": (CanonicalType.string(), Kind.TEXT, None),
    "collapse_whitespace": (CanonicalType.string(), Kind.TEXT, None),
    "unicode_nfc": (CanonicalType.string(), Kind.TEXT, None),
    "null_if_empty": (CanonicalType.string(), Kind.TEXT, None),
    "empty_if_null": (CanonicalType.string(), Kind.TEXT, None),
    "strip_leading_zeros": (CanonicalType.string(), Kind.TEXT, None),
    "round": (CanonicalType.decimal(24, 6), Kind.DECIMAL, {"digits": 2}),
    "truncate_timestamp": (CanonicalType.timestamp(6), Kind.TIMESTAMP, {"unit": "minute"}),
    "to_utc": (CanonicalType.timestamp(6), Kind.TIMESTAMP, {"from_tz": "Asia/Kolkata"}),
}
_STRATEGY = {
    Kind.TEXT: st.one_of(_TEXT, st.sampled_from([*EDGE_TEXT, "000", "0", "007", " x "])),
    Kind.DECIMAL: decimals,
    Kind.TIMESTAMP: naive_datetimes,
}


def test_every_builtin_normalizer_has_a_parity_case() -> None:
    assert set(_NORMALIZER_INPUTS) == set(BUILTIN_NORMALIZERS)


@pytest.mark.parametrize("name", sorted(_NORMALIZER_INPUTS))
@PARITY
@given(data=st.data())
def test_normalizer_parity(name: str, data: st.DataObject) -> None:
    ctype, kind, params = _NORMALIZER_INPUTS[name]
    values = data.draw(st.lists(st.one_of(st.none(), _STRATEGY[kind]), max_size=20))
    normalizer = BUILTIN_NORMALIZERS[name](params)
    out_type = normalizer.output_type(ctype)
    spec = _spec(
        Kind.DECIMAL if out_type.logical is LogicalType.DECIMAL else kind, scale=out_type.scale or 0
    )
    expected = [canonical_text(normalizer.to_python(v, ctype), spec) for v in values]
    arrow_type = _arrow_type(kind)
    duck = duckdb_eval(
        values,
        arrow_type,
        lambda c: canonical_sql(
            normalizer.to_sql(c, get_dialect("duckdb"), ctype), spec, get_dialect("duckdb")
        ),
    )
    assert duck == expected
    lite = sqlite_eval(
        values,
        lambda c: canonical_sql(
            normalizer.to_sql(c, get_dialect("sqlite"), ctype), spec, get_dialect("sqlite")
        ),
    )
    assert lite == expected
    frame = pl.from_arrow(pa.table({"v": pa.array(values, arrow_type)}))
    assert isinstance(frame, pl.DataFrame)
    staged = frame.select(normalizer.to_polars(pl.col("v"), ctype).alias("z"))
    polars_out = staged.select(canonical_polars(pl.col("z"), spec, staged.schema["z"]).alias("c"))[
        "c"
    ].to_list()
    assert polars_out == expected


# ---------------------------------------------------------------------- PostgreSQL

_PG_TYPES = {
    Kind.TEXT: "TEXT",
    Kind.INTEGER: "BIGINT",
    Kind.DECIMAL: "NUMERIC(24,6)",
    Kind.BOOLEAN: "BOOLEAN",
    Kind.DATE: "DATE",
    Kind.TIMESTAMP: "TIMESTAMP",
    Kind.TIME: "TIME",
    Kind.BINARY: "BYTEA",
}


@pytest.mark.integration
def test_postgres_parity(postgres_url: str) -> None:
    import psycopg

    cases: list[tuple[CanonSpec, list[Any]]] = [
        (
            _spec(
                Kind.TEXT,
                strings=StringOptions(trim="both", case_sensitive=False, collapse_whitespace=True),
            ),
            [None, *EDGE_TEXT, "Ünïcödé  X"],
        ),
        (_spec(Kind.TEXT, strings=StringOptions(null_equals_empty=True)), ["", " ", "a", None]),
        (_spec(Kind.INTEGER), [None, 0, -1, 2**63 - 1, -(2**63)]),
        (
            _spec(Kind.DECIMAL, scale=2),
            [
                None,
                Decimal("-1.005"),
                Decimal("1.005"),
                Decimal("-0.001"),
                Decimal("123456789012.345678"),
            ],
        ),
        (_spec(Kind.BOOLEAN), [None, True, False]),
        (_spec(Kind.DATE), [None, date(1900, 1, 1), date(2024, 2, 29)]),
        (_spec(Kind.TIMESTAMP, precision="ms"), [None, *DST_EDGES]),
        (
            _spec(Kind.TIMESTAMP, precision="us", timezone="Europe/Berlin"),
            [datetime(2024, 7, 1, 10), datetime(2024, 1, 1, 10)],
        ),
        (_spec(Kind.TIME, precision="ms"), [None, time(10, 11, 12, 345678), time(0, 0)]),
        (_spec(Kind.BINARY), [None, b"", b"\xab\x00"]),
    ]
    dialect = get_dialect("postgres")
    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute("SET TIME ZONE 'UTC'")
        for spec, values in cases:
            con.execute("DROP TABLE IF EXISTS dvf_parity")
            con.execute(f"CREATE TEMP TABLE dvf_parity (i INT, v {_PG_TYPES[spec.kind]})")
            with con.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO dvf_parity VALUES (%s, %s::{_PG_TYPES[spec.kind]})",
                    list(enumerate(values)),
                )
            rows = con.execute(
                f"SELECT {canonical_sql('v', spec, dialect)} FROM dvf_parity ORDER BY i"
            ).fetchall()
            assert [r[0] for r in rows] == [canonical_text(v, spec) for v in values], spec
        hash_rows = [["ab", None, "c"], ["", "x", None]]
        con.execute("CREATE TEMP TABLE dvf_hash (c0 TEXT, c1 TEXT, c2 TEXT)")
        with con.cursor() as cur:
            cur.executemany("INSERT INTO dvf_hash VALUES (%s, %s, %s)", hash_rows)
        got = [
            r[0]
            for r in con.execute(
                f"SELECT {row_hash_sql(['c0', 'c1', 'c2'], dialect)} FROM dvf_hash"
            ).fetchall()
        ]
        assert sorted(got) == sorted(row_hash(r) for r in hash_rows)
