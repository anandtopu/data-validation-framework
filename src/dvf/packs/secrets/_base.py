"""Shared machinery of the built-in secret providers: settings, caching, lazy imports."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, TypeVar

from pydantic import ValidationError

from dvf.config.base import StrictModel
from dvf.core.errors import SecretError
from dvf.security.secrets import SecretContext, SecretProvider, safe_detail

T = TypeVar("T")


class ProviderSettings(StrictModel):
    """Base of provider settings (the ``secrets.<provider>`` section and environment variables)."""


class PackSecretProvider(SecretProvider):
    """A built-in provider with typed settings, a per-run document cache, and lazy imports.

    Settings come from environment variables named in :attr:`ENV` (first set wins), overridden by
    ``SecretContext.options[<provider>]``. Fetched documents are cached for the provider's
    lifetime (one configuration load), so several keys of one secret cost one request.
    """

    name: ClassVar[str] = ""
    """The provider name used in references and messages."""
    label: ClassVar[str] = ""
    """Human-readable service name, for example ``"AWS Secrets Manager"``."""
    extra: ClassVar[str | None] = None
    """The pip extra that installs the client library, if any."""
    Settings: ClassVar[type[ProviderSettings]] = ProviderSettings
    ENV: ClassVar[Mapping[str, tuple[str, ...]]] = {}
    """Settings field -> environment variables that set it, in order of precedence."""

    def __init__(self, context: SecretContext) -> None:
        super().__init__(context)
        self._lock = threading.RLock()
        self._documents: dict[str, Any] = {}
        self.settings = self._load_settings()

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> Any:
        raw: dict[str, Any] = {}
        for field, names in self.ENV.items():
            value = self.context.env(*names)
            if value is not None:
                raw[field] = value
        configured = self.context.options.get(self.name) or {}
        if not isinstance(configured, Mapping):
            raise SecretError(f"The settings of the {self.name} secret provider must be a mapping.")
        raw.update({k: v for k, v in configured.items() if v is not None})
        try:
            return self.Settings.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc']) or 'settings'}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
            raise SecretError(
                f"Invalid settings for the {self.name} secret provider: {problems}."
            ) from None

    # ------------------------------------------------------------------ helpers
    def cached(self, key: str, load: Callable[[], T]) -> T:
        """Return the document cached under ``key``, loading it once."""
        with self._lock:
            if key in self._documents:
                value: T = self._documents[key]
                return value
            value = load()
            self._documents[key] = value
            return value

    def import_module(self, module: str, package: str) -> Any:
        """Import an optional client library, or raise a :class:`SecretError` saying how to get it."""
        try:
            return importlib.import_module(module)
        except ImportError:
            install = (
                f"pip install 'dvf-framework[{self.extra}]'"
                if self.extra
                else f"pip install {package}"
            )
            raise SecretError(
                f"The {self.name} secret provider ({self.label}) needs the '{package}' package, "
                f"which is not installed. Install it with: {install}"
            ) from None

    def detail(self, exc: BaseException | str) -> str:
        """A redacted, shortened description of an underlying error."""
        return safe_detail(exc, self.context.redactor)

    def protect(self, value: str | None) -> None:
        """Register a credential (token, client secret) for redaction."""
        self.context.protect(value)


def exception_names(exc: BaseException) -> set[str]:
    """Class names in the exception's MRO, to recognize library errors without importing them."""
    return {cls.__name__ for cls in type(exc).__mro__}
