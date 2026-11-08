"""Unit tests for Phase 1 building blocks."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from dvf.checks.schema import SchemaInputs, run_schema_checks, schema_diff
from dvf.compare.classify import classify_pair, summarize
from dvf.compare.plan import pair_columns
from dvf.config.connections import FileConnection, SQLiteConnection
from dvf.config.models import DatasetSpec, NormalizationConfig, TableConfig, ToleranceConfig
from dvf.connectors.arrow import rows_to_table, to_array
from dvf.connectors.duckdb import FileConnector
from dvf.connectors.sqlite import SQLiteConnector
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.enums import SchemaCheck, Status
from dvf.core.errors import ConnectorError, PluginError
from dvf.core.types import CanonicalType, LogicalType
from dvf.execution.context import RunInfo, new_run_id
from dvf.execution.selectors import Selectors
from dvf.normalize.canonical import (
    Kind,
    NumericCompare,
    canonical_text,
    numeric_value,
    parse_time,
    resolve_pair,
    single_spec,
)
from dvf.normalize.normalizers import Round, Trim, TruncateTimestamp
from dvf.scaffold.seeded import generate, write_csv, write_parquet
from dvf.security.masking import Privacy, to_json_safe
from dvf.sql.dialects import UnsupportedFeatureError, get_dialect


@pytest.mark.parametrize(
    ("source", "target", "label"),
    [
        (None, "", "null_vs_empty"),
        ("x", None, "null_vs_value"),
        (None, "N/A", "sentinel_substitution"),
        ("2024-01-01", "9999-12-31", "sentinel_substitution"),
        ("abc ", "abc", "whitespace_only"),
        ("Abc", "ABC", "case_only"),
        (" Abc", "ABC", "case_and_whitespace"),
        ("00123", "123", "leading_zeros"),
        ("12.50", "12.5", "type_coercion"),
        ("12.345", "12.35", "numeric_rounding"),
        ("12.349", "12.34", "precision_truncation"),
        ("2024-01-01 10:00:00", "2024-01-01 12:00:00", "timezone_shift"),
        ("2024-01-01 10:00:00.123456", "2024-01-01 10:00:00.123", "precision_truncation"),
        ("2024-01-01T10:00:00", "2024-01-01 10:00:00", "date_format"),
        ("Zürich", "ZÃ¼rich", "encoding"),
        ("café", "caf�", "encoding"),
        ("Alexander", "Alex", "string_truncation"),
        ("red", "blue", "value_changed"),
        ("2024-01-01", "2024-03-05", "value_changed"),
    ],
)
def test_classify_pair(source: object, target: object, label: str) -> None:
    assert classify_pair(source, target) == label


def test_summarize_hints() -> None:
    hints = summarize(Counter({"whitespace_only": 98, "case_only": 2}), total=200)
    assert hints[0].startswith("98% of mismatches are whitespace only")
    assert "based on 100 of 200" in hints[0]
    assert len(hints) == 1
    assert summarize(Counter()) == []


def _schema(
    *columns: tuple[str, str, CanonicalType, bool | None], pk: tuple[str, ...] = ()
) -> DatasetSchema:
    ref = DatasetRef(connection="c", kind=DatasetKind.TABLE, name="t")
    cols = tuple(
        ColumnSchema(name=n, native_type=nt, type=t, nullable=nl) for n, nt, t, nl in columns
    )
    return DatasetSchema(ref=ref, columns=cols, primary_key=pk)


def test_schema_checks_detect_every_problem() -> None:
    source = _schema(
        ("id", "INT", CanonicalType.integer(32), False),
        ("name", "VARCHAR(50)", CanonicalType.string(50), True),
        ("amount", "DECIMAL(10,2)", CanonicalType.decimal(10, 2), True),
        ("legacy", "TEXT", CanonicalType.string(), True),
        ("when", "TIMESTAMP", CanonicalType.timestamp(6), True),
        pk=("id",),
    )
    target = _schema(
        ("id", "BIGINT", CanonicalType.integer(64), True),
        ("name", "VARCHAR(20)", CanonicalType.string(20), False),
        ("amount", "INTEGER", CanonicalType.integer(32), True),
        ("when", "DATE", CanonicalType.date(), True),
        ("extra", "TEXT", CanonicalType.string(), True),
        pk=("name",),
    )
    table = TableConfig.model_validate({"name": "t", "source": {"table": "t"}, "keys": ["id"]})
    pairing = pair_columns(table, source, target)
    inputs = SchemaInputs(
        "s", "t", True, True, source, target, pairing, ("id",), ("name",), [("id", "id")]
    )
    outcomes = {o.type: o for o in run_schema_checks(list(SchemaCheck), inputs)}
    assert outcomes["schema.columns_present"].passed is False
    assert outcomes["schema.type_compatibility"].passed is False
    assert set(outcomes["schema.type_compatibility"].columns) == {"amount", "when"}
    assert outcomes["schema.length_precision"].columns == ["name"]
    assert outcomes["schema.nullability"].status is Status.WARNING
    assert outcomes["schema.constraints"].passed is False
    assert outcomes["schema.naming_standards"].status is Status.SKIPPED
    statuses = {row.status for row in schema_diff(pairing)}
    assert statuses == {"matched", "missing_in_target", "extra_in_target"}


def test_constraints_converted_to_data_check() -> None:
    schema = _schema(("id", "INT", CanonicalType.integer(32), None))
    table = TableConfig.model_validate({"name": "t", "source": {"table": "t"}, "keys": ["id"]})
    pairing = pair_columns(table, schema, schema)
    inputs = SchemaInputs(
        "s",
        "t",
        True,
        True,
        schema,
        schema,
        pairing,
        (),
        (),
        [("id", "id")],
        lambda cols: (2, 1, 10),
    )
    (outcome,) = run_schema_checks([SchemaCheck.CONSTRAINTS], inputs)
    assert outcome.passed is False and outcome.details["converted_to_data_check"]
    inputs.probe_target_uniqueness = None
    (outcome,) = run_schema_checks([SchemaCheck.CONSTRAINTS], inputs)
    assert outcome.status is Status.SKIPPED
    missing = SchemaInputs("s", "t", True, False)
    assert {o.status for o in run_schema_checks([SchemaCheck.NULLABILITY], missing)} == {
        Status.SKIPPED
    }


def test_resolve_pair_kinds() -> None:
    norm = NormalizationConfig()
    assert (
        resolve_pair(CanonicalType.integer(), CanonicalType.integer(), norm).source.kind
        is Kind.INTEGER
    )
    pair = resolve_pair(CanonicalType.decimal(10, 2), CanonicalType.decimal(12, 4), norm)
    assert pair.source.kind is Kind.DECIMAL and pair.source.scale == 4
    unknown = resolve_pair(CanonicalType.decimal(), CanonicalType.integer(), norm)
    assert unknown.target.scale == 10
    floats = resolve_pair(
        CanonicalType.float(), CanonicalType.decimal(10, 2), norm, ToleranceConfig(numeric_abs=0.5)
    )
    assert floats.numeric == NumericCompare(abs_tol=0.5, rel_tol=1e-9) and not floats.hashable
    ts = resolve_pair(
        CanonicalType.date(),
        CanonicalType.timestamp(6, tz=True),
        norm,
        ToleranceConfig(timestamp="2s"),
    )
    assert ts.source.kind is Kind.TIMESTAMP and ts.numeric is not None and ts.numeric.epoch
    mixed = resolve_pair(CanonicalType.string(), CanonicalType.integer(), norm)
    assert (mixed.source.kind, mixed.target.kind) == (Kind.TEXT, Kind.INTEGER)
    fixed = resolve_pair(
        CanonicalType.integer(), CanonicalType.integer(), NormalizationConfig(decimal_scale=2)
    )
    assert fixed.source.kind is Kind.DECIMAL
    assert (
        "numeric" in floats.describe()
        and "timestamp" in single_spec(CanonicalType.timestamp(), norm).describe()
    )


def test_reference_conversions() -> None:
    spec = single_spec(CanonicalType.timestamp(), NormalizationConfig(timestamp_precision="s"))
    assert canonical_text("2024-01-01T10:00:00.9Z", spec) == "2024-01-01T10:00:00"
    assert canonical_text(date(2024, 1, 1), spec) == "2024-01-01T00:00:00"
    epoch = NumericCompare(epoch=True)
    assert numeric_value(datetime(1970, 1, 1, 0, 0, 1), spec, epoch) == 1_000_000.0
    assert numeric_value(True, spec, NumericCompare()) == 1.0
    assert parse_time(timedelta(hours=25, minutes=1)) == time(1, 1)
    bool_spec = single_spec(CanonicalType.boolean(), NormalizationConfig())
    assert [canonical_text(v, bool_spec) for v in ("yes", "0", "maybe", 2)] == ["1", "0", None, "1"]
    int_spec = single_spec(CanonicalType.integer(), NormalizationConfig())
    assert canonical_text(Decimal("12.6"), int_spec) == "13"
    assert canonical_text("x", single_spec(CanonicalType.binary(), NormalizationConfig())) == "78"


def test_normalizer_parameters() -> None:
    assert Round({"value": 3}).digits == 3
    assert Round({"digits": 1}).to_python(Decimal("1.25"), CanonicalType.decimal(5, 2)) == Decimal(
        "1.3"
    )
    assert TruncateTimestamp({"unit": "h"}).to_python(
        "2024-01-01 10:59:59", CanonicalType.timestamp()
    ) == datetime(2024, 1, 1, 10)
    with pytest.raises(PluginError, match="takes no parameters"):
        Trim({"x": 1})
    with pytest.raises(PluginError, match="Invalid parameters"):
        Round({"digits": -1})
    assert not Round(None).applies_to(CanonicalType.string())


def test_file_connector_formats(tmp_path: Path) -> None:
    table = pa.table({"id": [1, 2], "name": ["a", "b"]})
    write_parquet(table, tmp_path / "a.parquet")
    write_csv(table, tmp_path / "b.csv")
    (tmp_path / "c.ndjson").write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
    (tmp_path / "d.tsv").write_text("id\tname\n1\tx\n", encoding="utf-8")
    (tmp_path / "e.unknown").write_text("x", encoding="utf-8")
    connector = FileConnector("files", FileConnection(type="files", path="."), tmp_path)
    with connector:
        for name, rows in (("a.parquet", 2), ("b.csv", 2), ("c.ndjson", 2), ("d.tsv", 1)):
            rel = connector.resolve(DatasetSpec(path=name))
            assert connector.exists(rel)
            assert (
                connector.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").column("n")[0].as_py()
                == rows
            )
        assert connector.list_datasets("*.parquet") == ["a.parquet"]
        assert not connector.exists(connector.resolve(DatasetSpec(path="missing.csv")))
        assert connector.exists(connector.resolve(DatasetSpec(path="*.csv")))
        filtered = connector.resolve(DatasetSpec(table="a.parquet", filter="id > 1"))
        assert (
            connector.query(f"SELECT COUNT(*) AS n FROM {filtered.sql}").column("n")[0].as_py() == 1
        )
        with pytest.raises(ConnectorError, match="Cannot infer"):
            connector.resolve(DatasetSpec(path="e.unknown"))
        with pytest.raises(ConnectorError, match="Phase 3"):
            connector.resolve(DatasetSpec(path="x.avro", options={"format": "avro"}))
        with pytest.raises(ConnectorError, match="as_of"):
            connector.resolve(DatasetSpec(path="a.parquet", as_of="2024-01-01"))
        with pytest.raises(ConnectorError, match="Query failed"):
            connector.query("SELECT * FROM nowhere")
        assert connector.test_connection().ok
    assert (
        not FileConnector("f", FileConnection(type="files", path="nope"), tmp_path)
        .test_connection()
        .ok
    )


def test_sqlite_connector(tmp_path: Path) -> None:
    import sqlite3

    con = sqlite3.connect(tmp_path / "x.sqlite")
    con.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR(10) NOT NULL, amount DECIMAL(8,2))"
    )
    con.execute("INSERT INTO t VALUES (1, 'a', 1.5)")
    con.commit()
    con.close()
    connector = SQLiteConnector(
        "lite", SQLiteConnection(type="sqlite", database="x.sqlite"), tmp_path
    )
    with connector:
        rel = connector.resolve(DatasetSpec(table="t"))
        schema = connector.get_schema(rel)
        assert schema.primary_key == ("id",)
        assert schema.column("name").nullable is False  # type: ignore[union-attr]
        assert schema.column("amount").type.logical is LogicalType.DECIMAL  # type: ignore[union-attr]
        query = connector.resolve(DatasetSpec(query="SELECT id, name FROM t"))
        assert [c.name for c in connector.get_schema(query).columns] == ["id", "name"]
        assert connector.list_datasets("t*") == ["t"]
        assert not connector.exists(connector.resolve(DatasetSpec(table="missing")))
        batches = list(connector.iter_batches("SELECT * FROM t", batch_size=1))
        assert sum(b.num_rows for b in batches) == 1
        with pytest.raises(ConnectorError, match="not 'path'"):
            connector.resolve(DatasetSpec(path="x.csv"))
        with pytest.raises(ConnectorError, match="not found"):
            connector.get_schema(connector.resolve(DatasetSpec(table="missing")))


def test_arrow_conversion_falls_back_to_text() -> None:
    import uuid

    assert to_array([1, "a"]).type == pa.string()
    assert to_array([None, None]).type == pa.string()
    assert to_array([timedelta(seconds=1)]).type == pa.duration("us")
    assert to_array([uuid.UUID(int=1), {"a": 1}]).to_pylist()[1] == '{"a": 1}'
    assert rows_to_table(["a", "a"], []).num_columns == 2


def test_dialects_and_masking() -> None:
    duck = get_dialect("duckdb")
    assert duck.literal("it's") == "'it''s'"
    assert duck.literal(None) == "NULL" and duck.literal(True) == "TRUE"
    assert (
        duck.literal(Decimal("1.50")) == "1.50" and duck.literal(date(2024, 1, 2)) == "'2024-01-02'"
    )
    with pytest.raises(UnsupportedFeatureError):
        duck.literal(float("nan"))
    with pytest.raises(UnsupportedFeatureError, match="only NFC"):
        duck.unicode_normalize("x", "NFKC")
    with pytest.raises(UnsupportedFeatureError, match="not available"):
        get_dialect("db2")
    assert get_dialect("sqlite").is_distinct("a", "b") == "(a IS NOT b)"
    privacy = Privacy(pii_columns=("ssn",))
    assert privacy.mask("ssn", "123") == "***"
    assert privacy.mask("email", "jane@example.com") == "j***@example.com"
    assert to_json_safe(b"\x01") == "0x01" and to_json_safe(float("inf")) == "inf"
    assert len(to_json_safe("x" * 600)) == 501


def test_selectors_and_run_info() -> None:
    selectors = Selectors.parse(["suite:fin*", "table:cust*", "tag:nightly", "orders"])
    assert selectors.table_selected("finance", "customers", [])
    assert not selectors.table_selected("hr", "customers", [])
    assert not selectors.check_selected(
        "finance",
        "customers",
        "not_null",
        "finance.customers.not_null",
        ["nightly"],
        __import__("dvf").core.enums.Severity.ERROR,
    )
    assert Selectors.parse(None).empty
    with pytest.raises(Exception, match="Unknown severity"):
        Selectors.parse(["severity:extreme"])
    info = RunInfo("r1", datetime(2026, 1, 2, 3, 4, 5).astimezone(), "uat")
    assert (
        info.render("${run.id}/${run.date}/${run.profile}/${run.other}")
        == "r1/2026-01-02/uat/${run.other}"
    )
    assert len(new_run_id()) == 21


def test_seeded_generator_rejects_tiny_datasets() -> None:
    with pytest.raises(ValueError, match="at least 60"):
        generate(rows=10)
    data = generate(rows=80, seed=3)
    assert data.expected.customers_mismatched_rows == 24
    assert data.target_customers.num_rows == 80 - 3 + 2 + 2
