"""CLI commands for notifications: ``dvf notify test`` sends a test message to the channels.

:func:`register` is called by ``dvf.cli.app`` after the application is defined; it imports
helpers from ``dvf.cli.app`` inside, not at module level.
"""

# No ``from __future__ import annotations``: Typer reads the parameter annotations of the
# commands defined inside register(), which refer to names imported there.

import json
from typing import Annotated

import typer


def register(app: typer.Typer) -> None:
    """Add this module's commands to ``app``."""
    from rich.markup import escape
    from rich.table import Table

    from dvf import api
    from dvf.cli.app import (
        ConfigPath,
        EnvFileOption,
        ExitCode,
        OutputFormat,
        ProfileOption,
        SetOption,
        VarOption,
        _config_error,
        _parse_vars,
        _print_diagnostics,
        _state,
        err,
        out,
    )
    from dvf.core.errors import ConfigError

    notify_app = typer.Typer(help="Work with notification channels.", no_args_is_help=True)
    app.add_typer(notify_app, name="notify")

    @notify_app.command("test")
    def notify_test_command(
        ctx: typer.Context,
        path: ConfigPath,
        channel: Annotated[
            str | None,
            typer.Option(
                "--channel",
                "-c",
                help="Send only to this channel (its name, or its type when unnamed), even if "
                "it is disabled.",
            ),
        ] = None,
        profile: ProfileOption = None,
        set_: SetOption = None,
        var: VarOption = None,
        env_file: EnvFileOption = None,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="Output format.")
        ] = OutputFormat.TEXT,
    ) -> None:
        """Send a test message to the configured notification channels.

        The message says it is a test and carries no run data. Exit codes: 0 every message was
        delivered, 2 a delivery failed, 3 configuration errors (or no channels).
        """
        from dvf.packs.integrations.dispatch import channels, notify_test

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
        _print_diagnostics(loaded.warnings)
        if not channels(loaded.config):
            err.print("[red]error[/] The configuration has no notifications.")
            raise typer.Exit(ExitCode.CONFIG_ERROR)
        try:
            deliveries = notify_test(
                loaded.config, loaded.registry, loaded.base_dir, channel=channel
            )
        except LookupError as exc:
            err.print(f"[red]error[/] {escape(str(exc))}")
            raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
        if not deliveries:
            err.print(
                "[yellow]warning[/] Every channel is disabled; name one with --channel to test it."
            )
            raise typer.Exit(ExitCode.CONFIG_ERROR)
        if output_format is OutputFormat.JSON:
            typer.echo(
                json.dumps(
                    [
                        {
                            "channel": d.channel,
                            "type": d.type,
                            "ok": d.ok,
                            "attempts": d.attempts,
                            "duration_ms": d.duration_ms,
                            "error": d.error,
                        }
                        for d in deliveries
                    ],
                    indent=2,
                )
            )
        else:
            table = Table(title="dvf notify test", title_justify="left")
            for column in ("Channel", "Type", "Result", "Attempts", "Time (ms)", "Details"):
                table.add_column(
                    column, justify="right" if column in ("Attempts", "Time (ms)") else "left"
                )
            for d in deliveries:
                table.add_row(
                    escape(d.channel),
                    escape(d.type),
                    "[green]sent[/]" if d.ok else "[red]failed[/]",
                    str(d.attempts),
                    f"{d.duration_ms:.0f}",
                    escape(d.error or ""),
                )
            out.print(table)
        if not all(d.ok for d in deliveries):
            raise typer.Exit(ExitCode.EXECUTION_ERROR)
