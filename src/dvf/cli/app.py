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
config_app = typer.Typer(help="Inspect and upgrade configuration.", no_args_is_help=True)
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
    """List components: 'ready' ones are implemented and verified, 'beta' ones are implemented but not
    yet verified against a live system, and 'declared' ones arrive in a later phase."""
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
    styles = {"ready": "[green]ready[/]", "beta": "[yellow]beta[/]", "declared": "[dim]declared[/]"}
    for entry in entries:
        table.add_row(
            entry.kind.value,
            entry.name,
            styles[entry.status],
            escape(entry.source),
            escape(entry.description),
        )
    out.print(table)
    counts = {status: sum(1 for e in entries if e.status == status) for status in styles}
    out.print(
        f"{len(entries)} components: {counts['ready']} ready, {counts['beta']} beta "
        f"(implemented, not yet verified against a live system), {counts['declared']} declared."
    )
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
    if result.baseline is not None:
        b = result.baseline
        out.print(
            f"[dim]vs run {escape(b.baseline_run_id)} ({escape(b.policy)}):[/] "
            f"{len(b.new_failures)} new, {len(b.resolved)} resolved, "
            f"{len(b.persisting)} persisting failures"
        )
    expired = [w for w in result.waivers if w.status == "expired"]
    applied = [w for w in result.waivers if w.status == "applied"]
    if applied or expired:
        out.print(
            f"[dim]waivers:[/] {len(applied)} applied"
            + (f", [red]{len(expired)} expired[/]" if expired else "")
        )
    if result.interrupted:
        err.print(
            f"[yellow]interrupted[/]: resume with 'dvf run CONFIG --resume {escape(result.run_id)}'"
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


def _print_notifications(output: Any) -> None:
    """Successful notifications (failed ones are already shown as warnings)."""
    for delivery in getattr(output, "notifications", None) or []:
        if delivery.ok:
            out.print(f"[dim]notified[/] {escape(delivery.channel)} ({escape(delivery.type)})")


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
    if exc.hint:
        err.print(f"[dim]hint[/] {escape(exc.hint)}")
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
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume", help="Continue an interrupted or killed run from its checkpoints."
        ),
    ] = None,
    force_resume: Annotated[
        bool, typer.Option("--force-resume", help="Resume even if the configuration changed.")
    ] = False,
    failed_from: Annotated[
        str | None,
        typer.Option(
            "--failed-from", help="Run only the checks that failed or errored in this run."
        ),
    ] = None,
    parallel: Annotated[
        int | None,
        typer.Option("--parallel", "-j", min=1, help="Tables to run at the same time."),
    ] = None,
    keep_temp: Annotated[
        bool, typer.Option("--keep-temp", help="Keep the local engine's temporary files.")
    ] = False,
    no_history: Annotated[
        bool, typer.Option("--no-history", help="Do not read or record run history.")
    ] = False,
    baseline: Annotated[
        str | None,
        typer.Option("--baseline", help="Compare with: last_run, last_passed, none, or a run ID."),
    ] = None,
) -> None:
    """Run the validations in a configuration and write reports.

    Exit codes: 0 passed, 1 validation failures, 2 execution errors (or an interrupted run),
    3 configuration errors.
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
            resume=resume,
            force_resume=force_resume,
            failed_from=failed_from,
            parallel=parallel,
            keep_temp=keep_temp,
            history=not no_history,
            baseline=baseline,
        )
    except ConfigError as exc:
        raise _config_error(exc) from exc
    except DvfError as exc:
        raise _execution_error(exc) from exc
    _print_run(output.result, output.artifacts, verbose)
    _print_notifications(output)
    _finish(output.result, fail_on, strict, min_score)


@app.command("rerun")
def rerun_command(
    ctx: typer.Context,
    run_id: Annotated[
        str, typer.Argument(help="The run whose failed and errored checks to rerun.")
    ],
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Configuration file (default: the one the run used)."),
    ] = None,
    output_dir: OutputDirOption = None,
    no_reports: Annotated[
        bool, typer.Option("--no-reports", help="Do not write report files.")
    ] = False,
    fail_on: FailOnOption = Severity.ERROR,
    verbose: VerboseOption = False,
) -> None:
    """Run again only the checks that failed or errored in an earlier run."""
    try:
        if config is None:
            handle = api.load_run(run_id)
            if not handle.config_files:
                raise ConfigError(
                    f"Run {run_id} does not record its configuration file.",
                    hint="Pass --config PATH.",
                )
            config = Path(handle.config_files[0])
        loaded = api.load_config(config, plugins=_state(ctx).plugins)
        output = api.run(
            loaded, failed_from=run_id, output_dir=output_dir, write_reports=not no_reports
        )
    except ConfigError as exc:
        raise _config_error(exc) from exc
    except DvfError as exc:
        raise _execution_error(exc) from exc
    _print_run(output.result, output.artifacts, verbose)
    _print_notifications(output)
    _finish(output.result, fail_on, False, None)


@app.command("explain")
def explain_command(
    ctx: typer.Context,
    path: ConfigPath,
    table: Annotated[str | None, typer.Option("--table", "-t", help="Only this table.")] = None,
    suite: Annotated[str | None, typer.Option("--suite", help="Only tables of this suite.")] = None,
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Selectors, as for dvf run. Repeatable.")
    ] = None,
    profile: ProfileOption = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="text or json.")
    ] = OutputFormat.TEXT,
    no_sql: Annotated[bool, typer.Option("--no-sql", help="Leave out the generated SQL.")] = False,
) -> None:
    """Show the plan and the SQL each step would run, without running validations."""
    from rich.syntax import Syntax

    try:
        loaded = api.load_config(path, profile=profile, plugins=_state(ctx).plugins)
        report = api.explain(loaded, table=table, suite=suite, only=only or [])
    except ConfigError as exc:
        raise _config_error(exc) from exc
    except DvfError as exc:
        raise _execution_error(exc) from exc
    if output_format is OutputFormat.JSON:
        out.print_json(json.dumps(report.to_dict(), default=str))
        return
    caps = ", ".join(f"{k} {v}" for k, v in report.connection_caps.items())
    out.print(
        f"[bold]{escape(report.project)}[/]: up to {report.parallel_tables} tables at a time; "
        f"connection caps: {escape(caps) or 'none'}"
    )
    for item in report.tables:
        out.rule(f"{escape(item.suite)}.{escape(item.table)}")
        out.print(f"source: {escape(item.source or '-')}   target: {escape(item.target or '-')}")
        if item.strategies:
            escalation = f" -> {item.on_mismatch} on mismatch" if item.on_mismatch else ""
            out.print(
                f"strategies: {escape(', '.join(item.strategies))}{escape(escalation)}; "
                f"engine {escape(item.engine)}"
                + (f" (row-level: {escape(item.row_engine)})" if item.row_engine else "")
            )
        for line in item.keys:
            out.print(f"  key  {escape(line)}")
        for line in item.columns:
            out.print(f"  col  {escape(line)}")
        if item.partitions:
            out.print(
                f"partitions ({len(item.partitions)}): "
                + escape(", ".join(p["name"] for p in item.partitions[:20]))
            )
        for problem in item.problems:
            out.print(f"[yellow]problem[/] {escape(problem)}")
        if not no_sql:
            for query in item.queries:
                out.print(f"[dim]-- {escape(query.purpose)} ({query.side}, {query.dialect})[/]")
                out.print(Syntax(query.sql, "sql", word_wrap=True))
        for check in item.checks:
            out.print(f"check [bold]{escape(check.check_id)}[/] ({check.side}, {check.severity})")
            if check.sql and not no_sql:
                out.print(Syntax(check.sql, "sql", word_wrap=True))


@app.command("trace")
def trace_command(
    ctx: typer.Context,
    path: ConfigPath,
    table: Annotated[str, typer.Option("--table", "-t", help="Table name.")],
    key: Annotated[
        list[str], typer.Option("--key", "-k", help="Key value (repeat for composite keys).")
    ],
    suite: Annotated[str | None, typer.Option("--suite", help="Suite of the table.")] = None,
    profile: ProfileOption = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="text or json.")
    ] = OutputFormat.TEXT,
) -> None:
    """Follow one record through both sides: raw value, each normalizer, canonical value, hash."""
    try:
        loaded = api.load_config(path, profile=profile, plugins=_state(ctx).plugins)
        report = api.trace(loaded, table=table, keys=key, suite=suite)
    except ConfigError as exc:
        raise _config_error(exc) from exc
    except DvfError as exc:
        raise _execution_error(exc) from exc
    if output_format is OutputFormat.JSON:
        out.print_json(json.dumps(report.to_dict(), default=str))
        return
    out.print(
        f"[bold]{escape(report.suite)}.{escape(report.table)}[/] key "
        f"{escape(', '.join(report.key))} (canonical {escape(', '.join(str(k) for k in report.canonical_key))}): "
        f"{report.source_rows} source row(s), {report.target_rows} target row(s)"
    )
    grid = Table(show_lines=False)
    for name in ("Column", "Step", "Source", "Target", ""):
        grid.add_column(name)
    for column in report.columns:
        s_row = column.source[0] if column.source else []
        t_row = column.target[0] if column.target else []
        mark = (
            "" if column.equal is None else ("[green]=[/]" if column.equal else "[red]differs[/]")
        )
        width = max(len(s_row), len(t_row))
        for i in range(width):
            step = column.steps[i] if i < len(column.steps) else ""
            grid.add_row(
                escape(column.column) if i == 0 else "",
                escape(step),
                escape(repr(s_row[i])) if i < len(s_row) else "",
                escape(repr(t_row[i])) if i < len(t_row) else "",
                mark if i == width - 1 else "",
            )
    out.print(grid)
    out.print(
        f"row hash: source {escape(str(report.source_hashes[:1]))}, "
        f"target {escape(str(report.target_hashes[:1]))}"
    )
    for note in report.notes:
        out.print(f"[yellow]note[/] {escape(note)}")


@app.command("debug")
def debug_command(
    run_id: Annotated[str, typer.Argument(help="Run ID, or the path of a results.json.")],
    check_id: Annotated[
        str | None, typer.Argument(help="Check ID (or its end); omit to list failed checks.")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Configuration (to find its .dvf folder).")
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="text or json.")
    ] = OutputFormat.TEXT,
) -> None:
    """Drill into a check of an earlier run: evidence, mismatches, SQL, timing, and repro files."""
    from rich.syntax import Syntax

    try:
        handle = api.load_run(run_id, config=config)
        check = handle.check(check_id) if check_id else None
    except ConfigError as exc:
        raise _config_error(exc) from exc
    if check is None:
        failed = handle.failed()
        if output_format is OutputFormat.JSON:
            out.print_json(json.dumps([c.check_id for c in failed]))
            return
        out.print(f"run {escape(handle.result.run_id)}: {len(failed)} failed or errored check(s)")
        for item in failed:
            out.print(
                f"{_status_text(item.status)} {escape(item.check_id)}: {escape(item.message or '')}"
            )
        return
    result = check.result
    if output_format is OutputFormat.JSON:
        out.print_json(result.model_dump_json())
        return
    out.rule(escape(result.check_id))
    out.print(f"{_status_text(result.status)} {escape(result.message or '')}")
    fields = {
        "type": result.type,
        "severity": result.severity.value,
        "engine": result.engine,
        "observed": result.observed,
        "expected": result.expected,
        "unexpected": result.unexpected_count,
        "total": result.total_count,
        "duration ms": result.duration_ms,
    }
    for name, value in fields.items():
        if value is not None:
            out.print(f"  {name}: {escape(str(value))}")
    if result.waiver is not None:
        w = result.waiver
        out.print(
            f"  waiver: {escape(w.selector)} ({'expired' if w.expired else 'active'}): {escape(w.reason)}"
        )
    for hint in result.hints:
        out.print(f"  [dim]hint:[/] {escape(hint)}")
    for line in result.normalization:
        out.print(f"  [dim]normalization:[/] {escape(line)}")
    for mismatch in result.column_mismatches:
        classes = ", ".join(f"{k} {v}" for k, v in mismatch.classifications.items())
        out.print(
            f"  column {escape(mismatch.column)}: {mismatch.mismatch_count:,} ({escape(classes)})"
        )
    if result.samples is not None:
        grid = Table(*[escape(c) for c in result.samples.columns], title="samples")
        for row in result.samples.rows[:20]:
            grid.add_row(*[escape(str(v)) for v in row])
        out.print(grid)
    if result.error is not None:
        out.print(f"[red]{escape(result.error.type)}[/]: {escape(result.error.message)}")
        if result.error.traceback:
            out.print(f"[dim]{escape(result.error.traceback)}[/]")
    for query in result.queries:
        out.print(
            f"[dim]-- {query.side.value} ({query.dialect}, {query.duration_ms} ms, {query.rows_returned} rows)[/]"
        )
        out.print(Syntax(query.sql, "sql", word_wrap=True))
    for artifact in result.artifacts:
        base = handle.output_dir
        location = (base / artifact.path) if base else Path(artifact.path)
        out.print(f"  {escape(artifact.kind)}: {escape(str(location))}")


@app.command("history")
def history_command(
    path: ConfigPath,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Runs to show.")] = 20,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="text or json.")
    ] = OutputFormat.TEXT,
) -> None:
    """List recent runs of the configuration's project."""
    try:
        runs = api.run_history(path, limit=limit)
    except ConfigError as exc:
        raise _config_error(exc) from exc
    except DvfError as exc:
        raise _execution_error(exc) from exc
    if output_format is OutputFormat.JSON:
        out.print_json(json.dumps(runs, default=str))
        return
    if not runs:
        out.print("no runs recorded yet")
        return
    grid = Table("Run ID", "Started", "Status", "Score", "Checks", "Failed", "Errors")
    for row in runs:
        grid.add_row(
            escape(row["run_id"]),
            escape(str(row["started_at"])[:19]),
            _status_text(Status(row["status"])),
            "" if row["quality_score"] is None else f"{row['quality_score']:.1f}",
            str(row["checks"]),
            str(row["failed"]),
            str(row["errors"]),
        )
    out.print(grid)


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
    _print_notifications(output)
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


# Commands added by later phases live in their own modules; each module's register(app) adds its
# commands. They import helpers from this module inside register(), after it is fully loaded.
_COMMAND_MODULES = (
    "dvf.cli.discovery",
    "dvf.cli.migration",
    "dvf.cli.packs",
    "dvf.cli.tools",
    "dvf.cli.integrations",
    "dvf.cli.notify",
)


def _register_command_modules() -> None:
    import importlib

    for module_name in _COMMAND_MODULES:
        importlib.import_module(module_name).register(app)


_register_command_modules()


def main() -> None:
    """Console script entry point."""
    app()
