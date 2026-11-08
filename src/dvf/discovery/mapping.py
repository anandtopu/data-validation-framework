"""``dvf import-mapping``: turn a source-to-target (S2T) mapping sheet into a configuration.

A mapping sheet has one row per target column. Headers are matched case-insensitively and
ignoring spaces and punctuation, against the aliases in :data:`HEADER_ALIASES`. Every problem is
reported with its file, sheet, and row, and all problems are reported at once.
"""

from __future__ import annotations

import csv
import importlib
import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from dvf.config.base import NormalizerInput
from dvf.config.diagnostics import Code, Diagnostic, SourceLocation
from dvf.config.loader import LoadedConfig, LoadOptions, load_config_data
from dvf.core.enums import DiagnosticLevel
from dvf.core.errors import ConfigError, DependencyError, DvfError
from dvf.discovery.common import UniqueNames, bare_name, file_stem, generated_header, write_output
from dvf.discovery.connections import ConnectionSpec, spec_from_config, spec_from_location
from dvf.discovery.yamlwriter import Document, Flow, Map, Seq, dump, plain
from dvf.plugins.registry import PluginKind, Registry, get_registry

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "source_table": (
        "source table",
        "source table name",
        "src table",
        "source object",
        "source entity",
        "source dataset",
        "from table",
    ),
    "source_column": (
        "source column",
        "source column name",
        "src column",
        "source field",
        "source attribute",
        "from column",
    ),
    "source_expr": (
        "source expr",
        "source expression",
        "source sql",
        "sql expression",
        "expression",
        "derivation sql",
    ),
    "target_table": (
        "target table",
        "target table name",
        "tgt table",
        "target object",
        "target entity",
        "target dataset",
        "destination table",
        "to table",
    ),
    "target_column": (
        "target column",
        "target column name",
        "tgt column",
        "target field",
        "target attribute",
        "destination column",
        "to column",
    ),
    "key": ("key", "key flag", "is key", "pk", "primary key", "business key", "key column"),
    "transformation": (
        "transformation",
        "transformation rule",
        "transformation logic",
        "business rule",
        "mapping rule",
        "logic",
    ),
    "normalizers": ("normalizers", "normalizer", "normalize", "normalization"),
    "compare": ("compare", "compare flag", "in scope", "include", "validate"),
    "pii": ("pii", "pii flag", "personal data", "sensitive", "gdpr"),
    "notes": ("notes", "note", "comment", "comments", "remarks", "description"),
    "checks": (
        "checks",
        "check",
        "rules",
        "rule",
        "dq rule",
        "dq rules",
        "dq checks",
        "validation rules",
        "validations",
    ),
    "suite": ("suite", "subject area", "module", "domain"),
    "source_filter": ("source filter", "source where", "source scope"),
    "target_filter": ("target filter", "target where", "target scope"),
}
"""Recognized columns of a mapping sheet and the header texts accepted for each."""

REQUIRED_HEADERS = ("source_table", "target_table", "target_column")
CHECK_RULES = (
    "not_null",
    "not_blank",
    "unique",
    "regex",
    "allowed_values",
    "range",
    "length",
    "max_length",
    "null_ratio",
)
"""Rule names accepted in the checks column."""

_TRUE = {"y", "yes", "true", "1", "x", "✓", "✔"}
_FALSE = {"n", "no", "false", "0", "", "-"}
_NO_TRANSFORMATION = {"", "-", "n/a", "na", "none", "direct", "direct move", "1:1", "as is", "same"}
_NORMALIZER_ITEM = re.compile(r"\s*([a-z][a-z0-9_.]*)\s*(?:\(([^()]*)\))?\s*$")
_HEADER_SCAN_ROWS = 20


def _normalize_header(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


_ALIAS_INDEX: dict[str, str] = {
    _normalize_header(alias): name
    for name, aliases in HEADER_ALIASES.items()
    for alias in (name, *aliases)
}


@dataclass
class SheetRow:
    """One data row of the mapping sheet, by recognized field name."""

    number: int
    values: dict[str, str]

    def get(self, name: str) -> str:
        return self.values.get(name, "").strip()


@dataclass
class Sheet:
    """The recognized rows of a mapping sheet."""

    file: Path
    sheet: str | None
    headers: dict[str, int]
    rows: list[SheetRow]
    ignored_headers: list[str] = field(default_factory=list)

    def where(self, row: int | None = None) -> str:
        place = self.file.as_posix() + (f", sheet '{self.sheet}'" if self.sheet else "")
        return place + (f", row {row}" if row is not None else "")


# ---------------------------------------------------------------------- reading


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _find_header(rows: Sequence[tuple[int, list[str]]]) -> tuple[int, dict[str, int]] | None:
    for index, (_, cells) in enumerate(rows[:_HEADER_SCAN_ROWS]):
        mapping: dict[str, int] = {}
        for position, cell in enumerate(cells):
            name = _ALIAS_INDEX.get(_normalize_header(cell))
            if name is not None and name not in mapping:
                mapping[name] = position
        if all(h in mapping for h in REQUIRED_HEADERS):
            return index, mapping
    return None


def _sheet_from_rows(
    file: Path, sheet: str | None, rows: list[tuple[int, list[str]]]
) -> Sheet | None:
    found = _find_header(rows)
    if found is None:
        return None
    index, headers = found
    header_cells = rows[index][1]
    ignored = [
        c for i, c in enumerate(header_cells) if c.strip() and i not in set(headers.values())
    ]
    data: list[SheetRow] = []
    for number, cells in rows[index + 1 :]:
        values = {name: (cells[pos] if pos < len(cells) else "") for name, pos in headers.items()}
        if not any(v.strip() for v in values.values()):
            continue
        data.append(SheetRow(number, values))
    return Sheet(file, sheet, headers, data, ignored)


def _missing_headers_error(file: Path, sheet: str | None) -> ConfigError:
    where = str(file) + (f", sheet '{sheet}'" if sheet else "")
    wanted = ", ".join(REQUIRED_HEADERS)
    return ConfigError(
        f"{where}: no header row with the columns {wanted} was found in the first "
        f"{_HEADER_SCAN_ROWS} rows.",
        hint="Name the columns as in docs/discovery.md (aliases such as 'Source Table' and "
        "'Target Column' are accepted).",
    )


def read_csv(path: Path) -> Sheet:
    """Read a CSV mapping sheet (comma, semicolon, tab, or pipe separated; UTF-8)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max(",;	|", key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[tuple[int, list[str]]] = []
    for cells in reader:
        rows.append((reader.line_num, cells))
    sheet = _sheet_from_rows(path, None, rows)
    if sheet is None:
        raise _missing_headers_error(path, None)
    return sheet


def read_xlsx(path: Path, sheet_name: str | None = None) -> Sheet:
    """Read an Excel mapping sheet: the named sheet, or the first sheet with a header row."""
    try:
        openpyxl: Any = importlib.import_module("openpyxl")
    except ImportError as exc:  # pragma: no cover - dev dependency
        raise DependencyError("openpyxl", "excel", "Reading Excel mapping sheets") from exc
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several exception types for bad files
        raise ConfigError(f"Cannot read the workbook {path}: {exc}") from exc
    try:
        names = list(workbook.sheetnames)
        if sheet_name is not None:
            chosen = next((n for n in names if n.lower() == sheet_name.lower()), None)
            if chosen is None:
                raise ConfigError(
                    f"{path}: sheet '{sheet_name}' not found.",
                    hint=f"Sheets: {', '.join(names)}.",
                )
            candidates = [chosen]
        else:
            candidates = names
        for name in candidates:
            worksheet = workbook[name]
            rows = [
                (i, [_cell(v) for v in values])
                for i, values in enumerate(worksheet.iter_rows(values_only=True), start=1)
            ]
            sheet = _sheet_from_rows(path, name, rows)
            if sheet is not None:
                return sheet
    finally:
        workbook.close()
    if sheet_name is not None:
        raise _missing_headers_error(path, sheet_name)
    raise ConfigError(
        f"{path}: no sheet has a header row with the columns {', '.join(REQUIRED_HEADERS)}.",
        hint="Pass --sheet NAME, and name the columns as in docs/discovery.md.",
    )


def read_sheet(path: Path, sheet: str | None = None) -> Sheet:
    """Read a mapping sheet from a ``.xlsx``/``.xlsm`` or ``.csv``/``.tsv``/``.txt`` file."""
    if not path.is_file():
        raise ConfigError(f"Mapping file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx(path, sheet)
    if suffix in (".csv", ".tsv", ".txt"):
        if sheet is not None:
            raise ConfigError("--sheet applies to Excel workbooks only.")
        return read_csv(path)
    raise ConfigError(
        f"Unsupported mapping file type '{path.suffix}'.", hint="Use .xlsx, .xlsm, or .csv."
    )


# ---------------------------------------------------------------------- parsing


class _Problems:
    def __init__(self, sheet: Sheet) -> None:
        self.sheet = sheet
        self.items: list[Diagnostic] = []

    def add(self, row: int | None, message: str, hint: str | None = None) -> None:
        place = [f"sheet '{self.sheet.sheet}'"] if self.sheet.sheet else []
        if row is not None:
            place.append(f"row {row}")
        prefix = ", ".join(place)
        self.items.append(
            Diagnostic(
                level=DiagnosticLevel.ERROR,
                code=Code.VALIDATION,
                message=f"{prefix}: {message}" if prefix else message,
                location=SourceLocation(file=str(self.sheet.file), line=row),
                hint=hint,
            )
        )


def _flag(value: str, field_name: str, row: int, problems: _Problems, default: bool) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return default
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    problems.add(row, f"'{field_name}' must be Y or N, got '{value}'")
    return default


def _parse_param(text: str) -> Any:
    text = text.strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d*", text):
        return float(text)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return text.strip("'\"")


def _split_items(text: str) -> list[str]:
    """Split on commas and semicolons outside parentheses."""
    items: list[str] = []
    depth, current = 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char in ",;\n" and depth == 0:
            items.append(current)
            current = ""
            continue
        current += char
    items.append(current)
    return [i.strip() for i in items if i.strip()]


def parse_normalizers(text: str, row: int, problems: _Problems, registry: Registry) -> list[Any]:
    """``rtrim; round(2); sap_alpha(length=10)`` as configuration normalizer entries."""
    result: list[Any] = []
    for item in _split_items(text):
        match = _NORMALIZER_ITEM.match(item)
        if match is None:
            problems.add(row, f"cannot read normalizer '{item}'", "Write name or name(args).")
            continue
        name, args = match.group(1), match.group(2)
        entry = registry.find(PluginKind.NORMALIZER, name)
        if entry is None:
            suggestions = registry.suggest(PluginKind.NORMALIZER, name)
            problems.add(
                row,
                f"unknown normalizer '{name}'",
                f"Did you mean '{suggestions[0]}'?"
                if suggestions
                else "Run 'dvf plugins list --kind normalizer'.",
            )
            continue
        params: dict[str, Any] = {}
        positional: list[Any] = []
        for arg in _split_items(args or ""):
            key, sep, value = arg.partition("=")
            if sep:
                params[key.strip()] = _parse_param(value)
            else:
                positional.append(_parse_param(arg))
        if positional and (params or len(positional) > 1):
            problems.add(row, f"normalizer '{item}': use one positional value or name=value pairs")
            continue
        ref: Any = name
        if positional:
            ref = {name: positional[0]}
        elif params:
            ref = {name: params}
        if entry.ready:
            try:
                normalized = TypeAdapter(NormalizerInput).validate_python(ref)
                entry.implementation(normalized.params)
            except (DvfError, ValidationError, TypeError) as exc:
                problems.add(row, f"normalizer '{item}': {exc}")
                continue
        result.append(ref)
    return result


def _bounds(text: str) -> tuple[Any, Any]:
    low, sep, high = text.partition("..")
    if not sep:
        raise ValueError("write MIN..MAX (either side may be empty)")
    return (
        _parse_param(low) if low.strip() else None,
        _parse_param(high) if high.strip() else None,
    )


def parse_rules(text: str, column: str, row: int, problems: _Problems) -> list[dict[str, Any]]:
    """Checks from the checks column, for ``column`` (see :data:`CHECK_RULES`)."""
    checks: list[dict[str, Any]] = []
    remaining = text.strip()
    while remaining:
        head, sep, tail = remaining.partition(";")
        name, colon, arg = head.partition(":")
        name = name.strip().lower()
        if name == "regex" and colon:
            arg, tail = remaining.split(":", 1)[1], ""  # a pattern takes the rest of the cell
        remaining = tail.strip() if sep or name == "regex" else ""
        arg = arg.strip()
        try:
            if name in ("not_null", "not_blank", "unique"):
                if arg:
                    raise ValueError(f"'{name}' takes no argument")
                checks.append({"type": name, "columns": [column]})
            elif name == "regex":
                if not arg:
                    raise ValueError("write regex:PATTERN")
                re.compile(arg)
                checks.append({"type": "regex", "column": column, "pattern": arg})
            elif name == "allowed_values":
                values = [v.strip() for v in arg.split("|") if v.strip()]
                if not values:
                    raise ValueError("write allowed_values:A|B|C")
                checks.append({"type": "allowed_values", "column": column, "values": values})
            elif name == "range":
                low, high = _bounds(arg)
                if low is None and high is None:
                    raise ValueError("give a minimum, a maximum, or both")
                check: dict[str, Any] = {"type": "range", "column": column}
                if low is not None:
                    check["min"] = low
                if high is not None:
                    check["max"] = high
                checks.append(check)
            elif name in ("length", "max_length"):
                low, high = (None, _parse_param(arg)) if name == "max_length" else _bounds(arg)
                if not all(v is None or isinstance(v, int) for v in (low, high)) or (
                    low is None and high is None
                ):
                    raise ValueError("lengths must be whole numbers")
                check = {"type": "length", "column": column}
                if low is not None:
                    check["min"] = low
                if high is not None:
                    check["max"] = high
                checks.append(check)
            elif name == "null_ratio":
                checks.append({"type": "null_ratio", "column": column, "max_ratio": float(arg)})
            else:
                problems.add(
                    row,
                    f"unknown rule '{name}' in the checks column",
                    "Known rules: " + ", ".join(CHECK_RULES) + ".",
                )
                continue
        except (ValueError, re.error) as exc:
            problems.add(row, f"rule '{head.strip()}': {exc}")
    return checks


# ---------------------------------------------------------------------- building


@dataclass
class _Column:
    row: int
    source: str
    source_expr: str
    target: str
    key: bool
    compare: bool
    pii: bool
    normalizers: list[Any]
    transformation: str
    notes: str


@dataclass
class _Table:
    suite: str
    source: str
    target: str
    rows: list[int] = field(default_factory=list)
    columns: list[_Column] = field(default_factory=list)
    source_filter: str = ""
    target_filter: str = ""
    checks: list[tuple[int, dict[str, Any]]] = field(default_factory=list)


@dataclass
class MappingResult:
    """The configuration generated from a mapping sheet."""

    document: Document
    tables: int
    columns: int
    sheet: str | None
    notes: list[str]
    output: Path | None = None

    @property
    def text(self) -> str:
        return dump(self.document)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output) if self.output else None,
            "sheet": self.sheet,
            "tables": self.tables,
            "columns": self.columns,
            "notes": self.notes,
        }


def _collect(
    sheet: Sheet, suite_name: str, problems: _Problems, registry: Registry
) -> list[_Table]:
    tables: dict[tuple[str, str, str], _Table] = {}
    # Target-only rows (audit columns) may leave the source table empty: they belong to the
    # source table the other rows of the same target table name.
    known_sources: dict[str, str] = {}
    for row in sheet.rows:
        if row.get("source_table") and row.get("target_table"):
            known_sources.setdefault(row.get("target_table").lower(), row.get("source_table"))
    for row in sheet.rows:
        n = row.number
        source_table, target_table = row.get("source_table"), row.get("target_table")
        source, expr, target = (
            row.get("source_column"),
            row.get("source_expr"),
            row.get("target_column"),
        )
        if not source_table and not source and not expr and target_table:
            source_table = known_sources.get(target_table.lower(), "")
        if not source_table:
            problems.add(n, "'source_table' is empty")
        if not target_table:
            problems.add(n, "'target_table' is empty")
        if source and expr:
            problems.add(n, "give 'source_column' or 'source_expr', not both")
        if not target and not source:
            problems.add(n, "the row has neither a source nor a target column")
        key = _flag(row.get("key"), "key", n, problems, False)
        compare = _flag(row.get("compare"), "compare", n, problems, True)
        pii = _flag(row.get("pii"), "pii", n, problems, False)
        if key and not (source and target):
            problems.add(n, "a key row needs both 'source_column' and 'target_column'")
        if compare and row.get("compare") and target and not (source or expr):
            problems.add(n, f"'{target}' is marked compare but has no source column or source_expr")
        if not source_table or not target_table:
            continue
        suite = row.get("suite") or suite_name
        table = tables.setdefault(
            (suite, source_table, target_table), _Table(suite, source_table, target_table)
        )
        table.rows.append(n)
        for field_name in ("source_filter", "target_filter"):
            value = row.get(field_name)
            current = getattr(table, field_name)
            if value and current and value != current:
                problems.add(n, f"'{field_name}' differs from an earlier row of the same table")
            elif value:
                setattr(table, field_name, value)
        normalizers = parse_normalizers(row.get("normalizers"), n, problems, registry)
        table.columns.append(
            _Column(
                row=n,
                source=source,
                source_expr=expr,
                target=target,
                key=key,
                compare=compare,
                pii=pii,
                normalizers=normalizers,
                transformation=row.get("transformation"),
                notes=row.get("notes"),
            )
        )
        rules = row.get("checks")
        if rules:
            if not target:
                problems.add(n, "checks need a 'target_column'")
            else:
                table.checks += [(n, c) for c in parse_rules(rules, target, n, problems)]
    return list(tables.values())


def _column_comment(col: _Column) -> str | None:
    parts = []
    if col.transformation.lower() not in _NO_TRANSFORMATION:
        parts.append(f"transformation: {col.transformation}")
    if col.notes:
        parts.append(col.notes)
    return f"row {col.row}: " + "; ".join(parts) if parts else None


def _merge_checks(table: _Table) -> list[tuple[list[int], dict[str, Any]]]:
    merged: list[tuple[list[int], dict[str, Any]]] = []
    by_type: dict[str, tuple[list[int], dict[str, Any]]] = {}
    for row, check in table.checks:
        if check["type"] in ("not_null", "not_blank"):
            existing = by_type.get(check["type"])
            if existing is None:
                existing = ([row], {"type": check["type"], "columns": list(check["columns"])})
                by_type[check["type"]] = existing
                merged.append(existing)
            else:
                existing[0].append(row)
                for column in check["columns"]:
                    if column not in existing[1]["columns"]:
                        existing[1]["columns"].append(column)
            continue
        merged.append(([row], dict(check)))
    seen: dict[tuple[str, str], int] = {}
    for _, check in merged:
        columns = check.get("columns") or [check.get("column")]
        ident = (check["type"], "+".join(str(c) for c in columns))
        count = seen.get(ident, 0) + 1
        seen[ident] = count
        if count > 1:
            check["name"] = f"rule_{count}"
    return merged


def _table_entry(
    table: _Table, name: str, include_unlisted: bool, sheet: Sheet
) -> tuple[Map, list[str]]:
    entry = Map()
    entry.add("name", name)
    source: dict[str, Any] = {"table": table.source}
    if table.source_filter:
        source["filter"] = table.source_filter
    target: dict[str, Any] = {"table": table.target}
    if table.target_filter:
        target["filter"] = table.target_filter
    entry.add("source", Flow(source))
    entry.add("target", Flow(target))
    comments: list[str] = []
    rows = sorted(table.rows)
    comments.append(
        f"{table.source} -> {table.target}, from {sheet.where()} row"
        + (f"s {rows[0]}-{rows[-1]}" if len(rows) > 1 else f" {rows[0]}")
    )
    keys: list[Any] = []
    for col in (c for c in table.columns if c.key):
        if col.source.lower() == col.target.lower() and not col.normalizers:
            keys.append(col.source)
            continue
        key: dict[str, Any] = {"source": col.source}
        if col.source.lower() != col.target.lower():
            key["target"] = col.target
        if col.normalizers:
            key["normalize"] = col.normalizers
        keys.append(Flow(key))
    if keys:
        entry.add("keys", keys)
    else:
        comments.append("REVIEW: no key rows; set 'keys' to enable row-level comparison")
    include: list[str] = []
    exclude: list[str] = []
    exclude_comments: list[str] = []
    mappings = Seq()
    for col in table.columns:
        if col.key:
            continue
        note = _column_comment(col)
        if not col.target:
            exclude.append(col.source)
            exclude_comments.append(f"row {col.row}: source column {col.source} is not migrated")
            continue
        if not col.source and not col.source_expr:
            exclude.append(col.target)
            exclude_comments.append(f"row {col.row}: target column {col.target} has no source")
            continue
        renamed = bool(col.source) and col.source.lower() != col.target.lower()
        if not (renamed or col.source_expr or col.normalizers or not col.compare or col.pii):
            include.append(col.target)
            if note:
                comments.append(f"{col.target}: {note}")
            continue
        mapping: dict[str, Any] = (
            {"source": col.source} if col.source else {"source_expr": col.source_expr}
        )
        mapping["target"] = col.target
        if col.normalizers:
            mapping["normalize"] = col.normalizers
        if not col.compare:
            mapping["compare"] = False
        if col.pii:
            mapping["pii"] = True
        mappings.add(Flow(mapping), inline=note)
    columns = Map()
    if not include_unlisted:
        columns.add(
            "include",
            sorted(set(include), key=str.lower),
            inline="columns listed in the sheet; mapped columns are always compared",
        )
    if exclude:
        columns.add("exclude", exclude, comment=exclude_comments)
    if len(mappings):
        columns.add("mappings", mappings)
    if len(columns):
        entry.add("columns", columns)
    if not keys:
        entry.add(
            "comparison",
            Map().add("strategies", ["row_count", "aggregate"]).add("on_mismatch", None),
        )
    checks = Seq()
    for check_rows, check in _merge_checks(table):
        checks.add(
            Flow(check),
            inline="row" + ("s " if len(check_rows) > 1 else " ") + ", ".join(map(str, check_rows)),
        )
    if len(checks):
        entry.add("checks", checks)
    return entry, comments


def _placeholder(side: str) -> ConnectionSpec:
    config = {"type": "duckdb", "database": f"./{side}.duckdb"}
    return ConnectionSpec(
        name=side,
        open_config=config,
        written_config=config,
        notes=[f"The {side} connection is a placeholder: replace it with your {side} system."],
    )


def import_mapping(
    path: Path,
    *,
    sheet: str | None = None,
    suite_name: str | None = None,
    project_name: str | None = None,
    loaded: LoadedConfig | None = None,
    config_path: Path | None = None,
    source: str | None = None,
    target: str | None = None,
    include_unlisted: bool = False,
    output: Path | None = None,
    overwrite: bool = False,
    registry: Registry | None = None,
) -> MappingResult:
    """Build a configuration from a mapping sheet and, with ``output``, write it.

    Connections come from ``loaded`` (connection names ``source`` and ``target``), from
    ``source`` and ``target`` locations (URLs, database files, folders), or are placeholders.
    """
    registry = registry or (loaded.registry if loaded is not None else get_registry())
    data = read_sheet(path, sheet)
    problems = _Problems(data)
    suite_default = suite_name or "mapping"
    tables = _collect(data, suite_default, problems, registry)
    if not data.rows:
        problems.add(None, "the sheet has a header row but no mapping rows")
    output_dir = (output.parent if output is not None else Path.cwd()).resolve()
    notes: list[str] = []
    if data.ignored_headers:
        notes.append("Columns not used: " + ", ".join(data.ignored_headers))
    if loaded is not None:
        assert config_path is not None
        names = list(loaded.config.connections)
        src_name = source or (names[0] if names else "source")
        tgt_name = target or (names[1] if len(names) > 1 else src_name)
        src = spec_from_config(loaded, config_path, src_name, output_dir=output_dir)
        tgt = spec_from_config(loaded, config_path, tgt_name, output_dir=output_dir)
    elif source or target:
        if not (source and target):
            raise ConfigError("Give both --source and --target locations, or neither.")
        src = spec_from_location(source, name="source", side="source", output_dir=output_dir)
        tgt = spec_from_location(target, name="target", side="target", output_dir=output_dir)
    else:
        src, tgt = _placeholder("source"), _placeholder("target")
    notes += src.notes + tgt.notes
    root = Map()
    root.add("version", 1)
    root.add("project", Map().add("name", project_name or file_stem(str(path)) or "mapping"))
    connections = Map()
    for spec in (src, tgt):
        if spec.name not in connections:
            inline = (
                "REVIEW: placeholder" if spec.notes and "placeholder" in spec.notes[0] else None
            )
            connections.add(spec.name, Flow(spec.written_config), inline=inline)
    root.add("connections", connections)
    suites = Seq()
    for suite in dict.fromkeys(t.suite for t in tables):
        names_used = UniqueNames()
        entries = Seq()
        for table in (t for t in tables if t.suite == suite):
            name = names_used.take(bare_name(table.target), "table")
            entry, comments = _table_entry(table, name, include_unlisted, data)
            entries.add(entry, comment=comments)
        suite_map = Map().add("name", suite).add("source", src.name).add("target", tgt.name)
        suite_map.add("tables", entries)
        suites.add(suite_map)
    root.add("suites", suites if len(suites) else [])
    document = Document(
        root,
        header=generated_header(
            "dvf import-mapping",
            f"Source: {data.where()}.",
            "Review the comments marked REVIEW, then run 'dvf validate-config' and 'dvf explain'.",
        ),
        footer=[f"Note: {n}" for n in notes],
    )
    if not problems.items:
        _validate(document, problems, registry, output_dir)
    if problems.items:
        raise ConfigError(
            f"{data.where()}: {len(problems.items)} problem(s) in the mapping sheet.",
            problems.items,
        )
    result = MappingResult(
        document=document,
        tables=len(tables),
        columns=sum(len(t.columns) for t in tables),
        sheet=data.sheet,
        notes=notes,
    )
    if output is not None:
        write_output(result.text, output, overwrite=overwrite)
        result.output = output
    return result


def _validate(document: Document, problems: _Problems, registry: Registry, base_dir: Path) -> None:
    """Load the generated configuration, so an invalid one is never written."""
    options = LoadOptions(strict=False, resolve_secrets=False, env_overrides=False)
    try:
        load_config_data(plain(document), options, registry=registry, base_dir=base_dir)
    except ConfigError as exc:
        for diagnostic in exc.diagnostics or []:
            problems.add(
                None,
                f"generated configuration is invalid at {diagnostic.path}: {diagnostic.message}",
            )
        if not exc.diagnostics:
            problems.add(None, f"generated configuration is invalid: {exc}")


def iter_aliases() -> Iterable[tuple[str, tuple[str, ...]]]:
    """Recognized fields and their header aliases (for documentation and help output)."""
    return HEADER_ALIASES.items()


__all__ = [
    "CHECK_RULES",
    "HEADER_ALIASES",
    "MappingResult",
    "Sheet",
    "import_mapping",
    "iter_aliases",
    "read_sheet",
]
