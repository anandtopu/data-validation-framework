"""OpenTelemetry traces and metrics of dvf runs (``dvf-framework[otel]``).

Spans: ``dvf.run`` > ``dvf.suite`` > ``dvf.table`` > ``dvf.check``. Table spans are current while
the table runs, so spans of instrumented database drivers nest under them; check spans are
recorded when the table finishes, with the checks' own start and end times.

Metrics: rows scanned and rows per second per table, query duration per side, query retries,
checks by status, and run duration. Attributes are low-cardinality (no check IDs on metrics).

dvf uses the global tracer and meter providers, so exporters (OTLP and others) are configured the
usual OpenTelemetry way, for example by ``opentelemetry-instrument`` from ``OTEL_*`` variables.
``exporter: console`` instead creates private SDK providers that print to standard error. When
telemetry is disabled, or ``opentelemetry-api`` is not installed, every method is a no-op.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dvf import __version__
from dvf.core.enums import Status

if TYPE_CHECKING:
    from dvf.config.models import TelemetryConfig
    from dvf.execution.context import RunInfo
    from dvf.models.results import CheckResult, DatasetPairResult, RunResult, SuiteResult

log = logging.getLogger(__name__)

INSTRUMENTATION_NAME = "dvf"


class TableSpan:
    """The span of one table, handed to :meth:`Telemetry.table_finished`."""

    def __init__(self, suite: str, table: str, span: Any = None) -> None:
        self.suite = suite
        self.table = table
        self.span = span


class Telemetry:
    """No-op telemetry: the interface used by the runner integration."""

    enabled = False

    @contextmanager
    def run_scope(self, info: RunInfo, project: str) -> Iterator[None]:
        yield

    def suite_started(self, suite: str) -> None:
        """Start a suite span."""

    def suite_finished(self, result: SuiteResult) -> None:
        """End a suite span."""

    @contextmanager
    def table_scope(self, suite: str, table: str) -> Iterator[TableSpan]:
        yield TableSpan(suite, table)

    def table_finished(self, handle: TableSpan, dataset: DatasetPairResult) -> None:
        """Record the table's checks and metrics."""

    def run_finished(self, result: RunResult) -> None:
        """Record run attributes and metrics."""

    def shutdown(self) -> None:
        """Flush and close providers dvf created."""


def _attributes(**values: Any) -> dict[str, Any]:
    return {
        k.replace("__", "."): v
        for k, v in values.items()
        if v is not None and isinstance(v, str | bool | int | float)
    }


def _ns(moment: datetime | None) -> int | None:
    return None if moment is None else int(moment.timestamp() * 1_000_000_000)


def _global_providers() -> tuple[Any, Any]:
    from opentelemetry import metrics, trace

    return trace.get_tracer_provider(), metrics.get_meter_provider()


def _console_providers(service_name: str) -> tuple[Any, Any]:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    resource = Resource.create({"service.name": service_name, "service.version": __version__})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    reader = PeriodicExportingMetricReader(
        ConsoleMetricExporter(out=sys.stderr), export_interval_millis=60_000
    )
    return tracer_provider, MeterProvider(resource=resource, metric_readers=[reader])


class OtelTelemetry(Telemetry):
    """Records spans and metrics through OpenTelemetry providers."""

    enabled = True

    def __init__(
        self, tracer_provider: Any, meter_provider: Any | None, *, owned: bool = False
    ) -> None:
        from opentelemetry import context, trace
        from opentelemetry.trace import Status as SpanStatus
        from opentelemetry.trace import StatusCode

        self._trace = trace
        self._context = context
        self._span_status = SpanStatus
        self._status_code = StatusCode
        self._providers = (tracer_provider, meter_provider)
        self._owned = owned
        self.tracer = tracer_provider.get_tracer(INSTRUMENTATION_NAME, __version__)
        self._run_span: Any = None
        self._run_context: Any = None
        self._suites: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._instruments: dict[str, Any] = {}
        if meter_provider is not None:
            meter = meter_provider.get_meter(INSTRUMENTATION_NAME, __version__)
            self._instruments = {
                "rows": meter.create_counter(
                    "dvf.rows.scanned", unit="{row}", description="Rows in the validated datasets"
                ),
                "throughput": meter.create_histogram(
                    "dvf.table.rows_per_second",
                    unit="{row}/s",
                    description="Rows scanned per second, per table",
                ),
                "query": meter.create_histogram(
                    "dvf.query.duration", unit="ms", description="Query time per side"
                ),
                "retries": meter.create_counter(
                    "dvf.query.retries",
                    unit="{retry}",
                    description="Queries retried after a transient error",
                ),
                "checks": meter.create_counter(
                    "dvf.checks", unit="{check}", description="Checks by status and type"
                ),
                "run": meter.create_histogram(
                    "dvf.run.duration", unit="ms", description="Run duration"
                ),
            }

    # ------------------------------------------------------------------ helpers
    def _set_status(self, span: Any, status: Status, description: str | None = None) -> None:
        span.set_attribute("dvf.status", status.value)
        if status is Status.ERROR:
            span.set_status(self._span_status(self._status_code.ERROR, description))

    def _metric(self, name: str, value: float, attributes: dict[str, Any] | None = None) -> None:
        instrument = self._instruments.get(name)
        if instrument is None:
            return
        if hasattr(instrument, "add"):
            instrument.add(value, attributes or {})
        else:
            instrument.record(value, attributes or {})

    # ------------------------------------------------------------------ run
    @contextmanager
    def run_scope(self, info: RunInfo, project: str) -> Iterator[None]:
        span = self.tracer.start_span(
            "dvf.run",
            attributes=_attributes(
                dvf__run_id=info.run_id, dvf__project=project, dvf__profile=info.profile
            ),
        )
        self._run_span = span
        self._run_context = self._trace.set_span_in_context(span)
        token = self._context.attach(self._run_context)
        try:
            yield
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(self._span_status(self._status_code.ERROR, type(exc).__name__))
            raise
        finally:
            self._context.detach(token)
            with self._lock:
                dangling = list(self._suites.values())
                self._suites.clear()
            for suite_span in dangling:
                suite_span.end()
            span.end()
            self.shutdown()

    def suite_started(self, suite: str) -> None:
        span = self.tracer.start_span(
            "dvf.suite", context=self._run_context, attributes=_attributes(dvf__suite=suite)
        )
        with self._lock:
            self._suites[suite] = span

    def suite_finished(self, result: SuiteResult) -> None:
        with self._lock:
            span = self._suites.pop(result.name, None)
        if span is None:
            return
        span.set_attribute("dvf.tables", len(result.datasets))
        self._set_status(span, result.status)
        span.end()

    # ------------------------------------------------------------------ tables
    @contextmanager
    def table_scope(self, suite: str, table: str) -> Iterator[TableSpan]:
        with self._lock:
            parent = self._suites.get(suite)
        parent_context = (
            self._trace.set_span_in_context(parent) if parent is not None else self._run_context
        )
        span = self.tracer.start_span(
            "dvf.table",
            context=parent_context,
            attributes=_attributes(dvf__suite=suite, dvf__table=table),
        )
        token = self._context.attach(self._trace.set_span_in_context(span))
        try:
            yield TableSpan(suite, table, span)
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(self._span_status(self._status_code.ERROR, type(exc).__name__))
            raise
        finally:
            self._context.detach(token)
            span.end()

    def table_finished(self, handle: TableSpan, dataset: DatasetPairResult) -> None:
        span = handle.span
        if span is None:
            return
        counts = dataset.row_counts
        span.set_attributes(
            _attributes(
                dvf__rows__source=counts.source,
                dvf__rows__target=counts.target,
                dvf__source=dataset.source.display() if dataset.source else None,
                dvf__target=dataset.target.display() if dataset.target else None,
                dvf__checks=len(dataset.checks),
            )
        )
        self._set_status(span, dataset.status)
        context = self._trace.set_span_in_context(span)
        for check in dataset.checks:
            self._check_span(check, context)
        rows = (counts.source or 0) + (counts.target or 0)
        if rows:
            self._metric("rows", rows)
            seconds = (dataset.duration_ms or 0) / 1000
            if seconds > 0:
                self._metric("throughput", rows / seconds)

    def _check_span(self, check: CheckResult, context: Any) -> None:
        span = self.tracer.start_span(
            "dvf.check",
            context=context,
            start_time=_ns(check.started_at),
            attributes=_attributes(
                dvf__check__id=check.check_id,
                dvf__check__type=check.type,
                dvf__check__status=check.status.value,
                dvf__check__severity=check.severity.value,
                dvf__check__dimension=check.dimension.value,
                dvf__check__rows=check.total_count,
                dvf__check__unexpected_rows=check.unexpected_count,
                dvf__check__columns=",".join(check.columns) or None,
                dvf__engine=check.engine,
                dvf__partition=check.partition,
            ),
        )
        if check.timing is not None:
            span.set_attributes(
                _attributes(
                    dvf__query_ms__source=check.timing.source_query_ms or None,
                    dvf__query_ms__target=check.timing.target_query_ms or None,
                )
            )
        retries = 0
        for query in check.queries:
            if query.duration_ms is not None:
                self._metric("query", query.duration_ms, {"dvf.side": query.side.value})
            attempts = query.parameters.get("attempts")
            if isinstance(attempts, int) and attempts > 1:
                retries += attempts - 1
        if retries:
            span.set_attribute("dvf.retries", retries)
            self._metric("retries", retries)
        self._metric(
            "checks", 1, {"dvf.check.status": check.status.value, "dvf.check.type": check.type}
        )
        if check.status is Status.ERROR:
            error = check.error.type if check.error is not None else "error"
            span.set_status(self._span_status(self._status_code.ERROR, error))
        span.end(end_time=_ns(check.ended_at) or _ns(check.started_at))

    # ------------------------------------------------------------------ end
    def run_finished(self, result: RunResult) -> None:
        span = self._run_span
        if span is None:
            return
        summary = result.summary
        span.set_attributes(
            _attributes(
                dvf__quality_score=summary.quality_score,
                dvf__rows_compared=summary.rows_compared,
                dvf__checks=sum(summary.checks_by_status.values()),
                dvf__checks__failed=summary.checks_by_status.get(Status.FAILED, 0),
                dvf__checks__error=summary.checks_by_status.get(Status.ERROR, 0),
                dvf__interrupted=result.interrupted,
            )
        )
        self._set_status(span, result.status)
        if result.duration_ms is not None:
            self._metric("run", result.duration_ms, {"dvf.status": result.status.value})

    def shutdown(self) -> None:
        if not self._owned:
            return
        for provider in self._providers:
            if provider is not None and hasattr(provider, "shutdown"):
                try:
                    provider.shutdown()
                except Exception:  # telemetry must never fail a run
                    log.debug("shutting down a telemetry provider failed", exc_info=True)


_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(message)


def create_telemetry(
    config: TelemetryConfig,
    *,
    tracer_provider: Any = None,
    meter_provider: Any = None,
) -> Telemetry:
    """Telemetry for a run: :class:`OtelTelemetry` when enabled and OpenTelemetry is importable,
    otherwise the no-op :class:`Telemetry`."""
    if not config.enabled:
        return Telemetry()
    try:
        import opentelemetry.trace  # noqa: F401
    except ImportError:
        _warn_once(
            "api",
            "telemetry.enabled is set but OpenTelemetry is not installed; install it with: "
            "pip install 'dvf-framework[otel]'",
        )
        return Telemetry()
    owned = False
    try:
        if tracer_provider is None:
            if config.exporter == "console":
                tracer_provider, meter_provider = _console_providers(config.service_name)
                owned = True
            else:
                tracer_provider, meter_provider = _global_providers()
        return OtelTelemetry(
            tracer_provider, meter_provider if config.metrics else None, owned=owned
        )
    except ImportError:
        _warn_once(
            "sdk",
            "telemetry.exporter 'console' needs the OpenTelemetry SDK; install it with: "
            "pip install 'dvf-framework[otel]'",
        )
    except Exception as exc:  # telemetry must never fail a run
        log.warning("telemetry is disabled for this run: %s", exc)
    return Telemetry()


__all__ = ["OtelTelemetry", "TableSpan", "Telemetry", "create_telemetry"]
