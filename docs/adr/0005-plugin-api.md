# ADR 0005: Plugin API: registry, entry points, and pluggy hooks

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

Migration teams need custom checks, connectors, normalizers, and report sections without forking the framework. Built-in components should not have privileged access that plugins lack, otherwise the plugin API rots.

## Decision

1. **One registry** keyed by kind and name. Kinds: `connector`, `check`, `comparator`, `normalizer`, `reporter`, `notifier`, `secret_provider`, `engine`, `sink`. Names are lowercase and may be namespaced with dots (`acme.vat_number_valid`).
2. **Three ways to register:**
   - Built-ins, registered at start-up by `dvf.plugins.builtins`.
   - Installed packages, through entry point groups named `dvf.<kind>s` (for example `dvf.checks`).
   - Project-local Python files, folders, or modules named in configuration or on the command line, using decorators such as `@register_check`.
3. **Declared versus ready.** An entry may carry only a name, description, and config model (*declared*) or also an implementation (*ready*). Phase 0 declares the built-ins so that configuration validation and the JSON Schema are complete before execution exists. `dvf plugins list` shows the status honestly.
4. **Config models.** Components with configuration provide a Pydantic model that subclasses the matching base (`CheckConfig`, `ConnectionConfig`, `NotificationConfig`). The exported JSON Schema includes every registered model.
5. **Lifecycle hooks** use pluggy with the project name `dvf` and entry point group `dvf.hooks`. Phase 0 calls `on_config_loaded`; the remaining hookspecs are defined now and called as later phases add execution.
6. **Compatibility.** `PLUGIN_API_VERSION` follows SemVer. A plugin may declare `dvf_plugin_api` as a version specifier; incompatible plugins are refused with a clear message.
7. **Isolation.** A plugin that fails to import or register is recorded as a load failure and reported; other plugins continue to load.
8. **Duplicates** are errors unless the same object is registered twice (idempotent) or the caller passes `replace_existing=True`.

## Consequences

- The public plugin surface is `dvf.plugins` and `dvf.models`. Everything else is internal and may change.
- Before 1.0, the plugin API is `0.x` and may change between minor versions. Phase 1 will add `plan()` and `evaluate()` to the `Check` base class; plugins written against Phase 0 will need updating.
- Loading local plugins runs project code. This is intended (the configuration is trusted), and the docs say so.
