"""The ``dvf`` command line interface: a thin layer over :mod:`dvf.api`."""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from dvf import __version__, api
from dvf.config.diagnostics import Diagnostic
from dvf.config.models import CONFIG_VERSION
from dvf.core.enums import Severity, Status
from dvf.core.errors import ConfigError, DvfError
from dvf.models.results import RESULTS_SCHEMA_VERSION, RunResult
from dvf.observability.logging import LogFormat, configure_logging
from dvf.plugins.local import load_local_plugins
from dvf.plugins.registry import PLUGIN_API_VERSION, PluginKind, get_registry
from dvf.security.redaction import global_redactor


class ExitCode(IntEnum):
    """Process exit codes (documented in the README and BUILD_PROMPT.md section 12.1)."""

    OK = 0
    VALIDATION_FAILED = 1
    EXECUTION_ERROR = 2
    CONFIG_ERROR = 3


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class DumpFormat(StrEnum):
    YAML = "yaml"
    JSON = "json"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class _State:
    plugins: list[str] = field(default_factory=list)


app = typer.Typer(
    name="dvf",
    help="Data and schema validation for data migrations and data engineering.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
schema_app = typer.Typer(help="Export JSON Schemas.", no_args_is_help=True)
plugins_app = typer.Typer(
    help="Inspect installed plugins and built-in components.", no_args_is_help=True
)
connections_app = typer.Typer(help="Work with connections.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(connections_app, name="connections")
app.add_typer(schema_app, name="schema")
app.add_typer(plugins_app, name="plugins")

out = Console(soft_wrap=True)
err = Console(stderr=True, soft_wrap=True)

# ---------------------------------------------------------------------- shared options

ConfigPath = Annotated[
    Path, typer.Argument(help="Configuration file (YAML, JSON, TOML, .properties, .ini).")
]
ProfileOption = Annotated[
    str | None, typer.Option("--profile", "-p", help="Environment profile to apply.")
]
SetOption = Annotated[
    list[str] | None,
    typer.Option(
        "--set",
        metavar="PATH=VALUE",
        help="Override a value, for example defaults.sample_size=10. Repeatable.",
    ),
]
VarOption = Annotated[
    list[str] | None,
    typer.Option("--var", metavar="NAME=VALUE", help="Set a ${var:NAME} value. Repeatable."),
]
EnvFileOption = Annotated[
    Path | None, typer.Option("--env-file", help="A .env file (default: .env next to the config).")
]
ResolveSecretsOption = Annotated[
    bool,
    typer.Option(
        "--resolve-secrets/--no-resolve-secrets", help="Fetch secret values to prove they resolve."
    ),
]
StrictOption = Annotated[
    bool,
    typer.Option(
        "--strict", help="Treat warnings (such as missing environment variables) as errors."
    ),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dvf {__version__}")
        raise typer.Exit(ExitCode.OK)


@app.callback()
def _main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", envvar="DVF_LOG_LEVEL", help="Logging level.")
    ] = LogLevel.WARNING,
    log_format: Annotated[
        LogFormat, typer.Option("--log-format", envvar="DVF_LOG_FORMAT", help="Log output format.")
    ] = LogFormat.TEXT,
    plugins: Annotated[
        list[str] | None,
        typer.Option("--plugins", help="Plugin file, folder, or module to load. Repeatable."),
    ] = None,
) -> None:
    """Data and schema validation for data migrations and data engineering."""
    configure_logging(log_level.value, log_format)
    ctx.obj = _State(plugins=list(plugins or []))


def _state(ctx: typer.Context) -> _State:
    state = ctx.find_object(_State)
    return state if state is not None else _State()


def _parse_vars(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            err.print(f"[red]error[/] --var expects NAME=VALUE, got {escape(item)!s}")
            raise typer.Exit(ExitCode.CONFIG_ERROR)
        result[name.strip()] = value
    return result


def _display_file(file: str) -> str:
    path = Path(file)
    if not path.is_absolute():
        return file
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return file


def _print_diagnostics(diagnostics: list[Diagnostic]) -> None:
    for diagnostic in diagnostics:
        color = "red" if diagnostic.is_error else "yellow"
        where = ""
        if diagnostic.location is not None:
            loc = diagnostic.location
            where = _display_file(loc.file)
            if loc.line is not None:
                where += f":{loc.line}" + (f":{loc.column}" if loc.column is not None else "")
        if diagnostic.path:
            where = f"{where} ({diagnostic.path})" if where else diagnostic.path
        err.print(
            f"[{color}]{diagnostic.level.value}[/] [dim]{diagnostic.code.value}[/] {escape(where)}"
        )
        err.print(f"    {escape(diagnostic.message)}")
        if diagnostic.hint:
            err.print(f"    [dim]hint:[/] {escape(diagnostic.hint)}")


# ---------------------------------------------------------------------- commands


@app.command("version")
def version_command(
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.")
    ] = OutputFormat.TEXT,
) -> None:
    """Show versions of dvf, its schemas, and its environment."""
    from importlib.metadata import version as dist_version

    import pydantic

    info = {
        "dvf": __version__,
        "config_version": CONFIG_VERSION,
        "results_schema_version": RESULTS_SCHEMA_VERSION,
        "plugin_api_version": PLUGIN_API_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pydantic": pydantic.VERSION,
        "sqlglot": dist_version("sqlglot"),
    }
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(info, indent=2))
        return
    table = Table(show_header=False, box=None, pad_edge=False)
    for key, value in info.items():
        table.add_row(f"[bold]{key}[/]", str(value))
    out.print(table)


@app.command("validate-config")
def validate_config_command(
    ctx: typer.Context,
    path: ConfigPath,
    profile: ProfileOption = None,
    set_: SetOption = None,
    var: VarOption = None,
    env_file: EnvFileOption = None,
    resolve_secrets: ResolveSecretsOption = False,
    strict: StrictOption = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.")
    ] = OutputFormat.TEXT,
) -> None:
    """Validate a configuration file and report every problem with its file, line, and column."""
    try:
        report = api.validate_config(
            path,
            profile=profile,
            overrides=set_ or [],
            variables=_parse_vars(var),
            env_file=env_file,
            strict=strict,
            resolve_secrets=resolve_secrets,
            plugins=_state(ctx).plugins,
        )
    except DvfError as exc:
        err.print(f"[red]error[/] {escape(str(exc))}")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    failed = not report.ok or (strict and bool(report.warnings))

    if output_format is OutputFormat.JSON:
        payload: dict[str, Any] = {
            "valid": not failed,
            "path": str(path),
            "summary": report.summary(),
            "files": [str(f) for f in report.loaded.files] if report.loaded else [],
            "diagnostics": [d.to_dict() for d in report.diagnostics],
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        _print_diagnostics(report.diagnostics)
        errors, warnings = len(report.errors), len(report.warnings)
        if report.ok:
            summary = report.summary()
            counts = ", ".join(f"{v} {k if v != 1 else k.rstrip('s')}" for k, v in summary.items())
            status = "[red]FAILED[/] (--strict)" if failed else "[green]OK[/]"
            note = f" with {warnings} warning{'s' if warnings != 1 else ''}" if warnings else ""
            out.print(f"{status} {escape(str(path))} is valid{note}: {counts}.")
        else:
            out.print(
                f"[red]INVALID[/] {escape(str(path))}: {errors} error{'s' if errors != 1 else ''}"
                + (f", {warnings} warning{'s' if warnings != 1 else ''}" if warnings else "")
                + "."
            )
    if failed:
        raise typer.Exit(ExitCode.CONFIG_ERROR)


@config_app.command("show")
def config_show_command(
    ctx: typer.Context,
    path: ConfigPath,
    profile: ProfileOption = None,
    set_: SetOption = None,
    var: VarOption = None,
    env_file: EnvFileOption = None,
    resolve_secrets: ResolveSecretsOption = False,
    strict: StrictOption = False,
    output_format: Annotated[
        DumpFormat, typer.Option("--format", help="Output format.")
    ] = DumpFormat.YAML,
) -> None:
    """Print the fully resolved configuration with secrets redacted."""
    try:
        loaded = api.load_config(
            path,
            profile=profile,
            overrides=set_ or [],
            variables=_parse_vars(var),
            env_file=env_file,
            strict=strict,
            resolve_secrets=resolve_secrets,
            plugins=_state(ctx).plugins,
        )
    except ConfigError as exc:
        _print_diagnostics(exc.diagnostics)
        if not exc.diagnostics:
            err.print(f"[red]error[/] {escape(str(exc))}")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    except DvfError as exc:
        err.print(f"[red]error[/] {escape(str(exc))}")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    _print_diagnostics(loaded.warnings)
    dumped = loaded.config.model_dump(mode="json", by_alias=True)
    redacted = global_redactor().redact(dumped)
    if output_format is DumpFormat.JSON:
        typer.echo(json.dumps(redacted, indent=2, ensure_ascii=False))
    else:
        typer.echo(
            yaml.safe_dump(redacted, sort_keys=False, allow_unicode=True, width=100).rstrip()
        )


def _write_json(document: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if output is None:
        sys.stdout.write(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    err.print(f"Wrote {escape(str(output))}")


OutputOption = Annotated[
    Path | None, typer.Option("--output", "-o", help="Write to this file instead of stdout.")
]


@schema_app.command("config")
def schema_config_command(ctx: typer.Context, output: OutputOption = None) -> None:
    """Export the configuration JSON Schema (including types from loaded plugins)."""
    registry = get_registry()
    _load_cli_plugins(ctx, registry)
    _write_json(api.config_json_schema(registry), output)


@schema_app.command("results")
def schema_results_command(output: OutputOption = None) -> None:
    """Export the results JSON Schema."""
    _write_json(api.results_json_schema(), output)


@plugins_app.command("list")
def plugins_list_command(
    ctx: typer.Context,
    kind: Annotated[
        PluginKind | None, typer.Option("--kind", "-k", help="Only this kind of component.")
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.")
    ] = OutputFormat.TEXT,
) -> None:
    """List components: 'ready' ones have an implementation, 'declared' ones arrive in a later phase."""
    registry = get_registry()
    _load_cli_plugins(ctx, registry)
    entries = registry.entries(kind)
    hooks = [name for name, _ in registry.hooks.list_name_plugin()]
    if output_format is OutputFormat.JSON:
        payload = {
            "plugin_api_version": PLUGIN_API_VERSION,
            "components": [
                {
                    "kind": e.kind.value,
                    "name": e.name,
                    "status": e.status,
                    "origin": e.origin,
                    "distribution": e.distribution,
                    "version": e.version,
                    "extra": e.extra,
                    "description": e.description,
                }
                for e in entries
            ],
            "hooks": hooks,
            "load_failures": [
                {"source": f.source, "error": f.error} for f in registry.load_failures
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    table = Table(title=f"dvf components (plugin API {PLUGIN_API_VERSION})", title_justify="left")
    table.add_column("Kind")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Description", overflow="fold")
    for entry in entries:
        status = "[green]ready[/]" if entry.ready else "[dim]declared[/]"
        table.add_row(
            entry.kind.value, entry.name, status, escape(entry.source), escape(entry.description)
        )
    out.print(table)
    ready = sum(1 for e in entries if e.ready)
    out.print(f"{len(entries)} components: {ready} ready, {len(entries) - ready} declared.")
    if hooks:
        out.print("Hook plugins: " + ", ".join(escape(h) for h in hooks))
    for failure in registry.load_failures:
        err.print(
            f"[yellow]warning[/] plugin {escape(failure.source)} failed to load: {escape(failure.error)}"
        )


_STATUS_STYLE = {
    Status.PASSED: "green",
    Status.WARNING: "yellow",
    Status.FAILED: "red",
    Status.ERROR: "bold red",
    Status.SKIPPED: "dim",
    Status.WAIVED: "blue",
}


def _status_text(status: Status) -> str:
    return f"[{_STATUS_STYLE[status]}]{status.value.upper()}[/]"


def _print_run(result: RunResult, artifacts: list[Any], verbose: bool) -> None:
    table = Table(title=f"dvf run {escape(result.run_id)}", title_justify="left")
    headers = (
        "Suite",
        "Table",
        "Status",
        "Source rows",
        "Target rows",
        "Missing",
        "Extra",
        "Mismatched",
        "Failed checks",
    )
    for column in headers:
        table.add_column(column, justify="left" if column in headers[:3] else "right")
    for suite in result.suites:
        for dataset in suite.datasets:
            rec = dataset.reconciliation
            bad = sum(1 for c in dataset.checks if c.status in (Status.FAILED, Status.ERROR))
            counts = dataset.row_counts
            table.add_row(
                escape(suite.name),
                escape(dataset.name),
                _status_text(dataset.status),
                "" if counts.source is None else f"{counts.source:,}",
                "" if counts.target is None else f"{counts.target:,}",
                f"{rec.missing_in_target:,}" if rec else "",
                f"{rec.extra_in_target:,}" if rec else "",
                f"{rec.mismatched:,}" if rec else "",
                str(bad),
            )
    out.print(table)
    problems = [
        c for c in result.iter_checks() if c.status in (Status.FAILED, Status.ERROR, Status.WARNING)
    ]
    shown = problems if verbose else problems[:15]
    for check in shown:
        out.print(
            f"{_status_text(check.status)} [bold]{escape(check.check_id)}[/]: {escape(check.message or '')}"
        )
        for mismatch in check.column_mismatches[:10]:
            classes = ", ".join(f"{k} {v}" for k, v in mismatch.classifications.items())
            out.print(
                f"    {escape(mismatch.column)}: {mismatch.mismatch_count:,} ({escape(classes)})"
            )
        if check.error is not None and check.error.hint:
            out.print(f"    [dim]hint:[/] {escape(check.error.hint)}")
    if len(problems) > len(shown):
        out.print(
            f"[dim]... {len(problems) - len(shown)} more; see the reports or use --verbose.[/]"
        )
    counts_by_status = result.summary.checks_by_status
    summary = ", ".join(
        f"{counts_by_status.get(s, 0)} {s.value}" for s in Status if counts_by_status.get(s, 0)
    )
    score = result.summary.quality_score
    out.print(
        f"{_status_text(result.status)} {summary or 'no checks'}"
        + (f"; quality score {score:.1f}" if score is not None else "")
        + f"; {(result.duration_ms or 0) / 1000:.1f}s"
    )
    for artifact in artifacts:
        out.print(f"[dim]wrote[/] {escape(_display_file(artifact.path))}")


FailOnOption = Annotated[
    Severity,
    typer.Option("--fail-on", help="Exit 1 when a check at or above this severity fails."),
]
MinScoreOption = Annotated[
    float | None, typer.Option("--min-score", help="Exit 1 when the quality score is below this.")
]
FormatsOption = Annotated[
    list[str] | None,
    typer.Option("--format", "-f", help="Report format (json is always written). Repeatable."),
]
OutputDirOption = Annotated[
    Path | None,
    typer.Option("--output-dir", "-o", help="Write reports here instead of reports.output_dir."),
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="List every problem.")]


def _finish(result: RunResult, fail_on: Severity, strict: bool, min_score: float | None) -> None:
    code = api.exit_code(result, fail_on=fail_on, strict=strict, min_score=min_score)
    if code:
        raise typer.Exit(code)


def _execution_error(exc: DvfError) -> typer.Exit:
    err.print(f"[red]error[/] {escape(str(exc))}")
    hint = getattr(exc, "hint", None)
    if hint:
        err.print(f"    [dim]hint:[/] {escape(hint)}")
    return typer.Exit(ExitCode.EXECUTION_ERROR)


def _config_error(exc: ConfigError) -> typer.Exit:
    _print_diagnostics(exc.diagnostics)
    if not exc.diagnostics:
        err.print(f"[red]error[/] {escape(str(exc))}")
    return typer.Exit(ExitCode.CONFIG_ERROR)


@app.command("run")
def run_command(
    ctx: typer.Context,
    path: ConfigPath,
    profile: ProfileOption = None,
    set_: SetOption = None,
    var: VarOption = None,
    env_file: EnvFileOption = None,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help="Run a subset: suite:NAME, table:GLOB, check:TYPE, tag:TAG, severity:LEVEL. Repeatable.",
        ),
    ] = None,
    formats: FormatsOption = None,
    output_dir: OutputDirOption = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Use this run ID.")] = None,
    no_reports: Annotated[
        bool, typer.Option("--no-reports", help="Do not write report files.")
    ] = False,
    fail_on: FailOnOption = Severity.ERROR,
    min_score: MinScoreOption = None,
    strict: Annotated[
        bool, typer.Option("--strict", help="Warnings also fail the run (exit 1).")
    ] = False,
    verbose: VerboseOption = False,
) -> None:
    """Run the validations in a configuration and write reports.

    Exit codes: 0 passed, 1 validation failures, 2 execution errors, 3 configuration errors.
    """
    try:
        loaded = api.load_config(
            path,
            profile=profile,
            overrides=set_ or [],
            variables=_parse_vars(var),
            env_file=env_file,
            plugins=_state(ctx).plugins,
        )
        _print_diagnostics(loaded.warnings)
        output = api.run(
            loaded,
            only=only or [],
            run_id=run_id,
            output_dir=output_dir,
            formats=formats,
            write_reports=not no_reports,
        )
    except ConfigError as exc:
        raise _config_error(exc) from exc
    except DvfError as exc:
        raise _execution_error(exc) from exc
    _print_run(output.result, output.artifacts, verbose)
    _finish(output.result, fail_on, strict, min_score)


@app.command("compare")
def compare_command(
    source: Annotated[
        str,
        typer.Argument(
            help="A file path, or duckdb:///db::table, sqlite:///db::table, postgresql://...::schema.table."
        ),
    ],
    target: Annotated[str, typer.Argument(help="The target, in the same forms as the source.")],
    key: Annotated[
        list[str] | None,
        typer.Option("--key", "-k", help="Key column (repeat for composite keys)."),
    ] = None,
    strategy: Annotated[
        list[str] | None,
        typer.Option("--strategy", "-s", help="Comparison strategy. Repeatable."),
    ] = None,
    engine: Annotated[
        str, typer.Option("--engine", help="auto, pushdown, duckdb, or polars.")
    ] = "auto",
    formats: FormatsOption = None,
    output_dir: OutputDirOption = None,
    no_reports: Annotated[
        bool, typer.Option("--no-reports", help="Do not write report files.")
    ] = False,
    sample_size: Annotated[
        int, typer.Option("--sample-size", help="Sample rows per difference.")
    ] = 20,
    fail_on: FailOnOption = Severity.ERROR,
    verbose: VerboseOption = False,
) -> None:
    """Compare two datasets without a configuration file."""
    default = ["row_count", "aggregate", "full_diff"] if key else ["row_count", "aggregate"]
    try:
        output = api.compare(
            source,
            target,
            keys=key or [],
            strategies=strategy or default,
            engine=engine,
            output_dir=output_dir,
            formats=formats or ["json", "html"],
            write_reports=not no_reports,
            sample_size=sample_size,
        )
    except ConfigError as exc:
        raise _config_error(exc) from exc
    except DvfError as exc:
        raise _execution_error(exc) from exc
    _print_run(output.result, output.artifacts, verbose)
    _finish(output.result, fail_on, False, None)


@app.command("report")
def report_command(
    results: Annotated[Path, typer.Option("--from", help="A results.json written by dvf run.")],
    formats: Annotated[
        list[str], typer.Option("--format", "-f", help="Format to render. Repeatable.")
    ],
    output_dir: Annotated[
        Path, typer.Option("--output-dir", "-o", help="Where to write the reports.")
    ],
    title: Annotated[str | None, typer.Option("--title", help="Report title.")] = None,
) -> None:
    """Re-render reports from a results document, without touching any data."""
    from dvf.reporting.base import ReportOptions

    try:
        artifacts = api.render_reports(
            results, formats, output_dir, options=ReportOptions(title=title)
        )
    except (OSError, ValueError, DvfError) as exc:
        err.print(f"[red]error[/] {escape(str(exc))}")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    for artifact in artifacts:
        out.print(f"wrote {escape(_display_file(artifact.path))}")


@app.command("init")
def init_command(
    directory: Annotated[Path, typer.Argument(help="Project folder to create.")] = Path(),
    example: Annotated[
        bool, typer.Option("--example", help="Include synthetic data with seeded discrepancies.")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing dvf.yaml.")] = False,
) -> None:
    """Create a dvf project. With --example, a runnable local demo with seeded defects."""
    from dvf.scaffold.example import write_example, write_starter

    try:
        schema = api.config_json_schema()
        writer = write_example if example else write_starter
        files = writer(directory, force=force, schema=schema)
    except DvfError as exc:
        err.print(f"[red]error[/] {escape(str(exc))}")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
    for file in files:
        out.print(f"created {escape(_display_file(str(file)))}")
    config = _display_file(str(directory / "dvf.yaml"))
    if example:
        out.print(f"Next: [bold]dvf run {escape(config)}[/]")
    else:
        out.print(
            f"Next: edit {escape(config)}, then [bold]dvf validate-config {escape(config)}[/]"
        )


@connections_app.command("test")
def connections_test_command(
    ctx: typer.Context,
    path: ConfigPath,
    profile: ProfileOption = None,
    set_: SetOption = None,
    var: VarOption = None,
    env_file: EnvFileOption = None,
) -> None:
    """Open every connection in a configuration and run a trivial query."""
    try:
        loaded = api.load_config(
            path,
            profile=profile,
            overrides=set_ or [],
            variables=_parse_vars(var),
            env_file=env_file,
            plugins=_state(ctx).plugins,
        )
    except ConfigError as exc:
        raise _config_error(exc) from exc
    table = Table(title="Connections", title_justify="left")
    for column in ("Name", "Type", "Status", "Details", "Time (ms)"):
        table.add_column(column)
    failed = False
    for name, conn_config in loaded.config.connections.items():
        entry = loaded.registry.get(PluginKind.CONNECTOR, conn_config.type)
        if not entry.ready:
            table.add_row(
                escape(name),
                conn_config.type,
                "[dim]not available[/]",
                "this connector is not implemented in this version",
                "",
            )
            failed = True
            continue
        connector = entry.implementation(name, conn_config, loaded.base_dir)
        try:
            report = connector.test_connection()
        finally:
            connector.close()
        failed |= not report.ok
        details = report.message + (f" ({report.server_version})" if report.server_version else "")
        table.add_row(
            escape(name),
            conn_config.type,
            "[green]ok[/]" if report.ok else "[red]failed[/]",
            escape(global_redactor().redact_text(details)),
            f"{report.duration_ms:.0f}",
        )
    out.print(table)
    if failed:
        raise typer.Exit(ExitCode.EXECUTION_ERROR)


def _load_cli_plugins(ctx: typer.Context, registry: Any) -> None:
    specs = _state(ctx).plugins
    if not specs:
        return
    try:
        load_local_plugins(registry, specs)
    except DvfError as exc:
        err.print(f"[red]error[/] {escape(str(exc))}")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc


def main() -> None:
    """Console script entry point."""
    app()
