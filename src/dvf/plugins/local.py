"""Loading project-local plugins from files, folders, or importable modules.

Loading a plugin executes its code. Configuration that names plugins is therefore trusted code,
and should be reviewed like code.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

from dvf.core.errors import PluginError
from dvf.plugins.hooks import PROJECT_NAME
from dvf.plugins.registry import Registry, activate

_HOOKIMPL_ATTR = f"{PROJECT_NAME}_impl"
_MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def load_local_plugins(
    registry: Registry, specs: Iterable[str], base_dir: Path | None = None
) -> list[str]:
    """Import each plugin spec into ``registry`` and return the sources loaded.

    A spec is a ``.py`` file, a folder (every ``*.py`` file not starting with ``_``), or a dotted
    module name. Relative paths resolve against ``base_dir`` (usually the config file's folder).
    """
    base = base_dir or Path.cwd()
    loaded: list[str] = []
    for spec in specs:
        if _looks_like_module(spec) and not (base / spec).exists():
            loaded.append(_load_module(registry, spec))
            continue
        path = Path(spec)
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if path.is_dir():
            files = sorted(p for p in path.glob("*.py") if not p.name.startswith("_"))
        elif path.is_file():
            files = [path]
        else:
            raise PluginError(f"Plugin path not found: {path}")
        for file in files:
            loaded.append(_load_file(registry, file))
    return loaded


def _looks_like_module(spec: str) -> bool:
    return bool(_MODULE_NAME_RE.match(spec)) and not spec.endswith(".py")


def _load_module(registry: Registry, name: str) -> str:
    key = f"module:{name}"
    if key in registry.loaded_local:
        return key
    try:
        with activate(registry):
            if name in sys.modules:
                # Re-run decorators so they register into this registry too.
                module = importlib.reload(sys.modules[name])
            else:
                module = importlib.import_module(name)
    except Exception as exc:
        raise PluginError(
            f"Could not import plugin module '{name}': {type(exc).__name__}: {exc}"
        ) from exc
    registry.loaded_local[key] = module
    _register_hooks_if_any(registry, module, key)
    return key


def _load_file(registry: Registry, path: Path) -> str:
    key = f"file:{path}"
    if key in registry.loaded_local:
        return key
    digest = hashlib.sha1(f"{id(registry)}:{path}".encode(), usedforsecurity=False).hexdigest()[:10]
    stem = re.sub(r"\W", "_", path.stem)
    module_name = f"_dvf_local_{stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"Cannot load plugin file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        with activate(registry):
            spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PluginError(f"Error in plugin file {path}: {type(exc).__name__}: {exc}") from exc
    registry.loaded_local[key] = module
    _register_hooks_if_any(registry, module, key)
    return key


def _register_hooks_if_any(registry: Registry, module: ModuleType, name: str) -> None:
    has_hooks = any(hasattr(getattr(module, attr, None), _HOOKIMPL_ATTR) for attr in dir(module))
    if has_hooks:
        registry.register_hooks(module, name=name)
