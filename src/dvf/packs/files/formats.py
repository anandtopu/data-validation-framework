"""File formats that DuckDB cannot read on its own (without downloading extensions).

* ORC, Delta Lake, and Apache Iceberg are opened as PyArrow datasets and registered in DuckDB,
  which pushes projections and filters into the scan (memory stays bounded).
* Avro, Excel, fixed-width text, and XML are stream-converted to Parquet files in a temporary
  folder of the connection, then read with ``read_parquet``.
* CSV, TSV, JSON, and NDJSON files that DuckDB cannot read directly (bz2 or xz compression, an
  encoding other than UTF-8, UTF-16, or Latin-1, or trailer lines to skip) are copied to UTF-8
  text first.

Every optional library is imported when a dataset needs it, never at import time.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import string
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import pyarrow as pa
from pydantic import Field, NonNegativeInt, PositiveInt, ValidationError, model_validator

from dvf.config.base import StrictModel
from dvf.connectors.staged import write_parquet_stream
from dvf.core.errors import ConnectorError
from dvf.packs.files import storage
from dvf.packs.files.storage import FileInfo

NATIVE_FORMATS = frozenset({"csv", "tsv", "parquet", "json", "ndjson"})
"""Read by DuckDB table functions."""
ARROW_FORMATS = frozenset({"orc", "delta", "iceberg"})
"""Opened as PyArrow datasets."""
CONVERTED_FORMATS = frozenset({"avro", "excel", "fixed_width", "xml"})
"""Stream-converted to Parquet."""
TIME_TRAVEL_FORMATS = frozenset({"delta", "iceberg"})

EXTENSION_FORMATS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".txt": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".jsonl": "ndjson",
    ".ndjson": "ndjson",
    ".avro": "avro",
    ".orc": "orc",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".xml": "xml",
}

DUCKDB_ENCODINGS = frozenset({"utf-8", "utf8", "utf-16", "utf16", "latin-1", "latin1"})
"""Encodings DuckDB's CSV reader handles without the encodings extension."""

BATCH_ROWS = 50_000
_EXCEL_ERRORS = frozenset({"#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!"})


def require(module: str, purpose: str, install: str) -> Any:
    """Import an optional library, or raise an actionable error."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ConnectorError(f"{purpose} needs {install}: pip install {install}") from exc


def detect_format(location: str) -> str | None:
    """The format implied by a path: its extension, or the layout of a table folder."""
    if location.lower().endswith(".metadata.json"):
        return "iceberg"
    if not storage.is_url(location) and "*" not in location:
        path = Path(location)
        if path.is_dir():
            if (path / "_delta_log").is_dir():
                return "delta"
            if (path / "metadata").is_dir() and any((path / "metadata").glob("*.metadata.json")):
                return "iceberg"
            for child in sorted(path.rglob("*")):
                relative = child.relative_to(path).as_posix()
                if child.is_file() and storage.is_data_file(relative):
                    return detect_format(child.as_posix())
            return None
    return format_of_name(location)


def format_of_name(name: str) -> str | None:
    """The format implied by a file name's extension (compression suffixes ignored)."""
    base = storage.strip_compression(name.lower()).rsplit("/", 1)[-1]
    suffix = base[base.rfind(".") :] if "." in base else ""
    return EXTENSION_FORMATS.get(suffix)


_FAMILIES = {
    "csv": {"csv", "tsv"},
    "tsv": {"csv", "tsv"},
    "json": {"json", "ndjson"},
    "ndjson": {"json", "ndjson"},
}


def belongs_to(name: str, fmt: str) -> bool:
    """Whether a data file inside a dataset folder is read as ``fmt``: its extension names the
    format (or a sibling text format: Spark writes JSON lines as ``.json``), or it has none."""
    if fmt not in EXTENSION_FORMATS.values():
        return True  # fixed width: any file name
    base = storage.strip_compression(name.lower()).rsplit("/", 1)[-1]
    if "." not in base:
        return True
    found = format_of_name(name)
    return found is not None and found in _FAMILIES.get(fmt, {fmt})


def parse_as_of(text: str) -> int | datetime:
    """``as_of`` as a version or snapshot number (digits) or a timestamp (ISO 8601, UTC if naive)."""
    value = text.strip()
    if value.isdigit():
        return int(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorError(
            f"Invalid 'as_of' value '{text}': use a version or snapshot number such as \"3\", "
            "or an ISO timestamp such as '2026-01-31T23:59:59Z'."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local_path(uri: str) -> str | None:
    """The local path of a ``file:`` URI or plain path, or ``None`` for remote URLs."""
    if not storage.is_url(uri):
        return Path(uri).as_posix()
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(url2pathname(unquote(parsed.path))).as_posix()


# ============================================================================ options


def _validate(model: type[StrictModel], options: dict[str, Any], fmt: str) -> Any:
    try:
        return model.model_validate(options)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'options'}: {e['msg']}" for e in exc.errors()
        )
        raise ConnectorError(f"Invalid options for the '{fmt}' format: {problems}") from exc


class _TypedOptions(StrictModel):
    types: dict[str, str] = Field(default_factory=dict)
    """DuckDB types for columns, applied with ``CAST`` when the data is read (for example
    ``{amount: DECIMAL(12,2), booked: DATE}``)."""
    ignore_errors: bool = False
    """Values that do not convert to their type become NULL (``TRY_CAST``) instead of failing the
    query; the ``bad_records`` check counts them."""


class AvroOptions(_TypedOptions):
    """Options of the ``avro`` format."""


class ExcelOptions(_TypedOptions):
    """Options of the ``excel`` format (``.xlsx`` and ``.xlsm`` workbooks)."""

    sheet: str | NonNegativeInt | None = None
    """Worksheet name, or its position starting at 0. Defaults to the first worksheet."""
    cell_range: str | None = None
    """Cells to read, such as ``B3:F200`` or ``A:D``. Defaults to the used area of the sheet."""
    header_row: NonNegativeInt | None = None
    """Sheet row (1-based) holding column names; data starts below it. Defaults to the first row
    of ``cell_range`` (or row 1). Use ``0`` when there is no header: columns are then named by
    their letters (``A``, ``B``, ...)."""
    skip_empty_rows: bool = True
    """Ignore rows whose cells are all empty."""


class FixedWidthColumn(StrictModel):
    """One field of a fixed-width layout."""

    name: str = Field(min_length=1)
    start: PositiveInt
    """Position of the first character, starting at 1."""
    width: PositiveInt
    type: str | None = None
    """DuckDB type, applied like the ``types`` option."""


class FixedWidthOptions(_TypedOptions):
    """Options of the ``fixed_width`` format."""

    columns: list[FixedWidthColumn] = Field(default_factory=list)
    """The layout as fields with ``start`` (1-based) and ``width``."""
    widths: list[PositiveInt] = Field(default_factory=list)
    """The layout as consecutive field widths (an alternative to ``columns``)."""
    names: list[str] = Field(default_factory=list)
    """Field names for ``widths``; defaults to ``column1``, ``column2``, ..."""
    encoding: str = "utf-8"
    """Text encoding, such as ``cp1252``, ``latin-1``, or ``cp037`` (EBCDIC)."""
    trim: Literal["both", "left", "right", "none"] = "both"
    """Whitespace removed from each value."""
    empty_as_null: bool = True
    """Empty values (after trimming) become NULL."""
    skip_header: NonNegativeInt = 0
    """Lines skipped at the start of every file (headers, control records)."""
    skip_footer: NonNegativeInt = 0
    """Lines skipped at the end of every file (trailer control records)."""
    skip_pattern: str | None = None
    """Regular expression: lines that match it (anywhere) are skipped."""
    record_length: PositiveInt | None = None
    """Read fixed-length records of this many characters instead of lines (files without line
    breaks, common for mainframe extracts)."""
    strict: bool = False
    """Lines whose length differs from the layout are errors (or, with ``ignore_errors``, are
    skipped). Otherwise short lines are padded and extra characters are ignored."""

    @model_validator(mode="after")
    def _layout(self) -> FixedWidthOptions:
        if bool(self.columns) == bool(self.widths):
            raise ValueError("set the layout with either 'columns' or 'widths'")
        if self.names and len(self.names) != len(self.widths):
            raise ValueError("'names' must have one name per entry of 'widths'")
        if self.skip_pattern is not None:
            try:
                re.compile(self.skip_pattern)
            except re.error as exc:
                raise ValueError(f"invalid 'skip_pattern': {exc}") from exc
        return self

    def layout(self) -> list[FixedWidthColumn]:
        if self.columns:
            return list(self.columns)
        result, start = [], 1
        for i, width in enumerate(self.widths):
            name = self.names[i] if self.names else f"column{i + 1}"
            result.append(FixedWidthColumn(name=name, start=start, width=width))
            start += width
        return result

    def all_types(self) -> dict[str, str]:
        found = {c.name: c.type for c in self.layout() if c.type}
        return {**found, **self.types}

    def record_end(self) -> int:
        return max(c.start + c.width - 1 for c in self.layout())


class XmlOptions(_TypedOptions):
    """Options of the ``xml`` format."""

    row_path: str = Field(min_length=1)
    """Element path of one row, such as ``order`` or ``orders/order``. It matches the end of an
    element's path; start it with ``/`` to match from the root element."""
    fields: dict[str, str] = Field(default_factory=dict)
    """Columns and their paths relative to the row element: ``"@id"`` (attribute), ``"name"``
    (child text), ``"customer/name"``, ``"customer/@id"``, or ``"."`` (the row's own text). When
    empty, columns are the row's attributes and the text of its direct children."""
    empty_as_null: bool = True
    """Empty values become NULL."""


class IcebergOptions(StrictModel):
    """Options of the ``iceberg`` format."""

    catalog: dict[str, Any] | None = None
    """pyiceberg catalog properties (``type`` such as ``rest``, ``glue``, ``sql``, or ``hive``,
    ``uri``, credentials as secret references, and an optional ``name``). The dataset's
    ``table`` or ``path`` is then the table identifier, such as ``sales.orders``."""
    properties: dict[str, str] = Field(default_factory=dict)
    """File IO properties for a table read from its metadata file (``s3.endpoint``, ...)."""
    row_filter: str | None = None
    """A pyiceberg row filter pushed into the scan, such as ``"region = 'eu'"``."""


class DeltaOptions(StrictModel):
    """Options of the ``delta`` format (none besides ``as_of`` on the dataset)."""


class OrcOptions(StrictModel):
    """Options of the ``orc`` format."""

    hive_partitioning: bool | None = None
    """Read ``key=value`` folder names as columns. Detected from the paths when omitted."""


class TextCopyOptions(StrictModel):
    """dvf's own options for CSV, TSV, JSON, and NDJSON (the rest go to DuckDB)."""

    skip_footer: NonNegativeInt = 0
    """Lines skipped at the end of every file (trailer control records)."""
    encoding: str | None = None


FORMAT_OPTIONS: dict[str, type[StrictModel]] = {
    "avro": AvroOptions,
    "excel": ExcelOptions,
    "fixed_width": FixedWidthOptions,
    "xml": XmlOptions,
    "iceberg": IcebergOptions,
    "delta": DeltaOptions,
    "orc": OrcOptions,
}


def format_options(fmt: str, dataset: dict[str, Any], connection: dict[str, Any]) -> Any:
    """Validate a non-native format's options.

    Dataset options are strict. Connection options apply to every dataset of the connection, so
    those the format does not know are ignored.
    """
    model = FORMAT_OPTIONS[fmt]
    known = set(model.model_fields)
    merged = {k: v for k, v in connection.items() if k in known}
    merged.update(dataset)
    return _validate(model, merged, fmt)


def needs_text_copy(fmt: str, files: Iterable[str], options: dict[str, Any]) -> bool:
    """Whether native-format files must be copied to UTF-8 text before DuckDB reads them."""
    if fmt not in NATIVE_FORMATS or fmt == "parquet":
        return False
    if int(options.get("skip_footer") or 0) > 0:
        return True
    encoding = options.get("encoding")
    if isinstance(encoding, str) and encoding.lower().replace("_", "-") not in DUCKDB_ENCODINGS:
        return True
    return any(storage.compression_of(f) in ("bz2", "xz") for f in files)


# ============================================================================ results


@dataclass
class ArrowSource:
    """A dataset DuckDB reads through PyArrow (registered per session)."""

    dataset: Any
    files: list[FileInfo] = field(default_factory=list)
    description: str = ""


@dataclass
class ParquetSource:
    """Parquet files written to the connection's temporary folder."""

    paths: list[Path]
    files: list[FileInfo] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    ignore_errors: bool = False
    bad_rows: list[dict[str, Any]] = field(default_factory=list)
    """Records the conversion skipped (fixed-width lines with the wrong length)."""


@dataclass
class TextSource:
    """UTF-8 copies of text files, read with a DuckDB table function."""

    paths: list[Path]
    files: list[FileInfo] = field(default_factory=list)


Prepared = ArrowSource | ParquetSource | TextSource


# ============================================================================ ORC


def ensure_orc_time_zones() -> None:
    """Point the ORC library at a time zone database where the system has none (Windows).

    Arrow's ORC reader and writer load IANA zone files for timestamp columns from ``TZDIR`` or
    ``/usr/share/zoneinfo``. On Windows, ``TZDIR`` is set to the ``tzdata`` package's files when
    that package is installed and ``TZDIR`` is unset.
    """
    if os.name != "nt" or os.environ.get("TZDIR"):
        return
    with contextlib.suppress(ImportError):
        tzdata = importlib.import_module("tzdata")
        zoneinfo = Path(str(tzdata.__file__)).parent / "zoneinfo"
        if zoneinfo.is_dir():
            os.environ["TZDIR"] = str(zoneinfo)


def open_orc(
    location: str, files: list[FileInfo], options: OrcOptions, storage_options: dict[str, Any]
) -> ArrowSource:
    import pyarrow.dataset as ds

    ensure_orc_time_zones()
    if not files:
        raise ConnectorError(f"No ORC file matches '{location}'.")
    filesystem = None
    paths = [f.path for f in files]
    if storage.is_url(location):
        import pyarrow.fs as pafs

        fs = storage.fsspec_filesystem(location, storage_options)
        filesystem = pafs.PyFileSystem(pafs.FSSpecHandler(fs))
        paths = [p.split("://", 1)[1] for p in paths]
    hive = options.hive_partitioning
    if hive is None:
        hive = any("=" in part for f in files for part in f.name.split("/")[:-1])
    partitioning = None
    if hive:
        base = _common_base(location)
        partitioning = ds.HivePartitioning.discover(infer_dictionary=False)
        return ArrowSource(
            ds.dataset(
                paths,
                format="orc",
                filesystem=filesystem,
                partitioning=partitioning,
                partition_base_dir=base if filesystem is None else base.split("://", 1)[-1],
            ),
            files,
            f"ORC dataset of {len(files)} file(s)",
        )
    return ArrowSource(
        ds.dataset(paths, format="orc", filesystem=filesystem),
        files,
        f"ORC dataset of {len(files)} file(s)",
    )


def _common_base(location: str) -> str:
    """The folder part of a path or glob, before any wildcard."""
    head = re.split(r"[*?\[]", location, maxsplit=1)[0]
    if head == location and not location.lower().endswith(".orc"):
        return location.rstrip("/")
    return head.rsplit("/", 1)[0] if "/" in head else head


# ============================================================================ Delta Lake


def open_delta(
    location: str, as_of: str | None, storage_options: dict[str, Any]
) -> ArrowSource | StagedScan:
    """A Delta table, optionally at a version or timestamp.

    Tables PyArrow datasets can read (no deletion vectors) are scanned in place; others are
    read through deltalake's query engine and staged as Parquet.
    """
    deltalake = require("deltalake", "Reading Delta tables", "deltalake")
    options = {str(k): str(v) for k, v in storage_options.items()}
    target = parse_as_of(as_of) if as_of is not None else None
    try:
        if isinstance(target, int):
            table = deltalake.DeltaTable(location, version=target, storage_options=options or None)
        else:
            table = deltalake.DeltaTable(location, storage_options=options or None)
            if target is not None:
                table.load_as_version(target)
    except Exception as exc:
        if as_of is not None and "version" in str(exc).lower():
            raise ConnectorError(
                f"Delta table '{location}' has no version for as_of '{as_of}': {exc}"
            ) from exc
        raise ConnectorError(f"Could not open Delta table '{location}': {exc}") from exc
    files = _delta_files(table, location)
    description = f"Delta table version {table.version()}"
    try:
        dataset = table.to_pyarrow_dataset()
    except Exception:
        return StagedScan(lambda: _delta_query(deltalake, table), files, description)
    return ArrowSource(dataset, files, description)


def _delta_query(deltalake: Any, table: Any) -> pa.RecordBatchReader:
    """Every row of a Delta table through deltalake's DataFusion engine (deletion vectors
    applied), as a stream."""
    result = (
        deltalake.QueryBuilder().register("dvf_delta", table).execute("SELECT * FROM dvf_delta")
    )
    return pa.RecordBatchReader.from_stream(result)


def _delta_files(table: Any, location: str) -> list[FileInfo]:
    actions = pa.table(table.get_add_actions(flatten=True))
    base = location.rstrip("/")
    files = []
    for row in actions.select(["path", "size_bytes", "modification_time"]).to_pylist():
        relative = unquote(str(row["path"]))
        full = relative if storage.is_url(relative) else f"{base}/{relative}"
        modified = row["modification_time"]
        files.append(
            FileInfo(
                path=full,
                name=relative,
                size=int(row["size_bytes"] or 0),
                modified=datetime.fromtimestamp(modified / 1000, UTC) if modified else None,
            )
        )
    return sorted(files, key=lambda f: f.name)


def delta_exists(location: str, storage_options: dict[str, Any]) -> bool:
    deltalake = require("deltalake", "Reading Delta tables", "deltalake")
    options = {str(k): str(v) for k, v in storage_options.items()}
    return bool(deltalake.DeltaTable.is_deltatable(location, storage_options=options or None))


# ============================================================================ Apache Iceberg


def _iceberg_io_properties(uri: str, properties: dict[str, str]) -> dict[str, str]:
    props = dict(properties)
    if local_path(uri) is not None and "py-io-impl" not in props:
        # PyArrowFileIO cannot open local Windows paths ("/C:/..."); fsspec's local IO can.
        with contextlib.suppress(ImportError):
            importlib.import_module("fsspec")
            props["py-io-impl"] = "pyiceberg.io.fsspec.FsspecFileIO"
    return props


def _iceberg_metadata(location: str) -> str:
    """The metadata file of a table: the path itself, or the newest one in a table folder."""
    if location.lower().endswith(".metadata.json"):
        local = local_path(location)
        if local is not None and not Path(local).is_file():
            raise ConnectorError(f"Iceberg metadata file not found: {location}")
        return location if storage.is_url(location) else Path(location).resolve().as_uri()
    local = local_path(location)
    if local is None:
        raise ConnectorError(
            f"Point an Iceberg dataset on object storage at its metadata file "
            f"('.../metadata/v3.metadata.json'), or configure a catalog: {location}"
        )
    folder = Path(local) / "metadata"
    hint = folder / "version-hint.text"
    if hint.is_file():
        version = hint.read_text(encoding="utf-8").strip()
        for candidate in (
            folder / f"v{version}.metadata.json",
            folder / f"{version}.metadata.json",
        ):
            if candidate.is_file():
                return candidate.resolve().as_uri()
    candidates = sorted(folder.glob("*.metadata.json"), key=_metadata_version)
    if not candidates:
        raise ConnectorError(f"No Iceberg metadata found in {folder}")
    return candidates[-1].resolve().as_uri()


def _metadata_version(path: Path) -> tuple[int, float]:
    match = re.match(r"v?(\d+)", path.name)
    return (int(match.group(1)) if match else -1, path.stat().st_mtime)


def load_iceberg_table(location: str, options: IcebergOptions) -> Any:
    """The pyiceberg table named by ``location`` (a metadata file, a table folder, or, with a
    catalog, a table identifier)."""
    require("pyiceberg", "Reading Iceberg tables", "pyiceberg")
    if options.catalog is not None:
        from pyiceberg.catalog import load_catalog

        props = {str(k): str(v) for k, v in options.catalog.items()}
        name = props.pop("name", "dvf")
        try:
            catalog = load_catalog(name, **props)
        except ImportError as exc:
            raise ConnectorError(
                f"The Iceberg catalog '{name}' needs a library that is not installed ({exc}). "
                "For example, the sql catalog needs SQLAlchemy: pip install 'pyiceberg[sql-sqlite]'."
            ) from exc
        except Exception as exc:
            raise ConnectorError(f"Could not open the Iceberg catalog '{name}': {exc}") from exc
        try:
            return catalog.load_table(location)
        except Exception as exc:
            raise ConnectorError(
                f"Iceberg table '{location}' not found in catalog '{name}': {exc}"
            ) from exc
    from pyiceberg.table import StaticTable

    metadata = _iceberg_metadata(location)
    try:
        return StaticTable.from_metadata(
            metadata, properties=_iceberg_io_properties(metadata, options.properties)
        )
    except Exception as exc:
        raise ConnectorError(f"Could not read Iceberg metadata '{metadata}': {exc}") from exc


def _iceberg_snapshot(table: Any, as_of: str | None) -> int | None:
    if as_of is None:
        current = table.current_snapshot()
        return None if current is None else int(current.snapshot_id)
    target = parse_as_of(as_of)
    if isinstance(target, int):
        if table.snapshot_by_id(target) is None:
            known = ", ".join(str(s.snapshot_id) for s in table.snapshots()[-5:])
            raise ConnectorError(
                f"Iceberg snapshot {target} not found; recent snapshots: {known or 'none'}."
            )
        return target
    millis = int(target.timestamp() * 1000)
    snapshot = table.snapshot_as_of_timestamp(millis)
    if snapshot is None:
        raise ConnectorError(f"The Iceberg table has no snapshot at or before {as_of}.")
    return int(snapshot.snapshot_id)


def open_iceberg(
    location: str, as_of: str | None, options: IcebergOptions
) -> ArrowSource | StagedScan:
    """Plan an Iceberg scan.

    Returns an :class:`ArrowSource` over the snapshot's Parquet files when they are local, carry
    no delete files, and the table never changed its schema; otherwise a :class:`StagedScan`
    to be staged as Parquet.
    """
    import pyarrow.dataset as ds

    table = load_iceberg_table(location, options)
    snapshot_id = _iceberg_snapshot(table, as_of)
    kwargs: dict[str, Any] = {}
    if snapshot_id is not None:
        kwargs["snapshot_id"] = snapshot_id
    if options.row_filter:
        kwargs["row_filter"] = options.row_filter
    try:
        scan = table.scan(**kwargs)
        tasks = list(scan.plan_files()) if snapshot_id is not None else []
    except Exception as exc:
        raise ConnectorError(f"Could not plan the Iceberg scan of '{location}': {exc}") from exc
    files: list[FileInfo] = []
    local_files: list[str] = []
    for task in tasks:
        uri = str(task.file.file_path)
        local = local_path(uri)
        modified = None
        if local is not None:
            local_files.append(local)
            with contextlib.suppress(OSError):
                modified = datetime.fromtimestamp(Path(local).stat().st_mtime, UTC)
        files.append(
            FileInfo(
                path=local or uri,
                name=_relative_to_table(uri, str(table.metadata.location)),
                size=int(task.file.file_size_in_bytes),
                modified=modified,
            )
        )
    fast = (
        len(local_files) == len(tasks)
        and all(not task.delete_files for task in tasks)
        and len(table.metadata.schemas) == 1
        and not options.row_filter
    )
    description = (
        f"Iceberg snapshot {snapshot_id}"
        if snapshot_id is not None
        else "Iceberg table (no snapshot)"
    )
    if fast:
        schema = table.schema().as_arrow()
        if local_files:
            dataset = ds.dataset(local_files, format="parquet")
        else:
            dataset = ds.dataset([], format="parquet", schema=schema)
        return ArrowSource(dataset, files, description)
    return StagedScan(scan.to_arrow_batch_reader, files, description)


@dataclass
class StagedScan:
    """A table scan staged as Parquet before DuckDB reads it: Iceberg tables with delete files,
    schema changes, or remote files, and Delta tables with deletion vectors."""

    read: Callable[[], pa.RecordBatchReader]
    files: list[FileInfo]
    description: str

    def stage(self, out: Path) -> ParquetSource:
        try:
            reader = self.read()
            write_parquet_stream(out, reader.schema, reader, BATCH_ROWS)
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(f"Could not read the {self.description}: {exc}") from exc
        return ParquetSource([out], self.files)


def _relative_to_table(uri: str, table_location: str) -> str:
    base = table_location.rstrip("/")
    if uri.startswith(base + "/"):
        return uri[len(base) + 1 :]
    return uri.rsplit("/", 1)[-1]


# ============================================================================ Avro


def _avro_type(
    schema: Any, named: dict[str, pa.DataType]
) -> tuple[pa.DataType, Callable[[Any], Any] | None]:
    """The Arrow type of an Avro schema, and a value converter where fastavro's values need one."""
    if isinstance(schema, list):
        branches = [s for s in schema if s != "null"]
        if len(branches) == 1:
            return _avro_type(branches[0], named)
        return pa.string(), _json_text
    if isinstance(schema, str):
        if schema in named:
            return named[schema], None
        return _AVRO_PRIMITIVES.get(schema, pa.string()), None
    if not isinstance(schema, dict):
        return pa.string(), _json_text
    kind = schema.get("type")
    logical = schema.get("logicalType")
    if logical == "decimal":
        precision, scale = int(schema.get("precision", 38)), int(schema.get("scale", 0))
        return (
            pa.decimal128(precision, scale) if precision <= 38 else pa.decimal256(precision, scale)
        ), None
    if logical == "uuid":
        return pa.string(), _text_or_none
    if logical == "date":
        return pa.date32(), None
    if logical == "time-millis":
        return pa.time32("ms"), None
    if logical == "time-micros":
        return pa.time64("us"), None
    if logical in ("timestamp-millis", "timestamp-micros", "timestamp-nanos"):
        unit = {"timestamp-millis": "ms", "timestamp-micros": "us", "timestamp-nanos": "ns"}[
            logical
        ]
        return pa.timestamp(unit, tz="UTC"), None
    if logical in ("local-timestamp-millis", "local-timestamp-micros", "local-timestamp-nanos"):
        unit = {"local-timestamp-millis": "ms", "local-timestamp-micros": "us"}.get(logical, "ns")
        return pa.timestamp(unit), None
    if kind == "record":
        fields, converters = [], {}
        for item in schema.get("fields", []):
            ftype, conv = _avro_type(item["type"], named)
            fields.append(pa.field(item["name"], ftype))
            if conv is not None:
                converters[item["name"]] = conv
        struct = pa.struct(fields)
        for key in (
            schema.get("name"),
            schema.get("namespace", "") + "." + str(schema.get("name")),
        ):
            if key:
                named[str(key)] = struct
        if not converters:
            return struct, None
        return struct, _record_converter(converters)
    if kind == "array":
        element, conv = _avro_type(schema.get("items"), named)
        return pa.list_(element), (_list_converter(conv) if conv else None)
    if kind == "map":
        value_type, conv = _avro_type(schema.get("values"), named)
        return pa.map_(pa.string(), value_type), _map_converter(conv)
    if kind == "enum":
        return pa.string(), None
    if kind == "fixed":
        return pa.binary(int(schema["size"])), None
    if isinstance(kind, str | list | dict):
        return _avro_type(kind, named)
    return pa.string(), _json_text


_AVRO_PRIMITIVES: dict[str, pa.DataType] = {
    "null": pa.string(),
    "boolean": pa.bool_(),
    "int": pa.int32(),
    "long": pa.int64(),
    "float": pa.float32(),
    "double": pa.float64(),
    "bytes": pa.binary(),
    "string": pa.string(),
}


def _json_text(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)


def _text_or_none(value: Any) -> Any:
    return None if value is None else str(value)


def _list_converter(convert: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def run(value: Any) -> Any:
        return None if value is None else [convert(x) for x in value]

    return run


def _map_converter(convert: Callable[[Any], Any] | None) -> Callable[[Any], Any]:
    """Avro maps arrive as dicts; Arrow map arrays take lists of key-value pairs."""

    def run(value: Any) -> Any:
        if value is None:
            return None
        return [(k, convert(x) if convert else x) for k, x in value.items()]

    return run


def _record_converter(converters: dict[str, Callable[[Any], Any]]) -> Callable[[Any], Any]:
    def convert(value: Any) -> Any:
        if value is None:
            return None
        return {k: (converters[k](v) if k in converters else v) for k, v in value.items()}

    return convert


def avro_schema(writer_schema: Any) -> tuple[pa.Schema, dict[str, Callable[[Any], Any]]]:
    """The Arrow schema of an Avro file's records, with per-column value converters."""
    named: dict[str, pa.DataType] = {}
    if not (isinstance(writer_schema, dict) and writer_schema.get("type") == "record"):
        dtype, conv = _avro_type(writer_schema, named)
        return pa.schema([pa.field("value", dtype)]), ({"value": conv} if conv else {})
    fields, converters = [], {}
    for item in writer_schema.get("fields", []):
        ftype, conv = _avro_type(item["type"], named)
        fields.append(pa.field(item["name"], ftype))
        if conv is not None:
            converters[item["name"]] = conv
    return pa.schema(fields), converters


def _avro_batches(
    reader: Any, schema: pa.Schema, converters: dict[str, Callable[[Any], Any]], wrap: bool
) -> Iterator[pa.RecordBatch]:
    rows: list[dict[str, Any]] = []
    for record in reader:
        row = {"value": record} if wrap else record
        if converters:
            row = {k: (converters[k](v) if k in converters else v) for k, v in row.items()}
        rows.append(row)
        if len(rows) >= BATCH_ROWS:
            yield pa.RecordBatch.from_pylist(rows, schema=schema)
            rows = []
    if rows:
        yield pa.RecordBatch.from_pylist(rows, schema=schema)


def convert_avro(file: FileInfo, out: Path, storage_options: dict[str, Any]) -> int:
    fastavro = require("fastavro", "Reading Avro files", "fastavro")
    with storage.open_binary(file.path, storage_options) as handle:
        try:
            reader = fastavro.reader(handle)
            schema, converters = avro_schema(reader.writer_schema)
            wrap = not (
                isinstance(reader.writer_schema, dict)
                and reader.writer_schema.get("type") == "record"
            )
            return write_parquet_stream(
                out, schema, _avro_batches(reader, schema, converters, wrap), BATCH_ROWS
            )
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(f"Could not read Avro file '{file.name}': {exc}") from exc


# ============================================================================ Excel


def _column_letter(index: int) -> str:
    """Excel column letters for a 1-based column index."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = string.ascii_uppercase[remainder] + letters
    return letters


def _excel_value(cell: Any) -> Any:
    value = cell.value
    if isinstance(value, str):
        if value in _EXCEL_ERRORS:
            return None
        return value
    if isinstance(value, datetime) and value.time() == time(0) and not _has_time_format(cell):
        return value.date()
    return value


def _has_time_format(cell: Any) -> bool:
    fmt = str(getattr(cell, "number_format", "") or "").lower()
    fmt = re.sub(r'"[^"]*"|\[[^\]]*\]', "", fmt)
    return "h" in fmt or "s" in fmt


_EXCEL_KIND_ORDER = ("bool", "int", "float", "date", "datetime", "time", "duration", "str")


def _excel_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float | Decimal):
        return "float"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, time):
        return "time"
    if isinstance(value, timedelta):
        return "duration"
    return "str"


def _excel_arrow_type(kinds: set[str]) -> tuple[pa.DataType, str]:
    if not kinds:
        return pa.string(), "str"
    if kinds == {"int"}:
        return pa.int64(), "int"
    if kinds <= {"int", "float"}:
        return pa.float64(), "float"
    if kinds == {"date"}:
        return pa.date32(), "date"
    if kinds <= {"date", "datetime"}:
        return pa.timestamp("us"), "datetime"
    single = {"bool": pa.bool_(), "time": pa.time64("us"), "duration": pa.duration("us")}
    if len(kinds) == 1 and next(iter(kinds)) in single:
        kind = next(iter(kinds))
        return single[kind], kind
    return pa.string(), "str"


def _excel_coerce(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "str":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, datetime | date | time):
            return value.isoformat()
        return str(value)
    if kind == "float":
        return float(value)
    if kind == "datetime" and type(value) is date:
        return datetime(value.year, value.month, value.day)
    return value


def _open_workbook(file: FileInfo, storage_options: dict[str, Any]) -> Any:
    openpyxl = require("openpyxl", "Reading Excel files", "openpyxl")
    handle = storage.open_binary(file.path, storage_options)
    try:
        return openpyxl.load_workbook(handle, read_only=True, data_only=True), handle
    except Exception as exc:
        handle.close()
        raise ConnectorError(f"Could not open Excel workbook '{file.name}': {exc}") from exc


def _excel_rows(
    file: FileInfo, options: ExcelOptions, storage_options: dict[str, Any]
) -> Iterator[tuple[int, list[Any]]]:
    """``(sheet row number, cells)`` for every row of the configured area."""
    bounds = _excel_bounds(file, options)
    workbook, handle = _open_workbook(file, storage_options)
    try:
        sheet = _pick_sheet(workbook, options.sheet, file.name)
        first_row = bounds.get("min_row", 1)
        for offset, row in enumerate(sheet.iter_rows(**bounds)):
            cells = list(row)
            number = (
                cells[0].row if cells and getattr(cells[0], "row", None) else first_row + offset
            )
            yield int(number), cells
    finally:
        workbook.close()
        handle.close()


def _excel_bounds(file: FileInfo, options: ExcelOptions) -> dict[str, int]:
    """``iter_rows`` bounds from ``cell_range`` (open sides omitted)."""
    if not options.cell_range:
        return {}
    range_boundaries = importlib.import_module("openpyxl.utils.cell").range_boundaries
    try:
        min_col, min_row, max_col, max_row = range_boundaries(options.cell_range.upper())
    except (ValueError, TypeError) as exc:
        raise ConnectorError(
            f"Invalid cell_range '{options.cell_range}' for '{file.name}': {exc}"
        ) from exc
    bounds: dict[str, int] = {}
    for key, value in (
        ("min_col", min_col),
        ("min_row", min_row),
        ("max_col", max_col),
        ("max_row", max_row),
    ):
        if value is not None:
            bounds[key] = int(value)
    return bounds


def _pick_sheet(workbook: Any, sheet: str | int | None, name: str) -> Any:
    if sheet is None:
        return workbook.worksheets[0]
    if isinstance(sheet, int):
        if sheet >= len(workbook.worksheets):
            raise ConnectorError(
                f"Workbook '{name}' has {len(workbook.worksheets)} sheet(s); sheet {sheet} does not exist."
            )
        return workbook.worksheets[sheet]
    if sheet not in workbook.sheetnames:
        raise ConnectorError(
            f"Workbook '{name}' has no sheet '{sheet}'; sheets: {', '.join(workbook.sheetnames)}."
        )
    return workbook[sheet]


def _excel_layout(
    file: FileInfo, options: ExcelOptions, storage_options: dict[str, Any]
) -> tuple[list[str], int | None, list[set[str]], int]:
    """Column names, the header row, the value kinds per column, and the first column index."""
    names: list[str] = []
    header_row = options.header_row
    kinds: list[set[str]] = []
    first_col = _excel_bounds(file, options).get("min_col", 1)
    for number, cells in _excel_rows(file, options, storage_options):
        if not cells:
            continue
        if header_row is None:
            header_row = number
        if header_row and number < header_row:
            continue
        if header_row and number == header_row:
            names = [str(c.value).strip() if c.value is not None else "" for c in cells]
            continue
        values = [_excel_value(c) for c in cells]
        if options.skip_empty_rows and all(v is None or v == "" for v in values):
            continue
        while len(kinds) < len(values):
            kinds.append(set())
        for i, value in enumerate(values):
            if value is not None and value != "":
                kinds[i].add(_excel_kind(value))
    width = max(len(names), len(kinds))
    while len(kinds) < width:
        kinds.append(set())
    # Formatting can stretch the used area: drop trailing columns without a name or a value.
    while width and not kinds[width - 1] and not (width <= len(names) and names[width - 1]):
        width -= 1
    kinds = kinds[:width]
    columns = _unique_names(
        [
            (names[i] if i < len(names) and names[i] else _column_letter(int(first_col) + i))
            for i in range(width)
        ]
    )
    return columns, header_row, kinds, int(first_col)


def _unique_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for name in names:
        key = name.lower()
        if key in seen:
            seen[key] += 1
            name = f"{name}_{seen[key]}"
        else:
            seen[key] = 1
        result.append(name)
    return result


def convert_excel(
    file: FileInfo, out: Path, options: ExcelOptions, storage_options: dict[str, Any]
) -> int:
    columns, header_row, kinds, _ = _excel_layout(file, options, storage_options)
    if not columns:
        write_parquet_stream(out, pa.schema([pa.field("A", pa.string())]), [], BATCH_ROWS)
        return 0
    typed = [_excel_arrow_type(k) for k in kinds]
    schema = pa.schema([pa.field(name, t[0]) for name, t in zip(columns, typed, strict=True)])

    def batches() -> Iterator[pa.RecordBatch]:
        rows: list[list[Any]] = []
        for number, cells in _excel_rows(file, options, storage_options):
            if header_row and number <= header_row:
                continue
            values = [_excel_value(c) for c in cells]
            if options.skip_empty_rows and all(v is None or v == "" for v in values):
                continue
            values = (values + [None] * len(columns))[: len(columns)]
            rows.append([_excel_coerce(v, typed[i][1]) for i, v in enumerate(values)])
            if len(rows) >= BATCH_ROWS:
                yield _rows_batch(rows, schema)
                rows = []
        if rows:
            yield _rows_batch(rows, schema)

    return write_parquet_stream(out, schema, batches(), BATCH_ROWS)


def excel_error_cells(
    file: FileInfo, options: ExcelOptions, storage_options: dict[str, Any], limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Cells holding Excel error values (``#DIV/0!``, ``#N/A``, ...): their count and samples."""
    columns, header_row, _, first_col = _excel_layout(file, options, storage_options)
    count = 0
    samples: list[dict[str, Any]] = []
    for number, cells in _excel_rows(file, options, storage_options):
        if header_row and number <= header_row:
            continue
        for i, cell in enumerate(cells):
            if isinstance(cell.value, str) and cell.value in _EXCEL_ERRORS:
                count += 1
                if len(samples) < limit:
                    samples.append(
                        {
                            "file": file.name,
                            "line": number,
                            "column": columns[i]
                            if i < len(columns)
                            else _column_letter(first_col + i),
                            "error_type": "EXCEL ERROR",
                            "message": f"cell {_column_letter(first_col + i)}{number} holds {cell.value}",
                            "record": cell.value,
                        }
                    )
    return count, samples


def _rows_batch(rows: list[list[Any]], schema: pa.Schema) -> pa.RecordBatch:
    arrays = [pa.array([r[i] for r in rows], type=f.type) for i, f in enumerate(schema)]
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


# ============================================================================ fixed width


def _records(
    file: FileInfo, options: FixedWidthOptions, storage_options: dict[str, Any]
) -> Iterator[tuple[int, str]]:
    """``(line number, record)`` after skipping headers, footers, and matching lines."""
    if options.record_length:
        length = options.record_length

        def raw() -> Iterator[str]:
            with storage.open_text(file.path, options.encoding, storage_options) as handle:
                while True:
                    chunk = handle.read(length)
                    if not chunk:
                        return
                    if chunk.strip("\r\n"):
                        yield chunk

        source: Iterator[str] = raw()
    else:
        source = storage.iter_lines(file.path, options.encoding, storage_options)
    skip = re.compile(options.skip_pattern) if options.skip_pattern else None
    delay: deque[tuple[int, str]] = deque()
    for number, line in enumerate(source, start=1):
        if number <= options.skip_header:
            continue
        if not line.strip() and not options.record_length:
            continue
        if skip is not None and skip.search(line):
            continue
        delay.append((number, line))
        if len(delay) > options.skip_footer:
            yield delay.popleft()


def _slice(line: str, column: FixedWidthColumn, trim: str, empty_as_null: bool) -> str | None:
    value = line[column.start - 1 : column.start - 1 + column.width]
    if trim == "both":
        value = value.strip()
    elif trim == "left":
        value = value.lstrip()
    elif trim == "right":
        value = value.rstrip()
    if empty_as_null and value == "":
        return None
    return value


def fixed_width_bad_line(line: str, end: int, strict: bool) -> str | None:
    if len(line) < end:
        return f"record has {len(line)} characters; the layout needs {end}"
    if strict and len(line) > end:
        return f"record has {len(line)} characters; the layout defines {end}"
    return None


def convert_fixed_width(
    file: FileInfo,
    out: Path,
    options: FixedWidthOptions,
    storage_options: dict[str, Any],
    bad_rows: list[dict[str, Any]],
) -> int:
    layout = options.layout()
    end = options.record_end()
    schema = pa.schema([pa.field(c.name, pa.string()) for c in layout])

    def batches() -> Iterator[pa.RecordBatch]:
        columns: list[list[str | None]] = [[] for _ in layout]
        for number, line in _records(file, options, storage_options):
            problem = fixed_width_bad_line(line, end, options.strict)
            if problem is not None and options.strict:
                if not options.ignore_errors:
                    raise ConnectorError(
                        f"Line {number} of '{file.name}': {problem}. Set ignore_errors: true to "
                        "skip such lines (the bad_records check counts them)."
                    )
                if len(bad_rows) < 10_000:
                    bad_rows.append(
                        {"file": file.name, "line": number, "message": problem, "record": line}
                    )
                continue
            for i, column in enumerate(layout):
                columns[i].append(_slice(line, column, options.trim, options.empty_as_null))
            if len(columns[0]) >= BATCH_ROWS:
                yield pa.RecordBatch.from_arrays(
                    [pa.array(c, pa.string()) for c in columns], schema=schema
                )
                columns = [[] for _ in layout]
        if columns[0]:
            yield pa.RecordBatch.from_arrays(
                [pa.array(c, pa.string()) for c in columns], schema=schema
            )

    return write_parquet_stream(out, schema, batches(), BATCH_ROWS)


def fixed_width_bad_records(
    file: FileInfo, options: FixedWidthOptions, storage_options: dict[str, Any], limit: int
) -> tuple[int, int, list[dict[str, Any]]]:
    """Records whose length does not fit the layout: ``(records, bad, samples)``."""
    end = options.record_end()
    total = bad = 0
    samples: list[dict[str, Any]] = []
    for number, line in _records(file, options, storage_options):
        total += 1
        problem = fixed_width_bad_line(line, end, options.strict)
        if problem is None:
            continue
        bad += 1
        if len(samples) < limit:
            samples.append(
                {
                    "file": file.name,
                    "line": number,
                    "column": None,
                    "error_type": "RECORD LENGTH",
                    "message": problem,
                    "record": line,
                }
            )
    return total, bad, samples


# ============================================================================ XML


def _local(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text.split(":")[-1]


def _row_matcher(row_path: str) -> Callable[[list[str]], bool]:
    anchored = row_path.startswith("/") and not row_path.startswith("//")
    parts = [p for p in row_path.strip("/").split("/") if p]
    if not parts:
        raise ConnectorError("The xml option 'row_path' names no element.")

    def matches(stack: list[str]) -> bool:
        if anchored:
            return stack == parts
        return len(stack) >= len(parts) and stack[-len(parts) :] == parts

    return matches


def _xml_attribute(element: Any, name: str) -> str | None:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return str(value)
    return None


def _xml_field(element: Any, path: str) -> str | None:
    parts = [p for p in path.strip().split("/") if p not in ("", ".")]
    attribute = parts.pop()[1:] if parts and parts[-1].startswith("@") else None
    node = element
    for part in parts:
        node = next((child for child in node if _local(child.tag) == part), None)
        if node is None:
            return None
    if attribute is not None:
        return _xml_attribute(node, attribute)
    return "".join(node.itertext()).strip()


def _auto_fields(element: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for key in element.attrib:
        found.setdefault(_local(key), "@" + _local(key))
    for child in element:
        name = _local(child.tag)
        if name in found and found[name].startswith("@"):
            found[f"{name}_attr"] = found.pop(name)
        found.setdefault(name, name)
    return found


def iter_xml_rows(file: FileInfo, row_path: str, storage_options: dict[str, Any]) -> Iterator[Any]:
    """Row elements, parsed incrementally; each is discarded after use (bounded memory).

    External entities are never resolved (ElementTree does not fetch them).
    """
    matches = _row_matcher(row_path)
    with storage.open_decompressed(file.path, storage_options) as handle:
        stack: list[str] = []
        elements: list[Any] = []
        row_depth: int | None = None
        try:
            for event, element in ET.iterparse(handle, events=("start", "end")):  # noqa: S314 - local data files; expat refuses entity expansion attacks
                if event == "start":
                    stack.append(_local(element.tag))
                    elements.append(element)
                    if row_depth is None and matches(stack):
                        row_depth = len(stack)
                    continue
                if row_depth is not None and len(stack) == row_depth:
                    yield element
                    row_depth = None
                if row_depth is None:
                    element.clear()
                    if len(elements) > 1:
                        with contextlib.suppress(ValueError):
                            elements[-2].remove(element)
                stack.pop()
                elements.pop()
        except ET.ParseError as exc:
            raise ConnectorError(f"XML file '{file.name}' is not well-formed: {exc}") from exc


def convert_xml(
    file: FileInfo, out: Path, options: XmlOptions, storage_options: dict[str, Any]
) -> int:
    fields = dict(options.fields)
    if not fields:
        for element in iter_xml_rows(file, options.row_path, storage_options):
            for name, path in _auto_fields(element).items():
                fields.setdefault(name, path)
        if not fields:
            fields = {"value": "."}
    schema = pa.schema([pa.field(name, pa.string()) for name in fields])
    paths = list(fields.values())

    def batches() -> Iterator[pa.RecordBatch]:
        columns: list[list[str | None]] = [[] for _ in paths]
        for element in iter_xml_rows(file, options.row_path, storage_options):
            for i, path in enumerate(paths):
                value = _xml_field(element, path)
                if options.empty_as_null and value == "":
                    value = None
                columns[i].append(value)
            if len(columns[0]) >= BATCH_ROWS:
                yield pa.RecordBatch.from_arrays(
                    [pa.array(c, pa.string()) for c in columns], schema=schema
                )
                columns = [[] for _ in paths]
        if columns[0]:
            yield pa.RecordBatch.from_arrays(
                [pa.array(c, pa.string()) for c in columns], schema=schema
            )

    return write_parquet_stream(out, schema, batches(), BATCH_ROWS)


# ============================================================================ text copies


def copy_text(
    file: FileInfo,
    out: Path,
    encoding: str | None,
    skip_footer: int,
    storage_options: dict[str, Any],
) -> int:
    """Copy a text file to UTF-8 (decompressed), dropping its last ``skip_footer`` lines."""
    out.parent.mkdir(parents=True, exist_ok=True)
    delay: deque[str] = deque()
    lines = 0
    with (
        storage.open_text(file.path, encoding or "utf-8", storage_options) as source,
        out.open("w", encoding="utf-8", newline="") as target,
    ):
        for line in source:
            if skip_footer:
                if not line.strip("\r\n"):
                    continue
                delay.append(line)
                if len(delay) <= skip_footer:
                    continue
                line = delay.popleft()
            target.write(line)
            lines += 1
    return lines


# ============================================================================ NDJSON bad lines


def ndjson_bad_lines(
    file: FileInfo, storage_options: dict[str, Any], limit: int, encoding: str = "utf-8"
) -> tuple[int, int, list[dict[str, Any]]]:
    """Lines that are not JSON objects: ``(records, bad, samples)``."""
    total = bad = 0
    samples: list[dict[str, Any]] = []
    for number, line in enumerate(
        storage.iter_lines(file.path, encoding, storage_options), start=1
    ):
        if not line.strip():
            continue
        total += 1
        try:
            value = json.loads(line)
            problem = (
                None if isinstance(value, dict) else f"not a JSON object ({type(value).__name__})"
            )
        except json.JSONDecodeError as exc:
            problem = f"invalid JSON: {exc.msg} at column {exc.colno}"
        if problem is None:
            continue
        bad += 1
        if len(samples) < limit:
            samples.append(
                {
                    "file": file.name,
                    "line": number,
                    "column": None,
                    "error_type": "INVALID JSON",
                    "message": problem,
                    "record": line,
                }
            )
    return total, bad, samples


__all__ = [
    "ARROW_FORMATS",
    "CONVERTED_FORMATS",
    "NATIVE_FORMATS",
    "TIME_TRAVEL_FORMATS",
    "ArrowSource",
    "AvroOptions",
    "DeltaOptions",
    "ExcelOptions",
    "FixedWidthColumn",
    "FixedWidthOptions",
    "IcebergOptions",
    "OrcOptions",
    "ParquetSource",
    "StagedScan",
    "TextSource",
    "XmlOptions",
    "convert_avro",
    "convert_excel",
    "convert_fixed_width",
    "convert_xml",
    "copy_text",
    "detect_format",
    "format_options",
    "needs_text_copy",
    "open_delta",
    "open_iceberg",
    "open_orc",
    "parse_as_of",
]
