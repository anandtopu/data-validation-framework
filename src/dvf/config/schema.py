"""JSON Schema export for configuration (including plugin-provided types) and results."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, create_model

from dvf.config.base import Identifier
from dvf.config.checks import CheckConfig
from dvf.config.connections import ConnectionConfig
from dvf.config.models import CONFIG_VERSION, DvfConfig, SuiteConfig, TableConfig
from dvf.config.notifications import NotificationConfig
from dvf.models.results import RESULTS_SCHEMA_VERSION, RunResult
from dvf.plugins.registry import PluginKind, Registry, get_registry

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_SUFFIX = {
    PluginKind.CONNECTOR: "ConnectionConfig",
    PluginKind.CHECK: "CheckConfig",
    PluginKind.NOTIFIER: "NotificationConfig",
}


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)


def _variants(registry: Registry, kind: PluginKind, base: type[BaseModel]) -> Any:
    """A discriminated union of every registered config model for ``kind``, keyed by ``type``."""
    variants: list[type[BaseModel]] = []
    for entry in registry.entries(kind):
        model = entry.config_model or base
        variant = create_model(
            f"{_camel(entry.name)}{_SUFFIX[kind]}",
            __base__=model,
            __doc__=entry.description or model.__doc__,
            type=(Literal[entry.name], Field(description=f"Use '{entry.name}'.")),  # type: ignore[arg-type]
        )
        variants.append(variant)
    if not variants:
        return base
    if len(variants) == 1:
        return variants[0]
    return Annotated[Union[tuple(variants)], Field(discriminator="type")]  # noqa: UP007


def config_json_schema(registry: Registry | None = None) -> dict[str, Any]:
    """The JSON Schema of the configuration file format, for IDE autocomplete and validation."""
    registry = registry or get_registry()
    check_union = _variants(registry, PluginKind.CHECK, CheckConfig)
    connection_union = _variants(registry, PluginKind.CONNECTOR, ConnectionConfig)
    notification_union = _variants(registry, PluginKind.NOTIFIER, NotificationConfig)

    table = create_model(
        "TableConfig",
        __base__=TableConfig,
        __doc__=TableConfig.__doc__,
        checks=(list[check_union], Field(default_factory=list)),  # type: ignore[valid-type]
    )
    suite = create_model(
        "SuiteConfig",
        __base__=SuiteConfig,
        __doc__=SuiteConfig.__doc__,
        tables=(list[table], Field(default_factory=list)),
    )
    root = create_model(
        "DvfConfig",
        __base__=DvfConfig,
        __doc__=DvfConfig.__doc__,
        connections=(dict[Identifier, connection_union], Field(default_factory=dict)),  # type: ignore[valid-type]
        suites=(list[suite], Field(default_factory=list)),
        notifications=(list[notification_union], Field(default_factory=list)),  # type: ignore[valid-type]
    )
    schema = _allow_interpolation(root.model_json_schema(mode="validation"))
    schema.setdefault("$defs", {})[_INTERPOLATION_DEF] = {
        "type": "string",
        "pattern": r"\$\{[^{}]+\}",
        "description": "A ${env:...}, ${var:...}, or ${secret:...} placeholder, resolved at load time.",
    }
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "title": "dvf configuration",
        "x-dvf-config-version": CONFIG_VERSION,
        **{k: v for k, v in schema.items() if k != "title"},
    }


_INTERPOLATION_DEF = "Interpolation"
_RELAXED_TYPES = {"integer", "number", "boolean"}


def _allow_interpolation(node: Any) -> Any:
    """Let typed values also be ``${...}`` placeholders, as the loader interpolates before validating.

    Numbers, booleans, and strings with a pattern or format accept a placeholder string.
    Enumerations and constants (such as discriminating ``type`` values) are left strict.
    """
    if isinstance(node, list):
        return [_allow_interpolation(item) for item in node]
    if not isinstance(node, dict):
        return node
    relaxed = {key: _allow_interpolation(value) for key, value in node.items()}
    kind = relaxed.get("type") if isinstance(relaxed.get("type"), str) else None
    is_constrained_string = kind == "string" and ("pattern" in relaxed or "format" in relaxed)
    if (
        (kind in _RELAXED_TYPES or is_constrained_string)
        and "const" not in relaxed
        and "enum" not in relaxed
    ):
        return {"anyOf": [relaxed, {"$ref": f"#/$defs/{_INTERPOLATION_DEF}"}]}
    return relaxed


def results_json_schema() -> dict[str, Any]:
    """The JSON Schema of the results document (``RunResult``)."""
    schema = RunResult.model_json_schema(mode="serialization")
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "title": "dvf results",
        "x-dvf-results-schema-version": RESULTS_SCHEMA_VERSION,
        **{k: v for k, v in schema.items() if k != "title"},
    }
