"""The stable public Python API.

``load_config`` and ``validate_config`` read configuration; ``run`` executes it and returns the
canonical :class:`~dvf.models.RunResult`; ``compare`` compares two datasets without a config file;
``render_reports`` re-renders reports from a results document; ``validate_dataframe`` validates a
pandas, Polars, Arrow, or Spark DataFrame inside a pipeline and quarantines failing rows.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvf.compare.adhoc import AdHocSpec, adhoc_config
from dvf.config.diagnostics import Diagnostic
from dvf.config.loader import LoadedConfig, LoadOptions
from dvf.config.loader import load_config as _load_config
from dvf.config.loader import load_config_data as _load_config_data
from dvf.config.models import DvfConfig
from dvf.config.schema import config_json_schema, results_json_schema
from dvf.core.enums import Severity
from dvf.core.errors import ConfigError, DataValidationError
from dvf.dataframes import ValidationOutput, foreach_batch_validator, validate_dataframe
from dvf.diagnose.explain import ExplainReport
from dvf.diagnose.explain import explain as _explain
from dvf.diagnose.runs import CheckHandle, RunHandle
from dvf.diagnose.runs import load_run as _load_run
from dvf.diagnose.trace import TraceReport
from dvf.diagnose.trace import trace as _trace
from dvf.execution.history import HistoryStore, RunDiff, list_named_baselines
from dvf.execution.history import compare_runs as _compare_runs
from dvf.execution.runner import Runner, RunOptions, RunOutput, exit_code_for
from dvf.execution.selectors import Selectors
from dvf.models.results import ArtifactRef, RunResult
from dvf.plugins.registry import Registry, get_registry
from dvf.reporting.base import ReportOptions
from dvf.reporting.base import render_reports as _render_reports

__all__ = [
    "AdHocSpec",
    "CheckHandle",
    "ConfigError",
    "DataValidationError",
    "DvfConfig",
    "ExplainReport",
    "LoadOptions",
    "LoadedConfig",
    "ReportOptions",
    "RunDiff",
    "RunHandle",
    "RunOutput",
    "RunResult",
    "TraceReport",
    "ValidationOutput",
    "ValidationReport",
    "capture_baseline",
    "compare",
    "compare_runs",
    "config_json_schema",
    "discover",
    "exit_code",
    "explain",
    "export_contract",
    "foreach_batch_validator",
    "get_registry",
    "import_contract",
    "import_mapping",
    "list_baselines",
    "load_config",
    "load_config_data",
    "load_run",
    "profile",
    "render_reports",
    "results_json_schema",
    "run",
    "run_history",
    "state_dir",
    "suggest",
    "trace",
    "validate_config",
    "validate_dataframe",
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
    resume: str | None = None,
    force_resume: bool = False,
    failed_from: str | None = None,
    parallel: int | None = None,
    keep_temp: bool = False,
    history: bool = True,
    baseline: str | None = None,
) -> RunOutput:
    """Run a configuration and return the results and the report files written.

    ``resume`` continues an interrupted or killed run from its checkpoints; ``failed_from`` runs
    only the checks that failed or errored in that earlier run. ``parallel`` overrides
    ``defaults.execution.max_parallel_tables``; ``baseline`` overrides ``history.baseline``.

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
    selectors = Selectors.parse(list(only))
    if failed_from is not None:
        handle = load_run(failed_from, config=loaded)
        rerun = handle.rerun_selectors()
        selectors = Selectors.parse([*selectors.raw, *rerun.raw])
    options = RunOptions(
        selectors=selectors,
        run_id=run_id,
        output_dir=Path(output_dir) if output_dir is not None else None,
        formats=list(formats) if formats is not None else None,
        write_reports=write_reports,
        resume=resume,
        force_resume=force_resume,
        parallel=parallel,
        keep_temp=keep_temp,
        history=history,
        baseline=baseline,
    )
    return Runner(loaded, options).run()


def state_dir(loaded: LoadedConfig) -> Path:
    """The folder holding a configuration's checkpoints, run results, and history."""
    path = Path(loaded.config.defaults.execution.state_dir)
    return path if path.is_absolute() else loaded.base_dir / path


def load_run(
    run: str | os.PathLike[str],
    *,
    config: str | os.PathLike[str] | LoadedConfig | None = None,
    state: str | os.PathLike[str] | None = None,
) -> RunHandle:
    """Load an earlier run by run ID (from the configuration's state folder, ``.dvf`` by
    default) or by ``results.json`` path. ``handle.check(check_id).mismatches()`` returns the
    mismatched values as an Arrow table (or ``format="pandas"`` / ``"polars"``)."""
    folder: Path | None = Path(state) if state is not None else None
    if folder is None and config is not None:
        loaded = config if isinstance(config, LoadedConfig) else load_config(config, strict=False)
        folder = state_dir(loaded)
    return _load_run(Path(run) if str(run).endswith(".json") else str(run), state_dir=folder)


def explain(
    config: str | os.PathLike[str] | LoadedConfig,
    *,
    table: str | None = None,
    suite: str | None = None,
    only: Sequence[str] = (),
) -> ExplainReport:
    """The plan of a run and the SQL each step would execute, without running validations
    (only catalog lookups touch the connections)."""
    loaded = config if isinstance(config, LoadedConfig) else load_config(config)
    runner = Runner(loaded, RunOptions(selectors=Selectors.parse(list(only)), write_reports=False))
    return _explain(runner, table=table, suite=suite)


def trace(
    config: str | os.PathLike[str] | LoadedConfig,
    *,
    table: str,
    keys: Sequence[str],
    suite: str | None = None,
) -> TraceReport:
    """Follow the record with ``keys`` through both sides: raw value, each normalizer, canonical
    value, and row hash."""
    loaded = config if isinstance(config, LoadedConfig) else load_config(config)
    runner = Runner(loaded, RunOptions(write_reports=False))
    return _trace(runner, table=table, keys=list(keys), suite=suite)


def run_history(
    config: str | os.PathLike[str] | LoadedConfig, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent runs of the configuration's project, newest first."""
    loaded = config if isinstance(config, LoadedConfig) else load_config(config, strict=False)
    store = HistoryStore(loaded.config.history.store, loaded.base_dir)
    return store.runs(loaded.config.project.name, limit)


def compare_runs(
    run_a: str | os.PathLike[str],
    run_b: str | os.PathLike[str],
    *,
    config: str | os.PathLike[str] | LoadedConfig | None = None,
    state: str | os.PathLike[str] | None = None,
) -> RunDiff:
    """How run ``run_b`` differs from the earlier run ``run_a`` (for example two mock cutover
    rehearsals): new, resolved, and persisting failures, status changes, and metric deltas.
    Runs are given by run ID (found through ``config`` or ``state``) or ``results.json`` path."""
    before = load_run(run_a, config=config, state=state).result
    after = load_run(run_b, config=config, state=state).result
    return _compare_runs(before, after)


def capture_baseline(
    config: str | os.PathLike[str] | LoadedConfig,
    name: str,
    *,
    side: str = "source",
    only: Sequence[str] = (),
    overwrite: bool = False,
    distinct: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Capture per-table metrics of one side (row counts, control totals, profiles, grouped
    counts) as the named baseline ``name`` in the state folder, before a migration (MIG-14).
    The ``baseline_metrics`` check compares later runs with it. Returns the file and document."""
    from dvf.packs.migration.baseline import capture_baseline as _capture

    loaded = config if isinstance(config, LoadedConfig) else load_config(config)
    return _capture(loaded, name, side=side, only=only, overwrite=overwrite, distinct=distinct)


def list_baselines(config: str | os.PathLike[str] | LoadedConfig) -> list[dict[str, Any]]:
    """The named baselines in the configuration's state folder, newest first."""
    loaded = config if isinstance(config, LoadedConfig) else load_config(config, strict=False)
    return list_named_baselines(state_dir(loaded))


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


# ---------------------------------------------------------------------- discovery and generation

if TYPE_CHECKING:
    from dvf.discovery.contract_export import ContractExportResult
    from dvf.discovery.contract_import import ContractImportResult
    from dvf.discovery.discover import DiscoveryResult
    from dvf.discovery.mapping import MappingResult
    from dvf.discovery.profiling import ProfileReport
    from dvf.discovery.suggest import SuggestResult


def _optional_config(
    config: str | os.PathLike[str] | LoadedConfig | None,
    profile: str | None = None,
    *,
    connect: bool = False,
) -> tuple[LoadedConfig | None, Path | None]:
    """Load ``config`` if given. Without ``connect`` (connections are only copied), missing
    environment variables are warnings and secrets are not fetched."""
    if config is None:
        return None, None
    if isinstance(config, LoadedConfig):
        return config, config.files[0] if config.files else None
    loaded = load_config(config, profile=profile, strict=connect, resolve_secrets=connect)
    return loaded, Path(config)


def _side(value: str) -> Any:
    if value not in ("auto", "source", "target", "both"):
        raise ConfigError(f"'side' must be auto, source, target, or both, got '{value}'.")
    return value


def discover(
    config: str | os.PathLike[str] | LoadedConfig | None = None,
    *,
    source: str | None = None,
    target: str | None = None,
    source_schema: str | None = None,
    target_schema: str | None = None,
    source_pattern: str = "*",
    target_pattern: str = "*",
    threshold: float = 0.8,
    fuzzy: bool = True,
    probe_keys: bool = False,
    suite_name: str = "discovered",
    project_name: str = "discovered",
    output: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
    profile: str | None = None,
) -> DiscoveryResult:
    """Introspect two connections and generate a comparison configuration.

    With ``config``, ``source`` and ``target`` name connections in it (default: the first
    comparison suite's, or the only two connections). Without it they are locations: database
    URLs (``postgresql://...``), ``duckdb:///path``, ``sqlite:///path``, or folders of files.
    With ``output`` the configuration is written there (``overwrite`` replaces a file).
    """
    from dvf.discovery.discover import DiscoverOptions
    from dvf.discovery.discover import discover as _discover

    loaded, config_path = _optional_config(config, profile, connect=True)
    options = DiscoverOptions(
        source_schema=source_schema,
        target_schema=target_schema,
        source_pattern=source_pattern,
        target_pattern=target_pattern,
        threshold=threshold,
        fuzzy=fuzzy,
        probe_keys=probe_keys,
        suite_name=suite_name,
        project_name=project_name,
    )
    return _discover(
        loaded=loaded,
        config_path=config_path,
        source=source,
        target=target,
        options=options,
        output=Path(output) if output is not None else None,
        overwrite=overwrite,
    )


def import_mapping(
    path: str | os.PathLike[str],
    *,
    sheet: str | None = None,
    config: str | os.PathLike[str] | LoadedConfig | None = None,
    source: str | None = None,
    target: str | None = None,
    suite_name: str | None = None,
    project_name: str | None = None,
    include_unlisted: bool = False,
    output: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
) -> MappingResult:
    """Convert a source-to-target mapping sheet (``.xlsx`` or ``.csv``) into a configuration.

    Connections come from ``config`` (``source`` and ``target`` name its connections), from
    ``source`` and ``target`` locations, or are placeholders to replace.
    """
    from dvf.discovery.mapping import import_mapping as _import_mapping

    loaded, config_path = _optional_config(config)
    return _import_mapping(
        Path(path),
        sheet=sheet,
        suite_name=suite_name,
        project_name=project_name,
        loaded=loaded,
        config_path=config_path,
        source=source,
        target=target,
        include_unlisted=include_unlisted,
        output=Path(output) if output is not None else None,
        overwrite=overwrite,
    )


def import_contract(
    path: str | os.PathLike[str],
    *,
    server: str | None = None,
    config: str | os.PathLike[str] | LoadedConfig | None = None,
    connection: str | None = None,
    location: str | None = None,
    suite_name: str | None = None,
    project_name: str | None = None,
    conformance: bool = True,
    between: str = "exclusive",
    output: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
) -> ContractImportResult:
    """Convert an ODCS v3 data contract into a configuration with schema and quality checks.

    Rules dvf cannot run are listed in ``result.unsupported`` with the reason.
    """
    from dvf.discovery.contract_import import import_contract as _import_contract

    if between not in ("exclusive", "inclusive"):
        raise ConfigError(f"'between' must be 'exclusive' or 'inclusive', got '{between}'.")
    loaded, config_path = _optional_config(config)
    return _import_contract(
        Path(path),
        server=server,
        loaded=loaded,
        config_path=config_path,
        connection=connection,
        location=location,
        suite_name=suite_name,
        project_name=project_name,
        conformance=conformance,
        between="inclusive" if between == "inclusive" else "exclusive",
        output=Path(output) if output is not None else None,
        overwrite=overwrite,
    )


def export_contract(
    config: str | os.PathLike[str] | LoadedConfig,
    *,
    tables: Sequence[str] = (),
    suite: str | None = None,
    side: str = "auto",
    contract_id: str | None = None,
    version: str = "1.0.0",
    status: str = "draft",
    output: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
) -> ContractExportResult:
    """Write an ODCS v3.1 contract describing the configuration's datasets and checks."""
    from dvf.discovery.contract_export import export_contract as _export_contract

    loaded = config if isinstance(config, LoadedConfig) else load_config(config)
    return _export_contract(
        loaded,
        tables=tables,
        suite=suite,
        side=_side(side),
        contract_id=contract_id,
        version=version,
        status=status,
        output=Path(output) if output is not None else None,
        overwrite=overwrite,
    )


def profile(
    config: str | os.PathLike[str] | LoadedConfig,
    *,
    tables: Sequence[str] = (),
    suite: str | None = None,
    side: str = "auto",
    top: int = 5,
    formats: bool = True,
) -> ProfileReport:
    """Column profiles (NULLs, distinct values, minimum, maximum, lengths, top values) of the
    selected tables, computed inside each database. ``side="auto"`` profiles the target of
    comparison suites and the source of single-dataset suites."""
    from dvf.discovery.profiling import profile as _profile

    loaded = config if isinstance(config, LoadedConfig) else load_config(config)
    return _profile(loaded, tables=tables, suite=suite, side=_side(side), top=top, formats=formats)


def suggest(
    config: str | os.PathLike[str] | LoadedConfig | None = None,
    *,
    profile_report: ProfileReport | str | os.PathLike[str] | None = None,
    tables: Sequence[str] = (),
    suite: str | None = None,
    side: str = "auto",
    margin: float = 0.0,
    max_values: int = 10,
    freshness: bool = True,
    output: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
) -> SuggestResult:
    """Candidate checks from column profiles, as a YAML fragment with evidence comments.

    Profiles the configuration, or reads ``profile_report`` (a report, or a JSON file written by
    ``dvf profile --format json``).
    """
    from dvf.discovery.profiling import load_profile
    from dvf.discovery.suggest import SuggestOptions
    from dvf.discovery.suggest import suggest as _suggest

    if profile_report is None:
        if config is None:
            raise ConfigError("Give a configuration or a profile report.")
        report = profile(config, tables=tables, suite=suite, side=side, top=max(5, max_values + 1))
    else:
        report = load_profile(profile_report)
    options = SuggestOptions(margin=margin, max_values=max_values, freshness=freshness)
    return _suggest(
        report,
        options,
        output=Path(output) if output is not None else None,
        overwrite=overwrite,
    )
