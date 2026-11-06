from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from dvf.config import checks as chk
from dvf.config import connections as conn
from dvf.config.base import NormalizerRef, parse_duration
from dvf.config.models import (
    ColumnMapping,
    ColumnsConfig,
    DatasetSpec,
    NormalizationConfig,
    SampleConfig,
    TableConfig,
    WaiverConfig,
)
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.types import CanonicalType


def test_parse_duration() -> None:
    assert parse_duration("500ms") == timedelta(milliseconds=500)
    assert parse_duration("1.5h") == timedelta(minutes=90)
    assert parse_duration(" 7d ") == timedelta(days=7)
    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("7 weeks")


@pytest.mark.parametrize(
    ("raw", "name", "params", "text"),
    [
        ("rtrim", "rtrim", {}, "rtrim"),
        ({"round": {"digits": 2}}, "round", {"digits": 2}, "round(digits=2)"),
        ({"round": 2}, "round", {"value": 2}, "round(value=2)"),
        ({"sap_alpha": None}, "sap_alpha", {}, "sap_alpha"),
        ({"name": "lower", "params": {}}, "lower", {}, "lower"),
    ],
)
def test_normalizer_shorthand(raw: Any, name: str, params: dict[str, Any], text: str) -> None:
    mapping = ColumnMapping.model_validate({"source": "a", "target": "b", "normalize": [raw]})
    ref = mapping.normalize[0]
    assert isinstance(ref, NormalizerRef)
    assert (ref.name, ref.params, str(ref)) == (name, params, text)


def test_table_shapes() -> None:
    base = {"source": {"table": "a"}}
    assert TableConfig.model_validate({"name": "t", **base}).label == "t"
    pattern = TableConfig.model_validate({"pattern": {"source": "S.*"}, "keys": "auto"})
    assert pattern.label == "S.*" and pattern.discover_keys
    bad_cases = [
        ({"source": {"table": "a"}}, "set one of 'name', 'pattern'"),
        ({"name": "t", "pattern": {"source": "x"}, **base}, "set only one of"),
        ({"name": "t"}, "set 'source'"),
        ({"name": "t", **base, "target": {"filter": "x"}}, "'target' needs one of"),
        ({"pattern": {"source": "x"}, "source": {"table": "a"}}, "pattern tables take"),
        ({"name": "t", **base, "keys": ["id"], "discover_keys": True}, "not both"),
        ({"name": "bad name", **base}, "String should match pattern"),
        ({"name": "t", "source": {"table": "a", "query": "q"}}, "set only one of"),
    ]
    for raw, message in bad_cases:
        with pytest.raises(ValidationError, match=message):
            TableConfig.model_validate(raw)


def test_columns_config() -> None:
    assert ColumnsConfig.model_validate({"include": "*"}).include == ["*"]
    with pytest.raises(ValidationError, match="mapped more than once"):
        ColumnsConfig.model_validate(
            {"mappings": [{"source": "a", "target": "X"}, {"source": "b", "target": "x"}]}
        )
    with pytest.raises(ValidationError, match="set one of 'source', 'source_expr'"):
        ColumnMapping.model_validate({"target": "x"})


def test_dataset_spec_and_sample() -> None:
    assert DatasetSpec.model_validate({"filter": "x"}).has_locator is False
    with pytest.raises(ValidationError):
        SampleConfig.model_validate({})
    with pytest.raises(ValidationError):
        SampleConfig.model_validate({"fraction": 0.1, "rows": 10})
    assert SampleConfig.model_validate({"rows": 10}).seed == 42


def test_normalization_timezone() -> None:
    assert NormalizationConfig(timezone="utc").timezone == "UTC"
    assert NormalizationConfig(timezone="Asia/Kolkata").timezone == "Asia/Kolkata"
    with pytest.raises(ValidationError, match="unknown time zone"):
        NormalizationConfig(timezone="Nowhere/City")


def test_waiver_expiry() -> None:
    waiver = WaiverConfig(check="a.b.c", reason="r", expires=date(2026, 1, 1))
    assert waiver.is_expired(date(2026, 1, 2))
    assert not waiver.is_expired(date(2026, 1, 1))
    assert not WaiverConfig(check="a", reason="r").is_expired()


@pytest.mark.parametrize(
    ("model", "raw", "message"),
    [
        (
            chk.RegexCheck,
            {"type": "regex", "column": "c", "pattern": "(["},
            "invalid regular expression",
        ),
        (chk.RangeCheck, {"type": "range", "column": "c"}, "set 'min', 'max', or both"),
        (chk.LengthCheck, {"type": "length", "column": "c", "min": 5, "max": 2}, "must not exceed"),
        (chk.LengthCheck, {"type": "length", "column": "c"}, "set 'min', 'max', or both"),
        (chk.RowCountRangeCheck, {"type": "row_count_range"}, "set 'min', 'max', or both"),
        (
            chk.RowCountRangeCheck,
            {"type": "row_count_range", "min": 3, "max": 1},
            "must not exceed",
        ),
        (
            chk.CustomSqlCheck,
            {"type": "custom_sql", "name": "n", "sql": "x", "returns": "scalar"},
            "'expect' is required",
        ),
        (
            chk.CustomSqlCheck,
            {
                "type": "custom_sql",
                "name": "n",
                "sql": "x",
                "expect": {"operator": "==", "value": 0},
            },
            "'expect' only applies",
        ),
        (
            chk.ReferentialIntegrityCheck,
            {"type": "ri", "columns": ["a", "b"], "parent": {"table": "p", "column": "a"}},
            "same length",
        ),
        (
            chk.NullRatioCheck,
            {"type": "null_ratio", "column": "c", "max_ratio": 2},
            "less than or equal",
        ),
        (chk.NotNullCheck, {"type": "not_null", "columns": ["a"], "mostly": 0}, "greater than 0"),
        (
            chk.FreshnessCheck,
            {"type": "freshness", "column": "c", "max_age": "soon"},
            "String should match",
        ),
    ],
)
def test_check_validation(model: type[chk.CheckConfig], raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        model.model_validate(raw)


def test_check_target_columns() -> None:
    assert chk.NotNullCheck(type="not_null", columns=["a", "b"]).target_columns() == ["a", "b"]
    assert chk.RegexCheck(type="regex", column="c", pattern="x").target_columns() == ["c"]
    assert chk.RowCountRangeCheck(type="row_count_range", min=1).target_columns() == []
    ri = chk.ReferentialIntegrityCheck.model_validate(
        {"type": "ri", "column": "a", "parent": {"table": "p", "columns": ["a"]}}
    )
    assert ri.columns == ["a"]


@pytest.mark.parametrize(
    ("model", "raw", "message"),
    [
        (conn.DatabaseConnection, {"type": "postgres"}, "set either 'url' or 'host'"),
        (
            conn.DatabaseConnection,
            {"type": "postgres", "host": "h", "port": 70000},
            "less than or equal",
        ),
        (
            conn.SapRfcConnection,
            {"type": "sap_rfc", "client": "100"},
            "set either 'ashost' or 'mshost'",
        ),
        (
            conn.SapRfcConnection,
            {"type": "sap_rfc", "client": "1", "ashost": "h"},
            "String should match",
        ),
        (conn.OdbcConnection, {"type": "odbc"}, "exactly one of"),
        (conn.SnowflakeConnection, {"type": "snowflake"}, "Field required"),
    ],
)
def test_connection_validation(
    model: type[conn.ConnectionConfig], raw: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        model.model_validate(raw)


def test_schema_alias_and_secret() -> None:
    connection = conn.DatabaseConnection.model_validate(
        {"type": "postgres", "host": "h", "schema": "public", "password": "pw"}
    )
    assert connection.schema_ == "public"
    assert "pw" not in repr(connection)
    assert connection.model_dump(by_alias=True)["schema"] == "public"


def test_dataset_models() -> None:
    ref = DatasetRef(connection="c", kind=DatasetKind.TABLE, name="s.t")
    assert ref.display() == "c:s.t"
    with pytest.raises(ValidationError, match="'query' must be set"):
        DatasetRef(connection="c", kind=DatasetKind.QUERY, name="q")
    with pytest.raises(ValidationError):
        DatasetRef(connection="c", kind=DatasetKind.TABLE, name="t", query="select 1")
    schema = DatasetSchema(
        ref=ref,
        columns=(ColumnSchema(name="ID", native_type="int", type=CanonicalType.integer(32)),),
    )
    assert schema.column("id") is not None
    assert schema.column("id", case_sensitive=True) is None
    assert schema.column("ID", case_sensitive=True) is not None
