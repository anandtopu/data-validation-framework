from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from dvf.core.errors import PluginError, UnknownPluginError
from dvf.plugins import (
    Check,
    CheckConfig,
    Dimension,
    PluginKind,
    Registry,
    hookimpl,
    register_check,
    register_connector,
)
from dvf.plugins import registry as registry_module
from dvf.plugins.local import load_local_plugins


class _MyCheck(Check):
    """My custom check."""

    dimension = Dimension.VALIDITY

    class Config(CheckConfig):
        column: str


def test_builtins_are_declared(registry: Registry) -> None:
    entry = registry.get(PluginKind.CHECK, "not_null")
    assert entry.status == "declared"
    assert entry.config_model is not None
    assert entry.origin == "builtin"
    assert registry.get(PluginKind.SECRET_PROVIDER, "env").ready
    assert registry.get(PluginKind.SECRET_PROVIDER, "dotenv").ready
    assert not registry.get(PluginKind.SECRET_PROVIDER, "azurekv").ready
    assert registry.get(PluginKind.CONNECTOR, "snowflake").extra == "snowflake"
    kinds = {e.kind for e in registry.entries()}
    assert kinds == set(PluginKind)


def test_register_and_lookup(registry: Registry) -> None:
    entry = registry.register(PluginKind.CHECK, "acme.my_check", _MyCheck)
    assert entry.ready
    assert entry.description == "My custom check."
    assert entry.config_model is _MyCheck.Config
    assert registry.find("check", "acme.my_check") is entry
    assert "acme.my_check" in registry.names(PluginKind.CHECK)


def test_unknown_lookup_suggests(registry: Registry) -> None:
    with pytest.raises(UnknownPluginError) as info:
        registry.get(PluginKind.CHECK, "not_nul")
    assert info.value.suggestions[0] == "not_null"
    assert "Did you mean 'not_null'" in str(info.value)


@pytest.mark.parametrize("name", ["Bad", "1abc", "a-b", "a..b", ""])
def test_invalid_names(registry: Registry, name: str) -> None:
    with pytest.raises(PluginError, match="Invalid check name"):
        registry.register(PluginKind.CHECK, name, _MyCheck)


def test_duplicates_are_errors_unless_same_object_or_replaced(registry: Registry) -> None:
    registry.register(PluginKind.CHECK, "acme.dup", _MyCheck)
    registry.register(PluginKind.CHECK, "acme.dup", _MyCheck)  # idempotent

    class Other(_MyCheck):
        pass

    with pytest.raises(PluginError, match="already registered"):
        registry.register(PluginKind.CHECK, "acme.dup", Other)
    with pytest.raises(PluginError, match="already registered"):
        registry.register(PluginKind.CHECK, "not_null", Other)
    assert (
        registry.register(PluginKind.CHECK, "acme.dup", Other, replace_existing=True).implementation
        is Other
    )


def test_builtin_implementation_attaches_to_declaration(registry: Registry) -> None:
    class NotNull(Check):
        pass

    entry = registry.register(PluginKind.CHECK, "not_null", NotNull, origin="builtin")
    assert entry.ready
    assert entry.config_model is not None  # kept from the declaration
    assert entry.description.startswith("Columns must not contain NULL")


def test_config_model_must_subclass_base(registry: Registry) -> None:
    class Wrong(BaseModel):
        column: str

    class BadCheck(Check):
        Config = Wrong

    with pytest.raises(PluginError, match="must subclass CheckConfig"):
        registry.register(PluginKind.CHECK, "acme.bad", BadCheck)


def test_plugin_api_compatibility(registry: Registry) -> None:
    class Future(_MyCheck):
        dvf_plugin_api = ">=9.0"

    class Invalid(_MyCheck):
        dvf_plugin_api = "not a spec"

    class Compatible(_MyCheck):
        dvf_plugin_api = ">=0.1,<1.0"

    with pytest.raises(PluginError, match=r"requires dvf plugin API >=9\.0"):
        registry.register(PluginKind.CHECK, "acme.future", Future)
    with pytest.raises(PluginError, match="invalid dvf_plugin_api"):
        registry.register(PluginKind.CHECK, "acme.invalid", Invalid)
    assert registry.register(PluginKind.CHECK, "acme.ok", Compatible).ready


def test_decorators_register_into_default_registry(default_registry: Registry) -> None:
    @register_check("acme.decorated")
    class Decorated(_MyCheck):
        """Decorated check."""

    @register_connector("acme.conn")
    class Conn:
        """A connector."""

    assert default_registry.get(PluginKind.CHECK, "acme.decorated").implementation is Decorated
    assert Decorated.plugin_name == "acme.decorated"
    assert default_registry.get(PluginKind.CONNECTOR, "acme.conn").description == "A connector."


class _FakeDist:
    name = "dvf-acme"
    version = "1.2.3"


def _entry_point(name: str, value: str, group: str) -> EntryPoint:
    ep = EntryPoint(name=name, value=value, group=group)
    return ep._for(_FakeDist())  # type: ignore[attr-defined, no-any-return]


def test_entry_points_load_and_isolate_failures(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("dvf_fake_plugin")
    module.MyCheck = _MyCheck  # type: ignore[attr-defined]
    module.not_a_class = 42  # type: ignore[attr-defined]

    class Hooks:
        @hookimpl
        def on_config_loaded(self, config: Any) -> None:
            pass

    module.hooks = Hooks()  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "dvf_fake_plugin", module)

    groups = {
        "dvf.checks": [
            _entry_point("acme.ep_check", "dvf_fake_plugin:MyCheck", "dvf.checks"),
            _entry_point("acme.broken", "dvf_missing_module:Thing", "dvf.checks"),
            _entry_point("acme.number", "dvf_fake_plugin:not_a_class", "dvf.checks"),
            _entry_point("acme.module", "dvf_fake_plugin", "dvf.checks"),
        ],
        "dvf.hooks": [
            _entry_point("acme", "dvf_fake_plugin:hooks", "dvf.hooks"),
            _entry_point("broken", "dvf_missing_module:hooks", "dvf.hooks"),
        ],
    }
    monkeypatch.setattr(registry_module, "entry_points", lambda group: groups.get(group, []))
    registry.load_entry_points()

    entry = registry.get(PluginKind.CHECK, "acme.ep_check")
    assert entry.origin == "entry-point"
    assert entry.source == "entry-point (dvf-acme 1.2.3)"
    sources = {f.source.split(" ")[0] for f in registry.load_failures}
    assert sources == {"dvf.checks:acme.broken", "dvf.checks:acme.number", "dvf.hooks:broken"}
    assert registry.find(PluginKind.CHECK, "acme.module") is None
    assert registry.hooks.has_plugin("dvf.hooks:acme")


def test_local_plugins_from_folder_file_and_module(
    registry: Registry, write: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        "plugins/vat.py",
        """
        from dvf.plugins import Check, CheckConfig, hookimpl, register_check

        CALLS = []

        @register_check("acme.vat")
        class Vat(Check):
            '''VAT check.'''
            class Config(CheckConfig):
                column: str

        @hookimpl
        def on_config_loaded(config):
            CALLS.append(config.project.name)
        """,
    )
    write("plugins/_private.py", "raise RuntimeError('must not be imported')\n")
    write(
        "single.py",
        "from dvf.plugins import register_normalizer\n@register_normalizer('acme.norm')\nclass N: pass\n",
    )
    write(
        "pkg_mod/acme_mod.py",
        "from dvf.plugins import register_reporter\n@register_reporter('acme.rep')\nclass R: pass\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path / "pkg_mod"))

    loaded = load_local_plugins(registry, ["plugins", "single.py", "acme_mod"], tmp_path)
    assert len(loaded) == 3
    assert registry.get(PluginKind.CHECK, "acme.vat").origin == "local"
    assert registry.get(PluginKind.NORMALIZER, "acme.norm").ready
    assert registry.get(PluginKind.REPORTER, "acme.rep").ready
    assert any(name.startswith("file:") for name, _ in registry.hooks.list_name_plugin())
    # Loading again is a no-op for this registry.
    assert load_local_plugins(registry, ["plugins"], tmp_path) == loaded[:1]


def test_local_plugin_errors(registry: Registry, write: Any, tmp_path: Path) -> None:
    write("broken.py", "raise ValueError('boom')\n")
    with pytest.raises(PluginError, match=r"Error in plugin file .*boom"):
        load_local_plugins(registry, ["broken.py"], tmp_path)
    with pytest.raises(PluginError, match="Plugin path not found"):
        load_local_plugins(registry, ["missing/"], tmp_path)
    with pytest.raises(PluginError, match="Could not import plugin module"):
        load_local_plugins(registry, ["dvf_no_such_module_xyz"], tmp_path)


def test_get_registry_is_a_lazy_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_default_registry", None)
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [])
    first = registry_module.get_registry()
    assert first is registry_module.get_registry()
    assert first.find(PluginKind.CHECK, "unique") is not None
    registry_module._reset_default_registry()
    assert registry_module._default_registry is None
