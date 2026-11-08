"""SAP ABAP Dictionary (DDIC) metadata mapped to dvf schemas.

Both SAP connectors describe tables from the dictionary when asked to: ``sap_hana`` reads the
dictionary tables with SQL, ``sap_rfc`` reads them through RFC (``DDIF_FIELDINFO_GET`` or
``RFC_READ_TABLE``). The tables used are:

========= ===============================================================================
DD02L     Tables: class (TRANSP, POOL, CLUSTER, VIEW), client dependence, delivery class
DD02T     Table descriptions
DD03L     Fields: position, key flag, ABAP type, length, decimals, data element, references
DD04L     Data elements: type, length, and decimals, used when DD03L leaves them empty
DD04T     Data element descriptions (field texts)
DD05S     Foreign key field pairs
DD08L     Foreign key relationships (check tables, cardinality)
========= ===============================================================================

Column types are physical: ABAP ``DATS`` is stored as 8 characters (``YYYYMMDD``), ``TIMS`` as 6,
so the canonical type is a string and the ``sap_dats`` and ``sap_tims`` normalizers convert it.
The ABAP type and length appear as the native type (``DATS(8)``, ``CURR(13,2)``), and
:attr:`DdicField.logical_type` gives the dictionary meaning (DATE for DATS).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import pyarrow as pa

from dvf.core.datasets import ColumnSchema, DatasetRef, DatasetSchema
from dvf.core.types import CanonicalType, from_sap_ddic

_PACKED_TYPES = frozenset({"DEC", "CURR", "QUAN"})
_PHYSICAL_TEXT = {"DATS": 8, "TIMS": 6, "ACCP": 6, "DATN": 8, "TIMN": 6}
"""ABAP types read as text of a fixed length. RFC returns all of them as text; on HANA, DATN and
TIMN are real DATE and TIME columns, so the HANA connector takes column types from its catalog."""
CLIENT_COLUMN_NAMES = ("MANDT", "CLIENT", "RCLNT", "MANDANT")
"""Names SAP uses for the client column (DDIC type CLNT), in order of preference."""


@dataclass(frozen=True)
class DdicField:
    """One field of a dictionary table (a DD03L row, completed from DD04L and DD04T)."""

    name: str
    position: int
    datatype: str
    """ABAP dictionary type (DD03L.DATATYPE): CHAR, NUMC, DATS, CURR, INT4, ..."""
    length: int
    """Length in characters, or digits for packed numbers (DD03L.LENG)."""
    decimals: int = 0
    key: bool = False
    not_null: bool = False
    rollname: str = ""
    """Data element."""
    domname: str = ""
    reftable: str = ""
    reffield: str = ""
    """Currency or unit field this amount or quantity refers to (DD03L.REFFIELD)."""
    checktable: str = ""
    description: str | None = None
    outputlen: int | None = None
    """Output length (DFIES.OUTPUTLEN), when known: the width RFC_READ_TABLE gives a field."""
    inttype: str = ""
    """ABAP internal type (C, N, D, T, P, I, F, X, ...), when known."""

    @property
    def native_type(self) -> str:
        if self.datatype in _PACKED_TYPES or self.decimals:
            return f"{self.datatype}({self.length},{self.decimals})"
        return f"{self.datatype}({self.length})" if self.length else self.datatype

    @property
    def logical_type(self) -> CanonicalType:
        """The dictionary meaning of the field (DATE for DATS)."""
        return from_sap_ddic(self.datatype, self.length or None, self.decimals)

    @property
    def canonical_type(self) -> CanonicalType:
        """The type of the stored values, which comparisons use."""
        fixed = _PHYSICAL_TEXT.get(self.datatype)
        if fixed is not None:
            return CanonicalType.string(fixed, fixed=True)
        return self.logical_type

    @property
    def is_client(self) -> bool:
        return self.datatype == "CLNT"

    def arrow_type(self) -> pa.DataType:
        """The Arrow type values of this field are staged as."""
        ctype = self.canonical_type
        code = self.datatype
        if code in _PACKED_TYPES:
            precision = max(1, min(self.length or 38, 38))
            return pa.decimal128(precision, min(self.decimals, precision))
        if code in ("INT1", "INT2"):
            return pa.int16()
        if code == "INT4":
            return pa.int32()
        if code == "INT8":
            return pa.int64()
        if code == "FLTP":
            return pa.float64()
        if ctype.logical.value == "BINARY":
            return pa.binary()
        return pa.string()


@dataclass(frozen=True)
class DdicForeignKey:
    """A foreign key of a dictionary table (DD08L with its DD05S field pairs)."""

    field: str
    """The table's check field."""
    check_table: str
    columns: tuple[tuple[str, str], ...]
    """Pairs of (field of this table, field of the check table)."""
    kind: str = ""
    """DD08L.FRKART: key fields, non-key fields, text table (TEXT), ..."""
    cardinality: str = ""


@dataclass(frozen=True)
class DdicTable:
    """A dictionary table with its fields and foreign keys."""

    name: str
    fields: tuple[DdicField, ...]
    table_class: str = "TRANSP"
    client_dependent: bool | None = None
    """DD02L.CLIDEP; ``None`` when unknown (then a key field of type CLNT decides)."""
    delivery_class: str = ""
    description: str | None = None
    foreign_keys: tuple[DdicForeignKey, ...] = ()

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.key)

    @property
    def client_column(self) -> str | None:
        """The client field (MANDT) of a client-dependent table, else ``None``."""
        if self.client_dependent is False:
            return None
        for f in self.fields:
            if f.key and f.is_client:
                return f.name
        return None

    @property
    def references(self) -> dict[str, str]:
        """Amount and quantity fields and the currency or unit field each refers to."""
        return {
            f.name: f.reffield for f in self.fields if f.datatype in ("CURR", "QUAN") and f.reffield
        }

    def field(self, name: str) -> DdicField | None:
        upper = name.upper()
        return next((f for f in self.fields if f.name.upper() == upper), None)

    def schema(self, ref: DatasetRef, columns: Sequence[str] | None = None) -> DatasetSchema:
        """The dvf schema of this table (or of ``columns`` of it, in that order)."""
        chosen = self.fields if columns is None else [self._require(c) for c in columns]
        return DatasetSchema(
            ref=ref,
            columns=tuple(
                ColumnSchema(
                    name=f.name,
                    native_type=f.native_type,
                    type=f.canonical_type,
                    nullable=not (f.key or f.not_null),
                    ordinal=i + 1,
                    comment=f.description,
                )
                for i, f in enumerate(chosen)
            ),
            primary_key=tuple(k for k in self.primary_key if k in {f.name for f in chosen}),
        )

    def _require(self, name: str) -> DdicField:
        found = self.field(name)
        if found is None:
            raise KeyError(f"table {self.name} has no field {name}")
        return found


# ====================================================================== row mapping


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes | bytearray):
        value = bytes(value).decode("utf-8", errors="replace")
    return str(value).strip()


def _int(value: Any) -> int:
    text = _text(value)
    if not text:
        return 0
    sign = -1 if text.endswith("-") else 1
    digits = text.rstrip("-").lstrip("+")
    return sign * int(digits) if digits.isdigit() else 0


def _flag(value: Any) -> bool:
    return _text(value).upper() == "X"


def _get(row: Mapping[str, Any], name: str) -> Any:
    if name in row:
        return row[name]
    return row.get(name.lower())


def _is_field_row(row: Mapping[str, Any]) -> bool:
    name = _text(_get(row, "FIELDNAME"))
    return bool(name) and not name.startswith(".")


def field_from_row(row: Mapping[str, Any], description: str | None = None) -> DdicField:
    """A field from a DD03L or DFIES (DDIF_FIELDINFO_GET) row."""
    outputlen = _int(_get(row, "OUTPUTLEN"))
    text = description or _text(_get(row, "FIELDTEXT")) or _text(_get(row, "SCRTEXT_L")) or None
    return DdicField(
        name=_text(_get(row, "FIELDNAME")),
        position=_int(_get(row, "POSITION")),
        datatype=_text(_get(row, "DATATYPE")).upper(),
        length=_int(_get(row, "LENG")),
        decimals=_int(_get(row, "DECIMALS")),
        key=_flag(_get(row, "KEYFLAG")),
        not_null=_flag(_get(row, "NOTNULL")),
        rollname=_text(_get(row, "ROLLNAME")),
        domname=_text(_get(row, "DOMNAME")),
        reftable=_text(_get(row, "REFTABLE")),
        reffield=_text(_get(row, "REFFIELD")),
        checktable=_text(_get(row, "CHECKTABLE")),
        description=text,
        outputlen=outputlen or None,
        inttype=_text(_get(row, "INTTYPE")).upper(),
    )


def fields_from_rows(
    rows: Iterable[Mapping[str, Any]],
    texts: Mapping[str, str] | None = None,
    elements: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[DdicField, ...]:
    """Fields from DD03L (or DFIES) rows, in position order.

    Include and append markers (``.INCLUDE``, ``.APPEND``, ``.INCLU--AP``) are skipped: the fields
    they bring in have rows of their own. ``texts`` maps data elements to descriptions (DD04T);
    ``elements`` maps data elements to DD04L rows, used where DD03L has no type.
    """
    out: list[DdicField] = []
    for row in rows:
        if not _is_field_row(row):
            continue
        rollname = _text(_get(row, "ROLLNAME"))
        merged = dict(row)
        element = (elements or {}).get(rollname)
        if element is not None and not _text(_get(row, "DATATYPE")):
            for key in ("DATATYPE", "LENG", "DECIMALS", "DOMNAME"):
                merged[key] = _get(element, key)
        out.append(field_from_row(merged, (texts or {}).get(rollname)))
    out.sort(key=lambda f: f.position)
    return tuple(out)


def foreign_keys_from_rows(
    dd08l: Iterable[Mapping[str, Any]], dd05s: Iterable[Mapping[str, Any]]
) -> tuple[DdicForeignKey, ...]:
    """Foreign keys from DD08L relationships and their DD05S field pairs."""
    pairs: dict[str, list[tuple[int, str, str]]] = {}
    for row in dd05s:
        check_field = _text(_get(row, "FIELDNAME"))
        pairs.setdefault(check_field, []).append(
            (_int(_get(row, "PRIMPOS")), _text(_get(row, "FORKEY")), _text(_get(row, "CHECKFIELD")))
        )
    out: list[DdicForeignKey] = []
    for row in dd08l:
        check_field = _text(_get(row, "FIELDNAME"))
        if not check_field or check_field.startswith("."):
            continue
        columns = tuple(
            (forkey or check_field, checkfield)
            for _, forkey, checkfield in sorted(pairs.get(check_field, []))
        )
        card = _text(_get(row, "CARDLEFT")) + ":" + _text(_get(row, "CARD"))
        out.append(
            DdicForeignKey(
                field=check_field,
                check_table=_text(_get(row, "CHECKTABLE")),
                columns=columns,
                kind=_text(_get(row, "FRKART")),
                cardinality=card if card != ":" else "",
            )
        )
    return tuple(out)


def table_from_rows(
    name: str,
    dd02l: Mapping[str, Any] | None,
    fields: Sequence[DdicField],
    *,
    description: str | None = None,
    foreign_keys: Sequence[DdicForeignKey] = (),
) -> DdicTable:
    clidep: bool | None = None
    table_class = "TRANSP"
    delivery = ""
    if dd02l is not None:
        clidep = _flag(_get(dd02l, "CLIDEP"))
        table_class = _text(_get(dd02l, "TABCLASS")) or table_class
        delivery = _text(_get(dd02l, "CONTFLAG"))
    return DdicTable(
        name=name.upper(),
        fields=tuple(fields),
        table_class=table_class,
        client_dependent=clidep,
        delivery_class=delivery,
        description=description,
        foreign_keys=tuple(foreign_keys),
    )


# ====================================================================== reading the dictionary


class DdicSource(Protocol):
    """Reads rows of a dictionary table: ``SELECT fields FROM table WHERE where``.

    ``where`` uses the syntax shared by Open SQL (RFC_READ_TABLE) and HANA SQL: ``=``,
    ``AND``, ``IN ( ... )``, and single-quoted literals.
    """

    def select(self, table: str, fields: Sequence[str], where: str) -> list[dict[str, Any]]: ...


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_IN_CHUNK = 40
DD03L_FIELDS = (
    "FIELDNAME",
    "POSITION",
    "KEYFLAG",
    "DATATYPE",
    "LENG",
    "DECIMALS",
    "ROLLNAME",
    "DOMNAME",
    "NOTNULL",
    "REFTABLE",
    "REFFIELD",
    "CHECKTABLE",
)


def _in_lists(column: str, values: Sequence[str]) -> list[str]:
    return [
        f"{column} IN ( {' , '.join(quote_literal(v) for v in values[i : i + _IN_CHUNK])} )"
        for i in range(0, len(values), _IN_CHUNK)
    ]


def read_table(
    source: DdicSource, table: str, *, language: str = "E", foreign_keys: bool = True
) -> DdicTable | None:
    """Read a table's definition from the dictionary; ``None`` when DD02L does not list it."""
    name = table.upper()
    active = f"TABNAME = {quote_literal(name)} AND AS4LOCAL = 'A'"
    dd02l = source.select("DD02L", ("TABNAME", "TABCLASS", "CLIDEP", "CONTFLAG"), active)
    if not dd02l:
        return None
    lang = quote_literal(language)
    texts02 = source.select("DD02T", ("DDTEXT",), f"{active} AND DDLANGUAGE = {lang}")
    rows = source.select("DD03L", DD03L_FIELDS, f"{active} AND AS4VERS = '0000'")
    rollnames = sorted({_text(_get(r, "ROLLNAME")) for r in rows} - {""})
    texts: dict[str, str] = {}
    elements: dict[str, dict[str, Any]] = {}
    for condition in _in_lists("ROLLNAME", rollnames):
        for row in source.select(
            "DD04T",
            ("ROLLNAME", "DDTEXT"),
            f"{condition} AND DDLANGUAGE = {lang} AND AS4LOCAL = 'A'",
        ):
            texts[_text(_get(row, "ROLLNAME"))] = _text(_get(row, "DDTEXT"))
        if any(not _text(_get(r, "DATATYPE")) for r in rows if _is_field_row(r)):
            for row in source.select(
                "DD04L",
                ("ROLLNAME", "DOMNAME", "DATATYPE", "LENG", "DECIMALS"),
                f"{condition} AND AS4LOCAL = 'A'",
            ):
                elements[_text(_get(row, "ROLLNAME"))] = row
    fields = fields_from_rows(rows, texts, elements)
    keys: tuple[DdicForeignKey, ...] = ()
    if foreign_keys:
        dd08l = source.select(
            "DD08L", ("FIELDNAME", "CHECKTABLE", "FRKART", "CARD", "CARDLEFT"), active
        )
        dd05s = source.select(
            "DD05S",
            ("FIELDNAME", "FORTABLE", "FORKEY", "CHECKTABLE", "CHECKFIELD", "PRIMPOS"),
            active,
        )
        keys = foreign_keys_from_rows(dd08l, dd05s)
    description = _text(_get(texts02[0], "DDTEXT")) if texts02 else None
    return table_from_rows(
        name, dd02l[0], fields, description=description or None, foreign_keys=keys
    )


@dataclass
class DdicCache:
    """Dictionary definitions read once per connection."""

    tables: dict[str, DdicTable | None] = field(default_factory=dict)

    def get(self, key: str) -> DdicTable | None:
        return self.tables.get(key.upper())

    def has(self, key: str) -> bool:
        return key.upper() in self.tables

    def put(self, key: str, table: DdicTable | None) -> None:
        self.tables[key.upper()] = table


__all__ = [
    "CLIENT_COLUMN_NAMES",
    "DdicCache",
    "DdicField",
    "DdicForeignKey",
    "DdicSource",
    "DdicTable",
    "field_from_row",
    "fields_from_rows",
    "foreign_keys_from_rows",
    "quote_literal",
    "read_table",
    "table_from_rows",
]
