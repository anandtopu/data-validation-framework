"""Decorators that register components into the current registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from dvf.plugins.registry import PluginKind, current_registry

T = TypeVar("T")


def register(
    kind: PluginKind | str,
    name: str,
    *,
    description: str | None = None,
    config_model: type[BaseModel] | None = None,
    replace_existing: bool = False,
) -> Callable[[T], T]:
    """Register the decorated class as a component of ``kind`` called ``name``."""

    def decorator(obj: T) -> T:
        current_registry().register(
            kind,
            name,
            obj,
            description=description,
            config_model=config_model,
            origin="local",
            replace_existing=replace_existing,
        )
        if isinstance(obj, type):
            obj.plugin_name = name  # type: ignore[attr-defined]
        return obj

    return decorator


def register_check(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a check class. Its nested ``Config`` model must subclass ``CheckConfig``."""
    return register(PluginKind.CHECK, name, **kwargs)


def register_connector(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a connector class. Its nested ``Config`` model must subclass ``ConnectionConfig``."""
    return register(PluginKind.CONNECTOR, name, **kwargs)


def register_comparator(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a comparison strategy."""
    return register(PluginKind.COMPARATOR, name, **kwargs)


def register_normalizer(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a normalizer."""
    return register(PluginKind.NORMALIZER, name, **kwargs)


def register_reporter(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a report format."""
    return register(PluginKind.REPORTER, name, **kwargs)


def register_notifier(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a notification channel. Its nested ``Config`` must subclass ``NotificationConfig``."""
    return register(PluginKind.NOTIFIER, name, **kwargs)


def register_secret_provider(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a secret provider (a :class:`dvf.security.secrets.SecretProvider` subclass)."""
    return register(PluginKind.SECRET_PROVIDER, name, **kwargs)


def register_engine(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register an execution engine."""
    return register(PluginKind.ENGINE, name, **kwargs)


def register_sink(name: str, **kwargs: Any) -> Callable[[T], T]:
    """Register a result sink."""
    return register(PluginKind.SINK, name, **kwargs)
