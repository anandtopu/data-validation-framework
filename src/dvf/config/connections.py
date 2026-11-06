"""Connection configuration models.

Each connector type registers one of these models (or its own subclass of
:class:`ConnectionConfig`). Secrets belong in ``${secret:...}`` references, never inline.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, PositiveInt, SecretStr, model_validator

from dvf.config.base import StrictModel

Port = int


class ConnectionConfig(StrictModel):
    """Settings shared by every connection."""

    type: str
    """Connector type, for example ``postgres``, ``snowflake``, or ``files``."""
    description: str | None = None
    read_only: bool = True
    """dvf never writes to read-only connections. Keep this on for source and target systems."""
    max_parallel_queries: PositiveInt | None = None
    """Upper bound on concurrent queries against this connection (protects production sources)."""
    query_timeout_s: PositiveInt | None = None
    """Per-query timeout in seconds; overrides ``defaults.execution.query_timeout_s``."""
    options: dict[str, Any] = Field(default_factory=dict)
    """Driver-specific options passed through unchanged."""


class DuckDBConnection(ConnectionConfig):
    """An embedded DuckDB database (in memory by default)."""

    database: str = ":memory:"
    """Database file path, or ``:memory:``."""
    extensions: list[str] = Field(default_factory=list)
    """DuckDB extensions to load, for example ``httpfs`` or ``spatial``."""


class SQLiteConnection(ConnectionConfig):
    """A SQLite database file."""

    database: str
    """Database file path, or ``:memory:``."""


FileFormat = Literal[
    "csv",
    "tsv",
    "parquet",
    "json",
    "ndjson",
    "avro",
    "orc",
    "excel",
    "xml",
    "fixed_width",
    "delta",
    "iceberg",
]


class FileConnection(ConnectionConfig):
    """Files on a local disk or object store (S3, ADLS, GCS) read as datasets."""

    path: str
    """Base folder or URL. Dataset ``path`` values are relative to it."""
    format: FileFormat | None = None
    """Default file format; inferred from the extension when omitted."""
    storage_options: dict[str, Any] = Field(default_factory=dict)
    """fsspec storage options (credentials should be secret references)."""


class DatabaseConnection(ConnectionConfig):
    """A relational database reached by host and port or by URL."""

    url: SecretStr | None = None
    """A SQLAlchemy-style URL. Treated as a secret because it may contain a password."""
    host: str | None = None
    port: Port | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    """Default schema for unqualified table names."""
    user: str | None = None
    password: SecretStr | None = None
    driver: str | None = None
    """ODBC driver name or driver variant, where the connector supports a choice."""

    @model_validator(mode="after")
    def _url_or_host(self) -> DatabaseConnection:
        if self.url is None and self.host is None:
            raise ValueError("set either 'url' or 'host'")
        return self


class SnowflakeConnection(ConnectionConfig):
    """Snowflake."""

    account: str
    user: str | None = None
    password: SecretStr | None = None
    authenticator: str | None = None
    """For example ``externalbrowser``, ``oauth``, or ``snowflake_jwt``."""
    private_key_path: str | None = None
    private_key_passphrase: SecretStr | None = None
    warehouse: str | None = None
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    role: str | None = None


class BigQueryConnection(ConnectionConfig):
    """Google BigQuery."""

    project: str
    dataset: str | None = None
    location: str | None = None
    credentials_path: str | None = None
    """Service account key file; omit to use application default credentials."""


class DatabricksConnection(ConnectionConfig):
    """Databricks SQL warehouse or cluster through the SQL connector."""

    server_hostname: str
    http_path: str
    access_token: SecretStr | None = None
    auth_type: str | None = None
    catalog: str | None = None
    schema_: str | None = Field(default=None, alias="schema")


class SparkConnection(ConnectionConfig):
    """A Spark session: local, a cluster, or Databricks Connect."""

    master: str | None = None
    """Spark master URL, for example ``local[*]``. Omit to use the active session."""
    app_name: str = "dvf"
    conf: dict[str, str] = Field(default_factory=dict)
    databricks_connect: bool = False
    catalog: str | None = None
    schema_: str | None = Field(default=None, alias="schema")


class MongoDBConnection(ConnectionConfig):
    """MongoDB."""

    uri: SecretStr
    """Connection string. Treated as a secret because it usually contains credentials."""
    database: str


class SapHanaConnection(ConnectionConfig):
    """SAP HANA at database level (requires the user-installed ``hdbcli`` driver)."""

    host: str
    port: Port = Field(default=30015, ge=1, le=65535)
    user: str | None = None
    password: SecretStr | None = None
    schema_: str | None = Field(default=None, alias="schema")
    client: str | None = Field(default=None, pattern=r"^\d{3}$")
    """SAP client (MANDT), applied as a filter to client-dependent tables."""
    encrypt: bool = True


class SapRfcConnection(ConnectionConfig):
    """SAP ECC or S/4HANA through RFC, with a pluggable backend."""

    ashost: str | None = None
    """Application server host (direct connection)."""
    mshost: str | None = None
    """Message server host (load-balanced connection)."""
    sysnr: str | None = Field(default=None, pattern=r"^\d{2}$")
    group: str | None = None
    client: str = Field(pattern=r"^\d{3}$")
    user: str | None = None
    password: SecretStr | None = None
    lang: str = "EN"
    backend: str = "pyrfc"
    """RFC backend: ``pyrfc`` (unmaintained upstream), ``nwrfcsdk``, ``custom_fm``, or ``odata``."""
    read_function: str = "RFC_READ_TABLE"
    """Function module used to read tables. Custom modules avoid the 512-byte row limit."""

    @model_validator(mode="after")
    def _server(self) -> SapRfcConnection:
        if self.ashost is None and self.mshost is None:
            raise ValueError("set either 'ashost' or 'mshost'")
        return self


class SqlAlchemyConnection(ConnectionConfig):
    """Any database with a SQLAlchemy dialect."""

    url: SecretStr


class OdbcConnection(ConnectionConfig):
    """Any database with an ODBC driver."""

    dsn: str | None = None
    connection_string: SecretStr | None = None

    @model_validator(mode="after")
    def _dsn_or_string(self) -> OdbcConnection:
        if (self.dsn is None) == (self.connection_string is None):
            raise ValueError("set exactly one of 'dsn' or 'connection_string'")
        return self
