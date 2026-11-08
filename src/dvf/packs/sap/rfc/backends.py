"""Pluggable backends of the ``sap_rfc`` connector.

A backend talks to one SAP system: it describes tables and reads them in Arrow record batches,
and function-module backends can also call function modules. The built-in backends are:

============ ====================================================================================
``pyrfc``    RFC through PyRFC and the SAP NW RFC SDK, both installed by the user. PyRFC is no
             longer maintained by SAP (January 2026). Tables are read with RFC_READ_TABLE.
``custom_fm`` PyRFC with a custom read module (``read_function``) that takes RFC_READ_TABLE's
             parameters and returns wider rows (``row_width``), avoiding most field chunking.
``odata``    OData v2 or v4 services and CDS views over HTTP, with the standard library only.
``nwrfcsdk`` A direct binding to the NW RFC SDK. Declared so configurations are validated
             honestly; selecting it raises :class:`NotImplementedError`.
============ ====================================================================================

Plugins add backends with :func:`register_rfc_backend` or the entry point group
``dvf.sap_rfc_backends`` (a :class:`SapRfcBackend` subclass); ``backend: <name>`` selects one.
"""

from __future__ import annotations

import fnmatch
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Any, ClassVar

import pyarrow as pa

from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType
from dvf.packs.sap.ddic import DdicTable, fields_from_rows, quote_literal, read_table
from dvf.packs.sap.drivers import PYRFC_HINT, import_driver
from dvf.packs.sap.rfc.read_table import (
    DEEP_TYPES,
    ReadPlan,
    RfcBufferExceededError,
    RfcCallError,
    arrow_schema,
    cut_rows,
    plan_chunks,
    probe_widths,
    read_rows,
    split_where,
)

if TYPE_CHECKING:
    from dvf.config.connections import SapRfcConnection

ENTRY_POINT_GROUP = "dvf.sap_rfc_backends"
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteColumn:
    """A column of a remote table or entity set."""

    name: str
    native_type: str
    canonical: CanonicalType
    arrow: pa.DataType
    key: bool = False
    nullable: bool = False
    description: str | None = None


@dataclass(frozen=True)
class RemoteTable:
    """A table (or OData entity set) as a backend describes it."""

    name: str
    columns: tuple[RemoteColumn, ...]
    ddic: DdicTable | None = None
    """The dictionary definition, for function-module backends."""

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.key)

    def column(self, name: str) -> RemoteColumn | None:
        upper = name.upper()
        return next((c for c in self.columns if c.name.upper() == upper), None)

    @classmethod
    def from_ddic(cls, ddic: DdicTable) -> RemoteTable:
        return cls(
            name=ddic.name,
            columns=tuple(
                RemoteColumn(
                    name=f.name,
                    native_type=f.native_type,
                    canonical=f.canonical_type,
                    arrow=f.arrow_type(),
                    key=f.key,
                    nullable=False,
                    description=f.description,
                )
                for f in ddic.fields
            ),
            ddic=ddic,
        )


@dataclass(frozen=True)
class TableRead:
    """What to read from a table."""

    table: str
    fields: tuple[str, ...] | None = None
    """Columns to read, in order; all readable columns when ``None``."""
    where: str | None = None
    """A condition evaluated by SAP: Open SQL for RFC backends, ``$filter`` syntax for OData."""
    max_rows: int | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


class SapRfcBackend(ABC):
    """Base class of ``sap_rfc`` backends."""

    name: ClassVar[str] = ""
    implemented: ClassVar[bool] = True
    """False for declared backends without an implementation."""
    summary: ClassVar[str] = ""

    def __init__(self, config: SapRfcConnection) -> None:
        self.config = config

    @classmethod
    def validate_config(cls, config: SapRfcConnection) -> None:
        """Raise ``ValueError`` when ``config`` lacks what this backend needs."""
        return None

    def open(self) -> None:
        """Connect (called once before the first request)."""
        return None

    def close(self) -> None:
        """Disconnect."""
        return None

    def call(self, function: str, **parameters: Any) -> dict[str, Any]:
        """Call a remote-enabled function module and return its exporting and table parameters."""
        raise NotImplementedError(f"The {self.name} backend cannot call function modules.")

    @abstractmethod
    def describe(self, table: str) -> RemoteTable | None:
        """The table's columns and key, or ``None`` when it does not exist."""

    @abstractmethod
    def read(self, table: RemoteTable, request: TableRead) -> Iterator[pa.RecordBatch]:
        """Stream the requested rows as record batches with :func:`read_schema` columns."""

    def list_tables(self, pattern: str) -> list[str]:
        """Names of tables matching a glob pattern."""
        raise ConnectorError(f"The {self.name} backend cannot list tables.")

    def server_version(self) -> str | None:
        return None


def read_columns(table: RemoteTable, request: TableRead) -> list[RemoteColumn]:
    """The columns ``request`` reads, in order (all readable columns by default)."""
    if request.fields is None:
        return [c for c in table.columns if not _is_deep(table, c.name)]
    out = []
    for name in request.fields:
        column = table.column(name)
        if column is None:
            raise ConnectorError(f"Table {table.name} has no field {name}.")
        if _is_deep(table, column.name):
            raise ConnectorError(
                f"Field {column.name} of {table.name} is a string of unlimited length, which "
                "RFC_READ_TABLE cannot read; leave it out of options.fields."
            )
        out.append(column)
    return out


def read_schema(table: RemoteTable, request: TableRead) -> pa.Schema:
    return pa.schema([pa.field(c.name, c.arrow) for c in read_columns(table, request)])


def _is_deep(table: RemoteTable, name: str) -> bool:
    if table.ddic is None:
        return False
    f = table.ddic.field(name)
    return f is not None and f.datatype in DEEP_TYPES


# ====================================================================== function-module backends


class _ReadTableDdicSource:
    """Reads dictionary tables with the read function (see :class:`DdicSource`)."""

    def __init__(self, backend: FunctionModuleBackend) -> None:
        self.backend = backend

    def select(self, table: str, fields: Sequence[str], where: str) -> list[dict[str, Any]]:
        from dvf.packs.sap.ddic import DdicField

        specs = {
            name: DdicField(name=name, position=i, datatype="CHAR", length=0)
            for i, name in enumerate(fields)
        }
        result = self.backend.call(
            self.backend.config.read_function,
            QUERY_TABLE=table,
            DELIMITER="",
            OPTIONS=[{"TEXT": line} for line in split_where(where)],
            FIELDS=[{"FIELDNAME": n} for n in fields],
        )
        return list(cut_rows(result, specs))


class FunctionModuleBackend(SapRfcBackend):
    """A backend that calls function modules; reads tables with the RFC_READ_TABLE interface."""

    @abstractmethod
    def call(self, function: str, **parameters: Any) -> dict[str, Any]: ...

    def describe(self, table: str) -> RemoteTable | None:
        name = table.upper()
        if self.config.ddic_source == "dd03l" or self.config.foreign_keys:
            ddic = read_table(
                _ReadTableDdicSource(self),
                name,
                language=self._language_key(),
                foreign_keys=self.config.foreign_keys,
            )
            if ddic is None:
                return None
            if self.config.ddic_source == "dd03l":
                return RemoteTable.from_ddic(ddic)
            fieldinfo = self._fieldinfo(name)
            if fieldinfo is None:
                return None
            return RemoteTable.from_ddic(
                DdicTable(
                    name=name,
                    fields=fieldinfo.fields,
                    table_class=ddic.table_class,
                    client_dependent=ddic.client_dependent,
                    delivery_class=ddic.delivery_class,
                    description=ddic.description,
                    foreign_keys=ddic.foreign_keys,
                )
            )
        fieldinfo = self._fieldinfo(name)
        return None if fieldinfo is None else RemoteTable.from_ddic(fieldinfo)

    def _language_key(self) -> str:
        lang = self.config.lang.strip()
        if len(lang) == 1:
            return lang
        from dvf.packs.sap.tables import T002_DEFAULTS

        inverse = {code: key for key, code in T002_DEFAULTS.items()}
        return inverse.get(lang.upper(), "E")

    def _fieldinfo(self, name: str) -> DdicTable | None:
        try:
            result = self.call(
                "DDIF_FIELDINFO_GET", TABNAME=name, LANGU=self._language_key(), ALL_TYPES="X"
            )
        except RfcCallError as exc:
            if exc.key in ("NOT_FOUND", "INTERNAL_ERROR"):
                return None
            raise
        rows = result.get("DFIES_TAB") or []
        if not rows:
            return None
        fields = fields_from_rows(rows)
        return DdicTable(
            name=name, fields=fields, table_class=str(result.get("DDOBJTYPE", "") or "TRANSP")
        )

    def read(self, table: RemoteTable, request: TableRead) -> Iterator[pa.RecordBatch]:
        ddic = table.ddic
        if ddic is None:
            raise ConnectorError(f"Table {table.name} has no dictionary definition.")
        columns = read_columns(table, request)
        fields = [ddic.field(c.name) for c in columns]
        chosen = tuple(f for f in fields if f is not None)
        cfg = self.config
        widths = probe_widths(self.call, table.name, chosen, cfg.read_function, cfg.delimiter)
        chunks = plan_chunks(chosen, ddic.primary_key, widths, cfg.row_width, cfg.delimiter)
        plan = ReadPlan(
            table=table.name,
            fields=chosen,
            keys=tuple(k for k in ddic.primary_key if any(f.name == k for f in chosen)),
            chunks=tuple(chunks),
            where_lines=tuple(split_where(request.where)),
            page_size=cfg.page_size,
            function=cfg.read_function,
            delimiter=cfg.delimiter,
            max_rows=request.max_rows,
        )
        yield from read_rows(self.call, plan, arrow_schema(chosen))

    def list_tables(self, pattern: str) -> list[str]:
        like = pattern.upper().replace("*", "%").replace("?", "_")
        rows = _ReadTableDdicSource(self).select(
            "DD02L",
            ("TABNAME",),
            f"TABNAME LIKE {quote_literal(like)} AND AS4LOCAL = 'A' AND TABCLASS = 'TRANSP'",
        )
        names = sorted({str(r["TABNAME"]).strip() for r in rows})
        return [n for n in names if fnmatch.fnmatchcase(n, pattern.upper())]

    def server_version(self) -> str | None:
        try:
            info = self.call("RFC_SYSTEM_INFO").get("RFCSI_EXPORT") or {}
        except (RfcCallError, NotImplementedError):
            return None
        release = str(info.get("RFCSAPRL", "")).strip()
        system = str(info.get("RFCSYSID", "")).strip()
        return f"SAP {system} release {release}".strip() if release or system else None


_ERROR_KEYS = ("key", "KEY")


def _error_key(exc: BaseException) -> str | None:
    for attr in _ERROR_KEYS:
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


class PyRfcBackend(FunctionModuleBackend):
    """RFC through PyRFC (user-installed; needs the SAP NW RFC SDK)."""

    name = "pyrfc"
    summary = "RFC through PyRFC and the NW RFC SDK (user-installed; PyRFC is unmaintained)"

    def __init__(self, config: SapRfcConnection) -> None:
        super().__init__(config)
        self._con: Any = None

    @classmethod
    def validate_config(cls, config: SapRfcConnection) -> None:
        if config.ashost is None and config.mshost is None:
            raise ValueError(f"set either 'ashost' or 'mshost' (backend '{config.backend}')")

    def logon_parameters(self) -> dict[str, Any]:
        cfg = self.config
        params: dict[str, Any] = {"client": cfg.client, "lang": cfg.lang}
        for key in ("ashost", "sysnr", "mshost", "msserv", "sysid", "group", "saprouter", "user"):
            value = getattr(cfg, key)
            if value is not None:
                params[key] = value
        if cfg.password is not None:
            params["passwd"] = cfg.password.get_secret_value()
        params.update(cfg.options)
        return params

    def open(self) -> None:
        if self._con is not None:
            return
        pyrfc = import_driver("pyrfc", "pyrfc", "The sap_rfc connector (pyrfc backend)", PYRFC_HINT)
        try:
            self._con = pyrfc.Connection(**self.logon_parameters())
        except Exception as exc:
            raise ConnectorError(
                f"Could not log on to SAP: {type(exc).__name__}: {exc}",
                hint="Check ashost/mshost, sysnr, client, and credentials; the user needs RFC "
                "authorization for RFC_READ_TABLE and DDIF_FIELDINFO_GET (S_RFC, S_TABU_DIS).",
            ) from exc

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None

    def call(self, function: str, **parameters: Any) -> dict[str, Any]:
        self.open()
        try:
            return dict(self._con.call(function, **parameters))
        except Exception as exc:
            key = _error_key(exc)
            if key == "DATA_BUFFER_EXCEEDED":
                raise RfcBufferExceededError(f"{function}: DATA_BUFFER_EXCEEDED", key) from exc
            if key is not None:
                raise RfcCallError(f"{function} raised {key}: {exc}", key) from exc
            raise ConnectorError(
                f"RFC call {function} failed: {type(exc).__name__}: {exc}"
            ) from exc


class CustomFmBackend(PyRfcBackend):
    """PyRFC with a custom read function module that returns wider rows."""

    name = "custom_fm"
    summary = "PyRFC with your own read function module (RFC_READ_TABLE interface, wider rows)"

    @classmethod
    def validate_config(cls, config: SapRfcConnection) -> None:
        super().validate_config(config)
        if config.read_function.upper() == "RFC_READ_TABLE":
            raise ValueError(
                "backend 'custom_fm' needs 'read_function' set to your module (for example "
                "Z_RFC_READ_TABLE) and 'row_width' set to its row width"
            )


class NwRfcSdkBackend(SapRfcBackend):
    """A direct binding to the SAP NW RFC SDK: declared, not implemented."""

    name = "nwrfcsdk"
    implemented = False
    summary = "Direct NW RFC SDK binding (declared, not implemented)"

    def _unavailable(self) -> NotImplementedError:
        return NotImplementedError(
            "The 'nwrfcsdk' backend of sap_rfc is declared but not implemented in this version of "
            "dvf. Use backend 'pyrfc' (which wraps the same SDK), 'odata', or HANA database access "
            "(connection type sap_hana), or add a backend through a plugin."
        )

    def open(self) -> None:
        raise self._unavailable()

    def describe(self, table: str) -> RemoteTable | None:
        raise self._unavailable()

    def read(self, table: RemoteTable, request: TableRead) -> Iterator[pa.RecordBatch]:
        raise self._unavailable()


# ====================================================================== registry

_BACKENDS: dict[str, type[SapRfcBackend]] = {}
_ENTRY_POINTS_LOADED = False


def register_rfc_backend(name: str, backend: type[SapRfcBackend]) -> None:
    """Make ``backend`` selectable as ``backend: <name>`` in ``sap_rfc`` connections."""
    if not (isinstance(backend, type) and issubclass(backend, SapRfcBackend)):
        raise TypeError(f"an RFC backend must subclass SapRfcBackend, got {backend!r}")
    _BACKENDS[name] = backend


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry.name in _BACKENDS:
            continue
        try:
            register_rfc_backend(entry.name, entry.load())
        except Exception as exc:  # a broken plugin must not break built-in backends
            _log.warning("Could not load SAP RFC backend %r: %s", entry.name, exc)


def backend_class(name: str) -> type[SapRfcBackend]:
    """The backend registered as ``name``."""
    _ensure_builtins()
    if name not in _BACKENDS:
        _load_entry_points()
    found = _BACKENDS.get(name)
    if found is None:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown RFC backend '{name}'; available: {known}")
    return found


def backend_names() -> dict[str, str]:
    """Registered backends and their status (``ready`` or ``declared``)."""
    _ensure_builtins()
    _load_entry_points()
    return {
        name: ("ready" if cls.implemented else "declared")
        for name, cls in sorted(_BACKENDS.items())
    }


def validate_backend_config(config: SapRfcConnection) -> None:
    """Validate a ``sap_rfc`` connection against its backend (used by the config model)."""
    backend_class(config.backend).validate_config(config)


def make_backend(config: SapRfcConnection) -> SapRfcBackend:
    try:
        cls = backend_class(config.backend)
    except ValueError as exc:
        raise ConnectorError(str(exc)) from exc
    return cls(config)


def _ensure_builtins() -> None:
    if "pyrfc" in _BACKENDS:
        return
    from dvf.packs.sap.rfc.odata import ODataBackend

    for backend in (PyRfcBackend, CustomFmBackend, NwRfcSdkBackend, ODataBackend):
        _BACKENDS.setdefault(backend.name, backend)


__all__ = [
    "ENTRY_POINT_GROUP",
    "CustomFmBackend",
    "FunctionModuleBackend",
    "NwRfcSdkBackend",
    "PyRfcBackend",
    "RemoteColumn",
    "RemoteTable",
    "SapRfcBackend",
    "TableRead",
    "backend_class",
    "backend_names",
    "make_backend",
    "read_columns",
    "read_schema",
    "register_rfc_backend",
    "validate_backend_config",
]
