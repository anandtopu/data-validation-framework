"""Azure Key Vault: ``${secret:azurekv:<vault>#<secret-name>}``.

Forms:

* ``azurekv:<vault>#<secret-name>``: the secret's value
* ``azurekv:<vault>/<secret-name>``: the same
* ``azurekv:<vault>/<secret-name>#<json-key>``: one field of a JSON secret
* ``azurekv:<vault>/<secret-name>/<version>[#<json-key>]``: a specific version

``<vault>`` is a vault name (``https://<vault>.vault.azure.net``) or a full ``https://`` vault URL.
Authentication uses azure-identity: ``DefaultAzureCredential`` unless the ``credential`` setting
names one credential type.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from dvf.core.errors import SecretError
from dvf.packs.secrets._base import PackSecretProvider, ProviderSettings, exception_names
from dvf.security.secrets import SecretRef, select_key

_VAULT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]$")
_SECRET_NAME_RE = re.compile(r"^[0-9A-Za-z-]{1,127}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z]{1,64}$")

CredentialKind = Literal[
    "default",
    "environment",
    "managed_identity",
    "workload_identity",
    "cli",
    "developer_cli",
    "powershell",
]


class AzureKeyVaultSettings(ProviderSettings):
    """Settings of the ``azurekv`` secret provider."""

    credential: CredentialKind = "default"
    """``default`` (DefaultAzureCredential), or one credential type: ``environment``,
    ``managed_identity``, ``workload_identity``, ``cli``, ``developer_cli``, ``powershell``
    (``DVF_AZUREKV_CREDENTIAL``)."""
    client_id: str | None = None
    """Client ID of a user-assigned managed identity (``DVF_AZUREKV_CLIENT_ID``)."""
    vault_suffix: str = "vault.azure.net"
    """DNS suffix for vault names: ``vault.azure.cn``, ``vault.usgovcloudapi.net`` for sovereign
    clouds (``DVF_AZUREKV_VAULT_SUFFIX``)."""


class AzureKeyVaultProvider(PackSecretProvider):
    """Azure Key Vault, through azure-keyvault-secrets and azure-identity."""

    name = "azurekv"
    label = "Azure Key Vault"
    extra = "azure"
    Settings: ClassVar[type[ProviderSettings]] = AzureKeyVaultSettings
    ENV: ClassVar[dict[str, tuple[str, ...]]] = {
        "credential": ("DVF_AZUREKV_CREDENTIAL",),
        "client_id": ("DVF_AZUREKV_CLIENT_ID",),
        "vault_suffix": ("DVF_AZUREKV_VAULT_SUFFIX",),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._credential: Any = None
        self._clients: dict[str, Any] = {}

    @property
    def opts(self) -> AzureKeyVaultSettings:
        settings: AzureKeyVaultSettings = self.settings
        return settings

    def resolve(self, ref: SecretRef) -> str:
        vault_url, name, version, json_key = self._parse(ref)
        what = f"Azure Key Vault secret '{name}' in {vault_url}"
        value = self.cached(
            f"{vault_url}|{name}|{version or ''}", lambda: self._fetch(vault_url, name, version)
        )
        return select_key(value, SecretRef(ref.provider, ref.path, json_key), what)

    # ------------------------------------------------------------------ parsing
    def _parse(self, ref: SecretRef) -> tuple[str, str, str | None, str | None]:
        path = ref.path
        if path.lower().startswith("https://"):
            parsed = urlsplit(path)
            if not parsed.hostname:
                raise SecretError(f"'{path}' is not a valid Key Vault URL.")
            vault_url = f"https://{parsed.netloc}"
            parts = [p for p in parsed.path.split("/") if p]
            if parts[:1] == ["secrets"]:
                parts = parts[1:]
        elif path.lower().startswith("http://"):
            raise SecretError("Key Vault URLs must use https://.")
        else:
            segments = [p for p in path.split("/") if p]
            vault, parts = (segments[0], segments[1:]) if segments else ("", [])
            if not _VAULT_NAME_RE.match(vault):
                raise SecretError(
                    f"'{vault}' is not a valid Key Vault name (3-24 letters, digits, and hyphens)."
                )
            vault_url = f"https://{vault}.{self.opts.vault_suffix.strip('.')}"
        if len(parts) > 2:
            raise SecretError(
                f"Invalid azurekv reference '{ref}': use <vault>#<secret>, <vault>/<secret>, or "
                "<vault>/<secret>/<version>, optionally with #<json-key>."
            )
        json_key = ref.key
        if not parts:
            if not ref.key:
                raise SecretError(
                    f"The azurekv reference '{ref}' names no secret; use <vault>#<secret-name>."
                )
            parts, json_key = [ref.key], None
        name = parts[0]
        version = parts[1] if len(parts) > 1 else None
        if not _SECRET_NAME_RE.match(name):
            raise SecretError(
                f"'{name}' is not a valid Key Vault secret name (letters, digits, and hyphens)."
            )
        if version is not None and not _VERSION_RE.match(version):
            raise SecretError(f"'{version}' is not a valid Key Vault secret version.")
        return vault_url, name, version, json_key

    # ------------------------------------------------------------------ client
    def _get_credential(self) -> Any:
        if self._credential is not None:
            return self._credential
        identity = self.import_module("azure.identity", "azure-identity")
        kind = self.opts.credential
        client_id = self.opts.client_id
        try:
            if kind == "default":
                kwargs = {"managed_identity_client_id": client_id} if client_id else {}
                credential = identity.DefaultAzureCredential(**kwargs)
            elif kind == "environment":
                credential = identity.EnvironmentCredential()
            elif kind == "managed_identity":
                credential = identity.ManagedIdentityCredential(
                    **({"client_id": client_id} if client_id else {})
                )
            elif kind == "workload_identity":
                credential = identity.WorkloadIdentityCredential()
            elif kind == "cli":
                credential = identity.AzureCliCredential()
            elif kind == "developer_cli":
                credential = identity.AzureDeveloperCliCredential()
            else:
                credential = identity.AzurePowerShellCredential()
        except Exception as exc:
            raise SecretError(
                f"Cannot create the Azure '{kind}' credential: {type(exc).__name__}: "
                f"{self.detail(exc)}"
            ) from None
        self._credential = credential
        return credential

    def _client(self, vault_url: str) -> Any:
        with self._lock:
            client = self._clients.get(vault_url)
            if client is None:
                secrets = self.import_module("azure.keyvault.secrets", "azure-keyvault-secrets")
                client = secrets.SecretClient(
                    vault_url=vault_url, credential=self._get_credential()
                )
                self._clients[vault_url] = client
            return client

    def _fetch(self, vault_url: str, name: str, version: str | None) -> str:
        client = self._client(vault_url)
        try:
            secret = client.get_secret(name, version) if version else client.get_secret(name)
        except Exception as exc:
            raise self._error(exc, vault_url, name) from None
        value = getattr(secret, "value", None)
        if value is None:
            raise SecretError(f"Azure Key Vault secret '{name}' in {vault_url} has no value.")
        self.protect(value)
        return str(value)

    def _error(self, exc: Exception, vault_url: str, name: str) -> SecretError:
        names = exception_names(exc)
        status = getattr(exc, "status_code", None)
        where = f"secret '{name}' in {vault_url}"
        if "ResourceNotFoundError" in names or status == 404:
            return SecretError(f"Azure Key Vault {where} was not found (or is disabled).")
        if "CredentialUnavailableError" in names or "ClientAuthenticationError" in names:
            return SecretError(
                f"Azure authentication failed for {where} with the '{self.opts.credential}' "
                f"credential: {self.detail(exc)}. Sign in with 'az login', or configure a "
                "managed identity or the AZURE_CLIENT_ID, AZURE_TENANT_ID, and "
                "AZURE_CLIENT_SECRET environment variables."
            )
        if status == 403:
            return SecretError(
                f"Access to Azure Key Vault {where} was denied. Grant the identity the 'Key Vault "
                "Secrets User' role (or a 'get' secret access policy)."
            )
        if "ServiceRequestError" in names:
            return SecretError(f"Cannot reach Azure Key Vault {vault_url}: {self.detail(exc)}")
        return SecretError(
            f"Azure Key Vault request for {where} failed: {type(exc).__name__}: {self.detail(exc)}"
        )
