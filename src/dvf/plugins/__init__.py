"""The public plugin API (ADR 0005).

Example::

    from dvf.plugins import Check, CheckConfig, Dimension, register_check

    @register_check("acme.vat_number_valid")
    class VatNumberValid(Check):
        \"\"\"VAT numbers must be valid for their country.\"\"\"

        dimension = Dimension.VALIDITY

        class Config(CheckConfig):
            column: str
"""

from __future__ import annotations

from dvf.config.checks import CheckConfig
from dvf.config.connections import ConnectionConfig
from dvf.config.notifications import NotificationConfig
from dvf.core.enums import Dimension, Severity, Side
from dvf.plugins.base import (
    Check,
    Comparator,
    Component,
    Connector,
    Normalizer,
    Notifier,
    Reporter,
)
from dvf.plugins.decorators import (
    register,
    register_check,
    register_comparator,
    register_connector,
    register_engine,
    register_normalizer,
    register_notifier,
    register_reporter,
    register_secret_provider,
    register_sink,
)
from dvf.plugins.hooks import hookimpl
from dvf.plugins.registry import (
    PLUGIN_API_VERSION,
    PluginEntry,
    PluginKind,
    PluginLoadFailure,
    Registry,
    activate,
    get_registry,
    new_registry,
)
from dvf.security.secrets import SecretContext, SecretProvider, SecretRef

__all__ = [
    "PLUGIN_API_VERSION",
    "Check",
    "CheckConfig",
    "Comparator",
    "Component",
    "ConnectionConfig",
    "Connector",
    "Dimension",
    "Normalizer",
    "NotificationConfig",
    "Notifier",
    "PluginEntry",
    "PluginKind",
    "PluginLoadFailure",
    "Registry",
    "Reporter",
    "SecretContext",
    "SecretProvider",
    "SecretRef",
    "Severity",
    "Side",
    "activate",
    "get_registry",
    "hookimpl",
    "new_registry",
    "register",
    "register_check",
    "register_comparator",
    "register_connector",
    "register_engine",
    "register_normalizer",
    "register_notifier",
    "register_reporter",
    "register_secret_provider",
    "register_sink",
]
