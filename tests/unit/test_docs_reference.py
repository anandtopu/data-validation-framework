"""The generated reference pages (docs/reference/*.md, docs/cli.md) and config field docs."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Annotated

import pytest
import typer
from pydantic import BaseModel

from dvf.config.models import DvfConfig, SinkConfig
from dvf.docsgen import PAGES, generate, stale_pages, write_pages
from dvf.docsgen.__main__ import main as docsgen_main
from dvf.docsgen.climd import cli_page
from dvf.docsgen.markdown import cell, code, literal, one_line, slug, table
from dvf.docsgen.pages import configuration_document
from dvf.docsgen.schemadoc import model_classes, own_field_owner, strip_interpolation
from dvf.plugins.registry import PluginKind, Registry
from tests.conftest import REPO_ROOT


@pytest.fixture(scope="module")
def pages() -> dict[str, str]:
    return generate()


def test_committed_reference_pages_are_up_to_date(pages: dict[str, str]) -> None:
    """Regenerate with `uv run nox -s docs_reference` if this fails."""
    stale = [
        rel
        for rel, text in pages.items()
        if (REPO_ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n") != text
    ]
    assert not stale, f"stale generated pages: {stale}; run `uv run nox -s docs_reference`"


def _config_models(registry: Registry) -> list[type[BaseModel]]:
    roots: list[type[BaseModel]] = [DvfConfig, SinkConfig]
    for entry in registry.entries():
        if entry.config_model is not None:
            roots.append(entry.config_model)
        params = getattr(entry.implementation, "params_model", None)
        if inspect.isclass(params) and issubclass(params, BaseModel):
            roots.append(params)
    return list(model_classes(roots).values())


def test_every_config_field_has_a_description(registry: Registry) -> None:
    """Attribute docstrings become the JSON Schema descriptions and the reference docs."""
    missing = set()
    for model in _config_models(registry):
        for name, info in model.model_fields.items():
            if not info.description:
                owner = own_field_owner(model, name)
                missing.add(f"{owner.__module__}.{owner.__qualname__}.{name}")
    assert not sorted(missing), (
        f"{len(missing)} configuration fields have no docstring:\n" + "\n".join(sorted(missing))
    )


def test_configuration_page_covers_every_section_and_type(
    pages: dict[str, str], registry: Registry
) -> None:
    text = pages["docs/reference/configuration.md"]
    for field in DvfConfig.model_fields:
        assert f"| `{field}` |" in text
    for kind, prefix in (
        (PluginKind.CONNECTOR, "connection"),
        (PluginKind.CHECK, "check"),
        (PluginKind.NOTIFIER, "notification"),
    ):
        for name in registry.names(kind):
            assert f"{{#{prefix}-{name.replace('.', '-')}}}" in text, name
    doc = configuration_document(registry)
    for definition in doc.sections:
        assert f"{{#{definition.lower()}}}" in text, definition


def test_components_page_lists_every_component(pages: dict[str, str], registry: Registry) -> None:
    text = pages["docs/reference/components.md"]
    for entry in registry.entries():
        assert f"| `{entry.name}` | {entry.status} |" in text
    assert "`on_config_loaded(config)`" in text
    assert "dvf.secret_providers" in text


def test_cli_page_lists_every_command(pages: dict[str, str]) -> None:
    text = pages["docs/cli.md"]
    for command in ("run", "explain", "plan", "schema-diff", "waivers list", "config migrate"):
        assert f"`dvf {command}`" in text, command
    assert "`plugin new`" in text or "`dvf plugin new`" in text
    assert "`DVF_LOG_LEVEL`" in text


def test_cli_page_for_a_small_app() -> None:
    app = typer.Typer()
    sub = typer.Typer(help="Things.")
    app.add_typer(sub, name="things")

    @app.command("go")
    def go(
        target: str,
        times: Annotated[int, typer.Option(min=1, max=9, help="How many.")] = 1,
        mode: Annotated[str, typer.Option("--mode", help="A mode.")] = "fast",
        path: Annotated[Path, typer.Option(help="A folder.")] = Path("a/b"),
        loud: Annotated[bool, typer.Option("--loud/--quiet", help="Volume.")] = False,
        tags: Annotated[list[str] | None, typer.Option("--tag", help="Repeatable tag.")] = None,
        extra: Annotated[list[str] | None, typer.Argument()] = None,
    ) -> None:
        """Go somewhere.

        Second paragraph with ``code``.
        """

    @sub.command("list")
    def things_list() -> None:
        """List things."""

    @sub.command("hidden", hidden=True)
    def hidden() -> None:
        """Not shown."""

    text = cli_page(app)
    assert "## `dvf go` {#dvf-go}" in text
    assert "dvf go [OPTIONS] TARGET [EXTRA...]" in text
    assert "integer (>= 1, <= 9)" in text
    assert "`a/b`" in text
    assert "`--loud` / `--quiet`" in text and "`--quiet`" in text
    assert "text, repeatable" in text
    assert "Second paragraph with `code`." in text
    assert "### `dvf things list`" in text
    assert "hidden" not in text.split("## Commands")[1].split("## `dvf`")[0]


def test_write_and_check_pages(tmp_path: Path, registry: Registry) -> None:
    assert sorted(stale_pages(tmp_path, registry)) == sorted(PAGES)
    written = write_pages(tmp_path, registry)
    assert len(written) == len(PAGES)
    assert stale_pages(tmp_path, registry) == []
    assert write_pages(tmp_path, registry) == []
    assert docsgen_main(["--root", str(tmp_path), "--check"]) == 0
    (tmp_path / PAGES[0]).write_text("old\n", encoding="utf-8")
    assert docsgen_main(["--root", str(tmp_path), "--check"]) == 1
    assert docsgen_main(["--root", str(tmp_path)]) == 0
    assert docsgen_main(["--root", str(tmp_path), "--check"]) == 0


def test_markdown_helpers() -> None:
    assert cell("a | b `x|y`") == "a \\| b `x|y`"
    assert code("a`b") == "``a`b``"
    assert code("`a") == "`` `a ``"
    assert literal("") == '`""`'
    assert literal(None) == "`null`"
    assert literal([1, "a"]) == '`[1, "a"]`'
    assert one_line("First ``x``.\n\n    Second.") == "First `x`. Second."
    assert slug("dvf config migrate") == "dvf-config-migrate"
    assert table(["A"], [["x|y"]]) == "| A |\n|---|\n| x\\|y |\n"


def test_strip_interpolation() -> None:
    interp = {"$ref": "#/$defs/Interpolation"}
    node = {
        "anyOf": [{"anyOf": [{"type": "integer", "minimum": 1}, interp]}, {"type": "null"}],
        "default": None,
    }
    assert strip_interpolation(node) == {
        "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
        "default": None,
    }
    assert strip_interpolation({"anyOf": [{"type": "boolean", "default": True}, interp]}) == {
        "type": "boolean",
        "default": True,
    }
    three = {"anyOf": [{"type": "integer"}, {"type": "number"}, interp]}
    assert strip_interpolation(three) == {"anyOf": [{"type": "integer"}, {"type": "number"}]}


def test_type_text_variants(registry: Registry) -> None:
    doc = configuration_document(registry)
    assert doc.type_text({}) == "any"
    assert doc.type_text({"type": ["string", "null"]}) == "string"
    assert doc.type_text({"type": "string", "format": "password"}) == "secret string"
    assert doc.type_text({"type": "string", "format": "date-time"}).startswith("date-time")
    assert doc.type_text({"type": "string", "format": "duration"}) == "duration"
    assert doc.type_text({"enum": ["a", "b"]}) == "one of `a`, `b`"
    assert doc.type_text({"const": "x"}) == "`x`"
    assert doc.type_text({"anyOf": [{"type": "null"}]}) == "null"
    assert doc.type_text({"type": "object"}) == "mapping"
    assert doc.type_text({"type": "object", "patternProperties": {"^a$": {"type": "integer"}}}) == (
        "mapping of integer"
    )
    assert doc.type_text({"$ref": "#/$defs/Severity"}).startswith("one of `info`")
    assert doc.constraints({"minLength": 1, "minItems": 1, "maxItems": 3}) == [
        "not empty",
        "at least one item",
        "at most 3 items",
    ]
