"""The built-in result sinks, by type name, for registration in the plugin registry."""

from __future__ import annotations

from dvf.plugins.base import ResultSink
from dvf.sinks.duckdb import DuckDBSink
from dvf.sinks.postgres import PostgresSink
from dvf.sinks.sqlite import SQLiteSink

BUILTIN_SINKS: dict[str, type[ResultSink]] = {
    "duckdb": DuckDBSink,
    "sqlite": SQLiteSink,
    "postgres": PostgresSink,
}
"""Sink type (``duckdb``, ``sqlite``, ``postgres``) to sink class.

``postgres`` imports psycopg only when it connects, so the map loads without the extra.
"""

__all__ = ["BUILTIN_SINKS", "DuckDBSink", "PostgresSink", "SQLiteSink"]
