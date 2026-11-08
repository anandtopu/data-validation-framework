"""SAP RFC connector (beta): application-level access to ECC and S/4HANA.

Prefer database access (``sap_hana``) or extracted files for volume: this connector reads rows
through the application server, stages them locally (see :mod:`dvf.connectors.staged`), and
runs every check in an embedded DuckDB.

* ``backend`` selects how dvf talks to SAP (:mod:`dvf.packs.sap.rfc.backends`): ``pyrfc``,
  ``custom_fm``, ``odata``, or a plugin's backend.
* Datasets name a table (or, with ``odata``, an entity set). ``options.fields`` limits the
  columns, ``options.where`` is a condition SAP evaluates (Open SQL, or an OData ``$filter``),
  and ``options.max_rows`` caps the rows. ``filter`` applies to the staged rows, in DuckDB SQL.
* Client-dependent tables are read in the logon ``client``; there is no MANDT filter to write.
* Schemas come from the ABAP dictionary (DDIF_FIELDINFO_GET or DD03L) or the OData metadata:
  ABAP types are the native types, keys the dictionary keys. Values stay as SAP stores them
  (ALPHA keys with leading zeros, DATS as ``YYYYMMDD``): use the SAP normalizers to compare.

It is unit-tested against a fake RFC_READ_TABLE and a local OData server, and has not yet been
verified against a live SAP system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dvf.config.connections import SapRfcConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.staged import StagedConnector, StagedData
from dvf.core.errors import ConnectorError
from dvf.packs.sap.rfc.backends import (
    RemoteTable,
    SapRfcBackend,
    TableRead,
    make_backend,
    read_columns,
    read_schema,
)


class SapRfcConnector(StagedConnector):
    """SAP ECC and S/4HANA through RFC or OData (staged locally)."""

    Config = SapRfcConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._backend: SapRfcBackend | None = None
        self._tables: dict[str, RemoteTable | None] = {}

    @property
    def cfg(self) -> SapRfcConnection:
        assert isinstance(self.config, SapRfcConnection)
        return self.config

    @property
    def backend(self) -> SapRfcBackend:
        if self._backend is None:
            self._backend = make_backend(self.cfg)
        return self._backend

    # ------------------------------------------------------------------ staged connector API
    def open_source(self) -> None:
        self.backend.open()

    def close_source(self) -> None:
        if self._backend is not None:
            self._backend.close()

    def server_version(self) -> str | None:
        self.open()
        return self.backend.server_version()

    def _request(self, spec: DatasetSpec) -> TableRead:
        if spec.query is not None:
            raise ConnectorError(
                f"Connection '{self.name}' (sap_rfc) reads tables, not SQL queries.",
                hint="Name a table and use options.where (evaluated by SAP) or 'filter' (on the "
                "staged rows); for SQL, connect to the database with sap_hana.",
            )
        if spec.path is not None or spec.table is None:
            raise ConnectorError(f"Connection '{self.name}' (sap_rfc) needs a 'table'.")
        if spec.as_of is not None:
            raise ConnectorError(f"Connection '{self.name}' (sap_rfc) does not support 'as_of'.")
        options = spec.options
        fields = options.get("fields")
        if fields is not None and (
            not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)
        ):
            raise ConnectorError("options.fields must be a list of field names.")
        where = options.get("where")
        if where is not None and not isinstance(where, str):
            raise ConnectorError("options.where must be a string.")
        max_rows = options.get("max_rows")
        if max_rows is not None and (not isinstance(max_rows, int) or max_rows < 0):
            raise ConnectorError("options.max_rows must be a non-negative integer.")
        return TableRead(
            table=spec.table,
            fields=tuple(fields) if fields is not None else None,
            where=where,
            max_rows=max_rows,
            options=options,
        )

    def remote_table(self, name: str) -> RemoteTable | None:
        """The backend's description of ``name`` (cached per connection)."""
        key = name.upper()
        if key not in self._tables:
            self.open()
            self._tables[key] = self.backend.describe(name)
        return self._tables[key]

    def _require(self, request: TableRead) -> RemoteTable:
        table = self.remote_table(request.table)
        if table is None:
            raise ConnectorError(
                f"Table '{request.table}' not found on connection '{self.name}'.",
                hint="Check the name (SAP tables are upper case); with backend odata, name an "
                "entity set of the service.",
            )
        return table

    def describe(self, spec: DatasetSpec) -> StagedData | None:
        request = self._request(spec)
        table = self._require(request)
        columns = read_columns(table, request)
        names = {c.name for c in columns}
        return StagedData(
            schema=read_schema(table, request),
            batches=(),
            native_types={c.name: c.native_type for c in columns},
            canonical_types={c.name: c.canonical for c in columns},
            primary_key=tuple(k for k in table.primary_key if k in names),
            nullable={c.name: c.nullable for c in columns},
        )

    def dataset_exists(self, spec: DatasetSpec) -> bool | None:
        return self.remote_table(self._request(spec).table) is not None

    def fetch(self, spec: DatasetSpec) -> StagedData:
        described = self.describe(spec)
        assert described is not None
        request = self._request(spec)
        table = self._require(request)
        batches = self.backend.read(table, request)
        return StagedData(
            schema=described.schema,
            batches=batches,
            native_types=described.native_types,
            canonical_types=described.canonical_types,
            primary_key=described.primary_key,
            nullable=described.nullable,
        )

    def list_datasets(self, pattern: str) -> list[str]:
        self.open()
        return self.backend.list_tables(pattern)

    def call(self, function: str, **parameters: Any) -> dict[str, Any]:
        """Call a remote-enabled function module through the backend (for plugins and checks)."""
        self.open()
        return self.backend.call(function, **parameters)


__all__ = ["SapRfcConnector"]
