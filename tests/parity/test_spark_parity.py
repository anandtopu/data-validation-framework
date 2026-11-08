"""Parity of the Spark SQL dialect with the Python reference, on a local Spark session.

The same Hypothesis strategies as ``test_canonical_parity`` generate values; each example is a
long list, and every variant of a canonical form is evaluated as one column of a single Spark
query, so the suite stays fast. Where Spark cannot match the reference, the test asserts the
documented behavior instead (see :mod:`dvf.sql.spark`):

* Spark has no Unicode normalization, so canonicalization skips it (as the plan notes say);
* Spark's ICU case mappings may follow a newer Unicode version than Python's tables; those few
  characters are measured and excluded from case-insensitive cases.

Everything runs with ANSI mode on (the Spark 4 default); one test repeats every kind with ANSI
mode off.
"""

from __future__ import annotations

import hashlib
import itertools
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dvf.core.types import CanonicalType, LogicalType
from dvf.normalize.canonical import (
    CanonSpec,
    Kind,
    StringOptions,
    canonical_sql,
    canonical_text,
    row_hash,
    row_hash_sql,
    simple_lower,
    simple_upper,
    with_strings,
)
from dvf.normalize.normalizers import BUILTIN_NORMALIZERS
from dvf.sql.dialects import HASH_SPACE, UnsupportedFeatureError, get_dialect
from tests.parity.test_canonical_parity import (
    _NORMALIZER_INPUTS,
    _STRATEGY,
    _TEXT,
    DST_EDGES,
    EDGE_TEXT,
    _arrow_type,
    _near_transition,
    _spec,
    decimals,
    naive_datetimes,
)
from tests.unit.test_spark_support import PANDAS_WARNING, local_spark

pytestmark = [pytest.mark.spark, pytest.mark.filterwarnings(PANDAS_WARNING)]

SPARK = settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
DIALECT = get_dialect("spark")
NTZ = "TIMESTAMP_NTZ"
"""Naive values are cast to the wall-clock type Spark gives naive Parquet timestamps."""


def values_of(strategy: st.SearchStrategy[Any], min_size: int = 20) -> st.SearchStrategy[Any]:
    """Long lists, so each Spark query checks many values."""
    return st.lists(st.one_of(st.none(), strategy), min_size=min_size, max_size=150)


class Evaluator:
    """Evaluates SQL expressions over a column of values in dvf's Spark session."""

    def __init__(self, session: Any) -> None:
        from dvf.connectors.spark import SparkConnector, child_session

        self.connector = SparkConnector.for_session(child_session(session), "parity")

    @property
    def session(self) -> Any:
        return self.connector.session

    def columns(
        self,
        values: Sequence[Any],
        arrow_type: pa.DataType,
        exprs: Sequence[str],
        spark_type: str | None = None,
    ) -> list[list[Any]]:
        """For each expression, its result per value (in order)."""
        table = pa.table(
            {"i": pa.array(range(len(values)), pa.int64()), "v": pa.array(values, arrow_type)}
        )
        self.connector.register_arrow("dvf_parity", table)
        source = "dvf_parity"
        if spark_type is not None:
            source = f"(SELECT i, CAST(v AS {spark_type}) AS v FROM dvf_parity) AS dvf_typed"
        selected = ", ".join(f"{e} AS e{j}" for j, e in enumerate(exprs))
        try:
            result = self.connector.query(f"SELECT i, {selected} FROM {source}")
        finally:
            self.connector.unregister_arrow("dvf_parity")
        order = result.column("i").to_pylist()
        out: list[list[Any]] = []
        for j in range(len(exprs)):
            got = result.column(f"e{j}").to_pylist()
            by_index = dict(zip(order, got, strict=True))
            out.append([by_index[i] for i in range(len(values))])
        return out

    def column(
        self, values: Sequence[Any], arrow_type: pa.DataType, build: Callable[[str], str]
    ) -> list[Any]:
        return self.columns(values, arrow_type, [build("v")])[0]


@pytest.fixture(scope="module")
def spark() -> Evaluator:
    return Evaluator(local_spark())


# ---------------------------------------------------------------------- documented behavior

_BMP = [chr(c) for c in range(1, 0x10000) if unicodedata.category(chr(c)) not in ("Cs", "Co", "Cn")]


@pytest.fixture(scope="module")
def case_diffs(spark: Evaluator) -> tuple[frozenset[str], frozenset[str]]:
    """BMP characters whose Spark lower/upper case differs from the reference."""
    lowered, uppered = spark.columns(_BMP, pa.string(), [DIALECT.lower("v"), DIALECT.upper("v")])
    lower = frozenset(c for c, out in zip(_BMP, lowered, strict=True) if out != simple_lower(c))
    upper = frozenset(c for c, out in zip(_BMP, uppered, strict=True) if out != simple_upper(c))
    return lower, upper


def test_case_mapping_differs_only_for_newer_unicode(
    case_diffs: tuple[frozenset[str], frozenset[str]],
) -> None:
    lower, upper = case_diffs
    # Spark 4.3 bundles ICU 78 (Unicode 17); Python 3.14 has Unicode 16 tables.
    assert len(lower) <= 4, sorted(map(hex, map(ord, lower)))
    assert len(upper) <= 4, sorted(map(hex, map(ord, upper)))
    # Characters with multi-character upper cases are mapped like the reference.
    assert not (upper & set("ßŉǰΐᾳﬁﬀ"))


def test_upper_of_strings_with_expanding_characters(spark: Evaluator) -> None:
    values = ["Straße", "ﬁx ß ŉ", "ᾳ ᾼ abc", "ΐ", "", None, "plain"]
    got = spark.column(values, pa.string(), DIALECT.upper)
    assert got == [None if v is None else simple_upper(v) for v in values]


def test_lower_handles_final_sigma_and_dotted_i(spark: Evaluator) -> None:
    values = ["ΟΔΟΣ ΣΑΣ", "İstanbul", "ΑΣ", "Σ"]
    assert spark.column(values, pa.string(), DIALECT.lower) == [simple_lower(v) for v in values]


def test_unicode_normalization_is_not_available() -> None:
    for form in ("NFC", "NFD", "NFKC", "NFKD"):
        assert DIALECT.supports_unicode_form(form) is False
        with pytest.raises(UnsupportedFeatureError):
            DIALECT.unicode_normalize("v", form)


def test_literals_round_trip(spark: Evaluator) -> None:
    samples = ["it's", "back\\slash", "tab\tnew\nline\r", "''", "\\'", "é ß \U0001f600", "%_"]
    exprs = [DIALECT.literal(s) for s in samples]
    got = spark.columns([1], pa.int64(), exprs)
    assert [g[0] for g in got] == samples


def test_length_counts_code_points_and_trailing_spaces(spark: Evaluator) -> None:
    values = ["a  ", "\U0001f600x", "é", None, " ", ""]
    got = spark.column(values, pa.string(), DIALECT.length)
    assert got == [None if v is None else len(v) for v in values]


def test_text_equality_is_exact(spark: Evaluator) -> None:
    pairs = [("a", "a "), ("a", "A"), ("e", "é"), ("x", "x"), ("x", None), (None, None)]
    lefts = [p[0] for p in pairs]
    rights = [p[1] for p in pairs]
    table = pa.table({"i": list(range(len(pairs))), "a": lefts, "b": rights})
    spark.connector.register_arrow("dvf_pairs", table)
    result = spark.connector.query(
        f"SELECT i, {DIALECT.boolean(DIALECT.is_distinct('a', 'b'))} AS d FROM dvf_pairs ORDER BY i"
    )
    assert result.column("d").to_pylist() == [True, True, True, False, True, False]


def test_regex_full_match_is_anchored(spark: Evaluator) -> None:
    values = ["abc", "abc\n", "xabc", None]
    full = spark.column(values, pa.string(), lambda c: DIALECT.regex_match(c, "abc", full=True))
    partial = spark.column(values, pa.string(), lambda c: DIALECT.regex_match(c, "abc", full=False))
    assert full == [True, False, False, None]
    assert partial == [True, True, True, None]


# ---------------------------------------------------------------------- text

ALL_STRING_OPTIONS = [
    StringOptions(
        trim=trim,
        case_sensitive=case,
        unicode_form=form,
        collapse_whitespace=collapse,
        null_equals_empty=empty,
    )
    for trim, case, form, collapse, empty in itertools.product(
        ["none", "rtrim", "ltrim", "both"],
        [True, False],
        ["NFC", "none"],
        [True, False],
        [True, False],
    )
]


def reference(value: Any, spec: CanonSpec) -> str | None:
    """The reference with Unicode normalization skipped, as Spark does (documented)."""
    if spec.kind is Kind.TEXT and spec.strings.unicode_form != "none":
        spec = with_strings(spec, unicode_form="none")
    return canonical_text(value, spec)


def assert_specs(
    spark: Evaluator,
    values: Sequence[Any],
    specs: Sequence[CanonSpec],
    arrow_type: pa.DataType,
    *,
    spark_type: str | None = None,
    to_spark: Callable[[Any], Any] = lambda v: v,
) -> None:
    exprs = [canonical_sql("v", spec, DIALECT) for spec in specs]
    got = spark.columns([to_spark(v) for v in values], arrow_type, exprs, spark_type)
    for spec, column in zip(specs, got, strict=True):
        expected = [reference(v, spec) for v in values]
        mismatches = [(v, g, e) for v, g, e in zip(values, column, expected, strict=True) if g != e]
        assert not mismatches, f"Spark differs for {spec.describe()}: {mismatches[:5]}"


@SPARK
@given(values=values_of(st.one_of(_TEXT, st.sampled_from(EDGE_TEXT))))
def test_text_parity(
    spark: Evaluator,
    case_diffs: tuple[frozenset[str], frozenset[str]],
    values: list[str | None],
) -> None:
    excluded = case_diffs[0]
    kept = [v for v in values if v is None or not (set(v) & excluded)]
    specs = [_spec(Kind.TEXT, strings=opts) for opts in ALL_STRING_OPTIONS]
    assert_specs(spark, kept, specs, pa.string())


def test_text_from_other_types(spark: Evaluator) -> None:
    """Text canonicalization of a non-string column casts it to STRING first."""
    spec = CanonSpec(kind=Kind.TEXT, logical=LogicalType.INTEGER)
    got = spark.column([1, -20, None], pa.int64(), lambda c: canonical_sql(c, spec, DIALECT))
    assert got == ["1", "-20", None]


# ---------------------------------------------------------------------- numbers, booleans, dates


@SPARK
@given(values=values_of(st.integers(-(2**63), 2**63 - 1)))
def test_integer_parity(spark: Evaluator, values: list[int | None]) -> None:
    assert_specs(spark, values, [_spec(Kind.INTEGER)], pa.int64())


HALF_WAY = [
    Decimal("-0.0005"),
    Decimal("1.0005"),
    Decimal("-1.005"),
    Decimal("0.5"),
    Decimal("-0.5"),
]


@SPARK
@given(values=values_of(st.one_of(decimals, st.sampled_from(HALF_WAY))))
def test_decimal_parity(spark: Evaluator, values: list[Decimal | None]) -> None:
    specs = [_spec(Kind.DECIMAL, scale=scale) for scale in range(7)]
    assert_specs(spark, values, specs, pa.decimal128(24, 6))


def test_decimal_unknown_scale_and_negative_zero(spark: Evaluator) -> None:
    values = [Decimal("-0.0000000000001"), Decimal("-0.000001"), Decimal("123456789012.345678")]
    specs = [_spec(Kind.DECIMAL, scale=s) for s in (0, 3, 10)]
    assert_specs(spark, values, specs, pa.decimal128(38, 13))


def test_decimal_of_integers(spark: Evaluator) -> None:
    """INTEGER columns compared with DECIMAL columns use the decimal form."""
    values = [0, -1, 2**63 - 1, -(2**63), None]
    specs = [_spec(Kind.DECIMAL, scale=s) for s in (0, 2, 6)]
    assert_specs(spark, values, specs, pa.int64())


@SPARK
@given(values=values_of(st.booleans(), min_size=1))
def test_boolean_parity(spark: Evaluator, values: list[bool | None]) -> None:
    assert_specs(spark, values, [_spec(Kind.BOOLEAN)], pa.bool_())


@SPARK
@given(values=values_of(st.dates(date(1900, 1, 1), date(2100, 12, 31))))
def test_date_parity(spark: Evaluator, values: list[date | None]) -> None:
    assert_specs(spark, values, [_spec(Kind.DATE)], pa.date32())


def test_dates_far_from_today(spark: Evaluator) -> None:
    values = [date(1, 1, 1), date(1582, 10, 4), date(1582, 10, 15), date(9999, 12, 31)]
    assert_specs(spark, values, [_spec(Kind.DATE)], pa.date32())


@SPARK
@given(values=values_of(st.one_of(naive_datetimes, st.sampled_from(DST_EDGES))))
def test_timestamp_utc_parity(spark: Evaluator, values: list[datetime | None]) -> None:
    specs = [_spec(Kind.TIMESTAMP, precision=p) for p in ("s", "ms", "us")]
    assert_specs(spark, values, specs, pa.timestamp("us"), spark_type=NTZ)


def test_naive_arrow_timestamps_arrive_as_utc_instants(spark: Evaluator) -> None:
    """createDataFrame reads naive Arrow timestamps as TIMESTAMP in the session time zone (UTC),
    so the tests cast them to TIMESTAMP_NTZ, the type naive Parquet timestamps get."""
    table = pa.table({"v": pa.array([datetime(2024, 1, 1, 10)], pa.timestamp("us"))})
    spark.connector.register_arrow("dvf_types", table)
    row = spark.connector.query(
        "SELECT typeof(v) AS t, CAST(CAST(v AS TIMESTAMP_NTZ) AS STRING) AS s FROM dvf_types"
    ).to_pylist()[0]
    assert row == {"t": "timestamp", "s": "2024-01-01 10:00:00"}


ZONES = ["Europe/Berlin", "America/New_York", "Asia/Kolkata"]


@SPARK
@given(values=values_of(st.one_of(naive_datetimes, st.sampled_from(DST_EDGES))))
def test_timestamp_local_zone_parity(spark: Evaluator, values: list[datetime | None]) -> None:
    # Ambiguous and non-existent local times are excluded, as in test_canonical_parity.
    for zone in ZONES:
        safe = [v for v in values if v is None or not _near_transition(v, zone)]
        spec = _spec(Kind.TIMESTAMP, precision="us", timezone=zone)
        assert_specs(spark, safe, [spec], pa.timestamp("us"), spark_type=NTZ)


def test_timestamp_in_dst_gap_and_overlap(spark: Evaluator) -> None:
    """Non-existent and repeated local times resolve like Python's zoneinfo with fold=0."""
    values = [datetime(2024, 3, 31, 2, 30), datetime(2024, 10, 27, 2, 30)]
    spec = _spec(Kind.TIMESTAMP, precision="s", timezone="Europe/Berlin")
    assert_specs(spark, values, [spec], pa.timestamp("us"), spark_type=NTZ)


@SPARK
@given(
    values=values_of(
        st.datetimes(datetime(1900, 1, 1), datetime(2100, 12, 31), timezones=st.just(UTC))
    )
)
def test_timestamp_aware_parity(spark: Evaluator, values: list[datetime | None]) -> None:
    specs = [_spec(Kind.TIMESTAMP, precision=p, aware=True) for p in ("s", "ms", "us")]
    assert_specs(spark, values, specs, pa.timestamp("us", tz="UTC"))


def test_aware_timestamps_from_dates(spark: Evaluator) -> None:
    """A DATE compared with a TIMESTAMP becomes midnight."""
    spec = CanonSpec(kind=Kind.TIMESTAMP, logical=LogicalType.DATE, precision="s")
    got = spark.column(
        [date(2024, 2, 29), None], pa.date32(), lambda c: canonical_sql(c, spec, DIALECT)
    )
    assert got == ["2024-02-29T00:00:00", None]


@SPARK
@given(values=values_of(st.times()))
def test_time_parity(spark: Evaluator, values: list[time | None]) -> None:
    # Spark has no usable TIME type: times arrive as text, as from CSV files or casts.
    specs = [_spec(Kind.TIME, precision=p) for p in ("s", "ms", "us")]
    assert_specs(
        spark, values, specs, pa.string(), to_spark=lambda v: None if v is None else v.isoformat()
    )


@SPARK
@given(values=values_of(st.binary(max_size=16)))
def test_binary_parity(spark: Evaluator, values: list[bytes | None]) -> None:
    assert_specs(spark, values, [_spec(Kind.BINARY)], pa.binary())


def test_epoch_microseconds(spark: Evaluator) -> None:
    values = [datetime(1969, 12, 31, 23, 59, 59, 999999), datetime(2024, 2, 29, 12, 0, 0, 1), None]
    epoch = datetime(1970, 1, 1)
    micro = timedelta(microseconds=1)
    expected = [None if v is None else float((v - epoch) // micro) for v in values]
    naive = spark.column(
        values, pa.timestamp("us"), lambda c: DIALECT.epoch_us(c, tz_aware=False, timezone="UTC")
    )
    assert naive == expected
    aware = spark.column(
        [None if v is None else v.replace(tzinfo=UTC) for v in values],
        pa.timestamp("us", tz="UTC"),
        lambda c: DIALECT.epoch_us(c, tz_aware=True, timezone="UTC"),
    )
    assert aware == expected
    berlin = spark.column(
        [datetime(2024, 7, 1, 10)],
        pa.timestamp("us"),
        lambda c: DIALECT.epoch_us(c, tz_aware=False, timezone="Europe/Berlin"),
    )
    assert berlin == [float((datetime(2024, 7, 1, 8) - epoch) // micro)]


def test_current_timestamp_is_utc(spark: Evaluator) -> None:
    got = spark.column([1], pa.int64(), lambda c: DIALECT.current_timestamp_utc())[0]
    now = datetime.now(UTC).replace(tzinfo=None)
    assert abs(got - now) < timedelta(minutes=5)


# ---------------------------------------------------------------------- row hash and buckets


@SPARK
@given(rows=st.lists(st.lists(st.one_of(st.none(), _TEXT), min_size=3, max_size=3), max_size=80))
def test_row_hash_parity(spark: Evaluator, rows: list[list[str | None]]) -> None:
    table = pa.table(
        {
            "i": pa.array(range(len(rows)), pa.int64()),
            **{f"c{k}": pa.array([r[k] for r in rows], pa.string()) for k in range(3)},
        }
    )
    spark.connector.register_arrow("dvf_hash", table)
    sql = f"SELECT i, {row_hash_sql(['c0', 'c1', 'c2'], DIALECT)} AS h FROM dvf_hash ORDER BY i"
    assert spark.connector.query(sql).column("h").to_pylist() == [row_hash(r) for r in rows]


def test_row_hash_of_mixed_kinds(spark: Evaluator) -> None:
    """Canonical expressions of several kinds serialized together, as projections do."""
    specs = [
        _spec(Kind.INTEGER),
        _spec(Kind.DECIMAL, scale=3),
        _spec(Kind.TIMESTAMP, precision="ms"),
        _spec(Kind.TEXT, strings=StringOptions(trim="both", case_sensitive=False)),
    ]
    rows = [
        (1, Decimal("-0.0004"), datetime(1999, 12, 31, 23, 59, 59, 999999), "  Müller "),
        (-(2**63), None, datetime(2024, 2, 29), None),
        (2**63 - 1, Decimal("123456789.5"), None, "x"),
    ]
    table = pa.table(
        {
            "i": pa.array(range(len(rows)), pa.int64()),
            "c0": pa.array([r[0] for r in rows], pa.int64()),
            "c1": pa.array([r[1] for r in rows], pa.decimal128(13, 4)),
            "c2": pa.array([r[2] for r in rows], pa.timestamp("us")),
            "c3": pa.array([r[3] for r in rows], pa.string()),
        }
    )
    spark.connector.register_arrow("dvf_mixed", table)
    exprs = [canonical_sql(f"c{j}", s, DIALECT) for j, s in enumerate(specs)]
    typed = "SELECT i, c0, c1, CAST(c2 AS TIMESTAMP_NTZ) AS c2, c3 FROM dvf_mixed"
    sql = f"SELECT i, {row_hash_sql(exprs, DIALECT)} AS h FROM ({typed}) AS t ORDER BY i"
    expected = [row_hash([reference(v, s) for v, s in zip(r, specs, strict=True)]) for r in rows]
    assert spark.connector.query(sql).column("h").to_pylist() == expected


def test_row_hash_without_columns(spark: Evaluator) -> None:
    assert spark.column([1], pa.int64(), lambda c: row_hash_sql([], DIALECT)) == [row_hash([])]


@SPARK
@given(values=st.lists(_TEXT, min_size=1, max_size=100))
def test_hash_int_and_bucket_parity(spark: Evaluator, values: list[str]) -> None:
    granularities = [1, 7, 1024, 65536]
    exprs: list[str] = []
    expected: list[list[int]] = []
    for offset in (0, 8, 16, 24):
        hashed = DIALECT.hash_int(DIALECT.md5("v"), offset)
        exprs.append(hashed)
        ints = [
            int(
                hashlib.md5(v.encode("utf-8"), usedforsecurity=False).hexdigest()[
                    offset : offset + 8
                ],
                16,
            )
            for v in values
        ]
        expected.append(ints)
        for granularity in granularities:
            exprs.append(DIALECT.bucket(hashed, granularity))
            expected.append([h * granularity // HASH_SPACE for h in ints])
    got = spark.columns(values, pa.string(), exprs)
    assert [[int(x) for x in column] for column in got] == expected


def test_md5_is_utf8(spark: Evaluator) -> None:
    values = ["héllo", "ΑΣ\U0001f600", "a", ""]
    got = spark.column(values, pa.string(), DIALECT.md5)
    assert got == [hashlib.md5(v.encode(), usedforsecurity=False).hexdigest() for v in values]


# ---------------------------------------------------------------------- normalizers


def test_every_builtin_normalizer_has_a_parity_case() -> None:
    assert set(_NORMALIZER_INPUTS) == set(BUILTIN_NORMALIZERS)


_SPARK_TYPES = {
    Kind.TEXT: pa.string(),
    Kind.DECIMAL: pa.decimal128(24, 6),
    Kind.TIMESTAMP: pa.timestamp("us"),
}


@pytest.mark.parametrize("name", sorted(_NORMALIZER_INPUTS))
@SPARK
@given(data=st.data())
def test_normalizer_parity(
    spark: Evaluator,
    case_diffs: tuple[frozenset[str], frozenset[str]],
    name: str,
    data: st.DataObject,
) -> None:
    ctype, kind, params = _NORMALIZER_INPUTS[name]
    normalizer = BUILTIN_NORMALIZERS[name](params)
    if name == "unicode_nfc":
        with pytest.raises(UnsupportedFeatureError):
            normalizer.to_sql("v", DIALECT, ctype)
        return
    values = data.draw(values_of(_STRATEGY[kind]))
    if name in ("lower", "upper"):
        excluded = case_diffs[0 if name == "lower" else 1]
        values = [v for v in values if v is None or not (set(v) & excluded)]
    out_type = normalizer.output_type(ctype)
    spec = _spec(
        Kind.DECIMAL if out_type.logical is LogicalType.DECIMAL else kind, scale=out_type.scale or 0
    )
    spec = with_strings(spec, unicode_form="none")
    expected = [canonical_text(normalizer.to_python(v, ctype), spec) for v in values]
    expr = canonical_sql(normalizer.to_sql("v", DIALECT, ctype), spec, DIALECT)
    spark_type = NTZ if kind is Kind.TIMESTAMP else None
    got = spark.columns(values, _arrow_type(kind), [expr], spark_type)[0]
    mismatches = [(v, g, e) for v, g, e in zip(values, got, expected, strict=True) if g != e]
    assert not mismatches, mismatches[:5]


def test_round_normalizer_on_doubles(spark: Evaluator) -> None:
    normalizer = BUILTIN_NORMALIZERS["round"]({"digits": 2})
    ctype = CanonicalType.float(64)
    values = [1.005, 2.675, -0.125, 1e-9, None]
    spec = _spec(Kind.DECIMAL, scale=2)
    got = spark.column(
        values,
        pa.float64(),
        lambda c: canonical_sql(normalizer.to_sql(c, DIALECT, ctype), spec, DIALECT),
    )
    assert got == [canonical_text(normalizer.to_python(v, ctype), spec) for v in values]


# ---------------------------------------------------------------------- ANSI mode off


def test_every_kind_with_ansi_mode_off(spark: Evaluator) -> None:
    from dvf.connectors.spark import SparkConnector, child_session

    legacy = Evaluator.__new__(Evaluator)
    session = child_session(spark.session, {"spark.sql.ansi.enabled": "false"})
    assert session.conf.get("spark.sql.ansi.enabled") == "false"
    legacy.connector = SparkConnector.for_session(session, "legacy")
    cases: list[tuple[list[Any], pa.DataType, list[CanonSpec], str | None]] = [
        (
            [None, *EDGE_TEXT, "Ünïcödé  X"],
            pa.string(),
            [_spec(Kind.TEXT, strings=o) for o in ALL_STRING_OPTIONS[::5]],
            None,
        ),
        ([None, 0, -1, 2**63 - 1, -(2**63)], pa.int64(), [_spec(Kind.INTEGER)], None),
        (
            [None, *HALF_WAY, Decimal("123456789012.345678")],
            pa.decimal128(24, 6),
            [_spec(Kind.DECIMAL, scale=s) for s in (0, 2, 6)],
            None,
        ),
        ([None, True, False], pa.bool_(), [_spec(Kind.BOOLEAN)], None),
        ([None, date(1900, 1, 1), date(2024, 2, 29)], pa.date32(), [_spec(Kind.DATE)], None),
        (
            [None, *DST_EDGES],
            pa.timestamp("us"),
            [
                _spec(Kind.TIMESTAMP, precision="ms"),
                _spec(Kind.TIMESTAMP, precision="us", timezone="Asia/Kolkata"),
            ],
            NTZ,
        ),
        (
            [None, *(d.replace(tzinfo=UTC) for d in DST_EDGES)],
            pa.timestamp("us", tz="UTC"),
            [_spec(Kind.TIMESTAMP, precision="s", aware=True)],
            None,
        ),
        # Times arrive as text (Spark has no TIME type); the reference parses text too.
        (
            [None, "10:11:12.345678", "00:00:00"],
            pa.string(),
            [_spec(Kind.TIME, precision="ms")],
            None,
        ),
        ([None, b"", bytes([0xAB, 0])], pa.binary(), [_spec(Kind.BINARY)], None),
    ]
    for values, arrow_type, specs, spark_type in cases:
        assert_specs(legacy, values, specs, arrow_type, spark_type=spark_type)
