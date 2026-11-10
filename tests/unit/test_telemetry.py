"""OpenTelemetry spans and metrics, recorded with the SDK's in-memory exporters, and the no-op
behavior when telemetry is disabled or OpenTelemetry is not installed."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from dvf import api
from dvf.config.models import TelemetryConfig
from dvf.core.enums import Dimension, Severity, Side, Status
from dvf.execution.context import RunInfo
from dvf.models.results import CheckResult, DatasetPairResult, QueryRecord, RowCounts
from dvf.packs.integrations import telemetry as telemetry_module
from dvf.packs.integrations.telemetry import OtelTelemetry, Telemetry, create_telemetry
from dvf.scaffold.seeded import load_duckdb


class Otel:
    """SDK providers with in-memory exporters."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.tracer_provider = TracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        self.reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(metric_readers=[self.reader])

    def finished(self, name: str | None = None) -> list[ReadableSpan]:
        spans = list(self.spans.get_finished_spans())
        return [s for s in spans if name is None or s.name == name]

    def metrics(self) -> dict[str, list[Any]]:
        data = self.reader.get_metrics_data()
        found: dict[str, list[Any]] = {}
        if data is None:
            return found
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    found.setdefault(metric.name, []).extend(metric.data.data_points)
        return found

    def close(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


@pytest.fixture
def otel(monkeypatch: pytest.MonkeyPatch) -> Any:
    providers = Otel()
    monkeypatch.setattr(
        telemetry_module,
        "_global_providers",
        lambda: (providers.tracer_provider, providers.meter_provider),
    )
    yield providers
    providers.close()


PEOPLE = pa.table({"id": [1, 2, 3], "email": ["a@x", None, "c@x"]})


def _run(tmp_path: Path, telemetry: dict[str, Any], **kwargs: Any) -> Any:
    load_duckdb({"people": PEOPLE, "orders": PEOPLE}, tmp_path / "db.duckdb")
    tables = [
        {
            "name": name,
            "source": {"table": name},
            "checks": [
                {"type": "not_null", "columns": ["email"]},
                {"type": "custom_sql", "name": "broken", "sql": "SELECT * FROM no_such_table"},
            ],
        }
        for name in ("people", "orders")
    ]
    config = {
        "version": 1,
        "project": {"name": "otel-demo"},
        "connections": {"db": {"type": "duckdb", "database": "db.duckdb"}},
        "suites": [{"name": "dq", "source": "db", "tables": tables}],
        "history": {"enabled": False},
        "telemetry": telemetry,
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    return api.run(loaded, write_reports=False, **kwargs)


def test_disabled_telemetry_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> Any:
        raise AssertionError("providers must not be touched")

    monkeypatch.setattr(telemetry_module, "_global_providers", fail)
    assert type(create_telemetry(TelemetryConfig())) is Telemetry
    output = _run(tmp_path, {})
    assert output.result.status is Status.ERROR


@pytest.mark.parametrize("parallel", [1, 2])
def test_spans_nest_run_suite_table_check(tmp_path: Path, otel: Otel, parallel: int) -> None:
    output = _run(tmp_path, {"enabled": True}, run_id="r-otel", parallel=parallel)
    (run,) = otel.finished("dvf.run")
    (suite,) = otel.finished("dvf.suite")
    tables = otel.finished("dvf.table")
    checks = otel.finished("dvf.check")
    assert run.parent is None
    assert suite.parent is not None and suite.parent.span_id == run.context.span_id
    assert {t.attributes["dvf.table"] for t in tables} == {"people", "orders"}
    assert all(t.parent is not None and t.parent.span_id == suite.context.span_id for t in tables)
    table_ids = {t.context.span_id: t for t in tables}
    assert len(checks) == len(list(output.result.iter_checks()))
    assert all(c.parent is not None and c.parent.span_id in table_ids for c in checks)
    assert {s.context.trace_id for s in otel.finished()} == {run.context.trace_id}

    assert run.attributes["dvf.run_id"] == "r-otel"
    assert run.attributes["dvf.project"] == "otel-demo"
    assert run.attributes["dvf.status"] == "error"
    assert run.status.status_code is StatusCode.ERROR
    people = next(t for t in tables if t.attributes["dvf.table"] == "people")
    assert people.attributes["dvf.status"] == "error"
    not_null = next(c for c in checks if c.attributes["dvf.check.id"] == "dq.people.not_null.email")
    assert not_null.attributes["dvf.check.status"] == "failed"
    assert not_null.attributes["dvf.check.rows"] == 3
    assert not_null.attributes["dvf.check.unexpected_rows"] == 1
    assert not_null.attributes["dvf.check.type"] == "not_null"
    assert not_null.status.status_code is StatusCode.UNSET  # a failed check is not a span error
    assert not_null.start_time is not None and not_null.end_time is not None
    assert not_null.start_time <= not_null.end_time
    broken = next(
        c for c in checks if c.attributes["dvf.check.id"] == "dq.people.custom_sql.broken"
    )
    assert broken.status.status_code is StatusCode.ERROR

    metrics = otel.metrics()
    assert {"dvf.checks", "dvf.query.duration", "dvf.run.duration"} <= set(metrics)
    statuses = {
        p.attributes["dvf.check.status"]: p.value
        for p in metrics["dvf.checks"]
        if p.attributes["dvf.check.type"] == "not_null"
    }
    assert statuses == {"failed": 2}
    sides = {p.attributes["dvf.side"] for p in metrics["dvf.query.duration"]}
    assert sides == {"source"}


def test_table_metrics_and_retries(otel: Otel) -> None:
    telemetry = create_telemetry(
        TelemetryConfig(enabled=True),
        tracer_provider=otel.tracer_provider,
        meter_provider=otel.meter_provider,
    )
    assert isinstance(telemetry, OtelTelemetry)
    now = datetime.now(UTC)
    check = CheckResult(
        check_id="s.t.row_count",
        name="row_count",
        type="row_count",
        dimension=Dimension.VOLUME,
        severity=Severity.ERROR,
        status=Status.PASSED,
        started_at=now,
        ended_at=now,
        engine="duckdb",
        queries=[
            QueryRecord(
                side=Side.SOURCE, sql="SELECT 1", duration_ms=12.0, parameters={"attempts": 3}
            ),
            QueryRecord(side=Side.TARGET, sql="SELECT 1", duration_ms=8.0),
        ],
    )
    dataset = DatasetPairResult(
        name="t",
        status=Status.PASSED,
        row_counts=RowCounts(source=1000, target=1000),
        checks=[check],
        duration_ms=500.0,
    )
    info = RunInfo(run_id="r1", started_at=now)
    with telemetry.run_scope(info, "p"):
        telemetry.suite_started("s")
        with telemetry.table_scope("s", "t") as handle:
            telemetry.table_finished(handle, dataset)
    metrics = otel.metrics()
    assert metrics["dvf.rows.scanned"][0].value == 2000
    throughput = metrics["dvf.table.rows_per_second"][0]
    assert throughput.sum == pytest.approx(4000.0)
    assert metrics["dvf.query.retries"][0].value == 2
    by_side = {p.attributes["dvf.side"]: p.sum for p in metrics["dvf.query.duration"]}
    assert by_side == {"source": 12.0, "target": 8.0}
    (span,) = otel.finished("dvf.check")
    assert span.attributes["dvf.retries"] == 2 and span.attributes["dvf.engine"] == "duckdb"
    (table,) = otel.finished("dvf.table")
    assert table.attributes["dvf.rows.source"] == 1000


def test_an_exception_marks_the_run_span(otel: Otel) -> None:
    telemetry = create_telemetry(
        TelemetryConfig(enabled=True, metrics=False),
        tracer_provider=otel.tracer_provider,
    )
    info = RunInfo(run_id="r1", started_at=datetime.now(UTC))
    with pytest.raises(RuntimeError), telemetry.run_scope(info, "p"):
        telemetry.suite_started("s")
        raise RuntimeError("boom")
    (run,) = otel.finished("dvf.run")
    assert run.status.status_code is StatusCode.ERROR
    assert run.events[0].name == "exception"
    assert len(otel.finished("dvf.suite")) == 1  # dangling suite spans are ended


def test_console_exporter_prints_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    telemetry = create_telemetry(TelemetryConfig(enabled=True, exporter="console"))
    assert isinstance(telemetry, OtelTelemetry)
    info = RunInfo(run_id="r-console", started_at=datetime.now(UTC))
    with telemetry.run_scope(info, "p"):
        pass
    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"name": "dvf.run"' in captured.err and "r-console" in captured.err


def test_missing_opentelemetry_is_a_noop_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
    monkeypatch.setattr(telemetry_module, "_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="dvf"):
        assert type(create_telemetry(TelemetryConfig(enabled=True))) is Telemetry
        output = _run(tmp_path, {"enabled": True})
    assert output.result.status is Status.ERROR
    messages = [r.getMessage() for r in caplog.records if "OpenTelemetry" in r.getMessage()]
    assert len(messages) == 1 and "dvf-framework[otel]" in messages[0]
