"""SQL dialects for Snowflake, BigQuery, Redshift, and Databricks (beta).

No live service is available, so these tests prove that every SQL fragment and full query dvf
generates for the cloud dialects parses with sqlglot in that dialect, pin the tricky primitives
with golden strings, and run semantically equivalent SQL in DuckDB where the functions coincide.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import duckdb
import pytest
import sqlglot

from dvf.checks.quality import AllowedValues, Length, NotBlank, NotNull, Range, Regex
from dvf.compare.plan import ComparisonPlan, build_plan, pair_columns
from dvf.config import checks as cfg
from dvf.config.base import NormalizerRef
from dvf.config.models import NormalizationConfig, TableConfig
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.types import CanonicalType, LogicalType, parse_native_type
from dvf.engines.base import (
    SqlDiff,
    duplicate_probe_sql,
    key_list_predicate,
    key_match_predicate,
    key_names,
    projection_sql,
)
from dvf.normalize.canonical import (
    CanonSpec,
    Kind,
    NumericCompare,
    StringOptions,
    canonical_sql,
    field_sql,
    numeric_sql,
    row_hash,
    row_hash_sql,
    simple_lower,
    simple_upper,
)
from dvf.normalize.normalizers import BUILTIN_NORMALIZERS
from dvf.plugins.base import Normalizer
from dvf.sql.bigquery import BigQueryDialect
from dvf.sql.databricks import DatabricksDialect
from dvf.sql.dialects import HASH_SPACE, Dialect, UnsupportedFeatureError, get_dialect
from dvf.sql.redshift import RedshiftDialect
from dvf.sql.snowflake import (
    CAPITAL_SHARP_S,
    CAPITAL_SIGMA,
    DOTTED_CAPITAL_I,
    SHARP_S,
    SMALL_SIGMA,
    SnowflakeDialect,
)

CLOUD = ["snowflake", "bigquery", "redshift", "databricks"]
CLASSES = {
    "snowflake": SnowflakeDialect,
    "bigquery": BigQueryDialect,
    "redshift": RedshiftDialect,
    "databricks": DatabricksDialect,
}


def parses(sql: str, dialect: str) -> None:
    """Fail unless ``sql`` is one statement that sqlglot parses in ``dialect``."""
    statements = sqlglot.parse(sql, read=dialect)
    assert len(statements) == 1 and statements[0] is not None, sql


def parses_expr(expr: str, dialect: str) -> None:
    parses(f"SELECT {expr} AS v FROM t", dialect)


@pytest.fixture(params=CLOUD)
def name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def dialect(name: str) -> Dialect:
    return get_dialect(name)


def test_get_dialect_loads_each_module(name: str) -> None:
    found = get_dialect(name)
    assert isinstance(found, CLASSES[name]) and found.name == name
    assert get_dialect(name) is found


# ====================================================================== canonical expressions

_STRINGS = [
    StringOptions(),
    StringOptions(
        trim="both", case_sensitive=False, collapse_whitespace=True, null_equals_empty=True
    ),
    StringOptions(trim="rtrim", unicode_form="NFKD"),
    StringOptions(trim="ltrim", unicode_form="none", case_sensitive=False),
]


def _specs() -> Iterator[tuple[str, CanonSpec]]:
    for i, strings in enumerate(_STRINGS):
        yield f"text{i}", CanonSpec(Kind.TEXT, LogicalType.STRING, strings=strings)
        yield f"text_from_int{i}", CanonSpec(Kind.TEXT, LogicalType.INTEGER, strings=strings)
    yield "integer", CanonSpec(Kind.INTEGER, LogicalType.INTEGER)
    for scale in (0, 2, 10, 18):
        yield f"decimal{scale}", CanonSpec(Kind.DECIMAL, LogicalType.DECIMAL, scale=scale)
    yield "float", CanonSpec(Kind.FLOAT, LogicalType.FLOAT)
    yield "boolean", CanonSpec(Kind.BOOLEAN, LogicalType.BOOLEAN)
    yield "date", CanonSpec(Kind.DATE, LogicalType.DATE)
    for precision in ("s", "ms", "us", "ns"):
        yield f"time_{precision}", CanonSpec(Kind.TIME, LogicalType.TIME, precision=precision)
        for logical, zone in itertools.product(
            (LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ, LogicalType.DATE),
            ("UTC", "America/New_York"),
        ):
            yield (
                f"ts_{precision}_{logical.value}_{zone}",
                CanonSpec(
                    Kind.TIMESTAMP,
                    logical,
                    precision=precision,
                    timezone=zone,
                    tz_aware=logical is LogicalType.TIMESTAMP_TZ,
                ),
            )
    yield "binary", CanonSpec(Kind.BINARY, LogicalType.BINARY)


SPECS = dict(_specs())


@pytest.mark.parametrize("spec_name", sorted(SPECS))
def test_canonical_sql_parses(name: str, dialect: Dialect, spec_name: str) -> None:
    spec = SPECS[spec_name]
    expr = canonical_sql(dialect.quote("col"), spec, dialect)
    parses_expr(expr, name)
    parses_expr(field_sql(expr, dialect), name)


@pytest.mark.parametrize(
    ("logical", "tz_aware", "zone"),
    [
        (LogicalType.TIMESTAMP, False, "UTC"),
        (LogicalType.TIMESTAMP, False, "Asia/Kolkata"),
        (LogicalType.TIMESTAMP_TZ, True, "UTC"),
        (LogicalType.DECIMAL, False, "UTC"),
    ],
)
def test_numeric_sql_parses(
    name: str, dialect: Dialect, logical: LogicalType, tz_aware: bool, zone: str
) -> None:
    epoch = logical is not LogicalType.DECIMAL
    spec = CanonSpec(
        Kind.TIMESTAMP if epoch else Kind.FLOAT, logical, timezone=zone, tz_aware=tz_aware
    )
    parses_expr(numeric_sql(dialect.quote("c"), spec, NumericCompare(epoch=epoch), dialect), name)


def test_row_hash_sql_parses(name: str, dialect: Dialect) -> None:
    parses_expr(row_hash_sql([], dialect), name)
    parses_expr(
        row_hash_sql([dialect.quote("a"), dialect.to_text(dialect.quote("b"))], dialect), name
    )


def test_hash_and_bucket_primitives_parse(name: str, dialect: Dialect) -> None:
    digest = row_hash_sql([dialect.quote("k")], dialect)
    for offset in (0, 8, 24):
        parses_expr(dialect.hash_int(digest, offset), name)
    x = dialect.hash_int("kh", 0)
    for granularity in (1, 256, 2**31):
        bucket = dialect.bucket(x, granularity)
        parses(
            f"SELECT {bucket} AS g, COUNT(*) AS n, SUM({dialect.hash_int('h', 0)}) AS s1 "
            f"FROM t GROUP BY {bucket}",
            name,
        )
    parses_expr(dialect.int_div("(v - 10)", 16), name)


def test_other_primitives_parse(name: str, dialect: Dialect) -> None:
    c = dialect.quote("c")
    exprs = [
        dialect.to_double(c),
        dialect.length(c),
        dialect.is_distinct(c, "d"),
        dialect.greatest("0.5", f"0.1 * {dialect.greatest(f'ABS({c})', 'ABS(d)')}"),
        dialect.concat("'a'", c, "'|'"),
        dialect.md5(c),
        dialect.boolean(f"{c} > 1"),
        dialect.count_if(f"{c} IS NULL"),
        dialect.lower(c),
        dialect.upper(c),
        dialect.trim(c, "ltrim", "0"),
        dialect.collapse_whitespace(c),
        dialect.regex_match(c, r"^[A-Z]{2}\d+$|x", full=True),
        dialect.regex_match(c, r"\s", full=False),
        dialect.integer_text(c),
        dialect.round_decimal(c, 4),
        dialect.truncate_timestamp(c, "millisecond"),
        dialect.to_utc(c, tz_aware=True, timezone="UTC"),
        dialect.to_utc(c, tz_aware=False, timezone="Europe/London"),
        dialect.current_timestamp_utc(),
        dialect.literal(None),
        dialect.literal(True),
        dialect.literal(1.5e-7),
    ]
    for unit in ("second", "minute", "hour", "day", "millisecond"):
        exprs.append(dialect.truncate_timestamp(c, unit))
    for expr in exprs:
        parses_expr(expr, name)
    parses(dialect.limit("SELECT * FROM t ORDER BY a", 10), name)
    parses(f"SELECT * FROM {dialect.subquery('SELECT 1 AS a', 'dvf_x')}", name)


def _factory(ref: NormalizerRef) -> Normalizer:
    return BUILTIN_NORMALIZERS[ref.name](ref.params)


@pytest.mark.parametrize(
    ("normalizer", "params", "ctype"),
    [
        ("trim", {}, CanonicalType.string()),
        ("rtrim", {}, CanonicalType.string()),
        ("ltrim", {}, CanonicalType.string()),
        ("lower", {}, CanonicalType.string()),
        ("upper", {}, CanonicalType.string()),
        ("collapse_whitespace", {}, CanonicalType.string()),
        ("null_if_empty", {}, CanonicalType.string()),
        ("empty_if_null", {}, CanonicalType.string()),
        ("strip_leading_zeros", {}, CanonicalType.string()),
        ("round", {"value": 2}, CanonicalType.decimal(12, 4)),
        ("truncate_timestamp", {"value": "ms"}, CanonicalType.timestamp(6)),
        ("truncate_timestamp", {"value": "day"}, CanonicalType.timestamp(6, tz=True)),
        ("to_utc", {"value": "Europe/Berlin"}, CanonicalType.timestamp(6)),
        ("to_utc", {}, CanonicalType.timestamp(6, tz=True)),
    ],
)
def test_normalizer_sql_parses(
    name: str, dialect: Dialect, normalizer: str, params: dict[str, Any], ctype: CanonicalType
) -> None:
    sql = _factory(NormalizerRef(name=normalizer, params=params)).to_sql(
        dialect.quote("c"), dialect, ctype
    )
    parses_expr(sql, name)


def test_unicode_normalization_support(name: str, dialect: Dialect) -> None:
    unicode_nfc = _factory(NormalizerRef(name="unicode_nfc"))
    if name == "bigquery":
        for form in ("NFC", "NFD", "NFKC", "NFKD"):
            assert dialect.supports_unicode_form(form)
            parses_expr(dialect.unicode_normalize("c", form), name)
        assert unicode_nfc.to_sql("c", dialect, CanonicalType.string()) == "NORMALIZE(c, NFC)"
        return
    assert not any(dialect.supports_unicode_form(f) for f in ("NFC", "NFD", "NFKC", "NFKD"))
    with pytest.raises(UnsupportedFeatureError, match="unicode_form to none"):
        unicode_nfc.to_sql("c", dialect, CanonicalType.string())
    # Canonicalization skips it rather than failing.
    spec = CanonSpec(Kind.TEXT, LogicalType.STRING, strings=StringOptions(unicode_form="NFC"))
    assert canonical_sql("c", spec, dialect) == "c"


# ====================================================================== full queries

_NATIVE = {
    "snowflake": {
        "id": "NUMBER(38,0)",
        "code": "VARCHAR(10)",
        "amount": "NUMBER(12,2)",
        "ratio": "FLOAT",
        "name": "VARCHAR(16777216)",
        "created": "TIMESTAMP_LTZ(9)",
        "loaded": "TIMESTAMP_NTZ(9)",
        "day": "DATE",
        "flag": "BOOLEAN",
        "payload": "VARBINARY(16)",
        "at": "TIME(9)",
    },
    "bigquery": {
        "id": "INT64",
        "code": "STRING",
        "amount": "NUMERIC(12, 2)",
        "ratio": "FLOAT64",
        "name": "STRING",
        "created": "TIMESTAMP",
        "loaded": "DATETIME",
        "day": "DATE",
        "flag": "BOOL",
        "payload": "BYTES",
        "at": "TIME",
    },
    "redshift": {
        "id": "bigint",
        "code": "character varying(10)",
        "amount": "numeric(12,2)",
        "ratio": "double precision",
        "name": "character varying(256)",
        "created": "timestamp with time zone",
        "loaded": "timestamp without time zone",
        "day": "date",
        "flag": "boolean",
        "payload": "varbyte(16)",
        "at": "time without time zone",
    },
    "databricks": {
        "id": "bigint",
        "code": "string",
        "amount": "decimal(12,2)",
        "ratio": "double",
        "name": "string",
        "created": "timestamp",
        "loaded": "timestamp_ntz",
        "day": "date",
        "flag": "boolean",
        "payload": "binary",
    },
}


def _schema(name: str) -> DatasetSchema:
    columns = tuple(
        ColumnSchema(name=col, native_type=native, type=parse_native_type(native, name), ordinal=i)
        for i, (col, native) in enumerate(_NATIVE[name].items(), start=1)
    )
    assert all(c.type.logical is not LogicalType.UNKNOWN for c in columns)
    return DatasetSchema(
        ref=DatasetRef(connection="c", kind=DatasetKind.TABLE, name="t"), columns=columns
    )


_TABLE = TableConfig.model_validate(
    {
        "name": "orders",
        "source": {"table": "orders"},
        "keys": ["id", "code"],
        "columns": {
            "mappings": [
                {
                    "source": "name",
                    "target": "name",
                    "normalize": ["trim", "lower", "collapse_whitespace"],
                },
                {"source": "amount", "target": "amount", "normalize": [{"round": 1}]},
                {
                    "source": "created",
                    "target": "created",
                    "normalize": [{"truncate_timestamp": "s"}],
                },
                {
                    "source": "loaded",
                    "target": "loaded",
                    "normalize": [{"to_utc": "America/New_York"}],
                    "tolerance": {"timestamp": "2s"},
                },
            ]
        },
    }
)

_NORMALIZATIONS = {
    "default": NormalizationConfig(),
    "strict": NormalizationConfig.model_validate(
        {
            "trim": "both",
            "case_sensitive": False,
            "collapse_whitespace": True,
            "null_equals_empty": True,
            "timezone": "Europe/Berlin",
            "timestamp_precision": "ms",
            "decimal_scale": 4,
        }
    ),
}


def _plan(source: str, target: str, normalization: str) -> ComparisonPlan:
    pairing = pair_columns(_TABLE, _schema(source), _schema(target))
    return build_plan(
        _TABLE,
        pairing,
        normalization=_NORMALIZATIONS[normalization],
        source_dialect=get_dialect(source),
        target_dialect=get_dialect(target),
        factory=_factory,
    )


@pytest.mark.parametrize("normalization", sorted(_NORMALIZATIONS))
def test_projection_and_diff_queries_parse(name: str, dialect: Dialect, normalization: str) -> None:
    plan = _plan(name, name, normalization)
    assert any(not c.pair_spec.hashable for c in plan.columns)  # ratio / loaded use tolerances
    relation = dialect.subquery("SELECT * FROM sales.orders WHERE region = 'EU'", "dvf_filtered")
    for side, mode in itertools.product(("source", "target"), ("keys", "hash", "full")):
        parses(projection_sql(plan, side, relation, mode), name)  # type: ignore[arg-type]
    keys = projection_sql(plan, "source", relation, "keys")
    parses(duplicate_probe_sql(key_names(plan), dialect.subquery(keys, "dvf_k"), dialect), name)

    hashed = {
        side: dialect.subquery(projection_sql(plan, side, relation, "hash"), f"dvf_{side[0]}")  # type: ignore[arg-type]
        for side in ("source", "target")
    }
    for dedupe in (True, False):
        diff = SqlDiff(plan, dialect, "hash", hashed["source"], hashed["target"], dedupe=dedupe)
        parses(diff.stats_sql(), name)
        parses(diff.keys_sql(5), name)
        parses(f"{diff.ctes()}{diff.mismatched_keys_select()}", name)

    # Phase B: full projections of the mismatched keys, sharing one statement with phase A.
    phase_a = SqlDiff(plan, dialect, "hash", hashed["source"], hashed["target"])
    columns = [f"dvf_mk{i}" for i in range(len(plan.keys))]
    preamble = [*phase_a.cte_items(), f"dvf_mk AS ({phase_a.mismatched_keys_select()})"]
    full = {}
    for side in ("source", "target"):
        where = key_match_predicate(plan, side, "dvf_mk", columns)  # type: ignore[arg-type]
        full[side] = dialect.subquery(
            projection_sql(plan, side, relation, "full", where=where),  # type: ignore[arg-type]
            f"dvf_f{side[0]}",
        )
    phase_b = SqlDiff(
        plan, dialect, "full", full["source"], full["target"], prefix="b_", preamble=preamble
    )
    parses(phase_b.stats_sql(), name)
    parses(phase_b.mismatch_sql(list(range(len(plan.columns))), 10), name)
    parses(phase_b.keys_sql(3), name)

    listed = key_list_predicate(plan, "source", [[1, "a'b"], [2, "c\\d"]])
    parses(projection_sql(plan, "source", relation, "full", where=listed), name)


@pytest.mark.parametrize(
    ("source", "target"),
    [("snowflake", "bigquery"), ("redshift", "databricks"), ("databricks", "snowflake")],
)
def test_cross_dialect_projections_parse(source: str, target: str) -> None:
    plan = _plan(source, target, "strict")
    for mode in ("hash", "full"):
        parses(projection_sql(plan, "source", "src_table", mode), source)  # type: ignore[arg-type]
        parses(projection_sql(plan, "target", "tgt_table", mode), target)  # type: ignore[arg-type]


def test_aggregate_and_profile_queries_parse(name: str, dialect: Dialect) -> None:
    plan = _plan(name, name, "strict")
    items = ["COUNT(*) AS dvf_rows"]
    for i, col in enumerate(plan.keys + plan.columns):
        expr = col.source
        items += [
            f"COUNT({expr.canonical}) AS a{i}_count",
            f"COUNT(DISTINCT {expr.canonical}) AS a{i}_count_distinct",
            f"SUM(CASE WHEN {expr.canonical} IS NULL THEN 1 ELSE 0 END) AS a{i}_null_count",
            f"MIN({expr.normalized}) AS a{i}_min",
            f"MAX({dialect.length(expr.canonical)}) AS a{i}_max_length",
        ]
        if expr.type.logical in (LogicalType.DECIMAL, LogicalType.INTEGER, LogicalType.FLOAT):
            items.append(f"AVG({dialect.to_double(expr.normalized)}) AS a{i}_avg")
    group = plan.keys[1].source.canonical
    sql = f"SELECT {group} AS g0, {', '.join(items)}\nFROM orders\nGROUP BY {group}"
    parses(dialect.limit(sql, 10_001), name)


class _CheckContext:
    """The parts of :class:`~dvf.checks.context.CheckContext` row checks use for predicates."""

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    def col(self, name: str) -> str:
        return self.dialect.quote(name)

    def text(self, expr: str) -> str:
        return self.dialect.to_text(expr)

    def literal(self, value: Any) -> str:
        return self.dialect.literal(value)

    def regex_match(self, expr: str, pattern: str, *, full: bool = True) -> str:
        return self.dialect.regex_match(expr, pattern, full=full)


_CHECKS = [
    (NotNull, cfg.NotNullCheck, {"type": "not_null", "columns": ["id", "code"]}),
    (NotBlank, cfg.NotBlankCheck, {"type": "not_blank", "columns": ["name"]}),
    (
        Regex,
        cfg.RegexCheck,
        {"type": "regex", "column": "email", "pattern": r"^[^@\s]+@[^@\s]+\.\w+$"},
    ),
    (
        Regex,
        cfg.RegexCheck,
        {"type": "regex", "column": "code", "pattern": r"\d{3}|N/A", "full_match": False},
    ),
    (
        AllowedValues,
        cfg.AllowedValuesCheck,
        {
            "type": "allowed_values",
            "column": "status",
            "values": ["Open", "it's"],
            "case_sensitive": False,
        },
    ),
    (
        AllowedValues,
        cfg.AllowedValuesCheck,
        {"type": "allowed_values", "column": "flag", "values": [True, False]},
    ),
    (Range, cfg.RangeCheck, {"type": "range", "column": "amount", "min": 0, "max": 1e6}),
    (Length, cfg.LengthCheck, {"type": "length", "column": "code", "min": 2, "max": 10}),
]


@pytest.mark.parametrize("index", range(len(_CHECKS)))
def test_check_predicates_parse(name: str, dialect: Dialect, index: int) -> None:
    check_cls, config_cls, raw = _CHECKS[index]
    check = check_cls(config_cls.model_validate(raw))
    predicate = check.predicate(_CheckContext(dialect))  # type: ignore[arg-type]
    valid = f"({predicate})"
    parses(
        "SELECT COUNT(*) AS total_count, "
        f"COALESCE(SUM(CASE WHEN {valid} THEN 0 ELSE 1 END), 0) AS unexpected_count\n"
        "FROM orders\nWHERE (region = 'EU')",
        name,
    )
    parses(
        dialect.limit(f"SELECT * FROM orders WHERE (CASE WHEN {valid} THEN 0 ELSE 1 END) = 1", 5),
        name,
    )


# ====================================================================== golden strings


GOLDEN: dict[str, dict[str, str]] = {
    "md5": {
        "snowflake": "MD5(x)",
        "bigquery": "TO_HEX(MD5(x))",
        "redshift": "MD5(x)",
        "databricks": "MD5(x)",
    },
    "hash_int": {
        "snowflake": "TO_NUMBER(SUBSTR(h, 9, 8), 'XXXXXXXX')",
        "bigquery": "CAST(CONCAT('0x', SUBSTR(h, 9, 8)) AS INT64)",
        "redshift": "STRTOL(SUBSTRING(h, 9, 8), 16)",
        "databricks": "CAST(CONV(SUBSTR(h, 9, 8), 16, 10) AS BIGINT)",
    },
    "int_div": {
        "snowflake": "CAST(((n) - MOD((n), 4294967296)) / 4294967296 AS BIGINT)",
        "bigquery": "DIV(n, 4294967296)",
        "redshift": "(CAST(n AS BIGINT) / 4294967296)",
        "databricks": "((n) DIV 4294967296)",
    },
    "timestamp_ms_utc": {
        "snowflake": "LEFT(REPLACE(TO_VARCHAR(CAST(x AS TIMESTAMP_NTZ), 'YYYY-MM-DD HH24:MI:SS.FF9'), ' ', 'T'), 23)",
        "bigquery": "LEFT(FORMAT_DATETIME('%E4Y-%m-%dT%H:%M:%E6S', CAST(x AS DATETIME)), 23)",
        "redshift": "LEFT(REPLACE(TO_CHAR(CAST(x AS TIMESTAMP), 'YYYY-MM-DD HH24:MI:SS.US'), ' ', 'T'), 23)",
        "databricks": "LEFT(REPLACE(DATE_FORMAT(CAST(x AS TIMESTAMP_NTZ), 'yyyy-MM-dd HH:mm:ss.SSSSSS'), ' ', 'T'), 23)",
    },
    "timestamp_s_aware": {
        "snowflake": "LEFT(REPLACE(TO_VARCHAR(CAST(CONVERT_TIMEZONE('UTC', x) AS TIMESTAMP_NTZ), 'YYYY-MM-DD HH24:MI:SS.FF9'), ' ', 'T'), 19)",
        "bigquery": "LEFT(FORMAT_TIMESTAMP('%E4Y-%m-%dT%H:%M:%E6S', x, 'UTC'), 19)",
        "redshift": "LEFT(REPLACE(TO_CHAR((x AT TIME ZONE 'UTC'), 'YYYY-MM-DD HH24:MI:SS.US'), ' ', 'T'), 19)",
        "databricks": "LEFT(REPLACE(DATE_FORMAT(CAST(x AS TIMESTAMP_NTZ), 'yyyy-MM-dd HH:mm:ss.SSSSSS'), ' ', 'T'), 19)",
    },
    "timestamp_us_local": {
        "snowflake": "LEFT(REPLACE(TO_VARCHAR(CONVERT_TIMEZONE('Europe/Berlin', 'UTC', CAST(x AS TIMESTAMP_NTZ)), 'YYYY-MM-DD HH24:MI:SS.FF9'), ' ', 'T'), 26)",
        "bigquery": "LEFT(FORMAT_TIMESTAMP('%E4Y-%m-%dT%H:%M:%E6S', TIMESTAMP(CAST(x AS DATETIME), 'Europe/Berlin'), 'UTC'), 26)",
        "redshift": "LEFT(REPLACE(TO_CHAR(CONVERT_TIMEZONE('Europe/Berlin', 'UTC', CAST(x AS TIMESTAMP)), 'YYYY-MM-DD HH24:MI:SS.US'), ' ', 'T'), 26)",
        "databricks": "LEFT(REPLACE(DATE_FORMAT(CONVERT_TIMEZONE('Europe/Berlin', 'UTC', CAST(x AS TIMESTAMP_NTZ)), 'yyyy-MM-dd HH:mm:ss.SSSSSS'), ' ', 'T'), 26)",
    },
    "decimal_2": {
        "snowflake": "TO_VARCHAR(CAST(ROUND(x, 2) AS NUMBER(38, 2)))",
        "bigquery": (
            "RPAD(IF(STRPOS(CAST(ROUND(CAST(x AS BIGNUMERIC), 2) AS STRING), '.') = 0, "
            "CONCAT(CAST(ROUND(CAST(x AS BIGNUMERIC), 2) AS STRING), '.'), "
            "CAST(ROUND(CAST(x AS BIGNUMERIC), 2) AS STRING)), "
            "STRPOS(CONCAT(CAST(ROUND(CAST(x AS BIGNUMERIC), 2) AS STRING), '.'), '.') + 2, '0')"
        ),
        "redshift": "CAST(CAST(ROUND(x, 2) AS DECIMAL(38, 2)) AS VARCHAR(MAX))",
        "databricks": "CAST(CAST(ROUND(x, 2) AS DECIMAL(38, 2)) AS STRING)",
    },
    "trim": {
        "snowflake": "TRIM(x, ' ')",
        "bigquery": "TRIM(x, ' ')",
        "redshift": "BTRIM(x, ' ')",
        "databricks": "TRIM(BOTH ' ' FROM x)",
    },
    "rtrim": {
        "snowflake": "RTRIM(x, ' ')",
        "bigquery": "RTRIM(x, ' ')",
        "redshift": "RTRIM(x, ' ')",
        "databricks": "TRIM(TRAILING ' ' FROM x)",
    },
    "length": {
        "snowflake": "LENGTH(x)",
        "bigquery": "LENGTH(x)",
        "redshift": "LENGTH(x)",
        "databricks": "LENGTH(x)",
    },
    "regex_full": {
        "snowflake": "REGEXP_LIKE(x, '(\\\\d+|a)')",
        "bigquery": "REGEXP_CONTAINS(x, '^(?:\\\\d+|a)$')",
        "redshift": "(x ~ '^(\\\\d+|a)$')",
        "databricks": "(x RLIKE '^(?:\\\\d+|a)\\\\z')",
    },
    "regex_partial": {
        "snowflake": "(REGEXP_INSTR(x, '\\\\d+') > 0)",
        "bigquery": "REGEXP_CONTAINS(x, '\\\\d+')",
        "redshift": "(x ~ '\\\\d+')",
        "databricks": "(x RLIKE '\\\\d+')",
    },
    "literal": {
        "snowflake": "'it''s \\\\ \t'",
        "bigquery": "'it\\'s \\\\ \\t'",
        "redshift": "'it''s \\\\ \t'",
        "databricks": "'it\\'s \\\\ \\t'",
    },
    "quote": {
        "snowflake": '"a""b"',
        "bigquery": "`a\\`b`",
        "redshift": '"a""b"',
        "databricks": "`a``b`",
    },
    "to_text": {
        "snowflake": "CAST(x AS VARCHAR)",
        "bigquery": "CAST(x AS STRING)",
        "redshift": "CAST(x AS VARCHAR(MAX))",
        "databricks": "CAST(x AS STRING)",
    },
    "is_distinct": {
        "snowflake": "(x IS DISTINCT FROM y)",
        "bigquery": "(x IS DISTINCT FROM y)",
        "redshift": "(CASE WHEN x = y OR (x IS NULL AND y IS NULL) THEN FALSE ELSE TRUE END)",
        "databricks": "(x IS DISTINCT FROM y)",
    },
    "binary_hex": {
        "snowflake": "HEX_ENCODE(x, 0)",
        "bigquery": "TO_HEX(x)",
        "redshift": "LOWER(TO_HEX(x))",
        "databricks": "LOWER(HEX(x))",
    },
    "date": {
        "snowflake": "TO_VARCHAR(CAST(x AS DATE), 'YYYY-MM-DD')",
        "bigquery": "FORMAT_DATE('%E4Y-%m-%d', CAST(x AS DATE))",
        "redshift": "TO_CHAR(CAST(x AS DATE), 'YYYY-MM-DD')",
        "databricks": "DATE_FORMAT(CAST(x AS DATE), 'yyyy-MM-dd')",
    },
}


def _golden_sql(primitive: str, d: Dialect) -> str:
    builders = {
        "md5": lambda: d.md5("x"),
        "hash_int": lambda: d.hash_int("h", 8),
        "int_div": lambda: d.int_div("n", HASH_SPACE),
        "timestamp_ms_utc": lambda: d.timestamp_text(
            "x", precision="ms", tz_aware=False, timezone="UTC", from_date=False
        ),
        "timestamp_s_aware": lambda: d.timestamp_text(
            "x", precision="s", tz_aware=True, timezone="UTC", from_date=False
        ),
        "timestamp_us_local": lambda: d.timestamp_text(
            "x", precision="us", tz_aware=False, timezone="Europe/Berlin", from_date=False
        ),
        "decimal_2": lambda: d.decimal_text("x", 2),
        "trim": lambda: d.trim("x"),
        "rtrim": lambda: d.trim("x", "rtrim"),
        "length": lambda: d.length("x"),
        "regex_full": lambda: d.regex_match("x", r"\d+|a", full=True),
        "regex_partial": lambda: d.regex_match("x", r"\d+", full=False),
        "literal": lambda: d.literal("it's \\ \t"),
        "quote": lambda: d.quote("a`b" if d.name in ("bigquery", "databricks") else 'a"b'),
        "to_text": lambda: d.to_text("x"),
        "is_distinct": lambda: d.is_distinct("x", "y"),
        "binary_hex": lambda: d.binary_hex("x"),
        "date": lambda: d.date_text("x"),
    }
    return builders[primitive]()


@pytest.mark.parametrize("primitive", sorted(GOLDEN))
def test_golden_primitives(name: str, dialect: Dialect, primitive: str) -> None:
    assert _golden_sql(primitive, dialect) == GOLDEN[primitive][name]


def test_bigquery_literals_never_contain_raw_line_breaks() -> None:
    d = get_dialect("bigquery")
    text = d.literal("a\nb\rc\x01d")
    assert "\n" not in text and "\r" not in text and "\x01" not in text
    assert text == "'a\\nb\\rc\\x01d'"
    parses_expr(text, "bigquery")


def test_literals_keep_base_behavior_for_non_strings(name: str, dialect: Dialect) -> None:
    from datetime import date, datetime
    from decimal import Decimal

    assert dialect.literal(None) == "NULL"
    assert dialect.literal(False) == "FALSE"
    assert dialect.literal(42) == "42"
    assert dialect.literal(Decimal("1.50")) == "1.50"
    assert dialect.literal(date(2024, 1, 2)) == "'2024-01-02'"
    assert dialect.literal(datetime(2024, 1, 2, 3, 4)) == "'2024-01-02T03:04:00'"


def test_adjacent_literal_traps() -> None:
    """``''`` would be two adjacent literals in Databricks and invalid in BigQuery."""
    for name in ("bigquery", "databricks"):
        literal = get_dialect(name).literal("it's")
        assert "''" not in literal
        parsed = sqlglot.parse_one(f"SELECT {literal}", read=name)
        assert len(list(parsed.find_all(sqlglot.exp.Literal))) == 1


# ====================================================================== value-level checks (DuckDB)
#
# DuckDB implements several of the same functions (CONCAT, MD5, LENGTH, REPLACE, LOWER, RPAD,
# STRPOS, MOD, hexadecimal casts), so the composed expressions can be run locally.

_ROWS: list[tuple[str | None, ...]] = [
    ("a", "b", None),
    ("ab", "", "c"),
    ("é", "ΑΣ", "İstanbul"),
    (None, None, None),
    ("x|y", "1:2", "~"),
]


def _duckdb_rows(sql_expr: str, rows: list[tuple[Any, ...]], names: list[str]) -> list[Any]:
    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE t ({', '.join(f'{n} VARCHAR' for n in names)})")
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in names)})", rows)
        return [r[0] for r in con.execute(f"SELECT {sql_expr} FROM t ORDER BY rowid").fetchall()]
    finally:
        con.close()


def test_row_hash_sql_matches_the_reference_when_run(name: str, dialect: Dialect) -> None:
    sql = row_hash_sql(["c0", "c1", "c2"], dialect)
    # Spell the few type names and functions DuckDB names differently.
    sql = sql.replace("VARCHAR(MAX)", "VARCHAR").replace("TO_HEX(MD5(", "(MD5(")
    got = _duckdb_rows(sql, _ROWS, ["c0", "c1", "c2"])
    assert got == [row_hash(list(r)) for r in _ROWS]


def test_case_premapping_matches_the_reference(name: str, dialect: Dialect) -> None:
    samples = ["ΑΣ", "ΟΔΟΣ ΣΑΣ", "İstanbul", "ß", "Straße", "Ab"]
    lowered = _duckdb_rows(dialect.lower("c0"), [(s,) for s in samples], ["c0"])
    uppered = _duckdb_rows(dialect.upper("c0"), [(s,) for s in samples], ["c0"])
    assert lowered == [simple_lower(s) for s in samples]
    assert uppered == [simple_upper(s) for s in samples]
    # Engines with full (context-sensitive, multi-character) case mappings, such as Java and
    # ICU, agree with the reference once the special characters are pre-mapped.
    for sample in samples:
        premapped = sample.replace(CAPITAL_SIGMA, SMALL_SIGMA).replace(DOTTED_CAPITAL_I, "i")
        assert premapped.lower() == simple_lower(sample)
        assert sample.replace(SHARP_S, CAPITAL_SHARP_S).upper() == simple_upper(sample)


def test_bigquery_decimal_padding_logic() -> None:
    d = get_dialect("bigquery")
    # What CAST(<BIGNUMERIC rounded to the scale> AS STRING) can return: no trailing zeros and no
    # exponent, with at most ``scale`` fractional digits.
    cases = {
        1: {"1.5": "1.5", "12": "12.0", "0": "0.0", "-7": "-7.0", "-0.5": "-0.5"},
        2: {"1.5": "1.50", "12": "12.00", "-1.25": "-1.25", "0": "0.00", "100.1": "100.10"},
        4: {
            "1.5": "1.5000",
            "-1.2345": "-1.2345",
            "123456789012345678901": "123456789012345678901.0000",
        },
    }
    for scale, expected in cases.items():
        inner = f"CAST(ROUND(CAST(x AS BIGNUMERIC), {scale}) AS STRING)"
        sql = d.decimal_text("x", scale).replace(inner, "r")
        # DuckDB's RPAD wants an INTEGER length where BigQuery takes INT64.
        length = f"STRPOS(CONCAT(r, '.'), '.') + {scale}"
        sql = sql.replace(length, f"CAST({length} AS INTEGER)")
        got = _duckdb_rows(sql, [(m,) for m in expected], ["r"])
        assert got == list(expected.values())


def test_snowflake_integer_division_is_exact() -> None:
    d = get_dialect("snowflake")
    space = HASH_SPACE
    values = [0, 1, space - 1, space, space + 1, 255 * space - 1, 255 * space, (2**32 - 1) * 2**20]
    sql = d.int_div("n", space)
    con = duckdb.connect()
    try:
        got = [
            con.execute(f"SELECT {sql} FROM (SELECT CAST(? AS HUGEINT) AS n)", [v]).fetchone()[0]
            for v in values
        ]  # type: ignore[index]
    finally:
        con.close()
    assert got == [v // space for v in values]


def test_bigquery_hash_int_offsets() -> None:
    d = get_dialect("bigquery")
    digest = "0123456789abcdeffedcba9876543210"
    con = duckdb.connect()
    try:
        for offset in (0, 8, 16, 24):
            sql = d.hash_int(d.literal(digest), offset).replace("AS INT64", "AS BIGINT")
            value = con.execute(f"SELECT {sql}").fetchone()[0]  # type: ignore[index]
            assert value == int(digest[offset : offset + 8], 16)
    finally:
        con.close()


def test_bucket_formula_matches_python(name: str, dialect: Dialect) -> None:
    # bucket() is int_div((x) * g, 2**32); check the arithmetic for the extreme hash values.
    for x in (0, 1, 2**31, 2**32 - 1):
        for granularity in (256, 4096, 2**31):
            assert (x * granularity) // HASH_SPACE < granularity
    assert dialect.bucket("x", 256) == dialect.int_div("(x) * 256", HASH_SPACE)


def test_timestamp_text_width_truncates_like_the_reference() -> None:
    from dvf.normalize.canonical import canonical_text

    full = "2024-03-10T02:30:59.123456"  # what each dialect formats before cutting
    for precision, width in {"s": 19, "ms": 23, "us": 26, "ns": 26}.items():
        spec = CanonSpec(Kind.TIMESTAMP, LogicalType.TIMESTAMP, precision=precision)
        assert full[:width] == canonical_text("2024-03-10 02:30:59.123456", spec)
        sql = get_dialect("redshift").timestamp_text(
            "x", precision=precision, tz_aware=False, timezone="UTC", from_date=False
        )
        assert sql.endswith(f", {width})")


def test_plan_notes_when_unicode_normalization_is_unavailable() -> None:
    plan = _plan("snowflake", "bigquery", "default")
    assert any("snowflake" in note and "source" in note for note in plan.notes)
    assert not any("bigquery" in note for note in plan.notes)
