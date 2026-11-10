"""Apache Cassandra connector: tables read with the DataStax driver, flattened, and staged.

A dataset names a table with ``table`` (``keyspace.table``, or ``table`` with the connection's
``keyspace``). ``options.where`` is a CQL predicate pushed down to Cassandra (it must be valid
CQL for the table's keys, or need ``options.allow_filtering``). Collections, tuples, and
user-defined types are flattened like documents; top-level columns keep their CQL type names in
schema checks.

Beta: verified with a fake driver, not yet against a live cluster. Install the driver with
``pip install 'dvf-framework[cassandra]'``.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveInt, SecretStr

from dvf.config.connections import ConnectionConfig
from dvf.config.models import DatasetSpec
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import CanonicalType, LogicalType
from dvf.packs.nosql.base import DocumentConnector, Sampling, chunked
from dvf.packs.nosql.flatten import split_path
from dvf.packs.nosql.options import DocumentOptions
from dvf.packs.nosql.staging import TypeHints

ConsistencyName = Literal[
    "ANY",
    "ONE",
    "TWO",
    "THREE",
    "QUORUM",
    "ALL",
    "LOCAL_QUORUM",
    "EACH_QUORUM",
    "SERIAL",
    "LOCAL_SERIAL",
    "LOCAL_ONE",
]


class CassandraConnection(ConnectionConfig):
    """Apache Cassandra, ScyllaDB, or DataStax Astra through the DataStax Python driver."""

    contact_points: list[str] = Field(default_factory=lambda: ["127.0.0.1"], min_length=1)
    """Hosts to connect to first."""
    port: int = Field(default=9042, ge=1, le=65535)
    """CQL native protocol port of the contact points; not used with ``secure_connect_bundle``."""
    keyspace: str | None = None
    """Keyspace for table names without one."""
    username: str | None = None
    """User name for plain-text authentication."""
    password: SecretStr | None = None
    """Password of ``username``. Use a ``${secret:...}`` reference; it is treated as a secret."""
    local_datacenter: str | None = None
    """Data center whose nodes serve reads (``DCAwareRoundRobinPolicy``)."""
    consistency_level: ConsistencyName = "LOCAL_QUORUM"
    """Read consistency level. ``LOCAL_QUORUM`` avoids false mismatches from replicas that have
    not caught up; datasets may override it."""
    secure_connect_bundle: str | None = None
    """DataStax Astra secure connect bundle (zip file)."""
    ssl: bool = False
    """Use TLS with the system's default certificate store."""
    protocol_version: int | None = Field(default=None, ge=3, le=6)
    """Native protocol version, from 3 to 6; the driver negotiates one when omitted."""
    connect_timeout_s: PositiveInt = 10
    """Seconds to wait for a connection to a node."""
    fetch_size: PositiveInt = Field(default=1000, le=100_000)
    """Rows per page; datasets may override it with ``options.batch_size``."""


class CassandraDatasetOptions(DocumentOptions):
    """Dataset options of the ``cassandra`` connector."""

    where: str | None = None
    """CQL predicate, for example ``customer_id = 42 AND order_date >= '2024-01-01'``."""
    columns: list[str] | None = None
    """Columns to read (default: all)."""
    allow_filtering: bool = False
    """Add ``ALLOW FILTERING`` (scans more data than the predicate implies)."""
    consistency_level: ConsistencyName | None = None
    """Overrides the connection's consistency level for this dataset."""


_SIMPLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

_CQL_CANONICAL: dict[str, CanonicalType] = {
    "tinyint": CanonicalType.integer(8),
    "smallint": CanonicalType.integer(16),
    "int": CanonicalType.integer(32),
    "bigint": CanonicalType.integer(64),
    "counter": CanonicalType.integer(64),
    "float": CanonicalType.float(32),
    "double": CanonicalType.float(64),
    "boolean": CanonicalType.boolean(),
    "text": CanonicalType.string(),
    "varchar": CanonicalType.string(),
    "ascii": CanonicalType.string(unicode=False),
    "inet": CanonicalType.string(),
    "timestamp": CanonicalType.timestamp(3, tz=True),
    "date": CanonicalType.date(),
    "time": CanonicalType.time(9),
    "uuid": CanonicalType.simple(LogicalType.UUID),
    "timeuuid": CanonicalType.simple(LogicalType.UUID),
    "blob": CanonicalType.binary(),
}


def quote_cql(name: str) -> str:
    """A CQL identifier: unquoted when it is a plain lower-case name, quoted otherwise."""
    if _SIMPLE_NAME.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _import(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise DependencyError("cassandra-driver", "cassandra", "The Cassandra connector") from exc


def plain(value: Any) -> Any:
    """Convert DataStax driver values (dates, times, sets, maps, UDTs) to plain Python."""
    if value is None or isinstance(value, str | bytes | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    name = type(value).__name__
    if name == "Date" and hasattr(value, "date"):
        try:
            return value.date()
        except (ValueError, OverflowError):
            return str(value)
    if name == "Time" and hasattr(value, "time"):
        return value.time()
    if name == "Duration":
        return str(value)
    if isinstance(value, Mapping) or name in ("OrderedMap", "OrderedMapSerializedKey"):
        return {k: plain(v) for k, v in value.items()}
    if hasattr(value, "_fields") and isinstance(value, tuple):  # user-defined types
        return {f: plain(getattr(value, f)) for f in getattr(value, "_fields", ())}
    if name == "SortedSet" or isinstance(value, set | frozenset):
        return {_hashable(plain(v)) for v in value}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    if isinstance(value, list):
        return tuple(value)
    return value


class CassandraConnector(DocumentConnector):
    """Cassandra tables, flattened and staged for SQL comparison."""

    Config: ClassVar[type[CassandraConnection]] = CassandraConnection
    options_model = CassandraDatasetOptions
    system = "Cassandra"
    timestamp_precision = 3

    def __init__(self, name: str, config: Any, base_dir: Any) -> None:
        super().__init__(name, config, base_dir)
        self._cluster: Any = None
        self._session: Any = None

    @property
    def cassandra_config(self) -> CassandraConnection:
        assert isinstance(self.config, CassandraConnection)
        return self.config

    # ------------------------------------------------------------------ lifecycle
    def open_source(self) -> None:
        cluster_module = _import("cassandra.cluster")
        query_module = _import("cassandra.query")
        cfg = self.cassandra_config
        kwargs: dict[str, Any] = {"connect_timeout": cfg.connect_timeout_s}
        if cfg.secure_connect_bundle:
            kwargs["cloud"] = {"secure_connect_bundle": cfg.secure_connect_bundle}
        else:
            kwargs["contact_points"] = list(cfg.contact_points)
            kwargs["port"] = cfg.port
        if cfg.username is not None:
            auth = _import("cassandra.auth")
            password = cfg.password.get_secret_value() if cfg.password is not None else ""
            kwargs["auth_provider"] = auth.PlainTextAuthProvider(
                username=cfg.username, password=password
            )
        if cfg.protocol_version is not None:
            kwargs["protocol_version"] = cfg.protocol_version
        if cfg.ssl:
            import ssl

            kwargs["ssl_context"] = ssl.create_default_context()
        profile_kwargs: dict[str, Any] = {
            "row_factory": query_module.dict_factory,
            "consistency_level": self._consistency(cfg.consistency_level),
        }
        if cfg.query_timeout_s:
            profile_kwargs["request_timeout"] = cfg.query_timeout_s
        if cfg.local_datacenter:
            policies = _import("cassandra.policies")
            profile_kwargs["load_balancing_policy"] = policies.DCAwareRoundRobinPolicy(
                local_dc=cfg.local_datacenter
            )
        profile = cluster_module.ExecutionProfile(**profile_kwargs)
        kwargs["execution_profiles"] = {cluster_module.EXEC_PROFILE_DEFAULT: profile}
        kwargs.update(cfg.options)
        cluster = cluster_module.Cluster(**kwargs)
        try:
            self._session = cluster.connect()
        except Exception as exc:
            cluster.shutdown()
            raise ConnectorError(
                f"Cannot connect to Cassandra for connection '{self.name}': {exc}",
                hint="Check contact_points, port, credentials, and local_datacenter.",
            ) from exc
        self._cluster = cluster

    def close_source(self) -> None:
        if self._cluster is not None:
            self._cluster.shutdown()
        self._cluster = None
        self._session = None

    def _consistency(self, name: str) -> Any:
        levels = _import("cassandra").ConsistencyLevel
        return levels.name_to_value[name]

    @property
    def session(self) -> Any:
        self.open()
        return self._session

    def server_version(self) -> str | None:
        row = self.session.execute("SELECT release_version FROM system.local").one()
        return str(row["release_version"]) if row else None

    # ------------------------------------------------------------------ metadata
    def table_name(self, spec: DatasetSpec) -> tuple[str, str]:
        """``(keyspace, table)`` of a dataset."""
        name = self.collection(spec)
        if "." in name:
            keyspace, table = name.split(".", 1)
            return keyspace, table
        if not self.cassandra_config.keyspace:
            raise ConnectorError(
                f"Table '{name}' has no keyspace and connection '{self.name}' sets none.",
                hint="Write 'keyspace.table', or set 'keyspace' on the connection.",
            )
        return self.cassandra_config.keyspace, name

    def _table_metadata(self, keyspace: str, table: str) -> Any:
        self.open()
        keyspaces = self._cluster.metadata.keyspaces
        ks = keyspaces.get(keyspace)
        return None if ks is None else ks.tables.get(table)

    def dataset_exists(self, spec: DatasetSpec) -> bool | None:
        return self._table_metadata(*self.table_name(spec)) is not None

    def list_datasets(self, pattern: str) -> list[str]:
        keyspace = self.cassandra_config.keyspace
        self.open()
        names = []
        for ks_name, ks in self._cluster.metadata.keyspaces.items():
            if keyspace and ks_name != keyspace:
                continue
            for table in ks.tables:
                qualified = table if keyspace else f"{ks_name}.{table}"
                if fnmatchcase(qualified, pattern):
                    names.append(qualified)
        return sorted(names)

    def _primary_columns(self, spec: DatasetSpec) -> tuple[str, ...]:
        meta = self._table_metadata(*self.table_name(spec))
        if meta is None:
            return ()
        return tuple(c.name for c in [*meta.partition_key, *meta.clustering_key])

    def default_primary_key(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        return self._primary_columns(spec)

    def keep_fields(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        return self._primary_columns(spec)

    def type_hints(self, spec: DatasetSpec, options: DocumentOptions) -> TypeHints:
        hints = super().type_hints(spec, options)
        meta = self._table_metadata(*self.table_name(spec))
        if meta is None:
            return hints
        declared = {name: str(column.cql_type) for name, column in meta.columns.items()}
        if options.fields is not None:
            by_column = {
                out: declared[path]
                for out, path in options.fields.items()
                if len(split_path(path)) == 1 and path in declared
            }
        else:
            by_column = declared
        hints.native_types = by_column
        hints.canonical_types = {
            name: _CQL_CANONICAL[cql] for name, cql in by_column.items() if cql in _CQL_CANONICAL
        }
        return hints

    # ------------------------------------------------------------------ reads
    def cql(self, spec: DatasetSpec, options: CassandraDatasetOptions, limit: int | None) -> str:
        """The CQL ``SELECT`` statement for a dataset."""
        keyspace, table = self.table_name(spec)
        columns = ", ".join(quote_cql(c) for c in options.columns) if options.columns else "*"
        statement = f"SELECT {columns} FROM {quote_cql(keyspace)}.{quote_cql(table)}"
        if options.where:
            statement += f" WHERE {options.where}"
        if limit:
            statement += f" LIMIT {int(limit)}"
        if options.allow_filtering:
            statement += " ALLOW FILTERING"
        return statement

    def iter_documents(
        self,
        spec: DatasetSpec,
        options: DocumentOptions,
        *,
        limit: int | None = None,
        sampling: Sampling | None = None,
    ) -> Iterator[list[Mapping[str, Any]]]:
        assert isinstance(options, CassandraDatasetOptions)
        query_module = _import("cassandra.query")
        fetch_size = options.batch_size or self.cassandra_config.fetch_size
        consistency = options.consistency_level or self.cassandra_config.consistency_level
        statement = query_module.SimpleStatement(
            self.cql(spec, options, limit),
            fetch_size=fetch_size,
            consistency_level=self._consistency(consistency),
        )
        try:
            rows = self.session.execute(statement)
        except Exception as exc:
            raise ConnectorError(
                f"CQL query failed on connection '{self.name}': {exc}",
                hint="Check options.where; predicates on non-key columns need allow_filtering.",
            ) from exc
        documents = ({k: plain(v) for k, v in row.items()} for row in rows)
        yield from chunked(documents, fetch_size)


__all__ = ["CassandraConnection", "CassandraConnector", "CassandraDatasetOptions", "plain"]
