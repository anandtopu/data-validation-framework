"""Catalog pack: the remaining catalog entries for 1.0 (sample comparison, schema checks SCH-07 to SCH-14, sinks, generic connectors).

Owned by the Phase 4 catalog completeness work. ``register`` is called by :func:`dvf.packs.register_packs`.

* comparator ``sample`` (REC-07): :mod:`dvf.packs.catalog.sample`;
* sinks ``delta`` and ``snowflake`` (beta): :mod:`dvf.packs.catalog.sinks`;
* connectors ``sqlalchemy`` and ``odbc``: :mod:`dvf.connectors.sqlalchemy`, :mod:`dvf.connectors.odbc`;
* catalog metadata for schema checks SCH-07, SCH-09, and SCH-10: :mod:`dvf.packs.catalog.introspect`
  (the checks themselves are in :mod:`dvf.checks.schema`).

No optional driver is imported here: SQLAlchemy, pyodbc, deltalake, and the Snowflake connector
are imported when a connection opens or a sink writes.
"""

from __future__ import annotations

from dvf.packs import attach
from dvf.plugins.registry import PluginKind, Registry


def register(registry: Registry) -> None:
    """Attach this pack's implementations and register its new components."""
    from dvf.connectors.odbc import OdbcConfig, OdbcConnector
    from dvf.connectors.sqlalchemy import SqlAlchemyConfig, SqlAlchemyConnector
    from dvf.packs.catalog.sample import SampleComparator
    from dvf.packs.catalog.sinks import DeltaSink, SnowflakeSink

    attach(registry, PluginKind.COMPARATOR, "sample", SampleComparator)
    attach(registry, PluginKind.SINK, "delta", DeltaSink)
    attach(registry, PluginKind.SINK, "snowflake", SnowflakeSink, maturity="beta")
    attach(
        registry,
        PluginKind.CONNECTOR,
        "sqlalchemy",
        SqlAlchemyConnector,
        config_model=SqlAlchemyConfig,
        extra="sqlalchemy",
    )
    attach(
        registry,
        PluginKind.CONNECTOR,
        "odbc",
        OdbcConnector,
        config_model=OdbcConfig,
        extra="odbc",
    )
