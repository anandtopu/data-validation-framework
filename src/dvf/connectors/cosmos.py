"""Azure Cosmos DB for NoSQL connector: containers read with ``azure-cosmos``, flattened, staged.

A dataset names a container with ``table``. ``options.query`` is a Cosmos DB SQL query (default
``SELECT * FROM c``) with optional ``options.parameters``; ``options.partition_key`` restricts
the read to one logical partition. Cosmos DB system properties (``_rid``, ``_self``, ``_etag``,
``_attachments``, ``_ts``) are dropped unless ``options.include_system_properties`` is set.
Values keep JSON's type names (``string``, ``number``, ``boolean``, ``null``, ``object``,
``array``) in schema checks.

Beta: verified with a fake SDK, not yet against Cosmos DB or its emulator. Install the SDK with
``pip install 'dvf-framework[cosmos]'`` (and ``azure-identity`` for ``credential: default``).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping
from fnmatch import fnmatchcase
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveInt, SecretStr, model_validator

from dvf.config.connections import ConnectionConfig
from dvf.config.models import DatasetSpec
from dvf.core.errors import ConnectorError, DependencyError
from dvf.packs.nosql.base import DocumentConnector, Sampling
from dvf.packs.nosql.options import DocumentOptions
from dvf.packs.nosql.values import Classified, ValueKind, classify_python

SYSTEM_PROPERTIES = frozenset({"_rid", "_self", "_etag", "_attachments", "_ts", "_lsn"})


class CosmosConnection(ConnectionConfig):
    """Azure Cosmos DB for NoSQL (formerly the SQL API)."""

    endpoint: str
    """Account URI, for example ``https://myaccount.documents.azure.com:443/``."""
    database: str
    """ID of the database holding the containers that datasets name."""
    key: SecretStr | None = None
    """Account key (or a resource token). Use a secret reference."""
    credential: Literal["key", "default"] = "key"
    """``key`` uses ``key``; ``default`` uses Microsoft Entra ID through azure-identity's
    ``DefaultAzureCredential``."""
    consistency_level: (
        Literal["Strong", "BoundedStaleness", "Session", "ConsistentPrefix", "Eventual"] | None
    ) = None
    """Read consistency; the account default when omitted (it can only be relaxed)."""
    preferred_locations: list[str] = Field(default_factory=list)
    """Regions to read from, in order of preference."""
    page_size: PositiveInt = Field(default=1000, le=100_000)
    """Items per page; datasets may override it with ``options.batch_size``."""

    @model_validator(mode="after")
    def _key_given(self) -> CosmosConnection:
        if self.credential == "key" and self.key is None:
            raise ValueError("set 'key' (a secret reference), or use 'credential: default'")
        return self


class CosmosDatasetOptions(DocumentOptions):
    """Dataset options of the ``cosmos`` connector."""

    query: str = "SELECT * FROM c"
    """Cosmos DB SQL query."""
    parameters: dict[str, Any] = Field(default_factory=dict)
    """Query parameters by name (with or without the leading ``@``)."""
    partition_key: Any = None
    """Read only this logical partition (otherwise the query runs across partitions)."""
    include_system_properties: bool = False
    """Keep ``_rid``, ``_self``, ``_etag``, ``_attachments``, and ``_ts``."""


def classify_cosmos(value: Any) -> Classified:
    """Classify JSON values with JSON's type names."""
    cell = classify_python(value)
    native = {
        ValueKind.NULL: "null",
        ValueKind.BOOL: "boolean",
        ValueKind.INT: "number",
        ValueKind.FLOAT: "number",
        ValueKind.DECIMAL: "number",
        ValueKind.STRING: "string",
        ValueKind.OBJECT: "object",
        ValueKind.ARRAY: "array",
    }.get(cell.kind, cell.native)
    return Classified(cell.kind, native, cell.value)


def _import(module: str, package: str = "azure-cosmos") -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise DependencyError(package, "cosmos", "The Cosmos DB connector") from exc


class CosmosConnector(DocumentConnector):
    """Azure Cosmos DB containers, flattened and staged for SQL comparison."""

    Config: ClassVar[type[CosmosConnection]] = CosmosConnection
    options_model = CosmosDatasetOptions
    system = "Cosmos DB"
    default_keep = ("id",)

    def __init__(self, name: str, config: Any, base_dir: Any) -> None:
        super().__init__(name, config, base_dir)
        self._client: Any = None
        self._database: Any = None
        self._pk_cache: dict[str, tuple[str, ...]] = {}

    @property
    def cosmos_config(self) -> CosmosConnection:
        assert isinstance(self.config, CosmosConnection)
        return self.config

    # ------------------------------------------------------------------ lifecycle
    def open_source(self) -> None:
        cosmos = _import("azure.cosmos")
        cfg = self.cosmos_config
        if cfg.credential == "default":
            identity = _import("azure.identity", "azure-identity")
            credential: Any = identity.DefaultAzureCredential()
        else:
            assert cfg.key is not None
            credential = cfg.key.get_secret_value()
        kwargs: dict[str, Any] = dict(cfg.options)
        if cfg.consistency_level:
            kwargs["consistency_level"] = cfg.consistency_level
        if cfg.preferred_locations:
            kwargs["preferred_locations"] = list(cfg.preferred_locations)
        try:
            self._client = cosmos.CosmosClient(cfg.endpoint, credential=credential, **kwargs)
            self._database = self._client.get_database_client(cfg.database)
        except Exception as exc:
            raise ConnectorError(
                f"Cannot connect to Cosmos DB for connection '{self.name}': {exc}",
                hint="Check the endpoint, the key or Entra ID credentials, and the database name.",
            ) from exc

    def close_source(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()
        self._client = None
        self._database = None

    def classify(self, value: Any) -> Classified:
        return classify_cosmos(value)

    def _container(self, name: str) -> Any:
        self.open()
        return self._database.get_container_client(name)

    # ------------------------------------------------------------------ datasets
    def _properties(self, name: str) -> dict[str, Any] | None:
        try:
            properties: dict[str, Any] = self._container(name).read()
        except Exception as exc:
            if "NotFound" in type(exc).__name__ or getattr(exc, "status_code", None) == 404:
                return None
            raise ConnectorError(f"Cannot read Cosmos DB container '{name}': {exc}") from exc
        return properties

    def dataset_exists(self, spec: DatasetSpec) -> bool | None:
        return self._properties(self.collection(spec)) is not None

    def list_datasets(self, pattern: str) -> list[str]:
        self.open()
        names = [str(c["id"]) for c in self._database.list_containers()]
        return sorted(n for n in names if fnmatchcase(n, pattern))

    def partition_key_paths(self, container: str) -> tuple[str, ...]:
        """The container's partition key paths as dot paths (``/customer/id`` is
        ``customer.id``)."""
        if container not in self._pk_cache:
            properties = self._properties(container) or {}
            paths = properties.get("partitionKey", {}).get("paths", [])
            self._pk_cache[container] = tuple(p.strip("/").replace("/", ".") for p in paths)
        return self._pk_cache[container]

    def default_primary_key(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        keys = [k for k in self.partition_key_paths(self.collection(spec)) if k != "id"]
        return (*keys, "id")

    # ------------------------------------------------------------------ reads
    def query_arguments(self, options: CosmosDatasetOptions, page_size: int) -> dict[str, Any]:
        """Keyword arguments of ``ContainerProxy.query_items`` for a dataset."""
        arguments: dict[str, Any] = {"query": options.query, "max_item_count": page_size}
        if options.parameters:
            arguments["parameters"] = [
                {"name": name if name.startswith("@") else f"@{name}", "value": value}
                for name, value in options.parameters.items()
            ]
        if options.partition_key is not None:
            arguments["partition_key"] = options.partition_key
        else:
            arguments["enable_cross_partition_query"] = True
        return arguments

    def iter_documents(
        self,
        spec: DatasetSpec,
        options: DocumentOptions,
        *,
        limit: int | None = None,
        sampling: Sampling | None = None,
    ) -> Iterator[list[Mapping[str, Any]]]:
        assert isinstance(options, CosmosDatasetOptions)
        container = self._container(self.collection(spec))
        page_size = options.batch_size or self.cosmos_config.page_size
        if limit:
            page_size = min(page_size, limit)
        items = container.query_items(**self.query_arguments(options, page_size))
        seen = 0
        for page in items.by_page():
            documents: list[Mapping[str, Any]] = []
            for document in page:
                if not options.include_system_properties:
                    document = {k: v for k, v in document.items() if k not in SYSTEM_PROPERTIES}
                documents.append(document)
                seen += 1
                if limit is not None and seen >= limit:
                    break
            if documents:
                yield documents
            if limit is not None and seen >= limit:
                return


__all__ = ["CosmosConnection", "CosmosConnector", "CosmosDatasetOptions", "classify_cosmos"]
