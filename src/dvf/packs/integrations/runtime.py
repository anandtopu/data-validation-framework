"""The runner's single entry point to notifications, OpenLineage, OpenTelemetry, and triage.

:class:`RunIntegrations` is created once per run. Every method is safe to call when the features
are disabled (they do nothing) and never raises: an integration that fails is logged, and the run
carries on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from dvf.packs.integrations.dispatch import Delivery, notify_crash, notify_run
from dvf.packs.integrations.lineage import LineageEmitter
from dvf.packs.integrations.messages import MessageContext
from dvf.packs.integrations.telemetry import TableSpan, Telemetry, create_telemetry
from dvf.packs.integrations.triage import report_section, run_triage
from dvf.security.redaction import global_redactor

if TYPE_CHECKING:
    from dvf.config.models import DvfConfig
    from dvf.execution.context import RunInfo
    from dvf.models.results import ArtifactRef, DatasetPairResult, RunResult, SuiteResult
    from dvf.plugins.registry import Registry
    from dvf.reporting.base import ReportSection

log = logging.getLogger(__name__)


class TableScope:
    """Handed to the runner for one table; call :meth:`finished` with the table's result."""

    def __init__(self, owner: RunIntegrations, suite: str, table: str, span: TableSpan) -> None:
        self._owner = owner
        self.suite = suite
        self.table = table
        self.span = span
        self.done = False

    def finished(self, dataset: DatasetPairResult) -> None:
        self.done = True
        owner = self._owner
        owner._guard("telemetry", owner.telemetry.table_finished, self.span, dataset)
        owner._guard("lineage", owner.lineage.table_finished, self.suite, dataset)


class RunIntegrations:
    """Notifications, lineage events, telemetry, and triage for one run."""

    def __init__(
        self,
        config: DvfConfig,
        registry: Registry,
        base_dir: Path,
        info: RunInfo,
        *,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.base_dir = base_dir
        self.info = info
        self.lineage = LineageEmitter(config, info, base_dir)
        self._telemetry = telemetry
        self.deliveries: list[Delivery] = []
        self._sections: list[ReportSection] = []

    @property
    def telemetry(self) -> Telemetry:
        if self._telemetry is None:
            self._telemetry = create_telemetry(self.config.telemetry)
        return self._telemetry

    @staticmethod
    def _guard(feature: str, func: object, *args: object) -> None:
        try:
            func(*args)  # type: ignore[operator]
        except Exception as exc:  # integrations must never fail a run
            log.warning(
                "%s integration failed: %s",
                feature,
                global_redactor().redact_text(f"{type(exc).__name__}: {exc}"),
            )

    # ------------------------------------------------------------------ run
    @contextmanager
    def run_scope(self) -> Iterator[None]:
        """Wrap the whole run: the run span, the START event, and FAIL handling on a crash."""
        with self.telemetry.run_scope(self.info, self.config.project.name):
            self._guard("lineage", self.lineage.run_started)
            try:
                yield
            except BaseException as exc:
                self._guard("lineage", self.lineage.run_crashed, exc)
                if isinstance(exc, Exception):
                    self._guard("notifications", self._notify_crash, exc)
                raise

    def _notify_crash(self, exc: Exception) -> None:
        context = MessageContext.for_run(self.config, self.info)
        self.deliveries = notify_crash(
            self.info.run_id,
            f"{type(exc).__name__}: {exc}",
            self.config,
            self.registry,
            context,
            self.base_dir,
            started_at=self.info.started_at,
        )

    def suite_started(self, suite: str) -> None:
        self._guard("telemetry", self.telemetry.suite_started, suite)

    def suite_finished(self, result: SuiteResult) -> None:
        self._guard("telemetry", self.telemetry.suite_finished, result)

    @contextmanager
    def table_scope(self, suite: str, table: str) -> Iterator[TableScope]:
        """Wrap one table's work (in its worker thread): its span and child lineage job."""
        self._guard("lineage", self.lineage.table_started, suite, table)
        failed = False
        with self.telemetry.table_scope(suite, table) as span:
            scope = TableScope(self, suite, table, span)
            try:
                yield scope
            except BaseException:
                failed = True
                raise
            finally:
                if failed and not scope.done:
                    self._guard("lineage", self.lineage.table_finished, suite, None, table)

    def before_reports(self, result: RunResult) -> None:
        """Run triage plugins (when opted in) and keep their report section."""
        try:
            suggestions = run_triage(result, self.config, self.registry)
        except Exception as exc:
            log.warning("triage failed: %s", exc)
            suggestions = []
        section = report_section(suggestions)
        self._sections = [section] if section is not None else []

    def report_sections(self) -> list[ReportSection]:
        return list(self._sections)

    def run_finished(
        self,
        result: RunResult,
        output_dir: Path | None = None,
        artifacts: Sequence[ArtifactRef] = (),
    ) -> list[Delivery]:
        """After results and reports exist: notifications, the final lineage event, and run
        telemetry. Returns the notification deliveries."""
        self._guard("telemetry", self.telemetry.run_finished, result)
        self._guard("lineage", self.lineage.run_finished, result)
        try:
            context = MessageContext.for_run(self.config, self.info, output_dir, artifacts)
            self.deliveries = notify_run(result, self.config, self.registry, context, self.base_dir)
        except Exception as exc:  # notifications must never fail a run
            log.warning("notifications failed: %s", global_redactor().redact_text(str(exc)))
            self.deliveries = []
        return self.deliveries


__all__ = ["RunIntegrations", "TableScope"]
