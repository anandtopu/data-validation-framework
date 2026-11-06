from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dvf.core.types import (
    CanonicalType,
    Compatibility,
    LogicalType,
    classify,
    from_sap_ddic,
    normalize_dialect,
    parse_native_type,
)

C = CanonicalType


@pytest.mark.parametrize(
    ("dialect", "native", "expected"),
    [
        ("postgres", "varchar(50)", "STRING(50)"),
        ("postgres", "character varying(20)", "STRING(20)"),
        ("postgres", "numeric(18,2)", "DECIMAL(18,2)"),
        ("postgres", "numeric", "DECIMAL"),
        ("postgres", "timestamptz", "TIMESTAMP_TZ(6)"),
        ("postgres", "timestamp(3) with time zone", "TIMESTAMP_TZ(3)"),
        ("postgres", "integer[]", "ARRAY<INTEGER(32)>"),
        ("postgres", "bpchar(10)", "STRING(10,fixed)"),
        ("postgres", "jsonb", "JSON"),
        ("postgres", "bytea", "BINARY"),
        ("postgres", "uuid", "UUID"),
        ("postgres", "double precision", "FLOAT(64)"),
        ("postgresql", "bigserial", "INTEGER(64)"),
        ("tsql", "nvarchar(max)", "STRING"),
        ("tsql", "varchar(10)", "STRING(10,ascii)"),
        ("tsql", "char(3)", "STRING(3,fixed,ascii)"),
        ("tsql", "datetime", "TIMESTAMP(3)"),
        ("tsql", "datetime2", "TIMESTAMP(7)"),
        ("tsql", "smalldatetime", "TIMESTAMP(0)"),
        ("tsql", "datetimeoffset", "TIMESTAMP_TZ(7)"),
        ("tsql", "bit", "BOOLEAN"),
        ("tsql", "money", "DECIMAL(19,4)"),
        ("tsql", "uniqueidentifier", "UUID"),
        ("tsql", "decimal", "DECIMAL(18,0)"),
        ("mssql", "timestamp", "BINARY(8,fixed)"),
        ("oracle", "date", "TIMESTAMP(0)"),
        ("oracle", "raw(16)", "BINARY(16)"),
        ("oracle", "number", "DECIMAL"),
        ("oracle", "number(10,2)", "DECIMAL(10,2)"),
        ("oracle", "float", "DECIMAL"),
        ("oracle", "varchar2(100 char)", "STRING(100)"),
        ("oracle", "clob", "STRING"),
        ("snowflake", "number", "DECIMAL(38,0)"),
        ("snowflake", "variant", "JSON"),
        ("snowflake", "timestamp_ntz(9)", "TIMESTAMP(9)"),
        ("snowflake", "timestamp_ltz", "TIMESTAMP_TZ(9)"),
        ("snowflake", "array", "ARRAY<JSON>"),
        ("bigquery", "int64", "INTEGER(64)"),
        ("bigquery", "numeric", "DECIMAL(38,9)"),
        ("bigquery", "bignumeric", "DECIMAL(76,38)"),
        ("bigquery", "datetime", "TIMESTAMP(6)"),
        ("bigquery", "timestamp", "TIMESTAMP_TZ(6)"),
        ("bigquery", "struct<a int64, b string>", "STRUCT<a:INTEGER(64),b:STRING>"),
        ("bigquery", "geography", "SPATIAL"),
        ("databricks", "map<string,int>", "MAP<STRING,INTEGER(32)>"),
        ("databricks", "timestamp_ntz", "TIMESTAMP(6)"),
        ("mysql", "int unsigned", "UINTEGER(32)"),
        ("mysql", "datetime", "TIMESTAMP(0)"),
        ("mysql", "year", "INTEGER(16)"),
        ("mysql", "tinytext", "STRING(255)"),
        ("duckdb", "hugeint", "INTEGER(128)"),
        ("hana", "seconddate", "TIMESTAMP(0)"),
        ("sap_hana", "tinyint", "UINTEGER(8)"),
        ("hana", "smalldecimal", "DECIMAL(16)"),
        ("hana", "alphanum(10)", "STRING(10)"),
        ("db2", "vargraphic(20)", "STRING(20)"),
        ("db2", "decfloat", "DECIMAL(34)"),
        (None, "boolean", "BOOLEAN"),
        (None, "interval", "INTERVAL"),
        ("generic", "time", "TIME(6)"),
        ("not_a_dialect", "varchar(5)", "STRING(5)"),
    ],
)
def test_parse_native_type(dialect: str | None, native: str, expected: str) -> None:
    assert str(parse_native_type(native, dialect)) == expected


@pytest.mark.parametrize("native", ["", "not a type !!", "varchar(", "<>"])
def test_unparseable_types_are_unknown(native: str) -> None:
    assert parse_native_type(native, "postgres").logical is LogicalType.UNKNOWN


def test_normalize_dialect() -> None:
    assert normalize_dialect("PostgreSQL") == "postgres"
    assert normalize_dialect("sqlserver") == "tsql"
    assert normalize_dialect("ansi") is None
    assert normalize_dialect(None) is None


@pytest.mark.parametrize(
    ("code", "length", "decimals", "expected"),
    [
        ("CHAR", 10, None, "STRING(10,fixed)"),
        ("NUMC", 8, None, "STRING(8,fixed)"),
        ("CLNT", 3, None, "STRING(3,fixed)"),
        ("LANG", 1, None, "STRING(1,fixed)"),
        ("CUKY", 5, None, "STRING(5,fixed)"),
        ("LCHR", 500, None, "STRING(500)"),
        ("DEC", 15, 2, "DECIMAL(15,2)"),
        ("CURR", 13, 2, "DECIMAL(13,2)"),
        ("QUAN", 13, None, "DECIMAL(13,0)"),
        ("INT1", None, None, "UINTEGER(8)"),
        ("INT2", None, None, "INTEGER(16)"),
        ("INT4", None, None, "INTEGER(32)"),
        ("INT8", None, None, "INTEGER(64)"),
        ("FLTP", None, None, "FLOAT(64)"),
        ("D34N", None, None, "DECIMAL(34)"),
        ("DATS", 8, None, "DATE"),
        ("TIMS", 6, None, "TIME(0)"),
        ("UTCL", None, None, "TIMESTAMP_TZ(7)"),
        ("STRG", None, None, "STRING"),
        ("SSTRING", 255, None, "STRING(255)"),
        ("RAW", 16, None, "BINARY(16,fixed)"),
        ("RSTR", None, None, "BINARY"),
        ("ZZZZ", None, None, "UNKNOWN"),
    ],
)
def test_sap_ddic(code: str, length: int | None, decimals: int | None, expected: str) -> None:
    assert str(from_sap_ddic(code, length, decimals)) == expected


def lvl(source: CanonicalType, target: CanonicalType) -> Compatibility:
    return classify(source, target).level


class TestClassify:
    def test_exact(self) -> None:
        assert lvl(C.string(10), C.string(10)) is Compatibility.EXACT

    def test_unknown(self) -> None:
        assert lvl(C.unknown(), C.string()) is Compatibility.UNKNOWN

    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            (C.integer(32), C.integer(64), Compatibility.WIDENING),
            (C.integer(64), C.integer(32), Compatibility.LOSSY),
            (C.integer(32, unsigned=True), C.integer(32), Compatibility.LOSSY),
            (C.integer(16, unsigned=True), C.integer(32), Compatibility.WIDENING),
            (C.float(32), C.float(64), Compatibility.WIDENING),
            (C.float(64), C.float(32), Compatibility.LOSSY),
            (C.decimal(18, 2), C.decimal(20, 4), Compatibility.WIDENING),
            (C.decimal(18, 4), C.decimal(18, 2), Compatibility.LOSSY),
            (C.decimal(18, 2), C.decimal(10, 2), Compatibility.LOSSY),
            (C.decimal(10, 2), C.decimal(), Compatibility.WIDENING),
            (C.decimal(), C.decimal(38, 10), Compatibility.LOSSY),
            (C.decimal(16), C.decimal(34), Compatibility.WIDENING),
            (C.decimal(16), C.decimal(10), Compatibility.LOSSY),
            (C.decimal(16), C.decimal(20, 2), Compatibility.LOSSY),
            (C.string(10), C.string(20), Compatibility.WIDENING),
            (C.string(20), C.string(10), Compatibility.LOSSY),
            (C.string(), C.string(4000), Compatibility.LOSSY),
            (C.string(10), C.string(10, unicode=False), Compatibility.LOSSY),
            (C.string(10, fixed=True), C.string(10), Compatibility.WIDENING),
            (C.binary(16), C.binary(), Compatibility.WIDENING),
            (C.timestamp(9), C.timestamp(3), Compatibility.LOSSY),
            (C.timestamp(0), C.timestamp(6), Compatibility.WIDENING),
            (C.time(0), C.time(3), Compatibility.WIDENING),
        ],
    )
    def test_same_logical(
        self, source: CanonicalType, target: CanonicalType, expected: Compatibility
    ) -> None:
        assert lvl(source, target) is expected

    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            (C.integer(32), C.string(), Compatibility.WIDENING),
            (C.integer(32), C.string(11), Compatibility.WIDENING),
            (C.integer(32), C.string(5), Compatibility.LOSSY),
            (C.decimal(), C.string(100), Compatibility.LOSSY),
            (C.date(), C.string(10), Compatibility.WIDENING),
            (C.string(8, fixed=True), C.date(), Compatibility.LOSSY),
            (C.boolean(), C.integer(8), Compatibility.WIDENING),
            (C.integer(8), C.boolean(), Compatibility.LOSSY),
            (C.integer(32), C.decimal(10, 0), Compatibility.WIDENING),
            (C.integer(64), C.decimal(18, 0), Compatibility.LOSSY),
            (C.integer(64), C.decimal(), Compatibility.WIDENING),
            (C.integer(32), C.float(64), Compatibility.WIDENING),
            (C.integer(64), C.float(64), Compatibility.LOSSY),
            (C.decimal(9, 0), C.integer(32), Compatibility.WIDENING),
            (C.decimal(10, 0), C.integer(32), Compatibility.LOSSY),
            (C.decimal(10, 2), C.integer(64), Compatibility.LOSSY),
            (C.decimal(10, 2), C.float(64), Compatibility.LOSSY),
            (C.float(64), C.decimal(38, 10), Compatibility.LOSSY),
            (C.date(), C.timestamp(6), Compatibility.WIDENING),
            (C.date(), C.timestamp(6, tz=True), Compatibility.WIDENING),
            (C.timestamp(6), C.date(), Compatibility.LOSSY),
            (C.timestamp(6), C.timestamp(6, tz=True), Compatibility.WIDENING),
            (C.timestamp(9), C.timestamp(3, tz=True), Compatibility.LOSSY),
            (C.timestamp(6, tz=True), C.timestamp(6), Compatibility.LOSSY),
            (C.simple(LogicalType.UUID), C.binary(16), Compatibility.WIDENING),
            (C.simple(LogicalType.UUID), C.binary(8), Compatibility.LOSSY),
            (C.binary(16), C.simple(LogicalType.UUID), Compatibility.LOSSY),
            (C.array(C.integer(32)), C.simple(LogicalType.JSON), Compatibility.WIDENING),
            (C.simple(LogicalType.JSON), C.array(C.integer(32)), Compatibility.LOSSY),
            (C.time(0), C.timestamp(0), Compatibility.INCOMPATIBLE),
            (C.boolean(), C.date(), Compatibility.INCOMPATIBLE),
        ],
    )
    def test_cross_logical(
        self, source: CanonicalType, target: CanonicalType, expected: Compatibility
    ) -> None:
        assert lvl(source, target) is expected

    def test_nested(self) -> None:
        assert lvl(C.array(C.integer(32)), C.array(C.integer(64))) is Compatibility.WIDENING
        assert (
            lvl(C.map(C.string(), C.integer(64)), C.map(C.string(), C.integer(32)))
            is Compatibility.LOSSY
        )
        source = C.struct([("a", C.integer(32)), ("b", C.string(10))])
        added = C.struct([("A", C.integer(32)), ("b", C.string(10)), ("c", C.date())])
        dropped = C.struct([("a", C.integer(32))])
        assert lvl(source, added) is Compatibility.WIDENING
        assert lvl(source, dropped) is Compatibility.LOSSY

    def test_reasons_are_explanatory(self) -> None:
        result = classify(C.decimal(18, 4), C.decimal(16, 2))
        assert result.level is Compatibility.LOSSY
        assert "scale reduced from 4 to 2" in result.reasons
        assert "integer digits" not in " ".join(result.reasons)

    def test_real_migration_pairs(self) -> None:
        oracle_date = parse_native_type("DATE", "oracle")
        assert lvl(oracle_date, parse_native_type("TIMESTAMP_NTZ(3)", "snowflake")) is (
            Compatibility.WIDENING
        )
        assert lvl(oracle_date, parse_native_type("DATE", "snowflake")) is Compatibility.LOSSY
        sap_kunnr = from_sap_ddic("CHAR", 10)
        assert lvl(sap_kunnr, parse_native_type("VARCHAR(10)", "snowflake")) is (
            Compatibility.WIDENING
        )


def test_rendering_of_composite_types() -> None:
    assert str(C.decimal(10)) == "DECIMAL(10)"
    assert str(C.string(5, unicode=False)) == "STRING(5,ascii)"
    assert str(C.binary(4, fixed=True)) == "BINARY(4,fixed)"
    assert str(C.timestamp()) == "TIMESTAMP"
    assert str(C.simple(LogicalType.UUID)) == "UUID"


_scalar_types = st.one_of(
    st.builds(C.integer, st.sampled_from([8, 16, 24, 32, 64, 128]), unsigned=st.booleans()),
    st.builds(C.float, st.sampled_from([32, 64])),
    st.builds(
        C.decimal,
        st.one_of(st.none(), st.integers(1, 76)),
        st.one_of(st.none(), st.integers(0, 38)),
    ),
    st.builds(
        C.string,
        st.one_of(st.none(), st.integers(1, 10_000)),
        fixed=st.booleans(),
        unicode=st.booleans(),
    ),
    st.builds(C.timestamp, st.one_of(st.none(), st.integers(0, 9)), tz=st.booleans()),
    st.sampled_from(
        [C.date(), C.boolean(), C.simple(LogicalType.UUID), C.simple(LogicalType.JSON)]
    ),
)


@given(_scalar_types)
def test_identical_types_are_exact(t: CanonicalType) -> None:
    assert classify(t, t).level is Compatibility.EXACT


@given(_scalar_types, _scalar_types)
def test_exact_only_for_equal_types(a: CanonicalType, b: CanonicalType) -> None:
    result = classify(a, b)
    assert (result.level is Compatibility.EXACT) == (a == b)
    if result.level is not Compatibility.EXACT:
        assert result.reasons, "non-exact results must explain themselves"
