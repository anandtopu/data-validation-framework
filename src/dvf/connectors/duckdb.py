"""DuckDB connector, and the file connector built on it."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import duckdb
import pyarrow as pa

from dvf.config.connections import ConnectionConfig, DuckDBConnection, FileConnection
from dvf.config.models import DatasetSpec
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError
from dvf.core.types import parse_native_type
from dvf.plugins.base import (
    CollationInfo,
    ConnectionReport,
    Connector,
    ObjectInventory,
    PhysicalDesign,
    Relation,
)
from dvf.sql.dialects import get_dialect

if TYPE_CHECKING:
    from dvf.packs.files.formats import Prepared
    from dvf.packs.files.storage import FileInfo

log = logging.getLogger(__name__)

_SQL = get_dialect("duckdb")

_EXTENSION_FORMATS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".txt": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".jsonl": "ndjson",
    ".ndjson": "ndjson",
}
_COMPRESSION_SUFFIXES = (".gz", ".zst", ".bz2", ".snappy")
_FILE_CONNECTION_FORMATS = {"avro", "orc", "excel", "xml", "fixed_width", "delta", "iceberg"}
_OPTION_ALIASES = {"delimiter": "delim", "separator": "sep"}
_DVF_TEXT_OPTIONS = ("skip_footer",)
"""Options of native formats that dvf handles itself (never passed to DuckDB)."""


def _duckdb_value(value: Any) -> str:
    """Render a Python value as a DuckDB literal, including lists and structs (for reader options)."""
    if isinstance(value, dict):
        items = ", ".join(f"{_SQL.literal(str(k))}: {_duckdb_value(v)}" for k, v in value.items())
        return "{" + items + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_duckdb_value(v) for v in value) + "]"
    return _SQL.literal(value)


def _is_url(path: str) -> bool:
    return "://" in path


def _table_function(fmt: str, locations: str | list[str], options: dict[str, Any]) -> str:
    """A DuckDB table function call reading CSV, TSV, Parquet, JSON, or NDJSON files."""
    options = dict(options)
    if fmt == "tsv":
        options.setdefault("delim", "\t")
    if fmt == "ndjson":
        options.setdefault("format", "newline_delimited")
    args = [_duckdb_value(locations)]
    for key, value in options.items():
        args.append(f"{_OPTION_ALIASES.get(key, key)} = {_duckdb_value(value)}")
    func = {"csv": "read_csv", "tsv": "read_csv", "parquet": "read_parquet"}.get(fmt, "read_json")
    return f"{func}({', '.join(args)})"


class DuckDBConnector(Connector):
    """An embedded DuckDB database, in memory or on disk."""

    dialect = "duckdb"
    Config: ClassVar[type[ConnectionConfig]] = DuckDBConnection
    _filter_alias: ClassVar[str] = "dvf_filtered"

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._con: duckdb.DuckDBPyConnection | None = None

    # ------------------------------------------------------------------ lifecycle
    def _database(self) -> str:
        assert isinstance(self.config, DuckDBConnection)
        database = self.config.database
        if database == ":memory:" or _is_url(database):
            return database
        path = Path(database)
        return str(path if path.is_absolute() else self.base_dir / path)

    def open(self) -> None:
        if self._con is not None:
            return
        database = self._database()
        read_only = self.config.read_only and database != ":memory:" and Path(database).exists()
        try:
            self._con = duckdb.connect(database, read_only=read_only)
            self._con.execute("SET TimeZone = 'UTC'")
            for extension in getattr(self.config, "extensions", []):
                self._con.install_extension(extension)
                self._con.load_extension(extension)
        except duckdb.Error as exc:
            raise ConnectorError(
                f"Could not open DuckDB database '{database}' for connection '{self.name}': {exc}"
            ) from exc

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def fork(self) -> Connector:
        """A session on the same database (a DuckDB cursor), safe to use from another thread."""
        twin = type(self)(self.name, self.config, self.base_dir)
        twin._con = self.connection.cursor()
        twin._con.execute("SET TimeZone = 'UTC'")
        return twin

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        self.open()
        assert self._con is not None
        return self._con

    def server_version(self) -> str | None:
        return f"DuckDB {duckdb.__version__}"

    # ------------------------------------------------------------------ queries
    def query(self, sql: str) -> pa.Table:
        try:
            return self.connection.execute(sql).to_arrow_table()
        except duckdb.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        # Bulk streams never depend on row order; preserving it makes DuckDB export Arrow on a
        # single thread (about six times slower for wide projections).
        con = self.connection
        con.execute("SET preserve_insertion_order = false")
        try:
            reader = con.execute(sql).to_arrow_reader(batch_size)
            yield from reader
        except duckdb.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        finally:
            con.execute("SET preserve_insertion_order = true")

    def register_arrow(self, name: str, table: pa.Table) -> bool:
        self.connection.register(name, table)
        return True

    def unregister_arrow(self, name: str) -> None:
        self.connection.unregister(name)

    # ------------------------------------------------------------------ datasets
    def _reader(self, path: str, options: dict[str, Any]) -> str:
        """A DuckDB table function reading ``path`` (relative to the base directory)."""
        options = dict(options)
        fmt = options.pop("format", None) or getattr(self.config, "format", None)
        location = path if _is_url(path) or Path(path).is_absolute() else self._base_path(path)
        if fmt is None:
            name = location.lower()
            for suffix in _COMPRESSION_SUFFIXES:
                name = name.removesuffix(suffix)
            fmt = _EXTENSION_FORMATS.get(Path(name).suffix)
        if fmt is None:
            raise ConnectorError(
                f"Cannot infer the file format of '{path}' on connection '{self.name}'; "
                "set 'format' on the connection or in the dataset's 'options'."
            )
        if fmt in _FILE_CONNECTION_FORMATS:
            raise ConnectorError(
                f"The '{fmt}' format is read by 'files' connections, not by connection "
                f"'{self.name}' ({self.dialect}); add a connection with type: files."
            )
        return _table_function(fmt, location, options)

    def _base_path(self, path: str) -> str:
        return (self.base_dir / path).as_posix()

    def _check_spec(self, spec: DatasetSpec) -> None:
        if spec.as_of is not None:
            raise ConnectorError(
                f"Connection '{self.name}' ({self.dialect}) does not support 'as_of' time travel."
            )

    def resolve(self, spec: DatasetSpec) -> Relation:
        self._check_spec(spec)
        table: str | None = None
        if spec.table is not None:
            base, table, kind, label = spec.table, spec.table, DatasetKind.TABLE, spec.table
        elif spec.query is not None:
            base, kind, label = f"(\n{spec.query}\n) AS dvf_query", DatasetKind.QUERY, "query"
        else:
            assert spec.path is not None
            base = self._reader(spec.path, spec.options)
            kind, label = DatasetKind.PATH, spec.path
        ref = DatasetRef(
            connection=self.name,
            kind=kind,
            name=label,
            query=spec.query if kind is DatasetKind.QUERY else None,
        )
        sql = base
        if spec.filter:
            sql = f"(SELECT * FROM {base} WHERE {spec.filter}) AS {self._filter_alias}"
        return Relation(
            ref=ref, sql=sql, base_sql=base, table=table, filter=spec.filter, connection=self.name
        )

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            rows = self.query(f"DESCRIBE {relation.table}").to_pylist()
            nullable_known = True
        else:
            rows = self.query(f"DESCRIBE SELECT * FROM {relation.base_sql}").to_pylist()
            nullable_known = False
        columns = tuple(
            ColumnSchema(
                name=row["column_name"],
                native_type=row["column_type"],
                type=parse_native_type(row["column_type"], "duckdb"),
                nullable=(row["null"] == "YES") if nullable_known else None,
                default=row.get("default"),
                ordinal=i + 1,
            )
            for i, row in enumerate(rows)
        )
        primary_key = tuple(row["column_name"] for row in rows if row.get("key") == "PRI")
        return DatasetSchema(ref=relation.ref, columns=columns, primary_key=primary_key)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        return self.get_schema(relation).primary_key

    def list_datasets(self, pattern: str) -> list[str]:
        rows = self.query(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')"
        ).to_pylist()
        from fnmatch import fnmatchcase

        names: list[str] = []
        for row in rows:
            qualified = f"{row['table_schema']}.{row['table_name']}"
            if fnmatchcase(qualified.lower(), pattern.lower()) or fnmatchcase(
                row["table_name"].lower(), pattern.lower()
            ):
                names.append(qualified if "." in pattern else row["table_name"])
        return sorted(names)

    # ------------------------------------------------------------------ catalog metadata
    def _catalog_tables(self) -> bool:
        """Catalog metadata applies to ``duckdb`` connections, not files or staged data."""
        return isinstance(self.config, DuckDBConnection)

    def get_physical_design(self, relation: Relation) -> PhysicalDesign | None:
        from dvf.packs.catalog.introspect import physical_design

        return physical_design(self, relation) if self._catalog_tables() else None

    def get_collations(self, relation: Relation) -> CollationInfo | None:
        from dvf.packs.catalog.introspect import collations

        return collations(self, relation) if self._catalog_tables() else None

    def get_object_inventory(self, relation: Relation) -> ObjectInventory | None:
        from dvf.packs.catalog.introspect import object_inventory

        return object_inventory(self, relation) if self._catalog_tables() else None


# ============================================================================ files


@dataclass
class BadRecords:
    """What :meth:`FileConnector.bad_records` found in a dataset."""

    records: int | None
    """Records read, good and bad (``None`` when the format cannot tell)."""
    bad: int
    """Records (or whole files) that could not be read."""
    rows: list[dict[str, Any]] = field(default_factory=list)
    """Up to ``limit`` bad records: ``file``, ``line``, ``column``, ``error_type``, ``message``,
    and ``record`` (the raw text or value)."""
    method: str = ""
    """How bad records were found, for the check's message."""
    files: int = 0


BAD_RECORD_COLUMNS = ("file", "line", "column", "error_type", "message", "record")


@dataclass
class _FileSource:
    """A files dataset: where it is, how it is read, and (once prepared) its PyArrow dataset or
    converted files."""

    key: str
    fmt: str
    location: str
    label: str
    options: Any
    """Validated format options (a model), or reader options (a dict) for native formats."""
    as_of: str | None = None
    view: str | None = None
    """View name for formats dvf prepares itself; ``None`` for DuckDB table functions."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    planned: Any = None
    """Delta, Iceberg, and ORC: the opened table or dataset (metadata only)."""
    prepared: Prepared | None = None
    """The data DuckDB reads (set once, shared by every session)."""


class _FileShared:
    """State shared by a file connection and the sessions forked from it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.folder: Path | None = None
        self.sources: dict[str, _FileSource] = {}
        """Datasets by their relation's ``base_sql``."""
        self.fsspec_protocols: set[str] = set()

    def temp_folder(self, temp_root: str | None) -> Path:
        with self.lock:
            if self.folder is None:
                self.folder = Path(tempfile.mkdtemp(prefix="dvf-files-", dir=temp_root))
            return self.folder

    def close(self) -> None:
        with self.lock:
            if self.folder is not None:
                shutil.rmtree(self.folder, ignore_errors=True)
                self.folder = None
            self.sources.clear()
            self.fsspec_protocols.clear()


_VIEW_RE = re.compile(r"dvf_file_[0-9a-f]{12}")
_EXTENSION_PROTOCOLS = {
    "http",
    "https",
    "s3",
    "s3a",
    "gs",
    "gcs",
    "az",
    "azure",
    "abfss",
    "hf",
    "r2",
}


class FileConnector(DuckDBConnector):
    """Files on local disk or object storage, read through DuckDB.

    CSV, TSV, Parquet, JSON, and NDJSON are read by DuckDB table functions. ORC, Delta Lake, and
    Iceberg are opened as PyArrow datasets and registered in DuckDB; Avro, Excel, fixed-width,
    and XML files are converted to Parquet in a temporary folder of the connection (shared by
    forked sessions and deleted when the connection closes). Preparation is lazy: it happens
    the first time a query reads the dataset.

    URLs are read through fsspec (registered as a DuckDB file system, with the connection's
    ``storage_options``), or through DuckDB extensions listed in ``options.extensions``.
    """

    Config = FileConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._shared = _FileShared()
        self._owner = True
        self._registered: set[str] = set()
        """PyArrow datasets registered in this session's DuckDB connection."""
        self.temp_root: str | None = None
        """Folder for converted files (``defaults.execution.temp_dir``); the system temp folder
        when unset."""

    # ------------------------------------------------------------------ lifecycle
    @property
    def file_config(self) -> FileConnection:
        assert isinstance(self.config, FileConnection)
        return self.config

    def _database(self) -> str:
        return ":memory:"

    def _root(self) -> Path:
        root = Path(self.file_config.path)
        return root if root.is_absolute() else self.base_dir / root

    def root(self) -> str:
        """The connection's base folder (absolute, forward slashes) or URL."""
        if _is_url(self.file_config.path):
            return self.file_config.path.rstrip("/")
        return self._root().as_posix()

    def _base_path(self, path: str) -> str:
        if _is_url(self.file_config.path):
            return self.file_config.path.rstrip("/") + "/" + path
        return (self._root() / path).as_posix()

    def _location(self, path: str) -> str:
        return path if _is_url(path) or Path(path).is_absolute() else self._base_path(path)

    @property
    def storage_options(self) -> dict[str, Any]:
        return dict(self.file_config.storage_options)

    def open(self) -> None:
        if self._con is not None:
            return
        self._con = duckdb.connect(":memory:")
        self._con.execute("SET TimeZone = 'UTC'")
        for extension in self.file_config.options.get("extensions", []):
            self._con.install_extension(extension)
            self._con.load_extension(extension)
        if _is_url(self.file_config.path):
            self._register_fsspec(self.file_config.path)

    def close(self) -> None:
        super().close()
        self._registered.clear()
        if self._owner:
            self._shared.close()

    def fork(self) -> Connector:
        """A session on the same database that shares converted files and planned datasets."""
        twin = type(self)(self.name, self.config, self.base_dir)
        twin._con = self.connection.cursor()
        twin._con.execute("SET TimeZone = 'UTC'")
        twin._shared = self._shared
        twin._owner = False
        twin.temp_root = self.temp_root
        twin.retry_policy = self.retry_policy
        return twin

    def _register_fsspec(self, url: str) -> None:
        """Serve ``url``'s protocol to DuckDB through fsspec, unless a DuckDB extension does."""
        from dvf.packs.files import storage

        protocol = storage.url_protocol(url)
        if protocol in self._shared.fsspec_protocols or protocol == "file":
            return
        if protocol in _EXTENSION_PROTOCOLS and self.file_config.options.get("extensions"):
            return
        try:
            filesystem = storage.fsspec_filesystem(url, self.storage_options)
            self.connection.register_filesystem(filesystem)
        except (ConnectorError, duckdb.Error) as exc:
            # DuckDB may still read the URL through an extension it autoloads.
            log.debug("fsspec file system for %s not registered: %s", protocol, exc)
            return
        self._shared.fsspec_protocols.add(protocol)

    # ------------------------------------------------------------------ resolving
    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.table is not None and spec.path is None:
            # Referential integrity parents, patterns, and Iceberg catalog tables name files
            # through 'table'.
            spec = spec.model_copy(update={"table": None, "path": spec.table})
        if spec.path is None:
            options = {k: v for k, v in self.file_config.options.items() if k != "extensions"}
            return super().resolve(spec.model_copy(update={"options": {**options, **spec.options}}))
        source = self._source(spec)
        sql = source.key
        if spec.filter:
            sql = f"(SELECT * FROM {source.key} WHERE {spec.filter}) AS {self._filter_alias}"
        return Relation(
            ref=DatasetRef(connection=self.name, kind=DatasetKind.PATH, name=spec.path),
            sql=sql,
            base_sql=source.key,
            table=None,
            filter=spec.filter,
            connection=self.name,
        )

    def _source(self, spec: DatasetSpec) -> _FileSource:
        from dvf.packs.files import formats

        assert spec.path is not None
        conn_options = {k: v for k, v in self.file_config.options.items() if k != "extensions"}
        dataset_options = dict(spec.options)
        fmt = (
            dataset_options.pop("format", None)
            or conn_options.pop("format", None)
            or self.file_config.format
        )
        conn_options.pop("format", None)
        catalog = fmt == "iceberg" and ("catalog" in dataset_options or "catalog" in conn_options)
        location = spec.path if catalog else self._location(spec.path)
        if fmt is None:
            fmt = formats.detect_format(location)
        if fmt is None:
            raise ConnectorError(
                f"Cannot infer the file format of '{spec.path}' on connection '{self.name}'; "
                "set 'format' on the connection or in the dataset's 'options'."
            )
        known = formats.NATIVE_FORMATS | formats.ARROW_FORMATS | formats.CONVERTED_FORMATS
        if fmt not in known:
            raise ConnectorError(
                f"Unknown file format '{fmt}' for '{spec.path}'; use one of {', '.join(sorted(known))}."
            )
        if spec.as_of is not None and fmt not in formats.TIME_TRAVEL_FORMATS:
            raise ConnectorError(
                f"Connection '{self.name}' supports 'as_of' time travel only for Delta and "
                f"Iceberg tables, not for '{fmt}' dataset '{spec.path}'."
            )
        if _is_url(location) and not catalog:
            self._register_fsspec(location)
        options: Any
        view: str | None = None
        prepared_by_dvf = True
        if fmt in formats.NATIVE_FORMATS:
            options = {**conn_options, **dataset_options}
            prepared_by_dvf = formats.needs_text_copy(fmt, [location], options)
            if not prepared_by_dvf:
                key = self._native_sql(fmt, location, options)
        else:
            options = formats.format_options(fmt, dataset_options, conn_options)
        if prepared_by_dvf:
            digest = hashlib.sha256(
                json.dumps(
                    [fmt, location, _jsonable(options), spec.as_of], sort_keys=True, default=str
                ).encode()
            ).hexdigest()
            view = f"dvf_file_{digest[:12]}"
            key = f'"{view}"'
        with self._shared.lock:
            existing = self._shared.sources.get(key)
            if existing is not None:
                return existing
            source = _FileSource(
                key=key,
                fmt=fmt,
                location=location,
                label=spec.path,
                options=options,
                as_of=spec.as_of,
                view=view,
            )
            self._shared.sources[key] = source
            return source

    def _native_sql(self, fmt: str, location: str, options: dict[str, Any]) -> str:
        """The DuckDB table function for native formats; a folder becomes its data files."""
        from dvf.packs.files import formats, storage

        options = {k: v for k, v in options.items() if k not in _DVF_TEXT_OPTIONS}
        locations: str | list[str] = location
        if not _is_url(location) and "*" not in location and Path(location).is_dir():
            files = [
                f
                for f in storage.list_files(location, self.root())
                if formats.belongs_to(f.name, fmt)
            ]
            if files:
                locations = [f.path for f in files]
                base = Path(location).as_posix().rstrip("/") + "/"
                if "hive_partitioning" not in options and any(
                    "=" in part for f in files for part in f.path.removeprefix(base).split("/")[:-1]
                ):
                    options["hive_partitioning"] = True
        return _table_function(fmt, locations, options)

    def _source_of(self, relation: Relation) -> _FileSource | None:
        return self._shared.sources.get(relation.base_sql)

    # ------------------------------------------------------------------ preparing
    def _plan(self, source: _FileSource) -> Any:
        """Open a Delta, Iceberg, or ORC dataset (metadata only), once."""
        from dvf.packs.files import formats

        if source.planned is not None:
            return source.planned
        with source.lock:
            if source.planned is None:
                try:
                    if source.fmt == "delta":
                        source.planned = formats.open_delta(
                            source.location, source.as_of, self.storage_options
                        )
                    elif source.fmt == "iceberg":
                        source.planned = formats.open_iceberg(
                            source.location, source.as_of, source.options
                        )
                    elif source.fmt == "orc":
                        source.planned = formats.open_orc(
                            source.location,
                            self._dataset_files(source),
                            source.options,
                            self.storage_options,
                        )
                except ConnectorError:
                    raise
                except Exception as exc:
                    raise ConnectorError(
                        f"Could not open {source.fmt} dataset '{source.label}' on connection "
                        f"'{self.name}': {exc}"
                    ) from exc
            return source.planned

    def _prepare(self, source: _FileSource) -> Prepared:
        """Prepare the data DuckDB reads (thread-safe, once per connection)."""
        if source.prepared is not None:
            return source.prepared
        planned = self._plan(source) if source.fmt in ("delta", "iceberg", "orc") else None
        with source.lock:
            if source.prepared is None:
                try:
                    prepared = self._build(source, planned)
                except ConnectorError:
                    raise
                except Exception as exc:
                    raise ConnectorError(
                        f"Could not read {source.fmt} dataset '{source.label}' on connection "
                        f"'{self.name}': {exc}"
                    ) from exc
                self._create_views(source, prepared)
                source.prepared = prepared
            return source.prepared

    def _build(self, source: _FileSource, planned: Any) -> Prepared:
        from dvf.packs.files import formats

        if isinstance(planned, formats.ArrowSource):
            return planned
        assert source.view is not None
        folder = self._shared.temp_folder(self.temp_root) / source.view
        if isinstance(planned, formats.StagedScan):
            return planned.stage(folder / "part-00000.parquet")
        files = self._dataset_files(source)
        if not files:
            raise ConnectorError(
                f"No file matches '{source.label}' on connection '{self.name}' ({source.location})."
            )
        if source.fmt in formats.NATIVE_FORMATS:
            # Copies are always UTF-8: they are read without the dataset's 'encoding'.
            encoding = source.options.get("encoding")
            text_paths = []
            for i, file in enumerate(files):
                out = folder / f"part-{i:05d}{_text_suffix(source.fmt)}"
                formats.copy_text(
                    file,
                    out,
                    encoding if isinstance(encoding, str) else None,
                    int(source.options.get("skip_footer") or 0),
                    self.storage_options,
                )
                text_paths.append(out)
            return formats.TextSource(text_paths, files)
        result = formats.ParquetSource([], files)
        options = source.options
        for i, file in enumerate(files):
            out = folder / f"part-{i:05d}.parquet"
            if source.fmt == "avro":
                formats.convert_avro(file, out, self.storage_options)
            elif source.fmt == "excel":
                formats.convert_excel(file, out, options, self.storage_options)
            elif source.fmt == "fixed_width":
                formats.convert_fixed_width(
                    file, out, options, self.storage_options, result.bad_rows
                )
            elif source.fmt == "xml":
                formats.convert_xml(file, out, options, self.storage_options)
            result.paths.append(out)
        result.types = options.all_types() if source.fmt == "fixed_width" else dict(options.types)
        result.ignore_errors = bool(options.ignore_errors)
        return result

    def _create_views(self, source: _FileSource, prepared: Prepared) -> None:
        from dvf.packs.files import formats

        if source.view is None or isinstance(prepared, formats.ArrowSource):
            return
        paths = [p.as_posix() for p in prepared.paths]
        if isinstance(prepared, formats.TextSource):
            options = _copy_reader_options(source.options)
            body = f"SELECT * FROM {_table_function(source.fmt, paths, options)}"
            self._execute(f'CREATE OR REPLACE VIEW "{source.view}" AS {body}')
            return
        raw = f"read_parquet({_duckdb_value(paths)}, union_by_name = true)"
        self._execute(f'CREATE OR REPLACE VIEW "{source.view}__raw" AS SELECT * FROM {raw}')
        body = f'SELECT * FROM "{source.view}__raw"'
        if prepared.types:
            cast = "TRY_CAST" if prepared.ignore_errors else "CAST"
            replace = ", ".join(
                f"{cast}({_SQL.quote(name)} AS {ctype}) AS {_SQL.quote(name)}"
                for name, ctype in prepared.types.items()
            )
            body = f'SELECT * REPLACE ({replace}) FROM "{source.view}__raw"'
        self._execute(f'CREATE OR REPLACE VIEW "{source.view}" AS {body}')

    def _execute(self, sql: str) -> None:
        try:
            self.connection.execute(sql)
        except duckdb.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc

    def _ensure(self, source: _FileSource) -> None:
        from dvf.packs.files import formats

        prepared = self._prepare(source)
        if isinstance(prepared, formats.ArrowSource) and source.view not in self._registered:
            assert source.view is not None
            self.connection.register(source.view, prepared.dataset)
            self._registered.add(source.view)

    def _ensure_mentioned(self, sql: str) -> None:
        for view in sorted(set(_VIEW_RE.findall(sql))):
            source = self._shared.sources.get(f'"{view}"')
            if source is not None:
                self._ensure(source)

    # ------------------------------------------------------------------ queries
    def query(self, sql: str) -> pa.Table:
        self._ensure_mentioned(sql)
        return super().query(sql)

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        self._ensure_mentioned(sql)
        yield from super().iter_batches(sql, batch_size)

    # ------------------------------------------------------------------ datasets
    def exists(self, relation: Relation) -> bool:
        source = self._source_of(relation)
        if source is None:
            return super().exists(relation)
        try:
            if source.fmt == "delta":
                from dvf.packs.files import formats

                return formats.delta_exists(source.location, self.storage_options)
            if source.fmt == "iceberg":
                self._plan(source)
                return True
            return bool(self._dataset_files(source))
        except ConnectorError:
            if _is_url(source.location) and source.fmt not in ("delta", "iceberg"):
                return super().exists(relation)
            return False

    def list_datasets(self, pattern: str) -> list[str]:
        from dvf.packs.files import storage

        if _is_url(self.file_config.path):
            files = storage.list_files(self._base_path(pattern), self.root(), self.storage_options)
            return sorted(f.name for f in files)
        root = self._root()
        return sorted(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())

    def test_connection(self) -> ConnectionReport:
        if not _is_url(self.file_config.path) and not self._root().is_dir():
            return ConnectionReport(False, f"folder not found: {self._root()}")
        report = super().test_connection()
        if report.ok:
            return ConnectionReport(True, f"reading files in {self.root()}", report.server_version)
        return report

    # ------------------------------------------------------------------ file-level access
    def _files_at(self, location: str) -> list[FileInfo]:
        from dvf.packs.files import storage

        return storage.list_files(location, self.root(), self.storage_options)

    def _dataset_files(self, source: _FileSource) -> list[FileInfo]:
        """The files of a dataset; in a folder, only those of the dataset's format."""
        from dvf.packs.files import formats

        files = self._files_at(source.location)
        if any(f.path == source.location for f in files):
            return files
        return [f for f in files if formats.belongs_to(f.name, source.fmt)]

    def list_files(
        self, relation: Relation | None = None, pattern: str | None = None
    ) -> list[FileInfo]:
        """The files behind a dataset, or those matching ``pattern``.

        ``pattern`` is a path or glob (``**`` matches folders recursively) relative to the
        connection's folder, or an absolute path or URL. A folder stands for its data files
        (names starting with ``_`` or ``.`` and ``.crc`` files are skipped). For Delta and Iceberg
        datasets the files are the data files of the version that is read. Each
        :class:`~dvf.packs.files.storage.FileInfo` has the path, the name relative to the
        connection's folder, the size in bytes, and the modification time in UTC.
        """
        if pattern is not None:
            return self._files_at(self._location(pattern))
        if relation is None:
            raise ConnectorError("list_files needs a relation or a pattern.")
        source = self._source_of(relation)
        if source is None:
            if relation.ref.kind is DatasetKind.PATH:
                return self._files_at(self._location(relation.ref.name))
            raise ConnectorError(
                f"Dataset {relation.ref.display()} is a query, not files; give the file check a "
                "'path' (a file or glob relative to the connection's folder)."
            )
        if source.fmt in ("delta", "iceberg"):
            planned = self._plan(source)
            return list(planned.files)
        return self._dataset_files(source)

    def dataset_format(self, relation: Relation) -> str | None:
        """The file format of a resolved dataset, or ``None`` for queries."""
        source = self._source_of(relation)
        return None if source is None else source.fmt

    def dataset_options(self, relation: Relation) -> Any:
        """The reader options of a resolved dataset (a dict for CSV, TSV, JSON, NDJSON, and
        Parquet; the validated options model for other formats)."""
        source = self._source_of(relation)
        return None if source is None else source.options

    def bad_records(self, relation: Relation, limit: int = 100) -> BadRecords:
        """Records that cannot be read, with up to ``limit`` samples.

        * CSV and TSV: rows DuckDB rejects (cast errors, too many or missing columns, unquoted
          values, invalid encoding), read with the dataset's options and ``store_rejects``;
        * NDJSON: lines that are not JSON objects;
        * fixed width: records whose length does not fit the layout;
        * Excel: cells holding error values (``#N/A``, ``#DIV/0!``, ...);
        * Avro, Excel, fixed width, and XML with ``types``: values that do not convert;
        * every other format, and files that cannot be opened at all: unreadable files.
        """
        source = self._source_of(relation)
        if source is None:
            raise ConnectorError(
                f"Dataset {relation.ref.display()} is a query; bad_records needs a files dataset."
            )
        if source.fmt in ("csv", "tsv"):
            return self._csv_rejects(source, limit)
        if source.fmt == "ndjson":
            return self._ndjson_bad(source, limit)
        if source.fmt in ("fixed_width", "excel", "avro", "xml"):
            return self._converted_bad(source, limit)
        return self._unreadable_files(source, limit)

    def _csv_rejects(self, source: _FileSource, limit: int) -> BadRecords:
        from dvf.packs.files import formats

        options = {
            k: v
            for k, v in source.options.items()
            if k not in (*_DVF_TEXT_OPTIONS, "ignore_errors", "store_rejects")
        }
        if source.view is not None:
            prepared = self._prepare(source)
            assert isinstance(prepared, formats.TextSource)
            locations: str | list[str] = [p.as_posix() for p in prepared.paths]
            options = _copy_reader_options(options)
            names = {
                p.as_posix(): f.name for p, f in zip(prepared.paths, prepared.files, strict=True)
            }
        else:
            files = self._dataset_files(source)
            locations = [f.path for f in files] if files else source.location
            names = {f.path: f.name for f in files}
        tag = uuid.uuid4().hex[:12]
        rejects, scans = f"dvf_rejects_{tag}", f"dvf_reject_scans_{tag}"
        options.update({"store_rejects": True, "rejects_table": rejects, "rejects_scan": scans})
        reader = _table_function(source.fmt, locations, options)
        try:
            # COUNT of every column makes DuckDB convert every value, so cast errors surface.
            good = int(self._scalar(f"SELECT COUNT(*), COUNT(COLUMNS(*)) FROM {reader}") or 0)
            bad = int(
                self._scalar(f"SELECT COUNT(*) FROM (SELECT DISTINCT file_id, line FROM {rejects})")
            )
            rows = self.query(
                f'SELECT s.file_path AS file, r.line, r.column_name AS "column", '
                f"CAST(r.error_type AS VARCHAR) AS error_type, r.error_message AS message, "
                f"TRIM(r.csv_line, chr(10) || chr(13)) AS record\n"
                f"FROM {rejects} AS r JOIN {scans} AS s USING (scan_id, file_id)\n"
                f"ORDER BY s.file_path, r.line LIMIT {int(limit)}"
            ).to_pylist()
        finally:
            for table in (rejects, scans):
                try:
                    self.connection.execute(f"DROP TABLE IF EXISTS {table}")
                except duckdb.Error:
                    log.debug("could not drop %s", table)
        for row in rows:
            row["file"] = names.get(str(row["file"]), str(row["file"]))
        return BadRecords(good + bad, bad, rows, "rows rejected by the CSV reader", len(names))

    def _scalar(self, sql: str) -> Any:
        table = self.query(sql)
        return table.column(0)[0].as_py() if table.num_rows else None

    def _ndjson_bad(self, source: _FileSource, limit: int) -> BadRecords:
        from dvf.packs.files import formats

        encoding = source.options.get("encoding") or "utf-8"
        records = bad = 0
        rows: list[dict[str, Any]] = []
        files = self._dataset_files(source)
        for file in files:
            total, failed, samples = formats.ndjson_bad_lines(
                file, self.storage_options, limit - len(rows), encoding
            )
            records += total
            bad += failed
            rows.extend(samples)
        return BadRecords(records, bad, rows, "lines that are not JSON objects", len(files))

    def _converted_bad(self, source: _FileSource, limit: int) -> BadRecords:
        from dvf.packs.files import formats

        rows: list[dict[str, Any]] = []
        bad = 0
        records: int | None = None
        files = self._dataset_files(source)
        methods = []
        options = source.options
        if source.fmt == "fixed_width":
            records = 0
            for file in files:
                total, failed, samples = formats.fixed_width_bad_records(
                    file, options, self.storage_options, max(limit - len(rows), 0)
                )
                records += total
                bad += failed
                rows.extend(samples)
            methods.append("records that do not fit the layout")
        if source.fmt == "excel":
            for file in files:
                failed, samples = formats.excel_error_cells(
                    file, options, self.storage_options, max(limit - len(rows), 0)
                )
                bad += failed
                rows.extend(samples)
            methods.append("cells holding Excel errors")
        unreadable = self._unreadable_files(source, limit)
        bad += unreadable.bad
        rows.extend(unreadable.rows[: max(limit - len(rows), 0)])
        if unreadable.bad < len(files):
            types = options.all_types() if source.fmt == "fixed_width" else dict(options.types)
            if types:
                failed, samples, total = self._type_failures(source, types, limit - len(rows))
                bad += failed
                rows.extend(samples)
                if records is None:
                    records = total
                methods.append("values that do not convert to their type")
        methods.append("files that cannot be read")
        return BadRecords(records, bad, rows, ", ".join(methods), len(files))

    def _type_failures(
        self, source: _FileSource, types: dict[str, str], limit: int
    ) -> tuple[int, list[dict[str, Any]], int]:
        self._prepare(source)
        raw = f'"{source.view}__raw"'
        checks = [
            (
                name,
                f"({_SQL.quote(name)} IS NOT NULL AND TRY_CAST({_SQL.quote(name)} AS {ctype}) IS NULL)",
                ctype,
            )
            for name, ctype in types.items()
        ]
        any_bad = " OR ".join(c for _, c, _ in checks)
        row = self.query(
            f"SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN {any_bad} THEN 1 ELSE 0 END), 0) AS bad FROM {raw}"
        ).to_pylist()[0]
        samples: list[dict[str, Any]] = []
        if int(row["bad"]) and limit > 0:
            union = "\nUNION ALL\n".join(
                f'SELECT {_SQL.literal(name)} AS "column", CAST({_SQL.quote(name)} AS VARCHAR) AS record, '
                f"{_SQL.literal(ctype)} AS ctype FROM {raw} WHERE {check}"
                for name, check, ctype in checks
            )
            for item in self.query(
                f"SELECT * FROM ({union}) AS dvf_bad LIMIT {int(limit)}"
            ).to_pylist():
                samples.append(
                    {
                        "file": source.label,
                        "line": None,
                        "column": item["column"],
                        "error_type": "CAST",
                        "message": f"'{item['record']}' is not a valid {item['ctype']}",
                        "record": item["record"],
                    }
                )
        return int(row["bad"]), samples, int(row["total"])

    def _unreadable_files(self, source: _FileSource, limit: int) -> BadRecords:
        """Files that cannot be read in full (corrupt Parquet, ORC, Avro, JSON, XML, ...)."""
        if source.fmt in ("delta", "iceberg"):
            files = list(self._plan(source).files)
            fmt = "parquet"
        else:
            files = self._dataset_files(source)
            fmt = source.fmt
        rows: list[dict[str, Any]] = []
        bad = 0
        for file in files:
            try:
                self._read_whole_file(fmt, file, source)
            except Exception as exc:
                bad += 1
                if len(rows) < limit:
                    message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    rows.append(
                        {
                            "file": file.name,
                            "line": None,
                            "column": None,
                            "error_type": "UNREADABLE FILE",
                            "message": message,
                            "record": None,
                        }
                    )
        return BadRecords(None, bad, rows, "files that cannot be read", len(files))

    def _read_whole_file(self, fmt: str, file: FileInfo, source: _FileSource) -> None:
        from dvf.packs.files import formats

        scratch = self._shared.temp_folder(self.temp_root) / "probe"
        if fmt in ("parquet", "json"):
            reader = _table_function(
                fmt,
                file.path,
                {}
                if fmt == "parquet"
                else {k: v for k, v in source.options.items() if k not in _DVF_TEXT_OPTIONS},
            )
            self.connection.execute(f"SELECT COUNT(COLUMNS(*)) FROM {reader}").fetchall()
        elif fmt == "orc":
            import pyarrow.orc as orc

            from dvf.packs.files import storage

            formats.ensure_orc_time_zones()

            with storage.open_binary(file.path, self.storage_options) as handle:
                orc_file = orc.ORCFile(handle)
                for stripe in range(orc_file.nstripes):
                    orc_file.read_stripe(stripe)
        elif fmt == "avro":
            formats.convert_avro(
                file, scratch / f"{uuid.uuid4().hex}.parquet", self.storage_options
            )
        elif fmt == "excel":
            formats.convert_excel(
                file, scratch / f"{uuid.uuid4().hex}.parquet", source.options, self.storage_options
            )
        elif fmt == "xml":
            for _ in formats.iter_xml_rows(file, source.options.row_path, self.storage_options):
                pass
        elif fmt == "fixed_width":
            for _ in formats._records(file, source.options, self.storage_options):
                pass
        shutil.rmtree(scratch, ignore_errors=True)


def _text_suffix(fmt: str) -> str:
    return {"csv": ".csv", "tsv": ".tsv", "json": ".json", "ndjson": ".ndjson"}.get(fmt, ".txt")


def _copy_reader_options(options: dict[str, Any]) -> dict[str, Any]:
    """Reader options for dvf's UTF-8 copies of text files (no encoding, no dvf options)."""
    return {k: v for k, v in options.items() if k not in (*_DVF_TEXT_OPTIONS, "encoding")}


def _jsonable(options: Any) -> Any:
    if hasattr(options, "model_dump"):
        return options.model_dump(mode="json")
    return options


__all__ = ["BAD_RECORD_COLUMNS", "BadRecords", "DuckDBConnector", "FileConnector"]
