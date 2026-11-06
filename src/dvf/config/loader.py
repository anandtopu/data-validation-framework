"""The configuration loading pipeline (ARCHITECTURE.md, ADR 0006)."""

from __future__ import annotations

import copy
import dataclasses
import difflib
import fnmatch
import os
import types
import typing
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ValidationError

from dvf.config.checks import CheckConfig
from dvf.config.connections import ConnectionConfig
from dvf.config.diagnostics import (
    Code,
    Diagnostic,
    LocationMap,
    SourceLocation,
    ValuePath,
    format_path,
    resolve_path,
)
from dvf.config.formats import (
    SUPPORTED_SUFFIXES,
    parse_key_path,
    parse_scalar,
    read_config_file,
    set_path,
)
from dvf.config.interpolation import Interpolator
from dvf.config.merge import deep_merge, merge_layer
from dvf.config.models import (
    CONFIG_VERSION,
    INHERITED_TABLE_FIELDS,
    DefaultsConfig,
    DvfConfig,
)
from dvf.config.notifications import NotificationConfig
from dvf.core.enums import DiagnosticLevel
from dvf.core.errors import ConfigError, PluginError
from dvf.models.results import make_check_id
from dvf.plugins.local import load_local_plugins
from dvf.plugins.registry import PluginKind, Registry, get_registry
from dvf.security.secrets import SecretContext, SecretResolver

ENV_OVERRIDE_PREFIX = "DVF__"


@dataclass(frozen=True)
class LoadOptions:
    """How to load a configuration."""

    profile: str | None = None
    """Environment profile to apply (in-file ``profiles.<name>`` and ``<stem>.<name>.<ext>``)."""
    overrides: Sequence[str] = ()
    """``path=value`` overrides, applied last."""
    variables: Mapping[str, str] = field(default_factory=dict)
    """Values for ``${var:name}``, overriding the ``vars:`` section."""
    env_file: Path | None = None
    """A .env file; defaults to ``.env`` next to the configuration file when it exists."""
    environ: Mapping[str, str] | None = None
    """Environment to read; defaults to ``os.environ``."""
    strict: bool = True
    """Missing environment variables and unresolvable secrets are errors (else warnings)."""
    resolve_secrets: bool = True
    """Fetch secret values. When false, only reference syntax and providers are checked."""
    plugins: Sequence[str] = ()
    """Extra plugin paths or modules (from the command line)."""
    local_override: bool = True
    """Apply ``<stem>.local.<ext>`` when it exists."""
    env_overrides: bool = True
    """Apply ``DVF__SECTION__KEY`` environment variables."""


@dataclass
class LoadedConfig:
    """A validated configuration with everything needed to explain it."""

    config: DvfConfig
    diagnostics: list[Diagnostic]
    files: list[Path]
    base_dir: Path
    registry: Registry
    locations: LocationMap
    profile: str | None = None

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if not d.is_error]


def load_config(
    path: str | os.PathLike[str],
    options: LoadOptions | None = None,
    *,
    registry: Registry | None = None,
) -> LoadedConfig:
    """Load, layer, interpolate, and validate a configuration file.

    Raises :class:`ConfigError` listing every error found. Warnings are returned on the result.
    """
    return _Loader(options or LoadOptions(), registry or get_registry()).load_file(Path(path))


def load_config_data(
    data: Mapping[str, Any],
    options: LoadOptions | None = None,
    *,
    registry: Registry | None = None,
    base_dir: Path | None = None,
    source_name: str = "<memory>",
) -> LoadedConfig:
    """Run the pipeline (minus file layering) on an in-memory mapping."""
    loader = _Loader(options or LoadOptions(), registry or get_registry())
    return loader.load_data(dict(data), base_dir or Path.cwd(), source_name)


class _Loader:
    def __init__(self, options: LoadOptions, registry: Registry) -> None:
        self.options = options
        self.registry = registry
        self.environ: Mapping[str, str] = (
            options.environ if options.environ is not None else os.environ
        )
        self.diagnostics: list[Diagnostic] = []
        self.locations = LocationMap()
        self.files: list[Path] = []
        self._failed_paths: list[ValuePath] = []

    # ------------------------------------------------------------------ entry points
    def load_file(self, path: Path) -> LoadedConfig:
        path = path.resolve()
        self.locations.default_file = str(path)
        data = self._read_with_extends(path, ())
        data = self._apply_profile(data, path)
        if self.options.local_override:
            local = path.with_name(f"{path.stem}.local{path.suffix}")
            if local.is_file():
                data = self._merge_file(data, local)
        return self._finish(data, path.parent)

    def load_data(self, data: dict[str, Any], base_dir: Path, source_name: str) -> LoadedConfig:
        self.locations.default_file = source_name
        data.pop("extends", None)
        profiles = data.pop("profiles", None)
        if self.options.profile:
            overlay = (profiles or {}).get(self.options.profile)
            if not isinstance(overlay, Mapping):
                self._fail(Code.PROFILE_NOT_FOUND, f"Profile '{self.options.profile}' not found.")
            data = deep_merge(data, overlay)
        return self._finish(data, base_dir)

    # ------------------------------------------------------------------ layering
    def _read(self, path: Path) -> tuple[dict[str, Any], LocationMap]:
        data, locations = read_config_file(path)
        self.files.append(path)
        return data, locations

    def _read_with_extends(self, path: Path, stack: tuple[Path, ...]) -> dict[str, Any]:
        data, locations = self._read(path)
        extends = data.pop("extends", [])
        if isinstance(extends, str):
            extends = [extends]
        if not isinstance(extends, list) or not all(isinstance(e, str) for e in extends):
            self._fail(
                Code.EXTENDS_ERROR,
                "'extends' must be a file path or a list of paths.",
                ("extends",),
                locations,
            )
        merged: dict[str, Any] = {}
        merged_locations = LocationMap(default_file=str(path))
        for entry in extends:
            parent = (path.parent / entry).resolve()
            if parent in (*stack, path):
                chain = " -> ".join(str(p) for p in (*stack, path, parent))
                self._fail(
                    Code.EXTENDS_ERROR, f"Circular 'extends': {chain}", ("extends",), locations
                )
            if not parent.is_file():
                self._fail(
                    Code.EXTENDS_ERROR,
                    f"Extended file not found: {parent}",
                    ("extends",),
                    locations,
                )
            outer = self.locations
            self.locations = LocationMap(default_file=str(parent))
            parent_data = self._read_with_extends(parent, (*stack, path))
            parent_locations, self.locations = self.locations, outer
            merged = merge_layer(merged, merged_locations, parent_data, parent_locations)
        merged = merge_layer(merged, merged_locations, data, locations)
        self.locations.overlay(merged_locations)
        return merged

    def _merge_file(self, data: dict[str, Any], path: Path) -> dict[str, Any]:
        overlay, overlay_locations = self._read(path)
        return merge_layer(data, self.locations, overlay, overlay_locations)

    def _apply_profile(self, data: dict[str, Any], path: Path) -> dict[str, Any]:
        profiles = data.pop("profiles", None)
        name = self.options.profile
        if not name:
            return data
        found = False
        if isinstance(profiles, Mapping) and isinstance(profiles.get(name), Mapping):
            found = True
            prefix: ValuePath = ("profiles", name)
            shifted = LocationMap(default_file=self.locations.default_file)
            for loc_path, location in list(self.locations.items()):
                if loc_path[: len(prefix)] == prefix:
                    shifted.set(loc_path[len(prefix) :], location)
            data = merge_layer(data, self.locations, profiles[name], shifted)
        candidates = [path.with_name(f"{path.stem}.{name}{path.suffix}")]
        candidates += [
            path.with_name(f"{path.stem}.{name}{s}") for s in SUPPORTED_SUFFIXES if s != path.suffix
        ]
        for candidate in candidates:
            if candidate.is_file():
                found = True
                data = self._merge_file(data, candidate)
                break
        if not found:
            self._fail(
                Code.PROFILE_NOT_FOUND,
                f"Profile '{name}' not found: no 'profiles.{name}' section and no "
                f"{path.stem}.{name}.* file next to {path.name}.",
            )
        return data

    def _apply_overrides(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.options.env_overrides:
            for key in sorted(self.environ):
                if not key.startswith(ENV_OVERRIDE_PREFIX):
                    continue
                segments = [s.lower() for s in key[len(ENV_OVERRIDE_PREFIX) :].split("__")]
                if not all(segments):
                    self._warn(
                        Code.INVALID_OVERRIDE, f"Ignoring malformed override variable {key}."
                    )
                    continue
                path = tuple(int(s) if s.isdigit() else s for s in segments)
                self._set_override(data, path, self.environ[key], SourceLocation(f"env:{key}"))
        for override in self.options.overrides:
            raw_path, sep, raw_value = override.partition("=")
            if not sep:
                self._error(
                    Code.INVALID_OVERRIDE, f"Override '{override}' must look like path=value."
                )
                continue
            try:
                path = parse_key_path(raw_path)
            except ValueError as exc:
                self._error(Code.INVALID_OVERRIDE, f"Invalid override '{override}': {exc}.")
                continue
            self._set_override(data, path, raw_value, SourceLocation(f"--set {raw_path.strip()}"))
        return data

    def _set_override(
        self, data: dict[str, Any], path: ValuePath, raw: str, location: SourceLocation
    ) -> None:
        try:
            set_path(data, path, parse_scalar(raw))
        except ValueError as exc:
            self._error(Code.INVALID_OVERRIDE, f"Cannot apply override {location.file}: {exc}.")
            return
        self.locations.drop_subtree(path)
        self.locations.set(path, location)

    # ------------------------------------------------------------------ pipeline
    def _finish(self, data: dict[str, Any], base_dir: Path) -> LoadedConfig:
        data = self._apply_overrides(data)
        self._load_plugins(data, base_dir)
        data = self._interpolate(data, base_dir)
        self._apply_inheritance(data)
        self._validate_polymorphic(data)
        config = self._validate_root(data)
        if config is not None:
            self._cross_check(config)
        self.diagnostics = _deduplicate(self.diagnostics)
        errors = [d for d in self.diagnostics if d.is_error]
        if errors or config is None:
            count = len(errors) or 1
            raise ConfigError(
                f"The configuration has {count} error{'s' if count != 1 else ''}.", self.diagnostics
            )
        try:
            self.registry.hooks.hook.on_config_loaded(config=config)
        except Exception as exc:
            raise PluginError(
                f"An on_config_loaded hook failed: {type(exc).__name__}: {exc}"
            ) from exc
        return LoadedConfig(
            config=config,
            diagnostics=self.diagnostics,
            files=self.files,
            base_dir=base_dir,
            registry=self.registry,
            locations=self.locations,
            profile=self.options.profile,
        )

    def _load_plugins(self, data: dict[str, Any], base_dir: Path) -> None:
        specs: list[str] = list(self.options.plugins)
        section = data.get("plugins")
        if isinstance(section, Mapping):
            for key in ("paths", "modules"):
                values = section.get(key)
                if isinstance(values, list):
                    specs.extend(str(v) for v in values)
        try:
            load_local_plugins(self.registry, specs, base_dir)
        except PluginError as exc:
            self._error(Code.PLUGIN_LOAD, str(exc), ("plugins",))
        for failure in self.registry.load_failures:
            self._warn(Code.PLUGIN_LOAD, f"Plugin {failure.source} failed to load: {failure.error}")

    def _interpolate(self, data: dict[str, Any], base_dir: Path) -> dict[str, Any]:
        env_file = self.options.env_file or (base_dir / ".env")
        dotenv: dict[str, str] = {}
        if env_file.is_file():
            dotenv = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
        elif self.options.env_file is not None:
            self._error(Code.FILE_NOT_FOUND, f"The .env file '{env_file}' does not exist.")
        context = SecretContext(environ=self.environ, dotenv=dotenv, base_dir=base_dir)
        interpolator = Interpolator(
            environ=self.environ,
            dotenv=dotenv,
            secrets=SecretResolver(self.registry, context),
            locations=self.locations,
            strict=self.options.strict,
            resolve_secrets=self.options.resolve_secrets,
        )
        raw_vars = data.get("vars")
        variables = interpolator.interpolate_vars(raw_vars) if isinstance(raw_vars, Mapping) else {}
        variables.update(self.options.variables)
        interpolator.variables = variables
        result = {
            key: (
                variables
                if key == "vars" and isinstance(raw_vars, Mapping)
                else interpolator.interpolate(value, (key,))
            )
            for key, value in data.items()
        }
        if self.options.variables and "vars" not in result:
            result["vars"] = dict(self.options.variables)
        self.diagnostics.extend(interpolator.diagnostics)
        return result

    def _apply_inheritance(self, data: dict[str, Any]) -> None:
        project_defaults = self._effective_defaults(
            data.get("defaults", {}), DefaultsConfig().model_dump(), ("defaults",)
        )
        suites = data.get("suites")
        if not isinstance(suites, list):
            return
        for i, suite in enumerate(suites):
            if not isinstance(suite, dict):
                continue
            suite_path: ValuePath = ("suites", i)
            self.locations.copy_subtree(("defaults",), (*suite_path, "defaults"))
            effective = self._effective_defaults(
                suite.get("defaults", {}),
                project_defaults,
                (*suite_path, "defaults"),
                base_dump=project_defaults,
            )
            suite["defaults"] = effective
            tables = suite.get("tables")
            if not isinstance(tables, list):
                continue
            for j, table in enumerate(tables):
                if not isinstance(table, dict):
                    continue
                table_path: ValuePath = (*suite_path, "tables", j)
                for name in INHERITED_TABLE_FIELDS:
                    inherited = effective.get(name)
                    if name in table:
                        if isinstance(inherited, Mapping) and isinstance(table[name], Mapping):
                            table[name] = deep_merge(inherited, table[name])
                    else:
                        table[name] = copy.deepcopy(inherited)
                    self.locations.copy_subtree(
                        (*suite_path, "defaults", name), (*table_path, name)
                    )
                checks = table.get("checks")
                if isinstance(checks, list):
                    default_side = "target" if suite.get("target") is not None else "source"
                    for check in checks:
                        if isinstance(check, dict):
                            check.setdefault("severity", table.get("severity"))
                            check.setdefault("side", default_side)

    def _effective_defaults(
        self,
        raw: Any,
        fallback: dict[str, Any],
        path: ValuePath,
        base_dump: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            return fallback  # root validation reports the wrong type
        merged = deep_merge(base_dump, raw) if base_dump is not None else dict(raw)
        try:
            return DefaultsConfig.model_validate(merged).model_dump()
        except ValidationError as exc:
            self._add_validation_errors(exc, path, DefaultsConfig, raw_root=merged)
            self._failed_paths.append(path)
            return fallback

    def _validate_polymorphic(self, data: dict[str, Any]) -> None:
        connections = data.get("connections")
        if isinstance(connections, dict):
            for name, raw in list(connections.items()):
                connections[name] = self._validate_component(
                    PluginKind.CONNECTOR, ConnectionConfig, raw, ("connections", name)
                )
        notifications = data.get("notifications")
        if isinstance(notifications, list):
            for k, raw in enumerate(notifications):
                notifications[k] = self._validate_component(
                    PluginKind.NOTIFIER, NotificationConfig, raw, ("notifications", k)
                )
        suites = data.get("suites")
        if not isinstance(suites, list):
            return
        for i, suite in enumerate(suites):
            tables = suite.get("tables") if isinstance(suite, dict) else None
            if not isinstance(tables, list):
                continue
            for j, table in enumerate(tables):
                checks = table.get("checks") if isinstance(table, dict) else None
                if not isinstance(checks, list):
                    continue
                for k, raw in enumerate(checks):
                    checks[k] = self._validate_component(
                        PluginKind.CHECK, CheckConfig, raw, ("suites", i, "tables", j, "checks", k)
                    )

    def _validate_component(
        self, kind: PluginKind, base: type[BaseModel], raw: Any, path: ValuePath
    ) -> Any:
        if not isinstance(raw, Mapping):
            return raw  # root validation reports the wrong type
        type_name = raw.get("type")
        if not isinstance(type_name, str) or not type_name:
            self._error(
                Code.MISSING_FIELD,
                f"Missing required field 'type' for this {kind.value}.",
                (*path, "type"),
            )
            self._failed_paths.append(path)
            return raw
        entry = self.registry.find(kind, type_name)
        if entry is None:
            suggestions = self.registry.suggest(kind, type_name)
            hint = (
                f"Did you mean '{suggestions[0]}'?"
                if suggestions
                else f"Run 'dvf plugins list --kind {kind.value}'."
            )
            self._error(
                Code.UNKNOWN_COMPONENT,
                f"Unknown {kind.value} type '{type_name}'.",
                (*path, "type"),
                hint=hint,
            )
            self._failed_paths.append(path)
            return raw
        model = entry.config_model or base
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            self._add_validation_errors(exc, path, model, raw_root=raw)
            self._failed_paths.append(path)
            return raw

    def _validate_root(self, data: dict[str, Any]) -> DvfConfig | None:
        try:
            return DvfConfig.model_validate(data)
        except ValidationError as exc:
            self._add_validation_errors(exc, (), DvfConfig, raw_root=data)
            return None

    # ------------------------------------------------------------------ cross-references
    def _cross_check(self, config: DvfConfig) -> None:
        reg = self.registry
        connections = set(config.connections)

        def check_connection(name: str | None, path: ValuePath) -> None:
            if name is not None and name not in connections:
                suggestions = difflib.get_close_matches(name, sorted(connections), n=1)
                hint = (
                    f"Did you mean '{suggestions[0]}'?"
                    if suggestions
                    else "Define it under 'connections:'."
                )
                self._error(
                    Code.UNKNOWN_CONNECTION, f"Unknown connection '{name}'.", path, hint=hint
                )

        def check_component(kind: PluginKind, name: str | None, path: ValuePath) -> None:
            if name is not None and reg.find(kind, name) is None:
                suggestions = reg.suggest(kind, name)
                hint = (
                    f"Did you mean '{suggestions[0]}'?"
                    if suggestions
                    else f"Run 'dvf plugins list --kind {kind.value}'."
                )
                self._error(
                    Code.UNKNOWN_COMPONENT, f"Unknown {kind.value} '{name}'.", path, hint=hint
                )

        check_component(PluginKind.ENGINE, config.defaults.engine, ("defaults", "engine"))
        for k, fmt in enumerate(config.reports.formats):
            check_component(PluginKind.REPORTER, fmt, ("reports", "formats", k))

        suite_names: set[str] = set()
        table_ids: list[str] = []
        for i, suite in enumerate(config.suites):
            sp: ValuePath = ("suites", i)
            if suite.name in suite_names:
                self._error(
                    Code.DUPLICATE_NAME, f"Duplicate suite name '{suite.name}'.", (*sp, "name")
                )
            suite_names.add(suite.name)
            check_connection(suite.source, (*sp, "source"))
            check_connection(suite.target, (*sp, "target"))
            table_names: set[str] = set()
            for j, table in enumerate(suite.tables):
                tp: ValuePath = (*sp, "tables", j)
                table_ids.append(f"{suite.name}.{table.label}")
                if table.name is not None:
                    if table.name in table_names:
                        self._error(
                            Code.DUPLICATE_NAME,
                            f"Duplicate table name '{table.name}' in suite '{suite.name}'.",
                            (*tp, "name"),
                        )
                    table_names.add(table.name)
                    if suite.is_comparison and table.target is None:
                        self._error(
                            Code.SUITE_TARGET_MISMATCH,
                            f"Table '{table.name}' needs a 'target' because suite '{suite.name}' compares against '{suite.target}'.",
                            tp,
                        )
                if not suite.is_comparison and (
                    table.target is not None or (table.pattern and table.pattern.target)
                ):
                    self._error(
                        Code.SUITE_TARGET_MISMATCH,
                        f"Suite '{suite.name}' has no target connection, so table '{table.label}' cannot have a target.",
                        (*tp, "target"),
                    )
                for side in ("source", "target"):
                    spec = getattr(table, side)
                    if spec is not None:
                        check_connection(spec.connection, (*tp, side, "connection"))
                check_component(PluginKind.ENGINE, table.engine, (*tp, "engine"))
                for k, strategy in enumerate(table.comparison.strategies):
                    check_component(
                        PluginKind.COMPARATOR, strategy, (*tp, "comparison", "strategies", k)
                    )
                check_component(
                    PluginKind.COMPARATOR,
                    table.comparison.on_mismatch,
                    (*tp, "comparison", "on_mismatch"),
                )
                for k, key in enumerate(table.keys):
                    for n, ref in enumerate(key.normalize):
                        check_component(
                            PluginKind.NORMALIZER, ref.name, (*tp, "keys", k, "normalize", n)
                        )
                for k, mapping in enumerate(table.columns.mappings):
                    for n, ref in enumerate(mapping.normalize):
                        check_component(
                            PluginKind.NORMALIZER,
                            ref.name,
                            (*tp, "columns", "mappings", k, "normalize", n),
                        )
                seen_ids: dict[str, int] = {}
                for k, check in enumerate(table.checks):
                    cp: ValuePath = (*tp, "checks", k)
                    parent = getattr(check, "parent", None)
                    if parent is not None:
                        check_connection(
                            getattr(parent, "connection", None), (*cp, "parent", "connection")
                        )
                    if (
                        not suite.is_comparison
                        and check.side is not None
                        and check.side.value != "source"
                    ):
                        self._error(
                            Code.SUITE_TARGET_MISMATCH,
                            f"Check side '{check.side.value}' needs a target, but suite '{suite.name}' has none.",
                            (*cp, "side"),
                        )
                    check_id = make_check_id(
                        suite.name,
                        table.label,
                        check.type,
                        columns=check.target_columns(),
                        name=check.name,
                    )
                    if check_id in seen_ids:
                        self._error(
                            Code.DUPLICATE_CHECK_ID,
                            f"Checks {seen_ids[check_id]} and {k} would both have ID '{check_id}'.",
                            cp,
                            hint="Give one of them a distinct 'name'.",
                        )
                    seen_ids.setdefault(check_id, k)
        self._check_waivers(config, table_ids)

    def _check_waivers(self, config: DvfConfig, table_ids: Iterable[str]) -> None:
        ids = list(table_ids)
        for k, waiver in enumerate(config.waivers):
            wp: ValuePath = ("waivers", k)
            if waiver.is_expired():
                self._warn(
                    Code.WAIVER_EXPIRED,
                    f"Waiver for '{waiver.check}' expired on {waiver.expires}; it will fail the run until renewed or removed.",
                    (*wp, "expires"),
                )
            parts = waiver.check.split(".")
            prefix = ".".join(parts[:2]) if len(parts) >= 2 else waiver.check
            if not any(fnmatch.fnmatchcase(table_id, prefix) for table_id in ids):
                self._warn(
                    Code.WAIVER_UNMATCHED,
                    f"Waiver '{waiver.check}' does not match any configured suite and table.",
                    (*wp, "check"),
                )

    # ------------------------------------------------------------------ diagnostics
    def _add_validation_errors(
        self, exc: ValidationError, prefix: ValuePath, model: type[BaseModel], raw_root: Any
    ) -> None:
        for error in exc.errors(include_url=False):
            relative = resolve_path(raw_root, error["loc"])
            path = (*prefix, *relative)
            if prefix == () and any(path[: len(f)] == f for f in self._failed_paths):
                continue  # already reported against the specific component model
            err_type = error["type"]
            message = str(error["msg"])
            if message.startswith("Value error, "):
                message = message[len("Value error, ") :]
            hint = None
            code = Code.VALIDATION
            field_name = str(relative[-1]) if relative else ""
            if err_type == "missing":
                code = Code.MISSING_FIELD
                message = f"Missing required field '{field_name}'."
                if path == ("version",):
                    hint = f"Add 'version: {CONFIG_VERSION}' at the top of the file."
            elif err_type == "extra_forbidden":
                code = Code.UNKNOWN_FIELD
                message = f"Unknown field '{field_name}'."
                allowed = _allowed_fields(model, relative[:-1])
                if allowed:
                    close = difflib.get_close_matches(field_name, sorted(allowed), n=1, cutoff=0.6)
                    hint = f"Did you mean '{close[0]}'?" if close else None
            elif path == ("version",):
                code = Code.UNSUPPORTED_VERSION
            else:
                value = error.get("input")
                if isinstance(value, str | int | float | bool) and field_name:
                    shown = repr(value) if len(repr(value)) <= 60 else repr(value)[:57] + "...'"
                    message = f"Invalid value {shown} for '{field_name}': {message}"
            self._add(DiagnosticLevel.ERROR, code, message, path, hint=hint)

    def _add(
        self,
        level: DiagnosticLevel,
        code: Code,
        message: str,
        path: ValuePath = (),
        *,
        hint: str | None = None,
        locations: LocationMap | None = None,
    ) -> None:
        location = (locations or self.locations).get(path)
        self.diagnostics.append(
            Diagnostic(level, code, message, path=format_path(path), location=location, hint=hint)
        )

    def _error(
        self, code: Code, message: str, path: ValuePath = (), *, hint: str | None = None
    ) -> None:
        self._add(DiagnosticLevel.ERROR, code, message, path, hint=hint)

    def _warn(
        self, code: Code, message: str, path: ValuePath = (), *, hint: str | None = None
    ) -> None:
        self._add(DiagnosticLevel.WARNING, code, message, path, hint=hint)

    def _fail(
        self,
        code: Code,
        message: str,
        path: ValuePath = (),
        locations: LocationMap | None = None,
    ) -> typing.NoReturn:
        self._add(DiagnosticLevel.ERROR, code, message, path, locations=locations)
        raise ConfigError(message, self.diagnostics)


def _deduplicate(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Collapse identical problems reported at several inherited paths into one diagnostic."""
    groups: dict[tuple[Any, ...], list[Diagnostic]] = {}
    order: list[tuple[Any, ...]] = []
    for diagnostic in diagnostics:
        location = diagnostic.location
        if location is None or location.line is None:
            key: tuple[Any, ...] = (id(diagnostic),)
        else:
            key = (diagnostic.level, diagnostic.code, diagnostic.message, location)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(diagnostic)
    result: list[Diagnostic] = []
    for key in order:
        first, *rest = groups[key]
        if rest:
            note = f"Also inherited by {len(rest)} other setting{'s' if len(rest) != 1 else ''}."
            hint = f"{first.hint} {note}" if first.hint else note
            first = dataclasses.replace(first, hint=hint)
        result.append(first)
    return result


# ---------------------------------------------------------------------- model introspection


def _strip_annotation(annotation: Any) -> Any:
    """Remove Annotated, Optional, and union wrappers, keeping a model or container type."""
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
            continue
        if origin in (typing.Union, types.UnionType):
            candidates = [a for a in typing.get_args(annotation) if a is not type(None)]
            preferred = [
                a
                for a in candidates
                if typing.get_origin(a) in (list, dict)
                or (isinstance(a, type) and issubclass(a, BaseModel))
            ]
            annotation = (preferred or candidates or [annotation])[0]
            continue
        return annotation


def _allowed_fields(model: type[BaseModel], loc: Sequence[Any]) -> set[str] | None:
    """Field names (and aliases) accepted at ``loc`` below ``model``."""
    current: Any = model
    for segment in loc:
        current = _strip_annotation(current)
        origin = typing.get_origin(current)
        if origin in (list, tuple):
            current = typing.get_args(current)[0]
            continue
        if origin is dict:
            current = typing.get_args(current)[1]
            continue
        if isinstance(current, type) and issubclass(current, BaseModel):
            match = next(
                (
                    f
                    for name, f in current.model_fields.items()
                    if name == segment or f.alias == segment
                ),
                None,
            )
            if match is None:
                return None
            current = match.annotation
            continue
        return None
    current = _strip_annotation(current)
    if typing.get_origin(current) in (list, tuple, dict):
        return None
    if isinstance(current, type) and issubclass(current, BaseModel):
        return {f.alias or name for name, f in current.model_fields.items()}
    return None
