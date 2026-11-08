"""NoSQL pack: MongoDB, Cassandra, DynamoDB, and Cosmos DB connectors, document flattening,
schema inference, the ``document_schema`` check, and the ``nested`` comparison strategy.

``register`` is called by :func:`dvf.packs.register_packs`. Drivers (``pymongo``,
``cassandra-driver``, ``boto3``, ``azure-cosmos``) are imported only when a connection opens.
"""

from __future__ import annotations

from dvf.plugins.registry import PluginKind, Registry


def register(registry: Registry) -> None:
    """Attach this pack's implementations and register its new components."""
    from dvf.connectors.cassandra import CassandraConnection, CassandraConnector
    from dvf.connectors.cosmos import CosmosConnection, CosmosConnector
    from dvf.connectors.dynamodb import DynamoDBConnection, DynamoDBConnector
    from dvf.connectors.mongodb import MongoDBConnector
    from dvf.packs import attach
    from dvf.packs.nosql.nested import NestedComparator
    from dvf.packs.nosql.schema_check import DocumentSchema, DocumentSchemaCheck

    # Verified against MongoDB 7 by tests/e2e/test_mongodb_to_postgres.py and the integration
    # tests in tests/unit/test_nosql_mongodb.py.
    attach(registry, PluginKind.CONNECTOR, "mongodb", MongoDBConnector, maturity="stable")
    for name, implementation, model, description, extra in (
        (
            "cassandra",
            CassandraConnector,
            CassandraConnection,
            "Apache Cassandra and compatible systems (ScyllaDB, DataStax Astra)",
            "cassandra",
        ),
        ("dynamodb", DynamoDBConnector, DynamoDBConnection, "Amazon DynamoDB", "dynamodb"),
        ("cosmos", CosmosConnector, CosmosConnection, "Azure Cosmos DB for NoSQL", "cosmos"),
    ):
        registry.register(
            PluginKind.CONNECTOR,
            name,
            implementation,
            description=description,
            config_model=model,
            origin="builtin",
            extra=extra,
            maturity="beta",  # unit-tested with fake drivers, not yet against a live service
        )
    registry.register(
        PluginKind.COMPARATOR,
        "nested",
        NestedComparator,
        description="Compare child rows per parent key: counts, totals, and an order-independent "
        "hash of canonical child rows (embedded arrays vs child tables)",
        origin="builtin",
    )
    registry.register(
        PluginKind.CHECK,
        "document_schema",
        DocumentSchema,
        description="Inferred document schema: expected fields, types, presence, and "
        "heterogeneity (SCH-13)",
        config_model=DocumentSchemaCheck,
        origin="builtin",
    )
