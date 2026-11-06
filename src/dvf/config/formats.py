"""Readers for YAML, JSON, TOML, .properties, and .ini configuration files.

Each reader returns the parsed mapping and a :class:`LocationMap`. YAML and .properties record a
line and column for every value; JSON, TOML, and INI record the file only.
"""

from __future__ import annotations

import configparser
import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from dvf.config.diagnostics import (
    Code,
    Diagnostic,
    LocationMap,
    PathSegment,
    SourceLocation,
    ValuePath,
    format_path,
)
from dvf.core.enums import DiagnosticLevel
from dvf.core.errors import ConfigError

SUPPORTED_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".properties", ".ini", ".cfg")

# ---------------------------------------------------------------------- YAML loader


class _YamlLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 style scalars.

    YAML 1.1 turns ``on``/``off``/``yes``/``no`` into booleans, ``0123`` into octal 83, and
    ``12:30`` into 750. Those surprises are removed: only ``true``/``false`` are booleans, numbers
    with leading zeros stay strings (SAP codes such as ``0100`` survive), and there is no base 60.
    """


_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT_RE = re.compile(r"^(?:[-+]?(?:0|[1-9][0-9_]*)|0x[0-9a-fA-F_]+)$")
_FLOAT_RE = re.compile(
    r"^(?:[-+]?(?:\.[0-9]+|[0-9][0-9_]*\.[0-9_]*|[0-9][0-9_]*(?:\.[0-9_]*)?[eE][-+]?[0-9]+)"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)
_REPLACED_TAGS = {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"}

_YamlLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in _REPLACED_TAGS]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_YamlLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL_RE, list("tTfF"))
_YamlLoader.add_implicit_resolver("tag:yaml.org,2002:int", _INT_RE, list("-+0123456789"))
_YamlLoader.add_implicit_resolver("tag:yaml.org,2002:float", _FLOAT_RE, list("-+0123456789."))

_MERGE_TAG = "tag:yaml.org,2002:merge"


def _error(
    code: Code, message: str, location: SourceLocation | None, path: str = ""
) -> ConfigError:
    diagnostic = Diagnostic(DiagnosticLevel.ERROR, code, message, path=path, location=location)
    return ConfigError(message, [diagnostic])


def read_yaml(text: str, file: str) -> tuple[Any, LocationMap]:
    loader = _YamlLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return {}, LocationMap(default_file=file)
        duplicates: list[Diagnostic] = []
        _find_duplicate_keys(node, (), file, duplicates, loader, set())
        if duplicates:
            raise ConfigError("Duplicate keys in YAML", duplicates)
        data = loader.construct_document(node)
        locations = LocationMap(default_file=file)
        _record_yaml_locations(node, (), file, locations, loader, set())
        return data, locations
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        location = (
            SourceLocation(file, mark.line + 1, mark.column + 1) if mark else SourceLocation(file)
        )
        problem = exc.problem or exc.context or "invalid YAML"
        raise _error(Code.PARSE_ERROR, f"YAML syntax error: {problem}", location) from exc
    except yaml.YAMLError as exc:
        raise _error(Code.PARSE_ERROR, f"YAML error: {exc}", SourceLocation(file)) from exc
    finally:
        loader.dispose()


def _key_of(node: yaml.Node, loader: _YamlLoader) -> PathSegment | None:
    if not isinstance(node, yaml.ScalarNode):
        return None
    value = loader.construct_object(node)
    return value if isinstance(value, str | int) else str(value)


def _mark(node: yaml.Node, file: str) -> SourceLocation:
    return SourceLocation(file, node.start_mark.line + 1, node.start_mark.column + 1)


def _find_duplicate_keys(
    node: yaml.Node,
    path: ValuePath,
    file: str,
    out: list[Diagnostic],
    loader: _YamlLoader,
    active: set[int],
) -> None:
    if id(node) in active:
        return
    active.add(id(node))
    if isinstance(node, yaml.MappingNode):
        seen: set[PathSegment] = set()
        for key_node, value_node in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = _key_of(key_node, loader)
            if key is not None:
                if key in seen:
                    full = (*path, key)
                    out.append(
                        Diagnostic(
                            DiagnosticLevel.ERROR,
                            Code.DUPLICATE_KEY,
                            f"Duplicate key '{key}'; the earlier value would be silently ignored.",
                            path=format_path(full),
                            location=_mark(key_node, file),
                        )
                    )
                seen.add(key)
                _find_duplicate_keys(value_node, (*path, key), file, out, loader, active)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _find_duplicate_keys(item, (*path, index), file, out, loader, active)
    active.discard(id(node))


def _record_yaml_locations(
    node: yaml.Node,
    path: ValuePath,
    file: str,
    locations: LocationMap,
    loader: _YamlLoader,
    active: set[int],
) -> None:
    if id(node) in active:
        return
    active.add(id(node))
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = _key_of(key_node, loader)
            if key is None:
                continue
            locations.set((*path, key), _mark(key_node, file))
            _record_yaml_locations(value_node, (*path, key), file, locations, loader, active)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            locations.set((*path, index), _mark(item, file))
            _record_yaml_locations(item, (*path, index), file, locations, loader, active)
    active.discard(id(node))


# ---------------------------------------------------------------------- JSON and TOML


def read_json(text: str, file: str) -> tuple[Any, LocationMap]:
    duplicates: list[str] = []

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    try:
        data = json.loads(text, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as exc:
        location = SourceLocation(file, exc.lineno, exc.colno)
        raise _error(Code.PARSE_ERROR, f"JSON syntax error: {exc.msg}", location) from exc
    if duplicates:
        message = f"Duplicate keys in JSON: {', '.join(sorted(set(duplicates)))}"
        raise _error(Code.DUPLICATE_KEY, message, SourceLocation(file))
    return data, LocationMap(default_file=file)


_TOML_POSITION_RE = re.compile(r"\(at line (\d+), column (\d+)\)")


def read_toml(text: str, file: str) -> tuple[Any, LocationMap]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        match = _TOML_POSITION_RE.search(str(exc))
        line, column = (int(match.group(1)), int(match.group(2))) if match else (None, None)
        message = _TOML_POSITION_RE.sub("", str(exc)).strip()
        raise _error(
            Code.PARSE_ERROR, f"TOML syntax error: {message}", SourceLocation(file, line, column)
        ) from exc
    return data, LocationMap(default_file=file)


# ---------------------------------------------------------------------- flat formats

_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def parse_key_path(text: str) -> ValuePath:
    """Parse ``suites[0].tables.1.name`` into ``("suites", 0, "tables", 1, "name")``.

    Bracketed numbers and all-digit dotted segments are list indices.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty key")
    segments: list[PathSegment] = []
    position = 0
    for match in _PATH_TOKEN_RE.finditer(stripped):
        between = stripped[position : match.start()]
        if between not in ("", ".") or (position == 0 and between):
            raise ValueError(f"invalid key '{text}'")
        name, index = match.groups()
        if index is not None:
            segments.append(int(index))
        elif name is not None:
            name = name.strip()
            segments.append(int(name) if name.isdigit() else name)
        position = match.end()
    if position != len(stripped) or not segments:
        raise ValueError(f"invalid key '{text}'")
    return tuple(segments)


def parse_scalar(text: str) -> Any:
    """Interpret a flat value (from .properties, .ini, environment, or ``--set``).

    ``null``/``~`` become None; booleans, integers, and floats follow the YAML 1.2 rules used for
    YAML files; values starting with ``[`` or ``{`` are parsed as YAML flow collections; anything
    else stays a string.
    """
    value = text.strip()
    if value in ("null", "~"):
        return None
    if _BOOL_RE.match(value) or _INT_RE.match(value) or _FLOAT_RE.match(value):
        return yaml.load(value, Loader=_YamlLoader)  # noqa: S506 - safe loader subclass
    if value.startswith(("[", "{")):
        try:
            return yaml.load(value, Loader=_YamlLoader)  # noqa: S506 - safe loader subclass
        except yaml.YAMLError:
            return value
    return value


def set_path(data: dict[str, Any], path: ValuePath, value: Any) -> None:
    """Set ``value`` at ``path``, creating mappings and appending list items as needed."""
    node: Any = data
    for i, segment in enumerate(path):
        last = i == len(path) - 1
        next_is_index = not last and isinstance(path[i + 1], int)
        if isinstance(segment, int):
            if not isinstance(node, list):
                raise ValueError(f"'{format_path(path[:i])}' is not a list")
            if segment > len(node):
                raise ValueError(
                    f"index {segment} skips items in '{format_path(path[:i])}' "
                    f"(it has {len(node)}); use index {len(node)} to append"
                )
            if segment == len(node):
                node.append(value if last else ([] if next_is_index else {}))
            elif last:
                node[segment] = value
            node = node[segment]
        else:
            if not isinstance(node, dict):
                raise ValueError(f"'{format_path(path[:i])}' is not a mapping")
            if last:
                node[segment] = value
            else:
                child = node.get(segment)
                if not isinstance(child, dict | list):
                    child = [] if next_is_index else {}
                    node[segment] = child
                node = child


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued lines, returning (first line number, content)."""
    lines: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip() if not buffer else raw.lstrip()
        if not buffer:
            start = number
            if not stripped or stripped.startswith(("#", "!")):
                continue
        trailing = len(stripped) - len(stripped.rstrip("\\"))
        if trailing % 2 == 1:
            buffer += stripped[:-1]
            continue
        lines.append((start, buffer + stripped))
        buffer = ""
    if buffer:
        lines.append((start, buffer))
    return lines


_SEPARATOR_RE = re.compile(r"(?<!\\)[=:]")


def read_properties(text: str, file: str) -> tuple[Any, LocationMap]:
    data: dict[str, Any] = {}
    locations = LocationMap(default_file=file)
    for line_number, line in _logical_lines(text):
        match = _SEPARATOR_RE.search(line)
        if match is None:
            raise _error(
                Code.PARSE_ERROR,
                f"Expected 'key = value', got '{line}'",
                SourceLocation(file, line_number, 1),
            )
        raw_key = line[: match.start()].replace("\\=", "=").replace("\\:", ":")
        raw_value = line[match.end() :]
        try:
            path = parse_key_path(raw_key)
            set_path(data, path, parse_scalar(raw_value))
        except ValueError as exc:
            raise _error(
                Code.PARSE_ERROR, f"Invalid property: {exc}", SourceLocation(file, line_number, 1)
            ) from exc
        locations.set(path, SourceLocation(file, line_number, 1))
    return data, locations


INI_ROOT_SECTION = "dvf"


def read_ini(text: str, file: str) -> tuple[Any, LocationMap]:
    parser = configparser.ConfigParser(
        interpolation=None, default_section="__dvf_no_default__", strict=True
    )
    parser.optionxform = str  # type: ignore[assignment, method-assign]
    try:
        parser.read_string(text, source=file)
    except configparser.Error as exc:
        line = getattr(exc, "lineno", None)
        raise _error(
            Code.PARSE_ERROR, f"INI syntax error: {exc}", SourceLocation(file, line)
        ) from exc
    data: dict[str, Any] = {}
    for section in parser.sections():
        try:
            prefix: ValuePath = () if section == INI_ROOT_SECTION else parse_key_path(section)
            for key, value in parser.items(section, raw=True):
                set_path(data, prefix + parse_key_path(key), parse_scalar(value))
        except ValueError as exc:
            raise _error(
                Code.PARSE_ERROR, f"Invalid INI entry: {exc}", SourceLocation(file)
            ) from exc
    return data, LocationMap(default_file=file)


_READERS: dict[str, Callable[[str, str], tuple[Any, LocationMap]]] = {
    ".yaml": read_yaml,
    ".yml": read_yaml,
    ".json": read_json,
    ".toml": read_toml,
    ".properties": read_properties,
    ".ini": read_ini,
    ".cfg": read_ini,
}


def read_config_file(path: Path) -> tuple[dict[str, Any], LocationMap]:
    """Read one configuration file into a mapping plus value locations."""
    file = str(path)
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        supported = ", ".join(SUPPORTED_SUFFIXES)
        raise _error(
            Code.UNSUPPORTED_FORMAT,
            f"Unsupported configuration format '{path.suffix}'. Use one of: {supported}.",
            SourceLocation(file),
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise _error(Code.FILE_NOT_FOUND, f"Configuration file not found: {file}", None) from exc
    except OSError as exc:
        raise _error(Code.FILE_NOT_FOUND, f"Cannot read {file}: {exc}", None) from exc
    data, locations = reader(text, file)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _error(
            Code.PARSE_ERROR,
            f"The top level of a configuration file must be a mapping, got {type(data).__name__}.",
            SourceLocation(file, 1, 1),
        )
    return data, locations
