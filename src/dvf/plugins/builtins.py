"""Built-in component declarations and implementations.

In Phase 0 most built-ins are *declared*: their name, description, and configuration model are
registered so configurations validate and the JSON Schema is complete. Implementations are
attached as each phase lands; ``dvf plugins list`` shows which are ready.
"""

from __future__ import annotations

from typing import Any

from dvf.config import checks as chk
from dvf.config import connections as conn
from dvf.config import notifications as notif
from dvf.plugins.registry import PluginKind, Registry
from dvf.security.secrets import DotEnvSecretProvider, EnvSecretProvider

_CONNECTORS: list[tuple[str, type[conn.ConnectionConfig], str, str | None]] = [
    ("duckdb", conn.DuckDBConnection, "DuckDB database, in memory or on disk", None),
    ("sqlite", conn.SQLiteConnection, "SQLite database file", None),
    (
        "files",
        conn.FileConnection,
        "CSV, Parquet, JSON, Avro, ORC, Excel, and more on local or cloud storage",
        None,
    ),
    ("postgres", conn.DatabaseConnection, "PostgreSQL", "postgres"),
    ("mysql", conn.DatabaseConnection, "MySQL 8.0.17 or later", "mysql"),
    ("mssql", conn.DatabaseConnection, "SQL Server, Azure SQL, Synapse, and Fabric SQL", "mssql"),
    ("oracle", conn.DatabaseConnection, "Oracle Database", "oracle"),
    ("db2", conn.DatabaseConnection, "IBM Db2", "db2"),
    ("teradata", conn.DatabaseConnection, "Teradata Vantage", "teradata"),
    ("redshift", conn.DatabaseConnection, "Amazon Redshift", "redshift"),
    ("snowflake", conn.SnowflakeConnection, "Snowflake", "snowflake"),
    ("bigquery", conn.BigQueryConnection, "Google BigQuery", "bigquery"),
    (
        "databricks",
        conn.DatabricksConnection,
        "Databricks SQL warehouses and clusters",
        "databricks",
    ),
    (
        "spark",
        conn.SparkConnection,
        "Apache Spark session: local, cluster, or Databricks Connect",
        "spark",
    ),
    ("mongodb", conn.MongoDBConnection, "MongoDB", "mongodb"),
    (
        "sap_hana",
        conn.SapHanaConnection,
        "SAP HANA at database level (user-installed hdbcli)",
        "sap-hana",
    ),
    (
        "sap_rfc",
        conn.SapRfcConnection,
        "SAP ECC and S/4HANA through RFC (pluggable backend)",
        "sap-rfc",
    ),
    ("sqlalchemy", conn.SqlAlchemyConnection, "Any database with a SQLAlchemy dialect", None),
    ("odbc", conn.OdbcConnection, "Any database with an ODBC driver", None),
]

_CHECKS: list[tuple[str, type[chk.CheckConfig]]] = [
    ("not_null", chk.NotNullCheck),
    ("not_blank", chk.NotBlankCheck),
    ("null_ratio", chk.NullRatioCheck),
    ("unique", chk.UniqueCheck),
    ("regex", chk.RegexCheck),
    ("allowed_values", chk.AllowedValuesCheck),
    ("range", chk.RangeCheck),
    ("length", chk.LengthCheck),
    ("referential_integrity", chk.ReferentialIntegrityCheck),
    ("custom_sql", chk.CustomSqlCheck),
    ("expression", chk.ExpressionCheck),
    ("row_count_range", chk.RowCountRangeCheck),
    ("freshness", chk.FreshnessCheck),
]

_COMPARATORS = {
    "row_count": "Compare total and grouped row counts",
    "aggregate": "Compare control totals per column (sum, min, max, distinct, nulls), overall or grouped",
    "profile": "Compare column profiles side by side",
    "row_hash": "Compare a hash of canonicalized columns per key",
    "hash_bucket": "Compare bucketed checksums in each database, then drill into mismatched buckets",
    "bisect": "Recursive segment checksums for mostly identical large tables",
    "full_diff": "Row and column level diff with side-by-side values",
    "sample": "Statistical sample comparison with a fixed seed",
}

_NORMALIZERS = {
    "trim": "Remove leading and trailing whitespace",
    "rtrim": "Remove trailing whitespace (blank-padded CHAR columns)",
    "ltrim": "Remove leading whitespace",
    "lower": "Lower-case strings",
    "upper": "Upper-case strings",
    "collapse_whitespace": "Replace runs of whitespace with a single space",
    "unicode_nfc": "Unicode NFC normalization",
    "null_if_empty": "Treat empty strings as NULL",
    "empty_if_null": "Treat NULL as an empty string",
    "round": "Round numbers to a number of decimal digits",
    "strip_leading_zeros": "Remove leading zeros from numeric strings",
    "truncate_timestamp": "Truncate timestamps to a precision",
    "to_utc": "Convert timestamps to UTC",
    "sap_alpha": "SAP ALPHA conversion exit: pad or strip leading zeros on keys",
    "sap_matnr_length": "SAP material number length change from 18 to 40 characters",
    "sap_dats": "SAP DATS: '00000000' or blank to NULL, YYYYMMDD to DATE",
    "sap_tims": "SAP TIMS: '000000' to NULL, HHMMSS to TIME",
    "sap_bool": "SAP flag: 'X' to true, blank to false",
    "sap_curr": "SAP currency amount: decimal shift from TCURX",
    "sap_quan": "SAP quantity with unit reference",
    "sap_numc": "SAP NUMC: digits-only, zero-padded",
    "sap_spras": "SAP language key: 1-character internal to 2-character ISO",
}

_REPORTERS = {
    "json": "Canonical results document (versioned JSON)",
    "html": "Self-contained interactive HTML report",
    "junit": "JUnit XML for CI test tabs",
    "markdown": "Markdown summary for pull requests and CI summaries",
    "excel": "Excel workbook for business sign-off",
    "pdf": "PDF rendered from the HTML report",
}

_NOTIFIERS: list[tuple[str, type[notif.NotificationConfig], str]] = [
    ("teams", notif.WebhookNotification, "Microsoft Teams webhook"),
    ("slack", notif.WebhookNotification, "Slack webhook"),
    ("webhook", notif.WebhookNotification, "Generic HTTP webhook"),
    ("email", notif.EmailNotification, "Email through SMTP"),
]

_ENGINES = {
    "auto": "Choose the engine from connector capabilities and data size",
    "pushdown": "SQL generated per dialect and run inside the source and target databases",
    "duckdb": "Local out-of-core engine with spill to disk",
    "polars": "Local DataFrame engine (lazy and streaming)",
    "spark": "Distributed engine for very large data and Databricks",
}

_SECRET_PROVIDERS_DECLARED = {
    "keyring": ("Operating system keyring", None),
    "azurekv": ("Azure Key Vault", "azure"),
    "awssm": ("AWS Secrets Manager", "aws"),
    "gcpsm": ("Google Cloud Secret Manager", "gcp"),
    "vault": ("HashiCorp Vault", "vault"),
    "databricks": ("Databricks secret scopes", "databricks"),
}

_SINKS = {
    "duckdb": ("DuckDB file", None),
    "sqlite": ("SQLite file", None),
    "postgres": ("PostgreSQL table", "postgres"),
    "snowflake": ("Snowflake table", "snowflake"),
    "delta": ("Delta Lake table", "delta"),
}


def declare_builtins(registry: Registry) -> None:
    """Register every built-in component into ``registry``."""
    for name, connection_model, description, extra in _CONNECTORS:
        registry.declare(
            PluginKind.CONNECTOR, name, description, config_model=connection_model, extra=extra
        )
    for name, check_model in _CHECKS:
        description = (check_model.__doc__ or "").strip().splitlines()[0]
        registry.declare(PluginKind.CHECK, name, description, config_model=check_model)
    for name, description in _COMPARATORS.items():
        registry.declare(PluginKind.COMPARATOR, name, description)
    for name, description in _NORMALIZERS.items():
        registry.declare(PluginKind.NORMALIZER, name, description)
    for name, description in _REPORTERS.items():
        registry.declare(
            PluginKind.REPORTER, name, description, extra="excel" if name == "excel" else None
        )
    for name, notifier_model, description in _NOTIFIERS:
        registry.declare(PluginKind.NOTIFIER, name, description, config_model=notifier_model)
    for name, description in _ENGINES.items():
        registry.declare(
            PluginKind.ENGINE, name, description, extra="spark" if name == "spark" else None
        )
    for name, (description, extra) in _SINKS.items():
        registry.declare(PluginKind.SINK, name, description, extra=extra)

    registry.register(PluginKind.SECRET_PROVIDER, "env", EnvSecretProvider, origin="builtin")
    registry.register(PluginKind.SECRET_PROVIDER, "dotenv", DotEnvSecretProvider, origin="builtin")
    for name, (description, extra) in _SECRET_PROVIDERS_DECLARED.items():
        registry.declare(PluginKind.SECRET_PROVIDER, name, description, extra=extra)
    _attach_implementations(registry)


def _attach_implementations(registry: Registry) -> None:
    """Attach the implementations that exist in this version to their declarations."""
    from dvf.checks.quality import BUILTIN_CHECKS
    from dvf.compare.comparators import BUILTIN_COMPARATORS
    from dvf.connectors.duckdb import DuckDBConnector, FileConnector
    from dvf.connectors.mssql import MSSQLConnector
    from dvf.connectors.mysql import MySQLConnector
    from dvf.connectors.oracle import OracleConnector
    from dvf.connectors.postgres import PostgresConnector
    from dvf.connectors.sqlite import SQLiteConnector
    from dvf.engines.polars_engine import PolarsEngine
    from dvf.engines.sql_engines import AutoEngine, DuckDBEngine, PushdownEngine
    from dvf.normalize.normalizers import BUILTIN_NORMALIZERS
    from dvf.reporting.builtin import BUILTIN_REPORTERS
    from dvf.sinks.builtin import BUILTIN_SINKS

    groups: list[tuple[PluginKind, dict[str, Any]]] = [
        (
            PluginKind.CONNECTOR,
            {
                "duckdb": DuckDBConnector,
                "files": FileConnector,
                "sqlite": SQLiteConnector,
                "postgres": PostgresConnector,
                "mysql": MySQLConnector,
                "mssql": MSSQLConnector,
                "oracle": OracleConnector,
            },
        ),
        (PluginKind.CHECK, dict(BUILTIN_CHECKS)),
        (PluginKind.COMPARATOR, dict(BUILTIN_COMPARATORS)),
        (PluginKind.NORMALIZER, dict(BUILTIN_NORMALIZERS)),
        (PluginKind.REPORTER, dict(BUILTIN_REPORTERS)),
        (PluginKind.SINK, dict(BUILTIN_SINKS)),
        (
            PluginKind.ENGINE,
            {
                "auto": AutoEngine,
                "pushdown": PushdownEngine,
                "duckdb": DuckDBEngine,
                "polars": PolarsEngine,
            },
        ),
    ]
    groups.append((PluginKind.CONNECTOR, _beta_connectors()))
    beta = set(_BETA_CONNECTORS)
    for kind, implementations in groups:
        for name, implementation in implementations.items():
            declared = registry.find(kind, name)
            registry.register(
                kind,
                name,
                implementation,
                description=declared.description if declared else None,
                config_model=declared.config_model if declared else None,
                origin="builtin",
                extra=declared.extra if declared else None,
                maturity="beta" if kind is PluginKind.CONNECTOR and name in beta else "stable",
            )
    from dvf.dataframes import DataFrameConnector, SparkFrameConnector

    registry.register(
        PluginKind.CONNECTOR,
        "dataframe",
        DataFrameConnector,
        description="In-process pandas, Polars, or Arrow data (dvf.validate_dataframe)",
        origin="builtin",
    )
    registry.register(
        PluginKind.CONNECTOR,
        "spark_dataframe",
        SparkFrameConnector,
        description="In-process Spark DataFrame (dvf.validate_dataframe)",
        origin="builtin",
        extra="spark",
    )
    from dvf.packs import register_packs

    register_packs(registry)


_BETA_CONNECTORS = ("snowflake", "bigquery", "redshift", "databricks")
"""Implemented and unit-tested, but not yet verified against a live service (shown as ``beta``)."""


def _beta_connectors() -> dict[str, Any]:
    from dvf.connectors.bigquery import BigQueryConnector
    from dvf.connectors.databricks import DatabricksConnector
    from dvf.connectors.redshift import RedshiftConnector
    from dvf.connectors.snowflake import SnowflakeConnector

    return {
        "snowflake": SnowflakeConnector,
        "bigquery": BigQueryConnector,
        "redshift": RedshiftConnector,
        "databricks": DatabricksConnector,
    }
