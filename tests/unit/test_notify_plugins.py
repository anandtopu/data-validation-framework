"""The example Jira and ServiceNow ticket plugins (examples/plugins/ticket_notifier.py), loaded
as local plugins and run against a local HTTP server that imitates the two APIs."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from dvf import api
from dvf.packs.integrations.dispatch import notify_test
from dvf.plugins.registry import PluginKind, Registry
from dvf.scaffold.seeded import load_duckdb
from tests.conftest import EXAMPLES
from tests.unit.test_notify import HttpRecorder

PLUGIN = EXAMPLES / "plugins" / "ticket_notifier.py"
TOKEN = "synthetic-jira-token-42"


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def server() -> Iterator[HttpRecorder]:
    with HttpRecorder() as recorder:
        yield recorder


def _config(server: HttpRecorder, **extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "ticket-demo"},
        "plugins": {"paths": [str(PLUGIN)]},
        "history": {"enabled": False},
        "notifications": [
            {
                "type": "example.jira",
                "name": "jira",
                "base_url": server.url,
                "project_key": "DQ",
                "user": "dq-bot@example.test",
                "api_token": TOKEN,
            },
            {
                "type": "example.servicenow",
                "name": "servicenow",
                "on": ["error"],
                "instance_url": server.url,
                "user": "dvf.integration",
                "password": "synthetic-sn-password",
                "assignment_group": "Data Platform",
            },
        ],
        **extra,
    }


def test_example_plugins_register(tmp_path: Path, server: HttpRecorder, registry: Registry) -> None:
    loaded = api.load_config_data(_config(server), base_dir=tmp_path, registry=registry)
    for name in ("example.jira", "example.servicenow"):
        entry = loaded.registry.get(PluginKind.NOTIFIER, name)
        assert entry.ready and entry.origin == "local"


def test_example_plugins_open_tickets(
    tmp_path: Path, server: HttpRecorder, registry: Registry
) -> None:
    server.respond(201, '{"id": "10001", "key": "DQ-1"}')
    server.respond(201, '{"result": {"number": "INC0010001"}}')
    loaded = api.load_config_data(_config(server), base_dir=tmp_path, registry=registry)
    deliveries = notify_test(loaded.config, loaded.registry, tmp_path)
    assert [(d.channel, d.ok) for d in deliveries] == [("jira", True), ("servicenow", True)]
    jira = next(r for r in server.requests if r.path == "/rest/api/2/issue")
    expected = base64.b64encode(f"dq-bot@example.test:{TOKEN}".encode()).decode()
    assert jira.headers["Authorization"] == f"Basic {expected}"
    fields = jira.json()["fields"]
    assert fields["project"] == {"key": "DQ"} and fields["issuetype"] == {"name": "Bug"}
    assert fields["summary"] == "[dvf] dvf test notification for ticket-demo"
    assert "status TEST" in fields["description"] and fields["labels"] == ["dvf"]
    incident = next(r for r in server.requests if r.path == "/api/now/table/incident").json()
    assert incident["assignment_group"] == "Data Platform" and incident["urgency"] == "2"


def test_a_failed_run_opens_a_jira_issue(
    tmp_path: Path, server: HttpRecorder, registry: Registry
) -> None:
    server.default = (201, '{"key": "DQ-2"}', {})
    load_duckdb({"t": pa.table({"id": [1, None]})}, tmp_path / "db.duckdb")
    config = _config(
        server,
        connections={"db": {"type": "duckdb", "database": "db.duckdb"}},
        suites=[
            {
                "name": "dq",
                "source": "db",
                "tables": [
                    {
                        "name": "t",
                        "source": {"table": "t"},
                        "checks": [{"type": "not_null", "columns": ["id"]}],
                    }
                ],
            }
        ],
    )
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry, strict=False)
    output = api.run(loaded, write_reports=False)
    assert [(d.channel, d.ok) for d in output.notifications] == [("jira", True)]
    description = server.requests[0].json()["fields"]["description"]
    assert "dq.t.not_null.id" in description and "1 of 2 rows" in description


def test_ticket_errors_are_redacted_and_retried(
    tmp_path: Path, server: HttpRecorder, registry: Registry
) -> None:
    server.default = (503, "unavailable", {})
    config = _config(server)
    config["notifications"][0]["retries"] = 1
    config["notifications"][0]["retry_backoff_s"] = 0
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry)
    deliveries = notify_test(loaded.config, loaded.registry, tmp_path, channel="jira")
    assert not deliveries[0].ok and deliveries[0].attempts == 2
    assert deliveries[0].error == "TicketError: HTTP 503 Service Unavailable"
    assert TOKEN not in str(deliveries)
