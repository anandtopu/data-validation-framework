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

from dvf.checks.context import CheckContext, CheckOutcome
from dvf.checks.pair import CheckEnvironment, EnvironmentAware, PairColumn, PairContext
from dvf.config.checks import CheckConfig
from dvf.config.connections import ConnectionConfig
from dvf.config.notifications import NotificationConfig
from dvf.core.enums import Dimension, Severity, Side
from dvf.plugins.base import (
    Check,
    Comparator,
    Component,
    ConnectionReport,
    Connector,
    Normalizer,
    Notifier,
    PairCheck,
    Relation,
    Reporter,
    ResultSink,
    RowCheck,
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
from dvf.reporting.base import ReportOptions, ReportSection
from dvf.security.secrets import SecretContext, SecretProvider, SecretRef

__all__ = [
    "PLUGIN_API_VERSION",
    "Check",
    "CheckConfig",
    "CheckContext",
    "CheckEnvironment",
    "CheckOutcome",
    "Comparator",
    "Component",
    "ConnectionConfig",
    "ConnectionReport",
    "Connector",
    "Dimension",
    "EnvironmentAware",
    "Normalizer",
    "NotificationConfig",
    "Notifier",
    "PairCheck",
    "PairColumn",
    "PairContext",
    "PluginEntry",
    "PluginKind",
    "PluginLoadFailure",
    "Registry",
    "Relation",
    "ReportOptions",
    "ReportSection",
    "Reporter",
    "ResultSink",
    "RowCheck",
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
