"""Regression tests for the Phase 4 security hardening fixes (synthetic data only)."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from dvf import api
from dvf.config.connections import SapRfcConnection
from dvf.core.errors import ConfigError, ConnectorError
from dvf.execution.artifacts import safe_name
from dvf.execution.checkpoint import CheckpointStore, check_run_id, load_results
from dvf.packs.sap.rfc.odata import ODataBackend
from dvf.security.redaction import MASK, global_redactor, redact_args

# ---------------------------------------------------------------------- run IDs and file names


@pytest.mark.parametrize("run_id", ["20261106T101500Z-3f9a", "r1", "batch-3-abc123", "R0.v2"])
def test_valid_run_ids(run_id: str, tmp_path: Path) -> None:
    assert check_run_id(run_id) == run_id
    assert CheckpointStore(tmp_path, run_id).directory == tmp_path / "runs" / run_id


@pytest.mark.parametrize(
    "run_id", ["../up", "..", "a/b", "a\\b", "D:x", "", ".hidden", "a..b", "x" * 129]
)
def test_run_ids_cannot_leave_the_state_folder(run_id: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Invalid run ID"):
        CheckpointStore(tmp_path, run_id)
    with pytest.raises(ConfigError, match="Invalid run ID"):
        load_results(tmp_path, run_id)


def test_run_rejects_a_traversing_run_id(tmp_path: Path) -> None:
    config = {"version": 1, "project": {"name": "p"}}
    loaded = api.load_config_data(config, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="Invalid run ID"):
        api.run(loaded, run_id="../../up")
    assert not (tmp_path.parent / "up").exists()


@pytest.mark.parametrize(
    ("check_id", "expected"),
    [
        ("s.t.not_null.a+b", "s.t.not_null.a+b"),
        ("..", "_"),
        (".", "_"),
        ("", "_"),
        ("../../up/file", "__.._up_file"),
        ("a/b\\c:d", "a_b_c_d"),
    ],
)
def test_safe_name(check_id: str, expected: str) -> None:
    assert safe_name(check_id) == expected


# ---------------------------------------------------------------------- redaction


def test_redact_args_masks_credentials_in_overrides() -> None:
    global_redactor().add("synthetic-registered-value")
    args = [
        "run",
        "dvf.yaml",
        "--set",
        "connections.pg.password=pw-synthetic-1",
        "--set=connections.sf.token=tok-synthetic",
        "--var",
        "api_key=k-synthetic",
        "--set",
        "connections.pg.host=db.example.org",
        "--set",
        "connections.pg.password=${secret:env:PG_PASSWORD}",
        "--note=synthetic-registered-value",
        "DVF__CONNECTIONS__PG__PASSWORD=env-synthetic",
    ]
    assert redact_args(args) == [
        "run",
        "dvf.yaml",
        "--set",
        f"connections.pg.password={MASK}",
        f"--set=connections.sf.token={MASK}",
        "--var",
        f"api_key={MASK}",
        "--set",
        "connections.pg.host=db.example.org",
        "--set",
        "connections.pg.password=${secret:env:PG_PASSWORD}",
        f"--note={MASK}",
        f"DVF__CONNECTIONS__PG__PASSWORD={MASK}",
    ]


def test_run_environment_redacts_cli_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"version": 1, "project": {"name": "p"}}
    loaded = api.load_config_data(config, base_dir=tmp_path)
    argv = ["dvf", "run", "--set", "connections.pg.password=pw-synthetic-1"]
    monkeypatch.setattr(sys, "argv", argv)
    output = api.run(loaded, write_reports=False)
    assert output.result.environment.cli_args == [
        "run",
        "--set",
        f"connections.pg.password={MASK}",
    ]


def test_config_diagnostics_do_not_echo_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DVF_T_PORT", "synthetic-port-value")
    config: dict[str, Any] = {
        "version": 1,
        "project": {"name": "p"},
        "connections": {
            "pg": {
                "type": "postgres",
                "host": "localhost",
                "port": "${secret:env:DVF_T_PORT}",
                "database": "d",
            }
        },
    }
    with pytest.raises(ConfigError) as info:
        api.load_config_data(config, base_dir=tmp_path)
    text = str(info.value)
    assert "synthetic-port-value" not in text
    assert "Invalid value for 'port'" in text
    plain = {**config, "connections": {"pg": {**config["connections"]["pg"], "port": "abc"}}}
    with pytest.raises(ConfigError, match="Invalid value 'abc' for 'port'"):
        api.load_config_data(plain, base_dir=tmp_path)


# ---------------------------------------------------------------------- OData origin confinement


@pytest.fixture
def two_servers() -> Iterator[tuple[str, str, list[str | None]]]:
    seen_elsewhere: list[str | None] = []
    state: dict[str, str] = {}

    class Main(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/svc/moved"):
                self.send_response(302)
                self.send_header("Location", state["other"] + "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"value": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    class Other(Main):
        def do_GET(self) -> None:
            seen_elsewhere.append(self.headers.get("Authorization"))
            super().do_GET()

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (Main, Other)]
    threads = [
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        for s in servers
    ]
    for thread in threads:
        thread.start()
    main_url, other_url = (f"http://127.0.0.1:{s.server_address[1]}" for s in servers)
    state["other"] = other_url
    try:
        yield main_url, other_url, seen_elsewhere
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _odata(url: str) -> ODataBackend:
    config = SapRfcConnection.model_validate(
        {
            "type": "sap_rfc",
            "backend": "odata",
            "url": url,
            "client": "200",
            "user": "DVF_TEST",
            "password": "synthetic-odata-password",
        }
    )
    return ODataBackend(config)


def test_odata_never_sends_credentials_to_another_host(
    two_servers: tuple[str, str, list[str | None]],
) -> None:
    main, other_url, seen_elsewhere = two_servers
    backend = _odata(f"{main}/svc")
    assert backend.get("Entity") == b'{"value": []}'
    with pytest.raises(ConnectorError, match="another host"):
        backend.get(f"{other_url}/svc/Entity?$skiptoken=3")  # a next link pointing elsewhere
    with pytest.raises(ConnectorError, match="HTTP 302"):
        backend.get("moved")  # a redirect to another origin is not followed
    assert seen_elsewhere == []
