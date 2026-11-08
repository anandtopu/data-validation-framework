"""The staged connector base: lazy staging, shared forks, schemas, and use in a comparison."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pyarrow as pa
import pytest
from pydantic import Field

from dvf import api
from dvf.config.connections import ConnectionConfig
from dvf.config.models import DatasetSpec
from dvf.connectors.staged import StagedConnector, StagedData, write_parquet_stream
from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, LogicalType
from dvf.plugins.registry import PluginKind, new_registry

ROWS = [{"id": i, "name": f"n{i}", "amount": i * 1.5} for i in range(1, 11)]


class FakeConfig(ConnectionConfig):
    rows: list[dict[str, Any]] = Field(default_factory=list)


class FakeStaged(StagedConnector):
    Config: ClassVar[type[ConnectionConfig]] = FakeConfig
    fetches: ClassVar[list[str]] = []
    lock = threading.Lock()

    def fetch(self, spec: DatasetSpec) -> StagedData:
        assert isinstance(self.config, FakeConfig)
        if spec.table == "missing":
            raise ConnectorError("no such collection")
        with self.lock:
            self.fetches.append(spec.table or "")
        rows = [r for r in self.config.rows if spec.options.get("min_id", 0) <= r["id"]]
        schema = pa.schema([("id", pa.int64()), ("name", pa.string()), ("amount", pa.float64())])

        def batches() -> Iterator[pa.RecordBatch]:
            for start in range(0, len(rows), 3):
                yield pa.RecordBatch.from_pylist(rows[start : start + 3], schema=schema)

        return StagedData(
            schema=schema,
            batches=batches(),
            native_types={"id": "int", "name": "string", "amount": "double"},
            canonical_types={"amount": CanonicalType.decimal(18, 2)},
            primary_key=("id",),
        )


def _connector(tmp_path: Path) -> FakeStaged:
    FakeStaged.fetches = []
    return FakeStaged("fake", FakeConfig(type="fake", rows=ROWS), tmp_path)


def test_staging_is_lazy_and_shared(tmp_path: Path) -> None:
    con = _connector(tmp_path)
    try:
        rel = con.resolve(DatasetSpec(table="orders"))
        again = con.resolve(DatasetSpec(table="orders", filter="id > 5"))
        assert rel.base_sql == again.base_sql
        assert FakeStaged.fetches == []
        assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 10}]
        assert con.query(f"SELECT COUNT(*) AS n FROM {again.sql}").to_pylist() == [{"n": 5}]
        twin = con.fork()
        rows = list(twin.iter_batches(f"SELECT * FROM {rel.sql}", batch_size=4))
        assert sum(b.num_rows for b in rows) == 10
        assert FakeStaged.fetches == ["orders"]  # fetched once, shared with the fork
        assert con.staged_rows(rel) == 10
        twin.close()
        assert con.query(f"SELECT MAX(id) AS m FROM {rel.sql}").to_pylist() == [{"m": 10}]
    finally:
        con.close()


def test_schema_uses_native_and_canonical_overrides(tmp_path: Path) -> None:
    con = _connector(tmp_path)
    try:
        rel = con.resolve(DatasetSpec(table="orders"))
        schema = con.get_schema(rel)
        assert [c.name for c in schema.columns] == ["id", "name", "amount"]
        assert schema.columns[0].native_type == "int"
        assert schema.columns[0].type.logical is LogicalType.INTEGER
        assert schema.columns[2].type == CanonicalType.decimal(18, 2)
        assert schema.primary_key == ("id",)
        assert con.get_primary_key(rel) == ("id",)
    finally:
        con.close()


def test_missing_dataset_and_options_select_data(tmp_path: Path) -> None:
    con = _connector(tmp_path)
    try:
        assert not con.exists(con.resolve(DatasetSpec(table="missing")))
        rel = con.resolve(DatasetSpec(table="orders", options={"min_id": 8}))
        assert con.exists(rel)
        assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 3}]
    finally:
        con.close()
    assert con._shared.folder is None  # the owner removes the staged files


def test_parallel_first_use_stages_once(tmp_path: Path) -> None:
    con = _connector(tmp_path)
    rel = con.resolve(DatasetSpec(table="orders"))
    forks = [con.fork() for _ in range(6)]
    counts: list[int] = []

    def work(c: Any) -> None:
        counts.append(c.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist()[0]["n"])

    threads = [threading.Thread(target=work, args=(f,)) for f in forks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counts == [10] * 6
    assert FakeStaged.fetches == ["orders"]
    for f in forks:
        f.close()
    con.close()


def test_write_parquet_stream_empty_and_reordered(tmp_path: Path) -> None:
    schema = pa.schema([("a", pa.int64()), ("b", pa.string())])
    assert write_parquet_stream(tmp_path / "e.parquet", schema, []) == 0
    batch = pa.RecordBatch.from_pydict({"b": ["x"], "a": [1]})
    assert write_parquet_stream(tmp_path / "r.parquet", schema, [batch]) == 1
    import pyarrow.parquet as pq

    assert pq.read_table(tmp_path / "r.parquet").to_pylist() == [{"a": 1, "b": "x"}]
    assert pq.read_table(tmp_path / "e.parquet").schema.names == ["a", "b"]


def test_staged_connector_in_a_comparison(tmp_path: Path) -> None:
    registry = new_registry()
    registry.register(PluginKind.CONNECTOR, "fake", FakeStaged, origin="local")
    target_rows = [dict(r) for r in ROWS]
    target_rows[3]["name"] = "changed"
    del target_rows[7]
    config = {
        "version": 1,
        "project": {"name": "staged"},
        "connections": {
            "src": {"type": "fake", "rows": ROWS},
            "tgt": {"type": "fake", "rows": target_rows},
        },
        "suites": [
            {
                "name": "s",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "orders",
                        "source": {"table": "orders"},
                        "target": {"table": "orders"},
                        "keys": ["id"],
                        "comparison": {"strategies": ["row_count", "full_diff"]},
                        "checks": [{"type": "not_null", "columns": ["name"]}],
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry, strict=False)
    out = api.run(loaded, write_reports=False, history=False, registry=registry)
    checks = {c.type: c for c in out.result.suites[0].datasets[0].checks}
    assert checks["compare.row_count"].status.value == "failed"
    assert checks["compare.keys"].unexpected_count == 1
    assert checks["compare.column_mismatch"].status.value == "failed"
    assert [m.column for m in checks["compare.column_mismatch"].column_mismatches] == ["name"]
    assert checks["not_null"].status.value == "passed"


def test_unresolved_relation_is_an_error(tmp_path: Path) -> None:
    con = _connector(tmp_path)
    other = _connector(tmp_path)
    try:
        rel = other.resolve(DatasetSpec(table="orders"))
        with pytest.raises(ConnectorError, match="not resolved"):
            con.get_schema(rel)
    finally:
        con.close()
        other.close()
