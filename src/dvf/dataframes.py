"""In-pipeline validation of DataFrames: :func:`validate_dataframe` (BUILD_PROMPT.md section 12.3).

A pipeline passes a pandas, Polars, Arrow, or Spark DataFrame and a list of checks, and gets back
the rows that passed, the rows that were quarantined (with a reason column), and the check
results::

    valid, quarantine, result = dvf.validate_dataframe(df, checks=[...], on_fail="quarantine")

Checks run through the same runner as ``dvf run``, against an in-process connection, so results
carry the same check IDs, severities, samples, and SQL as file-configured runs. Row-level checks
(``not_null``, ``regex``, ``range``, ``expression``, plugin ``RowCheck`` subclasses, ...) and
``unique`` also mark each failing row; dataset-level checks (``row_count_range``, ``freshness``,
``null_ratio``, ...) only affect the result.

What happens to a failing row is the check's ``action`` (or ``on_fail`` for checks without one):

* ``quarantine``: the row moves to the quarantine frame;
* ``drop``: the row is removed from both frames;
* ``warn``: the row stays valid and the check reports at most a warning;
* ``fail``: the row stays valid, and :class:`~dvf.core.errors.DataValidationError` is raised
  when the check fails.

A row failing several checks is quarantined if any of them quarantines it, else dropped if any
drops it. Rows are split with masks on the original frame, so pandas indexes, dtypes, and Polars
types are kept exactly. Nothing is written to disk: no reports, history, or checkpoints.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NamedTuple

import duckdb
import pyarrow as pa

from dvf.config.connections import ConnectionConfig, SparkConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.duckdb import DuckDBConnector
from dvf.connectors.spark import SparkConnector, global_temp_database
from dvf.core.datasets import DatasetKind, DatasetRef
from dvf.core.enums import Side, Status
from dvf.core.errors import ConnectorError, DataValidationError
from dvf.plugins.base import Connector, Relation

if TYPE_CHECKING:
    from dvf.config.checks import CheckConfig
    from dvf.models.results import DatasetPairResult
    from dvf.plugins.registry import Registry

Action = Literal["quarantine", "drop", "warn", "fail"]
ACTIONS: tuple[str, ...] = ("quarantine", "drop", "warn", "fail")
REASON_COLUMN = "dvf_reasons"
_ROW = "dvf_row_id"
_FRAME_VIEW = "dvf_frame"

# ====================================================================== in-process frames

_FRAMES: dict[str, Any] = {}
_FRAMES_LOCK = threading.Lock()


@contextmanager
def _registered(frame: Any) -> Iterator[str]:
    """Make ``frame`` (an Arrow table or a Spark DataFrame) reachable by a handle."""
    handle = secrets.token_hex(8)
    with _FRAMES_LOCK:
        _FRAMES[handle] = frame
    try:
        yield handle
    finally:
        with _FRAMES_LOCK:
            _FRAMES.pop(handle, None)


def _frame(handle: str) -> Any:
    with _FRAMES_LOCK:
        frame = _FRAMES.get(handle)
    if frame is None:
        raise ConnectorError(
            "This 'dataframe' connection has no DataFrame attached.",
            hint="DataFrame connections are created by dvf.validate_dataframe; they cannot be "
            "configured in a file.",
        )
    return frame


class DataFrameConnection(ConnectionConfig):
    """An in-process DataFrame passed through the Python API (``dvf.validate_dataframe``).

    Created by dvf; not meant to be written in configuration files.
    """

    handle: str
    """Identifier of the DataFrame registered by the Python API for this run."""
    label: str = "dataframe"
    """Name of the DataFrame shown in results."""


class DataFrameConnector(DuckDBConnector):
    """pandas, Polars, and Arrow data held in memory, queried with DuckDB without copying."""

    Config: ClassVar[type[ConnectionConfig]] = DataFrameConnection

    def _database(self) -> str:
        return ":memory:"

    def open(self) -> None:
        if self._con is not None:
            return
        assert isinstance(self.config, DataFrameConnection)
        table = _frame(self.config.handle)
        if not isinstance(table, pa.Table):
            raise ConnectorError("The 'dataframe' connector holds Arrow data only.")
        self._con = duckdb.connect(":memory:")
        self._con.execute("SET TimeZone = 'UTC'")
        self._con.register(_FRAME_VIEW, table)

    def fork(self) -> Connector:
        # Registered Arrow data belongs to one DuckDB connection; a fork registers it again
        # (without copying).
        return type(self)(self.name, self.config, self.base_dir)

    def resolve(self, spec: DatasetSpec) -> Relation:
        assert isinstance(self.config, DataFrameConnection)
        relation = super().resolve(spec.model_copy(update={"table": _FRAME_VIEW, "path": None}))
        ref = DatasetRef(connection=self.name, kind=DatasetKind.DATAFRAME, name=self.config.label)
        return replace(relation, ref=ref)

    def exists(self, relation: Relation) -> bool:
        return True

    def list_datasets(self, pattern: str) -> list[str]:
        return [_FRAME_VIEW]


class SparkFrameConnector(SparkConnector):
    """A Spark DataFrame passed through the Python API, queried in a child of its session.

    The DataFrame becomes a global temporary view, dropped when the connection closes; nothing
    is collected to the driver.
    """

    Config = DataFrameConnection  # type: ignore[assignment]
    _frame_sql: str | None = None

    @property
    def _config(self) -> SparkConnection:
        return SparkConnection(type="spark")

    def open(self) -> None:
        with self._shared.lock:
            if self._shared.session is not None:
                return
        assert isinstance(self.config, DataFrameConnection)
        frame = _frame(self.config.handle)
        if _kind(frame) != "spark":
            raise ConnectorError("The 'spark_dataframe' connector holds Spark DataFrames only.")
        session = self._attach(frame.sparkSession)
        view = f"dvf_frame_{secrets.token_hex(6)}"
        frame.createOrReplaceGlobalTempView(view)
        self._shared.global_views.add(view)
        quote = self.sql_dialect.quote
        self._frame_sql = f"{quote(global_temp_database(session))}.{quote(view)}"

    def fork(self) -> Connector:
        twin = super().fork()
        assert isinstance(twin, SparkFrameConnector)
        twin._frame_sql = self._frame_sql
        return twin

    def resolve(self, spec: DatasetSpec) -> Relation:
        assert isinstance(self.config, DataFrameConnection)
        self.open()
        base = self._frame_sql
        assert base is not None
        sql = base
        if spec.filter:
            sql = f"(SELECT * FROM {base} WHERE {spec.filter}) AS dvf_filtered"
        ref = DatasetRef(connection=self.name, kind=DatasetKind.DATAFRAME, name=self.config.label)
        return Relation(ref=ref, sql=sql, base_sql=base, filter=spec.filter, connection=self.name)

    def exists(self, relation: Relation) -> bool:
        return True

    def list_datasets(self, pattern: str) -> list[str]:
        return [_FRAME_VIEW]


# ====================================================================== validate_dataframe


class ValidationOutput(NamedTuple):
    """What :func:`validate_dataframe` returns; unpacks as ``valid, quarantine, result``."""

    valid: Any
    """Rows that were neither quarantined nor dropped, as the input's DataFrame type."""
    quarantine: Any
    """Quarantined rows with a ``dvf_reasons`` column naming the failed checks."""
    result: DatasetPairResult
    """The check results, as in a ``dvf run`` results document."""


_FrameKind = Literal["pandas", "polars", "polars_lazy", "arrow", "spark"]


def _kind(df: Any) -> _FrameKind:
    module = type(df).__module__.split(".")[0]
    name = type(df).__name__
    if module == "pandas" and name == "DataFrame":
        return "pandas"
    if module == "polars" and name == "DataFrame":
        return "polars"
    if module == "polars" and name == "LazyFrame":
        return "polars_lazy"
    if module == "pyarrow" and name in ("Table", "RecordBatch"):
        return "arrow"
    if module == "pyspark" and name == "DataFrame":
        return "spark"
    raise TypeError(
        f"validate_dataframe accepts pandas, Polars, Arrow, or Spark DataFrames, not {type(df)!r}"
    )


def _to_arrow(df: Any, kind: _FrameKind) -> pa.Table:
    if kind == "pandas":
        return pa.Table.from_pandas(df, preserve_index=False)
    if kind == "polars":
        return df.to_arrow()
    if kind == "arrow":
        return df if isinstance(df, pa.Table) else pa.Table.from_batches([df])
    raise AssertionError(kind)


def _check_dicts(checks: Sequence[CheckConfig | Mapping[str, Any]]) -> list[dict[str, Any]]:
    from dvf.config.checks import CheckConfig as _CheckConfig

    out: list[dict[str, Any]] = []
    for check in checks:
        if isinstance(check, _CheckConfig):
            data = check.model_dump(mode="json", by_alias=True, exclude_unset=True)
            data["type"] = check.type
        elif isinstance(check, Mapping):
            data = dict(check)
        else:
            raise TypeError(f"checks must be check models or mappings, not {type(check)!r}")
        out.append(data)
    return out


def validate_dataframe(
    df: Any,
    checks: Sequence[CheckConfig | Mapping[str, Any]],
    *,
    on_fail: Action = "quarantine",
    name: str = "dataframe",
    keys: Sequence[str] = (),
    severity: str = "error",
    sample_size: int = 20,
    privacy: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    registry: Registry | None = None,
) -> ValidationOutput:
    """Validate a DataFrame in a pipeline and split its rows by the outcome.

    ``checks`` are check configurations, as mappings (``{"type": "not_null", "columns": [...]}``)
    or check models. ``on_fail`` is the action for checks without their own ``action``.
    ``keys`` name the columns shown first in samples. ``privacy`` takes the
    ``defaults.privacy`` settings (samples are masked by default).

    Raises :class:`~dvf.core.errors.DataValidationError` when a check with action ``fail`` fails
    or when any check cannot be evaluated (status ``error``), and :class:`ConfigError` for
    invalid check configurations.
    """
    if on_fail not in ACTIONS:
        raise ValueError(f"on_fail must be one of {', '.join(ACTIONS)}, got {on_fail!r}")
    kind = _kind(df)
    if kind == "polars_lazy":
        raise TypeError("Collect the Polars LazyFrame first (validate_dataframe needs its rows).")
    if kind == "spark":
        return _validate_spark(
            df,
            checks,
            on_fail=on_fail,
            name=name,
            keys=keys,
            severity=severity,
            sample_size=sample_size,
            privacy=privacy,
            run_id=run_id,
            registry=registry,
        )
    table = _to_arrow(df, kind)
    if _ROW in table.column_names or REASON_COLUMN in table.column_names:
        raise ValueError(f"Columns named '{_ROW}' or '{REASON_COLUMN}' are reserved by dvf.")
    with _registered(table) as handle:
        result, loaded, registry = _run_checks(
            handle,
            "dataframe",
            checks,
            on_fail=on_fail,
            name=name,
            keys=keys,
            severity=severity,
            sample_size=sample_size,
            privacy=privacy,
            run_id=run_id,
            registry=registry,
        )
    indexed = table.append_column(_ROW, pa.array(range(table.num_rows), type=pa.int64()))
    with _registered(indexed) as handle:
        connector = DataFrameConnector(
            "dataframe", DataFrameConnection(type="dataframe", handle=handle), Path.cwd()
        )
        try:
            flags = _row_flags(connector, loaded, result, registry, on_fail)
        finally:
            connector.close()
    _raise_for(result, loaded, on_fail)
    if flags is None:
        return ValidationOutput(df, _empty_like(df, kind), result)
    quarantined, dropped, reasons = flags
    keep = [not (q or d) for q, d in zip(quarantined, dropped, strict=True)]
    return ValidationOutput(
        _take(df, kind, keep),
        _with_reasons(
            _take(df, kind, quarantined),
            kind,
            [r for r, q in zip(reasons, quarantined, strict=True) if q],
        ),
        result,
    )


def foreach_batch_validator(
    checks: Sequence[CheckConfig | Mapping[str, Any]],
    *,
    on_valid: Callable[[Any, int], None],
    on_quarantine: Callable[[Any, int], None] | None = None,
    on_result: Callable[[DatasetPairResult, int], None] | None = None,
    **options: Any,
) -> Callable[[Any, int], None]:
    """A function for Spark Structured Streaming's ``foreachBatch`` that validates each
    micro-batch with :func:`validate_dataframe` and hands the parts to your callbacks::

        query = stream.writeStream.foreachBatch(
            dvf.foreach_batch_validator(checks, on_valid=write_table, on_quarantine=write_bad)
        ).start()

    ``options`` are passed to :func:`validate_dataframe`. Works for any DataFrame type, so the
    same function can be unit-tested with a static frame.
    """

    def process(batch: Any, batch_id: int) -> None:
        run_id = options.get("run_id") or f"batch-{batch_id}-{secrets.token_hex(3)}"
        valid, quarantine, result = validate_dataframe(
            batch, checks, **{**options, "run_id": run_id}
        )
        on_valid(valid, batch_id)
        if on_quarantine is not None:
            on_quarantine(quarantine, batch_id)
        if on_result is not None:
            on_result(result, batch_id)

    return process


# ====================================================================== internals


def _effective_checks(
    checks: Sequence[CheckConfig | Mapping[str, Any]], on_fail: str
) -> list[dict[str, Any]]:
    out = []
    for data in _check_dicts(checks):
        action = data.get("action") or on_fail
        if action == "warn":
            data["severity"] = "warning"
        out.append(data)
    return out


def _run_checks(
    handle: str,
    connector_type: str,
    checks: Sequence[CheckConfig | Mapping[str, Any]],
    *,
    on_fail: str,
    name: str,
    keys: Sequence[str],
    severity: str,
    sample_size: int,
    privacy: Mapping[str, Any] | None,
    run_id: str | None,
    registry: Registry | None,
) -> tuple[DatasetPairResult, Any, Registry]:
    from dvf.api import load_config_data, run
    from dvf.plugins.registry import get_registry

    registry = registry or get_registry()
    defaults: dict[str, Any] = {
        "severity": severity,
        "sample_size": sample_size,
        "execution": {"checkpoints": False, "max_parallel_tables": 1},
    }
    if privacy:
        defaults["privacy"] = dict(privacy)
    config = {
        "version": 1,
        "project": {"name": "validate_dataframe"},
        "defaults": defaults,
        "connections": {"dataframe": {"type": connector_type, "handle": handle, "label": name}},
        "suites": [
            {
                "name": "pipeline",
                "source": "dataframe",
                "tables": [
                    {
                        "name": _table_name(name),
                        "source": {"table": _FRAME_VIEW},
                        "keys": list(keys),
                        "schema_checks": [],
                        "checks": _effective_checks(checks, on_fail),
                    }
                ],
            }
        ],
        "history": {"enabled": False},
        "reports": {"extracts": {"enabled": False}},
    }
    loaded = load_config_data(config, registry=registry, strict=False, resolve_secrets=False)
    output = run(
        loaded,
        write_reports=False,
        history=False,
        registry=registry,
        run_id=run_id or f"df-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}",
    )
    dataset = output.result.suites[0].datasets[0]
    return dataset, loaded, registry


def _table_name(name: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "dataframe"
    return cleaned if cleaned[0].isalpha() else f"df_{cleaned}"


def _check_results(result: DatasetPairResult) -> list[Any]:
    return [c for c in result.checks if not c.type.startswith("schema.")]


def _actions(loaded: Any, on_fail: str) -> list[str]:
    table = loaded.config.suites[0].tables[0]
    return [c.action or on_fail for c in table.checks if c.enabled]


def _row_flags(
    connector: DataFrameConnector,
    loaded: Any,
    result: DatasetPairResult,
    registry: Registry,
    on_fail: str,
) -> tuple[list[bool], list[bool], list[str]] | None:
    """Per row (in input order): quarantined, dropped, and the failed checks' names."""
    exprs = _failure_exprs(connector, loaded, result, registry, on_fail)
    if not exprs:
        return None
    dialect = connector.sql_dialect
    labels = ", ".join(
        f"CASE WHEN {flag} THEN {dialect.literal(label)} END" for flag, label, _ in exprs
    )
    quarantine = " OR ".join(f"({flag})" for flag, _, action in exprs if action == "quarantine")
    drop = " OR ".join(f"({flag})" for flag, _, action in exprs if action == "drop")
    sql = (
        f"SELECT {_ROW}, CONCAT_WS('; ', {labels}) AS reasons, "
        f"COALESCE({quarantine or 'FALSE'}, FALSE) AS q, COALESCE({drop or 'FALSE'}, FALSE) AS d "
        f"FROM {_FRAME_VIEW} ORDER BY {_ROW}"
    )
    table = connector.query(sql)
    return (
        [bool(v) for v in table.column("q").to_pylist()],
        [bool(v) for v in table.column("d").to_pylist()],
        [v or "" for v in table.column("reasons").to_pylist()],
    )


def _failure_exprs(
    connector: Connector,
    loaded: Any,
    result: DatasetPairResult,
    registry: Registry,
    on_fail: str,
) -> list[tuple[str, str, str]]:
    """``(failing-row SQL, label, action)`` for each check that can mark rows."""
    from dvf.checks.context import CheckContext
    from dvf.checks.quality import Unique
    from dvf.execution.context import ConnectorPool, RunInfo
    from dvf.plugins.base import RowCheck
    from dvf.plugins.registry import PluginKind
    from dvf.security.masking import Privacy

    config = loaded.config
    table = config.suites[0].tables[0]
    relation = connector.resolve(DatasetSpec(table=_FRAME_VIEW))
    schema = connector.get_schema(relation)
    results = _check_results(result)
    enabled = [c for c in table.checks if c.enabled]
    labels = (
        [_label(r.check_id) for r in results]
        if len(results) == len(enabled)
        else [c.name or c.type for c in enabled]
    )
    pool = ConnectorPool(config, registry, Path.cwd())
    out: list[tuple[str, str, str]] = []
    for check_config, label in zip(enabled, labels, strict=True):
        entry = registry.find(PluginKind.CHECK, check_config.type)
        if entry is None or not entry.ready:
            continue
        check = entry.implementation(check_config)
        ctx = CheckContext(
            config=check_config,
            side=Side.SOURCE,
            connector=connector,
            relation=relation,
            schema=schema,
            sample_size=0,
            key_columns=[k.source for k in table.keys],
            run=RunInfo(run_id="split", started_at=datetime.now(UTC)),
            connectors=pool,
            privacy=Privacy.from_config(config.defaults.privacy),
            normalization=table.normalization,
        )
        try:
            if isinstance(check, RowCheck):
                failing = _row_check_failing(ctx, check)
            elif isinstance(check, Unique):
                failing = _unique_failing(ctx)
            else:
                continue
        except Exception:  # noqa: S112 - the check's result already reports its error
            continue
        out.append((failing, label, check_config.action or on_fail))
    return out


def _label(check_id: str) -> str:
    parts = check_id.split(".", 2)
    return parts[2] if len(parts) == 3 else check_id


def _row_check_failing(ctx: Any, check: Any) -> str:
    columns = check.config.target_columns()
    valid = f"({check.predicate(ctx)})"
    if check.null_passes and columns:
        all_null = " AND ".join(f"{ctx.col(c)} IS NULL" for c in columns)
        valid = f"(({all_null}) OR {valid})"
    failing = f"(CASE WHEN {valid} THEN FALSE ELSE TRUE END)"
    if ctx.where:
        failing = f"(CASE WHEN ({ctx.where}) THEN {failing} ELSE FALSE END)"
    return failing


def _unique_failing(ctx: Any) -> str:
    columns = [ctx.col(c) for c in ctx.config.columns]
    cols = ", ".join(columns)
    in_scope = f"({ctx.where})" if ctx.where else "TRUE"
    if ctx.config.ignore_nulls:
        in_scope += " AND " + " AND ".join(f"{c} IS NOT NULL" for c in columns)
    counted = f"SUM(CASE WHEN {in_scope} THEN 1 ELSE 0 END) OVER (PARTITION BY {cols})"
    return f"(CASE WHEN {in_scope} THEN {counted} > 1 ELSE FALSE END)"


def _raise_for(result: DatasetPairResult, loaded: Any, on_fail: str) -> None:
    results = _check_results(result)
    actions = _actions(loaded, on_fail)
    errors = [r for r in results if r.status is Status.ERROR]
    if errors:
        first = errors[0]
        detail = first.error.message if first.error else first.message
        raise DataValidationError(
            f"{len(errors)} check(s) could not be evaluated; first: {first.check_id}: {detail}",
            result=result,
        )
    if len(actions) != len(results):
        return
    failed = [
        r
        for r, action in zip(results, actions, strict=True)
        if action == "fail" and r.status is Status.FAILED
    ]
    if failed:
        names = ", ".join(r.check_id for r in failed[:5])
        raise DataValidationError(
            f"{len(failed)} check(s) with action 'fail' failed: {names}", result=result
        )


def _take(df: Any, kind: _FrameKind, mask: list[bool]) -> Any:
    if kind == "pandas":
        return df[mask]
    if kind == "polars":
        import polars as pl

        return df.filter(pl.Series(mask, dtype=pl.Boolean))
    table = df if isinstance(df, pa.Table) else pa.Table.from_batches([df])
    return table.filter(pa.array(mask, type=pa.bool_()))


def _with_reasons(df: Any, kind: _FrameKind, reasons: list[str]) -> Any:
    if kind == "pandas":
        out = df.copy()
        out[REASON_COLUMN] = reasons
        return out
    if kind == "polars":
        import polars as pl

        return df.with_columns(pl.Series(REASON_COLUMN, reasons, dtype=pl.String))
    return df.append_column(REASON_COLUMN, pa.array(reasons, type=pa.string()))


def _empty_like(df: Any, kind: _FrameKind) -> Any:
    return _with_reasons(_take(df, kind, [False] * _length(df, kind)), kind, [])


def _length(df: Any, kind: _FrameKind) -> int:
    if kind == "pandas":
        return len(df)
    if kind == "polars":
        return int(df.height)
    return int(df.num_rows)


def _validate_spark(
    df: Any,
    checks: Sequence[CheckConfig | Mapping[str, Any]],
    *,
    on_fail: str,
    name: str,
    keys: Sequence[str],
    severity: str,
    sample_size: int,
    privacy: Mapping[str, Any] | None,
    run_id: str | None,
    registry: Registry | None,
) -> ValidationOutput:
    """Spark: checks run as Spark SQL in a child of the DataFrame's session, and the valid and
    quarantine frames are lazy Spark DataFrames derived from ``df`` (nothing is collected)."""
    from pyspark.sql import functions as F  # noqa: N812 - the conventional alias

    if REASON_COLUMN in df.columns:
        raise ValueError(f"Columns named '{REASON_COLUMN}' are reserved by dvf.")
    with _registered(df) as handle:
        result, loaded, registry = _run_checks(
            handle,
            "spark_dataframe",
            checks,
            on_fail=on_fail,
            name=name,
            keys=keys,
            severity=severity,
            sample_size=sample_size,
            privacy=privacy,
            run_id=run_id,
            registry=registry,
        )
        connector = SparkFrameConnector(
            "dataframe", DataFrameConnection(type="spark_dataframe", handle=handle), Path.cwd()
        )
        try:
            exprs = _failure_exprs(connector, loaded, result, registry, on_fail)
        finally:
            connector.close()
    _raise_for(result, loaded, on_fail)
    if not exprs:
        return ValidationOutput(df, df.limit(0).withColumn(REASON_COLUMN, F.lit("")), result)
    dialect = connector.sql_dialect
    labels = ", ".join(
        f"CASE WHEN {flag} THEN {dialect.literal(label)} END" for flag, label, _ in exprs
    )
    quarantine = " OR ".join(f"({flag})" for flag, _, action in exprs if action == "quarantine")
    drop = " OR ".join(f"({flag})" for flag, _, action in exprs if action == "drop")
    flagged = (
        df.withColumn(REASON_COLUMN, F.expr(f"CONCAT_WS('; ', {labels})"))
        .withColumn("dvf_q", F.expr(f"COALESCE({quarantine or 'FALSE'}, FALSE)"))
        .withColumn("dvf_d", F.expr(f"COALESCE({drop or 'FALSE'}, FALSE)"))
    )
    valid = flagged.filter("NOT dvf_q AND NOT dvf_d").drop("dvf_q", "dvf_d", REASON_COLUMN)
    quarantined = flagged.filter("dvf_q").drop("dvf_q", "dvf_d")
    return ValidationOutput(valid, quarantined, result)


__all__ = [
    "REASON_COLUMN",
    "DataFrameConnection",
    "DataFrameConnector",
    "SparkFrameConnector",
    "ValidationOutput",
    "foreach_batch_validator",
    "validate_dataframe",
]
