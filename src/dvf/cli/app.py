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
from dvf.core.errors import ConfigError, DvfError
from dvf.models.results import RESULTS_SCHEMA_VERSION
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
app.add_typer(config_app, name="config")
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
