"""CLI commands for CI/CD and orchestrator integrations: ``dvf dbt import``.

Owned by the Phase 4 CI/CD and orchestrator work. :func:`register` is called by
``dvf.cli.app`` after the application is defined; import helpers from ``dvf.cli.app`` inside
it, not at module level. Option types used in annotations live at module level, because Typer
resolves annotations against this module's globals.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer


class SummaryFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


ManifestArgument = Annotated[
    Path, typer.Argument(help="dbt manifest.json (in the dbt project's target folder).")
]
ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Copy the connection from this configuration file."),
]
ConnectionOption = Annotated[
    str | None,
    typer.Option("--connection", help="With --config: the connection to copy (if it has several)."),
]
LocationOption = Annotated[
    str | None,
    typer.Option(
        "--location",
        help="The warehouse as a location (postgresql://..., duckdb:///file) instead of a "
        "connection derived from the manifest's adapter.",
    ),
]
ModelOption = Annotated[
    list[str] | None,
    typer.Option("--model", "-m", help="Only models whose name matches this glob. Repeatable."),
]
TagOption = Annotated[
    list[str] | None, typer.Option("--tag", help="Only models with this dbt tag. Repeatable.")
]
ExcludeOption = Annotated[
    list[str] | None,
    typer.Option("--exclude", help="Leave out models whose name matches this glob. Repeatable."),
]
AllPackagesOption = Annotated[
    bool, typer.Option("--all-packages", help="Include models of installed dbt packages.")
]
NoTestsOption = Annotated[
    bool, typer.Option("--no-tests", help="Do not convert dbt tests into checks.")
]
NoRowCountOption = Annotated[
    bool, typer.Option("--no-row-count", help="Do not add a row_count_range (min 1) check.")
]
SuiteNameOption = Annotated[
    str | None, typer.Option("--suite-name", help="Name of the generated suite (dbt_models).")
]
ProjectNameOption = Annotated[
    str | None,
    typer.Option("--project-name", help="Project name (default: the dbt project's name)."),
]
OutputOption = Annotated[
    Path | None,
    typer.Option("--output", "-o", help="Write the configuration here (default: standard output)."),
]
ForceOption = Annotated[bool, typer.Option("--force", help="Replace an existing output file.")]
FormatOption = Annotated[
    SummaryFormat,
    typer.Option("--format", help="Summary format: text, or json (on standard output)."),
]


def register(app: typer.Typer) -> None:
    """Add this module's commands to ``app``."""
    from rich.markup import escape

    from dvf import api
    from dvf.cli.app import _config_error, _execution_error, _state, err
    from dvf.core.errors import ConfigError, DvfError

    dbt_app = typer.Typer(help="Work with dbt projects.", no_args_is_help=True)
    app.add_typer(dbt_app, name="dbt")

    @dbt_app.command("import")
    def dbt_import_command(
        ctx: typer.Context,
        manifest: ManifestArgument,
        config: ConfigOption = None,
        connection: ConnectionOption = None,
        location: LocationOption = None,
        model: ModelOption = None,
        tag: TagOption = None,
        exclude: ExcludeOption = None,
        all_packages: AllPackagesOption = False,
        no_tests: NoTestsOption = False,
        no_row_count: NoRowCountOption = False,
        suite_name: SuiteNameOption = None,
        project_name: ProjectNameOption = None,
        output: OutputOption = None,
        force: ForceOption = False,
        output_format: FormatOption = SummaryFormat.TEXT,
    ) -> None:
        """Generate a configuration with one table per dbt model, from a dbt manifest.

        dbt generic tests (not_null, unique, accepted_values, relationships, and common
        dbt_utils and dbt_expectations tests) become checks; tests without an equivalent are
        listed at the end of the file. Run it after 'dbt build' or 'dbt compile'.
        """
        from dvf.integrations.dbt import import_manifest

        try:
            loaded = None
            if config is not None:
                loaded = api.load_config(
                    config, strict=False, resolve_secrets=False, plugins=_state(ctx).plugins
                )
            if location is not None and loaded is not None:
                raise ConfigError("Give --config or --location, not both.")
            result = import_manifest(
                manifest,
                loaded=loaded,
                config_path=config,
                connection=connection,
                location=location,
                models=model or [],
                tags=tag or [],
                exclude=exclude or [],
                all_packages=all_packages,
                tests=not no_tests,
                row_count=not no_row_count,
                suite_name=suite_name,
                project_name=project_name,
                output=output,
                overwrite=force,
            )
        except ConfigError as exc:
            raise _config_error(exc) from exc
        except DvfError as exc:
            raise _execution_error(exc) from exc
        if output_format is SummaryFormat.JSON:
            typer.echo(json.dumps(result.to_dict(), indent=2))
            return
        if output is None:
            sys.stdout.write(result.text)
        err.print(
            f"converted {result.models} dbt model(s) into {result.checks} check(s); "
            f"{len(result.unsupported)} dbt test(s) not converted"
        )
        for note in result.notes:
            err.print(f"[dim]note:[/] {escape(note)}")
        if output is not None:
            err.print(f"[green]wrote[/] {escape(str(output))}")
            err.print(f"Next: [bold]dvf validate-config {escape(str(output))}[/]")
