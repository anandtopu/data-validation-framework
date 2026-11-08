"""``dvf discover``: introspect two connections and generate a comparison configuration.

Tables are matched by name (exact, case-insensitive, then fuzzy), then their columns the same
way. Each column pair is classified with the canonical type system, primary keys are read from
the catalogs (or, with ``probe_keys``, inferred from data), and the result is written as a valid
configuration with comments on everything that needs review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dvf.config.loader import LoadedConfig
from dvf.config.models import DatasetSpec
from dvf.core.datasets import ColumnSchema, DatasetSchema
from dvf.core.errors import ConfigError, DvfError
from dvf.core.types import Compatibility, classify
from dvf.discovery.common import (
    DATA_SUFFIXES,
    UniqueNames,
    bare_name,
    file_stem,
    generated_header,
    write_output,
)
from dvf.discovery.connections import (
    ConnectionSpec,
    open_pool,
    spec_from_config,
    spec_from_location,
)
from dvf.discovery.matching import MatchMethod, match_items
from dvf.discovery.yamlwriter import Document, Flow, Map, Seq, dump
from dvf.plugins.base import Connector

_RISKS = (Compatibility.LOSSY, Compatibility.INCOMPATIBLE, Compatibility.UNKNOWN)
_KEY_SUFFIXES = ("_id", "_key", "_code", "_no", "_number", "id")
_MAX_PROBED = 5


@dataclass
class DiscoverOptions:
    """What to introspect and how to match it."""

    source_schema: str | None = None
    target_schema: str | None = None
    source_pattern: str = "*"
    """Glob over table names (or file paths, for file connections)."""
    target_pattern: str = "*"
    threshold: float = 0.8
    """Lowest fuzzy score accepted for table and column matches (0 to 1)."""
    fuzzy: bool = True
    probe_keys: bool = False
    """Infer keys of tables without a declared primary key by testing candidate columns for
    uniqueness in the source (runs one query per such table)."""
    suite_name: str = "discovered"
    project_name: str = "discovered"


@dataclass(frozen=True)
class Dataset:
    """A listed dataset: its name as the connector returns it and the name used for matching."""

    name: str
    key: str
    is_file: bool

    def spec(self) -> DatasetSpec:
        return DatasetSpec(path=self.name) if self.is_file else DatasetSpec(table=self.name)

    def locator(self) -> dict[str, str]:
        return {"path": self.name} if self.is_file else {"table": self.name}


@dataclass
class ColumnMatch:
    source: str
    target: str
    method: str
    score: float
    source_type: str
    target_type: str
    compatibility: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TableMatch:
    """One matched table pair and what discovery found out about it."""

    name: str
    source: str
    target: str
    method: str
    score: float
    keys: list[tuple[str, str]] = field(default_factory=list)
    key_origin: str | None = None
    key_candidates: list[str] = field(default_factory=list)
    columns: list[ColumnMatch] = field(default_factory=list)
    source_only_columns: list[str] = field(default_factory=list)
    target_only_columns: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def type_risks(self) -> list[ColumnMatch]:
        return [c for c in self.columns if c.compatibility in {r.value for r in _RISKS}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "method": self.method,
            "score": self.score,
            "keys": [{"source": s, "target": t} for s, t in self.keys],
            "key_origin": self.key_origin,
            "key_candidates": self.key_candidates,
            "columns": [c.to_dict() for c in self.columns],
            "source_only_columns": self.source_only_columns,
            "target_only_columns": self.target_only_columns,
            "problems": self.problems,
        }


@dataclass
class DiscoveryResult:
    """The generated configuration and a summary of what was matched."""

    source_connection: str
    target_connection: str
    tables: list[TableMatch]
    unmatched_source: list[str]
    unmatched_target: list[str]
    notes: list[str]
    document: Document
    output: Path | None = None

    @property
    def text(self) -> str:
        return dump(self.document)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_connection": self.source_connection,
            "target_connection": self.target_connection,
            "output": str(self.output) if self.output else None,
            "tables": [t.to_dict() for t in self.tables],
            "unmatched_source_tables": self.unmatched_source,
            "unmatched_target_tables": self.unmatched_target,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------- listing


def list_datasets(
    conn: Connector, schema: str | None, pattern: str
) -> tuple[list[Dataset], list[str]]:
    """Datasets of ``conn`` matching ``schema`` and ``pattern``, and notes about the listing."""
    notes: list[str] = []
    if conn.config.type == "files":
        if schema:
            notes.append(f"connection '{conn.name}' holds files; the schema filter is ignored")
        names = conn.list_datasets(pattern)
        files = [n for n in names if _is_data_file(n)]
        return [Dataset(n, file_stem(n), True) for n in files], notes
    glob = pattern
    if schema:
        if conn.dialect == "sqlite":
            notes.append(f"connection '{conn.name}' is SQLite; the schema filter is ignored")
        else:
            glob = f"{schema}.{pattern}"
    names = conn.list_datasets(glob)
    return [Dataset(quote_table(conn, n), bare_name(n), False) for n in names], notes


_FOLDING = {
    "postgres": re.compile(r"^[a-z_][a-z0-9_$]*$"),
    "redshift": re.compile(r"^[a-z_][a-z0-9_$]*$"),
    "snowflake": re.compile(r"^[A-Z_][A-Z0-9_$]*$"),
    "oracle": re.compile(r"^[A-Z_][A-Z0-9_$#]*$"),
}


def quote_table(conn: Connector, name: str) -> str:
    """``name`` with the parts quoted that the dialect would otherwise fold to another case
    (``sales.OrderLines`` becomes ``sales."OrderLines"`` in PostgreSQL)."""
    plain = _FOLDING.get(conn.dialect)
    if plain is None:
        return name
    parts = name.split(".")
    return ".".join(p if plain.match(p) else conn.sql_dialect.quote(p) for p in parts)


def _is_data_file(name: str) -> bool:
    lowered = name.lower()
    for suffix in (".gz", ".zst", ".bz2", ".snappy"):
        lowered = lowered.removesuffix(suffix)
    return lowered.endswith(DATA_SUFFIXES)


# ---------------------------------------------------------------------- keys


def _key_candidates(table: str, columns: list[ColumnSchema]) -> list[str]:
    stem = table.lower().rstrip("s")
    preferred = {"id", f"{table.lower()}_id", f"{stem}_id", f"{table.lower()}id", f"{stem}id"}
    first = [c.name for c in columns if c.name.lower() in preferred]
    rest = [
        c.name
        for c in columns
        if c.name not in first and c.name.lower().endswith(_KEY_SUFFIXES) and len(c.name) > 2
    ]
    return [*first, *rest]


def _probe_unique(conn: Connector, dataset: Dataset, columns: list[str]) -> str | None:
    """The first of ``columns`` that is unique and never NULL in the dataset."""
    if not columns:
        return None
    relation = conn.resolve(dataset.spec())
    dialect = conn.sql_dialect
    items = ["COUNT(*) AS dvf_rows"]
    for i, name in enumerate(columns):
        col = dialect.quote(name)
        items.append(f"COUNT(DISTINCT {col}) AS d{i}")
        items.append(f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS n{i}")
    row = conn.query(f"SELECT {', '.join(items)} FROM {relation.sql}").to_pylist()[0]
    total = int(row["dvf_rows"] or 0)
    if total == 0:
        return None
    for i, name in enumerate(columns):
        if int(row[f"d{i}"] or 0) == total and int(row[f"n{i}"] or 0) == 0:
            return name
    return None


# ---------------------------------------------------------------------- discovery


class _Discovery:
    def __init__(
        self,
        source: ConnectionSpec,
        target: ConnectionSpec,
        source_conn: Connector,
        target_conn: Connector,
        options: DiscoverOptions,
    ) -> None:
        self.source = source
        self.target = target
        self.source_conn = source_conn
        self.target_conn = target_conn
        self.options = options
        self.notes: list[str] = [*source.notes, *target.notes]

    def run(self) -> DiscoveryResult:
        opts = self.options
        src_sets, src_notes = list_datasets(
            self.source_conn, opts.source_schema, opts.source_pattern
        )
        tgt_sets, tgt_notes = list_datasets(
            self.target_conn, opts.target_schema, opts.target_pattern
        )
        self.notes += src_notes + tgt_notes
        if not src_sets:
            self.notes.append(
                f"no source datasets matched pattern '{opts.source_pattern}'"
                + (f" in schema '{opts.source_schema}'" if opts.source_schema else "")
            )
        matched = match_items(
            src_sets, tgt_sets, lambda d: d.key, threshold=opts.threshold, fuzzy=opts.fuzzy
        )
        names = UniqueNames()
        tables: list[TableMatch] = []
        for match in matched.matches:
            table = TableMatch(
                name=names.take(match.source.key, "table"),
                source=match.source.name,
                target=match.target.name,
                method=match.method.value,
                score=match.score,
            )
            try:
                self._inspect(table, match.source, match.target)
            except DvfError as exc:
                table.problems.append(f"could not read the table metadata: {exc}")
            tables.append(table)
        result = DiscoveryResult(
            source_connection=self.source.name,
            target_connection=self.target.name,
            tables=tables,
            unmatched_source=[d.name for d in matched.unmatched_source],
            unmatched_target=[d.name for d in matched.unmatched_target],
            notes=self.notes,
            document=Document(Map()),
        )
        result.document = self._document(result)
        return result

    def _schema(self, conn: Connector, dataset: Dataset) -> DatasetSchema:
        relation = conn.resolve(dataset.spec())
        schema = conn.get_schema(relation)
        if not schema.primary_key and not dataset.is_file:
            primary = conn.get_primary_key(relation)
            if primary:
                schema = schema.model_copy(update={"primary_key": primary})
        return schema

    def _inspect(self, table: TableMatch, source: Dataset, target: Dataset) -> None:
        opts = self.options
        src = self._schema(self.source_conn, source)
        tgt = self._schema(self.target_conn, target)
        columns = match_items(
            list(src.columns),
            list(tgt.columns),
            lambda c: c.name,
            threshold=opts.threshold,
            fuzzy=opts.fuzzy,
        )
        to_target: dict[str, str] = {}
        to_source: dict[str, str] = {}
        for m in columns.matches:
            result = classify(m.source.type, m.target.type)
            table.columns.append(
                ColumnMatch(
                    source=m.source.name,
                    target=m.target.name,
                    method=m.method.value,
                    score=m.score,
                    source_type=m.source.native_type,
                    target_type=m.target.native_type,
                    compatibility=result.level.value,
                    reasons=list(result.reasons),
                )
            )
            to_target[m.source.name.lower()] = m.target.name
            to_source[m.target.name.lower()] = m.source.name
        table.source_only_columns = [c.name for c in columns.unmatched_source]
        table.target_only_columns = [c.name for c in columns.unmatched_target]
        self._keys(table, source, src, tgt, to_target, to_source)

    def _keys(
        self,
        table: TableMatch,
        source: Dataset,
        src: DatasetSchema,
        tgt: DatasetSchema,
        to_target: dict[str, str],
        to_source: dict[str, str],
    ) -> None:
        if src.primary_key:
            mapped = [(c, to_target.get(c.lower())) for c in src.primary_key]
            if all(t is not None for _, t in mapped):
                table.keys = [(s, t) for s, t in mapped if t is not None]
                table.key_origin = "primary key declared in the source"
                return
            missing = [s for s, t in mapped if t is None]
            table.problems.append(
                f"source primary key column(s) {', '.join(missing)} have no target column"
            )
        if tgt.primary_key:
            back = [(to_source.get(c.lower()), c) for c in tgt.primary_key]
            if all(s is not None for s, _ in back):
                table.keys = [(s, t) for s, t in back if s is not None]
                table.key_origin = "primary key declared in the target"
                return
        paired = [c for c in src.columns if c.name.lower() in to_target]
        candidates = _key_candidates(bare_name(source.key), paired)
        if self.options.probe_keys and candidates:
            found = _probe_unique(self.source_conn, source, candidates[:_MAX_PROBED])
            if found is not None:
                table.keys = [(found, to_target[found.lower()])]
                table.key_origin = "unique and never NULL in the source data (probed)"
                return
        table.key_candidates = candidates[:_MAX_PROBED]

    # ------------------------------------------------------------------ output
    def _document(self, result: DiscoveryResult) -> Document:
        opts = self.options
        root = Map()
        root.add("version", 1)
        root.add("project", Map().add("name", opts.project_name))
        connections = Map()
        connections.add(self.source.name, Flow(self.source.written_config))
        if self.target.name != self.source.name:
            connections.add(self.target.name, Flow(self.target.written_config))
        root.add("connections", connections)
        suite = Map()
        suite.add("name", opts.suite_name)
        suite.add("source", self.source.name)
        suite.add("target", self.target.name)
        tables = Seq()
        for table in result.tables:
            tables.add(self._table(table), comment=self._table_comments(table))
        suite.add(
            "tables",
            tables if len(tables) else [],
            comment=None if len(tables) else "No tables were matched; see the notes at the end.",
        )
        root.add("suites", Seq().add(suite))
        source_label = f"'{self.source.name}' ({self.source.type})"
        target_label = f"'{self.target.name}' ({self.target.type})"
        header = generated_header(
            "dvf discover",
            f"Source connection {source_label}, target connection {target_label}.",
            "Names were matched exactly, then case-insensitively"
            + (f", then fuzzily (threshold {opts.threshold:.2f})." if opts.fuzzy else "."),
            "Review every comment marked REVIEW (fuzzy matches, type risks, missing keys),",
            "then check the file with 'dvf validate-config' and 'dvf explain'.",
        )
        footer: list[str] = []
        if result.unmatched_source:
            footer.append("Source tables without a target (not configured):")
            footer += [f"  - {name}" for name in result.unmatched_source]
        if result.unmatched_target:
            footer.append("Target tables without a source (not configured):")
            footer += [f"  - {name}" for name in result.unmatched_target]
        if result.notes:
            footer.append("Notes:")
            footer += [f"  - {note}" for note in result.notes]
        return Document(root, header=header, footer=footer)

    def _table_comments(self, table: TableMatch) -> list[str]:
        lines = [f"{table.source} -> {table.target}: {table.method.replace('_', '-')} match"]
        if table.method == MatchMethod.FUZZY.value:
            lines[0] += f" (score {table.score:.2f}). REVIEW: confirm these are the same table"
        risks = table.type_risks
        if risks:
            lines.append("REVIEW type risks:")
            for col in risks:
                reasons = "; ".join(col.reasons)
                lines.append(
                    f"  {col.target}: {col.source_type} -> {col.target_type} is {col.compatibility}"
                    + (f" ({reasons})" if reasons else "")
                )
        if table.source_only_columns:
            lines.append("Columns only in the source: " + ", ".join(table.source_only_columns))
        if table.target_only_columns:
            lines.append("Columns only in the target: " + ", ".join(table.target_only_columns))
        lines += [f"REVIEW: {p}" for p in table.problems]
        if not table.keys and table.key_candidates:
            lines.append(
                "REVIEW: no primary key found; candidate key columns (not verified): "
                + ", ".join(table.key_candidates)
            )
        elif not table.keys:
            lines.append("REVIEW: no primary key found; set 'keys' to enable row-level comparison")
        return lines

    def _table(self, table: TableMatch) -> Map:
        entry = Map()
        entry.add("name", table.name)
        source_ds = Dataset(table.source, "", self.source_conn.config.type == "files")
        target_ds = Dataset(table.target, "", self.target_conn.config.type == "files")
        entry.add("source", Flow(source_ds.locator()))
        entry.add("target", Flow(target_ds.locator()))
        if table.keys:
            keys: list[Any] = [
                s if s.lower() == t.lower() else Flow({"source": s, "target": t})
                for s, t in table.keys
            ]
            entry.add("keys", keys, inline=table.key_origin)
        key_sources = {s.lower() for s, _ in table.keys}
        mappings = Seq()
        for col in table.columns:
            if col.source.lower() == col.target.lower() or col.source.lower() in key_sources:
                continue
            inline = f"{col.method.replace('_', '-')} match"
            if col.method == MatchMethod.FUZZY.value:
                inline = f"fuzzy match (score {col.score:.2f}). REVIEW"
            mappings.add(Flow({"source": col.source, "target": col.target}), inline=inline)
        if len(mappings):
            entry.add("columns", Map().add("mappings", mappings))
        comparison = Map()
        if table.keys:
            comparison.add("strategies", ["row_count", "aggregate", "row_hash"])
            comparison.add("on_mismatch", "full_diff")
        else:
            comparison.add("strategies", ["row_count", "aggregate"])
            comparison.add("on_mismatch", None, inline="row-level comparison needs keys")
        entry.add("comparison", comparison)
        return entry


# ---------------------------------------------------------------------- entry point


def _connection_names(
    loaded: LoadedConfig, source: str | None, target: str | None
) -> tuple[str, str]:
    if source and target:
        return source, target
    config = loaded.config
    suite = next((s for s in config.suites if s.target is not None), None)
    if suite is not None and suite.target is not None:
        return source or suite.source, target or suite.target
    names = list(config.connections)
    if len(names) == 2:
        return source or names[0], target or names[1]
    raise ConfigError(
        "Cannot tell which connections to compare.",
        hint="Pass --source NAME and --target NAME (connections: "
        + (", ".join(names) or "none")
        + ").",
    )


def discover(
    *,
    loaded: LoadedConfig | None = None,
    config_path: Path | None = None,
    source: str | None = None,
    target: str | None = None,
    options: DiscoverOptions | None = None,
    output: Path | None = None,
    overwrite: bool = False,
) -> DiscoveryResult:
    """Run discovery and, when ``output`` is given, write the configuration there.

    With ``loaded`` (and its ``config_path``), ``source`` and ``target`` are connection names in
    that configuration. Without it they are locations: database URLs, database files, or folders.
    """
    options = options or DiscoverOptions()
    if not 0 < options.threshold <= 1:
        raise ConfigError(f"The fuzzy threshold must be between 0 and 1, got {options.threshold}.")
    output_dir = (output.parent if output is not None else Path.cwd()).resolve()
    if loaded is not None:
        assert config_path is not None
        src_name, tgt_name = _connection_names(loaded, source, target)
        src_spec = spec_from_config(loaded, config_path, src_name, output_dir=output_dir)
        tgt_spec = spec_from_config(loaded, config_path, tgt_name, output_dir=output_dir)
        pool = open_pool([src_spec, tgt_spec], base_dir=loaded.base_dir, loaded=loaded)
    else:
        if not source or not target:
            raise ConfigError(
                "Give a configuration file, or both --source and --target locations.",
                hint="For example: dvf discover --source duckdb:///legacy.duckdb "
                "--target postgresql://user@host/db",
            )
        src_spec = spec_from_location(source, name="source", side="source", output_dir=output_dir)
        tgt_spec = spec_from_location(target, name="target", side="target", output_dir=output_dir)
        pool = open_pool([src_spec, tgt_spec], base_dir=Path.cwd())
    try:
        source_conn = pool.get(src_spec.name)
        target_conn = pool.get(tgt_spec.name)
        result = _Discovery(src_spec, tgt_spec, source_conn, target_conn, options).run()
    finally:
        pool.close_all()
    if output is not None:
        write_output(result.text, output, overwrite=overwrite)
        result.output = output
    return result


__all__ = [
    "ColumnMatch",
    "DiscoverOptions",
    "DiscoveryResult",
    "TableMatch",
    "discover",
    "list_datasets",
]
