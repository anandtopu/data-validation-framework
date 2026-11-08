"""Synthetic file and lakehouse fixtures shared by the file pack tests (no tests here).

Iceberg tables are written with pyiceberg through :class:`JsonFileCatalog`, a minimal catalog
that keeps table locations in a JSON file. pyiceberg's own SQL and in-memory catalogs need
SQLAlchemy, which the dev environment does not install. The catalog is also loadable through
``py-catalog-impl`` to test catalog-based datasets.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
from pyiceberg.catalog import MetastoreCatalog
from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC
from pyiceberg.serializers import FromInputFile
from pyiceberg.table import CommitTableResponse, Table
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER

FSSPEC_IO = {"py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO"}
"""pyiceberg's PyArrow file IO cannot open local Windows paths; fsspec's can."""


class JsonFileCatalog(MetastoreCatalog):
    """A test catalog: ``warehouse`` folder, table locations in ``<warehouse>/catalog.json``."""

    def __init__(self, name: str, **properties: str) -> None:
        super().__init__(name, **{**FSSPEC_IO, **properties})
        self.root = Path(properties["warehouse"])
        self.index = self.root / "catalog.json"

    def _tables(self) -> dict[str, str]:
        if not self.index.exists():
            return {}
        data: dict[str, str] = json.loads(self.index.read_text(encoding="utf-8"))
        return data

    def _save(self, identifier: str, location: str) -> None:
        tables = self._tables()
        tables[identifier] = location
        self.root.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps(tables), encoding="utf-8")

    def _key(self, identifier: Any) -> str:
        return ".".join(self.identifier_to_tuple(identifier))

    def create_table(  # type: ignore[override]
        self,
        identifier: Any,
        schema: Any,
        location: str | None = None,
        partition_spec: Any = UNPARTITIONED_PARTITION_SPEC,
        sort_order: Any = UNSORTED_SORT_ORDER,
        properties: Any = None,
    ) -> Table:
        key = self._key(identifier)
        if key in self._tables():
            raise TableAlreadyExistsError(key)
        location = location or (self.root / key.replace(".", "/")).resolve().as_uri()
        staged = self._create_staged_table(
            identifier, schema, location, partition_spec, sort_order, properties or {}
        )
        self._write_metadata(staged.metadata, staged.io, staged.metadata_location)
        self._save(key, staged.metadata_location)
        return self.load_table(identifier)

    def load_table(self, identifier: Any) -> Table:
        key = self._key(identifier)
        location = self._tables().get(key)
        if location is None:
            raise NoSuchTableError(key)
        io = self._load_file_io(location=location)
        metadata = FromInputFile.table_metadata(io.new_input(location))
        return Table(self.identifier_to_tuple(identifier), metadata, location, io, self)

    def commit_table(self, table: Any, requirements: Any, updates: Any) -> CommitTableResponse:
        current = self.load_table(table.name())
        staged = self._update_and_stage_table(current, table.name(), requirements, updates)
        self._write_metadata(staged.metadata, staged.io, staged.metadata_location)
        self._save(self._key(table.name()), staged.metadata_location)
        return CommitTableResponse(
            metadata=staged.metadata, metadata_location=staged.metadata_location
        )

    def table_exists(self, identifier: Any) -> bool:
        return self._key(identifier) in self._tables()

    # Namespace and view management is not needed by the tests.
    def drop_table(self, identifier: Any) -> None:
        raise NotImplementedError

    def rename_table(self, from_identifier: Any, to_identifier: Any) -> Table:
        raise NotImplementedError

    def register_table(self, identifier: Any, metadata_location: str) -> Table:
        raise NotImplementedError

    def create_namespace(self, namespace: Any, properties: Any = None) -> None:
        return None

    def drop_namespace(self, namespace: Any) -> None:
        return None

    def list_namespaces(self, namespace: Any = ()) -> list[Any]:
        return []

    def list_tables(self, namespace: Any) -> list[Any]:
        return []

    def load_namespace_properties(self, namespace: Any) -> dict[str, str]:
        return {}

    def update_namespace_properties(
        self, namespace: Any, removals: Any = None, updates: Any = None
    ) -> Any:
        raise NotImplementedError

    def list_views(self, namespace: Any) -> list[Any]:
        return []

    def drop_view(self, identifier: Any) -> None:
        raise NotImplementedError

    def load_view(self, identifier: Any) -> Any:
        raise NotImplementedError

    def view_exists(self, identifier: Any) -> bool:
        return False

    def register_view(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def orders_table(ids: list[int], *, shift: int = 0) -> pa.Table:
    """Synthetic orders with integer, text, decimal, date, and timestamp columns."""
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "customer": pa.array([f"c{i % 3}" for i in ids], pa.string()),
            "amount": pa.array([Decimal(i * 10 + shift) / 4 for i in ids], pa.decimal128(12, 2)),
            "order_date": pa.array([date(2026, 1, 1 + i % 28) for i in ids], pa.date32()),
            "updated_at": pa.array(
                [datetime(2026, 1, 1, 8, i % 60) for i in ids], pa.timestamp("us")
            ),
        }
    )


def write_iceberg(
    warehouse: Path, identifier: str, batches: list[pa.Table]
) -> tuple[Table, list[int]]:
    """Create an Iceberg table and append each batch as its own snapshot.

    Returns the table and each snapshot's timestamp (ms); appends are spaced so timestamps
    differ.
    """
    catalog = JsonFileCatalog("tests", warehouse=str(warehouse))
    table = catalog.create_table(identifier, schema=batches[0].schema)
    stamps = []
    for batch in batches:
        table.append(batch)
        table = catalog.load_table(identifier)
        snapshot = table.current_snapshot()
        assert snapshot is not None
        stamps.append(int(snapshot.timestamp_ms))
        time.sleep(0.02)
    return table, stamps


def iso_ms(millis: int) -> str:
    return datetime.fromtimestamp(millis / 1000, UTC).isoformat()
