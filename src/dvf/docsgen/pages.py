"""The configuration, results, and components reference pages."""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel

from dvf.config.checks import CheckConfig
from dvf.config.connections import ConnectionConfig
from dvf.config.models import CONFIG_VERSION, DvfConfig, SinkConfig
from dvf.config.notifications import NotificationConfig
from dvf.config.schema import _SUFFIX, _camel, config_json_schema, results_json_schema
from dvf.docsgen.markdown import GENERATED_NOTE, code, literal, one_line, table
from dvf.docsgen.schemadoc import Category, SchemaDocument, Variant, model_classes
from dvf.models.results import RESULTS_SCHEMA_VERSION, RunResult
from dvf.plugins.hooks import HookSpecs
from dvf.plugins.registry import PLUGIN_API_VERSION, PluginEntry, PluginKind, Registry

_CATEGORY_TEXT: dict[PluginKind, tuple[str, str, type[BaseModel], str]] = {
    PluginKind.CONNECTOR: (
        "connection",
        "Connection types",
        ConnectionConfig,
        "Each entry of `connections` is selected by its `type`. The connector guides explain "
        "how to connect: [Connectors](../connectors.md), [Files](../files.md), "
        "[NoSQL](../nosql.md), [SAP](../sap.md), and [Spark](../spark.md). Keep credentials in "
        "`${secret:...}` references.",
    ),
    PluginKind.CHECK: (
        "check",
        "Check types",
        CheckConfig,
        "Each entry of a table's `checks` list is selected by its `type`. "
        "[Checks and comparisons](../validations.md) explains what each one verifies; "
        "[Migration validations](../migration.md) and [Files](../files.md) cover the pack "
        "checks.",
    ),
    PluginKind.NOTIFIER: (
        "notification",
        "Notification types",
        NotificationConfig,
        "Each entry of `notifications` is selected by its `type`. See "
        "[Notifications](../notifications.md).",
    ),
}


def _categories(registry: Registry) -> list[Category]:
    categories = []
    for kind, (key, title, base, intro) in _CATEGORY_TEXT.items():
        category = Category(key=key, title=title, base=base, intro=intro)
        for entry in registry.entries(kind):
            definition = f"{_camel(entry.name)}{_SUFFIX[kind]}"
            category.variants[definition] = Variant(
                name=entry.name,
                definition=definition,
                summary=one_line(entry.description),
                status=entry.status,
                extra=entry.extra,
                model=entry.config_model or base,
            )
        categories.append(category)
    return categories


def _config_classes(registry: Registry) -> dict[str, type[BaseModel]]:
    roots: list[type[BaseModel]] = [DvfConfig, SinkConfig]
    for entry in registry.entries():
        if entry.config_model is not None:
            roots.append(entry.config_model)
    return model_classes(roots)


def configuration_document(registry: Registry) -> SchemaDocument:
    """The configuration schema prepared for rendering (also used by tests)."""
    return SchemaDocument(
        config_json_schema(registry),
        _config_classes(registry),
        root_name="DvfConfig",
        categories=_categories(registry),
    )


def configuration_page(registry: Registry) -> str:
    """``docs/reference/configuration.md``: every section, field, and component type."""
    doc = configuration_document(registry)
    intro = f"""# Configuration reference

{GENERATED_NOTE.format(source="the configuration models and the component registry")}
Every key a dvf configuration file may contain (configuration version {CONFIG_VERSION}), with its
type, default, allowed values, and limits. [Configuration](../configuration.md) explains the file
formats, layering, interpolation, and inheritance; this page is the exhaustive list.

- **Key** is the name in the file. Unknown keys are errors, with a "did you mean" suggestion.
- **Type** links to the section describing an object. "one of" lists the allowed values.
- **Default** is used when the key is omitted; **required** keys have none. Table and suite
  settings left out inherit from `defaults` (see [Inheritance](../configuration.md#inheritance)).
- Numbers, booleans, and strings with a pattern may also be written as a `${{env:...}}`,
  `${{var:...}}`, or `${{secret:...}}` placeholder; it is resolved before validation.

The same information drives editor autocomplete: `dvf schema config` writes the JSON Schema, and
`dvf init` saves it next to a new configuration.

"""
    body = [intro, doc.render_section("DvfConfig"), doc.render_sections()]
    body.extend(doc.render_category(category) for category in doc.categories)
    return _finish("\n".join(body))


def results_page() -> str:
    """``docs/reference/results.md``: the canonical results document (``results.json``)."""
    doc = SchemaDocument(results_json_schema(), model_classes([RunResult]), root_name="RunResult")
    intro = f"""# Results schema

{GENERATED_NOTE.format(source="the result models")}
Every report is rendered from one results document, `results.json`, written by every run (see
[ADR 0004](../adr/0004-canonical-result-model.md)). This page lists its fields; the JSON Schema is
committed as `schemas/results.schema.json` and printed by `dvf schema results`. The schema
version (currently `{RESULTS_SCHEMA_VERSION}`, in `schema_version`) follows SemVer independently
of the dvf version: fields may be added in a minor version, never removed or changed.

Read a results document in Python with `dvf.api.load_run(...)` or
`dvf.models.RunResult.model_validate_json(...)`.

"""
    return _finish("\n".join([intro, doc.render_section("RunResult"), doc.render_sections()]))


# ---------------------------------------------------------------------- components

_KIND_TEXT: dict[PluginKind, tuple[str, str]] = {
    PluginKind.CONNECTOR: (
        "Connectors",
        "Values of `type` in `connections`. Settings: [connection types]"
        "(configuration.md#connection-types).",
    ),
    PluginKind.CHECK: (
        "Checks",
        "Values of `type` in a table's `checks`. Settings: [check types]"
        "(configuration.md#check-types). Schema checks (`schema_checks`) are listed in "
        "[Checks and comparisons](../validations.md#schema-checks).",
    ),
    PluginKind.COMPARATOR: (
        "Comparison strategies",
        "Values of `comparison.strategies` and `comparison.on_mismatch`.",
    ),
    PluginKind.NORMALIZER: (
        "Normalizers",
        "Values of `normalize` on keys and column mappings: `rtrim`, `{round: 2}`, or "
        "`{name: round, params: {digits: 2}}`.",
    ),
    PluginKind.REPORTER: ("Report formats", "Values of `reports.formats` and `dvf run --format`."),
    PluginKind.NOTIFIER: (
        "Notification channels",
        "Values of `type` in `notifications`. Settings: [notification types]"
        "(configuration.md#notification-types).",
    ),
    PluginKind.SECRET_PROVIDER: (
        "Secret providers",
        "Providers of `${secret:<provider>:<path>#<key>}` references.",
    ),
    PluginKind.ENGINE: ("Engines", "Values of `engine` in `defaults`, suites, and tables."),
    PluginKind.SINK: (
        "Result sinks",
        "Values of `type` in `sinks`. Settings: [sinks](configuration.md#sinkconfig).",
    ),
}


def _settings(entry: PluginEntry) -> str:
    if entry.kind is PluginKind.CONNECTOR:
        return f"[settings](configuration.md#connection-{entry.name.replace('.', '-')})"
    if entry.kind is PluginKind.CHECK:
        return f"[settings](configuration.md#check-{entry.name.replace('.', '-')})"
    if entry.kind is PluginKind.NOTIFIER:
        return f"[settings](configuration.md#notification-{entry.name.replace('.', '-')})"
    params = getattr(entry.implementation, "params_model", None)
    if inspect.isclass(params) and issubclass(params, BaseModel):
        return ", ".join(_param(name, info) for name, info in params.model_fields.items())
    return ""


def _param(name: str, info: Any) -> str:
    text = code(name)
    if not info.is_required():
        default = info.get_default(call_default_factory=True)
        text += f" (default {literal(default)})"
    return text


def components_page(registry: Registry) -> str:
    """``docs/reference/components.md``: every registered component, by kind."""
    entries = registry.entries()
    counts = {s: sum(1 for e in entries if e.status == s) for s in ("ready", "beta", "declared")}
    out = [
        f"""# Components

{GENERATED_NOTE.format(source="the component registry")}
Every built-in component, as `dvf plugins list` shows it (plugin API {PLUGIN_API_VERSION}):
{len(entries)} components, {counts["ready"]} ready, {counts["beta"]} beta, and
{counts["declared"]} declared.

| Status | Meaning |
|---|---|
| ready | Implemented and verified. |
| beta | Implemented, but not yet verified against a live system. Feedback is welcome. |
| declared | Configurations validate, but the implementation is not in this version; using it is an error that says so. |

**Extra** is the optional dependency group that provides the driver:
`pip install 'dvf-framework[<extra>]'`. Installed plugins add their own components; see
[Plugins](../plugins.md) to write one.
"""
    ]
    for kind in PluginKind:
        title, used = _KIND_TEXT[kind]
        out.append(f"## {title} {{#{kind.value}}}\n")
        out.append(
            f"{used} Kind `{kind.value}`; plugins register through the entry point group "
            f"`{kind.entry_point_group}`.\n"
        )
        rows = [
            [
                code(e.name),
                e.status,
                code(e.extra) if e.extra else "",
                one_line(e.description),
                _settings(e),
            ]
            for e in registry.entries(kind)
        ]
        if rows:
            out.append(table(["Name", "Status", "Extra", "Description", "Settings"], rows))
        else:
            out.append("None are built in.\n")
    out.append(_hooks_section())
    return _finish("\n".join(out))


def _hooks_section() -> str:
    rows = []
    for name, member in inspect.getmembers(HookSpecs, inspect.isfunction):
        params = [p for p in inspect.signature(member).parameters if p != "self"]
        doc = inspect.getdoc(member) or ""
        first = doc.split("\n\n", 1)[0]
        rows.append([code(f"{name}({', '.join(params)})"), one_line(first)])
    return (
        "## Lifecycle hooks {#hooks}\n\n"
        "Plugins implement any of these with `@dvf.plugins.hookimpl` and register through the "
        "entry point group `dvf.hooks` or a local plugin path. Arguments are passed by keyword, "
        "so an implementation may accept only the ones it needs. See "
        "[Plugins](../plugins.md#lifecycle-hooks).\n\n" + table(["Hook", "Called"], rows)
    )


def _finish(text: str) -> str:
    """Normalize blank lines: at most one in a row, and one newline at the end."""
    lines: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line and lines and not lines[-1]:
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"
