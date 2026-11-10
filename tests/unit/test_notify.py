"""Notifications: configuration, messages and privacy, Slack/Teams/webhook payloads, delivery
end to end against a local HTTP server, retries, redaction, the runner, and ``dvf notify test``.

Real Slack and Teams endpoints are never called; the local server checks the HTTP protocol and
the payloads dvf sends.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.config.notifications import (
    EmailNotification,
    GenericWebhookNotification,
    NotificationConfig,
    SlackNotification,
    TeamsNotification,
)
from dvf.core.enums import Status
from dvf.packs.integrations.dispatch import (
    Channel,
    Dispatcher,
    channels,
    is_transient,
    notify_run,
)
from dvf.packs.integrations.http import post_json, retry_after_seconds
from dvf.packs.integrations.messages import (
    MessageContext,
    NotificationError,
    build_message,
    trigger_status,
)
from dvf.packs.integrations.notifiers import slack_payload, teams_payload
from dvf.plugins.registry import PluginKind, Registry
from dvf.scaffold.seeded import load_duckdb
from tests.unit.reporting_fixtures import build_run

cli = CliRunner()
SECRET_PATH = "T000/B000/notARealSecretToken123"


# ---------------------------------------------------------------------- local HTTP server


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class HttpRecorder:
    """A local HTTP server that records requests and answers with queued responses."""

    requests: list[Recorded] = field(default_factory=list)
    responses: list[tuple[int, str, dict[str, str]]] = field(default_factory=list)
    default: tuple[int, str, dict[str, str]] = (200, "ok", {})
    delay: float = 0.0
    """Seconds to wait before answering (to test timeouts)."""

    def __post_init__(self) -> None:
        recorder = self
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                with lock:
                    recorder.requests.append(
                        Recorded(self.command, self.path, dict(self.headers.items()), body)
                    )
                    status, text, headers = (
                        recorder.responses.pop(0) if recorder.responses else recorder.default
                    )
                payload = text.encode("utf-8")
                if recorder.delay:
                    time.sleep(recorder.delay)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_POST = _handle  # noqa: N815 - the handler method names http.server expects
            do_PUT = _handle  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def respond(self, status: int, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.responses.append((status, text, headers or {}))

    def __enter__(self) -> HttpRecorder:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep requests to the local test servers away from any system proxy."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def http_server() -> Iterator[HttpRecorder]:
    with HttpRecorder() as server:
        yield server


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def no_sleep(_: float) -> None:
    return


def _summarized_run() -> Any:
    run = build_run()
    run.summary = run.summarize()
    return run


def _context(run: Any = None, strict: bool = False) -> MessageContext:
    return MessageContext(strict_privacy=strict)


# ---------------------------------------------------------------------- configuration


def test_builtin_notifiers_are_ready(registry: Registry) -> None:
    expected = {
        "slack": SlackNotification,
        "teams": TeamsNotification,
        "webhook": GenericWebhookNotification,
        "email": EmailNotification,
    }
    for name, model in expected.items():
        entry = registry.get(PluginKind.NOTIFIER, name)
        assert entry.ready and entry.status == "ready"
        assert entry.config_model is model


def test_configuration_validates_every_channel(tmp_path: Path) -> None:
    config = {
        "version": 1,
        "project": {"name": "demo"},
        "notifications": [
            {"type": "slack", "webhook": "https://hooks.example.test/x", "mention": "<!here>"},
            {"type": "teams", "name": "ops", "webhook": "https://teams.example.test/x"},
            {
                "type": "webhook",
                "webhook": "https://hooks.example.test/y",
                "method": "PUT",
                "headers": {"Authorization": "Bearer abc"},
                "on": ["failed", "error", "warning"],
            },
            {
                "type": "email",
                "recipients": ["qa@example.test"],
                "smtp_host": "smtp.example.test",
                "security": "ssl",
                "smtp_port": 465,
            },
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path)
    kinds = [type(n) for n in loaded.config.notifications]
    assert kinds == [
        SlackNotification,
        TeamsNotification,
        GenericWebhookNotification,
        EmailNotification,
    ]
    assert [c.name for c in channels(loaded.config)] == ["slack", "ops", "webhook", "email"]
    email = loaded.config.notifications[3]
    assert isinstance(email, EmailNotification) and email.effective_security == "ssl"


def test_configuration_rejects_unknown_fields(tmp_path: Path) -> None:
    from dvf.core.errors import ConfigError

    config = {
        "version": 1,
        "project": {"name": "demo"},
        "notifications": [{"type": "slack", "webhook": "https://x.test", "channel": "#qa"}],
    }
    with pytest.raises(ConfigError):
        api.load_config_data(config, base_dir=tmp_path)


def test_channel_names_are_unique(tmp_path: Path) -> None:
    config = {
        "version": 1,
        "project": {"name": "demo"},
        "notifications": [
            {"type": "slack", "webhook": "https://a.test"},
            {"type": "slack", "webhook": "https://b.test"},
            {"type": "webhook", "name": "slack-2", "webhook": "https://c.test"},
            {"type": "webhook", "name": "slack-2", "webhook": "https://d.test"},
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path)
    assert [c.name for c in channels(loaded.config)] == ["slack", "slack-3", "slack-2", "slack-2-2"]


def test_email_security_defaults() -> None:
    base = {"type": "email", "recipients": ["a@example.test"], "smtp_host": "h"}
    assert EmailNotification.model_validate(base).effective_security == "starttls"
    plain = EmailNotification.model_validate({**base, "use_tls": False})
    assert plain.effective_security == "none"
    assert EmailNotification.model_validate({**base, "security": "ssl"}).effective_security == "ssl"


# ---------------------------------------------------------------------- messages


def _channel(**fields: Any) -> NotificationConfig:
    return GenericWebhookNotification.model_validate(
        {"type": "webhook", "webhook": "https://hooks.example.test/x", **fields}
    )


def test_message_lists_the_worst_failures_first() -> None:
    run = _summarized_run()
    message = build_message(run, _channel(max_failures=3), _context())
    assert message.status is Status.ERROR
    assert message.title == "synthetic-migration: ERROR"
    assert message.failures[0].status is Status.ERROR
    assert len(message.failures) == 3
    problems = [
        c for c in run.iter_checks() if c.status in (Status.FAILED, Status.ERROR, Status.WARNING)
    ]
    assert message.more_failures == len(problems) - 3
    assert message.quality_score == run.summary.quality_score
    assert message.baseline == {
        "baseline_run_id": "20261105T093000Z-prev01",
        "new_failures": 2,
        "resolved": 1,
        "persisting": 2,
    }
    payload = message.to_dict()
    json.dumps(payload)
    assert payload["schema"] == "dvf.notification/v1"
    assert payload["summary"]["checks_by_status"] == message.checks_by_status


def test_messages_carry_no_samples_unless_enabled() -> None:
    run = _summarized_run()
    default = build_message(run, _channel(max_failures=50), _context())
    assert all(not f.sample_rows for f in default.failures)
    assert any(f.message for f in default.failures)
    assert any(f.hints for f in default.failures)
    assert all("samples" not in f for f in default.to_dict()["top_failures"])
    with_samples = build_message(
        run, _channel(max_failures=50, include_samples=True, max_sample_rows=2), _context()
    )
    sampled = [f for f in with_samples.failures if f.sample_rows]
    assert sampled and all(len(f.sample_rows) <= 2 for f in sampled)


@pytest.mark.parametrize("how", ["counts_only", "strict"])
def test_counts_only_and_strict_privacy(how: str) -> None:
    run = _summarized_run()
    channel = _channel(max_failures=50, include_samples=True, counts_only=how == "counts_only")
    message = build_message(run, channel, _context(strict=how == "strict"))
    assert message.counts_only
    for failure in message.failures:
        assert failure.message is None and not failure.hints and not failure.sample_rows
        assert all(not c.classifications for c in failure.column_mismatches)
    text = json.dumps(message.to_dict())
    assert "example.test" not in text  # sample values and messages are gone
    assert message.checks_by_status and message.failures


def test_strict_suite_drops_messages_of_its_checks_only() -> None:
    run = _summarized_run()
    open_message = build_message(run, _channel(max_failures=50), _context())
    context = MessageContext(strict_suites=frozenset({"sales_migration"}))
    message = build_message(run, _channel(max_failures=50), context)
    assert any(f.message for f in open_message.failures if f.suite == "sales_migration")
    for strict, plain in zip(message.failures, open_message.failures, strict=True):
        if strict.suite == "sales_migration":
            assert strict.message is None and not strict.hints
        else:
            assert strict.message == plain.message


def test_interrupted_runs_trigger_error() -> None:
    run = _summarized_run()
    run.status = Status.PASSED
    run.interrupted = True
    assert trigger_status(run) is Status.ERROR
    message = build_message(run, _channel(), _context())
    assert message.status_label == "INTERRUPTED" and "INTERRUPTED" in message.title


def test_report_url_placeholders_are_filled() -> None:
    from datetime import UTC, datetime

    from dvf.execution.context import RunInfo

    info = RunInfo(run_id="r-1", started_at=datetime(2026, 11, 8, tzinfo=UTC))
    context = MessageContext(info=info)
    channel = _channel(report_url="https://ci.example.test/${run.date}/${run.id}/report.html")
    message = build_message(_summarized_run(), channel, context)
    assert message.report_url == "https://ci.example.test/2026-11-08/r-1/report.html"


# ---------------------------------------------------------------------- payloads


def test_slack_payload_uses_block_kit() -> None:
    message = build_message(
        _summarized_run(),
        _channel(report_url="https://ci.example.test/report.html", include_samples=True),
        _context(),
    )
    payload = slack_payload(message, mention="<!here>")
    assert payload["text"].startswith("<!here> synthetic-migration: ERROR")
    blocks = payload["blocks"]
    assert blocks[0]["type"] == "header" and len(blocks[0]["text"]["text"]) <= 150
    assert blocks[1] == {"type": "section", "text": {"type": "mrkdwn", "text": "<!here>"}}
    fields = next(b for b in blocks if b.get("fields"))["fields"]
    assert len(fields) <= 10 and fields[0]["text"] == "*Status*\nERROR"
    for block in blocks:
        if block["type"] == "section" and "text" in block:
            assert len(block["text"]["text"]) <= 3000
    button = next(b for b in blocks if b["type"] == "actions")["elements"][0]
    assert button["url"] == "https://ci.example.test/report.html"
    failures = next(b for b in blocks if "Top failures" in b.get("text", {}).get("text", ""))
    assert "&lt;script&gt;" in failures["text"]["text"] or "<script>" not in json.dumps(payload)


def test_slack_payload_shows_a_local_report_path_as_text() -> None:
    message = build_message(_summarized_run(), _channel(), _context())
    message = message.__class__(**{**message.__dict__, "report_path": "C:/reports/report.html"})
    payload = slack_payload(message)
    assert not any(b["type"] == "actions" for b in payload["blocks"])
    assert "C:/reports/report.html" in json.dumps(payload)


def test_teams_payload_is_an_adaptive_card() -> None:
    message = build_message(
        _summarized_run(), _channel(report_url="https://ci.example.test/r.html"), _context()
    )
    payload = teams_payload(message)
    assert payload["type"] == "message"
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["body"][0]["text"] == "synthetic-migration: ERROR"
    assert card["body"][0]["color"] == "Attention"
    facts = {f["title"]: f["value"] for f in card["body"][1]["facts"]}
    assert facts["Status"] == "ERROR" and facts["Run"] == "20261106T093000Z-abc123"
    assert card["actions"][0] == {
        "type": "Action.OpenUrl",
        "title": "Open report",
        "url": "https://ci.example.test/r.html",
    }
    assert len(json.dumps(payload)) < 28_000  # the Teams message size limit


# ---------------------------------------------------------------------- delivery


def _deliver(config: NotificationConfig, registry: Registry, **kwargs: Any) -> Any:
    message = build_message(_summarized_run(), config, _context())
    dispatcher = Dispatcher(registry, sleep=kwargs.pop("sleep", no_sleep))
    return dispatcher.deliver(Channel(config.name or config.type, config), message)


def test_slack_end_to_end(http_server: HttpRecorder, registry: Registry) -> None:
    config = SlackNotification.model_validate(
        {"type": "slack", "webhook": f"{http_server.url}/services/{SECRET_PATH}"}
    )
    delivery = _deliver(config, registry)
    assert delivery.ok and delivery.attempts == 1, delivery.error
    request = http_server.requests[0]
    assert request.method == "POST" and request.path == f"/services/{SECRET_PATH}"
    assert request.headers["Content-Type"].startswith("application/json")
    assert request.headers["User-Agent"].startswith("dvf/")
    assert request.json()["blocks"][0]["type"] == "header"


def test_teams_end_to_end(http_server: HttpRecorder, registry: Registry) -> None:
    http_server.default = (202, "", {})
    config = TeamsNotification.model_validate({"type": "teams", "webhook": http_server.url})
    delivery = _deliver(config, registry)
    assert delivery.ok, delivery.error
    body = http_server.requests[0].json()
    assert body["attachments"][0]["content"]["type"] == "AdaptiveCard"


def test_generic_webhook_end_to_end(http_server: HttpRecorder, registry: Registry) -> None:
    config = GenericWebhookNotification.model_validate(
        {
            "type": "webhook",
            "webhook": f"{http_server.url}/dq/events",
            "method": "PUT",
            "headers": {"Authorization": "Bearer synthetic-token-1234", "X-Team": "qa"},
        }
    )
    delivery = _deliver(config, registry)
    assert delivery.ok, delivery.error
    request = http_server.requests[0]
    assert request.method == "PUT" and request.path == "/dq/events"
    assert request.headers["Authorization"] == "Bearer synthetic-token-1234"
    assert request.headers["X-Team"] == "qa"
    body = request.json()
    assert body["schema"] == "dvf.notification/v1" and body["status"] == "error"
    assert body["top_failures"] and "samples" not in body["top_failures"][0]


def test_transient_failures_are_retried_with_retry_after(
    http_server: HttpRecorder, registry: Registry
) -> None:
    http_server.respond(503, "busy", {"Retry-After": "7"})
    http_server.respond(429, "slow down")
    delays: list[float] = []
    config = _channel(webhook=http_server.url, retries=2, retry_backoff_s=0.5)
    delivery = _deliver(config, registry, sleep=delays.append)
    assert delivery.ok and delivery.attempts == 3
    assert delays == [7.0, 1.0]  # Retry-After wins; then the doubled backoff


def test_permanent_failures_are_not_retried(http_server: HttpRecorder, registry: Registry) -> None:
    http_server.respond(404, "no_service")
    delivery = _deliver(_channel(webhook=http_server.url, retries=3), registry)
    assert not delivery.ok and delivery.attempts == 1
    assert delivery.error == "HTTP 404 Not Found: no_service"


def test_retries_stop_after_the_limit(http_server: HttpRecorder, registry: Registry) -> None:
    http_server.default = (500, "boom", {})
    delivery = _deliver(_channel(webhook=http_server.url, retries=2), registry)
    assert not delivery.ok and delivery.attempts == 3
    assert len(http_server.requests) == 3


def test_redirects_are_not_followed(http_server: HttpRecorder, registry: Registry) -> None:
    http_server.respond(302, "", {"Location": "https://elsewhere.example.test/"})
    delivery = _deliver(_channel(webhook=http_server.url), registry)
    assert not delivery.ok and "redirects are not followed" in (delivery.error or "")


def test_only_http_urls_are_accepted(tmp_path: Path, registry: Registry) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("do not read", encoding="utf-8")
    delivery = _deliver(_channel(webhook=secret_file.as_uri()), registry)
    assert not delivery.ok and "unsupported URL scheme 'file'" in (delivery.error or "")


def test_legacy_teams_error_text_is_a_failure(
    http_server: HttpRecorder, registry: Registry
) -> None:
    http_server.default = (
        200,
        "Webhook message delivery failed with error: Microsoft Teams endpoint returned HTTP "
        "error 429 with ContextId tcid=0",
        {},
    )
    config = TeamsNotification.model_validate(
        {"type": "teams", "webhook": http_server.url, "retries": 1}
    )
    delivery = _deliver(config, registry)
    assert not delivery.ok and delivery.attempts == 2
    assert "Teams rejected the message" in (delivery.error or "")


def test_webhook_secrets_never_appear_in_errors_or_logs(
    http_server: HttpRecorder, registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="dvf")
    # The server echoes the request path, which holds the secret, in its error body.
    http_server.respond(400, f"invalid_payload for /services/{SECRET_PATH}")
    url = f"{http_server.url}/services/{SECRET_PATH}"
    delivery = _deliver(
        SlackNotification.model_validate({"type": "slack", "webhook": url}), registry
    )
    assert not delivery.ok
    assert SECRET_PATH not in (delivery.error or "") and "***" in (delivery.error or "")
    refused = f"http://127.0.0.1:{closed_port()}/services/{SECRET_PATH}"
    config = SlackNotification.model_validate({"type": "slack", "webhook": refused, "retries": 1})
    delivery = _deliver(config, registry)
    assert not delivery.ok and delivery.attempts == 2
    assert "connection failed" in (delivery.error or "")
    assert SECRET_PATH not in caplog.text and SECRET_PATH not in (delivery.error or "")


def test_plugin_errors_are_contained(registry: Registry) -> None:
    class Broken:
        def __init__(self, config: NotificationConfig) -> None:
            self.config = config

        def send(self, message: Any) -> None:
            raise KeyError("boom")

    registry.register(PluginKind.NOTIFIER, "acme.broken", Broken, config_model=NotificationConfig)
    config = NotificationConfig.model_validate({"type": "acme.broken"})
    delivery = _deliver(config, registry)
    assert not delivery.ok and delivery.error == "KeyError: 'boom'"


def test_timeouts_are_transient(http_server: HttpRecorder) -> None:
    http_server.delay = 1.0
    with pytest.raises(NotificationError) as caught:
        post_json(http_server.url, {"x": 1}, timeout=0.2)
    assert caught.value.transient and "timed out after 0.2s" in str(caught.value)


def test_invalid_urls() -> None:
    for url in ("https://", "http://[::1"):
        with pytest.raises(NotificationError) as caught:
            post_json(url, {})
        assert not caught.value.transient


def test_retry_after_parsing() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    assert retry_after_seconds("5") == 5.0
    assert retry_after_seconds(None) is None and retry_after_seconds("soon") is None
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 100 < (retry_after_seconds(later) or 0) <= 120
    earlier = format_datetime(datetime.now(UTC) - timedelta(seconds=120), usegmt=True)
    assert retry_after_seconds(earlier) == 0.0


def test_is_transient() -> None:
    import smtplib
    import urllib.error

    assert is_transient(NotificationError("x", transient=True))
    assert not is_transient(NotificationError("x"))
    assert is_transient(ConnectionRefusedError())
    assert is_transient(TimeoutError())
    assert is_transient(smtplib.SMTPResponseException(451, b"try later"))
    assert not is_transient(smtplib.SMTPResponseException(550, b"no"))
    assert is_transient(smtplib.SMTPServerDisconnected())
    assert not is_transient(ValueError())
    assert is_transient(urllib.error.URLError("down"))


# ---------------------------------------------------------------------- the runner

PEOPLE = pa.table({"id": [1, 2, 3, 4], "email": ["a@example.test", None, "c@example.test", None]})


def _run_config(notifications: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "notify-demo"},
        "connections": {"db": {"type": "duckdb", "database": "db.duckdb"}},
        "suites": [
            {
                "name": "dq",
                "source": "db",
                "tables": [
                    {
                        "name": "people",
                        "source": {"table": "people"},
                        "checks": [{"type": "not_null", "columns": ["email"]}],
                    }
                ],
            }
        ],
        "history": {"enabled": False},
        "notifications": notifications,
        **extra,
    }


def test_run_sends_notifications_after_reports(http_server: HttpRecorder, tmp_path: Path) -> None:
    load_duckdb({"people": PEOPLE}, tmp_path / "db.duckdb")
    config = _run_config(
        [{"type": "webhook", "name": "hook", "webhook": http_server.url}],
        reports={"formats": ["json", "html"], "output_dir": "out"},
    )
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    output = api.run(loaded, run_id="r-notify")
    assert output.result.status is Status.FAILED
    assert [(d.channel, d.ok) for d in output.notifications] == [("hook", True)]
    body = http_server.requests[0].json()
    assert body["run_id"] == "r-notify" and body["status"] == "failed"
    assert body["top_failures"][0]["check_id"] == "dq.people.not_null.email"
    assert body["top_failures"][0]["unexpected_count"] == 2
    report = Path(body["report"]["path"])
    assert report.name == "report.html" and report.is_file()  # sent after reports exist


def test_run_respects_on_and_enabled(http_server: HttpRecorder, tmp_path: Path) -> None:
    load_duckdb({"people": PEOPLE}, tmp_path / "db.duckdb")
    config = _run_config(
        [
            {"type": "webhook", "webhook": http_server.url, "on": ["passed"]},
            {"type": "webhook", "webhook": http_server.url, "enabled": False},
        ]
    )
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    output = api.run(loaded, write_reports=False)
    assert output.notifications == [] and http_server.requests == []


def test_a_failing_channel_never_fails_the_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    load_duckdb({"people": PEOPLE}, tmp_path / "db.duckdb")
    url = f"http://127.0.0.1:{closed_port()}/hook/{SECRET_PATH}"
    config = _run_config([{"type": "webhook", "webhook": url, "retries": 0, "timeout_s": 2}])
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    with caplog.at_level(logging.WARNING, logger="dvf"):
        output = api.run(loaded, write_reports=False)
    assert output.result.status is Status.FAILED
    assert len(output.notifications) == 1 and not output.notifications[0].ok
    assert "notification 'webhook' (webhook) failed" in caplog.text
    assert SECRET_PATH not in caplog.text


def test_a_crashed_run_notifies_error_channels(http_server: HttpRecorder, tmp_path: Path) -> None:
    from dvf.core.errors import DvfError

    load_duckdb({"people": PEOPLE}, tmp_path / "db.duckdb")
    config = _run_config([{"type": "webhook", "webhook": http_server.url, "on": ["error"]}])
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    with pytest.raises(DvfError):
        api.run(loaded, formats=["no_such_format"], output_dir=tmp_path / "out")
    body = http_server.requests[0].json()
    assert body["status"] == "error" and "no_such_format" in body["error"]


def test_notify_run_with_markdown_summary_for_email(tmp_path: Path, registry: Registry) -> None:
    sent: list[Any] = []

    class Capture:
        def __init__(self, config: NotificationConfig) -> None:
            self.config = config

        def send(self, message: Any) -> None:
            sent.append(message)

    registry.register(
        PluginKind.NOTIFIER, "email", Capture, replace_existing=True, config_model=EmailNotification
    )
    loaded = api.load_config_data(
        {
            "version": 1,
            "project": {"name": "demo"},
            "notifications": [
                {
                    "type": "email",
                    "recipients": ["qa@example.test"],
                    "smtp_host": "localhost",
                    "attach_summary": True,
                }
            ],
        },
        base_dir=tmp_path,
        registry=registry,
    )
    deliveries = notify_run(_summarized_run(), loaded.config, registry, _context())
    assert deliveries[0].ok
    assert sent[0].summary_markdown and "synthetic-migration" in sent[0].summary_markdown


# ---------------------------------------------------------------------- dvf notify test


def _write_config(tmp_path: Path, notifications: list[dict[str, Any]]) -> Path:
    path = tmp_path / "dvf.yaml"
    path.write_text(
        json.dumps({"version": 1, "project": {"name": "cli-demo"}, "notifications": notifications}),
        encoding="utf-8",
    )
    return path


def test_notify_test_command(http_server: HttpRecorder, tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        [
            {"type": "slack", "webhook": http_server.url},
            {"type": "webhook", "name": "off", "webhook": http_server.url, "enabled": False},
        ],
    )
    result = cli.invoke(app, ["notify", "test", str(path)])
    assert result.exit_code == 0, result.output
    assert "sent" in result.output and len(http_server.requests) == 1
    payload = http_server.requests[0].json()
    assert payload["text"].startswith("dvf test notification for cli-demo")
    assert "TEST" in json.dumps(payload["blocks"])

    named = cli.invoke(app, ["notify", "test", str(path), "--channel", "off", "--format", "json"])
    assert named.exit_code == 0, named.output
    assert json.loads(named.stdout)[0]["channel"] == "off"
    assert http_server.requests[1].json()["event"] == "test"


def test_notify_test_command_reports_failures(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{closed_port()}/{SECRET_PATH}"
    path = _write_config(tmp_path, [{"type": "webhook", "webhook": url, "retries": 0}])
    result = cli.invoke(app, ["notify", "test", str(path), "--format", "json"])
    assert result.exit_code == 2
    rows = json.loads(result.stdout)
    assert rows[0]["ok"] is False and "connection failed" in rows[0]["error"]
    assert SECRET_PATH not in result.output


def test_notify_test_command_errors(tmp_path: Path) -> None:
    empty = _write_config(tmp_path, [])
    assert cli.invoke(app, ["notify", "test", str(empty)]).exit_code == 3
    path = _write_config(tmp_path, [{"type": "slack", "webhook": "https://x.example.test"}])
    unknown = cli.invoke(app, ["notify", "test", str(path), "--channel", "nope"])
    assert unknown.exit_code == 3
    assert "No notification channel named 'nope'" in unknown.output
