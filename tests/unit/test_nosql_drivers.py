"""The beta NoSQL connectors (DynamoDB, Cassandra, Cosmos DB) against fake drivers.

DynamoDB uses a real boto3 client with botocore's ``Stubber``, which validates every request and
response against the AWS API model. Cassandra and Cosmos DB use small fake driver modules
installed in ``sys.modules``, since their drivers are optional extras.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
import types
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dvf import api
from dvf.config.models import DatasetSpec
from dvf.connectors.cassandra import CassandraConnection, CassandraConnector
from dvf.connectors.cosmos import CosmosConnection, CosmosConnector
from dvf.connectors.dynamodb import DynamoDBConnection, DynamoDBConnector
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import LogicalType
from dvf.plugins.registry import PluginKind, new_registry

# ====================================================================== DynamoDB


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[DynamoDBConnector, Any]]:
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        "dynamodb",
        region_name="eu-west-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )
    stubber = Stubber(client)
    config = DynamoDBConnection(type="dynamodb", region="eu-west-1", page_size=2)
    connector = DynamoDBConnector("ddb", config, Path())
    monkeypatch.setattr(connector, "make_client", lambda: client)
    with stubber:
        yield connector, stubber
    connector.close()


KEYS = {
    "Table": {
        "TableName": "orders",
        "KeySchema": [
            {"AttributeName": "sk", "KeyType": "RANGE"},
            {"AttributeName": "pk", "KeyType": "HASH"},
        ],
    }
}


def _order(pk: str, sk: int, lines: int, total: str) -> dict[str, Any]:
    return {
        "pk": {"S": pk},
        "sk": {"N": str(sk)},
        "total": {"N": total},
        "tags": {"SS": ["b", "a"]},
        "lines": {"L": [{"M": {"no": {"N": str(n)}, "qty": {"N": "2"}}} for n in range(lines)]},
    }


def test_dynamodb_query_pages_types_and_keys(
    stubbed: tuple[DynamoDBConnector, Any], tmp_path: Path
) -> None:
    connector, stubber = stubbed
    connector.temp_root = str(tmp_path)
    params = {
        "TableName": "orders",
        "KeyConditionExpression": "pk = :pk",
        "FilterExpression": "#s <> :closed",
        "ExpressionAttributeValues": {":pk": {"S": "c1"}, ":closed": {"S": "closed"}},
        "ExpressionAttributeNames": {"#s": "status"},
        "ProjectionExpression": "pk, sk, total, tags, #s, lines",
        "ConsistentRead": True,
        "Limit": 2,
    }
    first = [_order("c1", 1, 2, "10.5"), _order("c1", 2, 1, "7")]
    stubber.add_response("describe_table", KEYS, {"TableName": "orders"})
    stubber.add_response(
        "query",
        {"Items": first, "LastEvaluatedKey": {"pk": {"S": "c1"}, "sk": {"N": "2"}}},
        params,
    )
    stubber.add_response(
        "query",
        {"Items": [_order("c1", 3, 0, "1.25")]},
        {**params, "ExclusiveStartKey": {"pk": {"S": "c1"}, "sk": {"N": "2"}}},
    )
    spec = DatasetSpec(
        table="orders",
        options={
            "key_condition": "pk = :pk",
            "filter": "#s <> :closed",
            "values": {":pk": "c1", ":closed": "closed"},
            "names": {"#s": "status"},
            "projection": "pk, sk, total, tags, #s, lines",
            "consistent_read": True,
        },
    )
    rel = connector.resolve(spec)
    schema = connector.get_schema(rel)
    native = {c.name: c.native_type for c in schema.columns}
    assert native == {"pk": "S", "sk": "N", "total": "N", "tags": "SS", "lines": "L"}
    assert schema.primary_key == ("pk", "sk")
    types_ = {c.name: c.type for c in schema.columns}
    assert types_["sk"].logical is LogicalType.INTEGER
    assert types_["total"].logical is LogicalType.DECIMAL and types_["total"].scale == 2
    rows = connector.query(f"SELECT * FROM {rel.sql} ORDER BY sk").to_pylist()
    assert [r["total"] for r in rows] == [Decimal("10.50"), Decimal("7.00"), Decimal("1.25")]
    assert rows[0]["tags"] == '["a","b"]'
    stubber.assert_no_pending_responses()


def test_dynamodb_scan_limit_unwind_catalog(
    stubbed: tuple[DynamoDBConnector, Any], tmp_path: Path
) -> None:
    connector, stubber = stubbed
    connector.temp_root = str(tmp_path)
    stubber.add_response("describe_table", KEYS, {"TableName": "orders"})
    stubber.add_response(
        "scan",
        {
            "Items": [_order("c1", 1, 2, "1"), _order("c2", 1, 1, "2")],
            "LastEvaluatedKey": {"pk": {"S": "c2"}, "sk": {"N": "1"}},
        },
        {"TableName": "orders", "IndexName": "by_status", "Limit": 2},
    )
    rel = connector.resolve(
        DatasetSpec(table="orders", options={"index": "by_status", "limit": 2, "unwind": "lines"})
    )
    schema = connector.get_schema(rel)
    assert [c.name for c in schema.columns] == ["pk", "sk", "lines_index", "no", "qty"]
    assert schema.primary_key == ("pk", "sk", "lines_index")
    assert connector.staged_rows(rel) == 3
    stubber.add_response("describe_table", KEYS, {"TableName": "orders"})
    stubber.add_client_error(
        "describe_table", service_error_code="ResourceNotFoundException", http_status_code=400
    )
    stubber.add_response("list_tables", {"TableNames": ["orders", "customers"]}, {})
    assert connector.dataset_exists(DatasetSpec(table="orders")) is True
    assert connector.dataset_exists(DatasetSpec(table="gone")) is False
    assert connector.list_datasets("c*") == ["customers"]
    stubber.assert_no_pending_responses()


def test_dynamodb_client_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    seen: dict[str, Any] = {}

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            seen["session"] = kwargs

        def client(self, name: str, **kwargs: Any) -> str:
            seen["client"] = (name, kwargs)
            return "client"

    monkeypatch.setattr(boto3.session, "Session", Session)
    config = DynamoDBConnection(
        type="dynamodb",
        region="eu-west-1",
        endpoint_url="http://localhost:8000",
        profile="dev",
        aws_access_key_id="synthetic-id",
        aws_secret_access_key="synthetic-secret",
        aws_session_token="synthetic-token",
    )
    assert DynamoDBConnector("d", config, Path()).make_client() == "client"
    assert seen["session"] == {
        "profile_name": "dev",
        "aws_access_key_id": "synthetic-id",
        "aws_secret_access_key": "synthetic-secret",
        "aws_session_token": "synthetic-token",
    }
    assert seen["client"] == (
        "dynamodb",
        {"region_name": "eu-west-1", "endpoint_url": "http://localhost:8000"},
    )


# ====================================================================== Cassandra


class _Column:
    def __init__(self, name: str, cql_type: str) -> None:
        self.name = name
        self.cql_type = cql_type


class _Table:
    def __init__(self) -> None:
        self.columns = {
            "customer_id": _Column("customer_id", "int"),
            "order_id": _Column("order_id", "timeuuid"),
            "placed": _Column("placed", "timestamp"),
            "total": _Column("total", "decimal"),
            "items": _Column("items", "list<frozen<item>>"),
            "day": _Column("day", "date"),
        }
        self.partition_key = [self.columns["customer_id"]]
        self.clustering_key = [self.columns["order_id"]]


class Date:
    """Stands in for ``cassandra.util.Date``."""

    def __init__(self, value: date) -> None:
        self._value = value

    def date(self) -> date:
        return self._value


def _cassandra_rows() -> list[dict[str, Any]]:
    import uuid

    return [
        {
            "customer_id": 7,
            "order_id": uuid.UUID(int=n),
            "placed": datetime(2024, 5, n, 9, 30),
            "total": Decimal("12.50") * n,
            "items": [{"sku": "A", "qty": n}, {"sku": "B", "qty": 1}],
            "day": Date(date(2024, 5, n)),
        }
        for n in (1, 2)
    ]


@pytest.fixture
def fake_cassandra(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"statements": [], "shutdown": 0}

    class ConsistencyLevel:
        name_to_value: ClassVar[dict[str, int]] = {"LOCAL_QUORUM": 6, "ONE": 1, "LOCAL_ONE": 10}

    class SimpleStatement:
        def __init__(self, query: str, **kwargs: Any) -> None:
            self.query_string = query
            self.kwargs = kwargs

    class Result:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows

        def __iter__(self) -> Iterator[dict[str, Any]]:
            return iter(self.rows)

        def one(self) -> dict[str, Any] | None:
            return self.rows[0] if self.rows else None

    class Session:
        def execute(self, statement: Any) -> Result:
            if isinstance(statement, str):
                return Result([{"release_version": "5.0.2"}])
            state["statements"].append(statement)
            if "WHERE broken" in statement.query_string:
                raise RuntimeError("InvalidRequest: Undefined column name broken")
            return Result(_cassandra_rows())

    class Keyspace:
        tables: ClassVar[dict[str, _Table]] = {"orders": _Table()}

    class Metadata:
        keyspaces: ClassVar[dict[str, Any]] = {"shop": Keyspace()}

    class Cluster:
        def __init__(self, **kwargs: Any) -> None:
            state["cluster"] = kwargs
            self.metadata = Metadata()

        def connect(self) -> Session:
            return Session()

        def shutdown(self) -> None:
            state["shutdown"] += 1

    class ExecutionProfile:
        def __init__(self, **kwargs: Any) -> None:
            state["profile"] = kwargs

    def module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("cassandra", ConsistencyLevel=ConsistencyLevel)
    module(
        "cassandra.cluster",
        Cluster=Cluster,
        ExecutionProfile=ExecutionProfile,
        EXEC_PROFILE_DEFAULT="default",
    )
    module("cassandra.query", SimpleStatement=SimpleStatement, dict_factory="dict_factory")
    module("cassandra.auth", PlainTextAuthProvider=lambda **kw: ("auth", kw))
    module("cassandra.policies", DCAwareRoundRobinPolicy=lambda **kw: ("dc", kw))
    return state


def test_cassandra_reads_flattens_and_types(fake_cassandra: dict[str, Any], tmp_path: Path) -> None:
    config = CassandraConnection(
        type="cassandra",
        contact_points=["10.0.0.1", "10.0.0.2"],
        keyspace="shop",
        username="reader",
        password="synthetic",
        local_datacenter="dc1",
        query_timeout_s=30,
        fetch_size=1,
    )
    connector = CassandraConnector("cass", config, tmp_path)
    try:
        assert connector.server_version() == "5.0.2"
        assert fake_cassandra["cluster"]["contact_points"] == ["10.0.0.1", "10.0.0.2"]
        assert fake_cassandra["cluster"]["auth_provider"][1]["username"] == "reader"
        profile = fake_cassandra["profile"]
        assert profile["consistency_level"] == 6 and profile["request_timeout"] == 30
        assert profile["load_balancing_policy"] == ("dc", {"local_dc": "dc1"})
        spec = DatasetSpec(
            table="orders",
            options={
                "where": "customer_id = 7",
                "columns": ["customer_id", "order_id", "placed", "total", "items", "day"],
                "consistency_level": "ONE",
                "limit": 5,
                "arrays": {"items": "count"},
            },
        )
        rel = connector.resolve(spec)
        schema = connector.get_schema(rel)
        statement = fake_cassandra["statements"][-1]
        assert statement.query_string == (
            "SELECT customer_id, order_id, placed, total, items, day FROM shop.orders "
            "WHERE customer_id = 7 LIMIT 5"
        )
        assert statement.kwargs == {"fetch_size": 1, "consistency_level": 1}
        native = {c.name: c.native_type for c in schema.columns}
        assert native["order_id"] == "timeuuid" and native["items"] == "list<frozen<item>>"
        canonical = {c.name: c.type for c in schema.columns}
        assert canonical["customer_id"].bits == 32
        assert canonical["order_id"].logical is LogicalType.UUID
        assert canonical["day"].logical is LogicalType.DATE
        assert schema.primary_key == ("customer_id", "order_id")
        rows = connector.query(f"SELECT * FROM {rel.sql} ORDER BY total").to_pylist()
        assert rows[0]["items"] == 2 and rows[1]["total"] == Decimal("25.00")
        assert rows[0]["placed"] == datetime(2024, 5, 1, 9, 30, tzinfo=UTC)
        assert rows[0]["day"] == date(2024, 5, 1)
        lines = connector.resolve(
            DatasetSpec(table="shop.orders", options={"unwind": "items", "allow_filtering": True})
        )
        line_schema = connector.get_schema(lines)
        assert [c.name for c in line_schema.columns] == [
            "customer_id",
            "order_id",
            "items_index",
            "sku",
            "qty",
        ]
        assert fake_cassandra["statements"][-1].query_string.endswith("ALLOW FILTERING")
        assert connector.staged_rows(lines) == 4
        assert connector.list_datasets("ord*") == ["orders"]
        assert connector.exists(connector.resolve(DatasetSpec(table="shop.orders")))
        assert not connector.dataset_exists(DatasetSpec(table="shop.nope"))
        broken = connector.resolve(DatasetSpec(table="orders", options={"where": "broken = 1"}))
        with pytest.raises(ConnectorError, match="CQL query failed"):
            connector.get_schema(broken)
    finally:
        connector.close()
    assert fake_cassandra["shutdown"] == 1


def test_cassandra_needs_a_keyspace_and_the_driver(tmp_path: Path) -> None:
    connector = CassandraConnector("c", CassandraConnection(type="cassandra"), tmp_path)
    with pytest.raises(ConnectorError, match="has no keyspace"):
        connector.table_name(DatasetSpec(table="orders"))
    if "cassandra" not in sys.modules:
        with pytest.raises(DependencyError, match="cassandra-driver"):
            connector.open()


def test_cassandra_in_a_comparison(fake_cassandra: dict[str, Any], tmp_path: Path) -> None:
    import duckdb

    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE order_items (customer_id INT, order_id UUID, sku VARCHAR, qty INT)")
    con.execute(
        "INSERT INTO order_items VALUES "
        "(7, '00000000-0000-0000-0000-000000000001', 'A', 1), "
        "(7, '00000000-0000-0000-0000-000000000001', 'B', 1), "
        "(7, '00000000-0000-0000-0000-000000000002', 'A', 2), "
        "(7, '00000000-0000-0000-0000-000000000002', 'B', 3)"
    )
    con.close()
    registry = new_registry()
    assert registry.get(PluginKind.CONNECTOR, "cassandra").status == "beta"
    config = {
        "version": 1,
        "project": {"name": "cass"},
        "connections": {
            "rel": {"type": "duckdb", "database": str(db)},
            "cass": {"type": "cassandra", "keyspace": "shop"},
        },
        "suites": [
            {
                "name": "s",
                "source": "rel",
                "target": "cass",
                "tables": [
                    {
                        "name": "items",
                        "source": {"table": "order_items"},
                        "target": {
                            "table": "orders",
                            "options": {
                                "unwind": "items",
                                "fields": {
                                    "customer_id": "customer_id",
                                    "order_id": "order_id",
                                    "sku": "items.sku",
                                    "qty": "items.qty",
                                },
                            },
                        },
                        "columns": {"exclude": ["items_index"]},
                        "comparison": {"strategies": ["nested"], "group_by": ["order_id"]},
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry)
    out = api.run(loaded, write_reports=False, history=False, registry=registry)
    checks = {c.type: c for c in out.result.suites[0].datasets[0].checks}
    nested = checks["compare.nested"]
    assert nested.status.value == "failed" and nested.unexpected_count == 1
    assert nested.samples is not None
    assert nested.samples.rows[0][:2] == ["00000000-0000-0000-0000-000000000002", "totals differ"]
    assert "sum(qty) 5 vs 3" in nested.samples.rows[0][4]


# ====================================================================== Cosmos DB


class _NotFoundError(Exception):
    status_code = 404


@pytest.fixture
def fake_cosmos(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"queries": []}
    items = [
        {
            "id": f"o{n}",
            "customerId": f"c{n % 2}",
            "total": 10.5 * n,
            "lines": [{"sku": "A"}] * n,
            "_rid": "r",
            "_etag": "e",
            "_ts": 1700000000,
        }
        for n in range(1, 6)
    ]

    class Paged:
        def __init__(self, size: int) -> None:
            self.size = size

        def by_page(self) -> Iterator[Iterator[dict[str, Any]]]:
            for start in range(0, len(items), self.size):
                yield iter([dict(i) for i in items[start : start + self.size]])

    class Container:
        def __init__(self, name: str) -> None:
            self.name = name

        def read(self) -> dict[str, Any]:
            if self.name != "orders":
                raise _NotFoundError("Resource Not Found")
            return {"id": "orders", "partitionKey": {"paths": ["/customerId"], "kind": "Hash"}}

        def query_items(self, **kwargs: Any) -> Paged:
            state["queries"].append(kwargs)
            return Paged(kwargs["max_item_count"])

    class Database:
        def get_container_client(self, name: str) -> Container:
            return Container(name)

        def list_containers(self) -> list[dict[str, Any]]:
            return [{"id": "orders"}, {"id": "customers"}]

    class CosmosClient:
        def __init__(self, url: str, credential: Any, **kwargs: Any) -> None:
            state["client"] = (url, credential, kwargs)

        def get_database_client(self, name: str) -> Database:
            state["database"] = name
            return Database()

    cosmos = types.ModuleType("azure.cosmos")
    cosmos.CosmosClient = CosmosClient  # type: ignore[attr-defined]
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda: "entra-credential"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.cosmos", cosmos)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    return state


def test_cosmos_queries_pages_and_drops_system_properties(
    fake_cosmos: dict[str, Any], tmp_path: Path
) -> None:
    config = CosmosConnection(
        type="cosmos",
        endpoint="https://example.documents.azure.com:443/",
        key="synthetic-key",
        database="shop",
        consistency_level="Session",
        preferred_locations=["West Europe"],
        page_size=2,
    )
    connector = CosmosConnector("cosmos", config, tmp_path)
    try:
        spec = DatasetSpec(
            table="orders",
            options={
                "query": "SELECT * FROM c WHERE c.total > @min",
                "parameters": {"min": 1},
                "arrays": {"lines": "count"},
            },
        )
        rel = connector.resolve(spec)
        schema = connector.get_schema(rel)
        _, credential, kwargs = fake_cosmos["client"]
        assert credential == "synthetic-key"
        assert kwargs == {"consistency_level": "Session", "preferred_locations": ["West Europe"]}
        assert fake_cosmos["queries"][-1] == {
            "query": "SELECT * FROM c WHERE c.total > @min",
            "max_item_count": 2,
            "parameters": [{"name": "@min", "value": 1}],
            "enable_cross_partition_query": True,
        }
        assert [c.name for c in schema.columns] == ["id", "customerId", "total", "lines"]
        assert {c.native_type for c in schema.columns} == {"string", "number", "array (count)"}
        assert schema.primary_key == ("customerId", "id")
        assert connector.staged_rows(rel) == 5
        limited = connector.resolve(
            DatasetSpec(
                table="orders",
                options={"partition_key": "c1", "limit": 3, "include_system_properties": True},
            )
        )
        names = [c.name for c in connector.get_schema(limited).columns]
        assert "_rid" in names and connector.staged_rows(limited) == 3
        assert fake_cosmos["queries"][-1]["partition_key"] == "c1"
        assert connector.list_datasets("o*") == ["orders"]
        assert not connector.dataset_exists(DatasetSpec(table="missing"))
    finally:
        connector.close()


def test_cosmos_entra_credential_and_validation(
    fake_cosmos: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="set 'key'"):
        CosmosConnection(type="cosmos", endpoint="https://x", database="d")
    config = CosmosConnection(
        type="cosmos", endpoint="https://x", database="d", credential="default"
    )
    connector = CosmosConnector("c", config, tmp_path)
    connector.open()
    assert fake_cosmos["client"][1] == "entra-credential"
    connector.close()


# ====================================================================== DynamoDB Local (live)

DYNAMODB_URL = os.environ.get("DVF_TEST_DYNAMODB_URL")


@pytest.fixture(scope="module")
def dynamodb_table() -> Iterator[str]:
    """A table in DynamoDB Local (``DVF_TEST_DYNAMODB_URL``, for example
    ``http://localhost:8000``), removed afterwards."""
    if not DYNAMODB_URL:
        pytest.skip("set DVF_TEST_DYNAMODB_URL to run")
    import boto3

    client = boto3.client(
        "dynamodb",
        endpoint_url=DYNAMODB_URL,
        region_name="us-east-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )
    name = f"p3_nosql_{secrets.token_hex(3)}"
    try:
        _create_orders_table(client, name)
        yield name
    finally:
        with contextlib.suppress(Exception):
            client.delete_table(TableName=name)


def _create_orders_table(client: Any, name: str) -> None:
    client.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "customer", "KeyType": "HASH"},
            {"AttributeName": "order_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "customer", "AttributeType": "S"},
            {"AttributeName": "order_id", "AttributeType": "N"},
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    for order_id in range(1, 7):
        lines = [
            {
                "M": {
                    "line_no": {"N": str(n)},
                    "qty": {"N": str(n + order_id % 2)},
                    "price": {"N": f"{n}.25"},
                }
            }
            for n in range(1, order_id % 3 + 2)
        ]
        client.put_item(
            TableName=name,
            Item={
                "customer": {"S": f"c{order_id % 2}"},
                "order_id": {"N": str(order_id)},
                "status": {"S": "open" if order_id % 2 else "shipped"},
                "tags": {"SS": ["new", f"t{order_id}"]},
                "lines": {"L": lines},
            },
        )


@pytest.mark.integration
def test_dynamodb_local_reads_and_nested_reconciliation(
    dynamodb_table: str, tmp_path: Path
) -> None:
    import duckdb

    assert DYNAMODB_URL
    connection = {
        "type": "dynamodb",
        "endpoint_url": DYNAMODB_URL,
        "region": "us-east-1",
        "aws_access_key_id": "synthetic",
        "aws_secret_access_key": "synthetic",
        "page_size": 2,
    }
    connector = DynamoDBConnector("ddb", DynamoDBConnection(**connection), tmp_path)
    try:
        assert dynamodb_table in connector.list_datasets("p3_nosql_*")
        assert connector.dataset_exists(DatasetSpec(table=dynamodb_table))
        assert not connector.dataset_exists(DatasetSpec(table=dynamodb_table + "_x"))
        rel = connector.resolve(DatasetSpec(table=dynamodb_table))
        schema = connector.get_schema(rel)
        assert schema.primary_key == ("customer", "order_id")
        assert {c.name: c.native_type for c in schema.columns}["tags"] == "SS"
        assert connector.staged_rows(rel) == 6
        query = connector.resolve(
            DatasetSpec(
                table=dynamodb_table,
                options={
                    "key_condition": "customer = :c AND order_id > :min",
                    "filter": "#s = :s",
                    "values": {":c": "c1", ":min": 1, ":s": "open"},
                    "names": {"#s": "status"},
                    "consistent_read": True,
                },
            )
        )
        assert connector.staged_rows(query) == 2  # orders 3 and 5
    finally:
        connector.close()

    db = tmp_path / "rel.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE lines (order_id INT, line_no INT, qty INT, price DECIMAL(8, 2))")
    for order_id in range(1, 7):
        for n in range(1, order_id % 3 + 2):
            qty = n + order_id % 2 + (1 if (order_id, n) == (4, 1) else 0)  # seeded defect
            con.execute(
                "INSERT INTO lines VALUES (?, ?, ?, ?)", [order_id, n, qty, Decimal(f"{n}.25")]
            )
    con.close()
    config = {
        "version": 1,
        "project": {"name": "ddb"},
        "connections": {"rel": {"type": "duckdb", "database": str(db)}, "ddb": connection},
        "suites": [
            {
                "name": "s",
                "source": "rel",
                "target": "ddb",
                "tables": [
                    {
                        "name": "lines",
                        "source": {"table": "lines"},
                        "target": {
                            "table": dynamodb_table,
                            "options": {"unwind": {"path": "lines", "keep": ["order_id"]}},
                        },
                        "keys": ["order_id", "line_no"],
                        "columns": {"exclude": ["lines_index"]},
                        "comparison": {"strategies": ["nested"], "group_by": ["order_id"]},
                    }
                ],
            }
        ],
    }
    registry = new_registry()
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry)
    out = api.run(loaded, write_reports=False, history=False, registry=registry)
    checks = {c.type: c for c in out.result.suites[0].datasets[0].checks}
    nested = checks["compare.nested"]
    assert (nested.unexpected_count, nested.total_count) == (1, 6)
    assert nested.samples is not None and nested.samples.rows[0][:2] == ["4", "totals differ"]
    mismatch = checks["compare.column_mismatch"]
    assert [(m.column, m.mismatch_count) for m in mismatch.column_mismatches] == [("qty", 1)]
