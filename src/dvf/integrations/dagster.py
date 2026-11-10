"""Dagster: asset checks built from a dvf configuration.

.. code-block:: python

    import dagster as dg
    from dvf.integrations.dagster import build_dvf_asset_checks

    dvf_checks = build_dvf_asset_checks("dvf/dvf.yaml", asset_key=lambda suite, table: [table])
    defs = dg.Definitions(assets=[...], asset_checks=[dvf_checks])

:func:`build_dvf_asset_checks` reads the configuration when the code location loads (without
fetching secrets) and declares one asset check per table (``granularity="table"``) or one per
configured check plus one for the schema checks and one for the comparison of each table
(``granularity="check"``). When the checks execute, one :func:`dvf.api.run` runs the whole
configuration and each asset check reports its share of the results, with metadata: status,
check counts, row counts, reconciliation totals, failed check IDs, and the results file.

Pass or fail follows ``dvf run``: an errored check, or a failed check at or above ``fail_on``,
fails the asset check with severity ``ERROR``; failed checks below ``fail_on`` (and warnings
with ``strict``) fail it with severity ``WARN``; a score below ``min_score`` fails it with
``ERROR``. A table that produced no results (filtered out or disabled) passes with
``dvf_status: not_run`` in its metadata, because Dagster expects a result for every check.

Importing this module needs ``dagster`` (1.7 or later); ``import dvf`` does not. It is verified
by unit tests with a stand-in ``dagster`` module, not on a running Dagster instance.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Literal

from dvf import api
from dvf.config.models import SuiteConfig, TableConfig
from dvf.core.enums import Severity, Status
from dvf.core.errors import ConfigError
from dvf.integrations.common import parse_severity, run_validation
from dvf.models.results import CheckResult, DatasetPairResult, RunResult, make_check_id

try:
    import dagster as dg  # type: ignore[import-not-found, unused-ignore]
except ImportError as exc:  # pragma: no cover - exercised only without the stand-in module
    raise ImportError(
        "dvf.integrations.dagster needs Dagster 1.7 or later: pip install dagster"
    ) from exc

Granularity = Literal["table", "check"]
AssetKeyInput = str | Sequence[str] | Any
AssetKeyFn = Callable[[str, str], AssetKeyInput]

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")
_SIDE_SUFFIXES = (".source", ".target", "_source", "_target")


@dataclass
class _Target:
    """One declared asset check and the results it reports."""

    name: str
    asset_key: Any
    suite: str
    table: str
    group: str
    """``table``, ``schema``, ``comparison``, or ``check``."""
    tail: str | None = None
    """For ``check``: the end of the check ID after ``suite.table.``."""
    description: str = ""
    pattern: bool = False
    """The table is a pattern; its datasets are named after the tables it matched."""
    results: list[CheckResult] = field(default_factory=list)
    datasets: list[DatasetPairResult] = field(default_factory=list)


def check_name(*parts: str) -> str:
    """A valid Dagster name (letters, digits, underscores) from ``parts``."""
    text = "__".join(_NAME_UNSAFE.sub("_", p).strip("_") for p in parts if p)
    return (text or "dvf")[:200]


def _to_asset_key(value: AssetKeyInput) -> Any:
    if isinstance(value, dg.AssetKey):
        return value
    if isinstance(value, str):
        return dg.AssetKey([value])
    return dg.AssetKey(list(value))


def _key_for(asset_key: AssetKeyFn | Mapping[str, AssetKeyInput] | None) -> AssetKeyFn:
    if asset_key is None:
        return lambda suite, table: [table]
    if callable(asset_key):
        return asset_key
    mapping = dict(asset_key)

    def lookup(suite: str, table: str) -> AssetKeyInput:
        for key in (f"{suite}.{table}", table):
            if key in mapping:
                return mapping[key]
        return [table]

    return lookup


def _table_tail(suite: SuiteConfig, table: TableConfig, check: Any) -> str:
    prefix = f"{suite.name}.{table.label}."
    full = make_check_id(
        suite.name, table.label, check.type, columns=check.target_columns(), name=check.name
    )
    return full[len(prefix) :]


def plan_targets(
    loaded: api.LoadedConfig,
    *,
    granularity: Granularity = "table",
    asset_key: AssetKeyFn | Mapping[str, AssetKeyInput] | None = None,
) -> list[_Target]:
    """The asset checks a configuration declares, in configuration order."""
    if granularity not in ("table", "check"):
        raise ConfigError(f"'granularity' must be 'table' or 'check', got '{granularity}'.")
    key_for = _key_for(asset_key)
    targets: list[_Target] = []
    names: set[str] = set()

    def add(target: _Target) -> None:
        base, n = target.name, 2
        while target.name.lower() in names:
            target.name = f"{base}_{n}"
            n += 1
        names.add(target.name.lower())
        targets.append(target)

    for suite, table in loaded.config.iter_tables():
        if not (suite.enabled and table.enabled):
            continue
        key = _to_asset_key(key_for(suite.name, table.label))
        what = f"dvf {suite.name}.{table.label}"
        pattern = table.pattern is not None
        if granularity == "table":
            add(
                _Target(
                    check_name("dvf", suite.name, table.label),
                    key,
                    suite.name,
                    table.label,
                    "table",
                    description=table.description or f"{what}: all checks",
                    pattern=pattern,
                )
            )
            continue
        if table.schema_checks:
            add(
                _Target(
                    check_name("dvf", table.label, "schema"),
                    key,
                    suite.name,
                    table.label,
                    "schema",
                    description=f"{what}: schema checks",
                    pattern=pattern,
                )
            )
        if suite.is_comparison and table.comparison.enabled:
            add(
                _Target(
                    check_name("dvf", table.label, "comparison"),
                    key,
                    suite.name,
                    table.label,
                    "comparison",
                    description=f"{what}: " + ", ".join(table.comparison.strategies),
                    pattern=pattern,
                )
            )
        for check in table.checks:
            if not check.enabled:
                continue
            tail = _table_tail(suite, table, check)
            add(
                _Target(
                    check_name("dvf", table.label, tail),
                    key,
                    suite.name,
                    table.label,
                    "check",
                    tail=tail,
                    description=f"{what}: {tail}",
                    pattern=pattern,
                )
            )
    return targets


def _owner(dataset: DatasetPairResult, suite: str, targets: list[_Target]) -> str | None:
    """The configured table (name or pattern) a dataset result belongs to."""
    named = {t.table for t in targets if t.suite == suite and not t.pattern}
    if dataset.name in named:
        return dataset.name
    patterns = list(dict.fromkeys(t.table for t in targets if t.suite == suite and t.pattern))
    lowered = dataset.name.lower()
    for label in patterns:
        if fnmatchcase(lowered, label.lower()) or fnmatchcase(lowered, bare(label).lower()):
            return label
    # Pattern datasets are named after the matched table, which may not match the glob as is.
    return patterns[0] if len(patterns) == 1 else None


def bare(pattern: str) -> str:
    """A table glob without its schema part (``sales.*`` -> ``*``)."""
    return pattern.rsplit(".", 1)[-1]


def assign_results(result: RunResult, targets: list[_Target]) -> None:
    """Attach each dataset and check result of ``result`` to the targets that report it."""
    for suite in result.suites:
        for dataset in suite.datasets:
            owner = _owner(dataset, suite.name, targets)
            if owner is None:
                continue
            mine = [t for t in targets if t.suite == suite.name and t.table == owner]
            for target in mine:
                target.datasets.append(dataset)
            prefix = f"{suite.name}.{dataset.name}."
            for check in dataset.checks:
                tail = check.check_id[len(prefix) :] if check.check_id.startswith(prefix) else ""
                for target in mine:
                    if _reports(target, check, tail):
                        target.results.append(check)
                        break


def _reports(target: _Target, check: CheckResult, tail: str) -> bool:
    if target.group == "table":
        return True
    if target.group == "schema":
        return check.type.startswith("schema.")
    if target.group == "comparison":
        return check.type.startswith("compare.")
    assert target.tail is not None
    return tail == target.tail or any(tail == target.tail + s for s in _SIDE_SUFFIXES)


def evaluate(
    target: _Target,
    *,
    fail_on: Severity,
    min_score: float | None = None,
    strict: bool = False,
) -> tuple[bool, str, str]:
    """``(passed, severity, description)`` of one asset check; severity is ERROR or WARN."""
    if not target.results:
        return True, "WARN", "no dvf results (the checks were filtered out or disabled)"
    blocking = [
        c
        for c in target.results
        if c.status is Status.ERROR
        or (c.status is Status.FAILED and c.severity.at_least(fail_on))
        or (c.status is Status.WARNING and c.severity.at_least(fail_on))
    ]
    if blocking:
        return False, "ERROR", _describe(blocking)
    score = _score(target.results)
    if min_score is not None and score is not None and score < min_score:
        return False, "ERROR", f"quality score {score:.1f} is below {min_score:g}"
    soft = [
        c
        for c in target.results
        if c.status is Status.FAILED or (strict and c.status is Status.WARNING)
    ]
    if soft:
        return False, "WARN", _describe(soft)
    return True, "WARN", f"{len(target.results)} dvf check(s) passed"


def _describe(checks: list[CheckResult]) -> str:
    shown = "; ".join(f"{c.check_id}: {c.message or c.status.value}" for c in checks[:5])
    more = len(checks) - 5
    return shown + (f" (and {more} more)" if more > 0 else "")


def _score(checks: list[CheckResult]) -> float | None:
    evaluated = [
        c
        for c in checks
        if c.status in (Status.PASSED, Status.WARNING, Status.FAILED, Status.WAIVED)
    ]
    if not evaluated:
        return None
    passed = sum(1 for c in evaluated if c.status in (Status.PASSED, Status.WAIVED))
    return round(100.0 * passed / len(evaluated), 2)


def metadata(target: _Target, results_path: str | None, run_id: str) -> dict[str, Any]:
    """Metadata of one asset check result (plain values; Dagster wraps them)."""
    counts: dict[str, int] = {}
    for check in target.results:
        counts[check.status.value] = counts.get(check.status.value, 0) + 1
    status = "not_run"
    if target.results:
        worst = next(
            (s for s in (Status.ERROR, Status.FAILED, Status.WARNING) if s.value in counts),
            Status.PASSED,
        )
        status = worst.value
    data: dict[str, Any] = {
        "dvf_status": status,
        "dvf_run_id": run_id,
        "dvf_checks": counts,
        "dvf_failed_checks": [
            c.check_id for c in target.results if c.status in (Status.FAILED, Status.ERROR)
        ][:50],
        "dvf_quality_score": _score(target.results),
        "dvf_results_path": results_path,
    }
    if target.group in ("table", "comparison") and target.datasets:
        rows = {"source": 0, "target": 0}
        reconciliation: dict[str, int] = {}
        for dataset in target.datasets:
            rows["source"] += dataset.row_counts.source or 0
            rows["target"] += dataset.row_counts.target or 0
            if dataset.reconciliation is not None:
                for name, value in dataset.reconciliation.model_dump().items():
                    reconciliation[name] = reconciliation.get(name, 0) + int(value)
        data["dvf_row_counts"] = rows
        if reconciliation:
            data["dvf_reconciliation"] = reconciliation
    return data


def build_dvf_asset_checks(
    config: str | os.PathLike[str],
    *,
    asset_key: AssetKeyFn | Mapping[str, AssetKeyInput] | None = None,
    granularity: Granularity = "table",
    name: str = "dvf_checks",
    profile: str | None = None,
    only: str | Sequence[str] | None = None,
    overrides: Sequence[str] = (),
    variables: Mapping[str, str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    formats: Sequence[str] | None = None,
    fail_on: Severity | str = Severity.ERROR,
    min_score: float | None = None,
    strict: bool = False,
    blocking: bool = False,
    history: bool = True,
) -> Any:
    """A Dagster multi-asset check that runs ``config`` and reports per table or per check.

    ``asset_key`` maps ``(suite, table)`` to the asset a check belongs to: a function, or a
    mapping keyed by ``"suite.table"`` or ``"table"`` (default: an asset key named after the
    table). ``blocking`` makes failing ``ERROR`` checks block downstream assets.
    """
    severity = parse_severity(fail_on)
    loaded = api.load_config(
        config,
        profile=profile,
        overrides=list(overrides),
        variables=variables,
        strict=False,
        resolve_secrets=False,
    )
    targets = plan_targets(loaded, granularity=granularity, asset_key=asset_key)
    if not targets:
        raise ConfigError(f"{config} has no enabled tables to build asset checks from.")
    specs = [
        dg.AssetCheckSpec(t.name, asset=t.asset_key, description=t.description, blocking=blocking)
        for t in targets
    ]

    def _dvf_checks(context: Any) -> Iterator[Any]:
        output, summary = run_validation(
            config,
            profile=profile,
            only=only,
            overrides=overrides,
            variables=variables,
            output_dir=output_dir,
            formats=formats,
            history=history,
            fail_on=severity,
            min_score=min_score,
            strict=strict,
        )
        log = getattr(context, "log", None)
        if log is not None:
            log.info(summary.message())
        fresh = [
            _Target(
                t.name, t.asset_key, t.suite, t.table, t.group, t.tail, t.description, t.pattern
            )
            for t in targets
        ]
        assign_results(output.result, fresh)
        for target in fresh:
            passed, level, description = evaluate(
                target, fail_on=severity, min_score=min_score, strict=strict
            )
            yield dg.AssetCheckResult(
                asset_key=target.asset_key,
                check_name=target.name,
                passed=passed,
                severity=getattr(dg.AssetCheckSeverity, level),
                metadata=metadata(target, summary.results_path, summary.run_id),
                description=description,
            )

    # Dagster accepts an unannotated `context` or one annotated with its own context class;
    # the annotation above is for type checkers only.
    _dvf_checks.__annotations__ = {}
    return dg.multi_asset_check(specs=specs, name=name)(_dvf_checks)


__all__ = ["assign_results", "build_dvf_asset_checks", "check_name", "evaluate", "plan_targets"]
