"""SQL engines: ``pushdown`` (diff inside the database), ``duckdb`` (diff locally), and ``auto``."""

from __future__ import annotations

import secrets
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.engines.base import (
    KEY_LIST_LIMIT,
    DiffJob,
    DiffResult,
    DiffSide,
    Engine,
    ProjectionMode,
    Side_,
    SqlDiff,
    collect_phase_a,
    collect_phase_b,
    duplicate_probe_sql,
    key_list_predicate,
    key_match_predicate,
    key_names,
    projected_columns,
    projection_sql,
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
        plan = job.plan
        connector = job.source.connector
        dialect = connector.sql_dialect
        started = time.perf_counter()

        def execute(sql: str) -> list[dict[str, Any]]:
            table = job.recorder.run(connector, sql, Side.BOTH, engine=self.name)
            return table.to_pylist()  # type: ignore[no-any-return]

        rels = {"source": job.source.relation.sql, "target": job.target.relation.sql}
        dedupe = False
        for side, rel in rels.items():
            keys = dialect.subquery(projection_sql(plan, side, rel, "keys"), "dvf_k")  # type: ignore[arg-type]
            if execute(duplicate_probe_sql(key_names(plan), keys, dialect))[0]["dvf_dups"]:
                dedupe = True
        hashed = {
            side: dialect.subquery(projection_sql(plan, side, rel, "hash"), f"dvf_{side[0]}")  # type: ignore[arg-type]
            for side, rel in rels.items()
        }
        phase_a = SqlDiff(plan, dialect, "hash", hashed["source"], hashed["target"], dedupe=dedupe)
        result = collect_phase_a(phase_a, execute, job, self.name)
        if job.mode == "full" and result.summary.mismatched:
            columns = [f"dvf_mk{i}" for i in range(len(plan.keys))]
            preamble = [*phase_a.cte_items(), f"dvf_mk AS ({phase_a.mismatched_keys_select()})"]
            full: dict[str, str] = {}
            for side, rel in rels.items():
                where = key_match_predicate(plan, side, "dvf_mk", columns)  # type: ignore[arg-type]
                full[side] = dialect.subquery(
                    projection_sql(plan, side, rel, "full", where=where),  # type: ignore[arg-type]
                    f"dvf_f{side[0]}",
                )
            phase_b = SqlDiff(
                plan,
                dialect,
                "full",
                full["source"],
                full["target"],
                dedupe=dedupe,
                prefix="b_",
                preamble=preamble,
            )
            collect_phase_b(phase_b, execute, job, result)
        result.timing.compare_ms = _ms(started)
        return result


class DuckDBEngine(Engine):
    """Canonicalizes each side in its own database, streams the projections into a local DuckDB,
    and diffs them there. DuckDB spills to disk, so memory stays bounded.

    Phase A loads keys and row hashes of every row; phase B loads the full values of only the
    mismatched rows, filtered in the source database by a key table (DuckDB-based connectors) or
    a literal key list (up to ``KEY_LIST_LIMIT`` keys).
    """

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
        mode: ProjectionMode,
    ) -> float:
        started = time.perf_counter()
        columns = projected_columns(job.plan, mode)
        schema = pa.schema(
            [(name, pa.float64() if kind == "double" else pa.string()) for name, kind in columns]
        )
        reader = pa.RecordBatchReader.from_batches(
            schema, _conform(side.connector.iter_batches(sql), schema)
        )
        con.register(f"{table}_arrow", reader)
        casts = ", ".join(
            f"CAST({name} AS {'DOUBLE' if kind == 'double' else 'VARCHAR'}) AS {name}"
            for name, kind in columns
        )
        con.execute(f"CREATE TABLE {table} AS SELECT {casts} FROM {table}_arrow")
        con.unregister(f"{table}_arrow")
        elapsed = _ms(started)
        rows = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        job.recorder.add(
            side.connector, sql, label, elapsed, int(rows[0]) if rows else None, engine=self.name
        )
        return elapsed

    def _load_mismatched(
        self,
        con: duckdb.DuckDBPyConnection,
        table: str,
        side: DiffSide,
        which: Side_,
        job: DiffJob,
        keys: pa.Table,
    ) -> float:
        """Load the full projection of the rows whose keys are in ``keys``."""
        plan = job.plan
        label = Side.SOURCE if which == "source" else Side.TARGET
        columns = [f"dvf_mk{i}" for i in range(len(plan.keys))]
        name = f"dvf_mk_{secrets.token_hex(4)}"
        registered = side.connector.register_arrow(name, keys)
        try:
            if registered:
                where: str | None = key_match_predicate(plan, which, name, columns)
            elif keys.num_rows <= KEY_LIST_LIMIT:
                where = key_list_predicate(
                    plan, which, [list(r.values()) for r in keys.to_pylist()]
                )
            else:
                where = None
            sql = projection_sql(plan, which, side.relation.sql, "full", where=where)
            target = table if where is not None else f"{table}_all"
            elapsed = self._load(con, target, side, sql, job, label, "full")
        finally:
            if registered:
                side.connector.unregister_arrow(name)
        if where is None:
            match = " AND ".join(f"m.dvf_mk{i} = r.k{i}" for i in range(len(plan.keys)))
            con.execute(
                f"CREATE TABLE {table} AS SELECT r.* FROM {target} r "
                f"WHERE EXISTS (SELECT 1 FROM dvf_mk m WHERE {match})"
            )
            con.execute(f"DROP TABLE {target}")
        return elapsed

    def diff(self, job: DiffJob) -> DiffResult:
        resources = job.resources
        root = Path(resources.temp_dir) if resources.temp_dir else None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix="dvf-duckdb-", dir=root)
        con = duckdb.connect(":memory:")
        plan = job.plan
        try:
            con.execute(f"SET temp_directory = '{temp_dir.replace(chr(39), chr(39) * 2)}'")
            con.execute("SET preserve_insertion_order = false")
            memory_limit = resources.memory_limit or self.memory_limit
            if memory_limit:
                con.execute(f"SET memory_limit = '{memory_limit}'")
            if resources.threads:
                con.execute(f"SET threads = {int(resources.threads)}")
            source_ms = self._load(
                con,
                "dvf_s",
                job.source,
                projection_sql(plan, "source", job.source.relation.sql, "hash"),
                job,
                Side.SOURCE,
                "hash",
            )
            target_ms = self._load(
                con,
                "dvf_t",
                job.target,
                projection_sql(plan, "target", job.target.relation.sql, "hash"),
                job,
                Side.TARGET,
                "hash",
            )
            local = get_dialect("duckdb")
            started = time.perf_counter()

            def execute(sql: str) -> list[dict[str, Any]]:
                return con.execute(sql).to_arrow_table().to_pylist()  # type: ignore[no-any-return]

            dedupe = any(
                execute(duplicate_probe_sql(key_names(plan), name, local))[0]["dvf_dups"]
                for name in ("dvf_s", "dvf_t")
            )
            phase_a = SqlDiff(plan, local, "hash", "dvf_s", "dvf_t", dedupe=dedupe)
            result = collect_phase_a(phase_a, execute, job, self.name)
            if job.mode == "full" and result.summary.mismatched:
                con.execute(
                    f"CREATE TABLE dvf_mk AS {phase_a.ctes()}{phase_a.mismatched_keys_select()}"
                )
                keys = con.execute("SELECT * FROM dvf_mk").to_arrow_table()
                compare_ms = _ms(started)
                source_ms += self._load_mismatched(con, "dvf_fs", job.source, "source", job, keys)
                target_ms += self._load_mismatched(con, "dvf_ft", job.target, "target", job, keys)
                started = time.perf_counter()
                phase_b = SqlDiff(plan, local, "full", "dvf_fs", "dvf_ft", dedupe=dedupe)
                collect_phase_b(phase_b, execute, job, result)
                result.timing.compare_ms = compare_ms + _ms(started)
            else:
                result.timing.compare_ms = _ms(started)
            result.timing.source_query_ms = source_ms
            result.timing.target_query_ms = target_ms
            return result
        finally:
            con.close()
            if not resources.keep_temp:
                shutil.rmtree(temp_dir, ignore_errors=True)


def is_local(job: DiffJob) -> bool:
    """Both sides are DuckDB-based (files or DuckDB databases): the local engine is fastest."""
    return job.source.connector.dialect == "duckdb" and job.target.connector.dialect == "duckdb"


class AutoEngine(Engine):
    """Pushdown when both datasets share a database connection; the Spark engine when both are
    Spark datasets in one Spark application; otherwise the local DuckDB engine (which is also
    used for DuckDB-based connections, where it avoids repeating work)."""

    name = "auto"

    def diff(self, job: DiffJob) -> DiffResult:
        engine: Engine
        if _both_spark(job):
            from dvf.engines.spark_engine import SparkEngine

            engine = SparkEngine()
        elif job.same_connection and not is_local(job):
            engine = PushdownEngine()
        else:
            engine = DuckDBEngine()
        return engine.diff(job)


def _both_spark(job: DiffJob) -> bool:
    """Both sides are Spark datasets readable from one session (no data leaves Spark)."""
    source, target = job.source.connector, job.target.connector
    if source.dialect != "spark" or target.dialect != "spark":
        return False
    colocated = getattr(source, "colocated", None)
    session = getattr(target, "session", None)
    return bool(colocated is not None and session is not None and colocated(session))


def _conform(batches: Iterator[pa.RecordBatch], schema: pa.Schema) -> Iterator[pa.RecordBatch]:
    """Rename and cast every batch to ``schema`` (drivers infer types batch by batch)."""
    for batch in batches:
        renamed = pa.Table.from_batches([batch]).rename_columns(schema.names)
        yield from renamed.cast(schema).to_batches()
