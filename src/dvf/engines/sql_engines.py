"""SQL engines: ``pushdown`` (diff inside the database), ``duckdb`` (diff locally), and ``auto``."""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterator
from typing import Any

import duckdb
import pyarrow as pa

from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.engines.base import (
    DiffJob,
    DiffResult,
    DiffSide,
    Engine,
    SqlDiff,
    projected_columns,
    projection_sql,
    run_sql_diff,
)
from dvf.sql.dialects import get_dialect


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


class PushdownEngine(Engine):
    """Runs the whole row-level diff as SQL inside one database.

    Both datasets must live on the same connection; nothing but summaries and samples leaves the
    database.
    """

    name = "pushdown"

    def diff(self, job: DiffJob) -> DiffResult:
        if not job.same_connection:
            raise PlanError(
                "Engine 'pushdown' compares rows inside one database, but the source and target are "
                f"on different connections ('{job.source.relation.connection}' and "
                f"'{job.target.relation.connection}').",
                hint="Use engine 'auto' or 'duckdb' to compare across connections.",
            )
        connector = job.source.connector
        dialect = connector.sql_dialect
        source_rel = dialect.subquery(
            projection_sql(job.plan, "source", job.source.relation.sql, job.mode), "dvf_s"
        )
        target_rel = dialect.subquery(
            projection_sql(job.plan, "target", job.target.relation.sql, job.mode), "dvf_t"
        )
        builder = SqlDiff(job.plan, dialect, job.mode, source_rel, target_rel)
        started = time.perf_counter()

        def execute(sql: str) -> list[dict[str, Any]]:
            table = job.recorder.run(connector, sql, Side.BOTH, engine=self.name)
            return table.to_pylist()  # type: ignore[no-any-return]

        result = run_sql_diff(builder, execute, job, self.name)
        result.timing.compare_ms = _ms(started)
        return result


class DuckDBEngine(Engine):
    """Canonicalizes each side in its own database, streams the projections into a local DuckDB,
    and diffs them there. DuckDB spills to disk, so memory stays bounded."""

    name = "duckdb"

    def __init__(self, memory_limit: str | None = None) -> None:
        self.memory_limit = memory_limit

    def _load(
        self,
        con: duckdb.DuckDBPyConnection,
        table: str,
        side: DiffSide,
        sql: str,
        job: DiffJob,
        label: Side,
    ) -> float:
        started = time.perf_counter()
        schema = pa.schema(
            [
                (name, pa.float64() if kind == "double" else pa.string())
                for name, kind in projected_columns(job.plan, job.mode)
            ]
        )
        reader = pa.RecordBatchReader.from_batches(
            schema, _conform(side.connector.iter_batches(sql), schema)
        )
        con.register(f"{table}_arrow", reader)
        casts = ", ".join(
            f"CAST({name} AS {'DOUBLE' if kind == 'double' else 'VARCHAR'}) AS {name}"
            for name, kind in projected_columns(job.plan, job.mode)
        )
        con.execute(f"CREATE TABLE {table} AS SELECT {casts} FROM {table}_arrow")
        con.unregister(f"{table}_arrow")
        elapsed = _ms(started)
        rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        job.recorder.add(side.connector, sql, label, elapsed, int(rows[0]) if rows else None)
        return elapsed

    def diff(self, job: DiffJob) -> DiffResult:
        temp_dir = tempfile.mkdtemp(prefix="dvf-duckdb-")
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"SET temp_directory = '{temp_dir.replace(chr(39), chr(39) * 2)}'")
            con.execute("SET preserve_insertion_order = false")
            if self.memory_limit:
                con.execute(f"SET memory_limit = '{self.memory_limit}'")
            source_ms = self._load(
                con,
                "dvf_s",
                job.source,
                projection_sql(job.plan, "source", job.source.relation.sql, job.mode),
                job,
                Side.SOURCE,
            )
            target_ms = self._load(
                con,
                "dvf_t",
                job.target,
                projection_sql(job.plan, "target", job.target.relation.sql, job.mode),
                job,
                Side.TARGET,
            )
            builder = SqlDiff(job.plan, get_dialect("duckdb"), job.mode, "dvf_s", "dvf_t")
            started = time.perf_counter()

            def execute(sql: str) -> list[dict[str, Any]]:
                return con.execute(sql).to_arrow_table().to_pylist()  # type: ignore[no-any-return]

            result = run_sql_diff(builder, execute, job, self.name)
            result.timing.source_query_ms = source_ms
            result.timing.target_query_ms = target_ms
            result.timing.compare_ms = _ms(started)
            return result
        finally:
            con.close()
            shutil.rmtree(temp_dir, ignore_errors=True)


class AutoEngine(Engine):
    """Pushdown when both datasets share a connection, otherwise the local DuckDB engine."""

    name = "auto"

    def diff(self, job: DiffJob) -> DiffResult:
        engine: Engine = PushdownEngine() if job.same_connection else DuckDBEngine()
        return engine.diff(job)


def _conform(batches: Iterator[pa.RecordBatch], schema: pa.Schema) -> Iterator[pa.RecordBatch]:
    """Rename and cast every batch to ``schema`` (drivers infer types batch by batch)."""
    for batch in batches:
        renamed = pa.Table.from_batches([batch]).rename_columns(schema.names)
        yield from renamed.cast(schema).to_batches()
