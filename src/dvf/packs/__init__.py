"""Built-in packs: groups of components that register themselves (Phase 3 onward).

Each pack is a module or package under ``dvf.packs`` with a ``register(registry)`` function. It
attaches implementations to components already declared in :mod:`dvf.plugins.builtins` (with
:func:`attach`) and registers new ones. Pack modules must not import optional drivers at import
time: import them inside connectors, when a connection opens, so a missing extra only affects the
connections that need it.
"""

from __future__ import annotations

import importlib
from typing import Any

from dvf.plugins.registry import PluginKind, Registry

PACKS: tuple[str, ...] = (
    "dvf.packs.spark",
    "dvf.packs.files",
    "dvf.packs.nosql",
    "dvf.packs.sap",
    "dvf.packs.migration",
    "dvf.packs.discovery",
    "dvf.packs.integrations",
    "dvf.packs.secrets",
    "dvf.packs.catalog",
)
"""Pack modules registered with the built-ins, in order."""


def attach(
    registry: Registry,
    kind: PluginKind,
    name: str,
    implementation: Any,
    *,
    maturity: str = "stable",
    description: str | None = None,
    config_model: Any = None,
    extra: str | None = None,
) -> None:
    """Register ``implementation`` for a built-in component.

    Description, configuration model, and extra default to the existing declaration's, so a pack
    only states what it changes. Use ``maturity="beta"`` for implementations that are not yet
    verified against a live system (AGENTS.md).
    """
    declared = registry.find(kind, name)
    registry.register(
        kind,
        name,
        implementation,
        description=description or (declared.description if declared else None),
        config_model=config_model or (declared.config_model if declared else None),
        origin="builtin",
        extra=extra or (declared.extra if declared else None),
        maturity=maturity,
    )


def register_packs(registry: Registry) -> None:
    """Call every pack's ``register`` function."""
    for module_name in PACKS:
        module = importlib.import_module(module_name)
        module.register(registry)


__all__ = ["PACKS", "attach", "register_packs"]
