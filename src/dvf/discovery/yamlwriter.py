"""A small YAML writer that keeps comments, used for generated configuration files.

PyYAML cannot write comments, and generated configurations need them: a fuzzy match, a type
risk, or a suggested check should carry its evidence next to it. Build a document from
:class:`Map`, :class:`Seq`, and :class:`Flow` nodes (plain dicts, lists, and scalars work too)
and render it with :func:`dump`. Scalars are quoted by PyYAML in flow-safe form, so the output
always reads back to the same data (``yaml.safe_load(dump(doc)) == plain(doc)``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

_INDENT = 2


def _lines(comment: str | Iterable[str] | None) -> list[str]:
    if comment is None:
        return []
    items = [comment] if isinstance(comment, str) else list(comment)
    result: list[str] = []
    for item in items:
        result.extend(item.splitlines() or [""])
    return result


@dataclass
class Entry:
    """One ``key: value`` pair of a :class:`Map`, with comments above it and after it."""

    key: str
    value: Any
    comments: list[str] = field(default_factory=list)
    inline: str | None = None


@dataclass
class Item:
    """One item of a :class:`Seq`, with comments above it and after it."""

    value: Any
    comments: list[str] = field(default_factory=list)
    inline: str | None = None


class Map:
    """A block mapping whose entries may carry comments."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        self.entries: list[Entry] = []
        for key, value in (data or {}).items():
            self.add(key, value)

    def add(
        self,
        key: str,
        value: Any,
        *,
        comment: str | Iterable[str] | None = None,
        inline: str | None = None,
    ) -> Map:
        """Append ``key: value`` (replacing an existing key) and return this map."""
        entry = Entry(key, value, _lines(comment), inline)
        for i, existing in enumerate(self.entries):
            if existing.key == key:
                self.entries[i] = entry
                return self
        self.entries.append(entry)
        return self

    def get(self, key: str) -> Any:
        return next((e.value for e in self.entries if e.key == key), None)

    def __contains__(self, key: object) -> bool:
        return any(e.key == key for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


class Seq:
    """A block sequence whose items may carry comments."""

    def __init__(self, items: Iterable[Any] = ()) -> None:
        self.items: list[Item] = [Item(v) for v in items]

    def add(
        self, value: Any, *, comment: str | Iterable[str] | None = None, inline: str | None = None
    ) -> Seq:
        self.items.append(Item(value, _lines(comment), inline))
        return self

    def __len__(self) -> int:
        return len(self.items)


@dataclass
class Flow:
    """A mapping or list written on one line in flow style: ``{source: a, target: b}``."""

    value: Any


@dataclass
class Document:
    """A whole file: header comments, the root mapping, and footer comments."""

    root: Map
    header: list[str] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- plain data


def plain(value: Any) -> Any:
    """``value`` with every writer node replaced by plain dicts and lists."""
    if isinstance(value, Document):
        return plain(value.root)
    if isinstance(value, Map):
        return {e.key: plain(e.value) for e in value.entries}
    if isinstance(value, Seq):
        return [plain(i.value) for i in value.items]
    if isinstance(value, Flow):
        return plain(value.value)
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


# ---------------------------------------------------------------------- scalars


def scalar(value: Any) -> str:
    """A scalar written so that it is valid in both block and flow context."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"cannot write the non-finite number {value} in a configuration")
        return repr(value)
    text = yaml.safe_dump(
        [str(value)], default_flow_style=True, allow_unicode=True, width=1_000_000_000
    ).strip()
    return text[1:-1]


def _flow(value: Any) -> str:
    value = plain(value)
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{scalar(k)}: {_flow(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    return scalar(value)


def _literal_block(text: str, indent: int) -> list[str] | None:
    """A ``|`` block for multi-line text, or None when the text needs quoting instead."""
    if "\n" not in text or text.startswith((" ", "\n")) or "\t" in text or "\r" in text:
        return None
    body = text[:-1] if text.endswith("\n") else text
    if body.endswith("\n"):
        return None
    lines = body.split("\n")
    if any(line != line.rstrip() for line in lines):
        return None
    indicator = "|" if text.endswith("\n") else "|-"
    pad = " " * indent
    return [indicator, *[(pad + line) if line else "" for line in lines]]


def _is_block(value: Any) -> bool:
    """Whether ``value`` is written on the lines below its key (not after it)."""
    if isinstance(value, Map | Seq):
        return len(value) > 0
    if isinstance(value, Mapping):
        return len(value) > 0
    if isinstance(value, list | tuple):
        return any(isinstance(v, Map | Seq | Mapping | list | tuple) for v in value)
    return False


def _inline_text(value: Any) -> str:
    if isinstance(value, Flow):
        return _flow(value.value)
    if isinstance(value, Map | Seq | Mapping | list | tuple):
        return _flow(value)
    return scalar(value)


def _comment_lines(comments: list[str], indent: int) -> list[str]:
    pad = " " * indent
    return [f"{pad}#" + (f" {c}" if c else "") for c in comments]


def _suffix(inline: str | None) -> str:
    return f"  # {inline}" if inline else ""


# ---------------------------------------------------------------------- blocks


def _entries(value: Any) -> list[Entry]:
    if isinstance(value, Map):
        return value.entries
    return [Entry(str(k), v) for k, v in value.items()]


def _items(value: Any) -> list[Item]:
    if isinstance(value, Seq):
        return value.items
    return [Item(v) for v in value]


def _render_map(value: Any, indent: int) -> list[str]:
    lines: list[str] = []
    pad = " " * indent
    for entry in _entries(value):
        lines.extend(_comment_lines(entry.comments, indent))
        key = scalar(entry.key)
        item = entry.value
        if _is_block(item):
            lines.append(f"{pad}{key}:{_suffix(entry.inline)}")
            if isinstance(item, Map | Mapping):
                lines.extend(_render_map(item, indent + _INDENT))
            else:
                lines.extend(_render_seq(item, indent + _INDENT))
            continue
        if isinstance(item, str):
            block = _literal_block(item, indent + _INDENT)
            if block is not None:
                lines.append(f"{pad}{key}: {block[0]}{_suffix(entry.inline)}")
                lines.extend(block[1:])
                continue
        lines.append(f"{pad}{key}: {_inline_text(item)}{_suffix(entry.inline)}")
    return lines


def _render_seq(value: Any, indent: int) -> list[str]:
    lines: list[str] = []
    pad = " " * indent
    for item in _items(value):
        inner = item.value
        if isinstance(inner, Map | Mapping) and len(inner) > 0:
            entries = _entries(inner)
            lines.extend(_comment_lines(item.comments + entries[0].comments, indent))
            first = Entry(entries[0].key, entries[0].value, [], entries[0].inline or item.inline)
            rest = entries[1:]
            body = _render_map(_from_entries([first, *rest]), indent + _INDENT)
            body[0] = f"{pad}- " + body[0][indent + _INDENT :]
            lines.extend(body)
            continue
        lines.extend(_comment_lines(item.comments, indent))
        if isinstance(inner, Seq | list | tuple) and _is_block(inner):
            body = _render_seq(inner, indent + _INDENT)
            body[0] = f"{pad}- " + body[0][indent + _INDENT :]
            lines.extend(body)
            continue
        lines.append(f"{pad}- {_inline_text(inner)}{_suffix(item.inline)}")
    return lines


def _from_entries(entries: list[Entry]) -> Map:
    result = Map()
    result.entries = list(entries)
    return result


def dump(document: Document | Map) -> str:
    """Render ``document`` as YAML text ending with a newline."""
    doc = document if isinstance(document, Document) else Document(document)
    lines = _comment_lines(doc.header, 0)
    if doc.header:
        lines.append("")
    lines.extend(_render_map(doc.root, 0))
    if doc.footer:
        lines.append("")
        lines.extend(_comment_lines(doc.footer, 0))
    return "\n".join(lines) + "\n"


__all__ = ["Document", "Entry", "Flow", "Item", "Map", "Seq", "dump", "plain", "scalar"]
