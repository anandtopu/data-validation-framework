"""Amazon DynamoDB connector: tables read with ``boto3`` (Query or Scan), flattened, and staged.

A dataset names a table with ``table``. With ``options.key_condition`` dvf runs a Query (on the
table or on ``options.index``); otherwise it runs a Scan. ``options.filter``, ``options.values``,
and ``options.names`` are DynamoDB expressions, exactly as in the AWS API. Items keep DynamoDB's
type names (``S``, ``N``, ``B``, ``BOOL``, ``NULL``, ``M``, ``L``, ``SS``, ``NS``, ``BS``) in
schema checks. Numbers that are whole and fit in 64 bits are integers; others are decimals.

Beta: verified with botocore's stubbed responses, not yet against DynamoDB or DynamoDB Local.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any, ClassVar

from pydantic import Field, PositiveInt, SecretStr

from dvf.config.connections import ConnectionConfig
from dvf.config.models import DatasetSpec
from dvf.core.errors import ConnectorError, DependencyError
from dvf.packs.nosql.base import DocumentConnector, Sampling
from dvf.packs.nosql.options import DocumentOptions
from dvf.packs.nosql.values import (
    INT64_MAX,
    INT64_MIN,
    Classified,
    ValueKind,
    canonical_json,
    classify_python,
)


class DynamoDBConnection(ConnectionConfig):
    """Amazon DynamoDB (or DynamoDB Local through ``endpoint_url``)."""

    region: str | None = None
    """AWS region, for example ``eu-west-1``; the AWS configuration's default when omitted."""
    endpoint_url: str | None = None
    """Custom endpoint, for example ``http://localhost:8000`` for DynamoDB Local."""
    profile: str | None = None
    """Named profile from the AWS configuration files."""
    aws_access_key_id: SecretStr | None = None
    """Access key; omit to use the default credential chain (environment, profile, role)."""
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None
    page_size: PositiveInt = 1000
    """Items requested per page (DynamoDB returns at most 1 MB per page)."""


class DynamoDBDatasetOptions(DocumentOptions):
    """Dataset options of the ``dynamodb`` connector."""

    key_condition: str | None = None
    """``KeyConditionExpression``; when set, dvf runs a Query instead of a Scan."""
    filter: str | None = None
    """``FilterExpression`` applied by DynamoDB after reading."""
    values: dict[str, Any] = Field(default_factory=dict)
    """``ExpressionAttributeValues`` as plain values, for example ``{":status": open}``."""
    names: dict[str, str] = Field(default_factory=dict)
    """``ExpressionAttributeNames``, for example ``{"#s": status}``."""
    index: str | None = None
    """Global or local secondary index to read."""
    projection: str | None = None
    """``ProjectionExpression``: the attributes to read."""
    consistent_read: bool = False
    """Strongly consistent reads (not available on global secondary indexes)."""


def _import_boto3() -> Any:
    try:
        return importlib.import_module("boto3")
    except ImportError as exc:
        raise DependencyError("boto3", "dynamodb", "The DynamoDB connector") from exc


def to_attribute(value: Any) -> dict[str, Any]:
    """A plain value as a DynamoDB attribute value (for ``ExpressionAttributeValues``)."""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int | Decimal):
        return {"N": str(value)}
    if isinstance(value, float):
        return {"N": repr(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, bytes | bytearray):
        return {"B": bytes(value)}
    if isinstance(value, Mapping):
        return {"M": {str(k): to_attribute(v) for k, v in value.items()}}
    if isinstance(value, list | tuple):
        return {"L": [to_attribute(v) for v in value]}
    raise ConnectorError(f"Cannot use {type(value).__name__} values in DynamoDB expressions")


def from_attribute(attribute: Mapping[str, Any]) -> Any:
    """A DynamoDB attribute value as a Python value (numbers as ``Decimal``, sets as sets)."""
    if len(attribute) != 1:
        raise ConnectorError(f"Unexpected DynamoDB attribute value: {sorted(attribute)}")
    ((kind, value),) = attribute.items()
    if kind == "S":
        return value
    if kind == "N":
        return Decimal(value)
    if kind == "B":
        return bytes(value)
    if kind == "BOOL":
        return bool(value)
    if kind == "NULL":
        return None
    if kind == "M":
        return {k: from_attribute(v) for k, v in value.items()}
    if kind == "L":
        return [from_attribute(v) for v in value]
    if kind == "SS":
        return _TypedSet("SS", value)
    if kind == "NS":
        return _TypedSet("NS", [Decimal(v) for v in value])
    if kind == "BS":
        return _TypedSet("BS", [bytes(v) for v in value])
    raise ConnectorError(f"Unknown DynamoDB attribute type '{kind}'")


class _TypedSet(list[Any]):
    """A DynamoDB set (``SS``, ``NS``, ``BS``), kept with its type name."""

    def __init__(self, native: str, values: list[Any]) -> None:
        super().__init__(values)
        self.native = native


def item(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """A DynamoDB item (attribute values) as a Python document."""
    return {name: from_attribute(value) for name, value in attributes.items()}


def classify_dynamodb(value: Any) -> Classified:
    """Classify values decoded by :func:`from_attribute`, keeping DynamoDB's type names."""
    if value is None:
        return Classified(ValueKind.NULL, "NULL", None)
    if isinstance(value, bool):
        return Classified(ValueKind.BOOL, "BOOL", value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value() and INT64_MIN <= value <= INT64_MAX:
            return Classified(ValueKind.INT, "N", int(value))
        return Classified(ValueKind.DECIMAL, "N", value)
    if isinstance(value, str):
        return Classified(ValueKind.STRING, "S", value)
    if isinstance(value, bytes):
        return Classified(ValueKind.BINARY, "B", value)
    if isinstance(value, _TypedSet):
        ordered = sorted(value, key=lambda v: canonical_json(v, classify_dynamodb))
        return Classified(ValueKind.ARRAY, value.native, ordered)
    if isinstance(value, Mapping):
        return Classified(ValueKind.OBJECT, "M", value)
    if isinstance(value, list):
        return Classified(ValueKind.ARRAY, "L", value)
    return classify_python(value)


class DynamoDBConnector(DocumentConnector):
    """Amazon DynamoDB tables (Query or Scan), flattened and staged for SQL comparison."""

    Config: ClassVar[type[DynamoDBConnection]] = DynamoDBConnection
    options_model = DynamoDBDatasetOptions
    system = "DynamoDB"
    decimal_precision = 38

    def __init__(self, name: str, config: Any, base_dir: Any) -> None:
        super().__init__(name, config, base_dir)
        self._client: Any = None
        self._key_cache: dict[str, tuple[str, ...]] = {}

    @property
    def dynamo_config(self) -> DynamoDBConnection:
        assert isinstance(self.config, DynamoDBConnection)
        return self.config

    def make_client(self) -> Any:
        """A ``boto3`` DynamoDB client for this connection."""
        boto3 = _import_boto3()
        cfg = self.dynamo_config
        session_kwargs: dict[str, Any] = {}
        if cfg.profile:
            session_kwargs["profile_name"] = cfg.profile
        if cfg.aws_access_key_id is not None:
            session_kwargs["aws_access_key_id"] = cfg.aws_access_key_id.get_secret_value()
        if cfg.aws_secret_access_key is not None:
            session_kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key.get_secret_value()
        if cfg.aws_session_token is not None:
            session_kwargs["aws_session_token"] = cfg.aws_session_token.get_secret_value()
        session = boto3.session.Session(**session_kwargs)
        client_kwargs: dict[str, Any] = dict(cfg.options)
        if cfg.region:
            client_kwargs["region_name"] = cfg.region
        if cfg.endpoint_url:
            client_kwargs["endpoint_url"] = cfg.endpoint_url
        return session.client("dynamodb", **client_kwargs)

    def open_source(self) -> None:
        self._client = self.make_client()

    def close_source(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()
        self._client = None

    @property
    def client(self) -> Any:
        self.open()
        return self._client

    def classify(self, value: Any) -> Classified:
        return classify_dynamodb(value)

    # ------------------------------------------------------------------ datasets
    def _describe(self, table: str) -> dict[str, Any] | None:
        try:
            response = self.client.describe_table(TableName=table)
        except Exception as exc:
            if type(exc).__name__ == "ResourceNotFoundException" or "ResourceNotFound" in str(exc):
                return None
            raise ConnectorError(f"Cannot describe DynamoDB table '{table}': {exc}") from exc
        description: dict[str, Any] = response.get("Table", {})
        return description

    def dataset_exists(self, spec: DatasetSpec) -> bool | None:
        return self._describe(self.collection(spec)) is not None

    def list_datasets(self, pattern: str) -> list[str]:
        names: list[str] = []
        for page in self.client.get_paginator("list_tables").paginate():
            names.extend(page.get("TableNames", []))
        return sorted(n for n in names if fnmatchcase(n, pattern))

    def _key_attributes(self, table: str) -> tuple[str, ...]:
        if table not in self._key_cache:
            description = self._describe(table) or {}
            schema = description.get("KeySchema", [])
            ordered = sorted(schema, key=lambda k: 0 if k.get("KeyType") == "HASH" else 1)
            self._key_cache[table] = tuple(k["AttributeName"] for k in ordered)
        return self._key_cache[table]

    def default_primary_key(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        return self._key_attributes(self.collection(spec))

    def keep_fields(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        return self._key_attributes(self.collection(spec))

    # ------------------------------------------------------------------ reads
    def request(
        self, spec: DatasetSpec, options: DynamoDBDatasetOptions
    ) -> tuple[str, dict[str, Any]]:
        """The operation (``query`` or ``scan``) and its parameters for a dataset."""
        params: dict[str, Any] = {"TableName": self.collection(spec)}
        operation = "scan"
        if options.key_condition:
            operation = "query"
            params["KeyConditionExpression"] = options.key_condition
        if options.filter:
            params["FilterExpression"] = options.filter
        if options.values:
            params["ExpressionAttributeValues"] = {
                k: to_attribute(v) for k, v in options.values.items()
            }
        if options.names:
            params["ExpressionAttributeNames"] = dict(options.names)
        if options.index:
            params["IndexName"] = options.index
        if options.projection:
            params["ProjectionExpression"] = options.projection
        if options.consistent_read:
            params["ConsistentRead"] = True
        return operation, params

    def iter_documents(
        self,
        spec: DatasetSpec,
        options: DocumentOptions,
        *,
        limit: int | None = None,
        sampling: Sampling | None = None,
    ) -> Iterator[list[Mapping[str, Any]]]:
        assert isinstance(options, DynamoDBDatasetOptions)
        operation, params = self.request(spec, options)
        page_size = options.batch_size or self.dynamo_config.page_size
        pagination: dict[str, Any] = {"PageSize": page_size}
        if limit:
            pagination["MaxItems"] = limit
        paginator = self.client.get_paginator(operation)
        seen = 0
        for page in paginator.paginate(**params, PaginationConfig=pagination):
            documents: list[Mapping[str, Any]] = [item(a) for a in page.get("Items", [])]
            if limit is not None and seen + len(documents) > limit:
                documents = documents[: limit - seen]
            seen += len(documents)
            if documents:
                yield documents
            if limit is not None and seen >= limit:
                return


__all__ = [
    "DynamoDBConnection",
    "DynamoDBConnector",
    "DynamoDBDatasetOptions",
    "classify_dynamodb",
    "from_attribute",
    "to_attribute",
]
