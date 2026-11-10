"""CLI commands for working with projects: plan, schema-diff, waivers list, config migrate, plugin new.

Owned by the Phase 4 docs and CLI work. :func:`register` is called by ``dvf.cli.app`` after the
application is defined; import helpers from ``dvf.cli.app`` inside it, not at module level.

- ``dvf plan`` is an alias of ``dvf explain`` (BUILD_PROMPT.md section 12.1).
- ``dvf schema-diff`` compares the schemas of configured table pairs, or of two ad hoc datasets,
  without running checks.
- ``dvf waivers list`` shows each waiver's ticket, approver, expiry, and status.
- ``dvf config migrate`` upgrades a configuration file to the current format version.
- ``dvf plugin new`` scaffolds an installable plugin package with tests.
"""

# No ``from __future__ import annotations``: Typer reads the parameter annotations of the
# commands defined inside register(), which refer to names imported there.

import json
from datetime import date
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer


class DiffFailOn(StrEnum):
    """Which schema differences make ``dvf schema-diff`` exit with code 1."""

    NEVER = "never"
    INCOMPATIBLE = "incompatible"
    LOSSY = "lossy"
    ANY = "any"


class WaiverFailOn(StrEnum):
    """Which waivers make ``dvf waivers list`` exit with code 1."""

    NEVER = "never"
    EXPIRED = "expired"
    EXPIRING = "expiring"


class ScaffoldKind(StrEnum):
    """Component kinds ``dvf plugin new`` can scaffold."""

    CHECK = "check"
    CONNECTOR = "connector"
    NORMALIZER = "normalizer"
    REPORTER = "reporter"
    NOTIFIER = "notifier"
    SECRET_PROVIDER = "secret_provider"  # noqa: S105 - a kind name, not a password
    SINK = "sink"


class _Level(IntEnum):
    NONE = 0
    ANY = 1
    LOSSY = 2
    INCOMPATIBLE = 3


_FAIL_LEVEL = {
    DiffFailOn.ANY: _Level.ANY,
    DiffFailOn.LOSSY: _Level.LOSSY,
    DiffFailOn.INCOMPATIBLE: _Level.INCOMPATIBLE,
}


def register(app: typer.Typer) -> None:
    """Add this module's commands to ``app``."""
    _register_plan(app)
    _register_schema_diff(app)
    _register_waivers(app)
    _register_config_migrate()
    _register_plugin_new(app)


# ---------------------------------------------------------------------- plan


def _register_plan(app: typer.Typer) -> None:
    from dvf.cli.app import explain_command

    app.command(
        "plan",
        help="Alias of 'dvf explain': show the plan and the SQL each step would run, without "
        "running validations.",
    )(explain_command)


# ---------------------------------------------------------------------- schema-diff


def diff_level(row: dict[str, Any]) -> _Level:
    """How serious one column row of a schema diff is."""
    status = row.get("status")
    if status == "missing_in_target":
        return _Level.INCOMPATIBLE
    if status == "extra_in_target":
        return _Level.ANY
    if status != "matched":
        return _Level.NONE
    compatibility = row.get("compatibility")
    if compatibility == "incompatible":
        return _Level.INCOMPATIBLE
    if compatibility in ("lossy", "unknown"):
        return _Level.LOSSY
    if compatibility == "compatible_widening" or row.get("nullable_change"):
        return _Level.ANY
    return _Level.NONE


def schema_diff_tables(
    loaded: Any, *, table: str | None, suite: str | None, only: list[str]
) -> list[dict[str, Any]]:
    """Resolve each comparison table (catalog lookups only) and diff its column schemas."""
    from dvf.checks.schema import schema_diff
    from dvf.core.errors import DvfError
    from dvf.diagnose.planning import find_tasks, prepare
    from dvf.execution.runner import Runner, RunOptions
    from dvf.execution.selectors import Selectors

    runner = Runner(loaded, RunOptions(selectors=Selectors.parse(only), write_reports=False))
    tables: list[dict[str, Any]] = []
    try:
        tasks = find_tasks(runner, table, suite)
        if suite is not None:
            tasks = [t for t in tasks if t.suite.name.lower() == suite.lower()]
        for task in tasks:
            item: dict[str, Any] = {
                "suite": task.suite.name,
                "table": task.table.label,
                "source": None,
                "target": None,
                "columns": [],
                "problems": [],
            }
            tables.append(item)
            if not task.suite.is_comparison:
                item["problems"].append("single-dataset suite: there is no target to compare")
                item["level"] = _Level.NONE
                continue
            try:
                prepared = prepare(runner, task.suite, task.table)
            except DvfError as exc:
                item["problems"].append(str(exc))
                item["level"] = _Level.INCOMPATIBLE
                continue
            state = prepared.state
            item["source"] = state.dataset.source.display() if state.dataset.source else None
            item["target"] = state.dataset.target.display() if state.dataset.target else None
            if prepared.pairing is None:
                item["problems"].append(prepared.plan_error or "the datasets could not be paired")
                item["level"] = _Level.INCOMPATIBLE
                continue
            rows = [r.model_dump(mode="json") for r in schema_diff(prepared.pairing)]
            for row in rows:
                row["level"] = diff_level(row).name.lower()
            item["columns"] = rows
            item["level"] = max((diff_level(r) for r in rows), default=_Level.NONE)
    finally:
        runner.pool.close_all()
    return tables


def _type_label(row: dict[str, Any], side: str) -> str:
    native = row.get(f"{side}_native_type")
    if native:
        return str(native)
    ctype = row.get(f"{side}_type")
    if isinstance(ctype, dict):
        return str(ctype.get("logical", "")).lower()
    return ""


def _result_label(row: dict[str, Any]) -> str:
    status = row.get("status")
    if status == "missing_in_target":
        return "missing in target"
    if status == "extra_in_target":
        return "extra in target"
    if status == "derived":
        return "derived from a source expression"
    labels = {"compatible_widening": "widening", "exact": "exact"}
    text = labels.get(str(row.get("compatibility")), str(row.get("compatibility") or ""))
    reasons = row.get("reasons") or []
    if reasons and text != "exact":
        text += ": " + "; ".join(reasons)
    if row.get("nullable_change"):
        text += f" ({row['nullable_change']})"
    return text


def _register_schema_diff(app: typer.Typer) -> None:
    from rich.markup import escape
    from rich.table import Table

    from dvf import api
    from dvf.cli.app import (
        ExitCode,
        OutputFormat,
        ProfileOption,
        SetOption,
        VarOption,
        _config_error,
        _execution_error,
        _parse_vars,
        _state,
        out,
    )
    from dvf.core.errors import ConfigError, DvfError

    styles = {
        _Level.NONE: "green",
        _Level.ANY: "cyan",
        _Level.LOSSY: "yellow",
        _Level.INCOMPATIBLE: "red",
    }

    @app.command("schema-diff")
    def schema_diff_command(
        ctx: typer.Context,
        first: Annotated[
            str,
            typer.Argument(
                metavar="CONFIG_OR_SOURCE",
                help="A configuration file, or the source dataset when TARGET is given.",
            ),
        ],
        second: Annotated[
            str | None,
            typer.Argument(
                metavar="TARGET",
                help="The target dataset for an ad hoc diff, in the forms 'dvf compare' accepts "
                "(a file path, duckdb:///db::table, sqlite:///db::table, postgresql://...::table).",
            ),
        ] = None,
        table: Annotated[str | None, typer.Option("--table", "-t", help="Only this table.")] = None,
        suite: Annotated[
            str | None, typer.Option("--suite", help="Only tables of this suite.")
        ] = None,
        only: Annotated[
            list[str] | None,
            typer.Option("--only", help="Selectors, as for dvf run. Repeatable."),
        ] = None,
        key: Annotated[
            list[str] | None,
            typer.Option("--key", "-k", help="Key column of an ad hoc diff. Repeatable."),
        ] = None,
        profile: ProfileOption = None,
        set_: SetOption = None,
        var: VarOption = None,
        fail_on: Annotated[
            DiffFailOn,
            typer.Option(
                "--fail-on",
                help="Exit 1 on: incompatible (missing columns, incompatible types), lossy "
                "(also narrowing and unclassified types), any difference, or never.",
            ),
        ] = DiffFailOn.INCOMPATIBLE,
        show_all: Annotated[
            bool, typer.Option("--all", help="Also list columns that match exactly.")
        ] = False,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="text or json.")
        ] = OutputFormat.TEXT,
    ) -> None:
        """Compare the column schemas of configured table pairs, or of two datasets, without
        running checks: missing and extra columns, type compatibility, and nullability."""
        try:
            if second is None:
                loaded = api.load_config(
                    Path(first),
                    profile=profile,
                    overrides=set_ or [],
                    variables=_parse_vars(var),
                    plugins=_state(ctx).plugins,
                )
            else:
                from dvf.compare.adhoc import AdHocSpec, adhoc_config

                spec = AdHocSpec(source=first, target=second, keys=list(key or []))
                loaded = api.load_config_data(adhoc_config(spec), base_dir=Path.cwd(), strict=False)
            tables = schema_diff_tables(loaded, table=table, suite=suite, only=only or [])
        except ConfigError as exc:
            raise _config_error(exc) from exc
        except DvfError as exc:
            raise _execution_error(exc) from exc
        worst = max((t["level"] for t in tables), default=_Level.NONE)
        failed = fail_on is not DiffFailOn.NEVER and worst >= _FAIL_LEVEL[fail_on]
        if output_format is OutputFormat.JSON:
            payload = {
                "tables": [{**t, "level": t["level"].name.lower()} for t in tables],
                "worst": worst.name.lower(),
                "failed": failed,
            }
            typer.echo(json.dumps(payload, indent=2, default=str))
        else:
            if not tables:
                out.print("no tables selected")
            for item in tables:
                level = item["level"]
                out.rule(f"{escape(item['suite'])}.{escape(item['table'])}")
                if item["source"] or item["target"]:
                    out.print(
                        f"source: {escape(item['source'] or '-')}   "
                        f"target: {escape(item['target'] or '-')}"
                    )
                for problem in item["problems"]:
                    out.print(f"[yellow]problem[/] {escape(problem)}")
                rows = [r for r in item["columns"] if show_all or r["level"] != "none"]
                if rows:
                    grid = Table(
                        "Source column", "Source type", "Target column", "Target type", "Result"
                    )
                    for row in rows:
                        color = styles[_Level[row["level"].upper()]]
                        grid.add_row(
                            escape(row.get("source_column") or "-"),
                            escape(_type_label(row, "source")),
                            escape(row.get("target_column") or "-"),
                            escape(_type_label(row, "target")),
                            f"[{color}]{escape(_result_label(row))}[/]",
                        )
                    out.print(grid)
                matched = sum(1 for r in item["columns"] if r["level"] == "none")
                differing = len(item["columns"]) - matched
                if item["columns"]:
                    out.print(
                        f"[{styles[level]}]{differing} difference(s)[/], "
                        f"{matched} column(s) identical"
                    )
            differing_tables = sum(1 for t in tables if t["level"] > _Level.NONE)
            out.print(f"{len(tables)} table(s), {differing_tables} with differences")
        if failed:
            raise typer.Exit(ExitCode.VALIDATION_FAILED)


# ---------------------------------------------------------------------- waivers list


def waiver_rows(waivers: list[Any], as_of: date, soon_days: int) -> list[dict[str, Any]]:
    """Each waiver with its status as of ``as_of``: active, expiring soon, or expired."""
    rows = []
    for waiver in waivers:
        expires = waiver.expires
        days_left = (expires - as_of).days if expires is not None else None
        if days_left is None:
            status = "active"
        elif days_left < 0:
            status = "expired"
        elif days_left <= soon_days:
            status = "expiring soon"
        else:
            status = "active"
        rows.append(
            {
                "check": waiver.check,
                "column": waiver.column,
                "reason": waiver.reason,
                "ticket": waiver.ticket,
                "approved_by": waiver.approved_by,
                "expires": expires.isoformat() if expires is not None else None,
                "days_left": days_left,
                "status": status,
            }
        )
    return rows


def _register_waivers(app: typer.Typer) -> None:
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
        _state,
        err,
        out,
    )
    from dvf.core.errors import ConfigError, DvfError

    waivers_app = typer.Typer(help="Review accepted differences (waivers).", no_args_is_help=True)
    app.add_typer(waivers_app, name="waivers")
    styles = {"active": "green", "expiring soon": "yellow", "expired": "red"}

    @waivers_app.command("list")
    def waivers_list_command(
        ctx: typer.Context,
        path: ConfigPath,
        soon_days: Annotated[
            int,
            typer.Option(
                "--soon-days",
                min=0,
                help="Waivers expiring within this many days are 'expiring soon'.",
            ),
        ] = 14,
        as_of: Annotated[
            str | None,
            typer.Option(
                "--as-of",
                metavar="YYYY-MM-DD",
                help="Evaluate expiry on this date instead of today (for example the cutover date).",
            ),
        ] = None,
        fail_on: Annotated[
            WaiverFailOn,
            typer.Option(
                "--fail-on",
                help="Exit 1 when a waiver is expired, also when one is expiring soon, or never.",
            ),
        ] = WaiverFailOn.NEVER,
        profile: ProfileOption = None,
        set_: SetOption = None,
        var: VarOption = None,
        env_file: EnvFileOption = None,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="text or json.")
        ] = OutputFormat.TEXT,
    ) -> None:
        """List waivers with their ticket, approver, expiry date, and status: active, expiring
        soon, or expired. Secrets are not fetched."""
        try:
            day = date.fromisoformat(as_of) if as_of else date.today()
        except ValueError as exc:
            err.print(
                f"[red]error[/] --as-of expects a date such as 2026-12-31, got {escape(str(as_of))}"
            )
            raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
        try:
            loaded = api.load_config(
                path,
                profile=profile,
                overrides=set_ or [],
                variables=_parse_vars(var),
                env_file=env_file,
                strict=False,
                resolve_secrets=False,
                plugins=_state(ctx).plugins,
            )
        except ConfigError as exc:
            raise _config_error(exc) from exc
        except DvfError as exc:
            err.print(f"[red]error[/] {escape(str(exc))}")
            raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
        rows = waiver_rows(loaded.config.waivers, day, soon_days)
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in styles}
        if output_format is OutputFormat.JSON:
            payload = {
                "as_of": day.isoformat(),
                "soon_days": soon_days,
                "counts": counts,
                "waivers": rows,
            }
            typer.echo(json.dumps(payload, indent=2))
        elif not rows:
            out.print("no waivers in this configuration")
        else:
            grid = Table(
                "Check", "Column", "Ticket", "Approved by", "Expires", "Status", "Reason",
                title=f"Waivers as of {day.isoformat()}",
                title_justify="left",
            )  # fmt: skip
            for row in rows:
                expires = row["expires"] or "never"
                if row["days_left"] is not None and row["days_left"] >= 0:
                    expires += f" ({row['days_left']}d)"
                grid.add_row(
                    escape(row["check"]),
                    escape(row["column"] or ""),
                    escape(row["ticket"] or ""),
                    escape(row["approved_by"] or ""),
                    escape(expires),
                    f"[{styles[row['status']]}]{row['status']}[/]",
                    escape(row["reason"]),
                )
            out.print(grid)
            out.print(
                f"{len(rows)} waiver(s): {counts['active']} active, "
                f"{counts['expiring soon']} expiring within {soon_days} days, "
                f"{counts['expired']} expired (expired waivers no longer apply)."
            )
        failing = counts["expired"] + (
            counts["expiring soon"] if fail_on is WaiverFailOn.EXPIRING else 0
        )
        if fail_on is not WaiverFailOn.NEVER and failing:
            raise typer.Exit(ExitCode.VALIDATION_FAILED)


# ---------------------------------------------------------------------- config migrate


def _register_config_migrate() -> None:
    from rich.markup import escape

    from dvf.cli.app import ExitCode, OutputFormat, _config_error, _display_file, config_app, out
    from dvf.config import migrate
    from dvf.core.errors import ConfigError

    @config_app.command("migrate")
    def config_migrate_command(
        path: Annotated[Path, typer.Argument(help="Configuration file to upgrade.")],
        check: Annotated[
            bool,
            typer.Option(
                "--check", help="Only report whether the file needs migrating (exit 1 if it does)."
            ),
        ] = False,
        output: Annotated[
            Path | None,
            typer.Option(
                "--output",
                "-o",
                help="Write the upgraded configuration here (YAML or JSON) instead of in place.",
            ),
        ] = None,
        no_backup: Annotated[
            bool, typer.Option("--no-backup", help="Do not keep a copy of the original file.")
        ] = False,
        from_version: Annotated[
            int | None,
            typer.Option(
                "--from-version",
                min=0,
                help="Version of a file without 'version:' (an 'extends:' fragment).",
            ),
        ] = None,
        output_format: Annotated[
            OutputFormat, typer.Option("--format", help="text or json.")
        ] = OutputFormat.TEXT,
    ) -> None:
        """Upgrade a configuration file to the current format version.

        The original is kept next to it as <file>.v<N>.bak (comments are not carried over to the
        upgraded file). Files listed in 'extends:' are migrated separately."""
        try:
            result = migrate.migrate_file(
                path, check=check, output=output, backup=not no_backup, from_version=from_version
            )
        except ConfigError as exc:
            raise _config_error(exc) from exc
        needs = result.changed and check
        if output_format is OutputFormat.JSON:
            payload = {
                "path": str(path),
                "from_version": result.from_version,
                "to_version": result.to_version,
                "changed": result.changed,
                "written": str(result.output) if result.output else None,
                "backup": str(result.backup) if result.backup else None,
                "steps": result.steps,
                "notes": result.notes,
            }
            typer.echo(json.dumps(payload, indent=2))
        else:
            shown = escape(_display_file(str(path)))
            if not result.changed and result.output is None:
                out.print(
                    f"[green]OK[/] {shown} is configuration version {result.to_version}, the "
                    "current version; nothing to migrate."
                )
            elif check:
                out.print(
                    f"[yellow]{shown} is version {result.from_version}[/]; "
                    f"'dvf config migrate {shown}' upgrades it to version {result.to_version}:"
                )
            else:
                where = escape(_display_file(str(result.output)))
                backup = (
                    f" (original kept as {escape(_display_file(str(result.backup)))})"
                    if result.backup
                    else ""
                )
                out.print(
                    f"[green]Upgraded[/] {shown} from version {result.from_version} to "
                    f"{result.to_version}: wrote {where}{backup}."
                )
            for step in result.steps:
                out.print(f"  {escape(step)}")
            for note in result.notes:
                out.print(f"  [yellow]note[/] {escape(note)}")
            if result.output is not None:
                out.print(
                    f"Next: [bold]dvf validate-config {escape(_display_file(str(result.output)))}[/]"
                )
        if needs:
            raise typer.Exit(ExitCode.VALIDATION_FAILED)


# ---------------------------------------------------------------------- plugin new


def _register_plugin_new(app: typer.Typer) -> None:
    from rich.markup import escape

    from dvf.cli.app import ExitCode, _display_file, err, out, plugins_app
    from dvf.plugins.scaffold import ScaffoldError, create_package

    plugin_app = typer.Typer(help="Create plugin packages.", no_args_is_help=True)
    app.add_typer(plugin_app, name="plugin")

    def plugin_new_command(
        kind: Annotated[ScaffoldKind, typer.Argument(help="Kind of component.")],
        name: Annotated[
            str,
            typer.Argument(
                help="Component name, namespaced with dots: acme.vat_number_valid (secret "
                "providers: acme_vault)."
            ),
        ],
        output: Annotated[
            Path | None,
            typer.Option("--output", "-o", help="Folder to create (default: ./<package name>)."),
        ] = None,
        package: Annotated[
            str | None,
            typer.Option("--package", help="Distribution name (default: dvf-<name>)."),
        ] = None,
        force: Annotated[
            bool, typer.Option("--force", help="Write into an existing, non-empty folder.")
        ] = False,
    ) -> None:
        """Create an installable plugin package: pyproject.toml with the entry point, the
        component, tests, and a README. The generated component works as written; replace its
        logic with your own."""
        try:
            spec, folder, files = create_package(
                kind.value, name, output, distribution=package, force=force
            )
        except ScaffoldError as exc:
            err.print(f"[red]error[/] {escape(str(exc))}")
            raise typer.Exit(ExitCode.CONFIG_ERROR) from exc
        for file in files:
            out.print(f"created {escape(_display_file(str(file)))}")
        out.print(
            f"Created {escape(spec.distribution)} with the {kind.value} "
            f"[bold]{escape(spec.name)}[/] ({escape(spec.module)}.{spec.kind.module}:"
            f"{escape(spec.class_name)}), entry point group {spec.kind.kind.entry_point_group}."
        )
        out.print("Next:")
        out.print(f"  cd {escape(_display_file(str(folder)))}")
        out.print('  pip install -e ".[test]"')
        out.print("  pytest")
        out.print(f"  dvf plugins list --kind {spec.kind.kind.value}")

    plugin_app.command("new")(plugin_new_command)
    plugins_app.command("new", hidden=True)(plugin_new_command)
