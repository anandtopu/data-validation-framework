"""OpenLineage run events with data quality facets (BUILD_PROMPT.md section 12.9).

dvf writes the events itself (no OpenLineage client dependency), following the OpenLineage 2-0-2
spec and the published facet schemas named by each facet's ``_schemaURL``:

* each run is a job run (``START``, then ``COMPLETE``, ``FAIL``, or ``ABORT``);
* every dataset a table reads is an input with a ``dataQualityAssertions`` input facet (one
  assertion per check) and, where dvf counted rows or profiled columns, ``dataQualityMetrics``;
* with ``granularity: table`` each table is also a child job run linked by the ``parent`` facet.

Dataset namespaces and names follow the OpenLineage naming conventions where they define one
(``postgres://host:5432`` and ``database.schema.table``, ``bigquery``, ``s3://bucket``, ``file``);
``lineage.dataset_namespaces`` overrides them per connection. Event delivery never fails a run:
errors are logged, and an HTTP backend that stays unreachable is skipped for the rest of the run.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from dvf import DISTRIBUTION, __version__
from dvf.core.datasets import DatasetKind, DatasetRef
from dvf.core.enums import Severity, Side, Status
from dvf.packs.integrations.http import post_json
from dvf.packs.integrations.messages import NotificationError, shorten
from dvf.security.redaction import MASK, global_redactor

if TYPE_CHECKING:
    from dvf.config.connections import ConnectionConfig
    from dvf.config.models import DvfConfig, LineageConfig, LineageTransportConfig
    from dvf.execution.context import RunInfo
    from dvf.models.results import CheckResult, ColumnProfile, DatasetPairResult, RunResult

log = logging.getLogger(__name__)

OPENLINEAGE_SPEC = "https://openlineage.io/spec/2-0-2/OpenLineage.json"
RUN_EVENT_SCHEMA_URL = f"{OPENLINEAGE_SPEC}#/$defs/RunEvent"
_FACETS = "https://openlineage.io/spec/facets"
ASSERTIONS_SCHEMA_URL = (
    f"{_FACETS}/1-1-0/DataQualityAssertionsDatasetFacet.json"
    "#/$defs/DataQualityAssertionsDatasetFacet"
)
METRICS_SCHEMA_URL = (
    f"{_FACETS}/1-0-2/DataQualityMetricsInputDatasetFacet.json"
    "#/$defs/DataQualityMetricsInputDatasetFacet"
)
JOB_TYPE_SCHEMA_URL = f"{_FACETS}/2-0-3/JobTypeJobFacet.json#/$defs/JobTypeJobFacet"
PARENT_SCHEMA_URL = f"{_FACETS}/1-1-0/ParentRunFacet.json#/$defs/ParentRunFacet"
PROCESSING_ENGINE_SCHEMA_URL = (
    f"{_FACETS}/1-1-1/ProcessingEngineRunFacet.json#/$defs/ProcessingEngineRunFacet"
)
ERROR_MESSAGE_SCHEMA_URL = f"{_FACETS}/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet"
TAGS_SCHEMA_URL = f"{_FACETS}/1-0-0/TagsRunFacet.json#/$defs/TagsRunFacet"
PRODUCER = f"https://pypi.org/project/{DISTRIBUTION}/{__version__}/"
"""The ``producer`` URI of every event and facet."""

_RUN_ID_NAMESPACE = uuid.UUID("6f2c3c9e-5d0b-4f55-9c1e-2b7f0a6d8e41")
_PASSING = frozenset({Status.PASSED, Status.WAIVED})
_DEFAULT_PORTS = {
    "postgres": 5432,
    "redshift": 5439,
    "mysql": 3306,
    "mssql": 1433,
    "oracle": 1521,
    "mongodb": 27017,
    "cassandra": 9042,
    "sap_hana": 30015,
}
_DEFAULT_SCHEMA = {"postgres": "public", "redshift": "public", "mssql": "dbo"}
_THREE_PART = frozenset({"postgres", "redshift", "mssql", "oracle", "snowflake", "databricks"})


# ---------------------------------------------------------------------- transports


class Transport(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class ConsoleTransport:
    """Writes each event as one JSON line to standard error (or ``stream``)."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            stream = self._stream or sys.stderr
            stream.write(line + "\n")
            stream.flush()


class FileTransport:
    """Appends each event as one JSON line to a file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")


class HttpTransport:
    """Posts each event to an OpenLineage HTTP endpoint, with retries for transient errors."""

    def __init__(
        self,
        url: str,
        endpoint: str = "api/v1/lineage",
        *,
        api_key: str | None = None,
        timeout_s: float = 5.0,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = url.rstrip("/") + "/" + endpoint.lstrip("/") if endpoint else url
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout_s
        self._retries = retries
        self._sleep = sleep

    def emit(self, event: dict[str, Any]) -> None:
        attempt = 0
        while True:
            try:
                post_json(self.url, event, headers=self._headers, timeout=self._timeout)
                return
            except NotificationError as exc:
                if not exc.transient or attempt >= self._retries:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else 2.0**attempt
                self._sleep(min(delay, 30.0))
                attempt += 1


def make_transport(
    config: LineageTransportConfig, base_dir: Path, info: RunInfo | None = None
) -> Transport:
    if config.type == "http":
        assert config.url is not None
        api_key = config.api_key.get_secret_value() if config.api_key else None
        global_redactor().add(api_key)
        return HttpTransport(
            config.url,
            config.endpoint,
            api_key=api_key,
            timeout_s=config.timeout_s,
            retries=config.retries,
        )
    if config.type == "file":
        assert config.path is not None
        rendered = info.render(config.path) if info is not None else config.path
        path = Path(rendered)
        return FileTransport(path if path.is_absolute() else base_dir / path)
    return ConsoleTransport()


# ---------------------------------------------------------------------- naming


def _host_port(value: str | None) -> tuple[str | None, int | None]:
    """Host and port of a URL, never its credentials."""
    if not value:
        return None, None
    try:
        parts = urlsplit(value if "://" in value else f"x://{value}")
        return parts.hostname, parts.port
    except ValueError:
        return None, None


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else str(value)


def dataset_namespace(connection: str, config: ConnectionConfig | None, base_dir: Path) -> str:
    """The OpenLineage namespace of a connection's datasets."""
    if config is None:
        return f"dvf://{connection}"
    kind = config.type
    host = getattr(config, "host", None)
    port = getattr(config, "port", None)
    if host is None:
        url = _secret(getattr(config, "url", None)) or _secret(getattr(config, "uri", None))
        host, url_port = _host_port(url)
        port = port or url_port
    if kind == "bigquery":
        return "bigquery"
    if kind == "snowflake":
        return f"snowflake://{str(getattr(config, 'account', '')).lower()}"
    if kind == "databricks":
        return f"databricks://{getattr(config, 'server_hostname', '')}"
    if kind in ("duckdb", "sqlite"):
        database = str(getattr(config, "database", ":memory:"))
        if database == ":memory:":
            return f"{kind}://memory"
        path = Path(database)
        return f"{kind}://{(path if path.is_absolute() else base_dir / path).resolve().as_posix()}"
    if kind == "files":
        return _file_location(str(getattr(config, "path", "")), base_dir)[0]
    if host:
        if kind == "redshift" and host.endswith(".redshift.amazonaws.com"):
            labels = host.split(".")
            host = f"{labels[0]}.{labels[2]}" if len(labels) > 3 else host
        port = port or _DEFAULT_PORTS.get(kind)
        return f"{kind}://{host}:{port}" if port else f"{kind}://{host}"
    return f"{kind}://{connection}"


def _file_location(location: str, base_dir: Path) -> tuple[str, str]:
    """(namespace, name) of a file or folder path or URL."""
    try:
        parts = urlsplit(location)
    except ValueError:
        return "file", location
    scheme = parts.scheme.lower()
    if scheme in ("s3", "s3a", "s3n"):
        return f"s3://{parts.netloc}", parts.path.lstrip("/")
    if scheme in ("gs", "gcs"):
        return f"gs://{parts.netloc}", parts.path.lstrip("/")
    if scheme in ("abfs", "abfss", "wasb", "wasbs"):
        return f"{scheme}://{parts.netloc}", parts.path.lstrip("/")
    if scheme in ("http", "https"):
        return f"{scheme}://{parts.hostname}", parts.path.lstrip("/")
    if len(scheme) > 1 and scheme != "file":
        return f"{scheme}://{parts.netloc}", parts.path.lstrip("/")
    raw = parts.path if scheme == "file" else location
    path = Path(raw) if raw else base_dir
    path = path if path.is_absolute() else base_dir / path
    return "file", path.resolve().as_posix()


def dataset_identity(
    ref: DatasetRef, config: ConnectionConfig | None, base_dir: Path, namespace: str
) -> tuple[str, str]:
    """(namespace, name) of a dataset, following the OpenLineage naming conventions."""
    kind = config.type if config is not None else ""
    if kind == "files" and ref.kind is DatasetKind.PATH:
        base = str(getattr(config, "path", ""))
        joined_ns, joined = _file_location(base, base_dir)
        name = ref.name
        if "://" in name or Path(name).is_absolute():
            return _file_location(name, base_dir)
        return joined_ns, str(PurePosixPath(joined) / name) if joined else name
    if ref.kind is not DatasetKind.TABLE:
        return namespace, ref.name
    parts = ref.name.split(".")
    database = getattr(config, "database", None) if config is not None else None
    if kind == "bigquery":
        dataset = getattr(config, "dataset", None)
        project = getattr(config, "project", None)
        if len(parts) == 1 and dataset:
            parts = [dataset, *parts]
        if len(parts) == 2 and project:
            parts = [project, *parts]
    elif kind == "databricks":
        catalog = getattr(config, "catalog", None)
        schema = getattr(config, "schema_", None)
        if len(parts) == 1 and schema:
            parts = [schema, *parts]
        if len(parts) == 2 and catalog:
            parts = [catalog, *parts]
    elif kind in _THREE_PART:
        schema = getattr(config, "schema_", None) or _DEFAULT_SCHEMA.get(kind)
        if len(parts) == 1 and schema:
            parts = [schema, *parts]
        if len(parts) == 2 and database:
            parts = [str(database), *parts]
    elif kind in ("mysql", "mongodb") and len(parts) == 1 and database:
        parts = [str(database), *parts]
    return namespace, ".".join(parts)


# ---------------------------------------------------------------------- facets


def _facet(schema_url: str, **fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": schema_url, **fields}


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _column_metrics(profile: ColumnProfile, strict: bool) -> dict[str, Any]:
    metrics: dict[str, Any] = {"nullCount": profile.null_count, "count": profile.count}
    if profile.distinct_count is not None:
        metrics["distinctCount"] = profile.distinct_count
    if not strict:
        for key in ("min", "max"):
            number = _number(getattr(profile, key))
            if number is not None:
                metrics[key] = number
    return metrics


def assertion(check: CheckResult, strict: bool) -> dict[str, Any]:
    """One entry of the ``dataQualityAssertions`` facet (facet schema 1-1-0)."""
    entry: dict[str, Any] = {
        "assertion": check.type,
        "success": check.status in _PASSING,
        "name": check.check_id,
        "severity": "error" if check.severity.at_least(Severity.ERROR) else "warn",
    }
    if len(check.columns) == 1:
        entry["column"] = check.columns[0]
    if check.message and not strict:
        description = shorten(check.message, 500)
        if description:
            entry["description"] = description
    if check.unexpected_count is not None:
        entry["actual"] = str(check.unexpected_count)
    params: dict[str, Any] = {
        "status": check.status.value,
        "dimension": check.dimension.value,
        "dvf_severity": check.severity.value,
    }
    if len(check.columns) > 1:
        params["columns"] = list(check.columns)
    for key in ("unexpected_count", "total_count", "unexpected_pct"):
        value = getattr(check, key)
        if value is not None:
            params[key] = value
    entry["params"] = params
    return entry


def _check_side(check: CheckResult, has_target: bool) -> str:
    if check.side is Side.SOURCE or not has_target:
        return "source"
    return "target"


@dataclass
class _Input:
    namespace: str
    name: str
    assertions: list[dict[str, Any]] = field(default_factory=list)
    row_count: int | None = None
    column_metrics: dict[str, Any] = field(default_factory=dict)

    def to_event(self) -> dict[str, Any]:
        input_facets: dict[str, Any] = {}
        if self.assertions:
            input_facets["dataQualityAssertions"] = _facet(
                ASSERTIONS_SCHEMA_URL, assertions=self.assertions
            )
        if self.row_count is not None or self.column_metrics:
            metrics = _facet(METRICS_SCHEMA_URL, columnMetrics=self.column_metrics)
            if self.row_count is not None:
                metrics["rowCount"] = self.row_count
            input_facets["dataQualityMetrics"] = metrics
        dataset: dict[str, Any] = {"namespace": self.namespace, "name": self.name, "facets": {}}
        if input_facets:
            dataset["inputFacets"] = input_facets
        return dataset


# ---------------------------------------------------------------------- emitter


class LineageEmitter:
    """Builds and sends the OpenLineage events of one run. Never raises."""

    def __init__(
        self,
        config: DvfConfig,
        info: RunInfo,
        base_dir: Path,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.settings: LineageConfig = config.lineage
        self.enabled = self.settings.enabled
        self.config = config
        self.info = info
        self.base_dir = base_dir
        self.job_name = self.settings.job_name or config.project.name
        self.namespace = self.settings.namespace
        self.run_uuid = run_uuid(self.namespace, info.run_id)
        self.strict = config.defaults.privacy.mode == "strict"
        self._strict_suites = {s.name for s in config.suites if s.defaults.privacy.mode == "strict"}
        self._transport = transport
        self._broken = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ sending
    def _send(self, event: dict[str, Any]) -> None:
        if not self.enabled or self._broken:
            return
        try:
            if self._transport is None:
                self._transport = make_transport(self.settings.transport, self.base_dir, self.info)
            self._transport.emit(event)
        except Exception as exc:  # lineage must never fail a run
            text = global_redactor().redact_text(str(exc) or type(exc).__name__)
            secret = self.settings.transport.api_key
            if secret is not None and secret.get_secret_value():
                text = text.replace(secret.get_secret_value(), MASK)
            with self._lock:
                already = self._broken
                if isinstance(self._transport, HttpTransport):
                    self._broken = True
            if not already:
                log.warning(
                    "OpenLineage %s event could not be sent: %s%s",
                    event.get("eventType"),
                    text,
                    "; no more events are sent in this run"
                    if isinstance(self._transport, HttpTransport)
                    else "",
                )

    def _event(
        self,
        event_type: str,
        run_id: str,
        job_name: str,
        *,
        run_facets: dict[str, Any] | None = None,
        inputs: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        return {
            "eventTime": datetime.now(UTC).isoformat(),
            "producer": PRODUCER,
            "schemaURL": RUN_EVENT_SCHEMA_URL,
            "eventType": event_type,
            "run": {"runId": run_id, "facets": run_facets or {}},
            "job": {
                "namespace": self.namespace,
                "name": job_name,
                "facets": {
                    "jobType": _facet(
                        JOB_TYPE_SCHEMA_URL,
                        processingType="BATCH",
                        integration="DVF",
                        jobType="VALIDATION" if job_name == self.job_name else "TABLE_VALIDATION",
                    )
                },
            },
            "inputs": list(inputs),
            "outputs": [],
        }

    def _run_facets(self, extra_tags: dict[str, Any] | None = None) -> dict[str, Any]:
        tags = {"dvf.run_id": self.info.run_id, "dvf.project": self.config.project.name}
        if self.info.profile:
            tags["dvf.profile"] = self.info.profile
        tags.update({k: v for k, v in (extra_tags or {}).items() if v is not None})
        return {
            "processing_engine": _facet(
                PROCESSING_ENGINE_SCHEMA_URL, version=__version__, name="dvf"
            ),
            "tags": _facet(
                TAGS_SCHEMA_URL,
                tags=[{"key": k, "value": str(v), "source": "DVF"} for k, v in tags.items()],
            ),
        }

    def _parent_facet(self) -> dict[str, Any]:
        job = {"namespace": self.namespace, "name": self.job_name}
        run = {"runId": self.run_uuid}
        return _facet(PARENT_SCHEMA_URL, run=run, job=job, root={"run": run, "job": job})

    # ------------------------------------------------------------------ inputs
    def _identity(self, ref: DatasetRef) -> tuple[str, str]:
        connection = self.config.connections.get(ref.connection)
        namespace = self.settings.dataset_namespaces.get(ref.connection) or dataset_namespace(
            ref.connection, connection, self.base_dir
        )
        return dataset_identity(ref, connection, self.base_dir, namespace)

    def _inputs(self, suite: str, datasets: Iterable[DatasetPairResult]) -> list[_Input]:
        found: dict[tuple[str, str], _Input] = {}
        strict = self.strict or suite in self._strict_suites
        for dataset in datasets:
            sides = {"source": dataset.source, "target": dataset.target}
            for side, ref in sides.items():
                if ref is None:
                    continue
                key = self._identity(ref)
                item = found.setdefault(key, _Input(*key))
                count = getattr(dataset.row_counts, side)
                if count is not None and item.row_count is None:
                    item.row_count = count
                for profile in dataset.profiles:
                    column_profile = getattr(profile, side)
                    if column_profile is None:
                        continue
                    column = (
                        profile.source_column or profile.column
                        if side == "source"
                        else profile.column
                    )
                    item.column_metrics.setdefault(column, _column_metrics(column_profile, strict))
            has_target = dataset.target is not None
            for check in dataset.checks:
                if check.status is Status.SKIPPED:
                    continue
                ref = (
                    dataset.source if _check_side(check, has_target) == "source" else dataset.target
                )
                if ref is None:
                    continue
                found[self._identity(ref)].assertions.append(assertion(check, strict))
        return list(found.values())

    # ------------------------------------------------------------------ lifecycle
    def run_started(self) -> None:
        if self.enabled:
            self._send(
                self._event("START", self.run_uuid, self.job_name, run_facets=self._run_facets())
            )

    def table_started(self, suite: str, table: str) -> None:
        if not self.enabled or self.settings.granularity != "table":
            return
        facets = {**self._run_facets(), "parent": self._parent_facet()}
        self._send(
            self._event(
                "START",
                table_run_uuid(self.namespace, self.info.run_id, suite, table),
                f"{self.job_name}.{suite}.{table}",
                run_facets=facets,
            )
        )

    def table_finished(
        self, suite: str, dataset: DatasetPairResult | None, table: str | None = None
    ) -> None:
        if not self.enabled or self.settings.granularity != "table":
            return
        name = dataset.name if dataset is not None else (table or "")
        status = dataset.status if dataset is not None else Status.ERROR
        event_type = "FAIL" if dataset is None or status in self.settings.fail_on else "COMPLETE"
        facets = {
            **self._run_facets({"dvf.suite": suite, "dvf.table": name, "dvf.status": status.value}),
            "parent": self._parent_facet(),
        }
        if event_type == "FAIL":
            facets["errorMessage"] = self._error_facet(
                [] if dataset is None else dataset.checks, suite
            )
        inputs = [] if dataset is None else [i.to_event() for i in self._inputs(suite, [dataset])]
        self._send(
            self._event(
                event_type,
                table_run_uuid(self.namespace, self.info.run_id, suite, name),
                f"{self.job_name}.{suite}.{name}",
                run_facets=facets,
                inputs=inputs,
            )
        )

    def run_finished(self, result: RunResult) -> None:
        if not self.enabled:
            return
        if result.interrupted:
            event_type = "ABORT"
        elif result.status in self.settings.fail_on:
            event_type = "FAIL"
        else:
            event_type = "COMPLETE"
        facets = self._run_facets(
            {
                "dvf.status": result.status.value,
                "dvf.quality_score": result.summary.quality_score,
                "dvf.checks": sum(result.summary.checks_by_status.values()),
            }
        )
        if event_type == "FAIL":
            facets["errorMessage"] = self._error_facet(list(result.iter_checks()), None)
        inputs: list[dict[str, Any]] = []
        if self.settings.granularity == "run":
            merged: dict[tuple[str, str], _Input] = {}
            for suite in result.suites:
                for item in self._inputs(suite.name, suite.datasets):
                    existing = merged.get((item.namespace, item.name))
                    if existing is None:
                        merged[(item.namespace, item.name)] = item
                    else:
                        existing.assertions += item.assertions
                        if existing.row_count is None:
                            existing.row_count = item.row_count
                        for column, metrics in item.column_metrics.items():
                            existing.column_metrics.setdefault(column, metrics)
            inputs = [i.to_event() for i in merged.values()]
        self._send(
            self._event(event_type, self.run_uuid, self.job_name, run_facets=facets, inputs=inputs)
        )

    def run_crashed(self, exc: BaseException) -> None:
        if not self.enabled:
            return
        interrupted = isinstance(exc, KeyboardInterrupt)
        facets = self._run_facets({"dvf.status": "interrupted" if interrupted else "error"})
        if not interrupted:
            message = (
                f"The run stopped with {type(exc).__name__}."
                if self.strict
                else global_redactor().redact_text(f"{type(exc).__name__}: {exc}")
            )
            facets["errorMessage"] = _facet(
                ERROR_MESSAGE_SCHEMA_URL, message=message, programmingLanguage="python"
            )
        self._send(
            self._event(
                "ABORT" if interrupted else "FAIL", self.run_uuid, self.job_name, run_facets=facets
            )
        )

    def _error_facet(self, checks: list[CheckResult], suite: str | None) -> dict[str, Any]:
        errors = [c for c in checks if c.status is Status.ERROR]
        failed = [c for c in checks if c.status is Status.FAILED]
        strict = self.strict or (suite is not None and suite in self._strict_suites)
        message = f"{len(errors)} checks errored, {len(failed)} failed"
        first = errors[0] if errors else (failed[0] if failed else None)
        if first is not None:
            message += f"; first: {first.check_id}"
            if not strict and first.message:
                message += f": {shorten(first.message, 500)}"
        if not checks:
            message = "The table could not be validated."
        return _facet(ERROR_MESSAGE_SCHEMA_URL, message=message, programmingLanguage="python")


def run_uuid(namespace: str, run_id: str) -> str:
    """The OpenLineage run ID (a UUID) of a dvf run: stable, so a resumed run keeps it."""
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, f"{namespace}/{run_id}"))


def table_run_uuid(namespace: str, run_id: str, suite: str, table: str) -> str:
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, f"{namespace}/{run_id}/{suite}/{table}"))


__all__ = [
    "ASSERTIONS_SCHEMA_URL",
    "METRICS_SCHEMA_URL",
    "PRODUCER",
    "RUN_EVENT_SCHEMA_URL",
    "ConsoleTransport",
    "FileTransport",
    "HttpTransport",
    "LineageEmitter",
    "Transport",
    "assertion",
    "dataset_identity",
    "dataset_namespace",
    "make_transport",
    "run_uuid",
    "table_run_uuid",
]
