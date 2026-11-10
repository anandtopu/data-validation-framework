"""Small Markdown helpers shared by the reference generators."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

from pydantic_core import to_jsonable_python

GENERATED_NOTE = (
    '!!! note "Generated page"\n'
    "    This page is generated from {source} by `uv run nox -s docs_reference`. "
    "Do not edit it by hand; change the source and regenerate.\n"
)
"""Admonition placed at the top of every generated page (``{source}`` is filled in)."""

_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.DOTALL)


def rst_to_md(text: str | None) -> str:
    """Convert docstring text to Markdown: RST double backticks become single backticks."""
    if not text:
        return ""
    return inspect.cleandoc(text).replace("``", "`")


def one_line(text: str | None) -> str:
    """Docstring text as a single line for a table cell (paragraph breaks become spaces)."""
    return " ".join(rst_to_md(text).split())


def cell(text: str) -> str:
    """Escape a table cell: pipes outside code spans would end the cell."""
    parts: list[str] = []
    last = 0
    for match in _CODE_SPAN.finditer(text):
        parts.append(text[last : match.start()].replace("|", "\\|"))
        parts.append(match.group(0))
        last = match.end()
    parts.append(text[last:].replace("|", "\\|"))
    return "".join(parts).replace("\n", " ").strip()


def code(value: str) -> str:
    """Wrap ``value`` in a code span that survives backticks inside it."""
    fence = "``" if "`" in value else "`"
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def literal(value: Any) -> str:
    """A configuration value as YAML-like inline code (``true``, ``null``, ``[a, b]``)."""
    value = to_jsonable_python(value)
    if isinstance(value, str):
        return code(value) if value else '`""`'
    return code(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")))


def table(headers: list[str], rows: list[list[str]]) -> str:
    """A Markdown table; cells are escaped."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(cell(c) for c in row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def slug(text: str) -> str:
    """A stable heading anchor: lowercase letters, digits, hyphens, and underscores."""
    return re.sub(r"[^a-z0-9_]+", "-", text.lower()).strip("-")
