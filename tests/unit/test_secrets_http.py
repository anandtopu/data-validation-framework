"""HashiCorp Vault and Databricks providers against a local fake HTTP server (synthetic values).

A live Vault check runs when ``DVF_TEST_VAULT_ADDR`` and ``DVF_TEST_VAULT_TOKEN`` point at a dev
server seeded with ``secret/dvf/pg`` (``username``, ``password``); it is marked ``integration``.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from dvf.core.errors import SecretError
from dvf.packs.secrets.databricks_provider import DatabricksSecretProvider
from dvf.plugins.registry import Registry
from dvf.security.redaction import Redactor
from dvf.security.secrets import SecretContext, SecretRef, SecretResolver

Reply = tuple[int, Any] | tuple[int, Any, dict[str, str]]


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeServer:
    url: str
    routes: dict[tuple[str, str], Reply | Callable[[Seen], Reply]] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)


@pytest.fixture
def server() -> Iterator[FakeServer]:
    state = FakeServer(url="")

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            seen = Seen(
                self.command,
                parts.path,
                parse_qs(parts.query),
                {k.lower(): v for k, v in self.headers.items()},
                self.rfile.read(length) if length else b"",
            )
            state.seen.append(seen)
            route = state.routes.get((self.command, parts.path))
            reply: Reply = route(seen) if callable(route) else (route or (404, {"errors": []}))
            status, payload = reply[0], reply[1]
            headers = reply[2] if len(reply) == 3 else {}  # type: ignore[misc]
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _handle  # noqa: N815

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _resolver(
    registry: Registry,
    environ: dict[str, str],
    options: dict[str, Any] | None = None,
    base_dir: Path | None = None,
) -> tuple[SecretResolver, Redactor]:
    redactor = Redactor()
    context = SecretContext(environ=environ, options=options or {}, base_dir=base_dir or Path.cwd())
    return SecretResolver(registry, context, redactor), redactor


def _resolve(resolver: SecretResolver, text: str) -> str:
    return resolver.resolve(SecretRef.parse(text))


# ====================================================================== Vault

TOKEN = "hvs.synthetic-root-token"


def _vault_routes(server: FakeServer) -> None:
    def authorized(reply: Reply) -> Callable[[Seen], Reply]:
        def handler(seen: Seen) -> Reply:
            if seen.headers.get("x-vault-token") not in (TOKEN, "hvs.synthetic-approle"):
                return 403, {"errors": ["permission denied"]}
            return reply

        return handler

    mounts = {
        "secret/app/db": {"path": "secret/", "type": "kv", "options": {"version": "2"}},
        "secret/app/one": {"path": "secret/", "type": "kv", "options": {"version": "2"}},
        "secret/app/gone": {"path": "secret/", "type": "kv", "options": {"version": "2"}},
        "kv1/legacy": {"path": "kv1/", "type": "kv", "options": None},
        "team/kv/nested/app": {"path": "team/kv/", "type": "kv", "options": {"version": "2"}},
    }
    for path, data in mounts.items():
        server.routes[("GET", f"/v1/sys/internal/ui/mounts/{path}")] = authorized(
            (200, {"data": data})
        )
    server.routes[("GET", "/v1/secret/data/app/db")] = authorized(
        (
            200,
            {
                "data": {
                    "data": {"username": "dvf", "password": "synthetic-vault-pw"},
                    "metadata": {},
                }
            },
        )
    )
    server.routes[("GET", "/v1/secret/data/app/one")] = authorized(
        (200, {"data": {"data": {"api_key": "synthetic-only-key"}, "metadata": {}}})
    )
    server.routes[("GET", "/v1/secret/data/app/gone")] = authorized(
        (200, {"data": {"data": None, "metadata": {"deleted_time": "2026-01-01T00:00:00Z"}}})
    )
    server.routes[("GET", "/v1/kv1/legacy")] = authorized((200, {"data": {"pw": "synthetic-kv1"}}))
    server.routes[("GET", "/v1/team/kv/data/nested/app")] = authorized(
        (200, {"data": {"data": {"token": "synthetic-nested"}}})
    )


def test_vault_kv2_kv1_detection_and_cache(registry: Registry, server: FakeServer) -> None:
    _vault_routes(server)
    resolver, redactor = _resolver(
        registry, {"VAULT_ADDR": server.url, "VAULT_TOKEN": TOKEN, "VAULT_NAMESPACE": "team-a/"}
    )
    assert _resolve(resolver, "vault:secret/app/db#password") == "synthetic-vault-pw"
    assert _resolve(resolver, "vault:secret/app/db#username") == "dvf"
    assert _resolve(resolver, "vault:secret/app/one") == "synthetic-only-key"
    assert _resolve(resolver, "vault:kv1/legacy#pw") == "synthetic-kv1"
    assert _resolve(resolver, "vault:team/kv/nested/app#token") == "synthetic-nested"
    reads = [s.path for s in server.seen if not s.path.startswith("/v1/sys/")]
    assert reads.count("/v1/secret/data/app/db") == 1  # two keys, one request
    assert {s.headers.get("x-vault-namespace") for s in server.seen} == {"team-a"}
    assert all(s.headers.get("x-vault-request") == "true" for s in server.seen)
    assert TOKEN in redactor and "synthetic-vault-pw" in redactor


@pytest.mark.parametrize(
    ("reference", "match"),
    [
        ("vault:secret/app/db", r"has 2 keys \(password, username\)"),
        ("vault:secret/app/db#nope", "has no key 'nope'"),
        ("vault:secret/app/gone#x", "is deleted"),
        ("vault:secret/app/missing#x", "was not found"),
        ("vault:secret/../sys/x#k", r"must not contain '\.' or '\.\.'"),
    ],
)
def test_vault_errors(registry: Registry, server: FakeServer, reference: str, match: str) -> None:
    _vault_routes(server)
    resolver, _ = _resolver(registry, {"VAULT_ADDR": server.url, "VAULT_TOKEN": TOKEN})
    with pytest.raises(SecretError, match=match) as info:
        _resolve(resolver, reference)
    assert TOKEN not in str(info.value)


def test_vault_bad_token_and_fallback_without_detection(
    registry: Registry, server: FakeServer
) -> None:
    _vault_routes(server)
    resolver, _ = _resolver(registry, {"VAULT_ADDR": server.url, "VAULT_TOKEN": "hvs.wrong-token"})
    with pytest.raises(SecretError, match="needs the 'read' capability") as info:
        _resolve(resolver, "vault:secret/app/db#password")
    assert "hvs.wrong-token" not in str(info.value)
    # detection was denied (403), so the first segment was taken as a KV v2 mount
    assert server.seen[-1].path == "/v1/secret/data/app/db"


def test_vault_explicit_mount_and_version(registry: Registry, server: FakeServer) -> None:
    _vault_routes(server)
    resolver, _ = _resolver(
        registry,
        {"VAULT_ADDR": server.url, "VAULT_TOKEN": TOKEN},
        {"vault": {"mount": "kv1", "kv_version": 1}},
    )
    assert _resolve(resolver, "vault:kv1/legacy#pw") == "synthetic-kv1"
    assert not any(s.path.startswith("/v1/sys/") for s in server.seen)
    with pytest.raises(SecretError, match="not inside the configured mount"):
        _resolve(resolver, "vault:secret/app/db#password")


def test_vault_approle_login(registry: Registry, server: FakeServer, tmp_path: Path) -> None:
    _vault_routes(server)

    def login(seen: Seen) -> Reply:
        body = json.loads(seen.body)
        if body == {"role_id": "synthetic-role", "secret_id": "synthetic-secret-id"}:
            return 200, {"auth": {"client_token": "hvs.synthetic-approle"}}
        return 400, {"errors": ["invalid role or secret ID"]}

    server.routes[("POST", "/v1/auth/ci-approle/login")] = login
    (tmp_path / "secret-id").write_text("synthetic-secret-id\n", encoding="utf-8")
    resolver, redactor = _resolver(
        registry,
        {
            "VAULT_ADDR": server.url,
            "VAULT_ROLE_ID": "synthetic-role",
            "DVF_VAULT_SECRET_ID_FILE": "secret-id",
            "DVF_VAULT_APPROLE_MOUNT": "ci-approle",
        },
        base_dir=tmp_path,
    )
    assert _resolve(resolver, "vault:secret/app/db#password") == "synthetic-vault-pw"
    assert "hvs.synthetic-approle" in redactor and "synthetic-secret-id" in redactor
    logins = [s for s in server.seen if s.path.endswith("/login")]
    assert len(logins) == 1
    bad, _ = _resolver(
        registry,
        {"VAULT_ADDR": server.url, "VAULT_ROLE_ID": "x", "VAULT_SECRET_ID": "wrong-secret-id"},
        {"vault": {"approle_mount": "ci-approle"}},
    )
    with pytest.raises(
        SecretError, match=r"AppRole login failed \(HTTP 400\): invalid role"
    ) as info:
        _resolve(bad, "vault:secret/app/db#password")
    assert "wrong-secret-id" not in str(info.value)


def test_vault_token_file_and_missing_credentials(
    registry: Registry, server: FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault_routes(server)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    resolver, _ = _resolver(registry, {"VAULT_ADDR": server.url})
    with pytest.raises(SecretError, match="No Vault credentials"):
        _resolve(resolver, "vault:secret/app/db#password")
    (tmp_path / ".vault-token").write_text(TOKEN + "\n", encoding="utf-8")
    resolver, _ = _resolver(registry, {"VAULT_ADDR": server.url})
    assert _resolve(resolver, "vault:secret/app/db#password") == "synthetic-vault-pw"


def test_vault_refuses_redirects_plain_http_and_missing_address(
    registry: Registry, server: FakeServer
) -> None:
    server.routes[("GET", "/v1/sys/internal/ui/mounts/secret/app/db")] = (
        307,
        None,
        {"Location": "http://203.0.113.9/v1/steal"},
    )
    resolver, _ = _resolver(registry, {"VAULT_ADDR": server.url, "VAULT_TOKEN": TOKEN})
    with pytest.raises(SecretError, match="redirect"):
        _resolve(resolver, "vault:secret/app/db#password")
    assert len(server.seen) == 1  # the redirect target was never contacted
    no_addr, _ = _resolver(registry, {"VAULT_TOKEN": TOKEN})
    with pytest.raises(SecretError, match="set VAULT_ADDR"):
        _resolve(no_addr, "vault:secret/app/db#password")
    remote, _ = _resolver(
        registry, {"VAULT_ADDR": "http://vault.internal:8200", "VAULT_TOKEN": TOKEN}
    )
    with pytest.raises(SecretError, match="plain HTTP"):
        _resolve(remote, "vault:secret/app/db#password")


def test_vault_tls_settings_are_checked(registry: Registry, tmp_path: Path) -> None:
    resolver, _ = _resolver(
        registry,
        {
            "VAULT_ADDR": "https://vault.example.org:8200",
            "VAULT_TOKEN": TOKEN,
            "VAULT_CACERT": "x.pem",
        },
        base_dir=tmp_path,
    )
    with pytest.raises(SecretError, match=r"VAULT_CACERT file 'x.pem' does not exist"):
        _resolve(resolver, "vault:secret/app/db#password")


def test_vault_unreachable(registry: Registry) -> None:
    resolver, _ = _resolver(
        registry,
        {"VAULT_ADDR": "http://127.0.0.1:9", "VAULT_TOKEN": TOKEN, "DVF_VAULT_TIMEOUT": "0.5"},
    )
    with pytest.raises(SecretError, match=r"Cannot reach HashiCorp Vault at 127.0.0.1:9") as info:
        _resolve(resolver, "vault:secret/app/db#password")
    assert TOKEN not in str(info.value)


@pytest.mark.integration
def test_vault_live() -> None:
    addr, token = os.environ.get("DVF_TEST_VAULT_ADDR"), os.environ.get("DVF_TEST_VAULT_TOKEN")
    if not addr or not token:
        pytest.skip("set DVF_TEST_VAULT_ADDR and DVF_TEST_VAULT_TOKEN to run against a dev server")
    from dvf.plugins.registry import new_registry

    resolver, _ = _resolver(new_registry(), {"VAULT_ADDR": addr, "VAULT_TOKEN": token})
    assert _resolve(resolver, "vault:secret/dvf/pg#password")
    with pytest.raises(SecretError, match="was not found"):
        _resolve(resolver, "vault:secret/dvf/does-not-exist#x")


# ====================================================================== Databricks

DBX_TOKEN = "dapi-synthetic-token"


def _encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _databricks_routes(server: FakeServer) -> None:
    values = {
        ("migration", "pg-password"): "synthetic-dbx-pw",
        ("migration", "config"): json.dumps({"user": "dvf", "password": "synthetic-json"}),
    }

    def get(seen: Seen) -> Reply:
        if seen.headers.get("authorization") not in (
            f"Bearer {DBX_TOKEN}",
            "Bearer oauth-synthetic",
        ):
            return 403, {"error_code": "PERMISSION_DENIED", "message": "Invalid access token."}
        scope, key = seen.query["scope"][0], seen.query["key"][0]
        if (scope, key) not in values:
            return 404, {
                "error_code": "RESOURCE_DOES_NOT_EXIST",
                "message": "Secret does not exist",
            }
        return 200, {"key": key, "value": _encoded(values[(scope, key)])}

    def token(seen: Seen) -> Reply:
        expected = "Basic " + base64.b64encode(b"sp-app-id:synthetic-sp-secret").decode()
        form = parse_qs(seen.body.decode())
        if seen.headers.get("authorization") == expected and form["grant_type"] == [
            "client_credentials"
        ]:
            return 200, {
                "access_token": "oauth-synthetic",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        return 401, {"error": "invalid_client", "error_description": "Client authentication failed"}

    server.routes[("GET", "/api/2.0/secrets/get")] = get
    server.routes[("POST", "/oidc/v1/token")] = token


def test_databricks_rest_with_token(registry: Registry, server: FakeServer) -> None:
    _databricks_routes(server)
    resolver, redactor = _resolver(
        registry, {"DATABRICKS_HOST": server.url, "DATABRICKS_TOKEN": DBX_TOKEN}
    )
    assert _resolve(resolver, "databricks:migration#pg-password") == "synthetic-dbx-pw"
    assert _resolve(resolver, "databricks:migration/pg-password") == "synthetic-dbx-pw"
    assert _resolve(resolver, "databricks:migration/config#password") == "synthetic-json"
    gets = [s for s in server.seen if s.path == "/api/2.0/secrets/get"]
    assert len(gets) == 2  # pg-password fetched once for both spellings
    assert DBX_TOKEN in redactor and "synthetic-dbx-pw" in redactor


@pytest.mark.parametrize(
    ("environ", "reference", "match"),
    [
        ({"DATABRICKS_TOKEN": DBX_TOKEN}, "databricks:migration#missing", "was not found"),
        ({"DATABRICKS_TOKEN": "dapi-wrong"}, "databricks:migration#pg-password", "READ permission"),
        ({}, "databricks:migration#pg-password", "No Databricks credentials"),
        ({"DATABRICKS_TOKEN": DBX_TOKEN}, "databricks:migration", "Invalid databricks reference"),
        (
            {"DATABRICKS_TOKEN": DBX_TOKEN},
            "databricks:bad scope#k",
            "not a valid Databricks secret scope",
        ),
    ],
)
def test_databricks_errors(
    registry: Registry,
    server: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environ: dict[str, str],
    reference: str,
    match: str,
) -> None:
    _databricks_routes(server)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    resolver, _ = _resolver(registry, {"DATABRICKS_HOST": server.url, **environ})
    with pytest.raises(SecretError, match=match) as info:
        _resolve(resolver, reference)
    assert "dapi-wrong" not in str(info.value)


def test_databricks_oauth_m2m(registry: Registry, server: FakeServer) -> None:
    _databricks_routes(server)
    resolver, redactor = _resolver(
        registry,
        {
            "DATABRICKS_HOST": server.url,
            "DATABRICKS_CLIENT_ID": "sp-app-id",
            "DATABRICKS_CLIENT_SECRET": "synthetic-sp-secret",
        },
    )
    assert _resolve(resolver, "databricks:migration#pg-password") == "synthetic-dbx-pw"
    assert "oauth-synthetic" in redactor and "synthetic-sp-secret" in redactor
    bad, _ = _resolver(
        registry,
        {
            "DATABRICKS_HOST": server.url,
            "DATABRICKS_CLIENT_ID": "x",
            "DATABRICKS_CLIENT_SECRET": "bad-secret",
        },
    )
    with pytest.raises(SecretError, match=r"OAuth token request failed \(HTTP 401\): Client auth"):
        _resolve(bad, "databricks:migration#pg-password")


def test_databricks_config_profile(registry: Registry, server: FakeServer, tmp_path: Path) -> None:
    _databricks_routes(server)
    config = tmp_path / "databrickscfg"
    config.write_text(
        f"[DEFAULT]\nhost = https://unused.example.org\n\n[migration]\nhost = {server.url}\n"
        f"token = {DBX_TOKEN}\n",
        encoding="utf-8",
    )
    resolver, redactor = _resolver(
        registry, {"DATABRICKS_CONFIG_FILE": str(config), "DATABRICKS_CONFIG_PROFILE": "migration"}
    )
    assert _resolve(resolver, "databricks:migration#pg-password") == "synthetic-dbx-pw"
    assert DBX_TOKEN in redactor
    missing, _ = _resolver(
        registry, {"DATABRICKS_CONFIG_FILE": str(config), "DATABRICKS_CONFIG_PROFILE": "nope"}
    )
    with pytest.raises(SecretError, match=r"Profile 'nope' is not in .*profiles: migration"):
        _resolve(missing, "databricks:migration#pg-password")


def test_databricks_needs_a_host_and_https(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    resolver, _ = _resolver(registry, {"DATABRICKS_TOKEN": DBX_TOKEN})
    with pytest.raises(SecretError, match="set DATABRICKS_HOST"):
        _resolve(resolver, "databricks:migration#pg-password")
    plain, _ = _resolver(
        registry,
        {"DATABRICKS_HOST": "http://adb-1.azuredatabricks.net", "DATABRICKS_TOKEN": DBX_TOKEN},
    )
    with pytest.raises(SecretError, match="plain HTTP"):
        _resolve(plain, "databricks:migration#pg-password")


def test_databricks_uses_dbutils_inside_databricks(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def get(scope: str, key: str) -> str:
        calls.append((scope, key))
        if key == "missing":
            raise RuntimeError(f"Secret does not exist with scope: {scope} and key: {key}")
        return "synthetic-dbutils-value"

    fake = types.SimpleNamespace(secrets=types.SimpleNamespace(get=get))
    monkeypatch.setattr(DatabricksSecretProvider, "_get_dbutils", lambda self: fake)
    resolver, redactor = _resolver(registry, {"DATABRICKS_RUNTIME_VERSION": "16.4"})
    assert _resolve(resolver, "databricks:migration#pg-password") == "synthetic-dbutils-value"
    assert calls == [("migration", "pg-password")]
    assert "synthetic-dbutils-value" in redactor
    with pytest.raises(SecretError, match=r"dbutils.secrets.get failed .*does not exist"):
        _resolve(resolver, "databricks:migration#missing")
    monkeypatch.setattr(DatabricksSecretProvider, "_get_dbutils", lambda self: None)
    always, _ = _resolver(registry, {}, {"databricks": {"use_dbutils": "always"}})
    with pytest.raises(SecretError, match="dbutils is not available"):
        _resolve(always, "databricks:migration#pg-password")
