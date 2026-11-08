"""Connectors for systems without SQL: stage datasets locally, then query them with DuckDB.

Document stores (MongoDB, DynamoDB, ...) and application interfaces (SAP RFC) cannot run the SQL
that checks and comparisons generate. A :class:`StagedConnector` fetches each dataset once, as
Arrow record batches, writes them to a Parquet file in a temporary folder, and exposes the file as
a view in an embedded DuckDB database. Every check, comparison strategy, and engine then works
unchanged, using the ``duckdb`` dialect.

Staging is lazy: resolving a dataset only names its view. The data is fetched the first time a
query mentions the view, so ``dvf explain`` and schema checks stay cheap when a subclass can
describe a dataset without reading it (:meth:`StagedConnector.describe`). Memory stays bounded:
batches are written as they arrive. Sessions forked for other threads share the staged files.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from dvf.config.models import DatasetSpec
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType, parse_native_type
from dvf.plugins.base import Connector, Relation


@dataclass
class StagedData:
    """What a subclass returns from :meth:`StagedConnector.fetch`."""

    schema: pa.Schema
    """The Arrow schema every batch is cast to."""
    batches: Iterable[pa.RecordBatch]
    """The rows, streamed. Batches are renamed and cast to :attr:`schema` before writing."""
    native_types: Mapping[str, str] = field(default_factory=dict)
    """Type names in the source system's terms (for example BSON types or ABAP types), shown in
    schema checks instead of the DuckDB type."""
    canonical_types: Mapping[str, CanonicalType] = field(default_factory=dict)
    """Canonical types that override the ones derived from the Arrow schema (for example DDIC
    lengths, or a decimal precision the Arrow type cannot carry)."""
    primary_key: tuple[str, ...] = ()
    nullable: Mapping[str, bool] = field(default_factory=dict)
    comments: Mapping[str, str] = field(default_factory=dict)
    """Notes per column shown as the column comment in schemas (for example how a column with
    mixed source types was converted)."""


@dataclass
class _Stage:
    view: str
    spec: DatasetSpec
    data: StagedData | None = None
    """Set once the dataset has been fetched (schema metadata is kept after staging)."""
    path: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Shared:
    """State shared by a connector and the sessions forked from it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.con: duckdb.DuckDBPyConnection | None = None
        self.folder: Path | None = None
        self.stages: dict[str, _Stage] = {}
        """Staged datasets by view name."""
        self.by_key: dict[str, str] = {}
        """View name by dataset key (equal specifications share one view)."""

    def connection(self, temp_root: str | None) -> duckdb.DuckDBPyConnection:
        with self.lock:
            if self.con is None:
                self.folder = Path(tempfile.mkdtemp(prefix="dvf-stage-", dir=temp_root))
                self.con = duckdb.connect(":memory:")
                self.con.execute("SET TimeZone = 'UTC'")
                spill = (self.folder / "spill").as_posix().replace("'", "''")
                self.con.execute(f"SET temp_directory = '{spill}'")
            return self.con

    def close(self) -> None:
        with self.lock:
            if self.con is not None:
                self.con.close()
                self.con = None
            if self.folder is not None:
                shutil.rmtree(self.folder, ignore_errors=True)
                self.folder = None
            self.stages.clear()
            self.by_key.clear()


_VIEW_RE = re.compile(r"dvf_stage_[0-9a-f]{12}")


class StagedConnector(Connector):
    """Base class for connectors whose datasets are staged locally and queried with DuckDB.

    Subclasses implement :meth:`open_source`, :meth:`close_source`, and :meth:`fetch`, and may
    override :meth:`describe`, :meth:`dataset_exists`, :meth:`list_datasets`, and
    :meth:`native_spec_key`. Dataset ``filter`` values are SQL in the DuckDB dialect, applied to
    the staged rows; push filters down to the source system through dataset ``options`` that
    :meth:`fetch` understands.

    A ``query`` dataset that mentions a staged view (dvf builds these internally, for example to
    type ``source_expr`` derivations) is SQL over the staging database: it is never sent to
    :meth:`fetch`, and the views it mentions are staged on first use.
    """

    dialect = "duckdb"

    stage_batch_rows: int = 50_000
    """Rows per Parquet row group written while staging."""

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._shared = _Shared()
        self._owner = True
        self._cursor: duckdb.DuckDBPyConnection | None = None
        self._source_open = False
        self.temp_root: str | None = None
        """Folder for staged files (``defaults.execution.temp_dir``); the system temp folder
        when unset."""

    # ------------------------------------------------------------------ subclass API
    def open_source(self) -> None:
        """Connect to the source system (called once per session, before the first fetch)."""

    def close_source(self) -> None:
        """Disconnect from the source system."""

    def fetch(self, spec: DatasetSpec) -> StagedData:  # pragma: no cover - interface
        """Read the dataset named by ``spec`` from the source system."""
        raise NotImplementedError(f"{type(self).__name__} does not implement fetch()")

    def describe(self, spec: DatasetSpec) -> StagedData | None:
        """The dataset's schema without reading it, when the source system can tell cheaply.

        Return ``None`` (the default) to stage the dataset and read its schema from the staged
        file. ``batches`` of the returned object are ignored.
        """
        return None

    def dataset_exists(self, spec: DatasetSpec) -> bool | None:
        """Whether the dataset exists, when the source can tell without reading it.

        Return ``None`` (the default) to decide by staging it: a fetch that raises
        :class:`~dvf.core.errors.ConnectorError` means it does not exist.
        """
        return None

    def native_spec_key(self, spec: DatasetSpec) -> dict[str, Any]:
        """The parts of ``spec`` that select data in the source system (not the SQL filter)."""
        data = spec.model_dump(mode="json", exclude={"filter"}, exclude_none=True)
        return dict(data)

    def dataset_label(self, spec: DatasetSpec) -> tuple[DatasetKind, str]:
        """How the dataset is named in results."""
        if spec.table is not None:
            return DatasetKind.TABLE, spec.table
        if spec.path is not None:
            return DatasetKind.PATH, spec.path
        return DatasetKind.TABLE, "dataset"

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if not self._source_open:
            self.open_source()
            self._source_open = True

    def close(self) -> None:
        if self._source_open:
            try:
                self.close_source()
            finally:
                self._source_open = False
        if self._cursor is not None:
            self._cursor.close()
            self._cursor = None
        if self._owner:
            self._shared.close()

    def fork(self) -> Connector:
        twin = type(self)(self.name, self.config, self.base_dir)
        twin._shared = self._shared
        twin._owner = False
        twin.temp_root = self.temp_root
        twin.retry_policy = self.retry_policy
        return twin

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """This session's cursor on the shared staging database."""
        if self._cursor is None:
            con = self._shared.connection(self.temp_root)
            self._cursor = con.cursor()
            self._cursor.execute("SET TimeZone = 'UTC'")
        return self._cursor

    def server_version(self) -> str | None:
        return None

    def test_connection(self) -> Any:
        from dvf.plugins.base import ConnectionReport

        try:
            self.open()
            version = self.server_version()
        except Exception as exc:
            return ConnectionReport(False, f"{type(exc).__name__}: {exc}")
        return ConnectionReport(True, "connected", version)

    # ------------------------------------------------------------------ datasets
    def spec_key(self, spec: DatasetSpec) -> str:
        payload = json.dumps(self.native_spec_key(spec), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.query is not None and mentions_staged_view(spec.query):
            return self._staging_query(spec)
        key = self.spec_key(spec)
        with self._shared.lock:
            view = self._shared.by_key.get(key)
            if view is None:
                view = f"dvf_stage_{key[:12]}"
                self._shared.by_key[key] = view
                self._shared.stages[view] = _Stage(view=view, spec=spec)
        kind, label = self.dataset_label(spec)
        quoted = f'"{view}"'
        sql = quoted
        if spec.filter:
            sql = f"(SELECT * FROM {quoted} WHERE {spec.filter}) AS dvf_filtered"
        return Relation(
            ref=DatasetRef(connection=self.name, kind=kind, name=label),
            sql=sql,
            base_sql=quoted,
            table=quoted,
            filter=spec.filter,
            connection=self.name,
        )

    def _staging_query(self, spec: DatasetSpec) -> Relation:
        """A SQL query over the staged views of this connection."""
        assert spec.query is not None
        base = f"({spec.query}) AS dvf_query"
        sql = base
        if spec.filter:
            sql = f"(SELECT * FROM {base} WHERE {spec.filter}) AS dvf_filtered"
        return Relation(
            ref=DatasetRef(
                connection=self.name, kind=DatasetKind.QUERY, name="query", query=spec.query
            ),
            sql=sql,
            base_sql=base,
            filter=spec.filter,
            connection=self.name,
        )

    @staticmethod
    def _is_staging_query(relation: Relation) -> bool:
        return relation.ref.kind is DatasetKind.QUERY and relation.base_sql.endswith(
            " AS dvf_query"
        )

    def spec_for(self, relation: Relation) -> DatasetSpec:
        """The dataset specification ``relation`` was resolved from."""
        return self._stage_for(relation).spec

    def _stage_for(self, relation: Relation) -> _Stage:
        view = relation.base_sql.strip('"')
        stage = self._shared.stages.get(view)
        if stage is None:
            raise ConnectorError(
                f"Dataset {relation.ref.display()} was not resolved by connection '{self.name}'"
            )
        return stage

    def ensure_staged(self, stage: _Stage) -> None:
        """Fetch and stage a dataset if that has not happened yet (thread-safe)."""
        if _staged(stage):
            return
        with stage.lock:
            if _staged(stage):  # another thread staged it while this one waited
                return
            self.open()
            con = self.connection
            assert self._shared.folder is not None
            path = self._shared.folder / f"{stage.view}.parquet"
            data = self.fetch(stage.spec)
            rows = write_parquet_stream(path, data.schema, data.batches, self.stage_batch_rows)
            location = path.as_posix().replace("'", "''")
            con.execute(
                f'CREATE OR REPLACE VIEW "{stage.view}" AS '
                f"SELECT * FROM read_parquet('{location}')"
            )
            data.batches = ()
            stage.data = data
            stage.path = path
            self.on_staged(stage.spec, rows)

    def on_staged(self, spec: DatasetSpec, rows: int) -> None:
        """Called after a dataset is staged (for logging or metrics)."""

    def _ensure_mentioned(self, sql: str) -> None:
        for view in set(_VIEW_RE.findall(sql)):
            stage = self._shared.stages.get(view)
            if stage is not None:
                self.ensure_staged(stage)

    def exists(self, relation: Relation) -> bool:
        if self._is_staging_query(relation):
            try:
                self.query(f"SELECT * FROM {relation.base_sql} LIMIT 0")
            except ConnectorError:
                return False
            return True
        stage = self._stage_for(relation)
        known = self.dataset_exists(stage.spec)
        if known is not None:
            return known
        try:
            self.ensure_staged(stage)
        except ConnectorError:
            return False
        return True

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if self._is_staging_query(relation):
            return self._query_schema(relation)
        stage = self._stage_for(relation)
        described = stage.data if stage.path is not None else self.describe(stage.spec)
        if described is None:
            self.ensure_staged(stage)
            described = stage.data
        assert described is not None
        return _schema_from(relation.ref, described)

    def _query_schema(self, relation: Relation) -> DatasetSchema:
        self._ensure_mentioned(relation.base_sql)
        try:
            rows = self.connection.execute(f"DESCRIBE SELECT * FROM {relation.base_sql}").fetchall()
        except duckdb.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        columns = tuple(
            ColumnSchema(
                name=str(r[0]),
                native_type=str(r[1]),
                type=parse_native_type(str(r[1]), "duckdb"),
                ordinal=i + 1,
            )
            for i, r in enumerate(rows)
        )
        return DatasetSchema(ref=relation.ref, columns=columns)

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        return self.get_schema(relation).primary_key

    def staged_rows(self, relation: Relation) -> int:
        """Rows staged for ``relation`` (staging it if needed), before the SQL filter."""
        stage = self._stage_for(relation)
        self.ensure_staged(stage)
        assert stage.path is not None
        return int(pq.ParquetFile(stage.path).metadata.num_rows)

    # ------------------------------------------------------------------ queries
    def query(self, sql: str) -> pa.Table:
        self._ensure_mentioned(sql)
        try:
            return self.connection.execute(sql).to_arrow_table()
        except duckdb.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        self._ensure_mentioned(sql)
        con = self.connection
        con.execute("SET preserve_insertion_order = false")
        try:
            yield from con.execute(sql).to_arrow_reader(batch_size)
        except duckdb.Error as exc:
            raise ConnectorError(f"Query failed on connection '{self.name}': {exc}") from exc
        finally:
            con.execute("SET preserve_insertion_order = true")

    def register_arrow(self, name: str, table: pa.Table) -> bool:
        self.connection.register(name, table)
        return True

    def unregister_arrow(self, name: str) -> None:
        self.connection.unregister(name)


def _staged(stage: _Stage) -> bool:
    return stage.path is not None


def mentions_staged_view(sql: str) -> bool:
    """Whether ``sql`` refers to a staged view (internal SQL over the staging database)."""
    return _VIEW_RE.search(sql) is not None


def write_parquet_stream(
    path: Path, schema: pa.Schema, batches: Iterable[pa.RecordBatch], row_group_rows: int = 50_000
) -> int:
    """Write ``batches`` (renamed and cast to ``schema``) to a Parquet file; return the row count.

    Memory stays bounded by one batch. An empty stream still writes a file with the schema.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with pq.ParquetWriter(path, schema) as writer:
        for batch in batches:
            if batch.num_rows == 0:
                continue
            table = pa.Table.from_batches([batch])
            if table.schema.names != schema.names:
                if set(schema.names) <= set(table.schema.names):
                    table = table.select(schema.names)
                else:
                    table = table.rename_columns(schema.names)
            table = table.cast(schema)
            writer.write_table(table, row_group_size=row_group_rows)
            rows += table.num_rows
    return rows


def _schema_from(ref: DatasetRef, data: StagedData) -> DatasetSchema:
    duck_types = _duckdb_types(data.schema)
    columns = []
    for i, f in enumerate(data.schema):
        duck_type = duck_types.get(f.name, "VARCHAR")
        native = data.native_types.get(f.name, duck_type)
        ctype = data.canonical_types.get(f.name) or parse_native_type(duck_type, "duckdb")
        columns.append(
            ColumnSchema(
                name=f.name,
                native_type=native,
                type=ctype,
                nullable=data.nullable.get(f.name, f.nullable) if data.nullable else None,
                ordinal=i + 1,
                comment=data.comments.get(f.name),
            )
        )
    return DatasetSchema(ref=ref, columns=tuple(columns), primary_key=tuple(data.primary_key))


def _duckdb_types(schema: pa.Schema) -> dict[str, str]:
    """The DuckDB type names DuckDB gives the columns of an Arrow schema."""
    con = duckdb.connect(":memory:")
    try:
        con.register("dvf_empty", schema.empty_table())
        rows = con.execute("DESCRIBE SELECT * FROM dvf_empty").fetchall()
        return {str(r[0]): str(r[1]) for r in rows}
    except duckdb.Error:
        return {}
    finally:
        con.close()


__all__ = ["StagedConnector", "StagedData", "mentions_staged_view", "write_parquet_stream"]
