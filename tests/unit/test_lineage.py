"""OpenLineage events: dataset naming, event and facet shapes (validated against a JSON Schema
that encodes the requirements of the OpenLineage 2-0-2 spec and the facet schemas dvf uses), the
file, console, and HTTP transports, table granularity, privacy, and failure handling."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pyarrow as pa
import pytest

from dvf import api
from dvf.config.connections import (
    BigQueryConnection,
    DatabaseConnection,
    DuckDBConnection,
    FileConnection,
    SnowflakeConnection,
)
from dvf.core.datasets import DatasetKind, DatasetRef
from dvf.core.enums import Status
from dvf.execution.context import RunInfo
from dvf.packs.integrations.lineage import (
    ASSERTIONS_SCHEMA_URL,
    METRICS_SCHEMA_URL,
    RUN_EVENT_SCHEMA_URL,
    ConsoleTransport,
    LineageEmitter,
    dataset_identity,
    dataset_namespace,
    run_uuid,
)
from dvf.scaffold.seeded import load_duckdb
from tests.unit.reporting_fixtures import build_run
from tests.unit.test_notify import HttpRecorder, closed_port

# ---------------------------------------------------------------------- the schema

_URI = {"type": "string", "minLength": 1}
FACET: dict[str, Any] = {
    "type": "object",
    "required": ["_producer", "_schemaURL"],
    "properties": {"_producer": _URI, "_schemaURL": _URI},
}


def _facet(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {"allOf": [FACET, {"type": "object", "required": required, "properties": properties}]}


_JOB_REF = {
    "type": "object",
    "required": ["namespace", "name"],
    "properties": {"namespace": {"type": "string"}, "name": {"type": "string"}},
}
_RUN_REF = {
    "type": "object",
    "required": ["runId"],
    "properties": {"runId": {"type": "string", "format": "uuid"}},
}
ASSERTIONS = _facet(
    ["assertions"],
    {
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["assertion", "success"],
                "additionalProperties": False,
                "properties": {
                    "assertion": {"type": "string"},
                    "success": {"type": "boolean"},
                    "column": {"type": "string"},
                    "severity": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "expected": {"type": "string"},
                    "actual": {"type": "string"},
                    "content": {"type": "string"},
                    "contentType": {"type": "string"},
                    "params": {"type": "object"},
                },
            },
        }
    },
)
METRICS = _facet(
    ["columnMetrics"],
    {
        "rowCount": {"type": "integer"},
        "bytes": {"type": "integer"},
        "fileCount": {"type": "integer"},
        "columnMetrics": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "nullCount": {"type": "integer"},
                    "distinctCount": {"type": "integer"},
                    "sum": {"type": "number"},
                    "count": {"type": "number"},
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                    "quantiles": {"type": "object"},
                },
            },
        },
    },
)
RUN_EVENT: dict[str, Any] = {
    "type": "object",
    "required": ["eventTime", "producer", "schemaURL", "run", "job"],
    "properties": {
        "eventTime": {"type": "string", "format": "date-time"},
        "producer": _URI,
        "schemaURL": {"const": RUN_EVENT_SCHEMA_URL},
        "eventType": {"enum": ["START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"]},
        "run": {
            "allOf": [_RUN_REF],
            "properties": {
                "facets": {
                    "type": "object",
                    "additionalProperties": FACET,
                    "properties": {
                        "parent": _facet(
                            ["run", "job"],
                            {
                                "run": _RUN_REF,
                                "job": _JOB_REF,
                                "root": {
                                    "type": "object",
                                    "required": ["run", "job"],
                                    "properties": {"run": _RUN_REF, "job": _JOB_REF},
                                },
                            },
                        ),
                        "processing_engine": _facet(["version"], {"version": {"type": "string"}}),
                        "errorMessage": _facet(
                            ["message", "programmingLanguage"],
                            {"message": {"type": "string"}},
                        ),
                        "tags": _facet(
                            ["tags"],
                            {
                                "tags": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["key", "value"],
                                        "properties": {
                                            "key": {"type": "string"},
                                            "value": {"type": "string"},
                                        },
                                    },
                                }
                            },
                        ),
                    },
                }
            },
        },
        "job": {
            "allOf": [_JOB_REF],
            "properties": {
                "facets": {
                    "type": "object",
                    "additionalProperties": FACET,
                    "properties": {
                        "jobType": _facet(
                            ["processingType", "integration"],
                            {"processingType": {"enum": ["BATCH", "STREAMING", "SERVICE"]}},
                        )
                    },
                }
            },
        },
        "inputs": {
            "type": "array",
            "items": {
                "allOf": [_JOB_REF],
                "properties": {
                    "facets": {"type": "object", "additionalProperties": FACET},
                    "inputFacets": {
                        "type": "object",
                        "additionalProperties": FACET,
                        "properties": {
                            "dataQualityAssertions": ASSERTIONS,
                            "dataQualityMetrics": METRICS,
                        },
                    },
                },
            },
        },
        "outputs": {"type": "array"},
    },
}


def validate(event: dict[str, Any]) -> None:
    jsonschema.validate(event, RUN_EVENT, format_checker=jsonschema.FormatChecker())
    datetime.fromisoformat(event["eventTime"])
    uuid.UUID(event["run"]["runId"])
    for dataset in event.get("inputs", []):
        facets = dataset.get("inputFacets", {})
        if "dataQualityAssertions" in facets:
            assert facets["dataQualityAssertions"]["_schemaURL"] == ASSERTIONS_SCHEMA_URL
        if "dataQualityMetrics" in facets:
            assert facets["dataQualityMetrics"]["_schemaURL"] == METRICS_SCHEMA_URL


# ---------------------------------------------------------------------- naming


def test_dataset_namespaces(tmp_path: Path) -> None:
    pg = DatabaseConnection.model_validate(
        {"type": "postgres", "host": "db.example.test", "database": "sales"}
    )
    assert dataset_namespace("pg", pg, tmp_path) == "postgres://db.example.test:5432"
    by_url = DatabaseConnection.model_validate(
        {"type": "mysql", "url": "mysql://app:s3cret-pass@mysql.example.test:3307/shop"}
    )
    namespace = dataset_namespace("my", by_url, tmp_path)
    assert namespace == "mysql://mysql.example.test:3307" and "s3cret" not in namespace
    redshift = DatabaseConnection.model_validate(
        {
            "type": "redshift",
            "host": "analytics.abc123xyz.eu-west-1.redshift.amazonaws.com",
            "database": "dw",
        }
    )
    assert dataset_namespace("rs", redshift, tmp_path) == "redshift://analytics.eu-west-1:5439"
    snowflake = SnowflakeConnection.model_validate({"type": "snowflake", "account": "Acme-Prod"})
    assert dataset_namespace("sf", snowflake, tmp_path) == "snowflake://acme-prod"
    bigquery = BigQueryConnection.model_validate(
        {"type": "bigquery", "project": "acme-dw", "dataset": "sales"}
    )
    assert dataset_namespace("bq", bigquery, tmp_path) == "bigquery"
    duck = DuckDBConnection.model_validate({"type": "duckdb", "database": "db.duckdb"})
    expected = f"duckdb://{(tmp_path / 'db.duckdb').resolve().as_posix()}"
    assert dataset_namespace("d", duck, tmp_path) == expected
    memory = DuckDBConnection.model_validate({"type": "duckdb"})
    assert dataset_namespace("m", memory, tmp_path) == "duckdb://memory"
    s3 = FileConnection.model_validate({"type": "files", "path": "s3://lake/raw/sales"})
    assert dataset_namespace("s3", s3, tmp_path) == "s3://lake"
    local = FileConnection.model_validate({"type": "files", "path": "data"})
    assert dataset_namespace("f", local, tmp_path) == "file"
    assert dataset_namespace("x", None, tmp_path) == "dvf://x"


def test_dataset_names(tmp_path: Path) -> None:
    pg = DatabaseConnection.model_validate({"type": "postgres", "host": "h", "database": "sales"})

    def table(name: str, connection: str = "c") -> DatasetRef:
        return DatasetRef(connection=connection, kind=DatasetKind.TABLE, name=name)

    assert dataset_identity(table("customers"), pg, tmp_path, "ns") == (
        "ns",
        "sales.public.customers",
    )
    assert dataset_identity(table("crm.customers"), pg, tmp_path, "ns")[1] == "sales.crm.customers"
    bigquery = BigQueryConnection.model_validate(
        {"type": "bigquery", "project": "acme-dw", "dataset": "sales"}
    )
    assert dataset_identity(table("orders"), bigquery, tmp_path, "bigquery")[1] == (
        "acme-dw.sales.orders"
    )
    s3 = FileConnection.model_validate({"type": "files", "path": "s3://lake/raw"})
    path = DatasetRef(connection="c", kind=DatasetKind.PATH, name="orders/2026.parquet")
    assert dataset_identity(path, s3, tmp_path, "s3://lake") == (
        "s3://lake",
        "raw/orders/2026.parquet",
    )
    local = FileConnection.model_validate({"type": "files", "path": "data"})
    namespace, name = dataset_identity(path, local, tmp_path, "file")
    assert namespace == "file"
    assert name == (tmp_path / "data").resolve().as_posix() + "/orders/2026.parquet"
    query = DatasetRef(connection="c", kind=DatasetKind.QUERY, name="recent", query="SELECT 1")
    assert dataset_identity(query, pg, tmp_path, "ns") == ("ns", "recent")


def test_run_ids_are_stable_uuids() -> None:
    first = run_uuid("dvf", "20261108T101500Z-3f9a")
    assert first == run_uuid("dvf", "20261108T101500Z-3f9a")
    assert first != run_uuid("dvf", "20261108T101500Z-0000")
    assert uuid.UUID(first).version == 5


# ---------------------------------------------------------------------- runs

SOURCE = pa.table(
    {"id": [1, 2, 3, 4], "amount": [10.0, 20.0, 30.0, None], "email": ["a@x", "b@x", None, "d@x"]}
)
TARGET = pa.table({"id": [1, 2, 3], "amount": [10.0, 20.0, 31.0], "email": ["a@x", "b@x", None]})


def _config(lineage: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "lineage-demo"},
        "connections": {
            "src": {"type": "duckdb", "database": "src.duckdb"},
            "tgt": {"type": "duckdb", "database": "tgt.duckdb"},
        },
        "suites": [
            {
                "name": "mig",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "accounts",
                        "source": {"table": "accounts"},
                        "target": {"table": "accounts"},
                        "keys": ["id"],
                        "comparison": {"strategies": ["row_count", "profile"]},
                        "checks": [{"type": "not_null", "columns": ["email"]}],
                    },
                    {
                        "name": "accounts_copy",
                        "source": {"table": "accounts"},
                        "target": {"table": "accounts"},
                        "keys": ["id"],
                        "comparison": {"strategies": ["row_count"]},
                    },
                ],
            }
        ],
        "history": {"enabled": False},
        "lineage": {"enabled": True, **lineage},
        **extra,
    }


def _run(tmp_path: Path, lineage: dict[str, Any], **extra: Any) -> Any:
    load_duckdb({"accounts": SOURCE}, tmp_path / "src.duckdb")
    load_duckdb({"accounts": TARGET}, tmp_path / "tgt.duckdb")
    loaded = api.load_config_data(_config(lineage, **extra), base_dir=tmp_path, strict=False)
    return api.run(loaded, run_id="r-lineage", write_reports=False)


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_events_with_quality_facets(tmp_path: Path) -> None:
    output = _run(tmp_path, {"transport": {"type": "file", "path": "lineage/${run.id}.jsonl"}})
    assert output.result.status is Status.FAILED
    events = _events(tmp_path / "lineage" / "r-lineage.jsonl")
    assert [e["eventType"] for e in events] == ["START", "COMPLETE"]
    for event in events:
        validate(event)
    start, complete = events
    assert start["run"]["runId"] == complete["run"]["runId"] == run_uuid("dvf", "r-lineage")
    assert complete["job"] == {
        "namespace": "dvf",
        "name": "lineage-demo",
        "facets": complete["job"]["facets"],
    }
    tags = {t["key"]: t["value"] for t in complete["run"]["facets"]["tags"]["tags"]}
    assert tags["dvf.run_id"] == "r-lineage" and tags["dvf.status"] == "failed"
    inputs = {(i["namespace"].split("/")[-1], i["name"]): i for i in complete["inputs"]}
    assert set(inputs) == {("src.duckdb", "accounts"), ("tgt.duckdb", "accounts")}
    target = inputs[("tgt.duckdb", "accounts")]["inputFacets"]
    assertions = target["dataQualityAssertions"]["assertions"]
    by_name = {a["name"]: a for a in assertions}
    row_count = by_name["mig.accounts.compare.row_count"]
    assert row_count["success"] is False and row_count["severity"] == "error"
    not_null = by_name["mig.accounts.not_null.email"]
    assert not_null == {
        "assertion": "not_null",
        "success": False,
        "name": "mig.accounts.not_null.email",
        "severity": "error",
        "column": "email",
        "description": not_null["description"],
        "actual": "1",
        "params": not_null["params"],
    }
    assert not_null["params"]["total_count"] == 3
    metrics = target["dataQualityMetrics"]
    assert metrics["rowCount"] == 3
    assert metrics["columnMetrics"]["amount"]["nullCount"] == 0
    assert metrics["columnMetrics"]["amount"]["max"] == 31.0
    source = inputs[("src.duckdb", "accounts")]["inputFacets"]
    assert source["dataQualityMetrics"]["rowCount"] == 4


def test_fail_on_failed_runs(tmp_path: Path) -> None:
    _run(
        tmp_path,
        {"fail_on": ["failed", "error"], "transport": {"type": "file", "path": "ol.jsonl"}},
    )
    events = _events(tmp_path / "ol.jsonl")
    assert events[-1]["eventType"] == "FAIL"
    validate(events[-1])
    message = events[-1]["run"]["facets"]["errorMessage"]["message"]
    assert message.startswith("0 checks errored, 7 failed; first: mig.accounts.compare.row_count")


def test_table_granularity(tmp_path: Path) -> None:
    _run(tmp_path, {"granularity": "table", "transport": {"type": "file", "path": "ol.jsonl"}})
    events = _events(tmp_path / "ol.jsonl")
    for event in events:
        validate(event)
    names = [(e["eventType"], e["job"]["name"]) for e in events]
    assert names[0] == ("START", "lineage-demo") and names[-1] == ("COMPLETE", "lineage-demo")
    assert ("START", "lineage-demo.mig.accounts") in names
    assert ("COMPLETE", "lineage-demo.mig.accounts") in names
    assert ("COMPLETE", "lineage-demo.mig.accounts_copy") in names
    child = next(e for e in events if e["job"]["name"] == "lineage-demo.mig.accounts")
    parent = child["run"]["facets"]["parent"]
    assert parent["run"]["runId"] == run_uuid("dvf", "r-lineage")
    assert parent["job"] == {"namespace": "dvf", "name": "lineage-demo"}
    assert events[-1]["inputs"] == []  # the inputs are on the table jobs
    complete = [e for e in events if e["eventType"] == "COMPLETE" and e["inputs"]]
    assert len(complete) == 2


def test_strict_privacy_drops_descriptions_and_values(tmp_path: Path) -> None:
    _run(
        tmp_path,
        {"transport": {"type": "file", "path": "ol.jsonl"}},
        defaults={"privacy": {"mode": "strict"}},
    )
    complete = _events(tmp_path / "ol.jsonl")[-1]
    validate(complete)
    for dataset in complete["inputs"]:
        facets = dataset["inputFacets"]
        assertions = facets.get("dataQualityAssertions", {}).get("assertions", [])
        assert all("description" not in a for a in assertions)
        for metrics in facets.get("dataQualityMetrics", {}).get("columnMetrics", {}).values():
            assert "min" not in metrics and "max" not in metrics


def test_http_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    with HttpRecorder() as server:
        server.default = (201, "", {})
        _run(
            tmp_path,
            {
                "transport": {
                    "type": "http",
                    "url": server.url + "/",
                    "api_key": "synthetic-ol-key-0001",
                }
            },
        )
    assert [r.path for r in server.requests] == ["/api/v1/lineage", "/api/v1/lineage"]
    assert server.requests[0].headers["Authorization"] == "Bearer synthetic-ol-key-0001"
    for request in server.requests:
        validate(request.json())
    assert [r.json()["eventType"] for r in server.requests] == ["START", "COMPLETE"]


def test_dataset_namespace_overrides(tmp_path: Path) -> None:
    _run(
        tmp_path,
        {
            "transport": {"type": "file", "path": "ol.jsonl"},
            "dataset_namespaces": {"tgt": "postgres://warehouse.example.test:5432"},
        },
    )
    complete = _events(tmp_path / "ol.jsonl")[-1]
    namespaces = {i["namespace"] for i in complete["inputs"]}
    assert "postgres://warehouse.example.test:5432" in namespaces


def test_unreachable_backend_never_fails_the_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url = f"http://127.0.0.1:{closed_port()}"
    with caplog.at_level(logging.WARNING, logger="dvf"):
        output = _run(
            tmp_path,
            {
                "granularity": "table",
                "transport": {
                    "type": "http",
                    "url": url,
                    "retries": 0,
                    "api_key": "synthetic-ol-key-0002",
                },
            },
        )
    assert output.result.status is Status.FAILED
    warnings = [r for r in caplog.records if "OpenLineage" in r.getMessage()]
    assert len(warnings) == 1  # the backend is skipped after the first failure
    assert "no more events are sent" in warnings[0].getMessage()
    assert "synthetic-ol-key-0002" not in caplog.text


def test_console_transport_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    ConsoleTransport().emit({"eventType": "START"})
    captured = capsys.readouterr()
    assert captured.out == "" and json.loads(captured.err) == {"eventType": "START"}


def test_crashed_run_emits_fail(tmp_path: Path) -> None:
    from dvf.core.errors import DvfError

    load_duckdb({"accounts": SOURCE}, tmp_path / "src.duckdb")
    load_duckdb({"accounts": TARGET}, tmp_path / "tgt.duckdb")
    config = _config({"transport": {"type": "file", "path": "ol.jsonl"}})
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    with pytest.raises(DvfError):
        api.run(loaded, formats=["no_such_format"], output_dir=tmp_path / "out")
    events = _events(tmp_path / "ol.jsonl")
    assert [e["eventType"] for e in events] == ["START", "FAIL"]
    validate(events[-1])
    assert "no_such_format" in events[-1]["run"]["facets"]["errorMessage"]["message"]


def test_interrupted_run_is_abort_and_disabled_emits_nothing(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    class Capture:
        def emit(self, event: dict[str, Any]) -> None:
            sent.append(event)

    loaded = api.load_config_data(
        {"version": 1, "project": {"name": "p"}, "lineage": {"enabled": True}}, base_dir=tmp_path
    )
    info = RunInfo(run_id="r1", started_at=datetime.now(UTC))
    run = build_run()
    run.interrupted = True
    LineageEmitter(loaded.config, info, tmp_path, transport=Capture()).run_finished(run)
    assert sent[0]["eventType"] == "ABORT"
    validate(sent[0])
    disabled = api.load_config_data({"version": 1, "project": {"name": "p"}}, base_dir=tmp_path)
    emitter = LineageEmitter(disabled.config, info, tmp_path, transport=Capture())
    emitter.run_started()
    emitter.run_finished(run)
    assert len(sent) == 1


def test_assertions_of_the_synthetic_run_validate(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    class Capture:
        def emit(self, event: dict[str, Any]) -> None:
            sent.append(event)

    loaded = api.load_config_data(
        {"version": 1, "project": {"name": "p"}, "lineage": {"enabled": True}}, base_dir=tmp_path
    )
    info = RunInfo(run_id="r1", started_at=datetime.now(UTC))
    LineageEmitter(loaded.config, info, tmp_path, transport=Capture()).run_finished(build_run())
    event = sent[0]
    validate(event)
    assert event["eventType"] == "FAIL"  # the synthetic run has errors
    assertions = [
        a
        for i in event["inputs"]
        for a in i.get("inputFacets", {}).get("dataQualityAssertions", {}).get("assertions", [])
    ]
    assert {a["severity"] for a in assertions} <= {"error", "warn"}
    assert any(a["success"] for a in assertions) and any(not a["success"] for a in assertions)
