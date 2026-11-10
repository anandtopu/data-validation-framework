"""Keyring, AWS Secrets Manager, Azure Key Vault, and Google Secret Manager providers.

Keyring uses an in-process backend, AWS uses botocore's Stubber, Azure and Google use fake client
modules. All values are synthetic.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from dvf.core.errors import SecretError
from dvf.packs.secrets.aws_provider import AWSSecretsManagerProvider
from dvf.packs.secrets.azure_provider import AzureKeyVaultProvider
from dvf.packs.secrets.gcp_provider import GoogleSecretManagerProvider
from dvf.plugins.registry import Registry
from dvf.security.redaction import Redactor
from dvf.security.secrets import SecretContext, SecretRef, SecretResolver


def _resolver(
    registry: Registry, environ: dict[str, str] | None = None, options: dict[str, Any] | None = None
) -> tuple[SecretResolver, Redactor]:
    redactor = Redactor()
    context = SecretContext(environ=environ or {}, options=options or {})
    return SecretResolver(registry, context, redactor), redactor


def _resolve(resolver: SecretResolver, text: str) -> str:
    return resolver.resolve(SecretRef.parse(text))


# ====================================================================== keyring


@pytest.fixture
def memory_keyring() -> Iterator[dict[tuple[str, str], str]]:
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.credentials import SimpleCredential

    store: dict[tuple[str, str], str] = {}

    class MemoryKeyring(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def get_password(self, service: str, username: str) -> str | None:
            if service == "broken":
                raise RuntimeError("backend locked")
            return store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            store.pop((service, username), None)

        def get_credential(self, service: str, username: str | None) -> SimpleCredential | None:
            for (svc, user), password in store.items():
                if svc == service and (username is None or user == username):
                    return SimpleCredential(user, password)
            return None

    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    try:
        yield store
    finally:
        keyring.set_keyring(previous)


def test_keyring_provider(registry: Registry, memory_keyring: dict[tuple[str, str], str]) -> None:
    memory_keyring[("dvf-prod-pg", "dvf_reader")] = "synthetic-keyring-pw"
    resolver, redactor = _resolver(registry)
    assert _resolve(resolver, "keyring:dvf-prod-pg#dvf_reader") == "synthetic-keyring-pw"
    assert "synthetic-keyring-pw" in redactor
    assert _resolve(resolver, "keyring:dvf-prod-pg") == "synthetic-keyring-pw"  # any user
    with pytest.raises(SecretError, match=r"no password for user 'other'.*keyring set dvf-prod-pg"):
        _resolve(resolver, "keyring:dvf-prod-pg#other")
    with pytest.raises(SecretError, match=r"could not read service 'broken'.*backend locked"):
        _resolve(resolver, "keyring:broken#u")
    with_default, _ = _resolver(registry, {"DVF_KEYRING_USERNAME": "dvf_reader"})
    assert _resolve(with_default, "keyring:dvf-prod-pg") == "synthetic-keyring-pw"


def test_keyring_missing_library(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)
    resolver, _ = _resolver(registry)
    with pytest.raises(SecretError, match=r"pip install 'dvf-framework\[keyring\]'"):
        _resolve(resolver, "keyring:svc#user")


# ====================================================================== AWS


ARN = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:prod/db-AbCdEf"


def _stubbed_client(region: str = "eu-west-1") -> tuple[Any, Any]:
    import boto3
    from botocore.stub import Stubber

    session = boto3.session.Session(
        region_name=region, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    client = session.client("secretsmanager")
    return client, Stubber(client)


def _value(secret_string: str | None = None, binary: bytes | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"ARN": ARN, "Name": "prod/db", "VersionId": "v" * 32}
    if secret_string is not None:
        response["SecretString"] = secret_string
    if binary is not None:
        response["SecretBinary"] = binary
    return response


def test_awssm_json_keys_cache_and_regions(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, stub = _stubbed_client()
    regions: list[str | None] = []

    def new_client(self: AWSSecretsManagerProvider, region: str | None) -> Any:
        regions.append(region)
        return client

    monkeypatch.setattr(AWSSecretsManagerProvider, "_new_client", new_client)
    document = json.dumps({"username": "dvf", "password": "synthetic-aws-pw", "port": 5432})
    stub.add_response("get_secret_value", _value(document), {"SecretId": "prod/db"})
    stub.add_response("get_secret_value", _value("plain-synthetic"), {"SecretId": ARN})
    stub.add_response(
        "get_secret_value",
        _value(binary=b"synthetic-binary"),
        {"SecretId": "bin", "VersionStage": "AWSPREVIOUS"},
    )
    with stub:
        resolver, redactor = _resolver(registry, {"AWS_REGION": "eu-west-1"})
        assert _resolve(resolver, "awssm:prod/db#password") == "synthetic-aws-pw"
        assert _resolve(resolver, "awssm:prod/db#port") == "5432"  # same document, no new call
        assert _resolve(resolver, f"awssm:{ARN}") == "plain-synthetic"
        staged, _ = _resolver(
            registry, {"AWS_REGION": "eu-west-1"}, {"awssm": {"version_stage": "AWSPREVIOUS"}}
        )
        assert _resolve(staged, "awssm:bin") == "synthetic-binary"
        stub.assert_no_pending_responses()
    assert regions == ["eu-west-1", "eu-central-1", "eu-west-1"]  # an ARN's region wins
    assert "synthetic-aws-pw" in redactor
    with pytest.raises(SecretError, match="has no key 'nope'"):
        _resolve(resolver, "awssm:prod/db#nope")


@pytest.mark.parametrize(
    ("code", "match"),
    [
        ("ResourceNotFoundException", "was not found in region eu-west-1"),
        ("AccessDeniedException", "secretsmanager:GetSecretValue"),
        ("DecryptionFailure", "kms:Decrypt"),
        ("InternalServiceError", "InternalServiceError: synthetic service message"),
    ],
)
def test_awssm_client_errors(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, code: str, match: str
) -> None:
    client, stub = _stubbed_client()
    monkeypatch.setattr(AWSSecretsManagerProvider, "_new_client", lambda self, region: client)
    stub.add_client_error(
        "get_secret_value",
        service_error_code=code,
        service_message="synthetic service message",
        expected_params={"SecretId": "missing"},
    )
    with stub:
        resolver, _ = _resolver(registry, {"AWS_REGION": "eu-west-1"})
        with pytest.raises(SecretError, match=match):
            _resolve(resolver, "awssm:missing")


def test_awssm_setup_errors(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    resolver, _ = _resolver(registry)
    with pytest.raises(SecretError, match="No AWS region"):
        _resolve(resolver, "awssm:prod/db")
    profile, _ = _resolver(registry, {"DVF_AWSSM_PROFILE": "nope", "AWS_REGION": "eu-west-1"})
    with pytest.raises(SecretError, match="profile 'nope' does not exist"):
        _resolve(profile, "awssm:prod/db")
    with pytest.raises(SecretError, match="not a valid AWS Secrets Manager secret name"):
        _resolve(resolver, "awssm:bad name!")


# ====================================================================== Azure


class ResourceNotFoundError(Exception):
    status_code = 404


class ClientAuthenticationError(Exception):
    status_code = 401


class HttpResponseError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture
def fake_azure(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"credentials": [], "clients": [], "calls": []}
    vaults = {
        "https://kv-dvf-test.vault.azure.net": {
            "sap-password": {"latest": "synthetic-azure-pw", "v1": "synthetic-old"},
            "db-config": {"latest": json.dumps({"user": "dvf", "password": "synthetic-json-pw"})},
            "forbidden": {"latest": HttpResponseError("Forbidden by policy", 403)},
            "noauth": {"latest": ClientAuthenticationError("synthetic auth failure")},
        }
    }

    class Credential:
        def __init__(self, kind: str, **kwargs: Any) -> None:
            state["credentials"].append((kind, kwargs))

    identity = types.ModuleType("azure.identity")
    for kind in (
        "DefaultAzureCredential",
        "EnvironmentCredential",
        "ManagedIdentityCredential",
        "WorkloadIdentityCredential",
        "AzureCliCredential",
        "AzureDeveloperCliCredential",
        "AzurePowerShellCredential",
    ):
        setattr(identity, kind, lambda kind=kind, **kw: Credential(kind, **kw))

    class Secret:
        def __init__(self, value: str) -> None:
            self.value = value

    class SecretClient:
        def __init__(self, vault_url: str, credential: Any) -> None:
            state["clients"].append(vault_url)
            self.vault_url = vault_url

        def get_secret(self, name: str, version: str | None = None) -> Secret:
            state["calls"].append((self.vault_url, name, version))
            entry = vaults.get(self.vault_url, {}).get(name)
            if entry is None:
                raise ResourceNotFoundError(f"Secret not found: {name}")
            value = entry[version or "latest"]
            if isinstance(value, Exception):
                raise value
            return Secret(value)

    secrets_module = types.ModuleType("azure.keyvault.secrets")
    secrets_module.SecretClient = SecretClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, "azure.keyvault.secrets", secrets_module)
    return state


def test_azurekv_forms(registry: Registry, fake_azure: dict[str, Any]) -> None:
    resolver, redactor = _resolver(registry)
    assert _resolve(resolver, "azurekv:kv-dvf-test#sap-password") == "synthetic-azure-pw"
    assert _resolve(resolver, "azurekv:kv-dvf-test/sap-password") == "synthetic-azure-pw"
    assert _resolve(resolver, "azurekv:kv-dvf-test/sap-password/v1") == "synthetic-old"
    assert _resolve(resolver, "azurekv:kv-dvf-test/db-config#password") == "synthetic-json-pw"
    assert _resolve(resolver, "azurekv:https://kv-dvf-test.vault.azure.net#sap-password")
    assert fake_azure["clients"] == ["https://kv-dvf-test.vault.azure.net"]  # one client, cached
    assert fake_azure["credentials"] == [("DefaultAzureCredential", {})]
    # the two spellings of the latest version share one request
    assert (
        fake_azure["calls"].count(("https://kv-dvf-test.vault.azure.net", "sap-password", None))
        == 1
    )
    assert "synthetic-azure-pw" in redactor


@pytest.mark.parametrize(
    ("reference", "match"),
    [
        ("azurekv:kv-dvf-test#missing", "was not found"),
        ("azurekv:kv-dvf-test#forbidden", "Key Vault Secrets User"),
        ("azurekv:kv-dvf-test#noauth", "az login"),
        ("azurekv:kv-dvf-test", "names no secret"),
        ("azurekv:x#name", "not a valid Key Vault name"),
        ("azurekv:kv-dvf-test#bad_name", "not a valid Key Vault secret name"),
        ("azurekv:http://kv.vault.azure.net#a", "must use https"),
        ("azurekv:kv-dvf-test/a/b/c", "Invalid azurekv reference"),
    ],
)
def test_azurekv_errors(
    registry: Registry, fake_azure: dict[str, Any], reference: str, match: str
) -> None:
    resolver, _ = _resolver(registry)
    with pytest.raises(SecretError, match=match):
        _resolve(resolver, reference)


def test_azurekv_credential_settings(registry: Registry, fake_azure: dict[str, Any]) -> None:
    resolver, _ = _resolver(
        registry,
        {"DVF_AZUREKV_CREDENTIAL": "managed_identity", "DVF_AZUREKV_CLIENT_ID": "00000000-0000"},
        {"azurekv": {"vault_suffix": "vault.azure.cn"}},
    )
    with pytest.raises(SecretError, match="was not found"):
        _resolve(resolver, "azurekv:kv-dvf-test#sap-password")
    assert fake_azure["credentials"] == [
        ("ManagedIdentityCredential", {"client_id": "00000000-0000"})
    ]
    assert fake_azure["clients"] == ["https://kv-dvf-test.vault.azure.cn"]


def test_azurekv_missing_library(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "azure.keyvault.secrets", None)
    monkeypatch.setitem(sys.modules, "azure.identity", types.ModuleType("azure.identity"))
    resolver, _ = _resolver(registry)
    with pytest.raises(SecretError, match=r"azure-keyvault-secrets.*dvf-framework\[azure\]"):
        _resolve(resolver, "azurekv:kv-dvf-test#sap-password")


# ====================================================================== Google


class NotFound(Exception):  # noqa: N818 (Google's name)
    pass


class PermissionDenied(Exception):  # noqa: N818
    pass


@pytest.fixture
def fake_gcp(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"clients": [], "names": []}
    data = {
        "projects/dvf-test-project/secrets/pg-password/versions/latest": b"synthetic-gcp-pw",
        "projects/dvf-test-project/secrets/pg-password/versions/3": b"synthetic-v3",
        "projects/other-project/secrets/cfg/versions/latest": b'{"token": "synthetic-tok"}',
        "projects/dvf-test-project/locations/europe-west1/secrets/regional/versions/latest": (
            b"synthetic-regional"
        ),
        "projects/dvf-test-project/secrets/binary/versions/latest": b"\xff\xfe",
    }

    class Client:
        def __init__(self, client_options: dict[str, str] | None = None) -> None:
            state["clients"].append(client_options)

        def access_secret_version(self, request: dict[str, str]) -> Any:
            name = request["name"]
            state["names"].append(name)
            if "denied" in name:
                raise PermissionDenied("403 Permission denied on resource")
            if name not in data:
                raise NotFound(f"404 Secret [{name}] not found")
            payload = types.SimpleNamespace(data=data[name])
            return types.SimpleNamespace(payload=payload)

    module = types.ModuleType("google.cloud.secretmanager")
    module.SecretManagerServiceClient = Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", module)
    return state


def test_gcpsm_forms(registry: Registry, fake_gcp: dict[str, Any]) -> None:
    resolver, redactor = _resolver(registry, {"GOOGLE_CLOUD_PROJECT": "dvf-test-project"})
    assert _resolve(resolver, "gcpsm:pg-password") == "synthetic-gcp-pw"
    assert _resolve(resolver, "gcpsm:dvf-test-project/pg-password/3") == "synthetic-v3"
    assert _resolve(resolver, "gcpsm:other-project/cfg#token") == "synthetic-tok"
    full = "projects/dvf-test-project/secrets/pg-password/versions/latest"
    assert _resolve(resolver, f"gcpsm:{full}") == "synthetic-gcp-pw"
    assert fake_gcp["names"].count(full) == 1  # cached across spellings
    assert fake_gcp["clients"] == [None]
    regional = "projects/dvf-test-project/locations/europe-west1/secrets/regional"
    assert _resolve(resolver, f"gcpsm:{regional}") == "synthetic-regional"
    assert fake_gcp["clients"][-1] == {
        "api_endpoint": "secretmanager.europe-west1.rep.googleapis.com"
    }
    assert "synthetic-gcp-pw" in redactor


@pytest.mark.parametrize(
    ("reference", "match"),
    [
        ("gcpsm:dvf-test-project/missing", "was not found"),
        ("gcpsm:dvf-test-project/denied", "roles/secretmanager.secretAccessor"),
        ("gcpsm:dvf-test-project/binary", "not UTF-8"),
        ("gcpsm:a/b/c/d", "Invalid gcpsm reference"),
        ("gcpsm:BAD_PROJECT/x", "not a valid Google Cloud project"),
        ("gcpsm:dvf-test-project/x/bad version", "not a valid Secret Manager version"),
    ],
)
def test_gcpsm_errors(
    registry: Registry, fake_gcp: dict[str, Any], reference: str, match: str
) -> None:
    resolver, _ = _resolver(registry)
    with pytest.raises(SecretError, match=match):
        _resolve(resolver, reference)


def test_gcpsm_needs_a_project(registry: Registry, fake_gcp: dict[str, Any]) -> None:
    resolver, _ = _resolver(registry)
    with pytest.raises(SecretError, match="No Google Cloud project"):
        _resolve(resolver, "gcpsm:pg-password")
    located, _ = _resolver(
        registry, options={"gcpsm": {"project": "dvf-test-project", "location": "europe-west1"}}
    )
    assert _resolve(located, "gcpsm:regional") == "synthetic-regional"


def test_gcpsm_missing_library(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", None)
    resolver, _ = _resolver(registry)
    with pytest.raises(SecretError, match=r"google-cloud-secret-manager.*dvf-framework\[gcp\]"):
        _resolve(resolver, "gcpsm:p-project/s")


def test_provider_classes_are_wired(registry: Registry) -> None:
    from dvf.plugins.registry import PluginKind

    assert registry.get(PluginKind.SECRET_PROVIDER, "awssm").implementation is (
        AWSSecretsManagerProvider
    )
    assert registry.get(PluginKind.SECRET_PROVIDER, "azurekv").implementation is (
        AzureKeyVaultProvider
    )
    assert registry.get(PluginKind.SECRET_PROVIDER, "gcpsm").implementation is (
        GoogleSecretManagerProvider
    )
