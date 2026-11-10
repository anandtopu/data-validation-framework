"""Typed settings of the built-in secret providers, by provider name.

Each provider reads its settings from environment variables and, when given, from
``SecretContext.options[<provider>]``. :class:`SecretsConfig` describes all of them for a
``secrets:`` configuration section and the JSON Schema; third-party providers may add sections of
their own (validated by the provider when it is first used).
"""

from __future__ import annotations

from pydantic import ConfigDict

from dvf.config.base import StrictModel
from dvf.packs.secrets.aws_provider import AWSSecretsManagerSettings
from dvf.packs.secrets.azure_provider import AzureKeyVaultSettings
from dvf.packs.secrets.databricks_provider import DatabricksSecretsSettings
from dvf.packs.secrets.gcp_provider import GoogleSecretManagerSettings
from dvf.packs.secrets.keyring_provider import KeyringSettings
from dvf.packs.secrets.vault_provider import VaultSettings


class SecretsConfig(StrictModel):
    """Settings of secret providers. Environment variables apply when a setting is not given here.

    Values here may use ``${env:...}`` but not ``${secret:...}``.
    """

    model_config = ConfigDict(extra="allow")

    keyring: KeyringSettings | None = None
    azurekv: AzureKeyVaultSettings | None = None
    awssm: AWSSecretsManagerSettings | None = None
    gcpsm: GoogleSecretManagerSettings | None = None
    vault: VaultSettings | None = None
    databricks: DatabricksSecretsSettings | None = None


__all__ = [
    "AWSSecretsManagerSettings",
    "AzureKeyVaultSettings",
    "DatabricksSecretsSettings",
    "GoogleSecretManagerSettings",
    "KeyringSettings",
    "SecretsConfig",
    "VaultSettings",
]
