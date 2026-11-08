"""Configuration models of the file-level checks (BUILD_PROMPT.md 7.7 and REC-09)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, NonNegativeInt, PositiveInt, field_validator, model_validator

from dvf.config.base import Duration, Identifier, StrictModel
from dvf.config.checks import CheckConfig


def _compiles(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    return pattern


class FileCheckConfig(CheckConfig):
    """Settings shared by the file-level checks."""

    path: str | None = None
    """Files to check: a file, folder, or glob relative to the connection's folder (``**``
    matches folders recursively), or a URL. Defaults to the dataset's own files. ``${run.date}``
    and the other run placeholders are replaced."""


class FileExistsCheck(FileCheckConfig):
    """At least one file must exist, or exactly ``count`` files."""

    count: NonNegativeInt | None = None
    """Exact number of files expected; omit to require at least one."""


class FileCountCheck(FileCheckConfig):
    """The number of files must fall within a range."""

    min: NonNegativeInt | None = None
    max: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _bounds(self) -> FileCountCheck:
        if self.min is None and self.max is None:
            raise ValueError("set 'min', 'max', or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("'min' must not exceed 'max'")
        return self


class FileNamePatternCheck(FileCheckConfig):
    """File names must match a regular expression."""

    pattern: str
    """Python regular expression, such as ``orders_\\d{8}\\.csv(\\.gz)?``."""
    match_on: Literal["name", "path"] = "name"
    """``name`` matches the file name; ``path`` matches the path relative to the connection's
    folder (``2026/11/orders.csv``)."""
    full_match: bool = True
    """Require the whole name to match, rather than any part of it."""

    @field_validator("pattern")
    @classmethod
    def _valid(cls, value: str) -> str:
        return _compiles(value)


class FileSizeCheck(FileCheckConfig):
    """File sizes must fall within a range; empty files fail unless allowed."""

    min_bytes: NonNegativeInt | None = None
    max_bytes: PositiveInt | None = None
    allow_empty: bool = False
    """Accept zero-byte files (they fail by default, even without ``min_bytes``)."""

    @model_validator(mode="after")
    def _bounds(self) -> FileSizeCheck:
        if (
            self.min_bytes is not None
            and self.max_bytes is not None
            and self.min_bytes > self.max_bytes
        ):
            raise ValueError("'min_bytes' must not exceed 'max_bytes'")
        return self


class FileSetRef(StrictModel):
    """Files on a connection, used as the reference of a checksum comparison."""

    connection: Identifier | None = None
    """A files connection; defaults to the checked dataset's connection."""
    path: str = Field(min_length=1)
    """File, folder, or glob relative to that connection's folder."""


class FileChecksumCheck(FileCheckConfig):
    """File checksums (MD5 or SHA-256) must match expected values, sidecar files, a manifest,
    or the files of another dataset (file-vs-file reconciliation)."""

    algorithm: Literal["md5", "sha256"] = "sha256"
    expected: dict[str, str] = Field(default_factory=dict)
    """Expected digests by file: a file name, a path relative to the connection's folder, or a
    glob."""
    sidecar: bool | str = False
    """Read each file's digest from a sidecar file next to it: ``true`` for ``<file>.md5`` or
    ``<file>.sha256`` (after the algorithm), or the sidecar suffix, such as ``.sha256sum``."""
    manifest: str | None = None
    """A manifest file, relative to the connection's folder: lines of ``<digest> <file>`` (the
    ``sha256sum``/``md5sum`` format) or CSV rows ``file,digest`` (an optional header is
    skipped)."""
    compare_to: FileSetRef | None = None
    """Compare with the files of another location, matched by name (``match_by``). Every file
    must exist on both sides with the same digest."""
    match_by: Literal["name", "relative_path"] = "name"
    """How ``compare_to`` pairs files: by file name, or by path relative to each side's folder
    or glob base."""

    @model_validator(mode="after")
    def _one_reference(self) -> FileChecksumCheck:
        given = [
            name
            for name, value in (
                ("expected", self.expected),
                ("sidecar", self.sidecar),
                ("manifest", self.manifest),
                ("compare_to", self.compare_to),
            )
            if value
        ]
        if len(given) != 1:
            raise ValueError(
                "set exactly one of 'expected', 'sidecar', 'manifest', or 'compare_to'"
                + (f" (got {', '.join(given)})" if given else "")
            )
        for name, digest in self.expected.items():
            if not re.fullmatch(r"[0-9a-fA-F]+", digest):
                raise ValueError(f"'expected.{name}' is not a hexadecimal digest")
        return self


class FileFreshnessCheck(FileCheckConfig):
    """The newest (or oldest) file must have been modified within ``max_age``."""

    max_age: Duration
    """Such as ``26h`` or ``2d``."""
    which: Literal["newest", "oldest"] = "newest"
    """``newest``: some file is recent enough. ``oldest``: every file is recent enough."""


class ControlField(StrictModel):
    """A control value at fixed positions of a control record."""

    start: PositiveInt
    """Position of the first character, starting at 1."""
    width: PositiveInt
    scale: NonNegativeInt = 0
    """Implied decimal places: ``000012345`` with scale 2 is 123.45."""


class ControlRecord(StrictModel):
    """Where a header or trailer control record is, and how to read its values."""

    line: int | None = None
    """Which non-empty line holds the record: 1 is the first, -1 the last. Defaults to 1 for a
    header and -1 for a trailer."""
    pattern: str | None = None
    """Regular expression with named groups, one per control value, such as
    ``^T\\|(?P<count>\\d+)\\|(?P<amount>-?[\\d.]+)$``. The record must match it."""
    fields: dict[str, ControlField] = Field(default_factory=dict)
    """Control values at fixed positions (an alternative to ``pattern``)."""

    @model_validator(mode="after")
    def _one_layout(self) -> ControlRecord:
        if (self.pattern is None) == (not self.fields):
            raise ValueError("set either 'pattern' or 'fields'")
        if self.pattern is not None:
            compiled = re.compile(_compiles(self.pattern))
            if not compiled.groupindex:
                raise ValueError("'pattern' needs named groups, such as (?P<count>\\d+)")
        if self.line == 0:
            raise ValueError("'line' counts from 1 (first) or from -1 (last)")
        return self

    def names(self) -> list[str]:
        if self.pattern is not None:
            return list(re.compile(self.pattern).groupindex)
        return list(self.fields)


class ControlTotalsCheck(FileCheckConfig):
    """Header or trailer control records (record counts, column sums) must match the data."""

    header: ControlRecord | None = None
    trailer: ControlRecord | None = None
    record_count: str | None = None
    """Name of the control value holding the number of data records."""
    sums: dict[str, str] = Field(default_factory=dict)
    """Data column to the name of the control value holding its sum, such as
    ``{amount: amount_total}``."""
    tolerance: float = Field(default=0.0, ge=0)
    """Largest absolute difference accepted for sums."""
    encoding: str = "utf-8"
    """Encoding of the files holding the control records."""

    @model_validator(mode="after")
    def _complete(self) -> ControlTotalsCheck:
        if self.header is None and self.trailer is None:
            raise ValueError("set 'header', 'trailer', or both")
        if self.record_count is None and not self.sums:
            raise ValueError("set 'record_count', 'sums', or both")
        available = set()
        for record in (self.header, self.trailer):
            if record is not None:
                available.update(record.names())
        wanted = (
            [self.record_count, *self.sums.values()] if self.record_count else [*self.sums.values()]
        )
        missing = [name for name in wanted if name not in available]
        if missing:
            raise ValueError(
                f"control value(s) {', '.join(missing)} are not defined by 'header' or 'trailer' "
                f"(defined: {', '.join(sorted(available)) or 'none'})"
            )
        return self


class SchemaColumn(StrictModel):
    """An expected column."""

    name: str = Field(min_length=1)
    type: str | None = None
    """Expected type in SQL terms (``INTEGER``, ``DECIMAL(12,2)``, ``VARCHAR``, ``DATE``,
    ``TIMESTAMP``); omit to accept any type."""


def _coerce_columns(value: Any) -> Any:
    if isinstance(value, dict):
        return [{"name": k, "type": v} for k, v in value.items()]
    if isinstance(value, list):
        return [{"name": v} if isinstance(v, str) else v for v in value]
    return value


class SchemaOnReadCheck(CheckConfig):
    """The columns and types read from the dataset must match an expected schema."""

    columns: list[SchemaColumn] = Field(default_factory=list)
    """Expected columns, as a list of ``{name, type}`` (or names), or a mapping of name to
    type."""
    schema_file: str | None = None
    """A YAML or JSON file with the expected columns (the same forms as ``columns``, optionally
    under a ``columns`` key), relative to the configuration's folder."""
    allow_extra_columns: bool = False
    """Accept columns that are not expected."""
    ordered: bool = False
    """Require the expected columns in the given order."""
    case_sensitive: bool = False
    """Compare column names case-sensitively."""
    type_match: Literal["logical", "exact"] = "logical"
    """``logical`` compares type families (any integer width, any decimal precision, any
    string length); ``exact`` also compares widths, precision, scale, and lengths."""

    @field_validator("columns", mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        return _coerce_columns(value)

    @model_validator(mode="after")
    def _source(self) -> SchemaOnReadCheck:
        if bool(self.columns) == (self.schema_file is not None):
            raise ValueError("set either 'columns' or 'schema_file'")
        return self

    def target_columns(self) -> list[str]:
        return []


class BadRecordsCheck(CheckConfig):
    """Unreadable and malformed records (CSV parse and quoting errors, invalid JSON lines,
    fixed-width length errors, unconvertible values, corrupt files) must not exceed a limit."""

    max_count: NonNegativeInt | None = None
    """Largest number of bad records accepted. With neither this nor ``mostly``, any bad record
    fails the check."""


__all__ = [
    "BadRecordsCheck",
    "ControlField",
    "ControlRecord",
    "ControlTotalsCheck",
    "FileCheckConfig",
    "FileChecksumCheck",
    "FileCountCheck",
    "FileExistsCheck",
    "FileFreshnessCheck",
    "FileNamePatternCheck",
    "FileSetRef",
    "FileSizeCheck",
    "SchemaColumn",
    "SchemaOnReadCheck",
]
