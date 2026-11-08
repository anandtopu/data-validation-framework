"""Base classes for plugin components (plugin API 0.x, ADR 0005).

Subclass these to add connectors, checks, comparators, normalizers, and reporters. Register the
subclass with a decorator from :mod:`dvf.plugins` or through an entry point.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ValidationError

from dvf.core.enums import Dimension
from dvf.core.errors import ConnectorError, PluginError

if TYPE_CHECKING:
    import polars as pl
    import pyarrow as pa

    from dvf.checks.context import CheckContext, CheckOutcome
    from dvf.checks.pair import PairContext
    from dvf.compare.context import ComparisonContext
    from dvf.config.checks import CheckConfig
    from dvf.config.connections import ConnectionConfig
    from dvf.config.models import DatasetSpec, SinkConfig
    from dvf.core.datasets import DatasetRef, DatasetSchema
    from dvf.core.types import CanonicalType
    from dvf.execution.context import RetryPolicy
    from dvf.models.results import ArtifactRef, RunResult
    from dvf.reporting.base import ReportOptions
    from dvf.sql.dialects import Dialect


class Component:
    """Common metadata for every component class."""

    plugin_name: ClassVar[str] = ""
    """Set by the registration decorators; may be set explicitly for entry points."""

    dvf_plugin_api: ClassVar[str | None] = None
    """Optional version specifier of the plugin API this component supports, such as ``">=0.1,<0.2"``."""


# ====================================================================== checks


class Check(Component):
    """Base class for checks.

    Subclasses declare a nested ``Config`` model (a subclass of
    :class:`~dvf.config.checks.CheckConfig`) and a :attr:`dimension`, and implement
    :meth:`evaluate`. For "every row must satisfy a condition" checks, subclass :class:`RowCheck`.
    """

    dimension: ClassVar[Dimension] = Dimension.CUSTOM

    def __init__(self, config: CheckConfig) -> None:
        self.config = config

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:  # pragma: no cover - interface
        """Run the check against ``ctx`` and return its outcome."""
        raise NotImplementedError(f"{type(self).__name__} does not implement evaluate()")


class RowCheck(Check):
    """A check that every row satisfies a SQL boolean expression.

    Implement :meth:`predicate`. dvf counts the rows where it is not true, applies ``where`` and
    ``mostly``, and captures sample rows. Build the expression with the context helpers
    (``ctx.col()``, ``ctx.literal()``, ``ctx.regex_match()``) so it works in every dialect.
    """

    null_passes: ClassVar[bool] = True
    """Rows where every checked column is NULL pass (leave NULL handling to ``not_null``)."""

    def predicate(self, ctx: CheckContext) -> str:  # pragma: no cover - interface
        """A SQL expression that is true for valid rows."""
        raise NotImplementedError(f"{type(self).__name__} does not implement predicate()")

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        columns = self.config.target_columns()
        return ctx.evaluate_predicate(
            self.predicate(ctx), columns=columns, null_passes=self.null_passes
        )


class PairCheck(Check):
    """A check that needs both datasets of a comparison table, usually paired by key.

    Implement :meth:`evaluate_pair`. It receives a :class:`~dvf.checks.pair.PairContext` holding
    both sides' connectors, relations, and schemas, the comparison plan (canonical keys), and
    helpers that bring selected keys and columns of both sides together (in a local DuckDB, or in
    the database when both sides share a connection) so the rule can be written as SQL. Pair
    checks run once per table (``side`` is ignored) and are skipped in single-dataset suites.
    """

    requires_keys: ClassVar[bool] = False
    """Whether the check pairs rows by key (it is then skipped when the table has no keys)."""

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        from dvf.core.errors import PlanError

        raise PlanError(
            f"Check '{self.plugin_name or type(self).__name__}' compares the source and target "
            "datasets, so it needs a comparison suite (one with a target connection)."
        )

    def evaluate_pair(self, ctx: PairContext) -> CheckOutcome:  # pragma: no cover - interface
        """Run the check against both datasets in ``ctx`` and return its outcome."""
        raise NotImplementedError(f"{type(self).__name__} does not implement evaluate_pair()")


# ====================================================================== connectors


@dataclass(frozen=True)
class Relation:
    """A dataset resolved by a connector, ready to be used in SQL."""

    ref: DatasetRef
    sql: str
    """An expression usable after ``FROM``, with the dataset's filter applied."""
    base_sql: str
    """The same expression without the filter."""
    table: str | None = None
    """The table name, when the dataset is a catalog table (used for metadata lookups)."""
    filter: str | None = None
    connection: str = ""


@dataclass(frozen=True)
class ConnectionReport:
    """The result of :meth:`Connector.test_connection`."""

    ok: bool
    message: str
    server_version: str | None = None
    duration_ms: float = 0.0


class Connector(Component):
    """Base class for connectors.

    A connector opens a session to one system, resolves dataset specifications into SQL
    relations, reports schemas, and runs queries that return Arrow data. Subclasses declare a
    nested ``Config`` model subclassing :class:`~dvf.config.connections.ConnectionConfig` and the
    SQL :attr:`dialect` their queries use.
    """

    dialect: ClassVar[str] = ""
    """SQL dialect used for pushdown queries (``duckdb``, ``postgres``, ``sqlite``, ...)."""

    def __init__(self, name: str, config: ConnectionConfig, base_dir: Path) -> None:
        self.name = name
        self.config = config
        self.base_dir = base_dir
        self.retry_policy: RetryPolicy | None = None
        """Set by the run: how often queries that failed transiently are retried."""

    def is_transient(self, exc: BaseException) -> bool:
        """Whether ``exc`` is worth retrying (a lost connection or a deadlock, not a bad query or
        a timeout). The default recognizes common driver messages; override for precision."""
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            text = str(current).lower()
            if any(phrase in text for phrase in _TRANSIENT_PHRASES):
                return True
            current = current.__cause__ or current.__context__
        return False

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def __enter__(self) -> Connector:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def sql_dialect(self) -> Dialect:
        from dvf.sql.dialects import get_dialect

        return get_dialect(self.dialect)

    def server_version(self) -> str | None:
        return None

    def fork(self) -> Connector:
        """A new, unopened session on the same system, used by another worker thread.

        The parallel executor gives each thread its own session. Override this when sessions
        must share state (DuckDB in-memory databases share one database through cursors).
        """
        return type(self)(self.name, self.config, self.base_dir)

    def test_connection(self) -> ConnectionReport:
        """Open the connection and run a trivial query."""
        started = time.perf_counter()
        try:
            self.open()
            self.query("SELECT 1 AS ok")
            version = self.server_version()
        except Exception as exc:
            return ConnectionReport(False, f"{type(exc).__name__}: {exc}", None, _ms_since(started))
        return ConnectionReport(True, "connected", version, _ms_since(started))

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:  # pragma: no cover - interface
        """Turn a dataset specification (table, query, or path) into a relation."""
        raise NotImplementedError

    def exists(self, relation: Relation) -> bool:
        """Whether the dataset exists. The default probes it with a query that returns no rows."""
        try:
            self.query(f"SELECT * FROM {relation.base_sql} WHERE 1 = 0")
        except Exception:
            return False
        return True

    def get_schema(self, relation: Relation) -> DatasetSchema:  # pragma: no cover - interface
        raise NotImplementedError

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        """Primary key columns, if the system declares them."""
        return ()

    def list_datasets(self, pattern: str) -> list[str]:
        """Names of datasets matching a glob pattern (used by pattern tables)."""
        raise ConnectorError(
            f"Connection '{self.name}' ({self.plugin_name or type(self).__name__}) cannot list datasets"
        )

    # ------------------------------------------------------------------ queries
    def query(self, sql: str) -> pa.Table:  # pragma: no cover - interface
        """Run a query and return all rows as an Arrow table (use for small results)."""
        raise NotImplementedError

    def iter_batches(
        self, sql: str, batch_size: int = 50_000
    ) -> Iterator[pa.RecordBatch]:  # pragma: no cover - interface
        """Run a query and stream the rows as Arrow record batches."""
        raise NotImplementedError

    def register_arrow(self, name: str, table: pa.Table) -> bool:
        """Make ``table`` queryable as ``name`` in this session, if the system can.

        Engines use it to filter rows by a list of keys without a long literal list. Return
        ``False`` (the default) when not supported; nothing is ever written to the source.
        """
        return False

    def unregister_arrow(self, name: str) -> None:
        """Undo :meth:`register_arrow`."""


_TRANSIENT_PHRASES = (
    "connection reset",
    "connection refused",
    "connection is closed",
    "connection already closed",
    "server closed the connection",
    "terminating connection",
    "lost connection",
    "broken pipe",
    "connection timed out",
    "deadlock",
    "could not serialize access",
    "temporarily unavailable",
    "too many connections",
)


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


# ====================================================================== comparators


class Comparator(Component):
    """Base class for comparison strategies (``row_count``, ``aggregate``, ``full_diff``, ...)."""

    def compare(self, ctx: ComparisonContext) -> list[CheckOutcome]:  # pragma: no cover
        """Compare the source and target datasets in ``ctx``."""
        raise NotImplementedError(f"{type(self).__name__} does not implement compare()")


# ====================================================================== normalizers


class Normalizer(Component):
    """Base class for normalizers.

    A normalizer rewrites values before canonicalization. It must give the same result in SQL,
    Polars, and Python; the parity tests in ``tests/parity`` check this for built-ins. Declare
    parameters with a ``params_model`` (a Pydantic model).
    """

    params_model: ClassVar[type[BaseModel] | None] = None

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        raw = dict(params or {})
        name = self.plugin_name or type(self).__name__
        if self.params_model is None:
            if raw:
                raise PluginError(f"Normalizer '{name}' takes no parameters, got {sorted(raw)}")
            self.params: BaseModel | None = None
            return
        try:
            self.params = self.params_model.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}"
                for e in exc.errors()
            )
            raise PluginError(f"Invalid parameters for normalizer '{name}': {problems}") from exc

    def applies_to(self, ctype: CanonicalType) -> bool:
        """Whether this normalizer changes columns of ``ctype`` (others pass through unchanged)."""
        return True

    def for_side(self, side: str) -> Normalizer | None:
        """This normalizer as used on ``side`` (``source`` or ``target``) of a comparison, or
        ``None`` to leave that side unchanged.

        The default returns ``self``: a normalizer applies on both sides, wherever
        :meth:`applies_to` accepts the column's type. Override it when the behavior depends on
        the side (``sap_curr`` shifts only the side that stores SAP-internal amounts, and reads
        the currency from a differently named column on each side). Return a copy instead of
        changing ``self``: one instance is shared by both sides and by worker threads.
        """
        return self

    def input_columns(self) -> tuple[str, ...]:
        """Other columns of the same row that this normalizer reads, by name (default: none).

        SQL implementations reference them with ``dialect.quote(name)``. The Polars engine
        fetches them along with the normalized column; :meth:`to_polars` reads them with
        :func:`polars_input`.
        """
        return ()

    def output_type(self, ctype: CanonicalType) -> CanonicalType:
        """The column type after normalization."""
        return ctype

    def to_sql(
        self, expr: str, dialect: Dialect, ctype: CanonicalType
    ) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def to_polars(self, expr: pl.Expr, ctype: CanonicalType) -> pl.Expr:  # pragma: no cover
        raise NotImplementedError(
            f"Normalizer '{self.plugin_name}' has no Polars implementation; use engine duckdb"
        )

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:  # pragma: no cover
        raise NotImplementedError


INPUT_COLUMN_PREFIX = "dvf_input__"
"""Prefix of the frame columns that hold a normalizer's :meth:`Normalizer.input_columns`."""


def polars_input(name: str) -> pl.Expr:
    """The Polars expression for column ``name`` of the same row, read by a normalizer that lists
    it in :meth:`Normalizer.input_columns`."""
    import polars as pl

    return pl.col(INPUT_COLUMN_PREFIX + name)


# ====================================================================== reporters and others


class Reporter(Component):
    """Base class for report renderers.

    A reporter turns a :class:`~dvf.models.results.RunResult` into files. It must not read data
    sources: everything it shows comes from the result document, so ``dvf report`` can re-render
    any format later.
    """

    format: ClassVar[str] = ""
    """Format name used in ``reports.formats``, such as ``html``."""

    def render(
        self, run: RunResult, out_dir: Path, options: ReportOptions
    ) -> list[ArtifactRef]:  # pragma: no cover - interface
        """Write the report into ``out_dir`` and return the files written."""
        raise NotImplementedError


class ResultSink(Component):
    """Base class for result sinks: write each run's results as tables, for dashboards.

    Subclasses implement :meth:`write`. They receive the sink's
    :class:`~dvf.config.models.SinkConfig` and the configuration's folder, so relative paths
    resolve like other paths in the configuration.
    """

    def __init__(self, config: SinkConfig, base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir

    def write(self, run: RunResult) -> None:  # pragma: no cover - interface
        """Append ``run`` (its run row and one row per check) to the sink's tables."""
        raise NotImplementedError


class Notifier(Component):
    """Base class for notification channels. Subclasses declare a nested ``Config`` model."""
