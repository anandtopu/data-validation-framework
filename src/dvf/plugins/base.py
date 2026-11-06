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
    from dvf.compare.context import ComparisonContext
    from dvf.config.checks import CheckConfig
    from dvf.config.connections import ConnectionConfig
    from dvf.config.models import DatasetSpec
    from dvf.core.datasets import DatasetRef, DatasetSchema
    from dvf.core.types import CanonicalType
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


class Notifier(Component):
    """Base class for notification channels. Subclasses declare a nested ``Config`` model."""
