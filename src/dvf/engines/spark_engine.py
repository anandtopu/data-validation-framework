"""The ``spark`` engine: the two-phase row diff computed in Spark.

Each side is projected first (canonical keys, row hashes, tolerance columns; see
:mod:`dvf.engines.base`):

* A side on a Spark connection that shares the engine's Spark application is projected in Spark
  SQL, in that connection's own session, with no data movement. Two Spark sides on one
  application are therefore compared in a single Spark SQL diff.
* Any other side's projection runs in its own database, streams to a Parquet file in a temporary
  folder (``defaults.execution.temp_dir``), and Spark reads the file. Only canonical keys and
  hashes move in phase A.

Each projection is cached by Spark (memory, spilling to disk) and exposed as a global temporary
view, so the same :class:`~dvf.engines.base.SqlDiff` statements the other engines use run in the
``spark`` dialect. Phase B fetches full values only for the rows phase A found different: Spark
sides filter by a semi-join with the mismatched keys; other sides filter in their database by a
key table (when the connector can receive one) or a literal key list.

The diff runs in the session of the first Spark side, or, with no Spark side, in a child of the
running Spark session (a ``local[*]`` session when none is running). Every dvf session enables
adaptive query execution with skew-join handling; set ``spark.sql.shuffle.partitions`` and other
settings in the Spark connection's ``conf``.

Staging local files requires a local master (``local[...]``): on a cluster, set
``defaults.execution.temp_dir`` to a folder every executor can read under the same path, or the
other side's projection is sent through the driver instead (``createDataFrame``).
"""

from __future__ import annotations

import importlib
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import pyarrow as pa

from dvf.connectors.spark import (
    SparkConnector,
    engine_session,
    global_temp_database,
    is_connect,
    keep_active_session,
    spark_errors,
)
from dvf.connectors.staged import write_parquet_stream
from dvf.core.enums import Side
from dvf.core.errors import ConnectorError
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


class SparkEngine(Engine):
    """Distributed two-phase diff in Spark (local, cluster, or the running session)."""

    name = "spark"

    def diff(self, job: DiffJob) -> DiffResult:
        run = _SparkDiff(job, self.name)
        try:
            return run.execute()
        finally:
            run.cleanup()


class _SparkDiff:
    """One diff: the Spark views it creates and the files it stages, cleaned up at the end."""

    def __init__(self, job: DiffJob, engine: str) -> None:
        self.job = job
        self.engine = engine
        self.host = _host(job)
        self.session = self.host.session
        self.database = global_temp_database(self.session)
        self.dialect = get_dialect("spark")
        self.token = secrets.token_hex(4)
        self.views: list[str] = []
        self.cached: list[Any] = []
        self.temp_dir: str | None = None
        self.rows_transferred = 0
        self.staged: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    def execute_sql(self, sql: str) -> list[dict[str, Any]]:
        table = self.job.recorder.run(self.host, sql, Side.BOTH, engine=self.engine)
        return table.to_pylist()  # type: ignore[no-any-return]

    def qualified(self, view: str) -> str:
        return f"{self.dialect.quote(self.database)}.{self.dialect.quote(view)}"

    def publish(self, df: Any, view: str) -> tuple[str, int]:
        """Cache ``df``, expose it as a global temporary view, and count its rows."""
        storage = importlib.import_module("pyspark").StorageLevel
        try:
            cached = df.persist(storage.MEMORY_AND_DISK)
            self.cached.append(cached)
            cached.createOrReplaceGlobalTempView(view)
            self.views.append(view)
            rows = int(cached.count())
        except spark_errors() as exc:
            raise ConnectorError(
                f"Spark could not compute the projection '{view}': {exc}".splitlines()[0]
            ) from exc
        return self.qualified(view), rows

    def colocated(self, side: DiffSide) -> bool:
        connector = side.connector
        return isinstance(connector, SparkConnector) and connector.colocated(self.session)

    def folder(self) -> Path:
        if self.temp_dir is None:
            root = self.job.resources.temp_dir
            if root is not None:
                Path(root).mkdir(parents=True, exist_ok=True)
            self.temp_dir = tempfile.mkdtemp(prefix="dvf-spark-", dir=root)
        return Path(self.temp_dir)

    # ------------------------------------------------------------------ sides
    def project(
        self,
        side: DiffSide,
        which: Side_,
        mode: ProjectionMode,
        where: str | None = None,
        view: str | None = None,
    ) -> tuple[str, float]:
        """A Spark view of ``side``'s projection; returns its name and the time it took."""
        view = view or f"dvf_{which[0]}{mode[0]}_{self.token}"
        sql = projection_sql(self.job.plan, which, side.relation.sql, mode, where=where)
        label = Side.SOURCE if which == "source" else Side.TARGET
        started = time.perf_counter()
        if self.colocated(side):
            connector = side.connector
            assert isinstance(connector, SparkConnector)
            name, rows = self.publish(connector.frame(sql), view)
        else:
            name, rows = self.stage(side, sql, view, mode)
            self.rows_transferred += rows
            self.staged[f"{which}_{mode}"] = rows
        elapsed = _ms(started)
        self.job.recorder.add(side.connector, sql, label, elapsed, rows, engine=self.engine)
        return name, elapsed

    def stage(self, side: DiffSide, sql: str, view: str, mode: ProjectionMode) -> tuple[str, int]:
        """Stream ``sql`` from the side's own database into Spark."""
        columns = projected_columns(self.job.plan, mode)
        schema = pa.schema(
            [(name, pa.float64() if kind == "double" else pa.string()) for name, kind in columns]
        )
        batches = side.connector.iter_batches(sql)
        if is_connect(self.session) or not _local(self.session, self.job):
            return self.publish(self.through_driver(batches, schema), view)
        path = self.folder() / f"{view}.parquet"
        write_parquet_stream(path, schema, batches)
        try:
            df = self.session.read.parquet(path.absolute().as_uri())
        except spark_errors() as exc:
            raise ConnectorError(f"Spark could not read the staged file {path}: {exc}") from exc
        return self.publish(df, view)

    def through_driver(self, batches: Any, schema: pa.Schema) -> Any:
        """A DataFrame of ``batches`` built on the driver, a chunk at a time."""
        with keep_active_session(self.session):
            return self._through_driver(batches, schema)

    def _through_driver(self, batches: Any, schema: pa.Schema) -> Any:
        frames: list[Any] = []
        chunk: list[pa.RecordBatch] = []
        size = 0
        for batch in batches:
            table = pa.Table.from_batches([batch]).rename_columns(schema.names).cast(schema)
            chunk.extend(table.to_batches())
            size += table.num_rows
            if size >= 500_000:
                frames.append(self.session.createDataFrame(pa.Table.from_batches(chunk, schema)))
                chunk, size = [], 0
        if chunk or not frames:
            frames.append(self.session.createDataFrame(pa.Table.from_batches(chunk, schema)))
        df = frames[0]
        for frame in frames[1:]:
            df = df.unionByName(frame)
        return df

    def project_mismatched(self, side: DiffSide, which: Side_, keys_view: str) -> float:
        """Phase B: the full projection of the rows whose keys are in ``keys_view``."""
        plan = self.job.plan
        columns = [f"dvf_mk{i}" for i in range(len(plan.keys))]
        if self.colocated(side):
            semi_join = key_match_predicate(plan, which, keys_view, columns)
            return self.project(side, which, "full", semi_join)[1]
        keys = self.host.query(f"SELECT * FROM {keys_view}")
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
            # Without a filter every row comes across, and Spark keeps the mismatched ones.
            unfiltered = f"dvf_{which[0]}a_{self.token}" if where is None else None
            view, elapsed = self.project(side, which, "full", where, unfiltered)
        finally:
            if registered:
                side.connector.unregister_arrow(name)
        if where is None:
            match = " AND ".join(f"r.k{i} = m.dvf_mk{i}" for i in range(len(plan.keys)))
            filtered = self.host.frame(
                f"SELECT r.* FROM {view} r LEFT SEMI JOIN {keys_view} m ON {match}"
            )
            self.publish(filtered, f"dvf_{which[0]}f_{self.token}")
        return elapsed

    # ------------------------------------------------------------------ diff
    def execute(self) -> DiffResult:
        job, plan = self.job, self.job.plan
        source_view, source_ms = self.project(job.source, "source", "hash")
        target_view, target_ms = self.project(job.target, "target", "hash")
        started = time.perf_counter()
        dedupe = any(
            self.execute_sql(duplicate_probe_sql(key_names(plan), view, self.dialect))[0][
                "dvf_dups"
            ]
            for view in (source_view, target_view)
        )
        phase_a = SqlDiff(plan, self.dialect, "hash", source_view, target_view, dedupe=dedupe)
        result = collect_phase_a(phase_a, self.execute_sql, job, self.engine)
        compare_ms = _ms(started)
        if job.mode == "full" and result.summary.mismatched:
            keys_view, _ = self.publish(
                self.host.frame(phase_a.ctes() + phase_a.mismatched_keys_select()),
                f"dvf_mk_{self.token}",
            )
            source_ms += self.project_mismatched(job.source, "source", keys_view)
            target_ms += self.project_mismatched(job.target, "target", keys_view)
            started = time.perf_counter()
            phase_b = SqlDiff(
                plan,
                self.dialect,
                "full",
                self.qualified(f"dvf_sf_{self.token}"),
                self.qualified(f"dvf_tf_{self.token}"),
                dedupe=dedupe,
            )
            collect_phase_b(phase_b, self.execute_sql, job, result)
            compare_ms += _ms(started)
        result.timing.source_query_ms = source_ms
        result.timing.target_query_ms = target_ms
        result.timing.compare_ms = compare_ms
        result.details["rows_transferred"] = self.rows_transferred
        if self.staged:
            result.details["spark_staged_rows"] = dict(self.staged)
        return result

    def cleanup(self) -> None:
        for view in self.views:
            try:
                self.session.catalog.dropGlobalTempView(view)
            except spark_errors():
                continue
        for df in self.cached:
            try:
                df.unpersist(blocking=True)
            except spark_errors():
                continue
        if self.temp_dir is not None and not self.job.resources.keep_temp:
            _remove_tree(Path(self.temp_dir))


def _remove_tree(folder: Path, attempts: int = 6) -> None:
    """Delete staged files. On Windows the JVM can hold a file open for a moment after Spark
    has finished with it, so deletion is retried briefly before giving up silently."""
    for attempt in range(attempts):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(0.25 * (attempt + 1))


def _host(job: DiffJob) -> SparkConnector:
    """The connector whose session runs the diff: the first Spark side's, else dvf's engine
    session on the running (or a new local) Spark application."""
    for side in (job.source, job.target):
        if isinstance(side.connector, SparkConnector):
            return side.connector
    return SparkConnector.for_session(engine_session(), "spark-engine")


def _local(session: Any, job: DiffJob) -> bool:
    """Whether executors can read files staged on this machine (a local master, or a shared
    ``temp_dir`` the user configured)."""
    try:
        master = str(session.sparkContext.master)
    except Exception:  # Spark Connect has no SparkContext here
        return False
    return master.startswith("local") or job.resources.temp_dir is not None


__all__ = ["SparkEngine"]
