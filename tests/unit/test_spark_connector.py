"""The Spark connector on a local Spark session: sessions, datasets, schemas, and queries."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.orc as orc
import pyarrow.parquet as pq
import pytest

from dvf.config.connections import SparkConnection
from dvf.config.models import DatasetSpec
from dvf.core.datasets import DatasetKind
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import LogicalType
from dvf.plugins.registry import PluginKind, new_registry
from tests.unit.test_spark_support import PANDAS_WARNING, local_spark

pytestmark = [pytest.mark.spark, pytest.mark.filterwarnings(PANDAS_WARNING)]

TABLE = pa.table(
    {
        "id": pa.array([1, 2, 3], pa.int64()),
        "name": ["a", None, "ß"],
        "amount": pa.array([Decimal("1.50"), None, Decimal("-2.25")], pa.decimal128(12, 2)),
        "ts": pa.array(
            [datetime(2024, 1, 1, 10), None, datetime(1960, 2, 29, 23, 59, 59, 123456)],
            pa.timestamp("us"),
        ),
        "tsz": pa.array(
            [datetime(2024, 1, 1, 10, tzinfo=UTC), None, datetime(1950, 7, 1, 1, tzinfo=UTC)],
            pa.timestamp("us", tz="UTC"),
        ),
        "day": pa.array([date(2024, 2, 29), None, date(1900, 1, 1)]),
        "raw": pa.array([b"\x00\xff", None, b""]),
        "tags": pa.array([[1, 2], None, []], pa.list_(pa.int64())),
        "info": pa.array([{"x": 1, "y": "a"}, None, {"x": None, "y": "b"}]),
    }
)


@pytest.fixture(scope="module")
def spark() -> Any:
    return local_spark()


@pytest.fixture
def files(tmp_path: Path) -> Path:
    pq.write_table(TABLE, tmp_path / "t.parquet")
    pacsv.write_csv(TABLE.select(["id", "name", "amount", "ts", "day"]), tmp_path / "t.csv")
    pacsv.write_csv(
        TABLE.select(["id", "name"]),
        tmp_path / "t.tsv",
        pacsv.WriteOptions(delimiter="\t"),
    )
    lines = [json.dumps({"id": i, "name": n}) for i, n in [(1, "a"), (2, None)]]
    (tmp_path / "t.ndjson").write_text("\n".join(lines), encoding="utf-8")
    orc.write_table(TABLE.select(["id", "name"]), tmp_path / "t.orc")
    return tmp_path


@pytest.fixture
def connector(spark: Any, files: Path) -> Any:
    from dvf.connectors.spark import SparkConnector

    conn = SparkConnector("lake", SparkConnection(type="spark"), files)
    conn.open()
    yield conn
    conn.close()


def make(files: Path, **settings: Any) -> Any:
    from dvf.connectors.spark import SparkConnector

    return SparkConnector("lake", SparkConnection(type="spark", **settings), files)


# ---------------------------------------------------------------------- sessions


def test_registered_as_ready() -> None:
    registry = new_registry()
    for kind in (PluginKind.CONNECTOR, PluginKind.ENGINE):
        entry = registry.get(kind, "spark")
        assert entry.ready
        assert entry.status == "ready"


def test_uses_a_child_of_the_running_session(spark: Any, connector: Any) -> None:
    from dvf.connectors.spark import same_application

    assert connector.session is not spark
    assert same_application(connector.session, spark)
    assert connector.session.conf.get("spark.sql.session.timeZone") == "UTC"
    assert connector.server_version().startswith("Apache Spark 4")
    assert connector.dialect == "spark"
    assert connector.sql_dialect.name == "spark"
    report = connector.test_connection()
    assert report.ok, report.message


def test_master_set_reuses_the_running_application(spark: Any, files: Path) -> None:
    conn = make(files, master="local[2]", conf={"spark.sql.shuffle.partitions": "3"})
    try:
        assert conn.session.sparkContext is spark.sparkContext
        assert conn.session.conf.get("spark.sql.shuffle.partitions") == "3"
    finally:
        conn.close()


def test_forks_share_the_session(connector: Any) -> None:
    twin = connector.fork()
    assert twin.session is connector.session
    twin.close()  # a fork never closes the shared session
    assert connector.query("SELECT 1 AS ok").to_pylist() == [{"ok": 1}]


def test_close_drops_dvf_views(connector: Any) -> None:
    rel = connector.resolve(DatasetSpec(path="t.parquet"))
    connector.query(f"SELECT COUNT(*) AS n FROM {rel.sql}")
    session = connector.session
    view = rel.base_sql.strip("`")
    assert session.catalog.tableExists(view)
    connector.close()
    assert not session.catalog.tableExists(view)


def test_current_schema(spark: Any, files: Path) -> None:
    conn = make(files, schema="default")
    try:
        assert conn.session.catalog.currentDatabase() == "default"
    finally:
        conn.close()
    missing = make(files, schema="dvf_no_such_schema")
    with pytest.raises(ConnectorError, match="cannot use catalog") as info:
        missing.open()
    assert info.value.hint


def test_databricks_connect_needs_its_package(files: Path) -> None:
    conn = make(files, databricks_connect=True)
    with pytest.raises(DependencyError) as info:
        conn.open()
    assert "databricks-connect" in str(info.value)
    assert info.value.hint is not None and "without pyspark" in info.value.hint


def test_session_start_failures_are_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyspark.sql

    from dvf.connectors import spark as module

    class Broken:
        def master(self, _: str) -> Broken:
            return self

        def appName(self, _: str) -> Broken:  # noqa: N802 - PySpark's builder API
            return self

        def config(self, *_: str) -> Broken:
            return self

        def getOrCreate(self) -> Any:  # noqa: N802 - PySpark's builder API
            raise RuntimeError("Java gateway process exited before sending its port number")

    monkeypatch.setattr(pyspark.sql.SparkSession, "builder", Broken())
    with pytest.raises(ConnectorError, match="Could not start") as info:
        module.start_session("local[1]")
    assert info.value.hint is not None and "Java" in info.value.hint


# ---------------------------------------------------------------------- datasets


def test_parquet_schema_and_rows(connector: Any) -> None:
    rel = connector.resolve(DatasetSpec(path="t.parquet"))
    assert rel.ref.kind is DatasetKind.PATH
    assert connector.exists(rel)
    schema = connector.get_schema(rel)
    types = {c.name: (c.native_type, c.type.logical) for c in schema.columns}
    assert types["id"] == ("bigint", LogicalType.INTEGER)
    assert types["amount"] == ("decimal(12,2)", LogicalType.DECIMAL)
    assert types["ts"] == ("timestamp_ntz", LogicalType.TIMESTAMP)
    assert types["tsz"] == ("timestamp", LogicalType.TIMESTAMP_TZ)
    assert types["info"][1] is LogicalType.STRUCT
    assert schema.primary_key == ()
    assert all(c.nullable is None for c in schema.columns)  # files declare no nullability
    assert connector.get_primary_key(rel) == ()
    assert connector.query(f"SELECT * FROM {rel.sql} ORDER BY id").to_pylist() == TABLE.to_pylist()


def test_filters_apply_to_the_dataset(connector: Any) -> None:
    rel = connector.resolve(DatasetSpec(path="t.parquet", filter="id >= 2"))
    rows = connector.query(f"SELECT id FROM {rel.sql} ORDER BY id").column("id").to_pylist()
    assert rows == [2, 3]


@pytest.mark.parametrize(
    ("path", "options"),
    [
        ("t.csv", {}),
        (
            "t.csv",
            {"schema": "id BIGINT, name STRING, amount DECIMAL(12,2), ts TIMESTAMP_NTZ, day DATE"},
        ),
        ("t.tsv", {}),
        ("t.ndjson", {}),
        ("t.orc", {}),
        ("t.orc", {"format": "orc"}),
    ],
)
def test_other_file_formats(connector: Any, path: str, options: dict[str, Any]) -> None:
    rel = connector.resolve(DatasetSpec(path=path, options=options))
    rows = connector.query(f"SELECT id, name FROM {rel.sql} ORDER BY id").to_pylist()
    assert rows[:2] == [{"id": 1, "name": "a"}, {"id": 2, "name": None}]


def test_csv_with_a_schema_keeps_exact_types(connector: Any) -> None:
    ddl = "id BIGINT, name STRING, amount DECIMAL(12,2), ts TIMESTAMP_NTZ, day DATE"
    rel = connector.resolve(DatasetSpec(path="t.csv", options={"schema": ddl}))
    native = [c.native_type for c in connector.get_schema(rel).columns]
    assert native == ["bigint", "string", "decimal(12,2)", "timestamp_ntz", "date"]
    rows = connector.query(f"SELECT amount, ts FROM {rel.sql} ORDER BY id").to_pylist()
    assert rows[2] == {"amount": Decimal("-2.25"), "ts": datetime(1960, 2, 29, 23, 59, 59, 123456)}


def test_absolute_paths_and_urls(connector: Any, files: Path) -> None:
    from dvf.connectors.spark import _location

    rel = connector.resolve(DatasetSpec(path=(files / "t.parquet").as_posix()))
    assert connector.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 3}]
    assert _location("s3a://bucket/data", files) == "s3a://bucket/data"
    assert _location("dbfs:/mnt/data", files) == "dbfs:/mnt/data"
    assert _location("data/*.parquet", files).startswith("file:")
    assert _location("data/*.parquet", files).endswith("/data/*.parquet")


def test_missing_files(connector: Any) -> None:
    rel = connector.resolve(DatasetSpec(path="missing.parquet"))
    assert not connector.exists(rel)
    with pytest.raises(ConnectorError) as info:
        connector.get_schema(rel)
    assert info.value.hint is not None and "path" in info.value.hint.lower()


def test_unknown_format(connector: Any, files: Path) -> None:
    (files / "data.bin").write_bytes(b"x")
    rel = connector.resolve(DatasetSpec(path="data.bin"))
    with pytest.raises(ConnectorError, match="Cannot infer the file format"):
        connector.query(f"SELECT * FROM {rel.sql}")


def test_formats_without_their_package_explain_how_to_add_it(connector: Any, files: Path) -> None:
    (files / "t.avro").write_bytes(b"Obj\x01")
    rel = connector.resolve(DatasetSpec(path="t.avro"))
    with pytest.raises(ConnectorError, match="not installed") as info:
        connector.query(f"SELECT * FROM {rel.sql}")
    assert info.value.hint is not None and "spark-avro" in info.value.hint
    delta = connector.resolve(DatasetSpec(path="t.parquet", options={"format": "delta"}))
    with pytest.raises(ConnectorError) as info:
        connector.query(f"SELECT * FROM {delta.sql}")
    assert info.value.hint is not None and "delta" in info.value.hint.lower()


def test_tables_and_queries(spark: Any, connector: Any) -> None:
    spark.createDataFrame(TABLE.select(["id", "name"])).createOrReplaceGlobalTempView("people")
    try:
        rel = connector.resolve(DatasetSpec(table="global_temp.people"))
        assert rel.ref.kind is DatasetKind.TABLE and rel.table == "global_temp.people"
        assert connector.exists(rel)
        nullable = {c.name: c.nullable for c in connector.get_schema(rel).columns}
        assert nullable == {"id": True, "name": True}
        query = connector.resolve(DatasetSpec(query="SELECT id FROM global_temp.people"))
        assert query.ref.kind is DatasetKind.QUERY
        assert connector.query(f"SELECT COUNT(*) AS n FROM {query.sql}").to_pylist() == [{"n": 3}]
        assert connector.list_datasets("global_temp.peo*") == ["global_temp.people"]
        assert not connector.exists(connector.resolve(DatasetSpec(table="global_temp.nope")))
    finally:
        spark.catalog.dropGlobalTempView("people")


def test_parquet_files_as_tables(connector: Any, files: Path) -> None:
    """Spark SQL reads files through ``format.`path``` table names (referential integrity
    parents name datasets with ``table`` only)."""
    uri = (files / "t.parquet").absolute().as_uri()
    rel = connector.resolve(DatasetSpec(table=f"parquet.`{uri}`"))
    assert connector.exists(rel)
    assert len(connector.get_schema(rel).columns) == len(TABLE.schema)


def test_time_travel(connector: Any) -> None:
    from dvf.connectors.spark import as_of_clause

    rel = connector.resolve(DatasetSpec(table="lake.orders", as_of="12"))
    assert rel.base_sql == "lake.orders VERSION AS OF 12"
    assert rel.table is None  # not a plain catalog table any more
    assert as_of_clause("2024-01-01 10:00:00") == "TIMESTAMP AS OF '2024-01-01 10:00:00'"
    assert connector._time_travel("delta", "3") == {"versionAsOf": 3}
    assert connector._time_travel("delta", "2024-01-01") == {"timestampAsOf": "2024-01-01"}
    assert connector._time_travel("iceberg", "8744736658442914487") == {
        "snapshot-id": 8744736658442914487
    }
    assert connector._time_travel("iceberg", "2024-01-01T00:00:00Z") == {
        "as-of-timestamp": 1704067200000
    }
    with pytest.raises(ConnectorError, match="Delta or Iceberg"):
        connector._time_travel("parquet", "3")
    with pytest.raises(ConnectorError, match="as_of"):
        connector.resolve(DatasetSpec(query="SELECT 1", as_of="3"))
    rel = connector.resolve(DatasetSpec(path="t.parquet", as_of="3"))
    with pytest.raises(ConnectorError, match="Delta or Iceberg"):
        connector.query(f"SELECT * FROM {rel.sql}")


# ---------------------------------------------------------------------- queries


def test_iter_batches_streams_exact_values(connector: Any) -> None:
    rel = connector.resolve(DatasetSpec(path="t.parquet"))
    sql = f"SELECT * FROM {rel.sql} ORDER BY id"
    batches = list(connector.iter_batches(sql, batch_size=2))
    assert [b.num_rows for b in batches] == [2, 1]
    streamed = pa.Table.from_batches(batches)
    assert streamed.schema.field("tsz").type == pa.timestamp("us", tz="UTC")
    assert streamed.to_pylist() == connector.query(sql).to_pylist() == TABLE.to_pylist()


def test_iter_batches_nested_timestamps_and_maps(connector: Any) -> None:
    sql = (
        "SELECT map('a', 1) AS m, array(TIMESTAMP_NTZ '1960-01-01 00:00:01') AS a, "
        "named_struct('t', TIMESTAMP '2024-01-01 10:00:00') AS s, "
        "INTERVAL '1' DAY AS d, CAST(NULL AS STRING) AS n"
    )
    batch = next(iter(connector.iter_batches(sql)))
    row = batch.to_pylist()[0]
    assert row["m"] == [("a", 1)]
    assert row["a"] == ["1960-01-01 00:00:01"]
    assert row["s"] == {"t": "2024-01-01 10:00:00"}
    assert row["n"] is None
    assert list(connector.iter_batches("SELECT 1 AS x WHERE 1 = 0")) == []


def test_dvf_sessions_never_become_active(spark: Any, connector: Any) -> None:
    from pyspark.sql import SparkSession

    connector.register_arrow("dvf_active_test", pa.table({"k": ["1"]}))
    connector.unregister_arrow("dvf_active_test")
    active = SparkSession.getActiveSession()
    assert active is not None and active._jsparkSession.equals(spark._jsparkSession)


def test_register_arrow(connector: Any) -> None:
    keys = pa.table({"k": ["1", "3"]})
    assert connector.register_arrow("dvf_keys_test", keys)
    rel = connector.resolve(DatasetSpec(path="t.parquet"))
    sql = (
        f"SELECT id FROM {rel.sql} WHERE EXISTS "
        "(SELECT 1 FROM dvf_keys_test k WHERE k.k = CAST(id AS STRING)) ORDER BY id"
    )
    assert connector.query(sql).column("id").to_pylist() == [1, 3]
    connector.unregister_arrow("dvf_keys_test")
    with pytest.raises(ConnectorError, match="TABLE_OR_VIEW_NOT_FOUND"):
        connector.query("SELECT * FROM dvf_keys_test")


def test_query_errors_carry_hints(connector: Any) -> None:
    with pytest.raises(ConnectorError) as info:
        connector.query("SELECT * FROM dvf_no_such_table")
    assert "Query failed on connection 'lake'" in str(info.value)
    assert info.value.hint is not None and "catalog" in info.value.hint
    with pytest.raises(ConnectorError) as info:
        connector.query("SELECT no_such_column FROM range(1)")
    assert info.value.hint is not None and "column" in info.value.hint


def test_query_timeout_cancels_the_job(files: Path) -> None:
    conn = make(files, query_timeout_s=1)
    try:
        with pytest.raises(ConnectorError, match="cancelled after 1 seconds"):
            conn.query("SELECT reflect('java.lang.Thread', 'sleep', 30000L) AS x FROM range(1)")
        assert conn.query("SELECT 1 AS ok").to_pylist() == [{"ok": 1}]
        # The job group is cleared afterwards.
        assert conn.session.sparkContext.getLocalProperty("spark.jobGroup.id") is None
    finally:
        conn.close()


def test_classic_sessions_are_not_retried(connector: Any) -> None:
    assert not connector.is_transient(RuntimeError("Connection reset by peer"))


# ---------------------------------------------------------------------- DataFrames


def test_from_dataframe(spark: Any) -> None:
    from dvf.connectors.spark import SparkConnector

    df = spark.createDataFrame(TABLE.select(["id", "name"]))
    conn, rel = SparkConnector.from_dataframe(df, "orders_df")
    try:
        assert rel.ref.kind is DatasetKind.DATAFRAME and rel.ref.name == "orders_df"
        assert rel.sql.startswith("`global_temp`.`dvf_frame_")
        assert conn.exists(rel)
        assert [c.name for c in conn.get_schema(rel).columns] == ["id", "name"]
        assert conn.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 3}]
        assert conn.session is not spark
    finally:
        conn.close()
    view = rel.sql.split(".")[1].strip("`")
    assert not spark.catalog.tableExists(f"global_temp.{view}")


def test_normalizers_work_as_dataframe_expressions(spark: Any) -> None:
    """``to_spark(col)`` is the dialect's SQL, usable with ``F.expr``."""
    from pyspark.sql import functions as F  # noqa: N812 - PySpark convention

    from dvf.core.types import CanonicalType
    from dvf.normalize.normalizers import BUILTIN_NORMALIZERS
    from dvf.sql.dialects import get_dialect

    df = spark.createDataFrame(pa.table({"code": ["  0042 ", "Straße"]}))
    dialect = get_dialect("spark")
    expr = BUILTIN_NORMALIZERS["upper"]().to_sql(
        BUILTIN_NORMALIZERS["trim"]().to_sql("code", dialect, CanonicalType.string()),
        dialect,
        CanonicalType.string(),
    )
    out = df.select(F.expr(expr).alias("c")).toArrow().column("c").to_pylist()
    assert out == ["0042", "STRAẞE"]


def test_local_sessions_pin_python_workers_to_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sys

    from dvf.connectors.spark import pin_python_workers

    monkeypatch.delenv("PYSPARK_PYTHON", raising=False)
    pin_python_workers()
    assert os.environ["PYSPARK_PYTHON"] == sys.executable

    monkeypatch.setenv("PYSPARK_PYTHON", "/opt/python/bin/python3.12")
    pin_python_workers()
    assert os.environ["PYSPARK_PYTHON"] == "/opt/python/bin/python3.12"


def test_windows_sessions_create_their_warehouse_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dvf.connectors.spark import prepare_warehouse

    monkeypatch.chdir(tmp_path)
    prepare_warehouse({}, windows=True)  # Spark's default, relative to the working folder
    assert (tmp_path / "spark-warehouse").is_dir()

    prepare_warehouse({"spark.sql.warehouse.dir": str(tmp_path / "a" / "wh")}, windows=True)
    assert (tmp_path / "a" / "wh").is_dir()

    uri = (tmp_path / "b" / "wh").as_uri()
    prepare_warehouse({"spark.sql.warehouse.dir": uri}, windows=True)
    assert (tmp_path / "b" / "wh").is_dir()

    prepare_warehouse({"spark.sql.warehouse.dir": "hdfs://nn/warehouse"}, windows=True)
    prepare_warehouse({"spark.sql.warehouse.dir": str(tmp_path / "c")}, windows=False)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b", "spark-warehouse"]


def test_error_summary_keeps_the_java_cause_of_py4j_errors() -> None:
    from dvf.connectors.spark import error_summary

    py4j = RuntimeError(
        "An error occurred while calling o168.createOrReplaceGlobalTempView.\n"
        ": org.apache.spark.SparkException: Job aborted due to stage failure\n"
        "\tat org.apache.spark.scheduler.DAGScheduler.failJobAndIndependentStages\n"
    )
    assert error_summary(py4j) == (
        "An error occurred while calling o168.createOrReplaceGlobalTempView. "
        "org.apache.spark.SparkException: Job aborted due to stage failure"
    )
    assert error_summary(RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] x\nmore")) == (
        "[TABLE_OR_VIEW_NOT_FOUND] x"
    )
    assert error_summary(RuntimeError("")) == "RuntimeError"
