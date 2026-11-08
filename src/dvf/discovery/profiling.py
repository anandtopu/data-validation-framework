"""``dvf profile``: column profiles of a configuration's datasets, computed inside each database.

The statistics (row count, NULLs, distinct values, minimum, maximum, mean, and text lengths) are
built with the same SQL as the ``profile`` comparison strategy, so a profile and a profile
comparison always agree. Top values and recognized value formats take one more query each.
Values shown in profiles follow the configuration's privacy settings.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dvf.compare.comparators import _profile, _profile_items
from dvf.compare.plan import ColumnPair, ColumnPairing, build_plan
from dvf.config.loader import LoadedConfig
from dvf.config.models import DatasetSpec, NormalizationConfig, SuiteConfig, TableConfig
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Side
from dvf.core.errors import ConfigError, DvfError
from dvf.core.types import LogicalType
from dvf.discovery.patterns import KNOWN_FORMATS
from dvf.execution.runner import Runner, RunOptions
from dvf.execution.selectors import Selectors
from dvf.normalize.canonical import Kind
from dvf.plugins.base import Connector, Relation
from dvf.security.masking import Privacy, to_json_safe
from dvf.security.redaction import MASK

SideChoice = Literal["auto", "source", "target", "both"]

PROFILE_FORMAT_VERSION = 1
"""Version of the profile JSON document (``dvf profile --format json``)."""

_SKIPPED_TYPES = (
    LogicalType.ARRAY,
    LogicalType.MAP,
    LogicalType.STRUCT,
    LogicalType.SPATIAL,
    LogicalType.JSON,
    LogicalType.UNKNOWN,
)
_NO_TOP_VALUES = (LogicalType.BINARY, LogicalType.FLOAT)
_PROFILE_NORMALIZATION = NormalizationConfig(unicode_form="none")
"""Profiles describe stored values: no trimming, case folding, or Unicode normalization."""


@dataclass
class ColumnProfileEntry:
    """The profile of one column."""

    name: str
    native_type: str
    type: str
    logical: str
    nullable: bool | None
    count: int
    null_count: int
    null_pct: float | None = None
    distinct_count: int | None = None
    min: Any = None
    max: Any = None
    mean: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    avg_length: float | None = None
    top_values: list[list[Any]] = field(default_factory=list)
    """``[value, count]`` pairs, most frequent first (masked like report samples)."""
    formats: dict[str, int] = field(default_factory=dict)
    """Recognized formats and how many non-NULL values match each (text columns)."""
    masked: bool = False
    """Some values were masked or left out by the privacy settings."""

    @property
    def non_null(self) -> int:
        return self.count - self.null_count

    @property
    def all_distinct(self) -> bool:
        return self.distinct_count is not None and self.distinct_count == self.non_null

    def format_of_all(self) -> str | None:
        """The most specific format that every non-NULL value matches."""
        if self.non_null == 0:
            return None
        return next(
            (f.name for f in KNOWN_FORMATS if self.formats.get(f.name) == self.non_null), None
        )


@dataclass
class TableProfile:
    """The profiles of one side of one table."""

    suite: str
    table: str
    side: str
    dataset: str
    connection: str
    dialect: str
    row_count: int | None = None
    columns: list[ColumnProfileEntry] = field(default_factory=list)
    skipped_columns: list[str] = field(default_factory=list)
    """Columns not profiled (nested, spatial, JSON, and unknown types)."""
    is_comparison: bool = False
    """The table's suite compares a source with a target."""
    error: str | None = None
    queries: list[str] = field(default_factory=list)

    def column(self, name: str) -> ColumnProfileEntry | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)


@dataclass
class ProfileReport:
    """Profiles of every selected table and side."""

    project: str
    generated_at: str
    tables: list[TableProfile] = field(default_factory=list)
    version: int = PROFILE_FORMAT_VERSION

    @property
    def errors(self) -> list[TableProfile]:
        return [t for t in self.tables if t.error]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileReport:
        """Read a document written by :meth:`to_dict` (``dvf profile --format json``)."""
        if not isinstance(data, dict) or "tables" not in data:
            raise ConfigError("This is not a dvf profile document (no 'tables').")
        if data.get("version", PROFILE_FORMAT_VERSION) > PROFILE_FORMAT_VERSION:
            raise ConfigError(
                f"The profile document has version {data.get('version')}; this dvf reads "
                f"version {PROFILE_FORMAT_VERSION}."
            )
        tables = []
        for raw in data["tables"]:
            columns = [ColumnProfileEntry(**c) for c in raw.get("columns", [])]
            tables.append(TableProfile(**{**raw, "columns": columns}))
        return cls(
            project=str(data.get("project", "")),
            generated_at=str(data.get("generated_at", "")),
            tables=tables,
            version=int(data.get("version", PROFILE_FORMAT_VERSION)),
        )


# ---------------------------------------------------------------------- SQL


def profile_sql(
    relation: Relation, schema: DatasetSchema, connector: Connector, table: TableConfig
) -> tuple[str, list[Any], list[str]]:
    """The statistics query, the planned columns, and the names of skipped columns."""
    dialect = connector.sql_dialect
    eligible = [c for c in schema.columns if c.type.logical not in _SKIPPED_TYPES]
    skipped = [c.name for c in schema.columns if c.type.logical in _SKIPPED_TYPES]
    pairing = ColumnPairing(pairs=[ColumnPair(target=c, source=c) for c in eligible])
    plan = build_plan(
        table,
        pairing,
        normalization=_PROFILE_NORMALIZATION,
        source_dialect=dialect,
        target_dialect=dialect,
        factory=_no_normalizers,
        require_keys=False,
    )
    items = ["COUNT(*) AS dvf_rows"] + [
        item
        for i, col in enumerate(plan.columns)
        for item in _profile_items(col, i, "source", dialect)
    ]
    return f"SELECT {', '.join(items)}\nFROM {relation.sql}", plan.columns, skipped


def _no_normalizers(ref: Any) -> Any:  # pragma: no cover - profiles never use normalizers
    raise ConfigError(f"Profiles do not apply normalizers ({ref}).")


def _top_values_sql(relation: Relation, connector: Connector, column: str, top: int) -> str:
    dialect = connector.sql_dialect
    col = dialect.quote(column)
    sql = (
        f"SELECT {col} AS dvf_value, COUNT(*) AS dvf_count\nFROM {relation.sql}\n"
        f"WHERE {col} IS NOT NULL\nGROUP BY {col}\nORDER BY 2 DESC, 1"
    )
    return dialect.limit(sql, top)


def _formats_sql(relation: Relation, connector: Connector, columns: Sequence[str]) -> str:
    dialect = connector.sql_dialect
    items = []
    for i, name in enumerate(columns):
        text = dialect.to_text(dialect.quote(name))
        for fmt in KNOWN_FORMATS:
            match = dialect.regex_match(text, fmt.pattern, full=True)
            items.append(f"SUM(CASE WHEN {match} THEN 1 ELSE 0 END) AS f{i}_{fmt.name}")
    return f"SELECT {', '.join(items)}\nFROM {relation.sql}"


# ---------------------------------------------------------------------- profiling


class _Profiler:
    def __init__(self, loaded: LoadedConfig, *, top: int, formats: bool) -> None:
        self.loaded = loaded
        self.top = top
        self.formats = formats

    def side(
        self,
        runner: Runner,
        suite: SuiteConfig,
        table: TableConfig,
        side: Side,
    ) -> TableProfile:
        assert table.source is not None
        if side is Side.SOURCE:
            spec = table.source
            conn_name = spec.connection or suite.source
        else:
            spec = table.target or table.source.model_copy(
                update={"filter": None, "connection": None}
            )
            conn_name = spec.connection or suite.target or suite.source
        result = TableProfile(
            suite=suite.name,
            table=table.label,
            side=side.value,
            dataset="",
            connection=conn_name,
            dialect="",
            is_comparison=suite.is_comparison,
        )
        try:
            connector = runner.pool.get(conn_name)
            result.dialect = connector.dialect
            relation = connector.resolve(render_spec(runner, spec))
            result.dataset = relation.ref.display()
            if not connector.exists(relation):
                result.error = f"the {side.value} dataset {relation.ref.display()} does not exist"
                return result
            schema = connector.get_schema(relation)
            self._statistics(result, connector, relation, schema, suite, table, side)
        except DvfError as exc:
            result.error = str(exc)
        return result

    def _statistics(
        self,
        result: TableProfile,
        connector: Connector,
        relation: Relation,
        schema: DatasetSchema,
        suite: SuiteConfig,
        table: TableConfig,
        side: Side,
    ) -> None:
        sql, planned, skipped = profile_sql(relation, schema, connector, table)
        result.skipped_columns = skipped
        result.queries.append(sql)
        row = connector.query(sql).to_pylist()[0]
        total = int(row["dvf_rows"] or 0)
        result.row_count = total
        pii = [m.target for m in table.columns.mappings if m.pii]
        pii += [m.source for m in table.columns.mappings if m.pii and m.source]
        privacy = Privacy.from_config(suite.defaults.privacy, pii)
        by_name = {c.name: c for c in schema.columns}
        text_columns: list[str] = []
        for i, col in enumerate(planned):
            stats = _profile(row, i, total, col, "source")
            column = by_name[col.label]
            entry = ColumnProfileEntry(
                name=column.name,
                native_type=column.native_type,
                type=str(column.type),
                logical=column.type.logical.value,
                nullable=column.nullable,
                count=stats.count,
                null_count=stats.null_count,
                null_pct=stats.null_pct,
                distinct_count=None if stats.distinct_count is None else int(stats.distinct_count),
                min=stats.min,
                max=stats.max,
                mean=stats.mean,
                min_length=None if stats.min_length is None else int(stats.min_length),
                max_length=None if stats.max_length is None else int(stats.max_length),
                avg_length=stats.avg_length,
            )
            wants_top = self.top > 0 and entry.non_null > 0 and not entry.all_distinct
            if wants_top and column.type.logical not in _NO_TOP_VALUES:
                top_sql = _top_values_sql(relation, connector, column.name, self.top)
                result.queries.append(top_sql)
                entry.top_values = [
                    [to_json_safe(r["dvf_value"]), int(r["dvf_count"])]
                    for r in connector.query(top_sql).to_pylist()
                ]
            _apply_privacy(entry, privacy)
            if col.pair_spec.source.kind is Kind.TEXT and entry.non_null > 0:
                text_columns.append(column.name)
            result.columns.append(entry)
        if self.formats and text_columns:
            fmt_sql = _formats_sql(relation, connector, text_columns)
            result.queries.append(fmt_sql)
            counts = connector.query(fmt_sql).to_pylist()[0]
            for i, name in enumerate(text_columns):
                entry = next(c for c in result.columns if c.name == name)
                entry.formats = {
                    f.name: int(counts[f"f{i}_{f.name}"] or 0)
                    for f in KNOWN_FORMATS
                    if int(counts[f"f{i}_{f.name}"] or 0) > 0
                }


def render_spec(runner: Runner, spec: DatasetSpec) -> DatasetSpec:
    """``spec`` with ``${run.*}`` placeholders replaced, as a run would."""
    updates = {
        name: runner.info.render(value)
        for name in ("table", "query", "path", "filter")
        if isinstance(value := getattr(spec, name), str)
    }
    return spec.model_copy(update=updates)


def _apply_privacy(entry: ColumnProfileEntry, privacy: Privacy) -> None:
    if privacy.strict:
        entry.masked = bool(entry.min is not None or entry.max is not None or entry.top_values)
        entry.min = entry.max = None
        entry.top_values = []
        return
    masked = False
    for attr in ("min", "max"):
        value = getattr(entry, attr)
        shown = privacy.mask(entry.name, value)
        masked |= shown != value
        setattr(entry, attr, shown)
    values: list[list[Any]] = []
    for value, count in entry.top_values:
        shown = privacy.mask(entry.name, value)
        masked |= shown != value
        values.append([shown, count])
    entry.top_values = values
    entry.masked = masked


def is_masked(value: Any) -> bool:
    """Whether a profile value was masked by the privacy settings."""
    return value == MASK or (isinstance(value, str) and "***" in value)


def load_profile(report: ProfileReport | str | os.PathLike[str]) -> ProfileReport:
    """A profile report, or one read from a JSON file written by ``dvf profile --format json``."""
    if isinstance(report, ProfileReport):
        return report
    path = Path(report)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read the profile {path}: {exc}") from exc
    try:
        return ProfileReport.from_dict(data)
    except TypeError as exc:
        raise ConfigError(f"{path} is not a dvf profile document: {exc}") from exc


def selected_sides(suite: SuiteConfig, choice: SideChoice) -> list[Side]:
    """The sides to profile for ``suite``: ``auto`` is the target of comparison suites."""
    if not suite.is_comparison:
        if choice == "target":
            return []
        return [Side.SOURCE]
    if choice == "both":
        return [Side.SOURCE, Side.TARGET]
    if choice == "source":
        return [Side.SOURCE]
    return [Side.TARGET]


def profile(
    loaded: LoadedConfig,
    *,
    tables: Sequence[str] = (),
    suite: str | None = None,
    side: SideChoice = "auto",
    top: int = 5,
    formats: bool = True,
) -> ProfileReport:
    """Profile the selected tables (globs over table names) of a loaded configuration."""
    if top < 0:
        raise ConfigError("--top must be 0 or more.")
    only = [f"table:{t}" for t in tables] + ([f"suite:{suite}"] if suite else [])
    runner = Runner(loaded, RunOptions(selectors=Selectors.parse(only), write_reports=False))
    report = ProfileReport(
        project=loaded.config.project.name,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    profiler = _Profiler(loaded, top=top, formats=formats)
    try:
        for task in runner._expand():
            for chosen in selected_sides(task.suite, side):
                report.tables.append(profiler.side(runner, task.suite, task.table, chosen))
    finally:
        runner.pool.close_all()
    if not report.tables:
        raise ConfigError(
            "No tables matched the selection.",
            hint="Check --table and --suite (table names are matched as globs).",
        )
    return report


__all__ = [
    "PROFILE_FORMAT_VERSION",
    "ColumnProfileEntry",
    "ProfileReport",
    "TableProfile",
    "is_masked",
    "load_profile",
    "profile",
    "profile_sql",
    "render_spec",
    "selected_sides",
]
