"""Configuration format migrations (``dvf config migrate``).

A configuration file states its format version in ``version:``. When the format changes, dvf
ships a :class:`Migrator` that upgrades a configuration from one version to the next; migrators
chain, so a file several versions behind is upgraded step by step. Each migrator receives the
file's raw mapping (before layering, interpolation, or validation), returns the upgraded
mapping, and may record notes for the user about anything that needs a manual decision.

Migrators upgrade one file. Fragments listed in ``extends:`` and sibling profile files are
migrated separately (fragments without ``version:`` take ``--from-version``). A migrator must
also upgrade the overlays under ``profiles:`` when the change affects them.

Version 1 is the first released format, so dvf ships no migrators yet; the framework is in place
so the first format change can ship with one (see :data:`MIGRATORS`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dvf.config.formats import read_config_file
from dvf.config.models import CONFIG_VERSION
from dvf.core.errors import ConfigError

MigrationFunction = Callable[[dict[str, Any], "MigrationNotes"], dict[str, Any]]
"""Upgrade a raw configuration mapping by one version; record notes for the user."""


class MigrationNotes:
    """Messages a migrator leaves for the user (shown by ``dvf config migrate``)."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)


@dataclass(frozen=True)
class Migrator:
    """Upgrades a configuration from :attr:`from_version` to ``from_version + 1``."""

    from_version: int
    description: str
    apply: MigrationFunction

    @property
    def to_version(self) -> int:
        return self.from_version + 1


class MigratorRegistry:
    """Migrators by the version they upgrade from."""

    def __init__(self) -> None:
        self._migrators: dict[int, Migrator] = {}

    def register(
        self, from_version: int, description: str
    ) -> Callable[[MigrationFunction], MigrationFunction]:
        """Decorator registering a function that upgrades ``from_version`` by one version."""

        def decorator(func: MigrationFunction) -> MigrationFunction:
            self.add(Migrator(from_version, description, func))
            return func

        return decorator

    def add(self, migrator: Migrator) -> None:
        if migrator.from_version in self._migrators:
            raise ValueError(f"A migrator from version {migrator.from_version} already exists.")
        self._migrators[migrator.from_version] = migrator

    def chain(self, from_version: int, to_version: int) -> list[Migrator]:
        """The migrators upgrading ``from_version`` to ``to_version``, in order."""
        steps = []
        for version in range(from_version, to_version):
            migrator = self._migrators.get(version)
            if migrator is None:
                raise ConfigError(
                    f"No migration from configuration version {version} to {version + 1}.",
                    hint=f"This dvf reads version {to_version} and can upgrade from versions "
                    f"{self.supported(to_version)}. Upgrade the file by hand following the "
                    "changelog, or with the dvf release that introduced the next version.",
                )
            steps.append(migrator)
        return steps

    def supported(self, to_version: int) -> str:
        """The versions that can be upgraded to ``to_version``, for messages."""
        versions = []
        version = to_version
        while version - 1 in self._migrators:
            version -= 1
            versions.append(version)
        return ", ".join(str(v) for v in sorted(versions)) or "none (no migrations exist yet)"

    def __len__(self) -> int:
        return len(self._migrators)


MIGRATORS = MigratorRegistry()
"""Migrators shipped with dvf. Register the next one as::

    @MIGRATORS.register(1, "Rename defaults.sample_size to defaults.samples")
    def _v1_to_v2(data: dict[str, Any], notes: MigrationNotes) -> dict[str, Any]:
        ...
        data["version"] = 2  # optional: the framework sets it
        return data
"""


@contextmanager
def use_migrators(registry: MigratorRegistry) -> Iterator[MigratorRegistry]:
    """Use ``registry`` instead of :data:`MIGRATORS` within the block (for tests)."""
    global MIGRATORS
    previous = MIGRATORS
    MIGRATORS = registry
    try:
        yield registry
    finally:
        MIGRATORS = previous


@dataclass
class MigrationResult:
    """The outcome of migrating a mapping or a file."""

    from_version: int
    to_version: int
    data: dict[str, Any]
    steps: list[str] = field(default_factory=list)
    """Descriptions of the migrators applied, in order."""
    notes: list[str] = field(default_factory=list)
    path: Path | None = None
    output: Path | None = None
    """Where the upgraded file was written (``None`` when nothing was written)."""
    backup: Path | None = None

    @property
    def changed(self) -> bool:
        return self.from_version != self.to_version


def _version_of(data: dict[str, Any], from_version: int | None, source: str) -> int:
    raw = data.get("version")
    if raw is None:
        if from_version is None:
            raise ConfigError(
                f"{source} has no 'version:' field, so its format version is unknown.",
                hint="Pass --from-version N (fragments listed in 'extends:' usually have no "
                f"version), or add 'version: {CONFIG_VERSION}' if the file is already current.",
            )
        return from_version
    if isinstance(raw, bool) or not isinstance(raw, int | str) or not str(raw).isdigit():
        raise ConfigError(f"{source} has an invalid 'version: {raw}'; expected an integer.")
    version = int(raw)
    if from_version is not None and from_version != version:
        raise ConfigError(
            f"{source} says 'version: {version}', but --from-version {from_version} was given.",
            hint="Leave out --from-version for files that state their version.",
        )
    return version


def migrate_data(
    data: dict[str, Any],
    *,
    to_version: int = CONFIG_VERSION,
    from_version: int | None = None,
    source: str = "The configuration",
    registry: MigratorRegistry | None = None,
) -> MigrationResult:
    """Upgrade a raw configuration mapping to ``to_version``."""
    registry = registry if registry is not None else MIGRATORS
    version = _version_of(data, from_version, source)
    if version > to_version:
        raise ConfigError(
            f"{source} is configuration version {version}, newer than version {to_version} that "
            "this dvf reads.",
            hint="Upgrade dvf: pip install --upgrade dvf-framework",
        )
    result = MigrationResult(from_version=version, to_version=to_version, data=dict(data))
    current = dict(data)
    had_version = "version" in data
    for migrator in registry.chain(version, to_version):
        notes = MigrationNotes()
        current = migrator.apply(dict(current), notes)
        if not isinstance(current, dict):
            raise TypeError(
                f"The migrator from version {migrator.from_version} returned "
                f"{type(current).__name__}, not a mapping."
            )
        if had_version or "version" in current:
            current["version"] = migrator.to_version
        result.steps.append(
            f"{migrator.from_version} -> {migrator.to_version}: {migrator.description}"
        )
        result.notes.extend(notes.items)
    if result.changed and had_version:
        current = {"version": current.pop("version"), **current}
    result.data = current
    return result


_WRITABLE = {".yaml": "yaml", ".yml": "yaml", ".json": "json"}


def render(data: dict[str, Any], suffix: str) -> str:
    """Serialize a configuration mapping as YAML or JSON (by file extension)."""
    kind = _WRITABLE.get(suffix.lower())
    if kind == "json":
        return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    if kind == "yaml":
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    raise ConfigError(
        f"dvf config migrate writes YAML and JSON files, not '{suffix}'.",
        hint="Pass --output new.yaml to write the upgraded configuration as YAML.",
    )


def backup_path(path: Path, version: int) -> Path:
    """A free backup file name next to ``path``: ``dvf.yaml.v1.bak``, then ``.v1.1.bak``, ..."""
    candidate = path.with_name(f"{path.name}.v{version}.bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.v{version}.{counter}.bak")
        counter += 1
    return candidate


def migrate_file(
    path: Path,
    *,
    check: bool = False,
    output: Path | None = None,
    backup: bool = True,
    to_version: int = CONFIG_VERSION,
    from_version: int | None = None,
    registry: MigratorRegistry | None = None,
) -> MigrationResult:
    """Upgrade the configuration file at ``path``.

    With ``check``, nothing is written. Otherwise the upgraded file replaces ``path`` (after
    copying the original to a backup file next to it, unless ``backup`` is false) or is written
    to ``output``. Comments are not preserved; the backup keeps the original text.
    """
    data, _locations = read_config_file(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} does not contain a configuration mapping.")
    result = migrate_data(
        data,
        to_version=to_version,
        from_version=from_version,
        source=str(path),
        registry=registry,
    )
    result.path = path
    if check or (not result.changed and output is None):
        return result
    target = output or path
    text = render(result.data, target.suffix)
    if output is None and backup:
        result.backup = backup_path(path, result.from_version)
        result.backup.write_bytes(path.read_bytes())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    result.output = target
    return result


__all__ = [
    "MIGRATORS",
    "MigrationNotes",
    "MigrationResult",
    "Migrator",
    "MigratorRegistry",
    "backup_path",
    "migrate_data",
    "migrate_file",
    "render",
    "use_migrators",
]
