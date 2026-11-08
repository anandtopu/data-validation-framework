"""Run-wide context: run identity, open connections, and query recording."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from dvf.core.enums import Side
from dvf.core.errors import ConnectorError
from dvf.models.results import QueryRecord
from dvf.plugins.registry import PluginKind, Registry

if TYPE_CHECKING:
    from dvf.config.models import DvfConfig
    from dvf.plugins.base import Connector

log = logging.getLogger(__name__)
_RUN_PLACEHOLDER = re.compile(r"\$\{(run\.[a-z_]+)\}")


def new_run_id(now: datetime | None = None) -> str:
    """A sortable, unique run ID such as ``20261106T101500Z-3f9a``."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


@dataclass(frozen=True)
class RunInfo:
    """Identity of a run, used for ``${run.*}`` placeholders."""

    run_id: str
    started_at: datetime
    profile: str | None = None

    @property
    def placeholders(self) -> dict[str, str]:
        started = self.started_at.astimezone(UTC)
        return {
            "run.id": self.run_id,
            "run.date": started.strftime("%Y-%m-%d"),
            "run.timestamp": started.strftime("%Y%m%dT%H%M%SZ"),
            "run.profile": self.profile or "",
        }

    def render(self, text: str) -> str:
        """Replace ``${run.id}``, ``${run.date}``, ``${run.timestamp}``, and ``${run.profile}``."""
        values = self.placeholders
        return _RUN_PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


@dataclass(frozen=True)
class RetryPolicy:
    """Retries of queries that failed with a transient error, with exponential backoff."""

    retries: int = 0
    backoff_s: float = 2.0

    def delay(self, attempt: int) -> float:
        return float(self.backoff_s * 2**attempt)


class ConnectorPool:
    """Opens connections on first use, gives each worker thread its own session, caps concurrent
    use per connection, and closes everything at the end of a run.

    Outside :meth:`lease`, :meth:`get` returns one shared session per connection (planning and
    sequential use). Inside a lease, each thread gets its own session, forked from that shared
    one, and the lease holds a slot of every connection the task needs: at most
    ``max_parallel_queries`` (or the execution default) tasks use a connection at the same time.
    """

    def __init__(
        self,
        config: DvfConfig,
        registry: Registry,
        base_dir: Path,
        default_cap: int | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._base_dir = base_dir
        self._default_cap = default_cap or config.defaults.execution.max_queries_per_connection
        self._open: dict[str, Connector] = {}
        self._idle: dict[str, list[Connector]] = {}
        self._forks: list[Connector] = []
        self._caps: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.RLock()
        self._local = threading.local()

    # ------------------------------------------------------------------ shared sessions
    def _create(self, name: str) -> Connector:
        conn_config = self._config.connections.get(name)
        if conn_config is None:
            raise ConnectorError(f"Unknown connection '{name}'.")
        entry = self._registry.get(PluginKind.CONNECTOR, conn_config.type)
        if not entry.ready:
            extra = f" It is planned with 'pip install dvf[{entry.extra}]'." if entry.extra else ""
            raise ConnectorError(
                f"Connector type '{conn_config.type}' (connection '{name}') is declared but not "
                f"implemented in this version.{extra}",
                hint="Run 'dvf plugins list --kind connector' to see the ready connectors.",
            )
        execution = self._config.defaults.execution
        if conn_config.query_timeout_s is None:
            conn_config = conn_config.model_copy(
                update={"query_timeout_s": execution.query_timeout_s}
            )
        connector: Connector = entry.implementation(name, conn_config, self._base_dir)
        connector.retry_policy = self.retry_policy
        if execution.temp_dir and hasattr(connector, "temp_root"):
            # Staged and converted files (NoSQL, RFC, Avro, Excel, ...) go to the run's temp dir.
            root = Path(execution.temp_dir)
            connector.temp_root = str(root if root.is_absolute() else self._base_dir / root)
            Path(connector.temp_root).mkdir(parents=True, exist_ok=True)
        connector.open()
        return connector

    @property
    def retry_policy(self) -> RetryPolicy:
        execution = self._config.defaults.execution
        return RetryPolicy(execution.retries, execution.retry_backoff_s)

    def _shared(self, name: str) -> Connector:
        with self._lock:
            existing = self._open.get(name)
            if existing is None:
                existing = self._create(name)
                self._open[name] = existing
            return existing

    def get(self, name: str) -> Connector:
        """The session for ``name``: this thread's leased session inside :meth:`lease`, else the
        shared one."""
        leases: dict[str, Connector] | None = getattr(self._local, "leases", None)
        if leases is None:
            return self._shared(name)
        session = leases.get(name)
        if session is None:
            session = self._checkout(name)
            leases[name] = session
        return session

    # ------------------------------------------------------------------ leases
    def cap(self, name: str) -> int:
        conn = self._config.connections.get(name)
        own = conn.max_parallel_queries if conn is not None else None
        return own or self._default_cap

    def _semaphore(self, name: str) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._caps.get(name)
            if sem is None:
                sem = threading.BoundedSemaphore(self.cap(name))
                self._caps[name] = sem
            return sem

    def _checkout(self, name: str) -> Connector:
        shared = self._shared(name)
        with self._lock:
            idle = self._idle.setdefault(name, [])
            if idle:
                return idle.pop()
            session = shared.fork()
        session.retry_policy = self.retry_policy
        session.open()
        with self._lock:
            self._forks.append(session)
        return session

    @contextmanager
    def lease(self, names: Iterable[str]) -> Iterator[None]:
        """Hold a slot of each named connection for the current thread (sorted, so tasks never
        deadlock) and give the thread its own sessions."""
        wanted = sorted({n for n in names if n in self._config.connections})
        acquired: list[str] = []
        leases: dict[str, Connector] = {}
        try:
            for name in wanted:
                self._semaphore(name).acquire()
                acquired.append(name)
            self._local.leases = leases
            yield
        finally:
            self._local.leases = None
            with self._lock:
                for name, session in leases.items():
                    self._idle.setdefault(name, []).append(session)
            for name in reversed(acquired):
                self._semaphore(name).release()

    def close_all(self) -> None:
        with self._lock:
            sessions = [*self._forks, *self._open.values()]
            self._forks.clear()
            self._idle.clear()
            self._open.clear()
        for connector in sessions:
            try:
                connector.close()
            except Exception:  # closing must never mask a run's own outcome
                log.debug("closing connection %s failed", connector.name, exc_info=True)


@dataclass
class QueryRecorder:
    """Collects the queries one check runs, for reproducibility."""

    records: list[QueryRecord] = field(default_factory=list)
    source_ms: float = 0.0
    target_ms: float = 0.0

    def run(
        self, connector: Connector, sql: str, side: Side, engine: str | None = None
    ) -> pa.Table:
        """Run ``sql``, retrying transient errors (lost connections, deadlocks) with backoff."""
        started = time.perf_counter()
        error: Exception | None = None
        table: pa.Table | None = None
        policy = connector.retry_policy
        attempts = 1 + (policy.retries if policy is not None else 0)
        attempt = 0
        while True:
            try:
                table = connector.query(sql)
                break
            except Exception as exc:
                if policy is None or attempt + 1 >= attempts or not connector.is_transient(exc):
                    error = exc
                    break
                delay = policy.delay(attempt)
                log.warning(
                    "transient error on connection %s, retrying in %.1fs (attempt %d of %d): %s",
                    connector.name,
                    delay,
                    attempt + 2,
                    attempts,
                    exc,
                )
                try:
                    connector.close()  # the next query reconnects
                except Exception:
                    log.debug("closing %s after a transient error failed", connector.name)
                time.sleep(delay)
                attempt += 1
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self.add(connector, sql, side, elapsed, None if table is None else table.num_rows, engine)
        if attempt:
            self.records[-1].parameters["attempts"] = attempt + 1
        if error is not None:
            raise error
        assert table is not None
        return table

    def add(
        self,
        connector: Connector,
        sql: str,
        side: Side,
        duration_ms: float,
        rows: int | None,
        engine: str | None = None,
    ) -> None:
        self.records.append(
            QueryRecord(
                side=side,
                dialect=connector.dialect,
                sql=sql.strip(),
                engine=engine or connector.dialect,
                duration_ms=duration_ms,
                rows_returned=rows,
            )
        )
        if side is Side.TARGET:
            self.target_ms += duration_ms
        else:
            self.source_ms += duration_ms
