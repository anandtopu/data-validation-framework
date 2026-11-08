"""Discovery pack: components used by discovery, mapping and contract import.

Registers the ``contract`` check (SCH-12 contract conformance): the checked dataset's schema
against an ODCS v3 contract or a dvf expected-schema file. ``register`` is called by
:func:`dvf.packs.register_packs`.
"""

from __future__ import annotations

from dvf.plugins.registry import PluginKind, Registry


def register(registry: Registry) -> None:
    """Attach this pack's implementations and register its new components."""
    from dvf.discovery.conformance import ContractCheck, ContractConformance

    registry.register(
        PluginKind.CHECK,
        "contract",
        ContractConformance,
        description="Schema conforms to an ODCS v3 contract or an expected-schema file (SCH-12)",
        config_model=ContractCheck,
        origin="builtin",
    )
