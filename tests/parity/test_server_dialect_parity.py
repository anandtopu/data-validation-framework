"""Parity of the MySQL, SQL Server, and Oracle dialects with the Python reference, on live servers.

Every canonical kind, the row hash, the hash-bucket primitives, and the built-in normalizers are
evaluated in each database on Hypothesis-generated values and compared with
:func:`~dvf.normalize.canonical.canonical_text`. Where a database fundamentally cannot match
the reference, the test asserts the documented behavior instead (see each dialect's docstring):

* Oracle stores ``''`` as NULL (empty canonical strings are NULL);
* MySQL and SQL Server have no Unicode normalization (skipped, as canonicalization notes);
* Oracle's COMPOSE only composes adjacent characters;
* SQL Server compares strings with trailing-space padding in ``NULLIF(x, '')`` and ``x = ''``;
* each database lower/upper-cases with its own Unicode tables, so characters whose case pairs
  are newer than those tables are excluded from case-insensitive cases (measured live);
* MySQL converts named time zones only inside the TIMESTAMP range, SQL Server with Windows
  rules (current rules for old dates).

Set ``DVF_TEST_MYSQL_URL``, ``DVF_TEST_MSSQL_URL``, and ``DVF_TEST_ORACLE_URL`` to run them.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

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
from dvf.sql.dialects import HASH_SPACE, UnsupportedFeatureError
from tests.server_backends import KIND_KEYS, SERVERS, Backend, make_backend

pytestmark = pytest.mark.integration

PARITY = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])

_ALL_CHARS = [
    chr(c) for c in range(1, 0x110000) if unicodedata.category(chr(c)) not in ("Cs", "Co", "Cn")
]
TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Co", "Cn"), blacklist_characters="\x00"),
    max_size=12,
)
EDGE_TEXT = [
    "",
    " ",
    "\u00a0x\u00a0",
    "  a  ",
    "\ta b\n",
    "é",
    "é",
    "ΑΣ",
    "İstanbul",
    "ß",
    "A  B",
    "0012",
    "a\U0001f600 ",
    "\U00010400x",
    "x　y",
    "Ⱥy",
    "ɐz",
    "  ",
]


@pytest.fixture(scope="module", params=SERVERS)
def server(request: pytest.FixtureRequest) -> Iterator[Backend]:
    backend = make_backend(request.param)
    try:
        yield backend
    finally:
        backend.close()


# ---------------------------------------------------------------------- documented behavior


def case_diffs(server: Backend, upper: bool) -> frozenset[str]:
    """Characters whose case mapping in the database differs from the reference."""
    func = server.dialect.upper if upper else server.dialect.lower
    outputs = server.map_chars(f"case_{upper}", func, _ALL_CHARS)
    reference = simple_upper if upper else simple_lower
    return frozenset(c for c, out in zip(_ALL_CHARS, outputs, strict=True) if out != reference(c))


def empty_equivalents(server: Backend) -> frozenset[str]:
    """Characters equal to ``''`` under the default collation (ignorable characters)."""
    if server.name == "oracle":
        return frozenset()
    outputs = server.map_chars(
        "ignorable", lambda v: f"CASE WHEN {v} = '' THEN 1 ELSE 0 END", _ALL_CHARS
    )
    # The space equals '' on SQL Server through padding, which reference() models instead.
    return frozenset(c for c, out in zip(_ALL_CHARS, outputs, strict=True) if out == 1 and c != " ")


def without(values: Sequence[Any], excluded: frozenset[str]) -> list[Any]:
    return [v for v in values if not isinstance(v, str) or not (set(v) & excluded)]


def oracle_normalize(text: str, form: str) -> str:
    """Oracle's COMPOSE(DECOMPOSE(x)): each character is decomposed on its own (marks of
    neighboring characters are not reordered), then only adjacent characters compose."""
    if form in ("NFD", "NFKD"):
        return unicodedata.normalize(form, text)
    base = "NFD" if form == "NFC" else "NFKD"
    decomposed = "".join(unicodedata.normalize(base, ch) for ch in text)
    out: list[str] = []
    for ch in decomposed:
        if out:
            pair = unicodedata.normalize("NFC", out[-1] + ch)
            if len(pair) == 1:
                out[-1] = pair
                continue
        out.append(ch)
    return "".join(out)


def stored(server: Backend, value: Any) -> Any:
    """The value as the database stores it (Oracle stores '' and b'' as NULL)."""
    if server.name == "oracle" and value in ("", b""):
        return None
    return value


def reference(server: Backend, value: Any, spec: CanonSpec) -> str | None:
    """The canonical text the database is documented to produce for ``value``."""
    value = stored(server, value)
    if value is None:
        return None
    if spec.kind is not Kind.TEXT:
        out = canonical_text(value, spec)
        return None if server.name == "oracle" and out == "" else out
    opts = spec.strings
    form = opts.unicode_form
    if form != "none" and not server.dialect.supports_unicode_form(form):
        spec = with_strings(spec, unicode_form="none")
    elif form != "none" and server.name == "oracle":
        value = oracle_normalize(str(value), form)
        spec = with_strings(spec, unicode_form="none")
    out = canonical_text(value, spec)
    return None if server.name == "oracle" and out == "" else out


def assert_parity(
    server: Backend, values: Sequence[Any], spec: CanonSpec, key: str | None = None
) -> None:
    key = key or KIND_KEYS[spec.kind]
    got = server.evaluate(key, values, lambda c: canonical_sql(c, spec, server.dialect))
    expected = [reference(server, v, spec) for v in values]
    mismatches = [(v, g, e) for v, g, e in zip(values, got, expected, strict=True) if g != e]
    assert not mismatches, f"{server.name} differs from the reference: {mismatches[:5]}"


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


# ---------------------------------------------------------------------- text

string_options = st.builds(
    StringOptions,
    trim=st.sampled_from(["none", "rtrim", "ltrim", "both"]),
    case_sensitive=st.booleans(),
    unicode_form=st.sampled_from(["NFC", "NFD", "NFKC", "NFKD", "none"]),
    collapse_whitespace=st.booleans(),
    null_equals_empty=st.booleans(),
)


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), TEXT, st.sampled_from(EDGE_TEXT)), max_size=25),
    opts=string_options,
)
def test_text_parity(server: Backend, values: list[str | None], opts: StringOptions) -> None:
    excluded: frozenset[str] = frozenset()
    if not opts.case_sensitive:
        excluded |= case_diffs(server, upper=False)
    if opts.null_equals_empty:
        excluded |= empty_equivalents(server)
    assert_parity(server, without(values, excluded), _spec(Kind.TEXT, strings=opts))


def test_case_mapping_differences_are_bounded(server: Backend) -> None:
    """Case mapping differs from the reference only for characters with recent case pairs."""
    limits = {"mysql": (200, 250), "mssql": (600, 650), "oracle": (60, 100)}
    lower, upper = case_diffs(server, upper=False), case_diffs(server, upper=True)
    assert len(lower) <= limits[server.name][0], sorted(map(hex, map(ord, lower)))[:20]
    assert len(upper) <= limits[server.name][1], sorted(map(hex, map(ord, upper)))[:20]
    # Characters that grow in UTF-8 (MySQL truncates after them) and ß are mapped explicitly.
    for ch in "ȺȾ":
        assert ch not in lower
    for ch in "ßɐɑɒȿɀ":
        assert ch not in upper


def test_unicode_normalization_support(server: Backend) -> None:
    d = server.dialect
    for form in ("NFC", "NFD", "NFKC", "NFKD"):
        assert d.supports_unicode_form(form) is (server.name == "oracle")
        if server.name != "oracle":
            with pytest.raises(UnsupportedFeatureError):
                d.unicode_normalize("v", form)


def test_oracle_composition_is_adjacent_only(server: Backend) -> None:
    if server.name != "oracle":
        pytest.skip("Oracle-specific behavior")
    spec = _spec(Kind.TEXT, strings=StringOptions(unicode_form="NFC"))
    values = ["é̖", "ậ", "각"]
    got = server.evaluate("text", values, lambda c: canonical_sql(c, spec, server.dialect))
    assert got == ["é̖", "ậ", "각"]
    assert unicodedata.normalize("NFC", values[0]) == "é̖"  # the reference composes


def test_empty_strings(server: Backend) -> None:
    spec = _spec(Kind.TEXT, strings=StringOptions(trim="both"))
    got = server.evaluate(
        "text", ["", "   ", "a"], lambda c: canonical_sql(c, spec, server.dialect)
    )
    if server.name == "oracle":
        assert got == [None, None, "a"]  # Oracle cannot tell '' from NULL
    else:
        assert got == ["", "", "a"]


def test_null_equals_empty_keeps_spaces(server: Backend) -> None:
    """Emptiness is tested by length, so SQL Server's padded '' comparison does not apply."""
    spec = _spec(Kind.TEXT, strings=StringOptions(null_equals_empty=True))
    got = server.evaluate(
        "text", ["", "  ", " a"], lambda c: canonical_sql(c, spec, server.dialect)
    )
    assert got == [None, "  ", " a"]


def test_length_counts_code_points_and_trailing_spaces(server: Backend) -> None:
    values = ["a  ", "\U0001f600x", "é", None, " "]
    got = server.evaluate("text", values, lambda c: server.dialect.length(c))
    expected = [None if stored(server, v) is None else len(v) for v in values]
    assert [None if g is None else int(g) for g in got] == expected


def test_text_equality_is_exact(server: Backend) -> None:
    d = server.dialect
    rows = [
        ("a", "a "),
        ("a", "A"),
        ("e", "é"),
        ("x", "x"),
        ("x", None),
        (None, None),
        ("a­b", "ab"),
    ]
    table = server.load_rows("distinct", [_text_type(server)] * 2, rows)
    result = server.connector.query(
        f"SELECT {d.boolean(d.is_distinct('c0', 'c1'))} AS c FROM {table} ORDER BY i"
    )
    assert [int(v) for v in result.column("c").to_pylist()] == [1, 1, 1, 0, 1, 0, 1]


def _text_type(server: Backend) -> str:
    return server.kinds["text"].sql_type


# ---------------------------------------------------------------------- numbers, booleans, dates


@PARITY
@given(values=st.lists(st.one_of(st.none(), st.integers(-(2**63), 2**63 - 1)), max_size=25))
def test_integer_parity(server: Backend, values: list[int | None]) -> None:
    assert_parity(server, values, _spec(Kind.INTEGER))


decimals = st.decimals(
    min_value=Decimal("-999999999999.999999"),
    max_value=Decimal("999999999999.999999"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)
HALF_WAY = [
    Decimal("-0.0005"),
    Decimal("1.0005"),
    Decimal("-1.005"),
    Decimal("0.5"),
    Decimal("-0.5"),
]


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), decimals, st.sampled_from(HALF_WAY)), max_size=25),
    scale=st.integers(0, 6),
)
def test_decimal_parity(server: Backend, values: list[Decimal | None], scale: int) -> None:
    assert_parity(server, values, _spec(Kind.DECIMAL, scale=scale))


def test_decimal_unknown_scale_and_negative_zero(server: Backend) -> None:
    values = [Decimal("-0.0000000000001"), Decimal("-0.000001"), Decimal("123456789012.345678")]
    for scale in (0, 3, 10):
        assert_parity(server, values, _spec(Kind.DECIMAL, scale=scale))


@PARITY
@given(values=st.lists(st.one_of(st.none(), st.booleans()), max_size=10))
def test_boolean_parity(server: Backend, values: list[bool | None]) -> None:
    assert_parity(server, values, _spec(Kind.BOOLEAN))


@PARITY
@given(
    values=st.lists(
        st.one_of(st.none(), st.dates(date(1900, 1, 1), date(2100, 12, 31))), max_size=25
    )
)
def test_date_parity(server: Backend, values: list[date | None]) -> None:
    assert_parity(server, values, _spec(Kind.DATE))


naive_datetimes = st.datetimes(datetime(1900, 1, 1), datetime(2100, 12, 31))
DST_EDGES = [
    datetime(2024, 3, 31, 1, 59, 59, 999999),
    datetime(2024, 10, 27, 0, 30),
    datetime(1960, 2, 29, 23, 59, 59, 123456),
    datetime(1969, 12, 31, 23, 59, 59, 999999),
    datetime(1900, 1, 1, 0, 0, 0, 1),
]


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), naive_datetimes, st.sampled_from(DST_EDGES)), max_size=25),
    precision=st.sampled_from(["s", "ms", "us"]),
)
def test_timestamp_utc_parity(
    server: Backend, values: list[datetime | None], precision: str
) -> None:
    assert_parity(server, values, _spec(Kind.TIMESTAMP, precision=precision))


ZONE_RANGES = {
    # MySQL converts named zones only inside the TIMESTAMP range.
    "mysql": (datetime(1970, 1, 2), datetime(2037, 12, 31)),
    # SQL Server uses Windows rules, which apply the current rules to older dates.
    "mssql": (datetime(2008, 1, 1), datetime(2037, 12, 31)),
    # Oracle offsets have whole minutes: local mean time offsets with seconds (before ~1906,
    # depending on the zone) are rounded; its time zone file lists transitions up to 2080.
    "oracle": (datetime(1910, 1, 1), datetime(2080, 12, 31)),
}


def _near_transition(value: datetime, zone: str) -> bool:
    tz = ZoneInfo(zone)
    before = value.replace(tzinfo=tz, fold=0).utcoffset()
    after = value.replace(tzinfo=tz, fold=1).utcoffset()
    roundtrip = value.replace(tzinfo=tz).astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    return before != after or roundtrip != value


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), naive_datetimes, st.sampled_from(DST_EDGES)), max_size=20),
    zone=st.sampled_from(["Europe/Berlin", "America/New_York", "Asia/Kolkata"]),
)
def test_timestamp_local_zone_parity(
    server: Backend, values: list[datetime | None], zone: str
) -> None:
    low, high = ZONE_RANGES[server.name]
    safe = [v for v in values if v is None or (low <= v <= high and not _near_transition(v, zone))]
    assert_parity(server, safe, _spec(Kind.TIMESTAMP, precision="us", timezone=zone))


def test_timestamp_local_zone_documented_limits(server: Backend) -> None:
    spec = _spec(Kind.TIMESTAMP, precision="s", timezone="Europe/Berlin")
    values = [datetime(1960, 7, 1, 10), datetime(2024, 7, 1, 10)]
    got = server.evaluate("timestamp", values, lambda c: canonical_sql(c, spec, server.dialect))
    assert got[1] == "2024-07-01T08:00:00"
    # IANA: Germany had no daylight saving time in 1960 (UTC+1).
    assert canonical_text(values[0], spec) == "1960-07-01T09:00:00"
    documented = {
        "mysql": "1960-07-01T10:00:00",  # outside the TIMESTAMP range: not converted
        "mssql": "1960-07-01T08:00:00",  # Windows rules: today's summer time applied
        "oracle": "1960-07-01T09:00:00",  # Oracle's time zone file has the history
    }
    assert got[0] == documented[server.name]


def test_timestamp_in_dst_gap_does_not_fail(server: Backend) -> None:
    spec = _spec(Kind.TIMESTAMP, precision="s", timezone="Europe/Berlin")
    got = server.evaluate(
        "timestamp",
        [datetime(2024, 3, 31, 2, 30)],
        lambda c: canonical_sql(c, spec, server.dialect),
    )
    assert got[0] is not None and got[0].startswith("2024-03-31T0")


AWARE_RANGES = {
    "mysql": (datetime(1970, 1, 2, tzinfo=UTC), datetime(2038, 1, 18, tzinfo=UTC)),
    "mssql": (datetime(1900, 1, 1, tzinfo=UTC), datetime(2100, 12, 31, tzinfo=UTC)),
    "oracle": (datetime(1900, 1, 1, tzinfo=UTC), datetime(2100, 12, 31, tzinfo=UTC)),
}


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
def test_timestamp_aware_parity(server: Backend, values: list[datetime | None]) -> None:
    low, high = AWARE_RANGES[server.name]  # MySQL TIMESTAMP holds 1970..2038 only
    safe = [v for v in values if v is None or low <= v <= high]
    assert_parity(server, safe, _spec(Kind.TIMESTAMP, precision="ms", aware=True), "timestamp_tz")


def test_mssql_datetime_rounds_to_milliseconds(server: Backend) -> None:
    if server.name != "mssql":
        pytest.skip("SQL Server DATETIME")
    values = [
        datetime(2024, 1, 1, 10, 0, 0, 3000),
        datetime(2024, 1, 1, 10, 0, 0, 7000),
        datetime(2024, 1, 1, 23, 59, 59, 997000),
        datetime(1960, 2, 29, 12, 0, 0, 123000),
    ]
    spec = _spec(Kind.TIMESTAMP, precision="us")
    table = server.load("datetime", values)
    fetched = server.connector.query(f"SELECT v FROM {table} ORDER BY i").column("v").to_pylist()
    got = server.evaluate("datetime", values, lambda c: canonical_sql(c, spec, server.dialect))
    # Canonical text matches what the driver (and SQL Server itself) shows: milliseconds.
    assert got == [canonical_text(v, spec) for v in fetched]
    assert got[1] == "2024-01-01T10:00:00.007000"


@PARITY
@given(
    values=st.lists(st.one_of(st.none(), st.times()), max_size=20),
    precision=st.sampled_from(["s", "ms", "us"]),
)
def test_time_parity(server: Backend, values: list[time | None], precision: str) -> None:
    assert_parity(server, values, _spec(Kind.TIME, precision=precision))


@PARITY
@given(values=st.lists(st.one_of(st.none(), st.binary(max_size=16)), max_size=20))
def test_binary_parity(server: Backend, values: list[bytes | None]) -> None:
    assert_parity(server, values, _spec(Kind.BINARY))


def test_epoch_microseconds(server: Backend) -> None:
    values = [datetime(1969, 12, 31, 23, 59, 59, 999999), datetime(2024, 2, 29, 12, 0, 0, 1), None]
    got = server.evaluate(
        "timestamp", values, lambda c: server.dialect.epoch_us(c, tz_aware=False, timezone="UTC")
    )
    epoch = datetime(1970, 1, 1)
    micro = timedelta(microseconds=1)
    expected = [None if v is None else float((v - epoch) // micro) for v in values]
    assert [None if g is None else float(g) for g in got] == expected


# ---------------------------------------------------------------------- row hash and buckets


@PARITY
@given(rows=st.lists(st.lists(st.one_of(st.none(), TEXT), min_size=3, max_size=3), max_size=20))
def test_row_hash_parity(server: Backend, rows: list[list[str | None]]) -> None:
    table = server.load_rows("hash", [_text_type(server)] * 3, rows)
    cols = ["c0", "c1", "c2"]
    result = server.connector.query(
        f"SELECT {row_hash_sql(cols, server.dialect)} AS h FROM {table} ORDER BY i"
    )
    expected = [row_hash([stored(server, v) for v in r]) for r in rows]
    assert result.column("h").to_pylist() == expected


def test_row_hash_of_mixed_kinds(server: Backend) -> None:
    """Canonical expressions of several kinds serialized together, as projections do."""
    types = {
        "mysql": ["BIGINT", "DECIMAL(12,3)", "DATETIME(6)", "LONGTEXT"],
        "mssql": ["BIGINT", "DECIMAL(12,3)", "DATETIME2(6)", "NVARCHAR(MAX)"],
        "oracle": ["NUMBER(19)", "NUMBER(12,3)", "TIMESTAMP(6)", "VARCHAR2(4000 CHAR)"],
    }[server.name]
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
    if server.name == "oracle":
        import oracledb

        table = server.tables.get("mixed")
        if table is None:
            server.load_rows("mixed", types, [])
        table = server.tables["mixed"]
        server.execute(f"DELETE FROM {table}")
        server.executemany(
            f"INSERT INTO {table} (i, c0, c1, c2, c3) VALUES (:1, :2, :3, :4, :5)",
            [(i, *r) for i, r in enumerate(rows)],
            [None, None, None, oracledb.DB_TYPE_TIMESTAMP, None],
        )
    else:
        table = server.load_rows("mixed", types, rows)
    exprs = [canonical_sql(f"c{j}", s, server.dialect) for j, s in enumerate(specs)]
    result = server.connector.query(
        f"SELECT {row_hash_sql(exprs, server.dialect)} AS h FROM {table} ORDER BY i"
    )
    expected = [
        row_hash([reference(server, v, s) for v, s in zip(r, specs, strict=True)]) for r in rows
    ]
    assert result.column("h").to_pylist() == expected


def test_row_hash_without_columns(server: Backend) -> None:
    got = server.evaluate("integer", [1], lambda c: row_hash_sql([], server.dialect))
    assert got == [row_hash([])]


@PARITY
@given(
    values=st.lists(TEXT, min_size=1, max_size=20), granularity=st.sampled_from([1, 7, 1024, 65536])
)
def test_hash_int_and_bucket_parity(server: Backend, values: list[str], granularity: int) -> None:
    d = server.dialect
    for offset in (0, 8, 16, 24):
        got = server.evaluate("text", values, lambda c, o=offset: d.hash_int(d.md5(c), o))
        buckets = server.evaluate(
            "text", values, lambda c, o=offset: d.bucket(d.hash_int(d.md5(c), o), granularity)
        )
        # Oracle stores '' as NULL, and STANDARD_HASH(NULL) is the digest of the empty string,
        # so '' hashes like the reference there too.
        expected: list[int] = []
        expected_buckets: list[int] = []
        for v in values:
            digest = hashlib.md5(v.encode("utf-8"), usedforsecurity=False).hexdigest()
            h = int(digest[offset : offset + 8], 16)
            expected.append(h)
            expected_buckets.append(h * granularity // HASH_SPACE)
        assert [None if g is None else int(g) for g in got] == expected
        assert [None if b is None else int(b) for b in buckets] == expected_buckets


def test_md5_is_utf8(server: Backend) -> None:
    values = ["héllo", "ΑΣ\U0001f600", "a"]
    got = server.evaluate("text", values, lambda c: server.dialect.md5(c))
    assert got == [hashlib.md5(v.encode(), usedforsecurity=False).hexdigest() for v in values]


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
    Kind.TEXT: st.one_of(TEXT, st.sampled_from([*EDGE_TEXT, "000", "0", "007", " x ", "00 "])),
    Kind.DECIMAL: st.one_of(decimals, st.sampled_from(HALF_WAY)),
    Kind.TIMESTAMP: st.one_of(naive_datetimes, st.sampled_from(DST_EDGES)),
}


def test_every_builtin_normalizer_has_a_parity_case() -> None:
    assert set(_NORMALIZER_INPUTS) == set(BUILTIN_NORMALIZERS)


def normalized_reference(
    server: Backend, name: str, normalizer: Any, value: Any, ctype: CanonicalType
) -> Any:
    """The normalizer's documented output on ``server`` (before canonicalization)."""
    value = stored(server, value)
    if server.name == "oracle" and name == "empty_if_null" and value is None:
        return None  # Oracle stores '' as NULL
    # null_if_empty and strip_leading_zeros test emptiness by length, so they match everywhere.
    return normalizer.to_python(value, ctype)


@pytest.mark.parametrize("name", sorted(_NORMALIZER_INPUTS))
@PARITY
@given(data=st.data())
def test_normalizer_parity(server: Backend, name: str, data: st.DataObject) -> None:
    ctype, kind, params = _NORMALIZER_INPUTS[name]
    normalizer = BUILTIN_NORMALIZERS[name](params)
    d = server.dialect
    if name == "unicode_nfc" and not d.supports_unicode_form("NFC"):
        with pytest.raises(UnsupportedFeatureError):
            normalizer.to_sql("v", d, ctype)
        return
    values = data.draw(st.lists(st.one_of(st.none(), _STRATEGY[kind]), max_size=20))
    excluded: frozenset[str] = frozenset()
    if name in ("lower", "upper"):
        excluded |= case_diffs(server, upper=name == "upper")
    if name in ("null_if_empty", "strip_leading_zeros", "empty_if_null"):
        excluded |= empty_equivalents(server)
    if name == "to_utc":
        low, high = ZONE_RANGES[server.name]
        values = [
            v
            for v in values
            if v is None or (low <= v <= high and not _near_transition(v, "Asia/Kolkata"))
        ]
    values = without(values, excluded)
    out_type = normalizer.output_type(ctype)
    spec = _spec(
        Kind.DECIMAL if out_type.logical is LogicalType.DECIMAL else kind, scale=out_type.scale or 0
    )
    got = server.evaluate(
        KIND_KEYS[kind], values, lambda c: canonical_sql(normalizer.to_sql(c, d, ctype), spec, d)
    )
    expected = [
        reference(server, normalized_reference(server, name, normalizer, v, ctype), spec)
        for v in values
    ]
    mismatches = [(v, g, e) for v, g, e in zip(values, got, expected, strict=True) if g != e]
    assert not mismatches, f"{server.name} {name}: {mismatches[:5]}"


# ---------------------------------------------------------------------- SQL generation on servers


def test_limit_and_subquery_run(server: Backend) -> None:
    d = server.dialect
    table = server.load("integer", [3, 1, 2, 5, 4])
    ordered = d.limit(f"SELECT v FROM {table} ORDER BY v", 2)
    wrapped = f"SELECT * FROM {d.subquery(ordered, 'q')}"
    assert sorted(server.connector.query(wrapped).column("v").to_pylist()) == [1, 2]
    grouped = d.limit(f"SELECT v, COUNT(*) AS n FROM {table} GROUP BY v ORDER BY n DESC, v", 1)
    assert server.connector.query(grouped).num_rows == 1
    assert server.connector.query(d.limit(f"SELECT v FROM {table}", 0)).num_rows == 0
    union = d.limit(f"SELECT v FROM {table} UNION ALL SELECT v FROM {table}", 3)
    assert server.connector.query(union).num_rows == 3


def test_greatest_count_if_and_boolean(server: Backend) -> None:
    d = server.dialect
    table = server.load("integer", [3, -7, None])
    sql = f"SELECT {d.greatest('v', '0')} AS g, {d.boolean('v > 0')} AS b FROM {table} ORDER BY i"
    result = server.connector.query(sql).to_pylist()
    assert [None if r["b"] is None else int(r["b"]) for r in result] == [1, 0, None]
    assert [None if r["g"] is None else int(r["g"]) for r in result][:2] == [3, 0]
    count = server.connector.query(f"SELECT {d.count_if('v < 0')} AS n FROM {table}")
    assert int(count.column("n")[0].as_py()) == 1


def test_regex_match(server: Backend) -> None:
    d = server.dialect
    if server.name == "mssql":
        with pytest.raises(UnsupportedFeatureError):
            d.regex_match("v", "a+", full=True)
        return
    values = ["aaa", "AAA", "aab", "aaa\n", "xaaa"]
    full = server.evaluate("text", values, lambda c: d.boolean(d.regex_match(c, "a+", full=True)))
    part = server.evaluate("text", values, lambda c: d.boolean(d.regex_match(c, "a+", full=False)))
    assert [int(v) for v in full] == [1, 0, 0, 0, 0]
    assert [int(v) for v in part] == [1, 0, 1, 1, 1]


def test_trim_other_characters(server: Backend) -> None:
    d = server.dialect
    values = ["00120", "000", "0 0", "x"]
    for mode, func in (("ltrim", str.lstrip), ("rtrim", str.rstrip), ("both", str.strip)):
        got = server.evaluate("text", values, lambda c, m=mode: d.trim(c, m, "0"))
        expected = [func(v, "0") for v in values]
        if server.name == "oracle":
            expected = [e or None for e in expected]
        assert got == expected, mode


def test_current_timestamp_is_utc(server: Backend) -> None:
    got = server.evaluate(
        "integer",
        [1],
        lambda c: server.dialect.timestamp_text(
            server.dialect.current_timestamp_utc(),
            precision="s",
            tz_aware=False,
            timezone="UTC",
            from_date=False,
        ),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    parsed = datetime.fromisoformat(got[0])
    assert abs((parsed - now).total_seconds()) < 300
