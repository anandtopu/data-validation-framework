"""The runner: executes suites, tables, schema checks, comparisons, and data quality checks.

Tables run in parallel (``defaults.execution.max_parallel_tables``), each worker with its own
sessions and within per-connection caps. Finished tables are checkpointed, so ``--resume`` can
continue an interrupted or killed run. Every failure is contained: a broken query becomes an
``error`` result with a hint, and the rest of the run continues. Results JSON is always written,
also when a run is interrupted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from fnmatch import fnmatchcase
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import Any

from dvf import __version__
from dvf.checks.context import CheckContext, CheckOutcome
from dvf.checks.schema import SchemaInputs, run_schema_checks, schema_diff
from dvf.compare.context import ComparisonContext
from dvf.compare.plan import ColumnPairing, ComparisonPlan, build_plan, pair_columns
from dvf.config.base import NormalizerRef
from dvf.config.checks import CheckConfig
from dvf.config.loader import LoadedConfig
from dvf.config.models import DatasetSpec, KeyMapping, SuiteConfig, TableConfig
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Dimension, SchemaCheck, Severity, Side, Status
from dvf.core.errors import ConfigError, DvfError, PlanError
from dvf.core.types import CanonicalType
from dvf.engines.base import DiffSide, EngineResources
from dvf.execution.artifacts import write_extract, write_repro
from dvf.execution.checkpoint import CheckpointStore, load_manifest, prune_runs
from dvf.execution.context import ConnectorPool, RunInfo, new_run_id
from dvf.execution.history import HistoryStore, compare_to_baseline
from dvf.execution.selectors import Selectors
from dvf.execution.waivers import WaiverBook
from dvf.models.results import (
    ArtifactRef,
    CheckResult,
    DatasetPairResult,
    ErrorInfo,
    RunEnvironment,
    RunResult,
    SuiteResult,
    TimingBreakdown,
    aggregate_status,
    make_check_id,
)
from dvf.observability.logging import log_context
from dvf.plugins.base import Check, Comparator, Connector, Normalizer, Relation
from dvf.plugins.registry import PluginKind, Registry
from dvf.reporting.base import ReportOptions, ReportSection, render_reports
from dvf.security.masking import Privacy
from dvf.security.redaction import global_redactor

log = logging.getLogger(__name__)

_IDENTIFIER_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_ENGINE_DISTRIBUTIONS = ("duckdb", "pyarrow", "sqlglot", "polars", "psycopg", "pydantic")


@dataclass
class RunOptions:
    """How to run a configuration."""

    selectors: Selectors = field(default_factory=Selectors)
    run_id: str | None = None
    output_dir: Path | None = None
    formats: list[str] | None = None
    write_reports: bool = True
    cli_args: list[str] = field(default_factory=list)
    resume: str | None = None
    """Run ID to continue: finished tables and partitions are loaded from its checkpoints."""
    force_resume: bool = False
    """Resume even though the configuration changed since the run started."""
    parallel: int | None = None
    """Tables run at the same time; overrides ``defaults.execution.max_parallel_tables``."""
    keep_temp: bool = False
    """Keep the local engine's temporary files (for debugging)."""
    history: bool = True
    """Read and record run history (baseline, trends) when ``history.enabled``."""
    baseline: str | None = None
    """Baseline for the run-to-run comparison; overrides ``history.baseline``."""
    today: date | None = None
    """The date waivers expire against (tests)."""


@dataclass
class RunOutput:
    """A finished run and the files it wrote."""

    result: RunResult
    output_dir: Path | None
    artifacts: list[ArtifactRef] = field(default_factory=list)


@dataclass
class TableTask:
    """One table of one suite, as passed to the ``modify_plan`` hook."""

    suite: SuiteConfig
    table: TableConfig


@dataclass
class RunPlan:
    """The tables a run will validate. ``modify_plan`` hooks may remove or reorder tasks."""

    tasks: list[TableTask]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _severity_status(passed: bool | None, severity: Severity) -> Status:
    if passed is None or passed:
        return Status.PASSED
    return Status.WARNING if severity in (Severity.INFO, Severity.WARNING) else Status.FAILED


def error_info(exc: BaseException) -> ErrorInfo:
    """Error details for results: the message, a hint, and a short traceback (redacted)."""
    redact = global_redactor().redact_text
    hint = getattr(exc, "hint", None)
    if hint is None and not isinstance(exc, DvfError):
        hint = "This is unexpected; run with --log-level debug and report it if it persists."
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=6))
    return ErrorInfo(
        type=type(exc).__name__,
        message=redact(str(exc)),
        traceback=redact(tb[-4000:]),
        hint=hint,
    )


class Runner:
    """Runs one loaded configuration."""

    def __init__(self, loaded: LoadedConfig, options: RunOptions | None = None) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.registry: Registry = loaded.registry
        self.options = options or RunOptions()
        execution = self.config.defaults.execution
        state = Path(execution.state_dir)
        self.state_dir = state if state.is_absolute() else loaded.base_dir / state
        snapshot = global_redactor().redact(self.config.model_dump(mode="json", by_alias=True))
        assert isinstance(snapshot, dict)
        self.snapshot: dict[str, Any] = snapshot
        self.config_hash = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()
        ).hexdigest()
        started = datetime.now(UTC)
        run_id = self.options.run_id or new_run_id(started)
        if self.options.resume:
            manifest = load_manifest(self.state_dir, self.options.resume)
            if manifest.get("config_hash") != self.config_hash and not self.options.force_resume:
                raise ConfigError(
                    f"The configuration changed since run '{self.options.resume}' started, so its "
                    "checkpoints may not match.",
                    hint="Start a new run, or pass --force-resume to continue anyway.",
                )
            run_id = self.options.resume
            started = datetime.fromisoformat(manifest["started_at"])
            if self.options.selectors.empty and manifest.get("selectors"):
                self.options.selectors = Selectors.parse(list(manifest["selectors"]))
        self.info = RunInfo(run_id=run_id, started_at=started, profile=loaded.profile)
        self.checkpoints = CheckpointStore(self.state_dir, run_id, enabled=execution.checkpoints)
        self.pool = ConnectorPool(self.config, self.registry, loaded.base_dir)
        self.waivers = WaiverBook(self.config.waivers, self.options.today)
        self.resources = EngineResources(
            memory_limit=execution.memory_limit,
            threads=execution.threads,
            temp_dir=str(self._path(execution.temp_dir)) if execution.temp_dir else None,
            keep_temp=self.options.keep_temp,
        )
        self.output_dir: Path | None = None
        self._normalizers: dict[str, Normalizer] = {}
        self._normalizer_lock = threading.Lock()

    def _path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.loaded.base_dir / path

    # ------------------------------------------------------------------ hooks
    def _hook(self, name: str, **kwargs: Any) -> list[Any]:
        caller = getattr(self.registry.hooks.hook, name)
        return list(caller(**kwargs))

    # ------------------------------------------------------------------ entry point
    def run(self) -> RunOutput:
        started = time.perf_counter()
        with log_context(run_id=self.info.run_id):
            return self._run(started)

    def _workers(self, tasks: int) -> int:
        wanted = self.options.parallel or self.config.defaults.execution.max_parallel_tables
        return max(1, min(wanted, tasks))

    def _run(self, started: float) -> RunOutput:
        self._hook("on_run_start", run_id=self.info.run_id, config=self.config)
        plan = RunPlan(tasks=self._expand())
        self._hook("modify_plan", plan=plan)
        self.checkpoints.start(
            started_at=self.info.started_at,
            config_hash=self.config_hash,
            config_files=[str(f) for f in self.loaded.files],
            selectors=list(self.options.selectors.raw),
            profile=self.loaded.profile,
        )
        if self.options.write_reports:
            self.output_dir = self._output_dir()
        tasks = plan.tasks
        datasets: list[DatasetPairResult | None] = [None] * len(tasks)
        suite_started: dict[str, float] = {}
        for task in tasks:
            if task.suite.name not in suite_started:
                self._hook("on_suite_start", run_id=self.info.run_id, suite=task.suite)
                suite_started[task.suite.name] = time.perf_counter()
        pending: list[tuple[int, TableTask, str]] = []
        resumed = False
        for i, task in enumerate(tasks):
            key = CheckpointStore.table_key(task.suite.name, task.table.label)
            saved = self.checkpoints.load_table(key) if self.options.resume else None
            if saved is not None:
                datasets[i] = saved
                resumed = True
            else:
                pending.append((i, task, key))
        interrupted = False
        try:
            workers = self._workers(len(pending))
            if workers <= 1:
                for i, task, key in pending:
                    datasets[i] = self._execute(task, key)
            else:
                interrupted = self._run_parallel(pending, datasets, workers)
        except KeyboardInterrupt:
            interrupted = True
            log.warning("run %s interrupted; resume with --resume", self.info.run_id)
        finally:
            self.pool.close_all()
        suites: dict[str, SuiteResult] = {}
        for i, task in enumerate(tasks):
            dataset = datasets[i]
            if dataset is None:
                dataset = DatasetPairResult(
                    name=task.table.label,
                    status=Status.SKIPPED,
                    details={**task.table.details, "interrupted": True},
                )
            suite = suites.setdefault(
                task.suite.name,
                SuiteResult(
                    name=task.suite.name, status=Status.SKIPPED, tags=list(task.suite.tags)
                ),
            )
            suite.datasets.append(dataset)
        for name, suite in suites.items():
            suite.status = aggregate_status(d.status for d in suite.datasets)
            suite.duration_ms = _ms(suite_started[name])
            self._hook("on_suite_end", run_id=self.info.run_id, result=suite)
        result = self._assemble(list(suites.values()), started)
        result.interrupted = interrupted
        result.resumed_from_checkpoint = resumed
        result.waivers = self.waivers.statuses()
        history = self._history()
        if history is not None:
            self._compare_history(history, result)
        self._hook("on_run_end", result=result)
        results_path = self.checkpoints.finish(result, self.output_dir)
        output_dir: Path | None = None
        artifacts: list[ArtifactRef] = []
        if self.options.write_reports:
            output_dir, artifacts = self._write_reports(result)
            json_artifact = next((a for a in artifacts if a.format == "json"), None)
            if json_artifact is not None:
                results_path = output_dir / json_artifact.path
        if history is not None:
            self._record_history(history, result, results_path)
        self._write_sinks(result)
        return RunOutput(result=result, output_dir=output_dir, artifacts=artifacts)

    def _run_parallel(
        self,
        pending: list[tuple[int, TableTask, str]],
        datasets: list[DatasetPairResult | None],
        workers: int,
    ) -> bool:
        """Run tables on ``workers`` threads; return True if interrupted."""
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dvf-table")
        futures = {executor.submit(self._execute, task, key): i for i, task, key in pending}
        try:
            for future in as_completed(futures):
                datasets[futures[future]] = future.result()
        except KeyboardInterrupt:
            log.warning(
                "interrupted: finishing the tables already running; press Ctrl+C again to stop now"
            )
            for future in futures:
                future.cancel()
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except KeyboardInterrupt:
                executor.shutdown(wait=False, cancel_futures=True)
            for future, i in futures.items():
                if future.done() and not future.cancelled() and future.exception() is None:
                    datasets[i] = future.result()
            return True
        executor.shutdown(wait=True)
        return False

    def _execute(self, task: TableTask, key: str) -> DatasetPairResult:
        """Run one table with its own connection sessions, then checkpoint it."""
        with (
            log_context(run_id=self.info.run_id, suite=task.suite.name, table=task.table.label),
            self.pool.lease(self._connections_for(task)),
        ):
            dataset = self._run_table(task.suite, task.table, key)
        self.checkpoints.save_table(key, dataset)
        return dataset

    @staticmethod
    def _connections_for(task: TableTask) -> list[str]:
        """Every connection a table may use, so its lease holds them all up front."""
        names = {task.suite.source}
        if task.suite.target:
            names.add(task.suite.target)
        for spec in (task.table.source, task.table.target):
            if spec is not None and spec.connection:
                names.add(spec.connection)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    if k == "connection" and isinstance(v, str):
                        names.add(v)
                    else:
                        walk(v)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        for check in task.table.checks:
            walk(check.model_dump())
        return sorted(names)

    # ------------------------------------------------------------------ planning
    def _expand(self) -> list[TableTask]:
        tasks: list[TableTask] = []
        selectors = self.options.selectors
        for suite in self.config.suites:
            if not suite.enabled:
                continue
            for table in suite.tables:
                if not table.enabled:
                    continue
                expanded = (
                    self._expand_pattern(suite, table) if table.pattern is not None else [table]
                )
                for item in expanded:
                    if selectors.table_selected(suite.name, item.label, [*suite.tags, *item.tags]):
                        tasks.append(TableTask(suite, item))
        return tasks

    def _expand_pattern(self, suite: SuiteConfig, table: TableConfig) -> list[TableConfig]:
        assert table.pattern is not None
        source_conn = (table.source.connection if table.source else None) or suite.source
        try:
            names = self.pool.get(source_conn).list_datasets(table.pattern.source)
        except DvfError as exc:
            log.warning(
                "pattern %s on %s could not be expanded: %s", table.pattern.source, source_conn, exc
            )
            names = []
        regex = re.compile(
            "^"
            + re.escape(table.pattern.source).replace(r"\*", "(.*)").replace(r"\?", "(.)")
            + "$",
            re.IGNORECASE,
        )
        tables: list[TableConfig] = []
        for name in names:
            match = regex.match(name) or regex.match(name.split(".")[-1])
            target_name = name
            if table.pattern.target is not None and match is not None:
                target_name = _fill_wildcards(table.pattern.target, list(match.groups()))
            label = _IDENTIFIER_UNSAFE.sub(
                "_", Path(name).stem if "/" in name or "." in Path(name).suffix else name
            )
            src = (table.source or DatasetSpec()).model_copy(update={"table": name})
            tgt = (table.target or DatasetSpec()).model_copy(update={"table": target_name})
            tables.append(
                table.model_copy(
                    update={"name": label[:128], "pattern": None, "source": src, "target": tgt}
                )
            )
        return tables

    # ------------------------------------------------------------------ tables
    def _normalizer(self, ref: NormalizerRef) -> Normalizer:
        key = str(ref)
        with self._normalizer_lock:
            cached = self._normalizers.get(key)
        if cached is not None:
            return cached
        entry = self.registry.get(PluginKind.NORMALIZER, ref.name)
        if not entry.ready:
            raise PlanError(
                f"Normalizer '{ref.name}' is declared but not implemented in this version.",
                hint="Run 'dvf plugins list --kind normalizer' to see the ready normalizers.",
            )
        normalizer = entry.implementation(ref.params)
        normalizer.plugin_name = ref.name
        with self._normalizer_lock:
            self._normalizers[key] = normalizer
        return normalizer  # type: ignore[no-any-return]

    def _run_table(
        self, suite: SuiteConfig, table: TableConfig, key: str | None = None
    ) -> DatasetPairResult:
        started = time.perf_counter()
        dataset = DatasetPairResult(
            name=table.label, status=Status.SKIPPED, details=dict(table.details)
        )
        checks: list[CheckResult] = []
        state = _TableState(
            suite=suite,
            table=table,
            dataset=dataset,
            checks=checks,
            key=key or CheckpointStore.table_key(suite.name, table.label),
        )
        try:
            self._resolve(state)
        except Exception as exc:
            log.debug("table %s.%s could not be resolved", suite.name, table.label, exc_info=True)
            checks.append(self._error_result(state, "dataset.access", exc, Dimension.SCHEMA))
        else:
            if suite.is_comparison:
                self._schema_and_comparisons(state)
            elif SchemaCheck.OBJECT_EXISTS in table.schema_checks:
                self._source_exists(state)
            self._quality_checks(state)
        dataset.checks = _dedupe_ids(checks)
        dataset.status = aggregate_status(c.status for c in dataset.checks)
        dataset.duration_ms = _ms(started)
        return dataset

    def _resolve(self, state: _TableState) -> None:
        suite, table = state.suite, state.table
        assert table.source is not None
        state.source_conn = self.pool.get(table.source.connection or suite.source)
        state.source_rel = state.source_conn.resolve(self._rendered(table.source))
        state.dataset.source = state.source_rel.ref
        state.source_exists = state.source_conn.exists(state.source_rel)
        if state.source_exists:
            state.source_schema = state.source_conn.get_schema(state.source_rel)
        if suite.target is not None:
            target_spec = table.target or table.source.model_copy(
                update={"filter": None, "connection": None}
            )
            state.target_conn = self.pool.get(target_spec.connection or suite.target)
            state.target_rel = state.target_conn.resolve(self._rendered(target_spec))
            state.dataset.target = state.target_rel.ref
            state.target_exists = state.target_conn.exists(state.target_rel)
            if state.target_exists:
                state.target_schema = state.target_conn.get_schema(state.target_rel)

    def _source_exists(self, state: _TableState) -> None:
        """Single-dataset suites still report a missing dataset."""
        inputs = SchemaInputs(
            source_name=state.dataset.source.display() if state.dataset.source else "source",
            target_name="",
            source_exists=state.source_exists,
            target_exists=True,
        )
        for outcome in run_schema_checks([SchemaCheck.OBJECT_EXISTS], inputs):
            self._record(
                state, outcome, outcome.type or "schema", Dimension.SCHEMA, _const(outcome)
            )

    def _rendered(self, spec: DatasetSpec) -> DatasetSpec:
        updates = {
            name: self.info.render(value)
            for name in ("table", "query", "path", "filter")
            if isinstance(value := getattr(spec, name), str)
        }
        return spec.model_copy(update=updates)

    def _key_mappings(self, state: _TableState) -> list[KeyMapping]:
        table = state.table
        if table.keys or not table.discover_keys:
            return list(table.keys)
        assert state.source_conn is not None and state.source_rel is not None
        primary = state.source_conn.get_primary_key(state.source_rel)
        return [KeyMapping(source=name) for name in primary]

    def _schema_and_comparisons(self, state: _TableState) -> None:
        suite, table = state.suite, state.table
        pairing: ColumnPairing | None = None
        keys: list[KeyMapping] = []
        if state.source_schema is not None and state.target_schema is not None:
            keys = self._key_mappings(state)
            pairing = pair_columns(
                table, state.source_schema, state.target_schema, key_mappings=keys
            )
            state.dataset.schema_diff = schema_diff(pairing)
            state.pairing = pairing
        inputs = SchemaInputs(
            source_name=state.dataset.source.display() if state.dataset.source else "source",
            target_name=state.dataset.target.display() if state.dataset.target else "target",
            source_exists=state.source_exists,
            target_exists=state.target_exists,
            source=state.source_schema,
            target=state.target_schema,
            pairing=pairing,
            source_pk=state.source_schema.primary_key if state.source_schema else (),
            target_pk=state.target_schema.primary_key if state.target_schema else (),
            key_columns=[(k.source, k.target_name) for k in keys],
            probe_target_uniqueness=self._uniqueness_probe(state),
        )
        if table.schema_checks:
            for outcome in run_schema_checks(list(table.schema_checks), inputs):
                self._record(
                    state, outcome, outcome.type or "schema", Dimension.SCHEMA, _const(outcome)
                )
        if not (table.comparison.enabled and pairing is not None):
            return
        assert state.source_conn and state.target_conn and state.source_rel and state.target_rel
        assert state.source_schema is not None and state.target_schema is not None
        privacy = Privacy.from_config(
            suite.defaults.privacy,
            [m.target for m in table.columns.mappings if m.pii],
        )
        columns_plan: ComparisonPlan | None = None
        plan_error: PlanError | None = None
        try:
            columns_plan = build_plan(
                table,
                pairing,
                normalization=table.normalization,
                source_dialect=state.source_conn.sql_dialect,
                target_dialect=state.target_conn.sql_dialect,
                factory=self._normalizer,
                source_expr_types=self._source_expr_types(state, pairing),
                pii_columns=list(privacy.pii_columns),
                require_keys=False,
            )
            if not columns_plan.keys:
                plan_error = PlanError(
                    f"Table '{table.label}' has no key columns, so rows cannot be paired.",
                    hint="Set 'keys' (for example keys: [id]) or 'keys: auto' to use the primary key.",
                )
        except DvfError as exc:
            plan_error = exc if isinstance(exc, PlanError) else PlanError(str(exc))
        ctx = ComparisonContext(
            suite=suite,
            table=table,
            source=DiffSide(state.source_conn, state.source_rel),
            target=DiffSide(state.target_conn, state.target_rel),
            source_schema=state.source_schema,
            target_schema=state.target_schema,
            pairing=pairing,
            columns_plan=columns_plan,
            plan_error=plan_error,
            engine=table.engine,
            registry=self.registry,
            privacy=privacy,
            sample_size=table.sample_size,
            normalization=table.normalization,
            dataset=state.dataset,
            run=self.info,
            resources=self.resources,
            checkpoints=self.checkpoints.partitions(state.key),
        )
        strategies = list(table.comparison.strategies)
        any_failed = False
        for strategy in strategies:
            any_failed |= self._compare(state, ctx, strategy)
        escalation = table.comparison.on_mismatch
        if escalation and escalation not in strategies and any_failed:
            self._compare(state, ctx, escalation)

    def _source_expr_types(
        self, state: _TableState, pairing: ColumnPairing
    ) -> dict[str, CanonicalType]:
        exprs = [p.source_expr for p in pairing.pairs if p.source_expr]
        if not exprs:
            return {}
        assert state.source_conn is not None and state.source_rel is not None
        select = ", ".join(f"({e}) AS dvf_e{i}" for i, e in enumerate(exprs))
        relation = state.source_conn.resolve(
            DatasetSpec(query=f"SELECT {select} FROM {state.source_rel.sql}")
        )
        schema = state.source_conn.get_schema(relation)
        return {e: schema.columns[i].type for i, e in enumerate(exprs) if i < len(schema.columns)}

    def _uniqueness_probe(
        self, state: _TableState
    ) -> Callable[[list[str]], tuple[int, int, int]] | None:
        conn, rel, schema = state.target_conn, state.target_rel, state.target_schema
        if conn is None or rel is None or schema is None:
            return None

        def probe(columns: list[str]) -> tuple[int, int, int]:
            dialect = conn.sql_dialect
            real = [(schema.column(c).name if schema.column(c) else c) for c in columns]  # type: ignore[union-attr]
            cols = ", ".join(dialect.quote(c) for c in real)
            null_any = " OR ".join(f"{dialect.quote(c)} IS NULL" for c in real)
            not_null = " AND ".join(f"{dialect.quote(c)} IS NOT NULL" for c in real)
            row = conn.query(
                f"SELECT COUNT(*) AS total, "
                f"COALESCE(SUM(CASE WHEN {null_any} THEN 1 ELSE 0 END), 0) AS null_keys, "
                f"(SELECT COUNT(*) FROM (SELECT {cols} FROM {rel.sql} WHERE {not_null} "
                f"GROUP BY {cols}) AS dvf_u) AS distinct_keys FROM {rel.sql}"
            ).to_pylist()[0]
            total, nulls, distinct = (
                int(row[k] or 0) for k in ("total", "null_keys", "distinct_keys")
            )
            return total - nulls - distinct, nulls, total

        return probe

    def _compare(self, state: _TableState, ctx: ComparisonContext, strategy: str) -> bool:
        """Run one strategy; return True if any of its results did not pass."""
        failed = False
        try:
            entry = self.registry.get(PluginKind.COMPARATOR, strategy)
            if not entry.ready:
                raise PlanError(
                    f"Comparison strategy '{strategy}' is declared but not implemented in this version.",
                    hint="Ready strategies: row_count, aggregate, profile, row_hash, full_diff.",
                )
            comparator = entry.implementation()
            assert isinstance(comparator, Comparator)
            started = time.perf_counter()
            outcomes = comparator.compare(ctx)
            elapsed = _ms(started)
        except Exception as exc:
            log.debug("comparison %s failed", strategy, exc_info=True)
            state.checks.append(
                self._error_result(state, f"compare.{strategy}", exc, Dimension.RECONCILIATION)
            )
            return True
        for outcome in outcomes:
            self._record(
                state,
                outcome,
                outcome.type or f"compare.{strategy}",
                Dimension.RECONCILIATION,
                _const(outcome),
                elapsed_ms=elapsed,
            )
            # Escalate on the outcome itself, so a selected full_diff still runs when the
            # strategies before it are not selected (dvf rerun, --only).
            failed |= outcome.passed is False or outcome.status in (
                Status.FAILED,
                Status.WARNING,
                Status.ERROR,
            )
        return failed

    def _quality_checks(self, state: _TableState) -> None:
        suite, table = state.suite, state.table
        for config in table.checks:
            if not config.enabled:
                continue
            sides = self._sides(suite, config)
            for side in sides:
                self._quality_check(
                    state, config, side, suffix=side.value if len(sides) > 1 else None
                )

    @staticmethod
    def _sides(suite: SuiteConfig, config: CheckConfig) -> list[Side]:
        side = config.side or (Side.TARGET if suite.is_comparison else Side.SOURCE)
        if side is Side.BOTH:
            return [Side.SOURCE, Side.TARGET] if suite.is_comparison else [Side.SOURCE]
        return [side]

    def _quality_check(
        self, state: _TableState, config: CheckConfig, side: Side, suffix: str | None
    ) -> None:
        table = state.table
        if side is Side.TARGET:
            conn, rel, schema, exists = (
                state.target_conn,
                state.target_rel,
                state.target_schema,
                state.target_exists,
            )
            keys = [k.target_name for k in table.keys]
        else:
            conn, rel, schema, exists = (
                state.source_conn,
                state.source_rel,
                state.source_schema,
                state.source_exists,
            )
            keys = [k.source for k in table.keys]
        name = (
            config.name
            if suffix is None
            else f"{config.name or config.type}.{suffix}"
            if config.name
            else suffix
        )
        entry = self.registry.find(PluginKind.CHECK, config.type)
        dimension = (
            getattr(entry.implementation, "dimension", Dimension.CUSTOM)
            if entry
            else Dimension.CUSTOM
        )

        def evaluate() -> CheckOutcome:
            if conn is None or rel is None:
                return CheckOutcome(
                    status=Status.SKIPPED, message=f"there is no {side.value} dataset"
                )
            if not exists:
                return CheckOutcome(
                    status=Status.SKIPPED, message=f"the {side.value} dataset does not exist"
                )
            if entry is None or not entry.ready:
                raise PlanError(
                    f"Check type '{config.type}' has no implementation in this version.",
                    hint="Run 'dvf plugins list --kind check'.",
                )
            implementation = entry.implementation
            check = implementation(config)
            if not isinstance(check, Check):
                raise PlanError(f"Check '{config.type}' does not subclass dvf.plugins.Check.")
            ctx = CheckContext(
                config=config,
                side=side,
                connector=conn,
                relation=rel,
                schema=schema,
                sample_size=table.sample_size,
                key_columns=keys,
                run=self.info,
                connectors=self.pool,
                privacy=Privacy.from_config(
                    state.suite.defaults.privacy,
                    [m.target for m in table.columns.mappings if m.pii],
                ),
                normalization=table.normalization,
            )
            if self._extracts_enabled():
                ctx.extract_rows = self.config.reports.extracts.max_rows_per_check
            try:
                outcome = check.evaluate(ctx)
            except NotImplementedError as exc:
                raise PlanError(
                    str(exc) or f"Check '{config.type}' is not implemented",
                    hint="Implement evaluate() on the check class, or subclass RowCheck and implement predicate().",
                ) from exc
            finally:
                state.last_queries = ctx.recorder
            outcome.queries = outcome.queries or ctx.recorder.records
            if outcome.timing is None:
                outcome.timing = TimingBreakdown(
                    source_query_ms=round(ctx.recorder.source_ms, 3),
                    target_query_ms=round(ctx.recorder.target_ms, 3),
                )
            return outcome

        self._record(
            state,
            CheckOutcome(),
            config.type,
            dimension,
            evaluate,
            config=config,
            side=side,
            name=name,
        )

    # ------------------------------------------------------------------ results
    def _record(
        self,
        state: _TableState,
        placeholder: CheckOutcome,
        check_type: str,
        dimension: Dimension,
        produce: Callable[[], CheckOutcome],
        *,
        config: CheckConfig | None = None,
        side: Side | None = None,
        name: str | None = None,
        elapsed_ms: float | None = None,
    ) -> CheckResult | None:
        suite, table = state.suite, state.table
        columns = config.target_columns() if config is not None else []
        check_id = make_check_id(suite.name, table.label, check_type, columns=columns, name=name)
        severity = (config.severity if config and config.severity else None) or table.severity
        tags = [*suite.tags, *table.tags, *(config.tags if config else [])]
        if not self.options.selectors.check_selected(
            suite.name, table.label, check_type, check_id, tags, severity
        ):
            return None
        self._hook("on_check_start", run_id=self.info.run_id, check_id=check_id)
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            outcome = produce()
            error = None
        except Exception as exc:
            log.debug("check %s failed with an error", check_id, exc_info=True)
            outcome, error = CheckOutcome(status=Status.ERROR, message=str(exc)), error_info(exc)
        if outcome.severity_cap == "warning" and severity.rank > Severity.WARNING.rank:
            severity = Severity.WARNING
        status = outcome.status or _severity_status(outcome.passed, severity)
        duration = elapsed_ms if elapsed_ms is not None else _ms(started)
        result = CheckResult(
            check_id=check_id,
            name=name or (config.name if config and config.name else check_type),
            type=check_type,
            dimension=outcome.dimension or dimension,
            severity=severity,
            status=status,
            side=side,
            tags=list(dict.fromkeys(tags)),
            owner=(config.owner if config and config.owner else None) or table.owner or suite.owner,
            columns=outcome.columns or columns,
            message=outcome.message,
            observed=_json_safe(outcome.observed),
            expected=_json_safe(outcome.expected),
            unexpected_count=outcome.unexpected_count,
            total_count=outcome.total_count,
            unexpected_pct=(
                None
                if outcome.unexpected_count is None or not outcome.total_count
                else round(100.0 * outcome.unexpected_count / outcome.total_count, 6)
            ),
            mostly=config.mostly if config is not None and config.mostly < 1 else None,
            samples=outcome.samples,
            column_mismatches=outcome.column_mismatches,
            metrics=outcome.metrics,
            hints=outcome.hints,
            queries=outcome.queries,
            normalization=outcome.normalization,
            engine=outcome.engine,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            duration_ms=duration,
            timing=_with_total(outcome.timing, duration),
            error=error,
            details={**(config.details if config else {}), **outcome.details},
        )
        result = self.waivers.apply(result)
        result = self._attach_artifacts(state, result, outcome, config)
        self._hook("enrich_result", result=result)
        self._hook("on_check_complete", run_id=self.info.run_id, result=result)
        if result.status in (Status.FAILED, Status.ERROR):
            self._hook("on_failure", run_id=self.info.run_id, result=result)
        state.checks.append(result)
        return result

    def _extracts_enabled(self) -> bool:
        extracts = self.config.reports.extracts
        return (
            self.output_dir is not None
            and extracts.enabled
            and self.config.defaults.privacy.mode != "strict"
        )

    def _attach_artifacts(
        self,
        state: _TableState,
        result: CheckResult,
        outcome: CheckOutcome,
        config: CheckConfig | None,
    ) -> CheckResult:
        """Write the extract and repro files of a failed check and link them on the result."""
        if self.output_dir is None or result.status not in (
            Status.FAILED,
            Status.WARNING,
            Status.ERROR,
        ):
            return result
        artifacts: list[ArtifactRef] = []
        try:
            if outcome.extract is not None and self._extracts_enabled():
                extracts = self.config.reports.extracts
                artifacts.append(
                    write_extract(
                        self.output_dir,
                        result.check_id,
                        outcome.extract,
                        extracts.format,
                        extracts.max_rows_per_check,
                    )
                )
            if result.queries and result.status is not Status.WARNING:
                artifacts += write_repro(
                    self.output_dir,
                    result,
                    run_id=self.info.run_id,
                    config_file=self.loaded.files[0] if self.loaded.files else None,
                    profile=self.loaded.profile,
                    suite=state.suite,
                    table=state.table,
                    check=config,
                )
        except OSError as exc:
            log.warning("could not write artifacts for %s: %s", result.check_id, exc)
        if not artifacts:
            return result
        return result.model_copy(update={"artifacts": [*result.artifacts, *artifacts]})

    def _error_result(
        self, state: _TableState, check_type: str, exc: BaseException, dimension: Dimension
    ) -> CheckResult:
        info = error_info(exc)
        check_id = make_check_id(state.suite.name, state.table.label, check_type)
        result = CheckResult(
            check_id=check_id,
            name=check_type,
            type=check_type,
            dimension=dimension,
            severity=state.table.severity,
            status=Status.ERROR,
            message=info.message,
            error=info,
            tags=[*state.suite.tags, *state.table.tags],
        )
        self._hook("on_check_complete", run_id=self.info.run_id, result=result)
        self._hook("on_failure", run_id=self.info.run_id, result=result)
        return result

    def _assemble(self, suites: list[SuiteResult], started: float) -> RunResult:
        ended = datetime.now(UTC)
        snapshot = self.snapshot
        result = RunResult(
            run_id=self.info.run_id,
            project=self.config.project.name,
            status=aggregate_status(s.status for s in suites),
            started_at=self.info.started_at,
            ended_at=ended,
            duration_ms=_ms(started),
            environment=self._environment(snapshot),
            suites=suites,
            config_snapshot=snapshot,
            metadata=dict(self.config.project.metadata),
        )
        rows = sum(
            d.row_counts.source or 0 for s in suites for d in s.datasets if d.target is not None
        )
        result.summary = result.summary.model_copy(update={"rows_compared": rows})
        result.summary = result.summarize()
        return result

    def _environment(self, snapshot: dict[str, Any]) -> RunEnvironment:
        engines: dict[str, str] = {}
        for dist in _ENGINE_DISTRIBUTIONS:
            try:
                engines[dist] = dist_version(dist)
            except PackageNotFoundError:
                continue
        plugins = {
            e.distribution: e.version or ""
            for e in self.registry.entries()
            if e.distribution and e.distribution != "dvf"
        }
        redact = global_redactor().redact_text
        return RunEnvironment(
            dvf_version=__version__,
            python_version=platform.python_version(),
            platform=platform.platform(),
            hostname=socket.gethostname(),
            profile=self.loaded.profile,
            git_sha=_git_sha(self.loaded.base_dir),
            config_files=[str(f) for f in self.loaded.files],
            config_hash=self.config_hash,
            cli_args=[redact(a) for a in (self.options.cli_args or sys.argv[1:])],
            plugins=plugins,
            engines=engines,
        )

    def _output_dir(self) -> Path:
        if self.options.output_dir is not None:
            return self.options.output_dir
        rendered = Path(self.info.render(self.config.reports.output_dir))
        return rendered if rendered.is_absolute() else self.loaded.base_dir / rendered

    # ------------------------------------------------------------------ history and sinks
    def _history(self) -> HistoryStore | None:
        settings = self.config.history
        if not (settings.enabled and self.options.history):
            return None
        try:
            return HistoryStore(settings.store, self.loaded.base_dir)
        except DvfError as exc:
            log.warning("run history is unavailable: %s", exc)
            return None

    def _compare_history(self, history: HistoryStore, result: RunResult) -> None:
        policy = self.options.baseline or self.config.history.baseline
        try:
            baseline = history.find_baseline(result.project, policy, result.run_id)
            if baseline is not None:
                previous = history.check_statuses(baseline["run_id"])
                result.baseline = compare_to_baseline(result, baseline, previous, policy)
            result.trend = history.trend(result.project, result.run_id)
        except Exception as exc:  # history must never fail a run
            log.warning("could not read run history: %s", exc)

    def _record_history(
        self, history: HistoryStore, result: RunResult, results_path: Path | None
    ) -> None:
        try:
            history.record(result, results_path, [str(f) for f in self.loaded.files])
            keep = self.config.history.retention_runs
            if keep:
                history.prune(result.project, keep)
                prune_runs(self.state_dir, keep)
        except Exception as exc:  # history must never fail a run
            log.warning("could not record run history: %s", exc)

    def _write_sinks(self, result: RunResult) -> None:
        for sink_config in self.config.sinks:
            if not sink_config.enabled:
                continue
            try:
                entry = self.registry.get(PluginKind.SINK, sink_config.type)
                if not entry.ready:
                    raise PlanError(
                        f"Result sink '{sink_config.type}' is declared but not implemented in "
                        "this version.",
                        hint="Run 'dvf plugins list --kind sink' to see the ready sinks.",
                    )
                entry.implementation(sink_config, self.loaded.base_dir).write(result)
            except Exception as exc:  # a sink must never fail a run
                log.warning("result sink '%s' failed: %s", sink_config.type, exc)
                result.metadata.setdefault("sink_errors", []).append(
                    f"{sink_config.type}: {global_redactor().redact_text(str(exc))}"
                )

    def _write_reports(self, result: RunResult) -> tuple[Path, list[ArtifactRef]]:
        reports = self.config.reports
        base = self.loaded.base_dir
        output_dir = self.output_dir or self._output_dir()
        formats = list(dict.fromkeys(["json", *(self.options.formats or reports.formats)]))
        sections: list[ReportSection] = []
        for fmt in formats:
            for contributed in self._hook("report_sections", result=result, report_format=fmt):
                for item in contributed or []:
                    section = ReportSection(
                        str(item.get("title", "")), str(item.get("content", ""))
                    )
                    if section not in sections:
                        sections.append(section)
        branding = reports.branding
        logo = None
        if branding.logo:
            logo_path = Path(branding.logo)
            logo = logo_path if logo_path.is_absolute() else base / logo_path
        templates = None
        if reports.templates_dir:
            t = Path(reports.templates_dir)
            templates = t if t.is_absolute() else base / t
        options = ReportOptions(
            title=branding.title,
            logo=logo,
            primary_color=branding.primary_color,
            include_passed=reports.include_passed,
            max_samples_per_check=reports.max_samples_per_check,
            templates_dir=templates,
            extra_sections=tuple(sections),
            metadata=dict(self.config.project.metadata),
        )
        artifacts = render_reports(result, formats, output_dir, options, self.registry)
        return output_dir, artifacts


@dataclass
class _TableState:
    suite: SuiteConfig
    table: TableConfig
    dataset: DatasetPairResult
    checks: list[CheckResult]
    source_conn: Connector | None = None
    source_rel: Relation | None = None
    source_schema: DatasetSchema | None = None
    source_exists: bool = False
    target_conn: Connector | None = None
    target_rel: Relation | None = None
    target_schema: DatasetSchema | None = None
    target_exists: bool = False
    pairing: ColumnPairing | None = None
    last_queries: Any = None
    key: str = ""


def _const(outcome: CheckOutcome) -> Callable[[], CheckOutcome]:
    return lambda: outcome


def _fill_wildcards(pattern: str, groups: list[str]) -> str:
    """Replace each ``*`` or ``?`` in ``pattern`` with the next captured group."""
    values = iter(groups)
    return re.sub(r"[*?]", lambda _: next(values, ""), pattern)


def _with_total(timing: TimingBreakdown | None, duration: float) -> TimingBreakdown:
    timing = timing or TimingBreakdown()
    return timing.model_copy(update={"total_ms": duration})


def _json_safe(value: Any) -> Any:
    from dvf.security.masking import to_json_safe

    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return to_json_safe(value)


def _dedupe_ids(checks: list[CheckResult]) -> list[CheckResult]:
    seen: dict[str, int] = {}
    out: list[CheckResult] = []
    for check in checks:
        count = seen.get(check.check_id, 0) + 1
        seen[check.check_id] = count
        out.append(
            check
            if count == 1
            else check.model_copy(update={"check_id": f"{check.check_id}#{count}"})
        )
    return out


def _git_sha(base_dir: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git from PATH is intended
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def exit_code_for(
    result: RunResult,
    *,
    fail_on: Severity = Severity.ERROR,
    strict: bool = False,
    min_score: float | None = None,
) -> int:
    """0 passed, 1 validation failures (at or above ``fail_on``) or score below ``min_score``,
    2 execution errors."""
    checks = list(result.iter_checks())
    if result.interrupted or any(c.status is Status.ERROR for c in checks):
        return 2
    for check in checks:
        if check.status is Status.FAILED and check.severity.at_least(fail_on):
            return 1
        if check.status is Status.WARNING and (strict or check.severity.at_least(fail_on)):
            return 1
    score = result.summary.quality_score
    if min_score is not None and score is not None and score < min_score:
        return 1
    return 0


@contextmanager
def _noop() -> Iterator[None]:
    yield


__all__ = [
    "RunOptions",
    "RunOutput",
    "RunPlan",
    "Runner",
    "TableTask",
    "exit_code_for",
    "fnmatchcase",
]
