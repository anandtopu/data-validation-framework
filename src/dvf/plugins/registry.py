"""The component registry (ADR 0005).

Built-in components, installed entry points, and project-local plugins all register here. An
entry is *declared* when it carries only a name, description, and config model, and *ready* once
an implementation is attached.
"""

from __future__ import annotations

import difflib
import inspect
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from importlib.metadata import entry_points
from types import ModuleType
from typing import Any

import pluggy
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from pydantic import BaseModel

from dvf.config.checks import CheckConfig
from dvf.config.connections import ConnectionConfig
from dvf.config.notifications import NotificationConfig
from dvf.core.errors import PluginError, UnknownPluginError
from dvf.plugins.hooks import PROJECT_NAME, HookSpecs

PLUGIN_API_VERSION = "0.1.0"
"""SemVer of the plugin contract. Plugins may require a range with ``dvf_plugin_api``."""

HOOKS_ENTRY_POINT_GROUP = "dvf.hooks"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class PluginKind(StrEnum):
    """Kinds of registrable components."""

    CONNECTOR = "connector"
    CHECK = "check"
    COMPARATOR = "comparator"
    NORMALIZER = "normalizer"
    REPORTER = "reporter"
    NOTIFIER = "notifier"
    SECRET_PROVIDER = "secret_provider"  # noqa: S105 - an enum name, not a password
    ENGINE = "engine"
    SINK = "sink"

    @property
    def entry_point_group(self) -> str:
        return f"dvf.{self.value}s"


_CONFIG_BASES: dict[PluginKind, type[BaseModel]] = {
    PluginKind.CONNECTOR: ConnectionConfig,
    PluginKind.CHECK: CheckConfig,
    PluginKind.NOTIFIER: NotificationConfig,
}


@dataclass(frozen=True)
class PluginEntry:
    """One registered component."""

    kind: PluginKind
    name: str
    description: str = ""
    implementation: Any = None
    config_model: type[BaseModel] | None = None
    origin: str = "builtin"
    """``builtin``, ``entry-point``, or ``local``."""
    distribution: str | None = None
    version: str | None = None
    extra: str | None = None
    """The pip extra expected to provide the implementation, for declared built-ins."""
    maturity: str = "stable"
    """``stable``, or ``beta`` for an implementation not yet verified against a live system."""

    @property
    def ready(self) -> bool:
        return self.implementation is not None

    @property
    def status(self) -> str:
        """``declared`` (no implementation), ``beta`` (implemented, not verified against a live
        system), or ``ready``."""
        if not self.ready:
            return "declared"
        return "beta" if self.maturity == "beta" else "ready"

    @property
    def source(self) -> str:
        if self.distribution:
            version = f" {self.version}" if self.version else ""
            return f"{self.origin} ({self.distribution}{version})"
        return self.origin


@dataclass(frozen=True)
class PluginLoadFailure:
    """A plugin that could not be loaded. Other plugins still load."""

    source: str
    error: str


class Registry:
    """Holds every component by kind and name, plus the pluggy hook manager."""

    def __init__(self) -> None:
        self._entries: dict[PluginKind, dict[str, PluginEntry]] = {k: {} for k in PluginKind}
        self.load_failures: list[PluginLoadFailure] = []
        self.hooks = pluggy.PluginManager(PROJECT_NAME)
        self.hooks.add_hookspecs(HookSpecs)
        self.loaded_local: dict[str, ModuleType] = {}
        """Local plugin sources (resolved path or module name) already imported for this registry."""

    # ------------------------------------------------------------------ registration
    def register(
        self,
        kind: PluginKind | str,
        name: str,
        implementation: Any = None,
        *,
        description: str | None = None,
        config_model: type[BaseModel] | None = None,
        origin: str = "local",
        distribution: str | None = None,
        version: str | None = None,
        extra: str | None = None,
        replace_existing: bool = False,
        maturity: str = "stable",
    ) -> PluginEntry:
        """Register a component and return its entry.

        Registering the same implementation twice is a no-op (the newer metadata wins).
        A built-in declaration accepts a built-in implementation without ``replace_existing``.
        """
        kind = PluginKind(kind)
        if not _NAME_RE.match(name):
            raise PluginError(
                f"Invalid {kind} name '{name}': use lowercase letters, digits, and underscores, "
                "optionally namespaced with dots (for example 'acme.vat_number_valid')."
            )
        if implementation is not None:
            _check_api_compatibility(implementation, f"{kind} '{name}'")
        if config_model is None and implementation is not None:
            config_model = _derive_config_model(implementation)
        base = _CONFIG_BASES.get(kind)
        if config_model is not None and base is not None and not _is_subclass(config_model, base):
            raise PluginError(
                f"The config model for {kind} '{name}' must subclass {base.__name__}, "
                f"got {config_model!r}."
            )
        if description is None:
            description = _first_doc_line(implementation)

        entry = PluginEntry(
            kind=kind,
            name=name,
            description=description,
            implementation=implementation,
            config_model=config_model,
            origin=origin,
            distribution=distribution,
            version=version,
            extra=extra,
            maturity=maturity,
        )
        existing = self._entries[kind].get(name)
        if existing is not None:
            entry = self._resolve_conflict(existing, entry, replace_existing=replace_existing)
        self._entries[kind][name] = entry
        return entry

    def declare(
        self,
        kind: PluginKind | str,
        name: str,
        description: str,
        *,
        config_model: type[BaseModel] | None = None,
        extra: str | None = None,
    ) -> PluginEntry:
        """Declare a built-in component whose implementation arrives later."""
        return self.register(
            kind,
            name,
            None,
            description=description,
            config_model=config_model,
            origin="builtin",
            extra=extra,
        )

    @staticmethod
    def _resolve_conflict(
        existing: PluginEntry, new: PluginEntry, *, replace_existing: bool
    ) -> PluginEntry:
        same_impl = new.implementation is not None and new.implementation is existing.implementation
        if same_impl:
            # Idempotent: keep the richer metadata (an entry point knows its distribution).
            return new if new.distribution or not existing.distribution else existing
        if existing.origin == "builtin" and not existing.ready and new.origin == "builtin":
            return replace(
                new,
                description=new.description or existing.description,
                config_model=new.config_model or existing.config_model,
                extra=new.extra or existing.extra,
            )
        if replace_existing:
            return new
        raise PluginError(
            f"{existing.kind} '{existing.name}' is already registered by {existing.source}. "
            "Pass replace_existing=True to override it deliberately."
        )

    def register_hooks(self, plugin: object, name: str | None = None) -> None:
        """Register an object or module that implements lifecycle hooks."""
        if self.hooks.is_registered(plugin):
            return
        self.hooks.register(plugin, name=name)

    # ------------------------------------------------------------------ lookup
    def find(self, kind: PluginKind | str, name: str) -> PluginEntry | None:
        return self._entries[PluginKind(kind)].get(name)

    def get(self, kind: PluginKind | str, name: str) -> PluginEntry:
        """Return the entry or raise :class:`UnknownPluginError` with suggestions."""
        kind = PluginKind(kind)
        entry = self._entries[kind].get(name)
        if entry is None:
            raise UnknownPluginError(kind.value, name, self.suggest(kind, name))
        return entry

    def names(self, kind: PluginKind | str) -> list[str]:
        return sorted(self._entries[PluginKind(kind)])

    def entries(self, kind: PluginKind | str | None = None) -> list[PluginEntry]:
        kinds = list(PluginKind) if kind is None else [PluginKind(kind)]
        return [self._entries[k][n] for k in kinds for n in sorted(self._entries[k])]

    def suggest(self, kind: PluginKind | str, name: str, limit: int = 3) -> list[str]:
        return difflib.get_close_matches(name, self.names(kind), n=limit, cutoff=0.6)

    # ------------------------------------------------------------------ discovery
    def load_entry_points(self) -> None:
        """Load components and hooks from installed distributions, isolating failures."""
        for kind in PluginKind:
            for ep in entry_points(group=kind.entry_point_group):
                dist = ep.dist.name if ep.dist is not None else None
                version = ep.dist.version if ep.dist is not None else None
                try:
                    with activate(self):
                        obj = ep.load()
                    if isinstance(obj, ModuleType):
                        continue  # importing the module registered its components
                    if not inspect.isclass(obj):
                        raise PluginError("an entry point must reference a class or a module")
                    name = getattr(obj, "plugin_name", "") or ep.name
                    self.register(
                        kind, name, obj, origin="entry-point", distribution=dist, version=version
                    )
                except Exception as exc:  # one broken plugin must not stop others
                    self.load_failures.append(
                        PluginLoadFailure(
                            f"{ep.group}:{ep.name} ({dist or 'unknown distribution'})",
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
        for ep in entry_points(group=HOOKS_ENTRY_POINT_GROUP):
            try:
                self.register_hooks(ep.load(), name=f"{HOOKS_ENTRY_POINT_GROUP}:{ep.name}")
            except Exception as exc:  # one broken plugin must not stop others
                self.load_failures.append(
                    PluginLoadFailure(f"{ep.group}:{ep.name}", f"{type(exc).__name__}: {exc}")
                )


# ---------------------------------------------------------------------- default registry

_default_registry: Registry | None = None
_default_lock = threading.RLock()
_active_registry: ContextVar[Registry | None] = ContextVar("dvf_active_registry", default=None)


def get_registry() -> Registry:
    """Return the process-wide registry, creating it (built-ins plus entry points) on first use."""
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            from dvf.plugins.builtins import declare_builtins

            registry = Registry()
            _default_registry = registry  # assigned first: entry points may call decorators
            declare_builtins(registry)
            registry.load_entry_points()
        return _default_registry


def new_registry(*, load_entry_points: bool = False) -> Registry:
    """Create an independent registry with the built-ins declared (useful for tests and embedding)."""
    from dvf.plugins.builtins import declare_builtins

    registry = Registry()
    declare_builtins(registry)
    if load_entry_points:
        registry.load_entry_points()
    return registry


def current_registry() -> Registry:
    """The registry decorators register into: the active one if set, else the default."""
    return _active_registry.get() or get_registry()


@contextmanager
def activate(registry: Registry) -> Iterator[Registry]:
    """Make ``registry`` the target of registration decorators within the block."""
    token = _active_registry.set(registry)
    try:
        yield registry
    finally:
        _active_registry.reset(token)


def _reset_default_registry() -> None:
    """Forget the default registry. For tests only."""
    global _default_registry
    with _default_lock:
        _default_registry = None


# ---------------------------------------------------------------------- helpers


def _is_subclass(candidate: object, base: type) -> bool:
    return inspect.isclass(candidate) and issubclass(candidate, base)


def _derive_config_model(implementation: Any) -> type[BaseModel] | None:
    for attr in ("Config", "config_model"):
        candidate = getattr(implementation, attr, None)
        if _is_subclass(candidate, BaseModel):
            return candidate
    return None


def _first_doc_line(obj: Any) -> str:
    """The first line of ``obj``'s own docstring (not one inherited from a base class)."""
    doc = getattr(obj, "__doc__", None) if obj is not None else None
    if not isinstance(doc, str) or not doc.strip():
        return ""
    return inspect.cleandoc(doc).splitlines()[0]


def _check_api_compatibility(implementation: Any, label: str) -> None:
    spec = getattr(implementation, "dvf_plugin_api", None)
    if not spec:
        return
    try:
        specifier = SpecifierSet(str(spec))
    except InvalidSpecifier as exc:
        raise PluginError(f"{label} declares an invalid dvf_plugin_api '{spec}': {exc}") from exc
    if not specifier.contains(Version(PLUGIN_API_VERSION), prereleases=True):
        raise PluginError(
            f"{label} requires dvf plugin API {spec}, but this dvf provides {PLUGIN_API_VERSION}."
        )
