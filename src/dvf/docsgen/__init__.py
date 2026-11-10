"""Generators for the documentation site's reference pages (BUILD_PROMPT.md section 15).

The configuration reference, the components list, the results schema, and the command line
reference are generated from the Pydantic models, the component registry, and the Typer
application, so they cannot drift from the code. Regenerate them with
``uv run nox -s docs_reference`` (or ``python -m dvf.docsgen``); a unit test fails when a
committed page is stale.

The registry used is :func:`dvf.plugins.registry.new_registry` without entry points, so the pages
list built-in components only, whatever plugins are installed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dvf.plugins.registry import Registry, new_registry

PAGES: tuple[str, ...] = (
    "docs/reference/configuration.md",
    "docs/reference/components.md",
    "docs/reference/results.md",
    "docs/cli.md",
)
"""Generated pages, relative to the repository root."""


def _renderers() -> dict[str, Callable[[Registry], str]]:
    from dvf.docsgen.climd import cli_page
    from dvf.docsgen.pages import components_page, configuration_page, results_page

    return {
        "docs/reference/configuration.md": configuration_page,
        "docs/reference/components.md": components_page,
        "docs/reference/results.md": lambda _registry: results_page(),
        "docs/cli.md": lambda _registry: cli_page(),
    }


def generate(registry: Registry | None = None) -> dict[str, str]:
    """Render every generated page: ``{relative path: Markdown}``."""
    registry = registry or new_registry()
    renderers = _renderers()
    return {path: renderers[path](registry) for path in PAGES}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except FileNotFoundError:
        return None


def stale_pages(root: Path, registry: Registry | None = None) -> list[str]:
    """Generated pages under ``root`` that are missing or differ from a fresh rendering."""
    return [rel for rel, text in generate(registry).items() if _read(root / rel) != text]


def write_pages(root: Path, registry: Registry | None = None) -> list[Path]:
    """Write the generated pages under ``root``; returns the files that changed."""
    changed = []
    for rel, text in generate(registry).items():
        path = root / rel
        if _read(path) == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        changed.append(path)
    return changed


__all__ = ["PAGES", "generate", "stale_pages", "write_pages"]
