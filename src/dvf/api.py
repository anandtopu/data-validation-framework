"""The stable public Python API.

``load_config`` and ``validate_config`` read configuration; ``run`` executes it and returns the
canonical :class:`~dvf.models.RunResult`; ``compare`` compares two datasets without a config file;
``render_reports`` re-renders reports from a results document. ``validate_dataframe`` (in-pipeline
mode) arrives in Phase 3.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dvf.compare.adhoc import AdHocSpec, adhoc_config
from dvf.config.diagnostics import Diagnostic
from dvf.config.loader import LoadedConfig, LoadOptions
from dvf.config.loader import load_config as _load_config
from dvf.config.loader import load_config_data as _load_config_data
from dvf.config.models import DvfConfig
from dvf.config.schema import config_json_schema, results_json_schema
from dvf.core.enums import Severity
from dvf.core.errors import ConfigError
from dvf.execution.runner import Runner, RunOptions, RunOutput, exit_code_for
from dvf.execution.selectors import Selectors
from dvf.models.results import ArtifactRef, RunResult
from dvf.plugins.registry import Registry, get_registry
from dvf.reporting.base import ReportOptions
from dvf.reporting.base import render_reports as _render_reports

__all__ = [
    "AdHocSpec",
    "ConfigError",
    "DvfConfig",
    "LoadOptions",
    "LoadedConfig",
    "ReportOptions",
    "RunOutput",
    "RunResult",
    "ValidationReport",
    "compare",
    "config_json_schema",
    "exit_code",
    "get_registry",
    "load_config",
    "load_config_data",
    "render_reports",
    "results_json_schema",
    "run",
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


def run(
    config: str | os.PathLike[str] | LoadedConfig | DvfConfig,
    *,
    profile: str | None = None,
    overrides: Sequence[str] = (),
    variables: Mapping[str, str] | None = None,
    env_file: str | os.PathLike[str] | None = None,
    only: Sequence[str] = (),
    run_id: str | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    formats: Sequence[str] | None = None,
    write_reports: bool = True,
    plugins: Sequence[str] = (),
    registry: Registry | None = None,
) -> RunOutput:
    """Run a configuration and return the results and the report files written.

    ``config`` is a file path, a :class:`LoadedConfig`, or a :class:`DvfConfig` built in code
    (relative paths are then resolved against the current directory). ``only`` takes selectors
    such as ``suite:finance``, ``table:customers``, ``check:not_null``, or ``tag:nightly``.
    """
    if isinstance(config, LoadedConfig):
        loaded = config
    elif isinstance(config, DvfConfig):
        loaded = load_config_data(
            config.model_dump(mode="python", by_alias=True, exclude_none=False),
            profile=profile,
            variables=variables,
            base_dir=Path.cwd(),
            registry=registry,
        )
    else:
        loaded = load_config(
            config,
            profile=profile,
            overrides=overrides,
            variables=variables,
            env_file=env_file,
            plugins=plugins,
            registry=registry,
        )
    options = RunOptions(
        selectors=Selectors.parse(list(only)),
        run_id=run_id,
        output_dir=Path(output_dir) if output_dir is not None else None,
        formats=list(formats) if formats is not None else None,
        write_reports=write_reports,
    )
    return Runner(loaded, options).run()


def compare(
    source: str,
    target: str,
    *,
    keys: Sequence[str] = (),
    strategies: Sequence[str] = ("row_count", "aggregate", "full_diff"),
    engine: str = "auto",
    output_dir: str | os.PathLike[str] | None = None,
    formats: Sequence[str] | None = ("json",),
    write_reports: bool = True,
    sample_size: int = 20,
    registry: Registry | None = None,
) -> RunOutput:
    """Compare two datasets without a configuration file.

    ``source`` and ``target`` are file paths (CSV, Parquet, JSON) or database locations such as
    ``duckdb:///path/db.duckdb::schema.table``, ``sqlite:///path/db.sqlite::table``, or
    ``postgresql://user@host/db::schema.table``.
    """
    spec = AdHocSpec(
        source=source,
        target=target,
        keys=list(keys),
        strategies=list(strategies),
        engine=engine,
        sample_size=sample_size,
        formats=list(formats or ["json"]),
    )
    data = adhoc_config(spec)
    loaded = load_config_data(data, base_dir=Path.cwd(), registry=registry, strict=False)
    options = RunOptions(
        output_dir=Path(output_dir) if output_dir is not None else None,
        write_reports=write_reports,
    )
    return Runner(loaded, options).run()


def exit_code(
    result: RunResult,
    *,
    fail_on: Severity | str = Severity.ERROR,
    strict: bool = False,
    min_score: float | None = None,
) -> int:
    """The CLI exit code for ``result``: 0 passed, 1 validation failures, 2 execution errors."""
    return exit_code_for(result, fail_on=Severity(fail_on), strict=strict, min_score=min_score)


def render_reports(
    result: RunResult | str | os.PathLike[str],
    formats: Sequence[str],
    output_dir: str | os.PathLike[str],
    *,
    options: ReportOptions | None = None,
    registry: Registry | None = None,
) -> list[ArtifactRef]:
    """Render reports from a :class:`RunResult` or a ``results.json`` file, without touching data."""
    if not isinstance(result, RunResult):
        result = RunResult.model_validate_json(Path(result).read_text(encoding="utf-8"))
    return _render_reports(
        result,
        list(formats),
        Path(output_dir),
        options or ReportOptions(),
        registry or get_registry(),
    )
