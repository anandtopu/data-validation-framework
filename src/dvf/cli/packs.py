"""CLI commands contributed by the Spark, file, NoSQL, and SAP packs (if any).

Owned by the Phase 3 pack work. :func:`register` is called by ``dvf.cli.app`` after the
application is defined; import helpers from ``dvf.cli.app`` inside it, not at module level.

SAP pack: ``dvf sap templates``, ``dvf sap copy-template NAME``, and ``dvf sap backends``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

sap_app = typer.Typer(
    help="SAP pack: business object templates and RFC backends.", no_args_is_help=True
)

TemplateName = Annotated[str, typer.Argument(help="Template name (see 'dvf sap templates').")]
FolderOption = Annotated[
    Path, typer.Option("--to", help="Folder to copy the template into.", file_okay=False)
]
ForceOption = Annotated[bool, typer.Option("--force", help="Replace an existing file.")]


@sap_app.command("templates")
def sap_templates() -> None:
    """List the SAP business object templates and the variables each one needs."""
    from dvf.packs.sap.catalog import TEMPLATES

    for template in TEMPLATES.values():
        typer.echo(f"{template.name}: {template.description}")
        typer.echo(f"    vars: {', '.join(template.variables)}")


@sap_app.command("copy-template")
def sap_copy_template(
    name: TemplateName, to: FolderOption = Path("sap"), force: ForceOption = False
) -> None:
    """Copy a template next to your configuration, to use with 'extends:'."""
    from dvf.packs.sap.catalog import TEMPLATES, copy_template

    template = TEMPLATES.get(name)
    if template is None:
        typer.echo(
            f"error: unknown template '{name}'; available: {', '.join(sorted(TEMPLATES))}",
            err=True,
        )
        raise typer.Exit(2)
    try:
        path = copy_template(name, to, force=force)
    except FileExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Copied {name} to {path}")
    typer.echo(f"Add it to 'extends:' and set these vars: {', '.join(template.variables)}")


@sap_app.command("backends")
def sap_backends() -> None:
    """List the sap_rfc backends: ready, or declared without an implementation."""
    from dvf.packs.sap.rfc.backends import backend_class, backend_names

    for name, status in backend_names().items():
        typer.echo(f"{name}: {status} - {backend_class(name).summary}")


def register(app: typer.Typer) -> None:
    """Add this module's commands to ``app``."""
    app.add_typer(sap_app, name="sap")
