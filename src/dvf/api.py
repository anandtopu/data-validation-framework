"""The stable public Python API.

Phase 0 covers configuration and schemas. ``run()`` and ``validate_dataframe()`` arrive in
Phase 1 and Phase 3.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dvf.config.diagnostics import Diagnostic
from dvf.config.loader import LoadedConfig, LoadOptions
from dvf.config.loader import load_config as _load_config
from dvf.config.loader import load_config_data as _load_config_data
from dvf.config.models import DvfConfig
from dvf.config.schema import config_json_schema, results_json_schema
from dvf.core.errors import ConfigError
from dvf.plugins.registry import Registry, get_registry

__all__ = [
    "ConfigError",
    "DvfConfig",
    "LoadOptions",
    "LoadedConfig",
    "ValidationReport",
    "config_json_schema",
    "get_registry",
    "load_config",
    "load_config_data",
    "results_json_schema",
    "validate_config",
]


def load_config(
    path: str | os.PathLike[str],
    *,
    profile: str | None = None,
    overrides: Sequence[str] = (),
    variables: Mapping[str, str] | None = None,
    env_file: str | os.PathLike[str] | None = None,
    strict: bool = True,
    resolve_secrets: bool = True,
    plugins: Sequence[str] = (),
    registry: Registry | None = None,
) -> LoadedConfig:
    """Load and validate a configuration file. Raises :class:`ConfigError` on any error."""
    options = LoadOptions(
        profile=profile,
        overrides=tuple(overrides),
        variables=dict(variables or {}),
        env_file=Path(env_file) if env_file is not None else None,
        strict=strict,
        resolve_secrets=resolve_secrets,
        plugins=tuple(plugins),
    )
    return _load_config(path, options, registry=registry)


def load_config_data(
    data: Mapping[str, Any],
    *,
    profile: str | None = None,
    variables: Mapping[str, str] | None = None,
    strict: bool = True,
    resolve_secrets: bool = True,
    base_dir: str | os.PathLike[str] | None = None,
    registry: Registry | None = None,
) -> LoadedConfig:
    """Validate an in-memory configuration mapping (for notebooks and tests)."""
    options = LoadOptions(
        profile=profile,
        variables=dict(variables or {}),
        strict=strict,
        resolve_secrets=resolve_secrets,
        env_overrides=False,
    )
    return _load_config_data(
        data,
        options,
        registry=registry,
        base_dir=Path(base_dir) if base_dir is not None else None,
    )


@dataclass
class ValidationReport:
    """The outcome of :func:`validate_config`. Never raises for configuration problems."""

    path: Path
    diagnostics: list[Diagnostic] = field(default_factory=list)
    loaded: LoadedConfig | None = None

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_error]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if not d.is_error]

    @property
    def ok(self) -> bool:
        return self.loaded is not None and not self.errors

    def summary(self) -> dict[str, int]:
        """Counts of suites, tables, checks, and connections in a valid configuration."""
        if self.loaded is None:
            return {}
        config = self.loaded.config
        tables = config.iter_tables()
        return {
            "suites": len(config.suites),
            "tables": len(tables),
            "checks": sum(len(table.checks) for _, table in tables),
            "connections": len(config.connections),
        }


def validate_config(
    path: str | os.PathLike[str],
    *,
    profile: str | None = None,
    overrides: Sequence[str] = (),
    variables: Mapping[str, str] | None = None,
    env_file: str | os.PathLike[str] | None = None,
    strict: bool = False,
    resolve_secrets: bool = False,
    plugins: Sequence[str] = (),
    registry: Registry | None = None,
) -> ValidationReport:
    """Validate a configuration file and report every problem found.

    By default this is lenient, so it can run in CI without credentials: missing environment
    variables are warnings and secrets are not fetched.
    """
    report = ValidationReport(path=Path(path))
    try:
        report.loaded = load_config(
            path,
            profile=profile,
            overrides=overrides,
            variables=variables,
            env_file=env_file,
            strict=strict,
            resolve_secrets=resolve_secrets,
            plugins=plugins,
            registry=registry,
        )
        report.diagnostics = list(report.loaded.diagnostics)
    except ConfigError as exc:
        report.diagnostics = list(exc.diagnostics)
        if not report.diagnostics:
            raise
    return report
