"""Secrets pack: OS keyring and cloud secret providers.

Attaches implementations to the secret providers declared in :mod:`dvf.plugins.builtins`:
``keyring``, ``azurekv``, ``awssm``, ``gcpsm``, ``vault``, and ``databricks``. Client libraries
(keyring, boto3, azure-identity and azure-keyvault-secrets, google-cloud-secret-manager) are
imported on first use; Vault and Databricks use the standard library's HTTPS client.
``register`` is called by :func:`dvf.packs.register_packs`.
"""

from __future__ import annotations

from dvf.packs import attach
from dvf.plugins.registry import PluginKind, Registry

VERIFIED_LIVE = frozenset({"vault"})
"""Providers verified against a live service (others register as ``beta``)."""


def register(registry: Registry) -> None:
    """Attach this pack's implementations and register its new components."""
    from dvf.packs.secrets.aws_provider import AWSSecretsManagerProvider
    from dvf.packs.secrets.azure_provider import AzureKeyVaultProvider
    from dvf.packs.secrets.databricks_provider import DatabricksSecretProvider
    from dvf.packs.secrets.gcp_provider import GoogleSecretManagerProvider
    from dvf.packs.secrets.keyring_provider import KeyringSecretProvider
    from dvf.packs.secrets.vault_provider import VaultSecretProvider

    providers = (
        KeyringSecretProvider,
        AzureKeyVaultProvider,
        AWSSecretsManagerProvider,
        GoogleSecretManagerProvider,
        VaultSecretProvider,
        DatabricksSecretProvider,
    )
    for provider in providers:
        attach(
            registry,
            PluginKind.SECRET_PROVIDER,
            provider.name,
            provider,
            maturity="stable" if provider.name in VERIFIED_LIVE else "beta",
            extra=provider.extra,
        )
