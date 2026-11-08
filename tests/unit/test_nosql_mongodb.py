"""The MongoDB connector: native query building (offline) and live reads of every BSON type.

The live tests run against the MongoDB in ``DVF_TEST_MONGODB_URL`` (for example
``mongodb://localhost:27017``) in a database named ``p3_nosql_<random>``, dropped afterwards.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from dvf.config.connections import MongoDBConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.mongodb import MongoDBConnector, MongoDBDatasetOptions
from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, LogicalType
from dvf.packs.nosql.inference import infer_document_schema


def _connector(
    tmp_path: Path, uri: str = "mongodb://127.0.0.1:1", **extra: Any
) -> MongoDBConnector:
    config = MongoDBConnection(
        type="mongodb", uri=uri, database=extra.pop("database", "db"), **extra
    )
    return MongoDBConnector("mongo", config, tmp_path)


def _options(con: MongoDBConnector, **options: Any) -> MongoDBDatasetOptions:
    parsed = con.parse_options(DatasetSpec(table="orders", options=options))
    assert isinstance(parsed, MongoDBDatasetOptions)
    return parsed


def test_native_queries_and_projection(tmp_path: Path) -> None:
    import bson

    con = _connector(tmp_path)
    spec = DatasetSpec(table="orders")
    kind, (query, projection) = con.native_query(
        spec,
        _options(
            con,
            filter={"_id": {"$oid": "65f1c0de0123456789abcdef"}, "n": {"$gt": 1}},
            fields={"id": "order_id", "sku": "lines.sku", "city": "customer.address.city"},
            unwind="lines",
        ),
    )
    assert kind == "find"
    assert query == {"_id": bson.ObjectId("65f1c0de0123456789abcdef"), "n": {"$gt": 1}}
    assert projection == {"_id": 0, "customer": 1, "lines": 1, "order_id": 1}
    _, (_, explicit) = con.native_query(spec, _options(con, projection=["a", "b"]))
    assert explicit == {"a": 1, "b": 1}
    _, (_, everything) = con.native_query(spec, _options(con))
    assert everything is None
    kind, pipeline = con.native_query(
        spec, _options(con, filter={"s": "x"}, projection={"s": 1}), limit=5, sampling="random"
    )
    assert kind == "aggregate"
    assert pipeline == [{"$match": {"s": "x"}}, {"$sample": {"size": 5}}, {"$project": {"s": 1}}]
    kind, pipeline = con.native_query(
        spec,
        _options(con, pipeline=[{"$match": {"d": {"$date": "2024-01-01T00:00:00Z"}}}]),
        limit=3,
    )
    assert kind == "aggregate" and pipeline[-1] == {"$limit": 3}
    assert pipeline[0]["$match"]["d"].year == 2024
    _, sampled = con.native_query(spec, _options(con, pipeline=[]), limit=3, sampling="random")
    assert sampled == [{"$sample": {"size": 3}}]


def test_pipeline_excludes_find_options_and_connection_errors_are_clear(tmp_path: Path) -> None:
    con = _connector(tmp_path, server_selection_timeout_ms=300)
    with pytest.raises(ConnectorError, match="'pipeline' replaces 'filter'"):
        con.parse_options(DatasetSpec(table="t", options={"pipeline": [], "filter": {}}))
    with pytest.raises(ConnectorError, match="Cannot reach MongoDB for connection 'mongo'"):
        con.open()
    report = con.test_connection()
    assert not report.ok and "Cannot reach MongoDB" in report.message
    con.close()


# ====================================================================== live MongoDB

MONGODB_URL = os.environ.get("DVF_TEST_MONGODB_URL")
live = pytest.mark.skipif(not MONGODB_URL, reason="set DVF_TEST_MONGODB_URL to run")


@pytest.fixture(scope="module")
def mongo_db() -> Iterator[str]:
    if not MONGODB_URL:
        pytest.skip("set DVF_TEST_MONGODB_URL to run")
    import bson
    import pymongo

    name = f"p3_nosql_{secrets.token_hex(3)}"
    client: Any = pymongo.MongoClient(MONGODB_URL, uuidRepresentation="standard")
    db = client[name]
    db["types"].insert_many(
        [
            {
                "_id": bson.ObjectId("65f1c0de0123456789abcde0"),
                "i": 1,
                "l": bson.Int64(2**40),
                "d": 1.5,
                "dec": bson.Decimal128("19.990"),
                "s": "a",
                "b": True,
                "dt": datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC),
                "u": uuid.UUID(int=7),
                "bin": bson.Binary(b"\x01\x02", 0),
                "obj": {"x": 1, "y": {"z": "deep"}},
                "arr": [1, 2, 3],
                "mixed": 1,
                "ts": bson.Timestamp(5, 1),
                "re": bson.Regex("^a", "i"),
            },
            {
                "_id": bson.ObjectId("65f1c0de0123456789abcde1"),
                "i": 2,
                "l": bson.Int64(3),
                "d": 2.25,
                "dec": bson.Decimal128("5"),
                "s": None,
                "b": False,
                "dt": datetime(2024, 1, 3, tzinfo=UTC),
                "obj": {"x": 2},
                "arr": [],
                "mixed": "one",
            },
        ]
    )
    db["orders"].insert_many(
        [
            {
                "order_id": n,
                "status": "open" if n % 2 else "shipped",
                "lines": [{"no": k} for k in range(n % 3 + 1)],
            }
            for n in range(1, 11)
        ]
    )
    try:
        yield name
    finally:
        client.drop_database(name)
        client.close()


@pytest.mark.integration
@live
def test_live_bson_types_are_staged_with_native_names(tmp_path: Path, mongo_db: str) -> None:
    assert MONGODB_URL
    con = _connector(tmp_path, MONGODB_URL, database=mongo_db)
    try:
        rel = con.resolve(DatasetSpec(table="types"))
        schema = con.get_schema(rel)
        native = {c.name: c.native_type for c in schema.columns}
        assert native["_id"] == "objectId" and native["i"] == "int" and native["l"] == "long"
        assert native["dec"] == "decimal" and native["dt"] == "date" and native["u"] == "uuid"
        assert native["bin"] == "binData" and native["obj.y.z"] == "string"
        assert native["arr"] == "array" and native["mixed"] in ("int|string", "string|int")
        assert native["ts"] == "timestamp" and native["re"] == "regex"
        types = {c.name: c.type for c in schema.columns}
        assert types["dec"] == CanonicalType.decimal(34, 3)
        assert types["dt"] == CanonicalType.timestamp(3, tz=True)
        assert types["u"].logical is LogicalType.UUID
        assert types["arr"].logical is LogicalType.JSON
        assert types["i"].logical is LogicalType.INTEGER
        assert schema.primary_key == ("_id",)
        assert "canonical text" in (schema.column("mixed").comment or "")  # type: ignore[union-attr]
        rows = con.query(f"SELECT * FROM {rel.sql} ORDER BY i").to_pylist()
        first, second = rows
        assert first["_id"] == "65f1c0de0123456789abcde0"
        assert first["l"] == 2**40 and first["dec"] == Decimal("19.990")
        assert second["dec"] == Decimal("5.000")
        assert first["dt"] == datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
        assert first["u"] == "00000000-0000-0000-0000-000000000007"
        assert first["bin"] == b"\x01\x02" and first["arr"] == "[1,2,3]" and second["arr"] == "[]"
        assert (first["mixed"], second["mixed"]) == ("1", "one")
        assert first["ts"] == "Timestamp(5, 1)" and first["re"] == "/^a/i"
        assert second["obj.y.z"] is None and second["s"] is None
        table = con.query(f"SELECT dec, dt FROM {rel.sql}")
        assert table.schema.field("dt").type == pa.timestamp("us", tz="UTC")
    finally:
        con.close()


@pytest.mark.integration
@live
def test_live_native_reads_and_catalog(tmp_path: Path, mongo_db: str) -> None:
    assert MONGODB_URL
    con = _connector(
        tmp_path,
        MONGODB_URL,
        database=mongo_db,
        read_preference="primaryPreferred",
        query_timeout_s=60,
    )
    try:
        assert con.server_version()
        assert con.test_connection().ok
        assert con.list_datasets("ord*") == ["orders"]
        assert con.exists(con.resolve(DatasetSpec(table="orders")))
        assert not con.exists(con.resolve(DatasetSpec(table="nope")))

        def count(**options: Any) -> int:
            rel = con.resolve(DatasetSpec(table="orders", options=options))
            return int(con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist()[0]["n"])

        assert count() == 10
        assert count(filter={"status": "open"}, read_concern="majority") == 5
        assert count(pipeline=[{"$match": {"order_id": {"$lte": 3}}}], allow_disk_use=True) == 3
        assert count(sort={"order_id": -1}, limit=4, batch_size=2, hint={"_id": 1}) == 4
        assert count(read_preference="secondaryPreferred", max_time_ms=5000) == 10
        assert count(collation={"locale": "en", "strength": 2}, filter={"status": "OPEN"}) == 5
        assert count(unwind={"path": "lines", "keep": ["order_id"]}) == 20
        rel = con.resolve(
            DatasetSpec(
                table="orders",
                options={"sort": {"order_id": -1}, "limit": 1, "fields": {"id": "order_id"}},
            )
        )
        assert con.query(f"SELECT id FROM {rel.sql}").to_pylist() == [{"id": 10}]

        report = infer_document_schema(con, DatasetSpec(table="orders"), 6, sampling="random")
        assert report.documents == 6 and report.sampling == "random"
        lines = report.field("lines[].no")
        assert lines is not None and lines.types == {"int": lines.present}
        status = report.field("status")
        assert status is not None and status.presence_pct == 100
    finally:
        con.close()
