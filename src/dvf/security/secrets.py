"""Secret references and providers.

Configuration refers to secrets as ``${secret:<provider>:<path>[#<key>]}``, for example
``${secret:env:PG_PASSWORD}`` or ``${secret:azurekv:migration-kv#sap-password}``. Providers are
plugins of kind ``secret_provider``. The built-in cloud and OS providers live in
:mod:`dvf.packs.secrets`.

Every resolved value is registered with the redactor, and each reference is resolved at most once
per :class:`SecretResolver` (one configuration load, so one run).
"""

from __future__ import annotations

import json
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from dotenv import dotenv_values

from dvf.core.errors import SecretError
from dvf.security.redaction import Redactor, global_redactor

if TYPE_CHECKING:
    from dvf.plugins.registry import Registry

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_ERROR_DETAIL = 300


@dataclass(frozen=True)
class SecretRef:
    """A parsed secret reference: ``provider:path#key``."""

    provider: str
    path: str
    key: str | None = None

    @classmethod
    def parse(cls, text: str) -> SecretRef:
        provider, sep, rest = text.partition(":")
        provider = provider.strip()
        if not sep or not _PROVIDER_RE.match(provider):
            raise SecretError(
                f"Invalid secret reference '{text}': expected '<provider>:<path>[#<key>]', "
                "for example 'env:PG_PASSWORD'."
            )
        path, _, key = rest.partition("#")
        path, key = path.strip(), key.strip()
        if not path:
            raise SecretError(f"Invalid secret reference '{text}': the path is empty.")
        return cls(provider=provider, path=path, key=key or None)

    def __str__(self) -> str:
        return f"{self.provider}:{self.path}" + (f"#{self.key}" if self.key else "")


@dataclass(frozen=True)
class SecretContext:
    """What providers may read: the process environment, the project's .env values, its folder.

    ``options`` holds provider settings by provider name (for example ``{"vault": {"addr":
    "https://vault.example.org:8200"}}``); providers fall back to environment variables.
    ``redactor`` receives every value that must never be shown, including credentials a provider
    uses to authenticate (tokens, client secrets).
    """

    environ: Mapping[str, str]
    dotenv: Mapping[str, str] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)
    options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    redactor: Redactor | None = None

    def env(self, *names: str) -> str | None:
        """The first of ``names`` set in the environment, then in the .env file, else ``None``."""
        for name in names:
            for source in (self.environ, self.dotenv):
                value = source.get(name)
                if value:
                    return value
        return None

    def protect(self, value: str | None) -> None:
        """Register ``value`` for redaction (it is masked wherever dvf writes text)."""
        (self.redactor or global_redactor()).add(value)


class SecretProvider(ABC):
    """Base class for secret providers. Register with ``@register_secret_provider("name")``."""

    plugin_name: ClassVar[str] = ""
    dvf_plugin_api: ClassVar[str | None] = None

    def __init__(self, context: SecretContext) -> None:
        self.context = context

    @abstractmethod
    def resolve(self, ref: SecretRef) -> str:
        """Return the secret value or raise :class:`SecretError`.

        Error messages must never contain the value or any credential.
        """


def select_key(document: str | Mapping[str, Any], ref: SecretRef, what: str) -> str:
    """Return field ``ref.key`` of a JSON object secret, or the whole value when there is no key.

    ``what`` names the secret in error messages (for example ``"AWS secret 'prod/db'"``). Errors
    name the available keys, never values.
    """
    if ref.key is None:
        if isinstance(document, Mapping):
            return json.dumps(document, separators=(",", ":"), sort_keys=True)
        return document
    data: Any = document
    if isinstance(document, str):
        try:
            data = json.loads(document)
        except ValueError:
            data = None
    if not isinstance(data, Mapping):
        raise SecretError(
            f"{what} is not a JSON object, so the key '#{ref.key}' cannot be selected. "
            f"Remove '#{ref.key}' to use the whole value."
        )
    if ref.key not in data:
        known = ", ".join(sorted(str(k) for k in data)) or "none"
        raise SecretError(f"{what} has no key '{ref.key}' (keys: {known}).")
    value = data[ref.key]
    if value is None:
        raise SecretError(f"Key '{ref.key}' of {what} is null.")
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def safe_detail(exc: BaseException | str, redactor: Redactor | None = None) -> str:
    """A short, redacted, single-line description of an underlying error for use in messages."""
    text = exc if isinstance(exc, str) else (str(exc) or type(exc).__name__)
    text = " ".join(text.split())
    text = (redactor or global_redactor()).redact_text(text)
    return text if len(text) <= _MAX_ERROR_DETAIL else text[:_MAX_ERROR_DETAIL] + "…"


class EnvSecretProvider(SecretProvider):
    """Environment variables, falling back to the project's .env file: ``${secret:env:NAME}``."""

    def resolve(self, ref: SecretRef) -> str:
        if ref.key:
            raise SecretError(f"The env provider takes a variable name only, got '{ref}'.")
        value = self.context.environ.get(ref.path)
        if value is None:
            value = self.context.dotenv.get(ref.path)
        if value is None:
            raise SecretError(f"Environment variable '{ref.path}' is not set.")
        return value


class DotEnvSecretProvider(SecretProvider):
    """A .env file: ``${secret:dotenv:NAME}`` (project .env) or ``${secret:dotenv:path/.env#NAME}``."""

    def resolve(self, ref: SecretRef) -> str:
        if ref.key:
            file = Path(ref.path)
            if not file.is_absolute():
                file = self.context.base_dir / file
            if not file.is_file():
                raise SecretError(f"The .env file '{file}' does not exist.")
            values = {k: v for k, v in dotenv_values(file).items() if v is not None}
            name = ref.key
        else:
            values = dict(self.context.dotenv)
            name = ref.path
        if name not in values:
            raise SecretError(f"'{name}' is not defined in the .env file.")
        return values[name]


class SecretResolver:
    """Resolves references through registered providers and records values for redaction.

    Each reference is fetched once per resolver; later uses get the cached value. Unexpected
    provider exceptions become :class:`SecretError` with a redacted message.
    """

    def __init__(
        self, registry: Registry, context: SecretContext, redactor: Redactor | None = None
    ) -> None:
        self._registry = registry
        self._redactor = redactor or context.redactor or global_redactor()
        self._context = replace(context, redactor=self._redactor)
        self._providers: dict[str, SecretProvider] = {}
        self._values: dict[str, str] = {}
        self._lock = threading.RLock()

    def check_provider(self, provider: str) -> None:
        """Raise :class:`SecretError` if ``provider`` is not registered."""
        from dvf.plugins.registry import PluginKind

        if self._registry.find(PluginKind.SECRET_PROVIDER, provider) is None:
            suggestions = self._registry.suggest(PluginKind.SECRET_PROVIDER, provider)
            hint = f" Did you mean '{suggestions[0]}'?" if suggestions else ""
            known = ", ".join(self._registry.names(PluginKind.SECRET_PROVIDER))
            raise SecretError(f"Unknown secret provider '{provider}'.{hint} Known: {known}.")

    def resolve(self, ref: SecretRef) -> str:
        from dvf.plugins.registry import PluginKind

        cache_key = str(ref)
        with self._lock:
            cached = self._values.get(cache_key)
            if cached is not None:
                return cached
            self.check_provider(ref.provider)
            entry = self._registry.get(PluginKind.SECRET_PROVIDER, ref.provider)
            if not entry.ready:
                hint = f" It is planned for the '{entry.extra}' extra." if entry.extra else ""
                raise SecretError(
                    f"Secret provider '{ref.provider}' is declared but not implemented in this "
                    f"version of dvf.{hint}"
                )
            try:
                provider = self._providers.get(ref.provider)
                if provider is None:
                    provider = entry.implementation(self._context)
                    self._providers[ref.provider] = provider
                value = provider.resolve(ref)
            except SecretError:
                raise
            except Exception as exc:
                # A provider bug or an unexpected library error: keep the message, never the value.
                raise SecretError(
                    f"Secret provider '{ref.provider}' failed: {type(exc).__name__}: "
                    f"{safe_detail(exc, self._redactor)}"
                ) from None
            if not isinstance(value, str):
                raise SecretError(
                    f"Secret provider '{ref.provider}' returned a {type(value).__name__}, "
                    "not a string."
                )
            self._redactor.add(value)
            self._values[cache_key] = value
            return value
