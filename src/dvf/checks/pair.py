"""Pair checks: validations that need both datasets of a table (migration validations).

A :class:`~dvf.plugins.base.PairCheck` receives a :class:`PairContext`: both sides' connectors,
relations (with the table's filters, window, and in-flight exclusion applied), schemas, and the
comparison plan with canonical keys. A check states its rule in SQL over a :class:`Workspace`:

* when both inputs live on one connection, the workspace runs the rule in that database, with each
  input inlined as a subquery (nothing leaves the database but counts and samples);
* otherwise each input is streamed, batch by batch, into a local DuckDB that spills to the run's
  temporary folder, so memory stays bounded, and the rule runs there.

Rules must therefore use portable SQL (``CASE``, ``COUNT``, ``SUM``, ``LEFT JOIN``, ``EXISTS``,
window functions) and the workspace's :attr:`Workspace.dialect` for anything dialect-specific.
Workspace column names are plain lower-case identifiers (``k0``, ``v_flag``) that are never
quoted, so they resolve the same way in every database.

Checks that need run-wide services (history, the state folder, their own check ID) subclass
:class:`EnvironmentAware`; the runner sets :attr:`EnvironmentAware.environment` before evaluation.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import duckdb
import pyarrow as pa

from dvf.core.enums import Side
from dvf.core.errors import PlanError
from dvf.engines.base import EngineResources
from dvf.execution.context import ConnectorPool, QueryRecorder, RunInfo
from dvf.models.results import QueryRecord, SampleSet
from dvf.plugins.base import Connector, Relation
from dvf.security.masking import Privacy
from dvf.sql.dialects import Dialect, get_dialect

if TYPE_CHECKING:
    from dvf.compare.context import ComparisonContext
    from dvf.compare.plan import ComparisonPlan
    from dvf.config.checks import CheckConfig
    from dvf.config.models import NormalizationConfig, SuiteConfig, TableConfig
    from dvf.core.datasets import DatasetSchema
    from dvf.execution.history import HistoryStore

log = logging.getLogger(__name__)

ROW_FILTERS_STATE = "dvf.row_filters"
"""Key in :attr:`ComparisonContext.state` holding the window and in-flight predicates per side
(``{"source": [...], "target": [...]}``), so checks can count the unfiltered population."""


# ====================================================================== environment


@dataclass(frozen=True)
class CheckEnvironment:
    """Where a check runs: its suite and table, its check ID, and run-wide services."""

    suite: SuiteConfig
    table: TableConfig
    check_id: str
    project: str
    base_dir: Path
    state_dir: Path
    run: RunInfo
    history_store: str | None = None
    """History store URL, when run history is enabled for this run."""
    baseline_policy: str = "last_passed"
    """The run's baseline policy (``history.baseline`` or ``--baseline``)."""
    resources: EngineResources = field(default_factory=EngineResources)

    def history(self) -> HistoryStore | None:
        """The run history store, or ``None`` when history is disabled for this run."""
        if self.history_store is None:
            return None
        from dvf.execution.history import HistoryStore

        return HistoryStore(self.history_store, self.base_dir)


class EnvironmentAware:
    """Mixin for checks that need their :class:`CheckEnvironment`.

    The runner sets :attr:`environment` on the check instance before calling ``evaluate`` (or
    ``evaluate_pair``). Outside a run (for example in ``dvf explain``) it stays ``None``.
    """

    environment: CheckEnvironment | None = None

    def require_environment(self) -> CheckEnvironment:
        if self.environment is None:
            raise PlanError(
                f"Check '{type(self).__name__}' needs the run context (history, state folder) and "
                "can only run inside 'dvf run'."
            )
        return self.environment


# ====================================================================== workspace


@dataclass(frozen=True)
class WorkspaceInput:
    """One query whose rows a :class:`Workspace` makes available as a table."""

    connector: Connector
    sql: str
    """A SELECT in the connector's dialect. Name its columns with plain lower-case aliases."""
    side: Side = Side.SOURCE
    """How the query is labelled in the recorded SQL."""


def _widen(schema: pa.Schema) -> pa.Schema:
    """``schema`` with types DuckDB cannot hold (or that a first batch guessed) made text."""
    fields = []
    for f in schema:
        if pa.types.is_null(f.type) or pa.types.is_decimal256(f.type):
            fields.append(pa.field(f.name, pa.string()))
        else:
            fields.append(f)
    return pa.schema(fields)


def _lower_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


class Workspace:
    """Where a pair check's rule runs: the shared database, or a local DuckDB with spill.

    Use :meth:`table` to reference an input in ``FROM`` clauses, and :meth:`rows`,
    :meth:`row`, or :meth:`scalar` to run rule queries. Close the workspace (or use it as a
    context manager) to drop the local database and its spill folder.
    """

    def __init__(
        self,
        inputs: Mapping[str, WorkspaceInput],
        *,
        recorder: QueryRecorder,
        resources: EngineResources | None = None,
        local: bool = False,
    ) -> None:
        if not inputs:
            raise PlanError("A workspace needs at least one input.")
        self.inputs = dict(inputs)
        self.recorder = recorder
        self.resources = resources or EngineResources()
        connectors = {id(i.connector): i.connector for i in self.inputs.values()}
        self._con: duckdb.DuckDBPyConnection | None = None
        self._temp_dir: str | None = None
        self.connector: Connector | None = None
        if len(connectors) == 1 and not local:
            self.mode: Literal["database", "local"] = "database"
            self.connector = next(iter(connectors.values()))
            self.dialect: Dialect = self.connector.sql_dialect
        else:
            self.mode = "local"
            self.dialect = get_dialect("duckdb")
            self._open_local()
            try:
                for name, item in self.inputs.items():
                    self._load(name, item)
            except BaseException:
                self.close()
                raise

    # ------------------------------------------------------------------ lifecycle
    def _open_local(self) -> None:
        resources = self.resources
        root = Path(resources.temp_dir) if resources.temp_dir else None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        self._temp_dir = tempfile.mkdtemp(prefix="dvf-pair-", dir=root)
        con = duckdb.connect(":memory:")
        con.execute(f"SET temp_directory = '{self._temp_dir.replace(chr(39), chr(39) * 2)}'")
        con.execute("SET preserve_insertion_order = false")
        if resources.memory_limit:
            con.execute(f"SET memory_limit = '{resources.memory_limit}'")
        if resources.threads:
            con.execute(f"SET threads = {int(resources.threads)}")
        self._con = con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
        if self._temp_dir is not None and not self.resources.keep_temp:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _load(self, name: str, item: WorkspaceInput) -> None:
        """Stream ``item``'s rows into the local table ``name``, one batch at a time."""
        con = self._con
        assert con is not None
        started = time.perf_counter()
        rows = 0
        schema: pa.Schema | None = None
        for batch in item.connector.iter_batches(item.sql):
            table = pa.Table.from_batches([batch])
            if schema is None:
                schema = _widen(table.schema)
                table = table.cast(schema)
                con.register("dvf_in", table)
                con.execute(f"CREATE TABLE {name} AS SELECT * FROM dvf_in")
            else:
                table = table.rename_columns(schema.names)
                # DuckDB casts on insert where Arrow cannot.
                with contextlib.suppress(pa.ArrowInvalid, pa.ArrowNotImplementedError):
                    table = table.cast(schema)
                con.register("dvf_in", table)
                con.execute(f"INSERT INTO {name} SELECT * FROM dvf_in")
            con.unregister("dvf_in")
            rows += table.num_rows
        if schema is None:
            # No batches at all: take the (empty) result's columns from a plain query.
            empty = item.connector.query(item.sql)
            empty = empty.cast(_widen(empty.schema))
            con.register("dvf_in", empty)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM dvf_in")
            con.unregister("dvf_in")
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self.recorder.add(item.connector, item.sql, item.side, elapsed, rows, engine="duckdb")

    # ------------------------------------------------------------------ SQL
    def table(self, name: str, alias: str | None = None) -> str:
        """``FROM`` item for input ``name`` (optionally aliased)."""
        if name not in self.inputs:
            raise PlanError(f"Workspace has no input '{name}'.")
        if self.mode == "database":
            # An unquoted alias without AS resolves the same way in every database (Oracle
            # rejects AS for table aliases and folds unquoted names to upper case).
            return f"(\n{self.inputs[name].sql}\n) {alias or name}"
        return name if alias is None else f"{name} {alias}"

    def query(self, sql: str) -> pa.Table:
        """Run a rule query in the workspace and record it."""
        if self.mode == "database":
            assert self.connector is not None
            return self.recorder.run(self.connector, sql, Side.BOTH)
        assert self._con is not None
        started = time.perf_counter()
        table = self._con.execute(sql).to_arrow_table()
        self.recorder.records.append(
            QueryRecord(
                side=Side.BOTH,
                dialect="duckdb",
                sql=sql.strip(),
                engine="duckdb",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                rows_returned=table.num_rows,
            )
        )
        return table

    def rows(self, sql: str) -> list[dict[str, Any]]:
        """Rows of a rule query, with lower-case column names."""
        return [_lower_keys(r) for r in self.query(sql).to_pylist()]

    def row(self, sql: str) -> dict[str, Any]:
        rows = self.rows(sql)
        return rows[0] if rows else {}

    def scalar(self, sql: str) -> Any:
        table = self.query(sql)
        if table.num_rows == 0 or table.num_columns == 0:
            return None
        return table.column(0)[0].as_py()

    def evidence(
        self, sql: str, *, sample_size: int, extract_rows: int, privacy: Privacy
    ) -> tuple[SampleSet | None, pa.Table | None]:
        """Masked samples (and an extract when enabled) from an unlimited query of failing rows."""
        limit = max(sample_size, extract_rows)
        if limit <= 0 or privacy.strict:
            return None, None
        return masked_evidence(
            self.query(self.dialect.limit(sql, limit)), sample_size, extract_rows, privacy
        )


def masked_evidence(
    table: pa.Table, sample_size: int, extract_rows: int, privacy: Privacy
) -> tuple[SampleSet | None, pa.Table | None]:
    """Samples and an optional extract from failing rows already fetched (masked)."""
    if privacy.strict:
        return None, None
    columns = [str(c).lower() for c in table.column_names]
    rows = [list(r.values()) for r in table.to_pylist()]
    masked = privacy.mask_rows(columns, rows)
    samples = None
    if sample_size > 0 and rows:
        samples = SampleSet(
            columns=columns,
            rows=masked[:sample_size],
            masked=privacy.mask_pii,
            truncated=len(rows) >= sample_size,
        )
    extract = None
    if extract_rows > 0 and len(rows) > sample_size:
        from dvf.execution.artifacts import text_table

        extract = text_table(columns, masked[:extract_rows])
    return samples, extract


# ====================================================================== pair context


@dataclass
class PairSide:
    """One side of a pair check: connector, relation (filters applied), schema, and key columns."""

    side: Side
    connector: Connector
    relation: Relation
    schema: DatasetSchema
    key_columns: list[str]
    """The table's key column names on this side, as configured."""

    @property
    def dialect(self) -> Dialect:
        return self.connector.sql_dialect

    def has_column(self, name: str) -> bool:
        return self.schema.column(name) is not None

    def column(self, name: str) -> str:
        """The real name of column ``name`` (matched case-insensitively)."""
        found = self.schema.column(name)
        if found is None:
            available = ", ".join(c.name for c in self.schema.columns[:30])
            raise PlanError(
                f"Column '{name}' not found in the {self.side.value} dataset "
                f"{self.relation.ref.display()}.",
                hint=f"Available columns: {available}",
            )
        return found.name

    def col(self, name: str) -> str:
        """Column ``name`` as a quoted identifier in this side's dialect."""
        return self.dialect.quote(self.column(name))


@dataclass(frozen=True)
class PairColumn:
    """A column loaded from both sides into a keyed workspace."""

    name: str
    """Its name in the workspace: a plain lower-case identifier such as ``v_flag``."""
    source: str | None = None
    """SQL expression on the source side, in the source dialect (``None``: NULL)."""
    target: str | None = None
    """SQL expression on the target side, in the target dialect (``None``: NULL)."""
    text: bool = False
    """Cast to text on each side, so values from different systems compare as strings."""


class PairContext:
    """What a pair check sees: both datasets, the comparison plan, SQL helpers, and evidence."""

    def __init__(
        self,
        *,
        config: CheckConfig,
        comparison: ComparisonContext,
        run: RunInfo,
        connectors: ConnectorPool,
        privacy: Privacy,
        sample_size: int,
        resources: EngineResources | None = None,
        environment: CheckEnvironment | None = None,
    ) -> None:
        table = comparison.table
        self.config = config
        self.comparison = comparison
        self.run = run
        self.connectors = connectors
        self.privacy = privacy
        self.sample_size = sample_size
        self.resources = resources or comparison.resources
        self.environment = environment
        self.recorder = QueryRecorder()
        self.extract_rows = 0
        """Failing rows to capture for an extract file (0: samples only). Set by the runner."""
        self.source = PairSide(
            Side.SOURCE,
            comparison.source.connector,
            comparison.source.relation,
            comparison.source_schema,
            [k.source for k in table.keys],
        )
        self.target = PairSide(
            Side.TARGET,
            comparison.target.connector,
            comparison.target.relation,
            comparison.target_schema,
            [k.target_name for k in table.keys],
        )

    # ------------------------------------------------------------------ basics
    @property
    def table(self) -> TableConfig:
        return self.comparison.table

    @property
    def normalization(self) -> NormalizationConfig:
        return self.comparison.normalization

    @property
    def same_connection(self) -> bool:
        return self.source.connector is self.target.connector

    @property
    def plan(self) -> ComparisonPlan:
        """The comparison plan with canonical keys; raises :class:`PlanError` without keys."""
        plan = self.comparison.plan
        if plan is None:
            error = self.comparison.plan_error
            if error is not None:
                raise error
            raise PlanError(
                f"Table '{self.table.label}' has no key columns, so rows cannot be paired.",
                hint="Set 'keys' (for example keys: [id]) or 'keys: auto' on the table.",
            )
        return plan

    def side(self, which: Side) -> PairSide:
        return self.target if which is Side.TARGET else self.source

    def render(self, text: str) -> str:
        """Replace ``${run.*}`` placeholders."""
        return self.run.render(text)

    def row_filters(self, which: Side) -> list[str]:
        """Window and in-flight predicates applied to ``which`` (already part of its relation)."""
        filters = self.comparison.state.get(ROW_FILTERS_STATE) or {}
        return list(filters.get(which.value, []))

    def side_where(self, which: Side) -> str | None:
        """The check's ``where`` (both sides) combined with ``source_where`` or ``target_where``."""
        parts: list[str] = []
        if self.config.where:
            parts.append(self.render(self.config.where))
        extra = getattr(self.config, f"{which.value}_where", None)
        if isinstance(extra, str) and extra:
            parts.append(self.render(extra))
        return " AND ".join(f"({p})" for p in parts) or None

    def from_where(
        self, which: Side, extra: str | None = None, *, relation_sql: str | None = None
    ) -> str:
        """``FROM <side> [WHERE <check filters> AND <extra>]``."""
        conditions = [c for c in (self.side_where(which), extra) if c]
        clause = f"FROM {relation_sql or self.side(which).relation.sql}"
        if conditions:
            clause += "\nWHERE " + " AND ".join(f"({c})" for c in conditions)
        return clause

    # ------------------------------------------------------------------ queries
    def query(self, which: Side, sql: str) -> pa.Table:
        """Run SQL on one side's connection, recording it."""
        return self.recorder.run(self.side(which).connector, sql, which)

    def rows(self, which: Side, sql: str) -> list[dict[str, Any]]:
        return [_lower_keys(r) for r in self.query(which, sql).to_pylist()]

    def row(self, which: Side, sql: str) -> dict[str, Any]:
        rows = self.rows(which, sql)
        return rows[0] if rows else {}

    def scalar(self, which: Side, sql: str) -> Any:
        table = self.query(which, sql)
        if table.num_rows == 0 or table.num_columns == 0:
            return None
        return table.column(0)[0].as_py()

    def count(self, which: Side, extra: str | None = None) -> int:
        """Rows of one side matching the check's filters and ``extra``."""
        return int(
            self.scalar(which, f"SELECT COUNT(*) AS n\n{self.from_where(which, extra)}") or 0
        )

    # ------------------------------------------------------------------ workspaces
    def key_exprs(self, which: Side) -> list[str]:
        """Canonical key expressions on one side (the same text on both sides for equal keys)."""
        return [
            (key.source if which is Side.SOURCE else key.target).canonical for key in self.plan.keys
        ]

    def key_names(self) -> list[str]:
        return [f"k{i}" for i in range(len(self.plan.keys))]

    def keyed(
        self,
        columns: Sequence[PairColumn] = (),
        *,
        source_where: str | None = None,
        target_where: str | None = None,
        local: bool = False,
    ) -> Workspace:
        """A workspace with tables ``src`` and ``tgt``: canonical keys ``k0..kN`` plus
        ``columns``, for the rows of each side matching the check's filters."""
        inputs: dict[str, WorkspaceInput] = {}
        for which, name, extra in (
            (Side.SOURCE, "src", source_where),
            (Side.TARGET, "tgt", target_where),
        ):
            side = self.side(which)
            items = [f"{expr} AS k{i}" for i, expr in enumerate(self.key_exprs(which))]
            for col in columns:
                expr = col.source if which is Side.SOURCE else col.target
                if expr is None:
                    expr = side.dialect.to_text("NULL") if col.text else "NULL"
                elif col.text:
                    expr = side.dialect.to_text(expr)
                items.append(f"{expr} AS {col.name}")
            sql = f"SELECT {', '.join(items)}\n{self.from_where(which, extra)}"
            inputs[name] = WorkspaceInput(side.connector, sql, which)
        return self.workspace(inputs, local=local)

    def workspace(self, inputs: Mapping[str, WorkspaceInput], *, local: bool = False) -> Workspace:
        """A workspace over arbitrary inputs (for example a key map or a change table)."""
        return Workspace(inputs, recorder=self.recorder, resources=self.resources, local=local)

    def evidence(self, ws: Workspace, sql: str) -> tuple[SampleSet | None, pa.Table | None]:
        """Masked samples and, when extracts are enabled, a larger extract of failing rows."""
        return ws.evidence(
            sql,
            sample_size=self.sample_size,
            extract_rows=self.extract_rows,
            privacy=self.privacy,
        )


__all__ = [
    "ROW_FILTERS_STATE",
    "CheckEnvironment",
    "EnvironmentAware",
    "PairColumn",
    "PairContext",
    "PairSide",
    "Workspace",
    "WorkspaceInput",
    "masked_evidence",
]
