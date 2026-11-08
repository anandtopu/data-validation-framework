"""Cloud warehouse connectors (Snowflake, BigQuery, Redshift, Databricks) against fake drivers.

No network and no real drivers: each test installs a fake driver module in ``sys.modules``.
Live tests at the end run only when ``DVF_TEST_<PLATFORM>`` is set (see ``_live_config``).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import sqlglot

from dvf.config.connections import (
    BigQueryConnection,
    DatabaseConnection,
    DatabricksConnection,
    SnowflakeConnection,
)
from dvf.config.models import DatasetSpec
from dvf.connectors.bigquery import BigQueryConnector
from dvf.connectors.databricks import DatabricksConnector
from dvf.connectors.redshift import RedshiftConnector
from dvf.connectors.snowflake import SnowflakeConnector, lower_unquoted, snowflake_type
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import LogicalType
from dvf.plugins.base import Connector
from tests.unit.cloud_fakes import (
    DefaultCredentialsError,
    FakeBigQuery,
    FakeDriver,
    FakeError,
    FakeTable,
    Forbidden,
    Result,
    SchemaField,
)

SECRET = "s3cr3t-Pa55"


def _parses(sql: str, dialect: str) -> None:
    sqlglot.parse_one(sql, read=dialect)


# ====================================================================== fixtures


@pytest.fixture
def sf(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    driver = FakeDriver("snowflake.connector", version="5.2.0")
    monkeypatch.setitem(sys.modules, "snowflake.connector", driver.module)
    return driver


@pytest.fixture
def rs(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    driver = FakeDriver("redshift_connector", version="2.2.0")
    monkeypatch.setitem(sys.modules, "redshift_connector", driver.module)
    return driver


@pytest.fixture
def dbx(monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    driver = FakeDriver("databricks.sql", version="4.9.0")
    monkeypatch.setitem(sys.modules, "databricks.sql", driver.module)
    return driver


@pytest.fixture
def bq(monkeypatch: pytest.MonkeyPatch) -> FakeBigQuery:
    fake = FakeBigQuery()
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake.module)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", fake.api_exceptions)
    monkeypatch.setitem(sys.modules, "google.auth.exceptions", fake.auth_exceptions)
    return fake


def _snowflake(tmp_path: Path, **settings: Any) -> SnowflakeConnector:
    config = SnowflakeConnection.model_validate(
        {"type": "snowflake", "account": "xy12345", **settings}
    )
    return SnowflakeConnector("sf", config, tmp_path)


def _bigquery(tmp_path: Path, **settings: Any) -> BigQueryConnector:
    config = BigQueryConnection.model_validate({"type": "bigquery", "project": "proj", **settings})
    return BigQueryConnector("bq", config, tmp_path)


def _redshift(tmp_path: Path, **settings: Any) -> RedshiftConnector:
    base: dict[str, Any] = {"type": "redshift"}
    if "url" not in settings:
        base["host"] = "cluster.abc.eu-west-1.redshift.amazonaws.com"
    config = DatabaseConnection.model_validate({**base, **settings})
    return RedshiftConnector("rs", config, tmp_path)


def _databricks(tmp_path: Path, **settings: Any) -> DatabricksConnector:
    config = DatabricksConnection.model_validate(
        {
            "type": "databricks",
            "server_hostname": "https://adb-123.4.azuredatabricks.net/",
            "http_path": "/sql/1.0/warehouses/abc",
            **settings,
        }
    )
    return DatabricksConnector("dbx", config, tmp_path)


# ====================================================================== dependencies


@pytest.mark.parametrize(
    ("module", "factory", "extra"),
    [
        ("snowflake.connector", _snowflake, "snowflake"),
        ("google.cloud.bigquery", _bigquery, "bigquery"),
        ("redshift_connector", _redshift, "redshift"),
        ("databricks.sql", _databricks, "databricks"),
    ],
)
def test_missing_driver_raises_dependency_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: str, factory: Any, extra: str
) -> None:
    monkeypatch.setitem(sys.modules, module, None)  # makes the import fail
    connector: Connector = factory(tmp_path)
    with pytest.raises(DependencyError) as info:
        connector.open()
    assert info.value.extra == extra
    assert f"dvf[{extra}]" in (info.value.hint or "")


@pytest.mark.parametrize(
    ("factory", "dialect"),
    [
        (_snowflake, "snowflake"),
        (_bigquery, "bigquery"),
        (_redshift, "redshift"),
        (_databricks, "databricks"),
    ],
)
def test_connector_dialect_and_config(tmp_path: Path, factory: Any, dialect: str) -> None:
    connector: Connector = factory(tmp_path)
    assert connector.dialect == dialect
    assert connector.sql_dialect.name == dialect
    assert isinstance(connector.config, type(connector).Config)  # type: ignore[attr-defined]
    forked = connector.fork()
    assert type(forked) is type(connector) and forked.config is connector.config


# ====================================================================== Snowflake


def test_snowflake_password_connect_args_and_session(tmp_path: Path, sf: FakeDriver) -> None:
    connector = _snowflake(
        tmp_path,
        user="DVF",
        password=SECRET,
        warehouse="WH",
        database="DB",
        schema="PUBLIC",
        role="READER",
        query_timeout_s=120,
        options={"session_parameters": {"QUERY_TAG": "nightly", "TIMEZONE": "Europe/Paris"}},
    )
    connector.open()
    kwargs = sf.connection.kwargs
    assert kwargs["account"] == "xy12345" and kwargs["user"] == "DVF"
    assert kwargs["password"] == SECRET
    assert (kwargs["warehouse"], kwargs["database"], kwargs["schema"], kwargs["role"]) == (
        "WH",
        "DB",
        "PUBLIC",
        "READER",
    )
    assert kwargs["session_parameters"] == {
        "QUERY_TAG": "nightly",
        "TIMEZONE": "UTC",  # always UTC, whatever the options say
        "STATEMENT_TIMEOUT_IN_SECONDS": 120,
    }
    assert kwargs["arrow_number_to_decimal"] is True
    assert "authenticator" not in kwargs
    connector.open()  # idempotent
    assert len(sf.connections) == 1
    connector.close()
    assert sf.connection.closed


@pytest.mark.parametrize(
    ("version", "passphrase_arg"),
    [("5.2.0", "private_key_password"), ("3.12.4", "private_key_file_pwd")],
)
def test_snowflake_key_pair(
    tmp_path: Path, sf: FakeDriver, version: str, passphrase_arg: str
) -> None:
    sf.module.__version__ = version  # type: ignore[attr-defined]
    connector = _snowflake(
        tmp_path,
        user="SVC_DVF",
        private_key_path="keys/rsa_key.p8",
        private_key_passphrase=SECRET,
    )
    connector.open()
    kwargs = sf.connection.kwargs
    assert kwargs["private_key_file"] == str(tmp_path / "keys" / "rsa_key.p8")
    assert kwargs[passphrase_arg] == SECRET
    assert kwargs["authenticator"] == "SNOWFLAKE_JWT"
    assert "password" not in kwargs


def test_snowflake_external_browser_and_oauth(tmp_path: Path, sf: FakeDriver) -> None:
    _snowflake(
        tmp_path, user="me@example.com", authenticator="externalbrowser", password=SECRET
    ).open()
    kwargs = sf.connection.kwargs
    assert kwargs["authenticator"] == "externalbrowser"
    assert "password" not in kwargs and "token" not in kwargs

    _snowflake(tmp_path, authenticator="oauth", password=SECRET).open()
    kwargs = sf.connection.kwargs
    assert kwargs["token"] == SECRET and "password" not in kwargs

    _snowflake(tmp_path, authenticator="oauth", password="ignored", options={"token": "tok"}).open()
    assert sf.connection.kwargs["token"] == "tok"


def test_snowflake_connect_error_is_wrapped_without_secrets(tmp_path: Path, sf: FakeDriver) -> None:
    sf.connect_error = FakeError("250001: Incorrect username or password was specified.")
    connector = _snowflake(tmp_path, user="DVF", password=SECRET)
    with pytest.raises(ConnectorError) as info:
        connector.open()
    assert "Authentication failed" in (info.value.hint or "")
    assert SECRET not in str(info.value) and SECRET not in (info.value.hint or "")


@pytest.mark.parametrize(
    ("message", "fragment"),
    [
        ("No active warehouse selected in the current session.", "warehouse"),
        ("Object 'ORDERS' does not exist or not authorized.", "grant USAGE"),
        ("Role 'ADMIN' specified in the connect string is not granted", "role"),
        ("Something else", "dvf connections test"),
    ],
)
def test_snowflake_query_error_hints(
    tmp_path: Path, sf: FakeDriver, message: str, fragment: str
) -> None:
    sf.responder.add("SELECT", FakeError(message))
    with pytest.raises(ConnectorError) as info:
        _snowflake(tmp_path).query("SELECT 1")
    assert fragment in (info.value.hint or "")


def test_snowflake_query_uses_arrow_and_lowercases_unquoted_names(
    tmp_path: Path, sf: FakeDriver
) -> None:
    sf.responder.add("SELECT", Result(["SOURCE_ROWS", "MixedCase", "COUNT(*)"], [(3, "a", 7)]))
    connector = _snowflake(tmp_path)
    table = connector.query("SELECT 3 AS source_rows")
    assert table.column_names == ["source_rows", "MixedCase", "COUNT(*)"]
    assert table.to_pylist() == [{"source_rows": 3, "MixedCase": "a", "COUNT(*)": 7}]
    call = sf.connection.cursors[-1].arrow_calls[-1]
    assert call == {"force_return_table": True, "force_microsecond_precision": True}
    assert sf.connection.cursors[-1].closed


def test_snowflake_query_falls_back_to_rows_when_not_arrow(tmp_path: Path, sf: FakeDriver) -> None:
    sf.responder.add("SHOW", Result(["NAME", "VALUE"], [("x", 1)], arrow=False))
    table = _snowflake(tmp_path).query("SHOW PARAMETERS")
    assert table.to_pylist() == [{"name": "x", "value": 1}]


def test_snowflake_iter_batches_streams_arrow_chunks(tmp_path: Path, sf: FakeDriver) -> None:
    rows = [(i, f"v{i}") for i in range(10)]
    sf.responder.add("SELECT", Result(["K0", "H"], rows))
    batches = list(_snowflake(tmp_path).iter_batches("SELECT ...", batch_size=3))
    assert all(b.num_rows <= 3 for b in batches)
    table = pa.Table.from_batches(batches)
    assert table.column_names == ["k0", "h"] and table.num_rows == 10

    sf.responder.add("SELECT", Result(["K0"], [(1,), (2,)], arrow=False))
    table = pa.Table.from_batches(list(_snowflake(tmp_path).iter_batches("SELECT 2", batch_size=1)))
    assert table.to_pylist() == [{"k0": 1}, {"k0": 2}]


def test_snowflake_lower_unquoted() -> None:
    table = pa.table({"A_1$": [1], "a": [2], "Ab": [3], "1X": [4]})
    assert lower_unquoted(table).column_names == ["a_1$", "a", "Ab", "1X"]


def test_snowflake_resolve_tables_queries_and_time_travel(tmp_path: Path) -> None:
    connector = _snowflake(tmp_path)
    rel = connector.resolve(DatasetSpec(table="DB.SALES.ORDERS", filter="REGION = 'EU'"))
    assert rel.table == "DB.SALES.ORDERS" and rel.base_sql == "DB.SALES.ORDERS"
    assert rel.sql == "(SELECT * FROM DB.SALES.ORDERS WHERE REGION = 'EU') AS dvf_filtered"
    query = connector.resolve(DatasetSpec(query="SELECT 1 AS id"))
    assert query.table is None and query.ref.query == "SELECT 1 AS id"

    travel = connector.resolve(DatasetSpec(table="ORDERS", as_of="2024-05-01 12:00:00+00:00"))
    assert travel.base_sql == "ORDERS AT(TIMESTAMP => TO_TIMESTAMP_TZ('2024-05-01 12:00:00+00:00'))"
    assert travel.table is None  # metadata comes from the time-travel relation itself
    assert "AS OF" in travel.ref.name
    offset = connector.resolve(DatasetSpec(table="ORDERS", as_of="-3600"))
    assert offset.base_sql == "ORDERS AT(OFFSET => -3600)"
    statement = connector.resolve(
        DatasetSpec(table="ORDERS", as_of="01b2c3d4-0000-1111-0000-123456789abc")
    )
    assert statement.base_sql == "ORDERS AT(STATEMENT => '01b2c3d4-0000-1111-0000-123456789abc')"
    for relation in (rel, query, travel, offset, statement):
        _parses(f"SELECT * FROM {relation.sql}", "snowflake")

    with pytest.raises(ConnectorError, match="as_of"):
        connector.resolve(DatasetSpec(query="SELECT 1", as_of="2024-01-01"))
    with pytest.raises(ConnectorError, match="path"):
        connector.resolve(DatasetSpec(path="x.csv"))


_DESCRIBE_COLUMNS = [
    "name",
    "type",
    "kind",
    "null?",
    "default",
    "primary key",
    "unique key",
    "check",
    "expression",
    "comment",
]


def test_snowflake_get_schema_from_describe_and_primary_keys(
    tmp_path: Path, sf: FakeDriver
) -> None:
    sf.responder.add(
        "DESCRIBE TABLE",
        Result(
            _DESCRIBE_COLUMNS,
            [
                ("ID", "NUMBER(38,0)", "COLUMN", "N", None, "Y", "N", None, None, None),
                ("REGION", "VARCHAR(16777216)", "COLUMN", "N", None, "Y", "N", None, None, "ISO"),
                ("AMOUNT", "NUMBER(12,2)", "COLUMN", "Y", "0", "N", "N", None, None, None),
                ("PAYLOAD", "BINARY(8388608)", "COLUMN", "Y", None, "N", "N", None, None, None),
                ("CREATED", "TIMESTAMP_LTZ(9)", "COLUMN", "Y", None, "N", "N", None, None, None),
                ("LOADED", "TIMESTAMP_NTZ(9)", "COLUMN", "Y", None, "N", "N", None, None, None),
                ("V", "VARIANT", "VIRTUAL", "Y", None, "N", "N", None, None, None),
            ],
            arrow=False,
        ),
    )
    sf.responder.add(
        "SHOW PRIMARY KEYS",
        Result(
            [
                "created_on",
                "database_name",
                "schema_name",
                "table_name",
                "column_name",
                "key_sequence",
            ],
            [(None, "DB", "S", "ORDERS", "REGION", 2), (None, "DB", "S", "ORDERS", "ID", 1)],
            arrow=False,
        ),
    )
    connector = _snowflake(tmp_path)
    rel = connector.resolve(DatasetSpec(table="ORDERS"))
    schema = connector.get_schema(rel)
    names = [c.name for c in schema.columns]
    assert names == ["ID", "REGION", "AMOUNT", "PAYLOAD", "CREATED", "LOADED"]  # virtual skipped
    by_name = {c.name: c for c in schema.columns}
    assert by_name["ID"].type.logical is LogicalType.DECIMAL and by_name["ID"].type.scale == 0
    assert by_name["ID"].nullable is False and by_name["AMOUNT"].nullable is True
    assert by_name["AMOUNT"].default == "0" and by_name["REGION"].comment == "ISO"
    assert by_name["PAYLOAD"].type.logical is LogicalType.BINARY
    assert by_name["PAYLOAD"].type.fixed_length is False
    assert by_name["CREATED"].type.logical is LogicalType.TIMESTAMP_TZ
    assert by_name["LOADED"].type.logical is LogicalType.TIMESTAMP
    assert [c.ordinal for c in schema.columns] == [1, 2, 3, 4, 5, 6]
    assert schema.primary_key == ("ID", "REGION")
    assert connector.exists(rel)

    sf.responder.add("DESCRIBE TABLE", FakeError("Table 'NOPE' does not exist or not authorized."))
    sf.responder.add("SHOW PRIMARY KEYS", FakeError("does not exist"))
    missing = connector.resolve(DatasetSpec(table="NOPE"))
    assert not connector.exists(missing)
    assert connector.get_primary_key(missing) == ()
    with pytest.raises(ConnectorError, match="not found"):
        connector.get_schema(missing)


def test_snowflake_get_schema_of_a_query_from_description(tmp_path: Path, sf: FakeDriver) -> None:
    description = [
        ("ID", 0, None, None, 38, 0, False),
        ("NAME", 2, None, 100, None, None, True),
        ("TS", 8, None, None, 0, 9, True),
        ("AMT", 0, None, None, 10, 2, True),
        ("RAW", 11, None, 16, None, None, True),
        ("F", 1, None, None, None, None, True),
    ]
    sf.responder.add("LIMIT 0", Result(description=description))
    connector = _snowflake(tmp_path)
    schema = connector.get_schema(connector.resolve(DatasetSpec(query="SELECT * FROM T")))
    assert [c.native_type for c in schema.columns] == [
        "NUMBER(38,0)",
        "VARCHAR(100)",
        "TIMESTAMP_NTZ(9)",
        "NUMBER(10,2)",
        "BINARY(16)",
        "FLOAT",
    ]
    assert schema.columns[0].nullable is False
    assert schema.columns[3].type.scale == 2
    assert schema.columns[4].type.logical is LogicalType.BINARY
    assert schema.columns[5].type.logical is LogicalType.FLOAT
    assert "LIMIT 0" in sf.connection.statements[-1]


def test_snowflake_type_parsing() -> None:
    assert snowflake_type("BINARY(16)").fixed_length is False
    assert snowflake_type("TIMESTAMP_TZ(3)").logical is LogicalType.TIMESTAMP_TZ
    assert snowflake_type("NUMBER(10,2)").scale == 2


def test_snowflake_list_datasets(tmp_path: Path, sf: FakeDriver) -> None:
    sf.responder.add(
        "information_schema.tables",
        Result(
            ["TABLE_SCHEMA", "TABLE_NAME", "IS_CURRENT"],
            [
                ("SALES", "ORDERS", True),
                ("SALES", "ORDER_LINES", True),
                ("SALES", "MixedCase", True),
                ("STAGING", "ORDERS_RAW", False),
            ],
        ),
    )
    connector = _snowflake(tmp_path, database="DB")
    assert connector.list_datasets("order*") == ["ORDERS", "ORDER_LINES"]
    assert connector.list_datasets("mixed*") == ['"MixedCase"']
    assert connector.list_datasets("staging.*") == ["STAGING.ORDERS_RAW"]


def test_snowflake_server_version(tmp_path: Path, sf: FakeDriver) -> None:
    sf.responder.add("CURRENT_VERSION", Result(["CURRENT_VERSION()"], [("9.12.1",)]))
    assert _snowflake(tmp_path).server_version() == "Snowflake 9.12.1"


# ====================================================================== BigQuery


def test_bigquery_service_account_and_adc(tmp_path: Path, bq: FakeBigQuery) -> None:
    connector = _bigquery(tmp_path, credentials_path="keys/sa.json", location="EU")
    connector.open()
    client = bq.client
    assert client.credentials_file == str(tmp_path / "keys" / "sa.json")
    assert (client.project, client.location) == ("proj", "EU")
    connector.close()
    assert client.closed

    _bigquery(tmp_path).open()
    assert bq.client.credentials_file is None and bq.client.project == "proj"


def test_bigquery_client_error_is_wrapped(
    tmp_path: Path, bq: FakeBigQuery, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(bq.module, "Client", broken)
    with pytest.raises(ConnectorError) as info:
        _bigquery(tmp_path).open()
    assert "application-default login" in (info.value.hint or "")


def test_bigquery_job_config_query_and_batches(tmp_path: Path, bq: FakeBigQuery) -> None:
    bq.query_results["FROM data"] = (pa.table({"k0": ["a", "b", "c"], "h": ["1", "2", "3"]}), [])
    connector = _bigquery(
        tmp_path,
        dataset="sales",
        query_timeout_s=30,
        options={"maximum_bytes_billed": 10**9},
    )
    table = connector.query("SELECT * FROM data")
    assert table.num_rows == 3
    job = bq.client.jobs[-1]
    assert job.job_config.settings == {
        "use_legacy_sql": False,
        "labels": {"tool": "dvf"},
        "default_dataset": "proj.sales",
        "job_timeout_ms": 30_000,
        "maximum_bytes_billed": 10**9,
    }
    assert job.timeout == 30
    assert "create_bqstorage_client" in job.rows.to_arrow_kwargs
    batches = list(connector.iter_batches("SELECT * FROM data", batch_size=2))
    assert [b.num_rows for b in batches] == [2, 1]


def test_bigquery_errors_and_bad_options(tmp_path: Path, bq: FakeBigQuery) -> None:
    bq.query_error = Forbidden("403 Access Denied: Table proj:sales.orders")
    with pytest.raises(ConnectorError) as info:
        _bigquery(tmp_path).query("SELECT 1")
    assert "roles/bigquery.dataViewer" in (info.value.hint or "")
    bq.query_error = None
    with pytest.raises(ConnectorError, match="Invalid BigQuery job option"):
        _bigquery(tmp_path, options={"no_such_option": 1}).query("SELECT 1")


def test_bigquery_resolve_and_time_travel(tmp_path: Path, bq: FakeBigQuery) -> None:
    connector = _bigquery(tmp_path, dataset="sales")
    rel = connector.resolve(DatasetSpec(table="my-proj.sales.orders", filter="region = 'EU'"))
    assert rel.base_sql == "`my-proj.sales.orders`" and rel.table == "my-proj.sales.orders"
    quoted = connector.resolve(DatasetSpec(table="`p`.`d`.`t`"))
    assert quoted.base_sql == "`p`.`d`.`t`"
    travel = connector.resolve(DatasetSpec(table="sales.orders", as_of="2024-05-01T00:00:00Z"))
    assert travel.base_sql == (
        "`sales.orders` FOR SYSTEM_TIME AS OF TIMESTAMP('2024-05-01T00:00:00Z')"
    )
    assert travel.table is None
    query = connector.resolve(DatasetSpec(query="SELECT 1 AS id"))
    for relation in (rel, quoted, travel, query):
        _parses(f"SELECT * FROM {relation.sql}", "bigquery")
    with pytest.raises(ConnectorError, match="as_of"):
        connector.resolve(DatasetSpec(query="SELECT 1", as_of="2024-01-01"))


def test_bigquery_schema_primary_key_and_exists(tmp_path: Path, bq: FakeBigQuery) -> None:
    constraints = type("C", (), {"primary_key": type("P", (), {"columns": ["id"]})()})()
    bq_table = FakeTable(
        schema=[
            SchemaField("id", "INTEGER", mode="REQUIRED"),
            SchemaField("amount", "NUMERIC", precision=10, scale=2, description="Gross"),
            SchemaField("big", "BIGNUMERIC"),
            SchemaField("name", "STRING", max_length=50),
            SchemaField("tags", "STRING", mode="REPEATED"),
            SchemaField("created", "TIMESTAMP"),
            SchemaField("loaded", "DATETIME"),
            SchemaField("flag", "BOOLEAN"),
            SchemaField("ratio", "FLOAT"),
            SchemaField("address", "RECORD", fields=(SchemaField("city", "STRING"),)),
        ],
        table_constraints=constraints,
        num_rows=42,
    )
    bq.tables["proj.sales.orders"] = bq_table
    connector = _bigquery(tmp_path, dataset="sales")
    rel = connector.resolve(DatasetSpec(table="orders"))
    schema = connector.get_schema(rel)
    natives = {c.name: c.native_type for c in schema.columns}
    assert natives == {
        "id": "INT64",
        "amount": "NUMERIC(10, 2)",
        "big": "BIGNUMERIC",
        "name": "STRING(50)",
        "tags": "ARRAY<STRING>",
        "created": "TIMESTAMP",
        "loaded": "DATETIME",
        "flag": "BOOL",
        "ratio": "FLOAT64",
        "address": "STRUCT<city STRING>",
    }
    types_ = {c.name: c.type for c in schema.columns}
    assert types_["id"].logical is LogicalType.INTEGER
    assert (types_["amount"].precision, types_["amount"].scale) == (10, 2)
    assert types_["big"].scale == 38
    assert types_["created"].logical is LogicalType.TIMESTAMP_TZ
    assert types_["loaded"].logical is LogicalType.TIMESTAMP
    assert types_["tags"].logical is LogicalType.ARRAY
    assert types_["address"].logical is LogicalType.STRUCT
    assert schema.columns[0].nullable is False and schema.columns[1].nullable is True
    assert schema.columns[1].comment == "Gross"
    assert schema.primary_key == ("id",) and schema.row_count_estimate == 42
    assert connector.exists(rel)
    assert not connector.exists(connector.resolve(DatasetSpec(table="sales.missing")))
    with pytest.raises(ConnectorError, match="not found"):
        connector.get_schema(connector.resolve(DatasetSpec(table="sales.missing")))
    with pytest.raises(ConnectorError, match="needs a dataset"):
        _bigquery(tmp_path).get_schema(connector.resolve(DatasetSpec(table="orders")))


def test_bigquery_query_schema_uses_a_dry_run(tmp_path: Path, bq: FakeBigQuery) -> None:
    bq.query_results["dvf_query"] = (pa.table({"x": [1]}), [SchemaField("x", "INT64")])
    connector = _bigquery(tmp_path)
    schema = connector.get_schema(connector.resolve(DatasetSpec(query="SELECT 1 AS x")))
    assert [(c.name, c.native_type) for c in schema.columns] == [("x", "INT64")]
    job = bq.client.jobs[-1]
    assert job.job_config.settings["dry_run"] is True


def test_bigquery_list_datasets(tmp_path: Path, bq: FakeBigQuery) -> None:
    bq.datasets = {
        "proj": {"sales": ["orders", "order_lines", "customers"], "staging": ["orders_raw"]},
        "other": {"sales_eu": ["orders"]},
    }
    connector = _bigquery(tmp_path, dataset="sales")
    assert connector.list_datasets("order*") == ["order_lines", "orders"]
    assert connector.list_datasets("s*.orders*") == ["sales.orders", "staging.orders_raw"]
    assert connector.list_datasets("other.sales_*.o*") == ["other.sales_eu.orders"]
    with pytest.raises(ConnectorError, match="needs a dataset"):
        _bigquery(tmp_path).list_datasets("orders")


def test_bigquery_server_version(tmp_path: Path, bq: FakeBigQuery) -> None:
    assert _bigquery(tmp_path).server_version() == "Google BigQuery (google-cloud-bigquery 3.99.0)"


# ====================================================================== Redshift


def test_redshift_connect_args_and_session(tmp_path: Path, rs: FakeDriver) -> None:
    connector = _redshift(
        tmp_path,
        database="dev",
        schema="Analytics",
        user="dvf",
        password=SECRET,
        query_timeout_s=60,
        options={"ssl": True, "dvf_fetch_size": 5000},
    )
    connector.open()
    conn = rs.connection
    assert conn.kwargs == {
        "host": "cluster.abc.eu-west-1.redshift.amazonaws.com",
        "port": 5439,
        "database": "dev",
        "user": "dvf",
        "password": SECRET,
        "application_name": "dvf",
        "ssl": True,
    }
    assert conn.autocommit is True
    assert conn.statements == [
        "SET timezone TO 'UTC'",
        'SET search_path TO "Analytics", public',
        "SET statement_timeout TO 60000",
    ]


def test_redshift_url_parsing(tmp_path: Path, rs: FakeDriver) -> None:
    connector = _redshift(
        tmp_path,
        url="redshift://dvf%40corp:p%40ss%3Aword@host.example.com:5440/prod?ssl=true&timeout=30",
    )
    assert connector._connect_args() == {
        "host": "host.example.com",
        "port": 5440,
        "database": "prod",
        "user": "dvf@corp",
        "password": "p@ss:word",
        "ssl": True,
        "timeout": 30,
        "application_name": "dvf",
    }
    with pytest.raises(ConnectorError, match="not a Redshift URL"):
        _redshift(tmp_path, url="mysql://u:p@h/db")._connect_args()


def test_redshift_connect_error_is_wrapped(tmp_path: Path, rs: FakeDriver) -> None:
    rs.connect_error = FakeError('password authentication failed for user "dvf"')
    with pytest.raises(ConnectorError) as info:
        _redshift(tmp_path, user="dvf", password=SECRET).open()
    assert "Authentication failed" in (info.value.hint or "")
    assert SECRET not in str(info.value)


def test_redshift_query_and_cursor_streaming(tmp_path: Path, rs: FakeDriver) -> None:
    rs.responder.add("^SELECT", Result(["id", "name"], [(1, "a"), (2, "b")]))
    connector = _redshift(tmp_path, options={"dvf_fetch_size": 2})
    assert connector.query("SELECT id, name FROM t").to_pylist() == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]

    pages = [[(1,), (2,)], [(3,), (4,)], [(5,)]]

    def fetch(sql: str) -> Result:
        return Result(["k0"], pages.pop(0) if pages else [])

    rs.responder.add("^FETCH FORWARD", fetch)
    batches = list(connector.iter_batches("SELECT k0 FROM t", batch_size=4))
    assert [b.num_rows for b in batches] == [4, 1]
    statements = rs.connection.statements[-7:]
    assert statements[0] == "BEGIN"
    assert statements[1].startswith("DECLARE dvf_cursor_") and statements[1].endswith(
        "CURSOR FOR SELECT k0 FROM t"
    )
    assert statements[2].startswith("FETCH FORWARD 2 FROM dvf_cursor_")
    assert statements[-2].startswith("CLOSE dvf_cursor_") and statements[-1] == "COMMIT"


def test_redshift_abandoned_stream_rolls_back(tmp_path: Path, rs: FakeDriver) -> None:
    rs.responder.add("^FETCH FORWARD", Result(["k0"], [(1,)] * 3))
    stream = _redshift(tmp_path).iter_batches("SELECT k0 FROM t", batch_size=3)
    next(stream)
    stream.close()
    assert rs.connection.statements[-1] == "ROLLBACK"


def test_redshift_rejects_time_travel_and_resolves(tmp_path: Path) -> None:
    connector = _redshift(tmp_path)
    with pytest.raises(ConnectorError, match="time travel"):
        connector.resolve(DatasetSpec(table="orders", as_of="2024-01-01"))
    rel = connector.resolve(DatasetSpec(table="sales.orders", filter="region = 'EU'"))
    query = connector.resolve(DatasetSpec(query="SELECT 1 AS id"))
    for relation in (rel, query):
        _parses(f"SELECT * FROM {relation.sql}", "redshift")


def test_redshift_schema_from_svv_columns(tmp_path: Path, rs: FakeDriver) -> None:
    rs.responder.add(
        "FROM svv_columns",
        Result(
            ["column_name", "data_type", "is_nullable", "column_default", "ordinal_position"],
            [
                ("id", "integer", "NO", None, 1, None, 32, 0, None),
                ("name", "character varying", "YES", None, 2, 256, None, None, "Full name"),
                ("amount", "numeric", "YES", "0", 3, None, 18, 2, None),
                ("code", "character", "YES", None, 4, 3, None, None, None),
                ("created", "timestamp with time zone", "YES", None, 5, None, None, None, None),
                ("raw", "binary varying", "YES", None, 6, 64, None, None, None),
            ],
        ),
    )
    rs.responder.add("PRIMARY KEY", Result(["column_name"], [("id",)]))
    connector = _redshift(tmp_path, schema="Analytics")
    schema = connector.get_schema(connector.resolve(DatasetSpec(table="Sales.Orders")))
    params = next(p for s, p in rs.connection.executed if "svv_columns" in s)
    assert params == ("sales", "orders")
    assert [c.native_type for c in schema.columns] == [
        "integer",
        "character varying(256)",
        "numeric(18,2)",
        "character(3)",
        "timestamp with time zone",
        "binary varying(64)",
    ]
    types_ = [c.type for c in schema.columns]
    assert types_[1].length == 256 and types_[2].scale == 2 and types_[3].fixed_length
    assert types_[4].logical is LogicalType.TIMESTAMP_TZ
    assert types_[5].logical is LogicalType.BINARY
    assert schema.columns[0].nullable is False and schema.columns[1].comment == "Full name"
    assert schema.primary_key == ("id",)

    connector.get_schema(connector.resolve(DatasetSpec(table='"Sales"."Orders"')))
    assert rs.connection.executed[-2][1] == ("Sales", "Orders")
    connector.get_schema(connector.resolve(DatasetSpec(table="orders")))
    assert rs.connection.executed[-2][1] == ("analytics", "orders")

    rs.responder.add("FROM svv_columns", Result(["column_name"], []))
    with pytest.raises(ConnectorError, match="not found"):
        connector.get_schema(connector.resolve(DatasetSpec(table="nope")))
    rs.responder.add("PRIMARY KEY", FakeError("permission denied"))
    assert connector.get_primary_key(connector.resolve(DatasetSpec(table="x"))) == ()


def test_redshift_query_schema_and_exists(tmp_path: Path, rs: FakeDriver) -> None:
    rs.responder.add(
        "LIMIT 0",
        Result(
            description=[
                ("id", 20, None, None, None, None, None),
                ("ts", 1184, None, None, None, None, None),
            ]
        ),
    )
    connector = _redshift(tmp_path)
    schema = connector.get_schema(connector.resolve(DatasetSpec(query="SELECT 1")))
    assert [(c.name, c.native_type) for c in schema.columns] == [
        ("id", "bigint"),
        ("ts", "timestamp with time zone"),
    ]
    rs.responder.add("FROM svv_tables WHERE table_schema = %s", Result(["?column?"], [(1,)]))
    assert connector.exists(connector.resolve(DatasetSpec(table="public.orders")))
    rs.responder.add("FROM svv_tables WHERE table_schema = %s", Result(["?column?"], []))
    assert not connector.exists(connector.resolve(DatasetSpec(table="public.nope")))


def test_redshift_list_datasets_and_version(tmp_path: Path, rs: FakeDriver) -> None:
    rs.responder.add(
        "FROM svv_tables WHERE table_schema NOT IN",
        Result(
            ["table_schema", "table_name"],
            [("public", "orders"), ("public", "order_lines"), ("staging", "orders_raw")],
        ),
    )
    connector = _redshift(tmp_path)
    assert connector.list_datasets("order*") == ["order_lines", "orders"]
    assert connector.list_datasets("*.orders*") == ["public.orders", "staging.orders_raw"]
    rs.responder.add(
        "version",
        Result(["version"], [("PostgreSQL 8.0.2 on i686-pc-linux-gnu, Redshift 1.0.77467",)]),
    )
    assert connector.server_version() == "Amazon Redshift 1.0.77467"


# ====================================================================== Databricks


def test_databricks_connect_args_and_session(tmp_path: Path, dbx: FakeDriver) -> None:
    connector = _databricks(
        tmp_path,
        access_token=SECRET,
        catalog="main",
        schema="sales",
        query_timeout_s=300,
        options={"_socket_timeout": 60},
    )
    connector.open()
    conn = dbx.connection
    assert conn.kwargs == {
        "server_hostname": "adb-123.4.azuredatabricks.net",
        "http_path": "/sql/1.0/warehouses/abc",
        "access_token": SECRET,
        "catalog": "main",
        "schema": "sales",
        "_socket_timeout": 60,
    }
    assert conn.statements == ["SET TIME ZONE 'UTC'", "SET STATEMENT_TIMEOUT = 300"]

    dbx.responder.add(
        "STATEMENT_TIMEOUT", FakeError("Configuration STATEMENT_TIMEOUT is not available")
    )
    _databricks(tmp_path, auth_type="databricks-oauth", query_timeout_s=10).open()
    assert dbx.connection.kwargs["auth_type"] == "databricks-oauth"
    assert "access_token" not in dbx.connection.kwargs  # a cluster: the timeout is skipped


def test_databricks_errors_are_wrapped(tmp_path: Path, dbx: FakeDriver) -> None:
    dbx.connect_error = FakeError("Error during request to server: 403 Invalid access token")
    with pytest.raises(ConnectorError) as info:
        _databricks(tmp_path, access_token=SECRET).open()
    assert "access_token" in (info.value.hint or "")
    assert SECRET not in str(info.value)
    dbx.connect_error = None
    dbx.responder.add("FROM main.t", FakeError("[TABLE_OR_VIEW_NOT_FOUND] main.t"))
    with pytest.raises(ConnectorError) as info:
        _databricks(tmp_path).query("SELECT * FROM main.t")
    assert "catalog, schema, and table" in (info.value.hint or "")


def test_databricks_query_and_batches(tmp_path: Path, dbx: FakeDriver) -> None:
    dbx.responder.add("FROM t", Result(["k0", "h"], [(str(i), "x") for i in range(5)]))
    connector = _databricks(tmp_path)
    assert connector.query("SELECT k0, h FROM t").num_rows == 5
    batches = list(connector.iter_batches("SELECT k0, h FROM t", batch_size=2))
    assert [b.num_rows for b in batches] == [2, 2, 1]


def test_databricks_resolve_and_time_travel(tmp_path: Path) -> None:
    connector = _databricks(tmp_path)
    version = connector.resolve(DatasetSpec(table="main.sales.orders", as_of="12"))
    assert version.base_sql == "main.sales.orders VERSION AS OF 12" and version.table is None
    stamp = connector.resolve(DatasetSpec(table="orders", as_of="2024-05-01 00:00:00"))
    assert stamp.base_sql == "orders TIMESTAMP AS OF '2024-05-01 00:00:00'"
    plain = connector.resolve(DatasetSpec(table="orders", filter="region = 'EU'"))
    query = connector.resolve(DatasetSpec(query="SELECT 1 AS id"))
    for relation in (version, stamp, plain, query):
        _parses(f"SELECT * FROM {relation.sql}", "databricks")
    with pytest.raises(ConnectorError, match="as_of"):
        connector.resolve(DatasetSpec(query="SELECT 1", as_of="3"))


def test_databricks_unity_catalog_schema_and_primary_key(tmp_path: Path, dbx: FakeDriver) -> None:
    dbx.responder.add("current_catalog", Result(["c", "s"], [("main", "sales")]))
    dbx.responder.add(
        "information_schema.columns",
        Result(
            [
                "column_name",
                "full_data_type",
                "is_nullable",
                "column_default",
                "ordinal_position",
                "comment",
            ],
            [
                ("id", "bigint", "NO", None, 1, None),
                ("amount", "decimal(12,2)", "YES", None, 2, "Gross"),
                ("created", "timestamp", "YES", None, 3, None),
                ("loaded", "timestamp_ntz", "YES", None, 4, None),
                ("tags", "array<string>", "YES", None, 5, None),
            ],
        ),
    )
    dbx.responder.add("table_constraints", Result(["column_name"], [("id",)]))
    connector = _databricks(tmp_path)
    schema = connector.get_schema(connector.resolve(DatasetSpec(table="Orders")))
    statements = dbx.connection.statements
    info_sql = next(s for s in statements if "information_schema.columns" in s)
    assert "`main`.information_schema.columns" in info_sql
    assert "table_schema = 'sales' AND table_name = 'orders'" in info_sql
    types_ = {c.name: c.type for c in schema.columns}
    assert types_["amount"].scale == 2
    assert types_["created"].logical is LogicalType.TIMESTAMP_TZ
    assert types_["loaded"].logical is LogicalType.TIMESTAMP
    assert schema.columns[0].nullable is False and schema.columns[1].comment == "Gross"
    assert schema.primary_key == ("id",)
    pk_sql = next(s for s in statements if "table_constraints" in s)
    _parses(pk_sql, "databricks")
    _parses(info_sql, "databricks")


def test_databricks_hive_metastore_falls_back_to_describe(tmp_path: Path, dbx: FakeDriver) -> None:
    dbx.responder.add("current_catalog", Result(["c", "s"], [("hive_metastore", "default")]))
    dbx.responder.add(
        "DESCRIBE TABLE",
        Result(
            ["col_name", "data_type", "comment"],
            [
                ("id", "int", None),
                ("day", "date", "partition"),
                ("", "", ""),
                ("# Partition Information", "", ""),
                ("# col_name", "data_type", "comment"),
                ("day", "date", None),
            ],
        ),
    )
    connector = _databricks(tmp_path)
    schema = connector.get_schema(connector.resolve(DatasetSpec(table="events")))
    assert [(c.name, c.native_type) for c in schema.columns] == [("id", "int"), ("day", "date")]
    assert schema.columns[0].nullable is None and schema.primary_key == ()
    assert not any("information_schema" in s for s in dbx.connection.statements)
    assert connector.exists(connector.resolve(DatasetSpec(table="events")))
    dbx.responder.add("DESCRIBE TABLE", FakeError("[TABLE_OR_VIEW_NOT_FOUND]"))
    assert not connector.exists(connector.resolve(DatasetSpec(table="nope")))
    with pytest.raises(ConnectorError, match="not found"):
        connector.get_schema(connector.resolve(DatasetSpec(table="nope")))


def test_databricks_query_schema(tmp_path: Path, dbx: FakeDriver) -> None:
    dbx.responder.add(
        "DESCRIBE QUERY",
        Result(["col_name", "data_type", "comment"], [("x", "decimal(5,1)", None)]),
    )
    connector = _databricks(tmp_path)
    schema = connector.get_schema(connector.resolve(DatasetSpec(query="SELECT 1.5 AS x")))
    assert [(c.name, c.native_type) for c in schema.columns] == [("x", "decimal(5,1)")]
    assert schema.columns[0].type.scale == 1


def test_databricks_list_datasets(tmp_path: Path, dbx: FakeDriver) -> None:
    dbx.responder.add("current_catalog", Result(["c", "s"], [("main", "sales")]))
    dbx.responder.add(
        "information_schema.tables",
        Result(
            ["table_schema", "table_name"],
            [("sales", "orders"), ("sales", "order_lines"), ("staging", "orders_raw")],
        ),
    )
    connector = _databricks(tmp_path)
    assert connector.list_datasets("order*") == ["order_lines", "orders"]
    assert connector.list_datasets("*.orders*") == ["sales.orders", "staging.orders_raw"]

    hive = _databricks(tmp_path)
    dbx.responder.add("current_catalog", Result(["c", "s"], [("hive_metastore", "default")]))
    dbx.responder.add(
        "SHOW TABLES",
        Result(["database", "tableName", "isTemporary"], [("default", "events", False)]),
    )
    assert hive.list_datasets("ev*") == ["events"]
    assert "SHOW TABLES IN `default`" in dbx.connection.statements


def test_databricks_server_version(tmp_path: Path, dbx: FakeDriver) -> None:
    dbx.responder.add("version", Result(["v"], [("3.5.2 a1b2c3",)]))
    assert _databricks(tmp_path).server_version() == "Databricks (Spark 3.5.2)"


# ====================================================================== live (opt-in)
#
# Promote a connector from beta by running these against a real account:
#
#   DVF_TEST_SNOWFLAKE='{"account": "...", "user": "...", "password": "...", "warehouse": "..."}'
#   DVF_TEST_BIGQUERY='{"project": "...", "credentials_path": "/path/sa.json"}'
#   DVF_TEST_REDSHIFT='{"url": "redshift://user:password@host:5439/dev"}'
#   DVF_TEST_DATABRICKS='{"server_hostname": "...", "http_path": "...", "access_token": "..."}'
#   uv run pytest -m integration tests/unit/test_cloud_connectors.py
#
# Each value is a JSON object with the connection settings (validated by the platform's config
# model). The tests only run SELECT statements on literals: nothing is created or written.

_LIVE = {
    "snowflake": (SnowflakeConnector, SnowflakeConnection),
    "bigquery": (BigQueryConnector, BigQueryConnection),
    "redshift": (RedshiftConnector, DatabaseConnection),
    "databricks": (DatabricksConnector, DatabricksConnection),
}


def _live_connector(platform: str, tmp_path: Path) -> Connector:
    raw = os.environ.get(f"DVF_TEST_{platform.upper()}")
    if not raw:
        pytest.skip(f"set DVF_TEST_{platform.upper()} to run live {platform} tests")
    connector_cls, config_cls = _LIVE[platform]
    config = config_cls.model_validate({"type": platform, **json.loads(raw)})
    return connector_cls(platform, config, tmp_path)  # type: ignore[abstract]


_TYPED = {
    # (python value, canonical kind, SQL type per platform)
    "decimal": {
        "snowflake": "NUMBER(18,3)",
        "bigquery": "NUMERIC",
        "redshift": "DECIMAL(18,3)",
        "databricks": "DECIMAL(18,3)",
    },
    "timestamp": {
        "snowflake": "TIMESTAMP_NTZ",
        "bigquery": "DATETIME",
        "redshift": "TIMESTAMP",
        "databricks": "TIMESTAMP_NTZ",
    },
    "date": {"snowflake": "DATE", "bigquery": "DATE", "redshift": "DATE", "databricks": "DATE"},
    "text": {
        "snowflake": "VARCHAR",
        "bigquery": "STRING",
        "redshift": "VARCHAR(256)",
        "databricks": "STRING",
    },
}

_LIVE_VALUES: list[tuple[str, Any]] = [
    ("decimal", Decimal("1.005")),
    ("decimal", Decimal("-0.0004")),
    ("decimal", Decimal("12345678901234.5")),
    ("timestamp", datetime(2024, 3, 10, 2, 30, 0, 123456)),
    ("timestamp", datetime(1969, 12, 31, 23, 59, 59, 999999)),
    ("date", date(2024, 2, 29)),
    ("text", "  Straße İstanbul ΟΔΟΣ  "),
    ("text", "tab\tand\nnewline 'quoted' back\\slash"),
]


@pytest.mark.integration
@pytest.mark.parametrize("platform", sorted(_LIVE))
def test_live_canonical_parity(platform: str, tmp_path: Path) -> None:
    """Canonical SQL evaluated by the real service equals the Python reference."""
    from dvf.core.types import CanonicalType
    from dvf.normalize.canonical import (
        CanonSpec,
        Kind,
        StringOptions,
        canonical_sql,
        canonical_text,
        row_hash,
        row_hash_sql,
    )

    connector = _live_connector(platform, tmp_path)
    dialect = connector.sql_dialect
    kinds = {
        "decimal": (Kind.DECIMAL, CanonicalType.decimal(18, 3), 3),
        "timestamp": (Kind.TIMESTAMP, CanonicalType.timestamp(6), 0),
        "date": (Kind.DATE, CanonicalType.date(), 0),
        "text": (Kind.TEXT, CanonicalType.string(), 0),
    }
    strings = StringOptions(
        trim="both", case_sensitive=False, unicode_form="none", collapse_whitespace=True
    )
    items, expected = [], []
    for i, (name, value) in enumerate(_LIVE_VALUES):
        kind, ctype, scale = kinds[name]
        spec = CanonSpec(
            kind=kind, logical=ctype.logical, scale=scale, precision="ms", strings=strings
        )
        text = value.isoformat(sep=" ") if isinstance(value, datetime) else value
        typed = f"CAST({dialect.literal(text)} AS {_TYPED[name][platform]})"
        items.append(f"{canonical_sql(typed, spec, dialect)} AS v{i}")
        expected.append(canonical_text(value, spec))
    hashed = row_hash_sql([dialect.literal(e) for e in expected], dialect)
    with connector:
        row = connector.query(f"SELECT {', '.join(items)}, {hashed} AS h").to_pylist()[0]
    assert [row[f"v{i}"] for i in range(len(expected))] == expected
    assert row["h"] == row_hash(expected)


@pytest.mark.integration
@pytest.mark.parametrize("platform", sorted(_LIVE))
def test_live_connection_and_hash_buckets(platform: str, tmp_path: Path) -> None:
    connector = _live_connector(platform, tmp_path)
    report = connector.test_connection()
    assert report.ok, report.message
    dialect = connector.sql_dialect
    digest = "0123456789abcdeffedcba9876543210"
    sql = (
        f"SELECT {dialect.hash_int(dialect.literal(digest), 0)} AS a, "
        f"{dialect.hash_int(dialect.literal(digest), 24)} AS b, "
        f"{dialect.bucket(dialect.hash_int(dialect.literal(digest), 8), 1000)} AS c"
    )
    with connector:
        row = connector.query(sql).to_pylist()[0]
    assert int(row["a"]) == 0x01234567 and int(row["b"]) == 0x76543210
    assert int(row["c"]) == 0x89ABCDEF * 1000 // 2**32
