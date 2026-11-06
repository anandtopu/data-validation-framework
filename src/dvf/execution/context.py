"""Run-wide context: run identity, open connections, and query recording."""

from __future__ import annotations

import logging
import re
import secrets
import time
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


class ConnectorPool:
    """Opens connections on first use and closes them at the end of a run."""

    def __init__(self, config: DvfConfig, registry: Registry, base_dir: Path) -> None:
        self._config = config
        self._registry = registry
        self._base_dir = base_dir
        self._open: dict[str, Connector] = {}

    def get(self, name: str) -> Connector:
        existing = self._open.get(name)
        if existing is not None:
            return existing
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
        connector: Connector = entry.implementation(name, conn_config, self._base_dir)
        connector.open()
        self._open[name] = connector
        return connector

    def close_all(self) -> None:
        for connector in self._open.values():
            try:
                connector.close()
            except Exception:  # closing must never mask a run's own outcome
                log.debug("closing connection %s failed", connector.name, exc_info=True)
        self._open.clear()


@dataclass
class QueryRecorder:
    """Collects the queries one check runs, for reproducibility."""

    records: list[QueryRecord] = field(default_factory=list)
    source_ms: float = 0.0
    target_ms: float = 0.0

    def run(
        self, connector: Connector, sql: str, side: Side, engine: str | None = None
    ) -> pa.Table:
        started = time.perf_counter()
        error: Exception | None = None
        table: pa.Table | None = None
        try:
            table = connector.query(sql)
        except Exception as exc:
            error = exc
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self.add(connector, sql, side, elapsed, None if table is None else table.num_rows, engine)
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
