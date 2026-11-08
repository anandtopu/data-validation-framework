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
    """Files on a local disk or object store (S3, ADLS, GCS) read as datasets.

    ``options`` hold reader options applied to every dataset of the connection (dataset
    ``options`` override them), plus ``extensions``: DuckDB extensions to load (for example
    ``httpfs``) instead of reading URLs through fsspec.
    """

    path: str
    """Base folder or URL. Dataset ``path`` values are relative to it."""
    format: FileFormat | None = None
    """Default file format; inferred from the extension (or a Delta or Iceberg table folder)
    when omitted."""
    storage_options: dict[str, Any] = Field(default_factory=dict)
    """Storage options for URLs: passed to fsspec (file listing, checksums, converted formats,
    and DuckDB through a registered fsspec file system) and to deltalake for Delta tables.
    Credentials should be secret references."""


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
    """Spark master URL, for example ``local[*]`` or ``spark://host:7077``. Omit to use the
    running session (a notebook or job), or a new ``local[*]`` session when none is running."""
    app_name: str = "dvf"
    """Application name of a session dvf starts."""
    conf: dict[str, str] = Field(default_factory=dict)
    """Spark settings, such as ``spark.sql.shuffle.partitions`` or ``spark.jars.packages``. Static
    settings apply only when dvf starts the session."""
    databricks_connect: bool = False
    """Use a Databricks Connect session (``databricks-connect`` installed instead of pyspark)."""
    catalog: str | None = None
    """Current catalog of dvf's session, for unqualified table names."""
    schema_: str | None = Field(default=None, alias="schema")
    """Current schema (database) of dvf's session, for unqualified table names."""


class MongoDBConnection(ConnectionConfig):
    """MongoDB (4.4 or later; verified with 7.0)."""

    uri: SecretStr
    """Connection string. Treated as a secret because it usually contains credentials."""
    database: str
    read_preference: (
        Literal["primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest"] | None
    ) = None
    """Which replica set members serve reads; the connection string's setting (or ``primary``)
    when omitted. Datasets may override it."""
    read_concern: Literal["local", "available", "majority", "linearizable", "snapshot"] | None = (
        None
    )
    """Read concern level (consistency); the server default when omitted. Datasets may override
    it."""
    uuid_representation: Literal[
        "standard", "pythonLegacy", "javaLegacy", "csharpLegacy", "unspecified"
    ] = "standard"
    """How binary UUIDs are decoded. ``standard`` reads subtype 4; choose a legacy value for
    subtype 3 UUIDs written by older drivers."""
    server_selection_timeout_ms: PositiveInt = 10_000
    """How long to wait for a reachable server before failing."""
    batch_size: PositiveInt = Field(default=1000, le=100_000)
    """Documents fetched per round trip; datasets may override it with ``options.batch_size``."""


class SapHanaConnection(ConnectionConfig):
    """SAP HANA at database level (requires the user-installed ``hdbcli`` driver)."""

    host: str
    port: Port = Field(default=30015, ge=1, le=65535)
    """SQL port: ``3<instance>15`` for single-container systems, ``3<instance>13`` (system
    database) or ``3<instance>41`` and up (tenants) for multitenant ones, 443 for HANA Cloud."""
    user: str | None = None
    password: SecretStr | None = None
    schema_: str | None = Field(default=None, alias="schema")
    """The SAP schema (for example ``SAPSR3`` or ``SAPHANADB``) for unqualified table names."""
    client: str | None = Field(default=None, pattern=r"^\d{3}$")
    """SAP client (MANDT), applied as a filter to client-dependent tables."""
    encrypt: bool = True
    database_name: str | None = None
    """Tenant database to connect to on a multitenant system (hdbcli ``databaseName``)."""
    ssl_validate_certificate: bool = True
    """Validate the server's TLS certificate when ``encrypt`` is on."""
    client_column: str | None = None
    """Name of the client column to filter on, for every table that has it. By default dvf finds
    it: the client-type key field from the dictionary (``ddic``), or else a first column named
    MANDT, CLIENT, RCLNT, or MANDANT."""
    ddic: bool = False
    """Describe tables from the ABAP dictionary (DD02L, DD03L, DD04L, DD04T, DD05S, DD08L):
    ABAP types and lengths, keys, and field texts, instead of the HANA catalog alone."""
    ddic_schema: str | None = None
    """Schema of the dictionary tables, when it is not ``schema``."""
    ddic_language: str = Field(default="E", min_length=1, max_length=1)
    """SAP language key of dictionary texts (``E`` for English, ``D`` for German)."""


class SapRfcConnection(ConnectionConfig):
    """SAP ECC or S/4HANA through RFC or OData, with a pluggable backend."""

    ashost: str | None = None
    """Application server host (direct connection)."""
    mshost: str | None = None
    """Message server host (load-balanced connection)."""
    sysnr: str | None = Field(default=None, pattern=r"^\d{2}$")
    sysid: str | None = None
    """System ID, for load-balanced connections through a message server."""
    msserv: str | None = None
    """Message server service or port, when it is not the default for ``sysid``."""
    group: str | None = None
    """Logon group, for load-balanced connections."""
    saprouter: str | None = None
    """SAProuter string, when the system is reached through one."""
    client: str = Field(pattern=r"^\d{3}$")
    """SAP client to log on to. Client-dependent tables are read in this client."""
    user: str | None = None
    password: SecretStr | None = None
    lang: str = "EN"
    backend: str = "pyrfc"
    """How dvf talks to SAP: ``pyrfc`` (RFC through the user-installed, no longer maintained PyRFC
    and NW RFC SDK), ``custom_fm`` (PyRFC with your own read function module), ``odata`` (OData
    v2 or v4 services and CDS views over HTTP), or a backend added by a plugin. ``nwrfcsdk`` (a
    direct SDK binding) is declared but not implemented."""
    read_function: str = "RFC_READ_TABLE"
    """Function module used to read tables. ``custom_fm`` needs your module here: it must take
    RFC_READ_TABLE's parameters and may return rows wider than 512 characters."""
    row_width: PositiveInt = Field(default=512, le=1_000_000)
    """Characters per data row of the read function: 512 for RFC_READ_TABLE; set your custom
    module's width for ``custom_fm``. Wider tables are read in chunks of fields."""
    delimiter: str = Field(default="", max_length=1)
    """Field delimiter passed to the read function. dvf cuts rows by the field offsets the
    function returns, so it is needed only by custom modules that require one."""
    page_size: PositiveInt = Field(default=50_000, le=10_000_000)
    """Rows per call (RFC_READ_TABLE ``ROWCOUNT``, OData ``$top``)."""
    ddic_source: Literal["fieldinfo", "dd03l"] = "fieldinfo"
    """How tables are described: the function module DDIF_FIELDINFO_GET, or DD03L read with the
    read function (for systems where DDIF_FIELDINFO_GET is not released for RFC)."""
    foreign_keys: bool = False
    """Also read foreign keys (DD05S and DD08L) when describing tables."""
    url: str | None = None
    """OData service root for the ``odata`` backend, for example
    ``https://host/sap/opu/odata/sap/API_BUSINESS_PARTNER``."""
    odata_version: Literal[2, 4] | None = None
    """OData protocol version; detected from the service metadata when unset."""
    verify_tls: bool = True
    """Validate the TLS certificate of the OData service."""

    @model_validator(mode="after")
    def _server(self) -> SapRfcConnection:
        from dvf.packs.sap.rfc.backends import validate_backend_config

        validate_backend_config(self)
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
