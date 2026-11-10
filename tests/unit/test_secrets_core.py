"""Secret resolver, references, JSON keys, settings, registration, and redaction (synthetic data)."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from dvf.core.errors import SecretError
from dvf.packs.secrets._base import PackSecretProvider, ProviderSettings
from dvf.packs.secrets._http import check_endpoint, is_loopback
from dvf.packs.secrets.config import SecretsConfig
from dvf.plugins.registry import PluginKind, Registry
from dvf.security.redaction import MASK, Redactor, redact_patterns
from dvf.security.secrets import (
    SecretContext,
    SecretProvider,
    SecretRef,
    SecretResolver,
    safe_detail,
    select_key,
)

# ---------------------------------------------------------------------- registration


def test_pack_registers_every_declared_provider(registry: Registry) -> None:
    statuses = {
        name: registry.get(PluginKind.SECRET_PROVIDER, name).status
        for name in ("keyring", "azurekv", "awssm", "gcpsm", "vault", "databricks")
    }
    assert statuses == {
        "keyring": "beta",
        "azurekv": "beta",
        "awssm": "beta",
        "gcpsm": "beta",
        "vault": "ready",  # verified against a live Vault dev server
        "databricks": "beta",
    }


# ---------------------------------------------------------------------- select_key


def test_select_key_reads_json_fields_and_never_shows_values() -> None:
    doc = '{"username": "synthetic-user", "password": "synthetic-pw", "port": 5432, "n": null}'
    ref = SecretRef.parse("awssm:app/db#password")
    assert select_key(doc, ref, "secret 'app/db'") == "synthetic-pw"
    assert select_key(doc, SecretRef("awssm", "app/db", "port"), "s") == "5432"
    assert select_key(doc, SecretRef("awssm", "app/db"), "s") == doc
    assert select_key({"a": 1}, SecretRef("x", "p"), "s") == '{"a":1}'
    with pytest.raises(
        SecretError, match=r"has no key 'nope' \(keys: n, password, port, username\)"
    ):
        select_key(doc, SecretRef("awssm", "app/db", "nope"), "secret 'app/db'")
    with pytest.raises(SecretError, match="is null"):
        select_key(doc, SecretRef("awssm", "app/db", "n"), "s")
    with pytest.raises(SecretError, match="not a JSON object") as info:
        select_key("plain-synthetic-value", SecretRef("awssm", "x", "k"), "secret 'x'")
    assert "plain-synthetic-value" not in str(info.value)


# ---------------------------------------------------------------------- resolver


class _Counting(SecretProvider):
    calls: ClassVar[list[str]] = []

    def resolve(self, ref: SecretRef) -> str:
        self.calls.append(str(ref))
        if ref.path == "boom":
            raise RuntimeError("driver exploded near synthetic-leak-value")
        if ref.path == "notstr":
            return 42  # type: ignore[return-value]
        return f"value-of-{ref.path}"


def test_resolver_caches_registers_and_wraps_errors(registry: Registry) -> None:
    registry.register(PluginKind.SECRET_PROVIDER, "counting", _Counting)
    redactor = Redactor()
    redactor.add("synthetic-leak-value")
    resolver = SecretResolver(registry, SecretContext(environ={}), redactor)
    _Counting.calls.clear()
    ref = SecretRef.parse("counting:alpha")
    assert resolver.resolve(ref) == "value-of-alpha"
    assert resolver.resolve(SecretRef.parse("counting:alpha")) == "value-of-alpha"
    assert _Counting.calls == ["counting:alpha"]
    assert "value-of-alpha" in redactor
    with pytest.raises(SecretError, match=r"failed: RuntimeError: driver exploded near \*\*\*"):
        resolver.resolve(SecretRef.parse("counting:boom"))
    with pytest.raises(SecretError, match="returned a int"):
        resolver.resolve(SecretRef.parse("counting:notstr"))


def test_resolver_passes_its_redactor_to_providers(registry: Registry) -> None:
    seen: list[Any] = []

    class Spy(SecretProvider):
        def resolve(self, ref: SecretRef) -> str:
            seen.append(self.context.redactor)
            self.context.protect("synthetic-aux-token")
            return "synthetic-value"

    registry.register(PluginKind.SECRET_PROVIDER, "spy", Spy)
    redactor = Redactor()
    SecretResolver(registry, SecretContext(environ={}), redactor).resolve(SecretRef("spy", "x"))
    assert seen == [redactor]
    assert "synthetic-aux-token" in redactor


def test_context_env_prefers_environment_then_dotenv() -> None:
    context = SecretContext(environ={"A": "", "B": "env-b"}, dotenv={"A": "dot-a", "C": "dot-c"})
    assert context.env("A") == "dot-a"
    assert context.env("MISSING", "B") == "env-b"
    assert context.env("C") == "dot-c"
    assert context.env("NONE") is None


# ---------------------------------------------------------------------- settings


class _Settings(ProviderSettings):
    region: str | None = None
    timeout_s: float = 30.0


class _Pack(PackSecretProvider):
    name = "packtest"
    label = "Pack test"
    Settings: ClassVar[type[ProviderSettings]] = _Settings
    ENV: ClassVar[dict[str, tuple[str, ...]]] = {
        "region": ("DVF_T_REGION", "T_REGION"),
        "timeout_s": ("DVF_T_TIMEOUT",),
    }

    def resolve(self, ref: SecretRef) -> str:
        return self.cached(ref.path, lambda: f"{self.settings.region}:{ref.path}")


def test_settings_from_environment_and_options() -> None:
    provider = _Pack(SecretContext(environ={"T_REGION": "eu-west-1", "DVF_T_TIMEOUT": "5"}))
    assert provider.settings.region == "eu-west-1"
    assert provider.settings.timeout_s == 5.0
    overridden = _Pack(
        SecretContext(environ={"T_REGION": "eu-west-1"}, options={"packtest": {"region": "x"}})
    )
    assert overridden.resolve(SecretRef("packtest", "p")) == "x:p"


def test_invalid_settings_never_echo_values() -> None:
    with pytest.raises(SecretError, match="timeout_s") as info:
        _Pack(SecretContext(environ={"DVF_T_TIMEOUT": "synthetic-not-a-number"}))
    assert "synthetic-not-a-number" not in str(info.value)
    with pytest.raises(SecretError, match="Extra inputs") as info:
        _Pack(SecretContext(environ={}, options={"packtest": {"tokn": "synthetic-typo"}}))
    assert "synthetic-typo" not in str(info.value)


def test_missing_client_library_says_how_to_install() -> None:
    provider = _Pack(SecretContext(environ={}))
    provider.extra = "aws"
    with pytest.raises(SecretError, match=r"pip install 'dvf-framework\[aws\]'"):
        provider.import_module("dvf_no_such_module_xyz", "no-such-package")


def test_secrets_config_model_accepts_provider_sections() -> None:
    config = SecretsConfig.model_validate(
        {
            "vault": {"addr": "https://vault.example.org:8200", "kv_version": "auto"},
            "awssm": {"region": "eu-west-1"},
            "acme_vault": {"anything": 1},
        }
    )
    assert config.vault is not None and config.vault.kv_version is None
    assert config.awssm is not None and config.awssm.region == "eu-west-1"


# ---------------------------------------------------------------------- endpoints


def test_check_endpoint_rules() -> None:
    assert check_endpoint("https://vault.example.org:8200/", what="V", allow_http=False) == (
        "https://vault.example.org:8200"
    )
    assert check_endpoint("http://127.0.0.1:8200", what="V", allow_http=False)
    assert check_endpoint("http://10.0.0.5:8200", what="V", allow_http=True)
    with pytest.raises(SecretError, match="plain HTTP"):
        check_endpoint("http://vault.internal:8200", what="V", allow_http=False)
    with pytest.raises(SecretError, match="must not contain credentials"):
        check_endpoint("https://user:synthetic@vault.example.org", what="V", allow_http=False)
    with pytest.raises(SecretError, match="not an http"):
        check_endpoint("file:///etc/passwd", what="V", allow_http=False)
    with pytest.raises(SecretError, match="query string"):
        check_endpoint("https://vault.example.org/?x=1", what="V", allow_http=False)
    assert is_loopback("localhost") and is_loopback("::1") and not is_loopback("example.org")


# ---------------------------------------------------------------------- redaction


def test_redactor_masks_encoded_forms_of_registered_values() -> None:
    redactor = Redactor()
    redactor.add('p@ss/w"rd 9')
    assert redactor.redact_text("postgresql://u:p%40ss%2Fw%22rd%209@h/db") == (
        f"postgresql://u:{MASK}@h/db"
    )
    assert redactor.redact_text('{"pw": "p@ss/w\\"rd 9"}') == f'{{"pw": "{MASK}"}}'
    assert redactor.redact_text("x=p%40ss%2Fw%22rd+9") == f"x={MASK}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("postgresql://u:pa@ss@db.example.org/x", f"postgresql://u:{MASK}@db.example.org/x"),
        ("redis://:synthetic@cache:6379/0", f"redis://:{MASK}@cache:6379/0"),
        ("https://host/path?sig=abc123&se=2026", f"https://host/path?sig={MASK}&se=2026"),
        ("https://h/cb?access_token=t0k3n#frag", f"https://h/cb?access_token={MASK}#frag"),
        ("Server=db;UID=sa;PWD={p;w}}};Encrypt=yes", f"Server=db;UID=sa;PWD={MASK};Encrypt=yes"),
        ("user=dvf password=synthetic host=db", f"user=dvf password={MASK} host=db"),
        ("Authorization: Bearer abc.def.ghi", f"Authorization: Bearer {MASK}"),
        ("X-Vault-Token: hvs.synthetic", f"X-Vault-Token: {MASK}"),
        (
            "post to https://hooks.slack.com/services/T000/B000/XXXX now",
            f"post to https://hooks.slack.com/services/{MASK} now",
        ),
        (
            "https://acme.webhook.office.com/webhookb2/abc@def/IncomingWebhook/x/y",
            f"https://acme.webhook.office.com/{MASK}",
        ),
        (
            "postgresql://u@host:5432/db?sslmode=require",
            "postgresql://u@host:5432/db?sslmode=require",
        ),
        ("no secrets here: a = b", "no secrets here: a = b"),
    ],
)
def test_redact_patterns(text: str, expected: str) -> None:
    assert redact_patterns(text) == expected


def test_safe_detail_is_short_single_line_and_redacted() -> None:
    redactor = Redactor()
    redactor.add("synthetic-secret")
    detail = safe_detail("line one\nsynthetic-secret " + "x" * 400, redactor)
    assert "\n" not in detail and "synthetic-secret" not in detail and len(detail) <= 301
