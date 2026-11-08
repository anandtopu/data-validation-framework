"""CLI commands for discovery and generation: discover, import-mapping, import-contract,
export-contract, profile, suggest.

Owned by the Phase 3 discovery work. :func:`register` is called by ``dvf.cli.app`` after the
application is defined; import helpers from ``dvf.cli.app`` inside it, not at module level.
Option types used in annotations live at module level, because Typer resolves annotations
against this module's globals.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer


class TextOrJson(StrEnum):
    TEXT = "text"
    JSON = "json"


class SideOption(StrEnum):
    AUTO = "auto"
    SOURCE = "source"
    TARGET = "target"
    BOTH = "both"


class BetweenOption(StrEnum):
    EXCLUSIVE = "exclusive"
    INCLUSIVE = "inclusive"


OutputFile = Annotated[
    Path | None,
    typer.Option(
        "--output", "-o", help="Write the generated file here (default: standard output)."
    ),
]
ForceOption = Annotated[bool, typer.Option("--force", help="Replace an existing output file.")]
SummaryFormat = Annotated[
    TextOrJson,
    typer.Option("--format", help="Summary format: text, or json (on standard output)."),
]
TablesOption = Annotated[
    list[str] | None,
    typer.Option("--table", "-t", help="Only tables matching this glob. Repeatable."),
]
SuiteOption = Annotated[str | None, typer.Option("--suite", help="Only tables of this suite.")]
EnvProfileOption = Annotated[
    str | None, typer.Option("--profile", "-p", help="Environment profile of the configuration.")
]
SourceOption = Annotated[
    str | None,
    typer.Option(
        "--source",
        help="With a configuration: the source connection's name. Without: a location "
        "(postgresql://..., duckdb:///file, sqlite:///file, or a folder of files).",
    ),
]
TargetOption = Annotated[
    str | None,
    typer.Option("--target", help="The target connection's name, or a location (as --source)."),
]
ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Copy connections from this configuration file."),
]
SuiteNameOption = Annotated[
    str | None, typer.Option("--suite-name", help="Name of the generated suite.")
]
ProjectNameOption = Annotated[
    str | None, typer.Option("--project-name", help="Project name of the generated configuration.")
]
SideChoiceOption = Annotated[
    SideOption,
    typer.Option(
        "--side",
        help="auto (target of comparison suites, source otherwise), source, target, or both.",
    ),
]


def _emit(text: str, output: Path | None) -> None:
    """Generated text to standard output when no output file was written."""
    if output is None:
        sys.stdout.write(text)


def register(app: typer.Typer) -> None:
    """Add this module's commands to ``app``."""
    from rich.markup import escape
    from rich.table import Table

    from dvf import api
    from dvf.cli.app import ExitCode, _config_error, _execution_error, _state, err, out
    from dvf.core.errors import ConfigError, DvfError

    def fail(exc: DvfError) -> typer.Exit:
        if isinstance(exc, ConfigError):
            return _config_error(exc)
        return _execution_error(exc)

    def notes(lines: list[str]) -> None:
        for line in lines:
            err.print(f"[dim]note:[/] {escape(line)}")

    def wrote(output: Path | None) -> None:
        if output is not None:
            err.print(f"[green]wrote[/] {escape(str(output))}")

    def load(ctx: typer.Context, path: Path, *, connect: bool, profile: str | None = None) -> Any:
        loaded = api.load_config(
            path,
            profile=profile,
            strict=connect,
            resolve_secrets=connect,
            plugins=_state(ctx).plugins,
        )
        return loaded

    # ------------------------------------------------------------------ discover
    @app.command("discover")
    def discover_command(
        ctx: typer.Context,
        config: Annotated[
            Path | None,
            typer.Argument(help="Configuration holding the two connections (optional)."),
        ] = None,
        source: SourceOption = None,
        target: TargetOption = None,
        source_schema: Annotated[
            str | None, typer.Option("--source-schema", help="Only tables in this source schema.")
        ] = None,
        target_schema: Annotated[
            str | None, typer.Option("--target-schema", help="Only tables in this target schema.")
        ] = None,
        source_pattern: Annotated[
            str, typer.Option("--source-pattern", help="Glob over source table names or files.")
        ] = "*",
        target_pattern: Annotated[
            str, typer.Option("--target-pattern", help="Glob over target table names or files.")
        ] = "*",
        threshold: Annotated[
            float,
            typer.Option(
                "--threshold", min=0.01, max=1.0, help="Lowest fuzzy match score accepted (0-1)."
            ),
        ] = 0.8,
        no_fuzzy: Annotated[
            bool,
            typer.Option("--no-fuzzy", help="Match names exactly and case-insensitively only."),
        ] = False,
        probe_keys: Annotated[
            bool,
            typer.Option(
                "--probe-keys",
                help="Infer keys of tables without a primary key by testing candidate columns "
                "for uniqueness in the source (one query per such table).",
            ),
        ] = False,
        suite_name: Annotated[
            str, typer.Option("--suite-name", help="Name of the generated suite.")
        ] = "discovered",
        project_name: Annotated[
            str, typer.Option("--project-name", help="Project name of the generated configuration.")
        ] = "discovered",
        profile: EnvProfileOption = None,
        output: OutputFile = None,
        force: ForceOption = False,
        output_format: SummaryFormat = TextOrJson.TEXT,
    ) -> None:
        """Introspect a source and a target, match tables and columns, and generate a configuration.

        Names are matched exactly, then case-insensitively, then fuzzily. Primary keys come from
        the catalogs. Review the comments marked REVIEW in the generated file.
        """
        try:
            loaded = load(ctx, config, connect=True, profile=profile) if config else None
            result = api.discover(
                loaded,
                source=source,
                target=target,
                source_schema=source_schema,
                target_schema=target_schema,
                source_pattern=source_pattern,
                target_pattern=target_pattern,
                threshold=threshold,
                fuzzy=not no_fuzzy,
                probe_keys=probe_keys,
                suite_name=suite_name,
                project_name=project_name,
                output=output,
                overwrite=force,
            )
        except DvfError as exc:
            raise fail(exc) from exc
        if output_format is TextOrJson.JSON:
            typer.echo(json.dumps(result.to_dict(), indent=2, default=str))
            if output is None:
                err.print("[dim]Pass --output to write the configuration with --format json.[/]")
            return
        _emit(result.text, output)
        fuzzy = sum(1 for t in result.tables if t.method == "fuzzy")
        risks = sum(len(t.type_risks) for t in result.tables)
        keyless = sum(1 for t in result.tables if not t.keys)
        err.print(
            f"matched {len(result.tables)} table(s) ({fuzzy} fuzzy); "
            f"{len(result.unmatched_source)} source and {len(result.unmatched_target)} target "
            f"table(s) unmatched; {risks} type risk(s); {keyless} table(s) without keys"
        )
        notes(result.notes)
        wrote(output)

    # ------------------------------------------------------------------ import-mapping
    @app.command("import-mapping")
    def import_mapping_command(
        ctx: typer.Context,
        mapping: Annotated[Path, typer.Argument(help="Mapping sheet: .xlsx, .xlsm, or .csv.")],
        sheet: Annotated[
            str | None,
            typer.Option("--sheet", help="Excel sheet (default: the first with a header row)."),
        ] = None,
        config: ConfigOption = None,
        source: SourceOption = None,
        target: TargetOption = None,
        suite_name: SuiteNameOption = None,
        project_name: ProjectNameOption = None,
        include_unlisted: Annotated[
            bool,
            typer.Option(
                "--include-unlisted",
                help="Also compare columns the sheet does not list (default: only listed ones).",
            ),
        ] = False,
        output: OutputFile = None,
        force: ForceOption = False,
        output_format: SummaryFormat = TextOrJson.TEXT,
    ) -> None:
        """Convert a source-to-target mapping sheet into a configuration.

        Every problem is reported with its row; nothing is written until the sheet is valid.
        """
        try:
            loaded = load(ctx, config, connect=False) if config else None
            result = api.import_mapping(
                mapping,
                sheet=sheet,
                config=loaded,
                source=source,
                target=target,
                suite_name=suite_name,
                project_name=project_name,
                include_unlisted=include_unlisted,
                output=output,
                overwrite=force,
            )
        except DvfError as exc:
            raise fail(exc) from exc
        if output_format is TextOrJson.JSON:
            typer.echo(json.dumps(result.to_dict(), indent=2))
            return
        _emit(result.text, output)
        err.print(f"converted {result.columns} mapping row(s) into {result.tables} table(s)")
        notes(result.notes)
        wrote(output)

    # ------------------------------------------------------------------ contracts
    @app.command("import-contract")
    def import_contract_command(
        ctx: typer.Context,
        contract: Annotated[Path, typer.Argument(help="ODCS v3 contract (YAML or JSON).")],
        server: Annotated[
            str | None,
            typer.Option(
                "--server", help="Server of the contract to connect to (default: the first)."
            ),
        ] = None,
        config: ConfigOption = None,
        connection: Annotated[
            str | None, typer.Option("--connection", help="With --config: the connection to use.")
        ] = None,
        source: Annotated[
            str | None,
            typer.Option("--source", help="A location instead of the contract's servers."),
        ] = None,
        suite_name: SuiteNameOption = None,
        project_name: ProjectNameOption = None,
        no_conformance: Annotated[
            bool, typer.Option("--no-conformance", help="Leave out the SCH-12 'contract' checks.")
        ] = False,
        between: Annotated[
            BetweenOption,
            typer.Option("--between", help="How to read mustBeBetween bounds."),
        ] = BetweenOption.EXCLUSIVE,
        output: OutputFile = None,
        force: ForceOption = False,
        output_format: SummaryFormat = TextOrJson.TEXT,
    ) -> None:
        """Convert an ODCS v3 data contract into schema and quality checks.

        Rules dvf cannot run are listed with the reason, never dropped silently.
        """
        try:
            loaded = load(ctx, config, connect=False) if config else None
            result = api.import_contract(
                contract,
                server=server,
                config=loaded,
                connection=connection,
                location=source,
                suite_name=suite_name,
                project_name=project_name,
                conformance=not no_conformance,
                between=between.value,
                output=output,
                overwrite=force,
            )
        except DvfError as exc:
            raise fail(exc) from exc
        if output_format is TextOrJson.JSON:
            typer.echo(json.dumps(result.to_dict(), indent=2))
            return
        _emit(result.text, output)
        err.print(
            f"converted {result.objects} schema object(s) into {result.checks} check(s); "
            f"{len(result.unsupported)} element(s) not converted"
        )
        for item in result.unsupported:
            err.print(f"[yellow]not converted[/] {escape(str(item))}")
        notes(result.notes)
        wrote(output)

    @app.command("export-contract")
    def export_contract_command(
        ctx: typer.Context,
        config: Annotated[Path, typer.Argument(help="Configuration whose datasets to describe.")],
        tables: TablesOption = None,
        suite: SuiteOption = None,
        side: Annotated[
            SideOption,
            typer.Option("--side", help="auto (target of comparison suites), source, or target."),
        ] = SideOption.AUTO,
        contract_id: Annotated[str | None, typer.Option("--id", help="Contract id.")] = None,
        version: Annotated[str, typer.Option("--version", help="Contract version.")] = "1.0.0",
        status: Annotated[str, typer.Option("--status", help="Contract status.")] = "draft",
        profile: EnvProfileOption = None,
        output: OutputFile = None,
        force: ForceOption = False,
        output_format: SummaryFormat = TextOrJson.TEXT,
    ) -> None:
        """Write an ODCS v3.1 data contract from a configuration's datasets and checks."""
        try:
            loaded = load(ctx, config, connect=True, profile=profile)
            result = api.export_contract(
                loaded,
                tables=tables or [],
                suite=suite,
                side=side.value,
                contract_id=contract_id,
                version=version,
                status=status,
                output=output,
                overwrite=force,
            )
        except DvfError as exc:
            raise fail(exc) from exc
        if output_format is TextOrJson.JSON:
            typer.echo(json.dumps(result.to_dict(), indent=2))
            return
        _emit(result.text, output)
        err.print(
            f"described {result.objects} dataset(s); {len(result.unsupported)} check(s) not exported"
        )
        for item in result.unsupported:
            err.print(f"[yellow]not exported[/] {escape(str(item))}")
        wrote(output)

    # ------------------------------------------------------------------ profile and suggest
    @app.command("profile")
    def profile_command(
        ctx: typer.Context,
        config: Annotated[Path, typer.Argument(help="Configuration whose datasets to profile.")],
        tables: TablesOption = None,
        suite: SuiteOption = None,
        side: SideChoiceOption = SideOption.AUTO,
        top: Annotated[
            int, typer.Option("--top", min=0, help="Most frequent values to show (0: none).")
        ] = 5,
        no_formats: Annotated[
            bool,
            typer.Option("--no-formats", help="Skip value format detection (one query per table)."),
        ] = False,
        profile: EnvProfileOption = None,
        output_format: Annotated[
            TextOrJson, typer.Option("--format", help="text or json.")
        ] = TextOrJson.TEXT,
        output: Annotated[
            Path | None, typer.Option("--output", "-o", help="Write the JSON profile to this file.")
        ] = None,
    ) -> None:
        """Profile columns (NULLs, distinct values, min, max, lengths, top values) inside each
        database. Exit code 2 when a dataset could not be profiled."""
        try:
            loaded = load(ctx, config, connect=True, profile=profile)
            report = api.profile(
                loaded,
                tables=tables or [],
                suite=suite,
                side=side.value,
                top=top,
                formats=not no_formats,
            )
        except DvfError as exc:
            raise fail(exc) from exc
        document = json.dumps(report.to_dict(), indent=2, default=str, ensure_ascii=False) + "\n"
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(document, encoding="utf-8", newline="\n")
            wrote(output)
        if output_format is TextOrJson.JSON:
            if output is None:
                sys.stdout.write(document)
        else:
            for table in report.tables:
                title = f"{table.suite}.{table.table} ({table.side}: {table.dataset or table.connection})"
                if table.error:
                    err.print(f"[red]error[/] {escape(title)}: {escape(table.error)}")
                    continue
                grid = Table(
                    title=f"{escape(title)}: {table.row_count:,} rows", title_justify="left"
                )
                for name in (
                    "Column",
                    "Type",
                    "Nulls %",
                    "Distinct",
                    "Min",
                    "Max",
                    "Length",
                    "Top values",
                ):
                    grid.add_column(name, overflow="fold")
                for col in table.columns:
                    length = (
                        f"{col.min_length}-{col.max_length} (avg {col.avg_length:.1f})"
                        if col.max_length is not None and col.avg_length is not None
                        else ""
                    )
                    top_values = ", ".join(f"{v} ({n})" for v, n in col.top_values[:top])
                    fmt = col.format_of_all()
                    grid.add_row(
                        escape(col.name),
                        escape(col.native_type),
                        "" if col.null_pct is None else f"{col.null_pct:.2f}",
                        "" if col.distinct_count is None else f"{col.distinct_count:,}",
                        escape("" if col.min is None else str(col.min)),
                        escape("" if col.max is None else str(col.max)),
                        escape(length + (f" {fmt}" if fmt else "")),
                        escape(top_values),
                    )
                out.print(grid)
                if table.skipped_columns:
                    out.print(f"[dim]not profiled: {escape(', '.join(table.skipped_columns))}[/]")
        if report.errors:
            raise typer.Exit(ExitCode.EXECUTION_ERROR)

    @app.command("suggest")
    def suggest_command(
        ctx: typer.Context,
        config: Annotated[
            Path | None, typer.Argument(help="Configuration to profile (or use --from-profile).")
        ] = None,
        from_profile: Annotated[
            Path | None,
            typer.Option(
                "--from-profile", help="A JSON profile written by 'dvf profile --format json'."
            ),
        ] = None,
        tables: TablesOption = None,
        suite: SuiteOption = None,
        side: SideChoiceOption = SideOption.AUTO,
        margin: Annotated[
            float,
            typer.Option(
                "--margin",
                min=0.0,
                help="Widen ranges and lengths by this share of the observed span.",
            ),
        ] = 0.0,
        max_values: Annotated[
            int,
            typer.Option(
                "--max-values", min=1, help="Most distinct values for an allowed_values check."
            ),
        ] = 10,
        no_freshness: Annotated[
            bool, typer.Option("--no-freshness", help="Do not suggest freshness checks.")
        ] = False,
        profile: EnvProfileOption = None,
        output: OutputFile = None,
        force: ForceOption = False,
        output_format: SummaryFormat = TextOrJson.TEXT,
    ) -> None:
        """Suggest candidate checks from column profiles, as a YAML fragment with evidence."""
        if config is None and from_profile is None:
            err.print("[red]error[/] give a configuration or --from-profile FILE")
            raise typer.Exit(ExitCode.CONFIG_ERROR)
        try:
            loaded = (
                load(ctx, config, connect=True, profile=profile)
                if config is not None and from_profile is None
                else None
            )
            result = api.suggest(
                loaded,
                profile_report=from_profile,
                tables=tables or [],
                suite=suite,
                side=side.value,
                margin=margin,
                max_values=max_values,
                freshness=not no_freshness,
                output=output,
                overwrite=force,
            )
        except DvfError as exc:
            raise fail(exc) from exc
        if output_format is TextOrJson.JSON:
            typer.echo(json.dumps(result.to_dict(), indent=2, default=str))
            return
        _emit(result.text, output)
        err.print(f"suggested {len(result.suggestions)} check(s)")
        for item in result.skipped:
            err.print(f"[yellow]not profiled[/] {escape(item)}")
        wrote(output)
        if result.skipped:
            raise typer.Exit(ExitCode.EXECUTION_ERROR)
