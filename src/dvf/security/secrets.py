"""Secret references and providers.

Configuration refers to secrets as ``${secret:<provider>:<path>[#<key>]}``, for example
``${secret:env:PG_PASSWORD}`` or ``${secret:azurekv:migration-kv#sap-password}``. Providers are
plugins of kind ``secret_provider``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from dotenv import dotenv_values

from dvf.core.errors import SecretError
from dvf.security.redaction import Redactor, global_redactor

if TYPE_CHECKING:
    from dvf.plugins.registry import Registry

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]*$")


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
    """What providers may read: the process environment, the project's .env values, its folder."""

    environ: Mapping[str, str]
    dotenv: Mapping[str, str] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)


class SecretProvider(ABC):
    """Base class for secret providers. Register with ``@register_secret_provider("name")``."""

    plugin_name: ClassVar[str] = ""
    dvf_plugin_api: ClassVar[str | None] = None

    def __init__(self, context: SecretContext) -> None:
        self.context = context

    @abstractmethod
    def resolve(self, ref: SecretRef) -> str:
        """Return the secret value or raise :class:`SecretError`."""


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
    """Resolves references through registered providers and records values for redaction."""

    def __init__(
        self, registry: Registry, context: SecretContext, redactor: Redactor | None = None
    ) -> None:
        self._registry = registry
        self._context = context
        self._redactor = redactor or global_redactor()
        self._providers: dict[str, SecretProvider] = {}

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

        self.check_provider(ref.provider)
        entry = self._registry.get(PluginKind.SECRET_PROVIDER, ref.provider)
        if not entry.ready:
            hint = f" It is planned for the '{entry.extra}' extra." if entry.extra else ""
            raise SecretError(
                f"Secret provider '{ref.provider}' is declared but not implemented in this "
                f"version of dvf.{hint}"
            )
        provider = self._providers.get(ref.provider)
        if provider is None:
            provider = entry.implementation(self._context)
            self._providers[ref.provider] = provider
        value: str = provider.resolve(ref)
        self._redactor.add(value)
        return value
