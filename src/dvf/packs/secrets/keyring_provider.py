"""Operating system keyring: ``${secret:keyring:<service>#<username>}``.

Uses the ``keyring`` library, so it reads Windows Credential Manager, the macOS Keychain, or the
Secret Service (GNOME Keyring, KWallet) on Linux. Store a value with
``keyring set <service> <username>``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from dvf.core.errors import SecretError
from dvf.packs.secrets._base import PackSecretProvider, ProviderSettings
from dvf.security.secrets import SecretRef


class KeyringSettings(ProviderSettings):
    """Settings of the ``keyring`` secret provider."""

    username: str | None = None
    """Username used when a reference has no ``#<username>`` part (``DVF_KEYRING_USERNAME``)."""


class KeyringSecretProvider(PackSecretProvider):
    """Operating system keyring (Windows Credential Manager, macOS Keychain, Secret Service)."""

    name = "keyring"
    label = "operating system keyring"
    extra = "keyring"
    Settings: ClassVar[type[ProviderSettings]] = KeyringSettings
    ENV: ClassVar[dict[str, tuple[str, ...]]] = {"username": ("DVF_KEYRING_USERNAME",)}

    def resolve(self, ref: SecretRef) -> str:
        keyring = self.import_module("keyring", "keyring")
        settings: KeyringSettings = self.settings
        service = ref.path
        username = ref.key or settings.username
        try:
            if username:
                value = keyring.get_password(service, username)
            else:
                credential: Any = keyring.get_credential(service, None)
                value = None if credential is None else credential.password
        except Exception as exc:
            backend = _backend_name(keyring)
            raise SecretError(
                f"The keyring backend ({backend}) could not read service '{service}': "
                f"{type(exc).__name__}: {self.detail(exc)}"
            ) from None
        if value is None:
            who = f"user '{username}'" if username else "any user"
            raise SecretError(
                f"The keyring has no password for {who} in service '{service}'. Store one with: "
                f"keyring set {service} {username or '<username>'}"
            )
        return str(value)


def _backend_name(keyring: Any) -> str:
    try:
        backend = keyring.get_keyring()
    except Exception:
        return "unknown"
    return f"{type(backend).__module__}.{type(backend).__name__}"
