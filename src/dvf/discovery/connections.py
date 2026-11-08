"""Connections for the generation commands: from a configuration file or from location strings.

Generated configurations must never contain resolved secrets, so a connection taken from a
configuration file is copied from the file as written (before interpolation), and a database URL
given on the command line with a password is written as a ``${secret:env:...}`` reference.
Relative file paths are rewritten so they still point at the same files from the folder the
generated configuration is written to.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dvf.config.formats import read_config_file
from dvf.config.loader import LoadedConfig, LoadOptions, load_config_data
from dvf.config.merge import deep_merge
from dvf.core.errors import ConfigError
from dvf.execution.context import ConnectorPool
from dvf.plugins.registry import Registry, get_registry

_SCHEME = re.compile(r"^(?P<scheme>[a-z][a-z0-9+]*)://", re.IGNORECASE)
_PATH_FIELDS = {"duckdb": "database", "sqlite": "database", "files": "path"}
_DUCKDB_SUFFIXES = (".duckdb", ".ddb")
_SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")


@dataclass
class ConnectionSpec:
    """A connection to open now and the form to write into a generated configuration."""

    name: str
    open_config: dict[str, Any]
    """The configuration used to connect (may contain a password)."""
    written_config: dict[str, Any]
    """The configuration written to files (secrets replaced by references)."""
    notes: list[str] = field(default_factory=list)

    @property
    def type(self) -> str:
        return str(self.open_config.get("type", ""))


def _is_dynamic(value: str) -> bool:
    return value == ":memory:" or "://" in value or "${" in value


def relative_path(path: Path, base_dir: Path) -> str:
    """``path`` relative to ``base_dir`` with forward slashes, or absolute if not possible."""
    try:
        text = os.path.relpath(path.resolve(), base_dir.resolve())
    except ValueError:  # another drive on Windows
        return path.resolve().as_posix()
    return Path(text).as_posix()


def rebase_paths(connection: Mapping[str, Any], from_dir: Path, to_dir: Path) -> dict[str, Any]:
    """Rewrite a connection's relative file path so it is relative to ``to_dir``."""
    result = copy.deepcopy(dict(connection))
    field_name = _PATH_FIELDS.get(str(result.get("type", "")))
    value = result.get(field_name) if field_name else None
    if field_name is None or not isinstance(value, str) or _is_dynamic(value):
        return result
    path = Path(value)
    if not path.is_absolute():
        result[field_name] = relative_path(from_dir / path, to_dir)
    return result


def from_location(location: str, *, side: str, cwd: Path | None = None) -> dict[str, Any]:
    """A connection configuration for a location given on the command line.

    Accepted forms: ``postgresql://...``, ``mysql://...``, ``duckdb:///path``, ``sqlite:///path``,
    a DuckDB or SQLite file (by extension), or a folder of data files.
    """
    base = cwd or Path.cwd()
    match = _SCHEME.match(location)
    if match is None:
        path = Path(location).expanduser()
        path = path if path.is_absolute() else base / path
        if path.is_dir():
            return {"type": "files", "path": path.resolve().as_posix()}
        suffix = path.suffix.lower()
        if suffix in _DUCKDB_SUFFIXES:
            return {"type": "duckdb", "database": path.resolve().as_posix()}
        if suffix in _SQLITE_SUFFIXES:
            return {"type": "sqlite", "database": path.resolve().as_posix()}
        raise ConfigError(
            f"The {side} location '{location}' is not a folder, a .duckdb file, or a .sqlite/.db file.",
            hint="Use a folder of data files, duckdb:///path, sqlite:///path, or postgresql://...",
        )
    scheme = match.group("scheme").lower()
    if scheme.startswith("postgres"):
        return {"type": "postgres", "url": location}
    if scheme.startswith("mysql"):
        return {"type": "mysql", "url": location}
    if scheme in ("duckdb", "sqlite"):
        # SQLAlchemy style: three slashes before a relative path, four before an absolute one.
        rest = location[len(match.group(0)) :]
        database = (rest[1:] if rest.startswith("/") else rest) or ":memory:"
        if database != ":memory:":
            path = Path(database)
            database = (path if path.is_absolute() else base / path).resolve().as_posix()
        return {"type": scheme, "database": database}
    raise ConfigError(
        f"Unsupported {side} location scheme '{scheme}://'.",
        hint="Use postgresql://, mysql://, duckdb:///, sqlite:///, or a folder; for other "
        "systems define the connections in a configuration file and pass --config.",
    )


def spec_from_location(
    location: str, *, name: str, side: str, output_dir: Path, cwd: Path | None = None
) -> ConnectionSpec:
    """A :class:`ConnectionSpec` for a command-line location, safe to write to ``output_dir``."""
    config = from_location(location, side=side, cwd=cwd)
    written = dict(config)
    notes: list[str] = []
    field_name = _PATH_FIELDS.get(config["type"])
    if field_name and not _is_dynamic(config[field_name]):
        written[field_name] = relative_path(Path(config[field_name]), output_dir)
    url = config.get("url")
    if isinstance(url, str) and urlsplit(url).password is not None:
        variable = f"{side.upper()}_DATABASE_URL"
        written["url"] = f"${{secret:env:{variable}}}"
        notes.append(
            f"The {side} URL contains a password, so the configuration reads it from the "
            f"environment variable {variable}; set it before running."
        )
    return ConnectionSpec(name=name, open_config=config, written_config=written, notes=notes)


def raw_config_data(path: Path) -> dict[str, Any]:
    """A configuration file as written (no interpolation), with its ``extends`` files merged."""

    def read(file: Path, stack: tuple[Path, ...]) -> dict[str, Any]:
        data, _ = read_config_file(file)
        extends = data.pop("extends", [])
        if isinstance(extends, str):
            extends = [extends]
        merged: dict[str, Any] = {}
        for item in extends if isinstance(extends, list) else []:
            parent = (file.parent / str(item)).resolve()
            if parent in stack or not parent.is_file():
                continue
            merged = deep_merge(merged, read(parent, (*stack, parent)))
        result: dict[str, Any] = deep_merge(merged, data)
        return result

    resolved = path.resolve()
    return read(resolved, (resolved,))


def spec_from_config(
    loaded: LoadedConfig, config_path: Path, name: str, *, output_dir: Path
) -> ConnectionSpec:
    """The connection ``name`` of a configuration file, as written in the file."""
    if name not in loaded.config.connections:
        known = ", ".join(sorted(loaded.config.connections)) or "none"
        raise ConfigError(
            f"Connection '{name}' is not defined in {config_path}.",
            hint=f"Defined connections: {known}.",
        )
    raw = raw_config_data(config_path).get("connections", {}).get(name)
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"Connection '{name}' could not be read as written from {config_path} "
            "(it may come from a profile, a local override, or an environment variable).",
            hint="Define the connection in the configuration file or one it extends.",
        )
    written = rebase_paths(raw, loaded.base_dir, output_dir)
    # The loaded configuration opens the connection; only its type is needed here.
    opened = {"type": loaded.config.connections[name].type}
    return ConnectionSpec(name=name, open_config=opened, written_config=written)


def open_pool(
    specs: list[ConnectionSpec],
    *,
    base_dir: Path,
    registry: Registry | None = None,
    loaded: LoadedConfig | None = None,
) -> ConnectorPool:
    """A connector pool for ``specs`` (or for ``loaded``, which already holds them)."""
    registry = registry or (loaded.registry if loaded is not None else get_registry())
    if loaded is None:
        data = {
            "version": 1,
            "project": {"name": "dvf-discovery"},
            "connections": {s.name: s.open_config for s in specs},
        }
        options = LoadOptions(strict=False, resolve_secrets=True, env_overrides=False)
        loaded = load_config_data(data, options, registry=registry, base_dir=base_dir)
    return ConnectorPool(loaded.config, registry, loaded.base_dir)


__all__ = [
    "ConnectionSpec",
    "from_location",
    "open_pool",
    "raw_config_data",
    "rebase_paths",
    "relative_path",
    "spec_from_config",
    "spec_from_location",
]
