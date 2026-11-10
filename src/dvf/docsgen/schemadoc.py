"""Render a JSON Schema (from Pydantic models) as Markdown reference sections.

The configuration and results references are generated from the same JSON Schemas that dvf
exports for editors (:func:`dvf.config.schema.config_json_schema`), so the documentation shows
exactly what a file may contain: shorthands accepted by validators, allowed values, and limits.
Defaults built by factories are not in a JSON Schema; they are read from the Pydantic models.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined, to_jsonable_python

from dvf.docsgen.markdown import code, literal, one_line, rst_to_md, table

INTERPOLATION_REF = "#/$defs/Interpolation"


def strip_interpolation(node: Any) -> Any:
    """Remove the ``${...}`` placeholder alternative the config schema adds to typed values."""
    if isinstance(node, list):
        return [strip_interpolation(item) for item in node]
    if not isinstance(node, dict):
        return node
    result = {key: strip_interpolation(value) for key, value in node.items()}
    branches = result.get("anyOf")
    if isinstance(branches, list) and {"$ref": INTERPOLATION_REF} in branches:
        rest = [b for b in branches if b != {"$ref": INTERPOLATION_REF}]
        outer = {k: v for k, v in result.items() if k != "anyOf"}
        if len(rest) == 1 and isinstance(rest[0], dict):
            return {**rest[0], **outer}
        return {**outer, "anyOf": rest}
    return result


def model_classes(roots: list[type[BaseModel]]) -> dict[str, type[BaseModel]]:
    """Every Pydantic model reachable from ``roots`` through field annotations, by class name."""
    found: dict[str, type[BaseModel]] = {}
    stack = list(roots)
    while stack:
        model = stack.pop(0)
        if found.get(model.__name__) is model:
            continue
        found.setdefault(model.__name__, model)
        for info in model.model_fields.values():
            stack.extend(_models_in(info.annotation))
    return found


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def own_field_owner(model: type[BaseModel], name: str) -> type:
    """The class in ``model``'s MRO that declares field ``name``."""
    for klass in model.__mro__:
        if name in inspect.get_annotations(klass):
            return klass
    return model


@dataclass
class Variant:
    """One member of a polymorphic section (a connection, check, or notification type)."""

    name: str
    definition: str
    """The ``$defs`` name of the variant's schema."""
    summary: str = ""
    status: str = ""
    extra: str | None = None
    model: type[BaseModel] | None = None


@dataclass
class Category:
    """A polymorphic section keyed by ``type``, rendered once with its common fields."""

    key: str
    """Anchor prefix and identifier, for example ``connection``."""
    title: str
    base: type[BaseModel]
    intro: str
    variants: dict[str, Variant] = field(default_factory=dict)
    """Variants by ``$defs`` name."""

    @property
    def anchor(self) -> str:
        return f"{self.key}-types"

    def variant_anchor(self, variant: Variant) -> str:
        return f"{self.key}-{variant.name.replace('.', '-')}"


@dataclass
class _Section:
    definition: str
    path: str
    category: Category | None = None
    """Set for models reached through a variant of ``category``."""
    also: list[str] = field(default_factory=list)


class SchemaDocument:
    """Renders the models of one JSON Schema as Markdown sections with cross-links."""

    def __init__(
        self,
        schema: Mapping[str, Any],
        classes: Mapping[str, type[BaseModel]],
        *,
        root_name: str,
        categories: Sequence[Category] = (),
        link_prefix: str = "",
    ) -> None:
        cleaned = strip_interpolation(dict(schema))
        self.defs: dict[str, Any] = dict(cleaned.get("$defs", {}))
        self.root_name = root_name
        self.defs[root_name] = {k: v for k, v in cleaned.items() if k != "$defs"}
        self.classes = dict(classes)
        self.categories = list(categories)
        self.link_prefix = link_prefix
        self._by_variant = {d: c for c in self.categories for d in c.variants}
        self.sections: dict[str, _Section] = {}
        self._order: list[str] = []
        self._variant_nested: dict[str, list[str]] = {}
        self._walk()

    # ------------------------------------------------------------------ discovery
    def _walk(self) -> None:
        queue: list[tuple[str, str, Category | None, str | None]] = [
            (self.root_name, "", None, None)
        ]
        while queue:
            name, path, category, owner_variant = queue.pop(0)
            if name in self.sections:
                section = self.sections[name]
                if path not in section.also and path != section.path:
                    section.also.append(path)
                continue
            if name in self._by_variant:
                continue
            self.sections[name] = _Section(name, path, category)
            if owner_variant is not None:
                self._variant_nested.setdefault(owner_variant, []).append(name)
            else:
                self._order.append(name)
            for prop, node in self.defs[name].get("properties", {}).items():
                child = f"{path}.{prop}" if path else prop
                for ref, suffix in self._refs(node):
                    if ref in self._by_variant:
                        continue
                    queue.append((ref, child + suffix, category, owner_variant))
        for cat in self.categories:
            for definition, variant in sorted(cat.variants.items(), key=lambda kv: kv[1].name):
                vpath = f"{cat.key} {code(variant.name)}"
                for prop, node in self.defs[definition].get("properties", {}).items():
                    for ref, suffix in self._refs(node):
                        if ref in self._by_variant:
                            continue
                        self._queue_nested(ref, f"{vpath}: {prop}{suffix}", cat, definition)

    def _queue_nested(self, ref: str, path: str, category: Category, owner: str) -> None:
        if ref in self.sections:
            section = self.sections[ref]
            if path != section.path and path not in section.also:
                section.also.append(path)
            return
        self.sections[ref] = _Section(ref, path, category)
        self._variant_nested.setdefault(owner, []).append(ref)
        for prop, node in self.defs[ref].get("properties", {}).items():
            for child, suffix in self._refs(node):
                if child not in self._by_variant:
                    self._queue_nested(child, f"{path}.{prop}{suffix}", category, owner)

    def _refs(self, node: Any, suffix: str = "") -> Iterator[tuple[str, str]]:
        """Model ``$defs`` referenced by ``node`` (enums excluded), with a path suffix."""
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref != INTERPOLATION_REF:
            name = ref.rsplit("/", 1)[-1]
            if "properties" in self.defs.get(name, {}):
                yield name, suffix
        for key in ("anyOf", "oneOf", "allOf"):
            for branch in node.get(key, []):
                yield from self._refs(branch, suffix)
        if isinstance(node.get("items"), dict):
            yield from self._refs(node["items"], suffix + "[]")
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            yield from self._refs(additional, suffix + ".<name>")
        for value in node.get("patternProperties", {}).values():
            yield from self._refs(value, suffix + ".<name>")

    # ------------------------------------------------------------------ types
    def anchor(self, definition: str) -> str:
        return definition.lower()

    def _link(self, definition: str) -> str:
        if definition in self._by_variant:
            cat = self._by_variant[definition]
            variant = cat.variants[definition]
            return f"[{variant.name}]({self.link_prefix}#{cat.variant_anchor(variant)})"
        return f"[object]({self.link_prefix}#{self.anchor(definition)})"

    def type_text(self, node: Any) -> str:
        """A readable type: ``integer``, ``list of string``, ``mapping of [object](#x)``."""
        if not isinstance(node, dict) or not node:
            return "any"
        if "discriminator" in node:
            branches: list[Any] = node.get("oneOf") or node.get("anyOf") or []
            refs = [str(b.get("$ref", "")) for b in branches if isinstance(b, dict)]
            names = {r.rsplit("/", 1)[-1] for r in refs}
            for cat in self.categories:
                if names & set(cat.variants):
                    return f"[{cat.key}]({self.link_prefix}#{cat.anchor}) (by `type`)"
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            target = self.defs.get(name, {})
            if "enum" in target:
                return "one of " + ", ".join(literal(v) for v in target["enum"])
            return self._link(name)
        if "const" in node:
            return literal(node["const"])
        if "enum" in node:
            return "one of " + ", ".join(literal(v) for v in node["enum"])
        for key in ("anyOf", "oneOf"):
            if key in node:
                parts = [
                    self.type_text(b)
                    for b in node[key]
                    if not (isinstance(b, dict) and b.get("type") == "null")
                ]
                unique = list(dict.fromkeys(parts))
                if not unique:
                    return "null"
                return unique[0] if len(unique) == 1 else " or ".join(unique)
        kind = node.get("type")
        if isinstance(kind, list):
            return " or ".join(str(k) for k in kind if k != "null") or "null"
        if kind == "array":
            item = self.type_text(node.get("items", {}))
            return f"list of {item}"
        if kind == "object":
            additional = node.get("additionalProperties")
            patterns = node.get("patternProperties")
            if isinstance(additional, dict) and additional:
                return f"mapping of {self.type_text(additional)}"
            if isinstance(patterns, dict) and patterns:
                return f"mapping of {self.type_text(next(iter(patterns.values())))}"
            return "mapping"
        if kind == "string":
            fmt = node.get("format")
            if fmt == "password":
                return "secret string"
            if fmt == "date":
                return "date (`YYYY-MM-DD`)"
            if fmt == "date-time":
                return "date-time (ISO 8601)"
            if fmt == "duration":
                return "duration"
            return "string"
        if isinstance(kind, str):
            return kind
        return "any"

    def constraints(self, node: Any) -> list[str]:
        """Limits such as ``> 0``, ``at most 128 characters``, or a pattern."""
        found: list[str] = []
        if not isinstance(node, dict):
            return found
        labels = {
            "exclusiveMinimum": "> {}",
            "minimum": ">= {}",
            "exclusiveMaximum": "< {}",
            "maximum": "<= {}",
            "minLength": "at least {} characters",
            "maxLength": "at most {} characters",
            "minItems": "at least {} items",
            "maxItems": "at most {} items",
        }
        for key, label in labels.items():
            if key not in node:
                continue
            if key == "minLength" and node[key] == 1:
                found.append("not empty")
            elif key == "minItems" and node[key] == 1:
                found.append("at least one item")
            else:
                found.append(label.format(node[key]))
        if "pattern" in node:
            found.append("matching " + code(node["pattern"]))
        for key in ("anyOf", "oneOf"):
            for branch in node.get(key, []):
                for item in self.constraints(branch):
                    if item not in found:
                        found.append(item)
        return found

    # ------------------------------------------------------------------ fields
    def _field_info(self, definition: str, prop: str) -> Any:
        model = self.classes.get(definition) or self._variant_model(definition)
        if model is None:
            return None
        for name, info in model.model_fields.items():
            if (info.alias or name) == prop or name == prop:
                return info
        return None

    def _variant_model(self, definition: str) -> type[BaseModel] | None:
        cat = self._by_variant.get(definition)
        if cat is None:
            return None
        return cat.variants[definition].model or cat.base

    def default_text(self, definition: str, prop: str, node: Mapping[str, Any]) -> str:
        required = prop in self.defs[definition].get("required", [])
        if required:
            return "**required**"
        if "default" in node:
            return literal(node["default"])
        info = self._field_info(definition, prop)
        if info is not None:
            if info.default_factory is not None:
                value = info.get_default(call_default_factory=True)
                if isinstance(value, BaseModel):
                    return "see section"
                return literal(to_jsonable_python(value))
            if info.default is not PydanticUndefined:
                return literal(to_jsonable_python(info.default))
        return ""

    @staticmethod
    def description(node: Mapping[str, Any]) -> str:
        text = node.get("description")
        if not text:
            for key in ("anyOf", "oneOf"):
                for branch in node.get(key, []):
                    if isinstance(branch, dict) and branch.get("description"):
                        text = branch["description"]
                        break
        return one_line(text)

    def field_rows(self, definition: str, skip: AbstractSet[str] = frozenset()) -> list[list[str]]:
        rows: list[list[str]] = []
        for prop, node in self.defs[definition].get("properties", {}).items():
            if prop in skip:
                continue
            kind = self.type_text(node)
            limits = self.constraints(node)
            if limits:
                kind += "; " + ", ".join(limits)
            rows.append(
                [
                    code(prop),
                    kind,
                    self.default_text(definition, prop, node),
                    self.description(node),
                ]
            )
        return rows

    # ------------------------------------------------------------------ rendering
    def render_section(self, definition: str, level: int = 2) -> str:
        section = self.sections[definition]
        node = self.defs[definition]
        title = code(section.path) if section.path and section.category is None else None
        if title is None:
            title = definition if section.category is not None else "Top level"
        out = [f"{'#' * level} {title} {{#{self.anchor(definition)}}}\n"]
        if section.category is not None and section.path:
            out.append(f"Used by {section.path}.\n")
        if node.get("description") and definition != self.root_name:
            out.append(rst_to_md(node["description"]) + "\n")
        if section.also:
            out.append("Also used at: " + ", ".join(_path_label(p) for p in section.also) + ".\n")
        rows = self.field_rows(definition)
        if rows:
            out.append(table(["Key", "Type", "Default", "Description"], rows))
        return "\n".join(out)

    def render_sections(self, level: int = 2) -> str:
        """Every section reached from the root (excluding the root and component types)."""
        return "\n".join(self.render_section(d, level) for d in self._order if d != self.root_name)

    def render_category(self, category: Category, level: int = 2) -> str:
        out = [f"{'#' * level} {category.title} {{#{category.anchor}}}\n", category.intro + "\n"]
        common_anchor = f"{category.key}-common"
        out.append(f"{'#' * (level + 1)} Common fields {{#{common_anchor}}}\n")
        out.append(f"Every {category.key} type accepts these fields.\n")
        sample = next(iter(category.variants), None)
        if sample is not None:
            rows = [r for r in self._base_rows(category, sample) if r[0] != code("type")]
            out.append(table(["Key", "Type", "Default", "Description"], rows))
        for definition, variant in sorted(category.variants.items(), key=lambda kv: kv[1].name):
            out.append(self._render_variant(category, definition, variant, level + 1))
        return "\n".join(out)

    def _base_rows(self, category: Category, definition: str) -> list[list[str]]:
        model = category.variants[definition].model or category.base
        common = set(self.defs[definition].get("properties", {}))
        common -= _specific_fields(category, model)
        rows = self.field_rows(definition)
        return [r for r in rows if r[0].strip("`") in common]

    def _render_variant(
        self, category: Category, definition: str, variant: Variant, level: int
    ) -> str:
        model = variant.model or category.base
        specific = _specific_fields(category, model)
        skip = {p for p in self.defs[definition].get("properties", {}) if p not in specific}
        skip.add("type")
        out = [f"{'#' * level} `{variant.name}` {{#{category.variant_anchor(variant)}}}\n"]
        lines = []
        if variant.summary:
            lines.append(variant.summary.rstrip(".") + ".")
        doc = rst_to_md(model.__doc__) if model is not category.base else ""
        if doc and one_line(doc).rstrip(".") != variant.summary.rstrip("."):
            lines.append(doc)
        out.append("\n\n".join(lines) + "\n" if lines else "")
        facts = [f"Status: **{variant.status}**"] if variant.status else []
        if variant.extra:
            facts.append(f"install: `pip install 'dvf-framework[{variant.extra}]'`")
        if facts:
            out.append("; ".join(facts) + ".\n")
        rows = self.field_rows(definition, skip)
        if rows:
            out.append(table(["Key", "Type", "Default", "Description"], rows))
        else:
            out.append(f"No fields besides the [common fields](#{category.key}-common).\n")
        for nested in self._variant_nested.get(definition, []):
            out.append(self.render_section(nested, level + 1))
        return "\n".join(part for part in out if part)


def _specific_fields(category: Category, model: type[BaseModel]) -> set[str]:
    """Fields of a variant model declared below the category's base model."""
    return {
        info.alias or name
        for name, info in model.model_fields.items()
        if not issubclass(category.base, own_field_owner(model, name))
    }


def _path_label(path: str) -> str:
    return path if "`" in path else code(path)
