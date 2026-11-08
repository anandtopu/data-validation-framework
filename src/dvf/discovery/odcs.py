"""Reading data contracts: ODCS v3 documents and dvf expected-schema files.

ODCS (Open Data Contract Standard, https://bitol-io.github.io/open-data-contract-standard/)
v3.0.x, v3.1, and v3.2 are read. The parts dvf uses:

* ``schema``: objects (tables) with ``name``, ``physicalName``, ``properties``, ``quality``,
  and ``relationships`` (v3.1);
* properties: ``name``, ``physicalName``, ``logicalType``, ``physicalType``, ``required``,
  ``unique``, ``primaryKey``, ``primaryKeyPosition``, ``logicalTypeOptions``, ``enum`` (v3.2),
  ``quality``, ``relationships`` (v3.1), ``classification``;
* ``servers``: where the data lives.

An expected-schema file is a smaller format for SCH-12 contract conformance::

    name: orders              # optional
    dialect: postgres         # optional: dialect of the 'type' strings
    columns:
      - {name: order_id, type: INTEGER, nullable: false}
      - {name: amount, type: "DECIMAL(10,2)"}
      - {name: status, logical_type: string}
    primary_key: [order_id]
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dvf.core.errors import ConfigError

LOGICAL_TYPES = (
    "string",
    "date",
    "timestamp",
    "time",
    "number",
    "integer",
    "object",
    "array",
    "boolean",
    "map",
    "vector",
)
"""ODCS v3 logical types (``timestamp`` and ``time`` since v3.1, ``map`` and ``vector`` since v3.2)."""


@dataclass
class ContractProperty:
    """A property (column) of a contract object."""

    name: str
    physical_name: str | None = None
    logical_type: str | None = None
    physical_type: str | None = None
    required: bool = False
    unique: bool = False
    primary_key: bool = False
    primary_key_position: int | None = None
    options: dict[str, Any] = field(default_factory=dict)
    """``logicalTypeOptions``."""
    enum: list[Any] | None = None
    quality: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    classification: str | None = None
    description: str | None = None
    nested: bool = False
    """True for an ``object`` or ``array`` property with nested properties (not a column)."""

    @property
    def column(self) -> str:
        """The column name in the dataset."""
        return self.physical_name or self.name


@dataclass
class ContractObject:
    """A schema object (table, view, file) of a contract."""

    name: str
    physical_name: str | None = None
    physical_type: str | None = None
    description: str | None = None
    properties: list[ContractProperty] = field(default_factory=list)
    quality: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    declared_primary_key: list[str] = field(default_factory=list)
    """Primary key given directly (expected-schema files)."""

    @property
    def table(self) -> str:
        """The dataset name."""
        return self.physical_name or self.name

    def primary_key(self) -> list[str]:
        """Primary key columns, in ``primaryKeyPosition`` order."""
        if self.declared_primary_key:
            return list(self.declared_primary_key)
        keyed = [p for p in self.properties if p.primary_key]
        keyed.sort(key=lambda p: (p.primary_key_position or 10**6, self.properties.index(p)))
        return [p.column for p in keyed]

    def find(self, name: str) -> ContractProperty | None:
        lowered = name.lower()
        return next(
            (
                p
                for p in self.properties
                if p.name.lower() == lowered or p.column.lower() == lowered
            ),
            None,
        )


@dataclass
class Contract:
    """A parsed contract."""

    source: str
    """The file it came from (for messages)."""
    format: str
    """``odcs`` or ``expected_schema``."""
    objects: list[ContractObject]
    api_version: str | None = None
    id: str | None = None
    name: str | None = None
    version: str | None = None
    status: str | None = None
    servers: list[dict[str, Any]] = field(default_factory=list)
    dialect: str | None = None
    """Dialect of physical types, for expected-schema files that state it."""
    notes: list[str] = field(default_factory=list)

    def object(self, name: str | None = None, *, table: str | None = None) -> ContractObject:
        """The object called ``name`` (or whose name matches ``table``), or the only object."""
        if name is not None:
            found = self._find(name)
            if found is None:
                raise ConfigError(
                    f"Contract {self.source} has no schema object '{name}'.",
                    hint="Objects: " + ", ".join(o.name for o in self.objects),
                )
            return found
        if len(self.objects) == 1:
            return self.objects[0]
        if table is not None:
            found = self._find(table) or self._find(table.rsplit(".", 1)[-1])
            if found is not None:
                return found
        raise ConfigError(
            f"Contract {self.source} has {len(self.objects)} schema objects; say which one to use.",
            hint="Set 'object' to one of: " + ", ".join(o.name for o in self.objects),
        )

    def _find(self, name: str) -> ContractObject | None:
        lowered = name.lower()
        return next(
            (o for o in self.objects if o.name.lower() == lowered or o.table.lower() == lowered),
            None,
        )


# ---------------------------------------------------------------------- parsing helpers


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _list_of_dicts(value: Any, where: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
        raise ConfigError(f"{where} must be a list of mappings.")
    return [dict(v) for v in value]


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _property(raw: Mapping[str, Any], where: str) -> ContractProperty:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}: every property needs a 'name'.")
    position = raw.get("primaryKeyPosition")
    options = raw.get("logicalTypeOptions") or {}
    if not isinstance(options, Mapping):
        raise ConfigError(f"{where}.{name}: 'logicalTypeOptions' must be a mapping.")
    nested = bool(raw.get("properties")) or (
        raw.get("logicalType") == "array" and isinstance(raw.get("items"), Mapping)
    )
    enum = raw.get("enum")
    return ContractProperty(
        name=name,
        physical_name=_text(raw.get("physicalName")),
        logical_type=_text(raw.get("logicalType")),
        physical_type=_text(raw.get("physicalType")),
        required=_bool(raw.get("required", False)),
        unique=_bool(raw.get("unique", False)),
        primary_key=_bool(raw.get("primaryKey", False)),
        primary_key_position=int(position)
        if isinstance(position, int | str)
        and str(position).lstrip("-").isdigit()
        and int(position) > 0
        else None,
        options=dict(options),
        enum=list(enum) if isinstance(enum, list) else None,
        quality=_list_of_dicts(raw.get("quality"), f"{where}.{name}.quality"),
        relationships=_list_of_dicts(raw.get("relationships"), f"{where}.{name}.relationships"),
        classification=_text(raw.get("classification")),
        description=_text(raw.get("description")),
        nested=nested,
    )


def parse_odcs(data: Mapping[str, Any], source: str = "<contract>") -> Contract:
    """Parse an ODCS v3 document."""
    api_version = _text(data.get("apiVersion"))
    notes: list[str] = []
    if "dataset" in data and "schema" not in data:
        raise ConfigError(
            f"{source} looks like an ODCS v2 contract ('dataset'); dvf reads ODCS v3.",
            hint="Upgrade the contract to ODCS v3 (schema objects with properties).",
        )
    if api_version is None:
        notes.append("the contract has no 'apiVersion'; it is read as ODCS v3")
    elif not api_version.lstrip("v").startswith("3"):
        raise ConfigError(
            f"{source} declares apiVersion '{api_version}'; dvf reads ODCS v3 (v3.0.0 to v3.2.x)."
        )
    kind = data.get("kind")
    if kind is not None and kind != "DataContract":
        notes.append(f"kind is '{kind}', expected 'DataContract'")
    schema = data.get("schema")
    if schema is None:
        schema = []
        notes.append("the contract has no 'schema' section")
    objects: list[ContractObject] = []
    for i, raw in enumerate(_list_of_dicts(schema, f"{source}: schema")):
        where = f"{source}: schema[{i}]"
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}: every schema object needs a 'name'.")
        props_raw = raw.get("properties")
        if props_raw is None and isinstance(raw.get("columns"), list):
            raise ConfigError(
                f"{where}: 'columns' was renamed 'properties' in ODCS v3.",
                hint="Rename 'columns' to 'properties'.",
            )
        properties = [
            _property(p, f"{where}.{name}")
            for p in _list_of_dicts(props_raw, f"{where}.properties")
        ]
        objects.append(
            ContractObject(
                name=name,
                physical_name=_text(raw.get("physicalName")),
                physical_type=_text(raw.get("physicalType")),
                description=_text(raw.get("description")),
                properties=properties,
                quality=_list_of_dicts(raw.get("quality"), f"{where}.quality"),
                relationships=_list_of_dicts(raw.get("relationships"), f"{where}.relationships"),
            )
        )
    servers = data.get("servers") or []
    if isinstance(servers, Mapping):  # tolerate the v2 shape: a mapping of name -> server
        servers = [{"server": k, **dict(v)} for k, v in servers.items() if isinstance(v, Mapping)]
    return Contract(
        source=source,
        format="odcs",
        objects=objects,
        api_version=api_version,
        id=_text(data.get("id")),
        name=_text(data.get("name")),
        version=_text(data.get("version")),
        status=_text(data.get("status")),
        servers=_list_of_dicts(servers, f"{source}: servers"),
        notes=notes,
    )


def parse_expected_schema(data: Mapping[str, Any], source: str = "<schema>") -> Contract:
    """Parse a dvf expected-schema file."""
    columns = _list_of_dicts(data.get("columns"), f"{source}: columns")
    if not columns:
        raise ConfigError(f"{source}: an expected-schema file needs a non-empty 'columns' list.")
    properties: list[ContractProperty] = []
    for i, raw in enumerate(columns):
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{source}: columns[{i}] needs a 'name'.")
        nullable = raw.get("nullable")
        properties.append(
            ContractProperty(
                name=name,
                physical_type=_text(raw.get("type")),
                logical_type=_text(raw.get("logical_type")),
                required=nullable is False or _bool(raw.get("required", False)),
                unique=_bool(raw.get("unique", False)),
            )
        )
    primary_key = data.get("primary_key") or []
    if isinstance(primary_key, str):
        primary_key = [primary_key]
    name = _text(data.get("name")) or Path(source).stem
    obj = ContractObject(name=name, properties=properties, declared_primary_key=list(primary_key))
    return Contract(
        source=source,
        format="expected_schema",
        objects=[obj],
        name=name,
        dialect=_text(data.get("dialect")),
    )


def parse_contract(data: Any, source: str = "<contract>") -> Contract:
    """Parse an ODCS v3 contract or an expected-schema file, telling them apart by shape."""
    if not isinstance(data, Mapping):
        raise ConfigError(f"{source} must contain a mapping at the top level.")
    if "columns" in data and "schema" not in data and "apiVersion" not in data:
        return parse_expected_schema(data, source)
    return parse_odcs(data, source)


def load_contract(path: Path) -> Contract:
    """Read a contract or expected-schema file (YAML or JSON)."""
    if not path.is_file():
        raise ConfigError(f"Contract file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read the contract {path}: {exc}") from exc
    return parse_contract(data, path.as_posix())


__all__ = [
    "LOGICAL_TYPES",
    "Contract",
    "ContractObject",
    "ContractProperty",
    "load_contract",
    "parse_contract",
    "parse_expected_schema",
    "parse_odcs",
]
