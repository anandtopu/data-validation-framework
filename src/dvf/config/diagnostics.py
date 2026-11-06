"""Configuration diagnostics: what went wrong, where, and how to fix it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dvf.core.enums import DiagnosticLevel

PathSegment = str | int
ValuePath = tuple[PathSegment, ...]


class Code(StrEnum):
    """Stable diagnostic codes. Documented in docs/configuration.md."""

    FILE_NOT_FOUND = "DVF-C001"
    PARSE_ERROR = "DVF-C002"
    UNSUPPORTED_FORMAT = "DVF-C003"
    DUPLICATE_KEY = "DVF-C004"
    EXTENDS_ERROR = "DVF-C005"
    PROFILE_NOT_FOUND = "DVF-C006"
    INVALID_OVERRIDE = "DVF-C007"
    INTERPOLATION_SYNTAX = "DVF-C010"
    ENV_MISSING = "DVF-C011"
    VAR_MISSING = "DVF-C012"
    SECRET_PROVIDER_UNKNOWN = "DVF-C013"  # noqa: S105 - an enum name, not a password
    SECRET_UNRESOLVED = "DVF-C014"  # noqa: S105 - an enum name, not a password
    RUN_PLACEHOLDER_UNKNOWN = "DVF-C015"
    VALIDATION = "DVF-C100"
    MISSING_FIELD = "DVF-C101"
    UNKNOWN_FIELD = "DVF-C102"
    UNSUPPORTED_VERSION = "DVF-C103"
    UNKNOWN_COMPONENT = "DVF-C110"
    UNKNOWN_CONNECTION = "DVF-C120"
    DUPLICATE_NAME = "DVF-C121"
    SUITE_TARGET_MISMATCH = "DVF-C124"
    DUPLICATE_CHECK_ID = "DVF-C125"
    WAIVER_EXPIRED = "DVF-C130"
    WAIVER_UNMATCHED = "DVF-C131"
    PLUGIN_LOAD = "DVF-C140"


@dataclass(frozen=True)
class SourceLocation:
    """Where a value came from."""

    file: str
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        if self.line is None:
            return self.file
        if self.column is None:
            return f"{self.file}:{self.line}"
        return f"{self.file}:{self.line}:{self.column}"


@dataclass(frozen=True)
class Diagnostic:
    """One problem found while loading configuration."""

    level: DiagnosticLevel
    code: Code
    message: str
    path: str = ""
    location: SourceLocation | None = None
    hint: str | None = None

    @property
    def is_error(self) -> bool:
        return self.level is DiagnosticLevel.ERROR

    def format(self) -> str:
        where = str(self.location) if self.location else ""
        if self.path:
            where = f"{where} ({self.path})" if where else self.path
        prefix = f"{where}: " if where else ""
        hint = f" Hint: {self.hint}" if self.hint else ""
        return f"{prefix}{self.message}{hint} [{self.code}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "code": self.code.value,
            "message": self.message,
            "path": self.path,
            "file": self.location.file if self.location else None,
            "line": self.location.line if self.location else None,
            "column": self.location.column if self.location else None,
            "hint": self.hint,
        }


def format_path(path: Iterable[PathSegment]) -> str:
    """Render ``("suites", 0, "tables")`` as ``suites[0].tables``."""
    out = ""
    for segment in path:
        if isinstance(segment, int):
            out += f"[{segment}]"
        else:
            out += f".{segment}" if out else str(segment)
    return out


@dataclass
class LocationMap:
    """Maps value paths to the file position they came from."""

    _locations: dict[ValuePath, SourceLocation] = field(default_factory=dict)
    default_file: str | None = None

    def set(self, path: ValuePath, location: SourceLocation) -> None:
        self._locations[path] = location

    def get(self, path: ValuePath) -> SourceLocation | None:
        """The location of ``path`` or of its nearest ancestor that has one."""
        for end in range(len(path), -1, -1):
            location = self._locations.get(path[:end])
            if location is not None:
                return location
        return SourceLocation(self.default_file) if self.default_file else None

    def items(self) -> Iterable[tuple[ValuePath, SourceLocation]]:
        return self._locations.items()

    def overlay(self, other: LocationMap, prefix: ValuePath = ()) -> None:
        """Record ``other``'s locations, shifted by ``prefix``, replacing existing ones."""
        for path, location in other.items():
            self._locations[prefix + path] = location

    def drop_subtree(self, prefix: ValuePath) -> None:
        """Forget locations at and below ``prefix`` (used when a list is replaced)."""
        for path in [p for p in self._locations if p[: len(prefix)] == prefix]:
            del self._locations[path]

    def copy_subtree(self, source: ValuePath, destination: ValuePath) -> None:
        """Copy locations from ``source`` to ``destination`` (used for inherited values)."""
        for path, location in list(self._locations.items()):
            if path[: len(source)] == source:
                self._locations.setdefault(destination + path[len(source) :], location)


def resolve_path(data: Any, loc: Iterable[Any]) -> ValuePath:
    """Keep only the segments of a Pydantic error ``loc`` that exist in the raw data.

    Pydantic inserts union member tags (``'list[KeyMapping]'``) and validator names into error
    locations; walking the raw data removes them.
    """
    path: list[PathSegment] = []
    node = data
    for segment in loc:
        if (isinstance(node, Mapping) and segment in node) or (
            isinstance(node, list) and isinstance(segment, int) and 0 <= segment < len(node)
        ):
            path.append(segment)
            node = node[segment]
        elif (
            isinstance(node, Mapping) and isinstance(segment, str) and not _looks_like_tag(segment)
        ):
            path.append(segment)  # a missing or unknown key: keep it, the walk ends here
            node = None
    return tuple(path)


def _looks_like_tag(segment: str) -> bool:
    return any(ch in segment for ch in "[]-") and not segment.isidentifier()
