"""Spark pack: the ``spark`` connector and engine (the ``spark`` dialect is in :mod:`dvf.sql.spark`).

Both are verified on a local Spark session (Apache Spark 4.3 in local mode): the parity tests
hold the dialect to the Python reference, and the seeded-defect harness runs on the engine. They
register as ready. Cluster masters and Databricks Connect use the same code but are not covered
by dvf's tests (see ``docs/spark.md``).

``register`` is called by :func:`dvf.packs.register_packs`. Nothing here imports pyspark: the
connector imports it when a connection opens.
"""

from __future__ import annotations

from typing import Any

from dvf.packs import attach
from dvf.plugins.registry import PluginKind, Registry


def _connector() -> Any:
    from dvf.connectors.spark import SparkConnector

    return SparkConnector


def _engine() -> Any:
    from dvf.engines.spark_engine import SparkEngine

    return SparkEngine


def register(registry: Registry) -> None:
    """Attach the Spark connector and engine to their built-in declarations."""
    attach(registry, PluginKind.CONNECTOR, "spark", _connector())
    attach(registry, PluginKind.ENGINE, "spark", _engine())
