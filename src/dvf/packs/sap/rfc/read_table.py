"""Reading SAP tables through RFC_READ_TABLE (or a custom module with the same interface).

RFC_READ_TABLE returns each row as one character field of 512 characters (``WA``); a request
whose fields do not fit raises ``DATA_BUFFER_EXCEEDED``. This module works within that limit:

* **Field chunking.** A first call with ``NO_DATA = 'X'`` returns every field's width without
  reading rows (the buffer check is skipped then). Fields are packed into chunks that fit
  ``row_width``; every chunk repeats the key fields, and chunks are joined on the key. A chunk
  the function still rejects is split in two and retried.
* **Paging.** ``ROWSKIPS`` and ``ROWCOUNT`` read ``page_size`` rows per call. RFC_READ_TABLE has
  no ``ORDER BY``, so each page's chunks must return the same keys and no key may repeat across
  pages; otherwise the read fails instead of returning wrong data.
* **Conditions** (``options.where``, Open SQL) are split into ``OPTIONS`` lines of at most 72
  characters, never inside a literal.
* **Values** are cut by the offsets and lengths the function returns (so a ``DELIMITER`` inside a
  value cannot shift columns), then converted by DDIC type: packed numbers with a trailing minus
  sign (``123.45-``), integers, floats, raw (hexadecimal) fields; character fields lose their
  trailing blanks; DATS and TIMS stay text for the ``sap_dats`` and ``sap_tims`` normalizers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import pyarrow as pa

from dvf.core.errors import ConnectorError
from dvf.packs.sap.ddic import DdicField

WHERE_LINE_WIDTH = 72
RFC_READ_TABLE_WIDTH = 512
DEEP_TYPES = frozenset({"STRG", "RSTR", "SSTR", "STRING", "RAWSTRING", "SSTRING"})
"""Dictionary types RFC_READ_TABLE cannot return (strings of unlimited length)."""

_PACKED = frozenset({"DEC", "CURR", "QUAN"})
_INTEGERS = frozenset({"INT1", "INT2", "INT4", "INT8"})

Call = Callable[..., Mapping[str, Any]]
"""Calls a function module: ``call(name, **parameters)`` returns the exporting and table
parameters by name."""


class RfcCallError(ConnectorError):
    """A function module raised an ABAP exception (``key`` names it)."""

    def __init__(self, message: str, key: str | None = None, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.key = key


class RfcBufferExceededError(RfcCallError):
    """The requested fields do not fit the read function's row buffer (DATA_BUFFER_EXCEEDED)."""


@dataclass(frozen=True)
class ReadPlan:
    """How one table is read: the fields, the chunks, and the call parameters."""

    table: str
    fields: tuple[DdicField, ...]
    keys: tuple[str, ...]
    chunks: tuple[tuple[str, ...], ...]
    where_lines: tuple[str, ...] = ()
    page_size: int | None = 50_000
    function: str = "RFC_READ_TABLE"
    delimiter: str = ""
    max_rows: int | None = None


# ====================================================================== conditions


def _tokens(where: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    i = 0
    while i < len(where):
        ch = where[i]
        if ch == "'":
            current.append(ch)
            if quoted and i + 1 < len(where) and where[i + 1] == "'":
                current.append("'")
                i += 2
                continue
            quoted = not quoted
        elif ch.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1
    if quoted:
        raise ConnectorError(f"Unbalanced quote in the SAP condition: {where}")
    if current:
        tokens.append("".join(current))
    return tokens


def split_where(where: str | None, width: int = WHERE_LINE_WIDTH) -> list[str]:
    """Split an Open SQL condition into ``OPTIONS`` lines of at most ``width`` characters.

    Lines break between tokens, never inside a quoted literal; a single token longer than
    ``width`` cannot be sent and raises :class:`ConnectorError`.
    """
    if not where or not where.strip():
        return []
    lines: list[str] = []
    line = ""
    for token in _tokens(where):
        if len(token) > width:
            raise ConnectorError(
                f"A token of the SAP condition is longer than {width} characters: {token[:40]}...",
                hint="RFC_READ_TABLE takes conditions in lines of 72 characters; shorten the "
                "literal or filter the staged rows with 'filter' instead.",
            )
        candidate = f"{line} {token}" if line else token
        if len(candidate) > width:
            lines.append(line)
            line = token
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


# ====================================================================== planning


def estimated_width(f: DdicField) -> int:
    """A conservative width of ``f`` in an RFC_READ_TABLE row, when the function cannot say."""
    if f.datatype in _PACKED or f.inttype == "P":
        return max(f.outputlen or 0, f.length + f.length // 3 + 2)
    if f.datatype in _INTEGERS:
        return max(f.outputlen or 0, 11 if f.datatype != "INT8" else 20)
    if f.datatype == "FLTP":
        return 24
    if f.datatype == "RAW":
        return 2 * f.length
    return max(f.length, f.outputlen or 0, 1)


def plan_chunks(
    fields: Sequence[DdicField],
    keys: Sequence[str],
    widths: Mapping[str, int],
    row_width: int,
    delimiter: str = "",
) -> list[tuple[str, ...]]:
    """Pack fields into chunks that fit ``row_width``; each chunk starts with the key fields."""

    def width(names: Sequence[str]) -> int:
        return sum(widths[n] for n in names) + len(delimiter) * max(0, len(names) - 1)

    names = [f.name for f in fields]
    if width(names) <= row_width:
        return [tuple(names)]
    key_list = [k for k in keys if k in names]
    if not key_list:
        raise ConnectorError(
            "The table's fields do not fit one RFC_READ_TABLE row, and it has no key fields to "
            "join chunks on.",
            hint="Read fewer fields (options.fields), use a custom read module with a wider row "
            "(backend: custom_fm), the odata backend, or HANA database access.",
        )
    if width(key_list) > row_width:
        raise ConnectorError(
            f"The key fields alone ({', '.join(key_list)}) do not fit a row of {row_width} "
            "characters.",
            hint="Use a custom read module with a wider row (backend: custom_fm, row_width).",
        )
    chunks: list[tuple[str, ...]] = []
    current = list(key_list)
    for name in names:
        if name in key_list:
            continue
        if width([*current, name]) > row_width and len(current) > len(key_list):
            chunks.append(tuple(current))
            current = list(key_list)
        if width([*current, name]) > row_width:
            raise ConnectorError(
                f"Field {name} ({widths[name]} characters) does not fit a row of {row_width} "
                "characters next to the key fields.",
                hint="Leave it out (options.fields) or use a custom read module "
                "(backend: custom_fm, row_width).",
            )
        current.append(name)
    if len(current) > len(key_list) or not chunks:
        chunks.append(tuple(current))
    return chunks


# ====================================================================== values


def _signed(text: str) -> str:
    """ABAP writes negative numbers with a trailing minus sign (``123.45-``)."""
    if text.endswith("-"):
        return "-" + text[:-1].strip()
    return text


def parse_value(text: str, f: DdicField) -> Any:
    """Convert one field cut from a row to a Python value, by DDIC type."""
    code = f.datatype
    if code in _PACKED or (f.inttype == "P" and code not in _INTEGERS):
        stripped = text.strip()
        if not stripped:
            return None
        try:
            return Decimal(_signed(stripped).replace(",", ""))
        except InvalidOperation as exc:
            raise ConnectorError(f"Field {f.name}: cannot read {stripped!r} as a number") from exc
    if code in _INTEGERS:
        stripped = text.strip()
        return int(_signed(stripped)) if stripped else None
    if code == "FLTP":
        stripped = text.strip()
        return float(_signed(stripped).replace(",", ".")) if stripped else None
    if code == "RAW":
        stripped = text.strip()
        return bytes.fromhex(stripped) if stripped else b""
    return text.rstrip(" ")


def _offsets(fields_table: Sequence[Mapping[str, Any]]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for row in fields_table:
        name = str(row.get("FIELDNAME", "")).strip()
        out[name] = (
            int(str(row.get("OFFSET", 0)).strip() or 0),
            int(str(row.get("LENGTH", 0)).strip() or 0),
        )
    return out


def widths_from_metadata(fields_table: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Field widths from the ``FIELDS`` table of a read call."""
    return {name: length for name, (_, length) in _offsets(fields_table).items()}


def cut_rows(
    result: Mapping[str, Any], fields: Mapping[str, DdicField]
) -> Iterator[dict[str, Any]]:
    """The rows of one read call as dictionaries of converted values."""
    offsets = _offsets(result.get("FIELDS") or [])
    missing = [n for n in fields if n not in offsets]
    if missing:
        raise ConnectorError(f"The read function did not return the fields {', '.join(missing)}")
    for data in result.get("DATA") or []:
        wa = str(data.get("WA", "")) if isinstance(data, Mapping) else str(data)
        row: dict[str, Any] = {}
        for name, f in fields.items():
            offset, length = offsets[name]
            row[name] = parse_value(wa[offset : offset + length].ljust(length), f)
        yield row


# ====================================================================== reading


def _call(call: Call, plan: ReadPlan, names: Sequence[str], **extra: Any) -> Mapping[str, Any]:
    return call(
        plan.function,
        QUERY_TABLE=plan.table,
        DELIMITER=plan.delimiter,
        OPTIONS=[{"TEXT": line} for line in plan.where_lines],
        FIELDS=[{"FIELDNAME": n} for n in names],
        **extra,
    )


def probe_widths(
    call: Call, plan_table: str, fields: Sequence[DdicField], function: str, delimiter: str
) -> dict[str, int]:
    """Field widths from a ``NO_DATA`` call, or estimates when the function rejects it."""
    try:
        result = call(
            function,
            QUERY_TABLE=plan_table,
            DELIMITER=delimiter,
            NO_DATA="X",
            OPTIONS=[],
            FIELDS=[{"FIELDNAME": f.name} for f in fields],
        )
        widths = widths_from_metadata(result.get("FIELDS") or [])
    except RfcBufferExceededError:
        widths = {}
    return {f.name: widths.get(f.name) or estimated_width(f) for f in fields}


def _read_chunk(
    call: Call,
    plan: ReadPlan,
    names: tuple[str, ...],
    fields: Mapping[str, DdicField],
    **paging: Any,
) -> list[list[dict[str, Any]]]:
    """Rows of one chunk; a chunk the function rejects is split in two (keys repeated)."""
    try:
        result = _call(call, plan, names, **paging)
        return [list(cut_rows(result, {n: fields[n] for n in names}))]
    except RfcBufferExceededError:
        keys = [n for n in names if n in plan.keys]
        rest = [n for n in names if n not in plan.keys]
        if len(rest) < 2 or not keys:
            raise RfcBufferExceededError(
                f"Fields {', '.join(names)} of {plan.table} do not fit the row buffer of "
                f"{plan.function}.",
                "DATA_BUFFER_EXCEEDED",
                hint="Read fewer fields (options.fields), use a custom read module "
                "(backend: custom_fm), the odata backend, or HANA database access.",
            ) from None
        half = len(rest) // 2
        left = (*keys, *rest[:half])
        right = (*keys, *rest[half:])
        return _read_chunk(call, plan, left, fields, **paging) + _read_chunk(
            call, plan, right, fields, **paging
        )


def read_rows(call: Call, plan: ReadPlan, schema: pa.Schema) -> Iterator[pa.RecordBatch]:
    """Read the table described by ``plan``, one record batch per page."""
    fields = {f.name: f for f in plan.fields}
    names = [f.name for f in plan.fields]
    seen: set[tuple[Any, ...]] = set()
    multi = len(plan.chunks) > 1
    skip = 0
    total = 0
    while True:
        count = plan.page_size or 0
        if plan.max_rows is not None:
            left = plan.max_rows - total
            if left <= 0:
                return
            count = min(count, left) if count else left
        paging = {"ROWSKIPS": skip, "ROWCOUNT": count}
        parts: list[list[dict[str, Any]]] = []
        for chunk in plan.chunks:
            parts.extend(_read_chunk(call, plan, chunk, fields, **paging))
        rows = _join(plan, parts) if len(parts) > 1 else parts[0]
        if multi or plan.keys:
            _check_unique(plan, rows, seen)
        if rows:
            columns = {n: [r.get(n) for r in rows] for n in names}
            yield pa.RecordBatch.from_pydict(
                {n: pa.array(columns[n], schema.field(n).type) for n in names}, schema=schema
            )
        first = len(parts[0])
        total += first
        if not count or first < count:
            return
        skip += count


def _key_of(plan: ReadPlan, row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(k) for k in plan.keys)


def _join(plan: ReadPlan, parts: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    first = parts[0]
    order = [_key_of(plan, r) for r in first]
    merged = {k: dict(r) for k, r in zip(order, first, strict=True)}
    for part in parts[1:]:
        keys = {_key_of(plan, r): r for r in part}
        if set(keys) != set(merged) or len(keys) != len(part):
            raise ConnectorError(
                f"{plan.function} returned different rows for different field chunks of "
                f"{plan.table} (it has no ORDER BY, and the table changed or the database "
                "returned rows in another order).",
                hint="Read fewer fields so one call fits (options.fields), use a custom read "
                "module with a wider row (backend: custom_fm), or HANA database access.",
            )
        for key, row in keys.items():
            merged[key].update(row)
    return [merged[k] for k in order]


def _check_unique(plan: ReadPlan, rows: list[dict[str, Any]], seen: set[tuple[Any, ...]]) -> None:
    if not plan.keys:
        return
    for row in rows:
        key = _key_of(plan, row)
        if key in seen:
            raise ConnectorError(
                f"{plan.function} returned key {key} of {plan.table} twice while paging (it has "
                "no ORDER BY, so pages can overlap when rows are not returned in the same order).",
                hint="Raise page_size so the table is read in one call, filter it into smaller "
                "reads (options.where), or use the odata backend or HANA database access.",
            )
        seen.add(key)


def arrow_schema(fields: Sequence[DdicField]) -> pa.Schema:
    return pa.schema([pa.field(f.name, f.arrow_type()) for f in fields])


__all__ = [
    "DEEP_TYPES",
    "RFC_READ_TABLE_WIDTH",
    "WHERE_LINE_WIDTH",
    "ReadPlan",
    "RfcBufferExceededError",
    "RfcCallError",
    "arrow_schema",
    "cut_rows",
    "estimated_width",
    "parse_value",
    "plan_chunks",
    "probe_widths",
    "read_rows",
    "split_where",
]
