"""DuckDB connector, and the file connector built on it."""

from __future__ import annotations

import glob as globmod
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import pyarrow as pa

from dvf.config.connections import ConnectionConfig, DuckDBConnection, FileConnection
from dvf.config.models import DatasetSpec
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError
from dvf.core.types import parse_native_type
from dvf.plugins.base import ConnectionReport, Connector, Relation
from dvf.sql.dialects import get_dialect

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
_PHASE3_FORMATS = {"avro", "orc", "excel", "xml", "fixed_width", "delta", "iceberg"}
_OPTION_ALIASES = {"delimiter": "delim", "separator": "sep"}


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
        try:
            reader = self.connection.execute(sql).to_arrow_reader(batch_size)
            yield from reader
        except duckdb.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc

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
        if fmt in _PHASE3_FORMATS:
            raise ConnectorError(
                f"The '{fmt}' format is not supported yet (planned for Phase 3); "
                "supported formats: csv, tsv, parquet, json, ndjson."
            )
        args = [_SQL.literal(location)]
        if fmt == "tsv":
            options.setdefault("delim", "\t")
        if fmt == "ndjson":
            options.setdefault("format", "newline_delimited")
        for key, value in options.items():
            args.append(f"{_OPTION_ALIASES.get(key, key)} = {_duckdb_value(value)}")
        func = {"csv": "read_csv", "tsv": "read_csv", "parquet": "read_parquet"}.get(
            fmt, "read_json"
        )
        return f"{func}({', '.join(args)})"

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


class FileConnector(DuckDBConnector):
    """CSV, TSV, Parquet, JSON, and NDJSON files on a local disk, read through DuckDB.

    Object storage URLs work when the matching DuckDB extension (for example ``httpfs``) is
    listed in the connection's ``options.extensions``.
    """

    Config = FileConnection

    def _database(self) -> str:
        return ":memory:"

    def _root(self) -> Path:
        assert isinstance(self.config, FileConnection)
        root = Path(self.config.path)
        return root if root.is_absolute() else self.base_dir / root

    def _base_path(self, path: str) -> str:
        assert isinstance(self.config, FileConnection)
        if _is_url(self.config.path):
            return self.config.path.rstrip("/") + "/" + path
        return (self._root() / path).as_posix()

    def open(self) -> None:
        if self._con is not None:
            return
        assert isinstance(self.config, FileConnection)
        self._con = duckdb.connect(":memory:")
        self._con.execute("SET TimeZone = 'UTC'")
        for extension in self.config.options.get("extensions", []):
            self._con.install_extension(extension)
            self._con.load_extension(extension)

    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.table is not None and spec.path is None:
            # Referential integrity parents and patterns name files through 'table'.
            spec = spec.model_copy(update={"table": None, "path": spec.table})
        assert isinstance(self.config, FileConnection)
        options = {k: v for k, v in self.config.options.items() if k != "extensions"}
        spec = spec.model_copy(update={"options": {**options, **spec.options}})
        return super().resolve(spec)

    def exists(self, relation: Relation) -> bool:
        if relation.ref.kind is DatasetKind.PATH and not _is_url(relation.ref.name):
            location = Path(self._base_path(relation.ref.name))
            if not globmod.has_magic(relation.ref.name):
                return location.exists()
            return bool(globmod.glob(str(location)))  # noqa: PTH207 - absolute glob pattern
        return super().exists(relation)

    def list_datasets(self, pattern: str) -> list[str]:
        root = self._root()
        return sorted(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())

    def test_connection(self) -> ConnectionReport:
        assert isinstance(self.config, FileConnection)
        if not _is_url(self.config.path) and not self._root().is_dir():
            return ConnectionReport(False, f"folder not found: {self._root()}")
        report = super().test_connection()
        if report.ok:
            return ConnectionReport(True, f"reading files in {self._root()}", report.server_version)
        return report
