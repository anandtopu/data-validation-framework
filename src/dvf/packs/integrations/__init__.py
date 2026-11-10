"""Integrations pack: notifiers, OpenLineage, OpenTelemetry, and the triage interface.

``register`` attaches the Slack, Teams, generic webhook, and email notifiers to the components
declared in :mod:`dvf.plugins.builtins`. The runner reaches the rest through
:class:`dvf.packs.integrations.runtime.RunIntegrations`:

* :mod:`~dvf.packs.integrations.dispatch` sends notifications by run status, with retries;
* :mod:`~dvf.packs.integrations.lineage` emits OpenLineage run events with data quality facets;
* :mod:`~dvf.packs.integrations.telemetry` records OpenTelemetry spans and metrics;
* :mod:`~dvf.packs.integrations.triage` calls opted-in ``triage_failures`` hooks.

See ``docs/notifications.md`` and ``docs/observability.md``. ``register`` is called by
:func:`dvf.packs.register_packs`.
"""

from __future__ import annotations

from dvf.packs import attach
from dvf.plugins.registry import PluginKind, Registry

VERIFIED = frozenset({"slack", "teams", "webhook", "email"})
"""Notifiers verified end to end against local HTTP and SMTP test servers (the protocol, not the
real Slack and Teams services)."""


def register(registry: Registry) -> None:
    """Attach this pack's implementations and register its new components."""
    from dvf.packs.integrations.notifiers import BUILTIN_NOTIFIERS

    for name, notifier in BUILTIN_NOTIFIERS.items():
        notifier.plugin_name = name
        attach(
            registry,
            PluginKind.NOTIFIER,
            name,
            notifier,
            maturity="stable" if name in VERIFIED else "beta",
            description=(notifier.__doc__ or "").strip().splitlines()[0] or None,
            config_model=notifier.Config,
        )
