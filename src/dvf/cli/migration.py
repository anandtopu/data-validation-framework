"""CLI commands for migration validation: baseline capture and run-to-run comparison.

``dvf baseline capture|list`` (MIG-14) and ``dvf history compare RUN_A RUN_B`` (MIG-08).
:func:`register` is called by ``dvf.cli.app`` after the application is defined; it imports
helpers from ``dvf.cli.app`` inside it, not at module level.

``dvf history`` was a single command (``dvf history CONFIG``). To add ``compare`` without
breaking it, :func:`register` turns it into a group whose default subcommand is ``list``:
``dvf history CONFIG`` keeps working and means ``dvf history list CONFIG``.
"""

# No ``from __future__ import annotations``: Typer reads the parameter annotations of the
# commands defined inside register(), which refer to names imported there.

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup


class _DefaultListGroup(TyperGroup):
    """A group that runs ``list`` when the first argument is not one of its subcommands."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in ("--help", "-h"):
            args = ["list", *args]
        return super().parse_args(ctx, args)


def register(app: typer.Typer) -> None:
    """Add this module's commands to ``app``."""
    _register_baseline(app)
    _register_history_compare(app)


def _register_baseline(app: typer.Typer) -> None:
    from rich.markup import escape
    from rich.table import Table

    from dvf import api
    from dvf.cli.app import (
        ConfigPath,
        ExitCode,
        OutputFormat,
        ProfileOption,
        SetOption,
        VarOption,
        _config_error,
        _display_file,
        _execution_error,
        _parse_vars,
        err,
        out,
    )
    from dvf.core.errors import ConfigError, DvfError

    baseline_app = typer.Typer(
        help="Capture and list pre-migration metric baselines (MIG-14).", no_args_is_help=True
    )
    app.add_typer(baseline_app, name="baseline")

    @baseline_app.command("capture")
    def baseline_capture_command(
        path: ConfigPath,
        name: Annotated[
            str, typer.Option("--name", "-n", help="Baseline name, for example pre_cutover.")
        ],
        side: Annotated[
            str, typer.Option("--side", help="Dataset to capture: source or target.")
        ] = "source",
        only: Annotated[
            list[str] | None,
            typer.Option("--only", help="Selector such as table:customers. Repeatable."),
        ] = None,
        overwrite: Annotated[
            bool, typer.Option("--overwrite", help="Replace an existing baseline of that name.")
        ] = False,
        no_distinct: Annotated[
            bool,
            typer.Option("--no-distinct", help="Skip distinct counts (faster on large tables)."),
        ] = False,
        profile: ProfileOption = None,
        set_: SetOption = None,
        var: VarOption = None,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="text or json.")
        ] = OutputFormat.TEXT,
    ) -> None:
        """Snapshot per-table metrics (row counts, control totals, profiles, grouped counts)
        before a migration, for the baseline_metrics check and go/no-go decisions."""
        if side not in ("source", "target"):
            err.print("[red]error[/] --side must be 'source' or 'target'")
            raise typer.Exit(ExitCode.CONFIG_ERROR)
        try:
            loaded = api.load_config(
                path, profile=profile, overrides=set_ or [], variables=_parse_vars(var)
            )
            file, document = api.capture_baseline(
                loaded,
                name,
                side=side,
                only=only or [],
                overwrite=overwrite,
                distinct=not no_distinct,
            )
        except ConfigError as exc:
            raise _config_error(exc) from exc
        except DvfError as exc:
            raise _execution_error(exc) from exc
        tables: dict[str, Any] = document["tables"]
        failed = {k: v["error"] for k, v in tables.items() if "error" in v}
        if output_format is OutputFormat.JSON:
            out.print_json(json.dumps({"path": str(file), **document}, default=str))
        else:
            grid = Table("Table", "Dataset", "Rows", "Columns", "Groups")
            for key, entry in tables.items():
                if "error" in entry:
                    grid.add_row(escape(key), "[red]error[/]", "", "", escape(entry["error"]))
                    continue
                grid.add_row(
                    escape(key),
                    escape(str(entry.get("dataset", ""))),
                    f"{entry['row_count']:,}",
                    str(len(entry.get("columns", {}))),
                    str(len(entry.get("groups", []))) if "groups" in entry else "",
                )
            out.print(grid)
            out.print(
                f"baseline '{escape(name)}' ({side}) written to {escape(_display_file(str(file)))}"
            )
        if failed:
            err.print(f"[yellow]warning[/] {len(failed)} table(s) could not be captured")
            raise typer.Exit(ExitCode.EXECUTION_ERROR)

    @baseline_app.command("list")
    def baseline_list_command(
        path: ConfigPath,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="text or json.")
        ] = OutputFormat.TEXT,
    ) -> None:
        """List the named baselines in the configuration's state folder."""
        try:
            baselines = api.list_baselines(path)
        except ConfigError as exc:
            raise _config_error(exc) from exc
        if output_format is OutputFormat.JSON:
            out.print_json(json.dumps(baselines, default=str))
            return
        if not baselines:
            out.print("no baselines captured yet")
            return
        grid = Table("Name", "Captured (UTC)", "Side", "Tables")
        for entry in baselines:
            grid.add_row(
                escape(entry["name"]),
                escape(str(entry["captured_at"] or "")[:19]),
                escape(str(entry["side"] or "")),
                str(entry["tables"]),
            )
        out.print(grid)


def _register_history_compare(app: typer.Typer) -> None:
    from rich.markup import escape
    from rich.table import Table

    from dvf import api
    from dvf.cli.app import ExitCode, OutputFormat, _config_error, out
    from dvf.core.errors import ConfigError

    existing = next((c for c in app.registered_commands if c.name == "history"), None)
    history_app = typer.Typer(
        cls=_DefaultListGroup,
        help="Run history: list recent runs, or compare two runs (mock cutover rehearsals).",
        no_args_is_help=True,
    )
    if existing is not None and existing.callback is not None:
        app.registered_commands.remove(existing)
        history_app.command("list", help=existing.help or existing.callback.__doc__)(
            existing.callback
        )

    @history_app.command("compare")
    def history_compare_command(
        run_a: Annotated[str, typer.Argument(help="The earlier run: run ID or results.json.")],
        run_b: Annotated[str, typer.Argument(help="The later run: run ID or results.json.")],
        config: Annotated[
            Path | None,
            typer.Option("--config", "-c", help="Configuration (to find its .dvf folder)."),
        ] = None,
        fail_on_new: Annotated[
            bool, typer.Option("--fail-on-new", help="Exit with 1 when run B has new failures.")
        ] = False,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="text or json.")
        ] = OutputFormat.TEXT,
    ) -> None:
        """Compare two runs, such as two mock cutover rehearsals: new, resolved, and persisting
        failures, status changes, and metric deltas from RUN_A to RUN_B."""
        try:
            diff = api.compare_runs(run_a, run_b, config=config)
        except ConfigError as exc:
            raise _config_error(exc) from exc
        if output_format is OutputFormat.JSON:
            out.print_json(json.dumps(diff.to_dict(), default=str))
        else:
            out.print(
                f"{escape(diff.run_a)} ({diff.status_a}) -> {escape(diff.run_b)} ({diff.status_b}): "
                f"{len(diff.new_failures)} new, {len(diff.resolved)} resolved, "
                f"{len(diff.persisting)} persisting failures"
            )
            for title, items in (
                ("new failures", diff.new_failures),
                ("resolved", diff.resolved),
                ("persisting", diff.persisting),
                ("checks only in run B", diff.added_checks),
                ("checks only in run A", diff.removed_checks),
            ):
                if items:
                    out.print(f"[bold]{title}[/]")
                    for item in items[:50]:
                        out.print(f"  {escape(item)}")
                    if len(items) > 50:
                        out.print(f"  … {len(items) - 50} more")
            grid = Table("Metric", "Run A", "Run B", "Change")
            for row in diff.summary_deltas:
                grid.add_row(
                    *(escape(_shown(row[k])) for k in ("metric", "before", "after", "change"))
                )
            out.print(grid)
            if diff.dataset_deltas:
                tables = Table("Table", "Metric", "Run A", "Run B", "Change")
                for row in diff.dataset_deltas[:100]:
                    tables.add_row(
                        *(
                            escape(_shown(row[k]))
                            for k in ("table", "metric", "before", "after", "change")
                        )
                    )
                out.print(tables)
            if diff.check_deltas:
                checks = Table("Check", "Unexpected (A)", "Unexpected (B)", "Change")
                for row in diff.check_deltas[:50]:
                    checks.add_row(
                        *(escape(_shown(row[k])) for k in ("check_id", "before", "after", "change"))
                    )
                out.print(checks)
        if fail_on_new and diff.new_failures:
            raise typer.Exit(ExitCode.VALIDATION_FAILED)

    app.add_typer(history_app, name="history")


def _shown(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.6g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)
