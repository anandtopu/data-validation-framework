"""dvf.integrations.common: run_validation, the run summary, and the quality gates.

The helpers at the top (synthetic results, a recording fake of ``dvf.api.run``, the seeded
example project, and stand-in modules) are shared by the other ``test_integrations_*`` files.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import dvf.integrations
from dvf import api
from dvf.core.enums import Dimension, Severity, Status
from dvf.core.errors import ConfigError
from dvf.execution.runner import RunOutput
from dvf.integrations import common
from dvf.integrations.common import (
    DvfRunSummary,
    QualityGateError,
    enforce_gate,
    parse_severity,
    problem_rows,
    run_validation,
    split_selectors,
    summarize,
)
from dvf.models.results import (
    ArtifactRef,
    CheckResult,
    DatasetPairResult,
    ReconciliationSummary,
    RowCounts,
    RunEnvironment,
    RunResult,
    SuiteResult,
    aggregate_status,
)

# ---------------------------------------------------------------------- shared helpers


def check(
    check_id: str,
    status: Status,
    severity: Severity = Severity.ERROR,
    *,
    check_type: str | None = None,
    message: str | None = None,
) -> CheckResult:
    """A synthetic check result; the type defaults to the ID's part after ``suite.table.``."""
    parts = check_id.split(".")
    return CheckResult(
        check_id=check_id,
        name=parts[-1],
        type=check_type
        or ".".join(parts[2:4] if parts[2] in ("compare", "schema") else parts[2:3]),
        dimension=Dimension.ACCURACY,
        severity=severity,
        status=status,
        message=message or f"{parts[-1]} {status.value}",
    )


def dataset(name: str, checks: list[CheckResult], **extra: Any) -> DatasetPairResult:
    return DatasetPairResult(
        name=name, status=aggregate_status(c.status for c in checks), checks=checks, **extra
    )


def make_result(suites: dict[str, list[DatasetPairResult]], *, run_id: str = "R1") -> RunResult:
    result = RunResult(
        run_id=run_id,
        project="demo",
        status=Status.PASSED,
        started_at=datetime(2026, 11, 8, tzinfo=UTC),
        duration_ms=1234.0,
        environment=RunEnvironment(dvf_version="1.0.0", python_version="3.12", platform="test"),
        suites=[
            SuiteResult(
                name=name,
                status=aggregate_status(d.status for d in datasets),
                datasets=datasets,
            )
            for name, datasets in suites.items()
        ],
    )
    result.status = aggregate_status(s.status for s in result.suites)
    result.summary = result.summarize()
    return result


def sample_result() -> RunResult:
    """Two tables: customers fails a comparison and a warning-level check; orders passes."""
    customers = dataset(
        "customers",
        [
            check("sales.customers.schema.columns_present", Status.PASSED),
            check("sales.customers.compare.row_count", Status.FAILED, message="200 vs 201 rows"),
            check("sales.customers.compare.aggregate", Status.PASSED),
            check("sales.customers.not_null.customer_id", Status.PASSED),
            check("sales.customers.regex.email", Status.WARNING, Severity.WARNING),
        ],
        row_counts=RowCounts(source=200, target=201),
        reconciliation=ReconciliationSummary(matched=197, missing_in_target=3),
    )
    orders = dataset(
        "orders",
        [
            check("sales.orders.compare.row_count", Status.PASSED),
            check("sales.orders.unique.order_id", Status.PASSED),
        ],
        row_counts=RowCounts(source=10, target=10),
    )
    return make_result({"sales": [customers, orders]})


@dataclass
class FakeRun:
    """Records the calls to ``dvf.api.run`` and returns a prepared result."""

    result: RunResult
    output_dir: Path | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, config: Any, **kwargs: Any) -> RunOutput:
        self.calls.append({"config": config, **kwargs})
        artifacts: list[ArtifactRef] = []
        out = Path(kwargs["output_dir"]) if kwargs.get("output_dir") else self.output_dir
        if out is not None and kwargs.get("write_reports", True):
            out.mkdir(parents=True, exist_ok=True)
            (out / "results.json").write_text(self.result.model_dump_json(), encoding="utf-8")
            (out / "summary.md").write_text(f"# dvf run {self.result.run_id}\n", encoding="utf-8")
            artifacts = [
                ArtifactRef(kind="report", path="results.json", format="json"),
                ArtifactRef(kind="report", path=str(out / "summary.md"), format="markdown"),
            ]
        return RunOutput(result=self.result, output_dir=out, artifacts=artifacts)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRun:
    fake = FakeRun(sample_result(), output_dir=tmp_path / "reports")
    monkeypatch.setattr(api, "run", fake)
    return fake


@pytest.fixture(scope="module")
def example_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The seeded example (``dvf init --example``): its run fails the default gate."""
    from dvf.scaffold.example import write_example

    folder = tmp_path_factory.mktemp("example")
    write_example(folder)
    return folder / "dvf.yaml"


StubInstaller = Callable[..., types.ModuleType]


@pytest.fixture
def import_with_stubs(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubInstaller]:
    """Import a ``dvf.integrations`` module with stand-in modules in ``sys.modules``."""
    imported: list[str] = []

    def install(name: str, stubs: dict[str, types.ModuleType]) -> types.ModuleType:
        for module_name, module in stubs.items():
            monkeypatch.setitem(sys.modules, module_name, module)
        sys.modules.pop(name, None)
        imported.append(name)
        return importlib.import_module(name)

    yield install
    for name in imported:
        sys.modules.pop(name, None)
        parent, _, child = name.rpartition(".")
        if parent in sys.modules and hasattr(sys.modules[parent], child):
            delattr(sys.modules[parent], child)


def module(name: str, **attributes: Any) -> types.ModuleType:
    """A stand-in module with ``attributes``."""
    stub = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(stub, key, value)
    return stub


# ---------------------------------------------------------------------- tests


def test_package_exposes_the_common_helpers_lazily() -> None:
    assert dvf.integrations.run_validation is run_validation
    assert dvf.integrations.DvfRunSummary is DvfRunSummary
    with pytest.raises(AttributeError, match="no attribute"):
        _ = dvf.integrations.nothing  # type: ignore[attr-defined]


def test_split_selectors_accepts_strings_and_lists() -> None:
    assert split_selectors(None) == []
    assert split_selectors("tag:pr, table:orders\nsuite:x") == ["tag:pr", "table:orders", "suite:x"]
    assert split_selectors(["tag:pr", "table:a table:b"]) == ["tag:pr", "table:a", "table:b"]
    assert split_selectors("") == []


def test_parse_severity_rejects_unknown_values() -> None:
    assert parse_severity("WARNING") is Severity.WARNING
    assert parse_severity(Severity.CRITICAL) is Severity.CRITICAL
    with pytest.raises(ConfigError, match="info, warning, error, critical"):
        parse_severity("fatal")


def test_summarize_applies_the_gates_like_dvf_run() -> None:
    result = sample_result()
    failed = summarize(result)
    assert failed.exit_code == 1 and not failed.passed and failed.outcome == "validation_failed"
    assert failed.failed_checks == ("sales.customers.compare.row_count",)
    assert failed.warning_checks == ("sales.customers.regex.email",)
    assert failed.quality_score == result.summary.quality_score
    assert failed.checks_by_status == {"passed": 5, "failed": 1, "warning": 1}
    assert failed.report_dir is None and failed.results_path is None
    lenient = summarize(result, fail_on="critical")
    assert lenient.exit_code == 0 and lenient.passed
    assert summarize(result, fail_on="critical", strict=True).exit_code == 1
    assert summarize(result, fail_on="critical", min_score=99).exit_code == 1
    for gate in (summarize(result, fail_on="critical"), failed):
        assert gate.exit_code == api.exit_code(result, fail_on=gate.fail_on)


def test_errored_checks_mean_exit_code_2() -> None:
    result = make_result(
        {"s": [dataset("t", [check("s.t.not_null.a", Status.ERROR, message="no such column")])]}
    )
    summary = summarize(result, fail_on="critical")
    assert summary.exit_code == 2 and summary.outcome == "execution_error"
    assert summary.error_checks == ("s.t.not_null.a",)
    assert "execution errors" in summary.message()


def test_summary_message_dict_and_markdown(tmp_path: Path) -> None:
    summary = summarize(sample_result(), min_score=90, strict=True)
    message = summary.message()
    assert message.startswith("dvf run R1 (demo): FAILED, 5 passed, 1 warning, 1 failed")
    assert "--fail-on error --min-score 90 --strict" in message
    assert "sales.customers.compare.row_count" in message
    data = summary.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["passed"] is False and data["outcome"] == "validation_failed"
    text = summary.markdown()
    assert "## dvf run `R1`" in text and "`sales.customers.compare.row_count`" in text
    passing = summarize(sample_result(), fail_on="critical")
    assert passing.message().endswith("quality gates passed")
    empty = summarize(make_result({}))
    assert "no checks" in empty.message() and empty.quality_score is None


def test_message_lists_at_most_five_failures() -> None:
    checks = [check(f"s.t.not_null.c{i}", Status.FAILED) for i in range(7)]
    summary = summarize(make_result({"s": [dataset("t", checks)]}))
    assert summary.message().endswith("and 2 more")


def test_run_validation_calls_dvf_api_run(fake_run: FakeRun, tmp_path: Path) -> None:
    output, summary = run_validation(
        "dvf.yaml",
        profile="ci",
        only="tag:pr, table:orders",
        overrides=["defaults.sample_size=5"],
        variables={"env": "qa"},
        output_dir=tmp_path / "out",
        formats=["junit"],
        run_id="R1",
        history=False,
        baseline="last_run",
        parallel=2,
        plugins=["plugins/"],
        fail_on="warning",
    )
    call = fake_run.calls[0]
    assert call["config"] == "dvf.yaml"
    assert call["only"] == ["tag:pr", "table:orders"]
    assert call["profile"] == "ci" and call["overrides"] == ["defaults.sample_size=5"]
    assert call["variables"] == {"env": "qa"} and call["formats"] == ["junit"]
    assert call["history"] is False and call["baseline"] == "last_run" and call["parallel"] == 2
    assert call["plugins"] == ["plugins/"] and call["run_id"] == "R1"
    assert output.result is fake_run.result
    assert summary.fail_on == "warning" and summary.exit_code == 1
    assert summary.results_path == str(tmp_path / "out" / "results.json")
    assert summary.report_dir == str(tmp_path / "out")
    assert summary.reports == (
        str(tmp_path / "out" / "results.json"),
        str(tmp_path / "out" / "summary.md"),
    )
    assert summary.markdown() == "# dvf run R1\n"


def test_run_validation_rejects_a_bad_gate_before_running(fake_run: FakeRun) -> None:
    with pytest.raises(ConfigError):
        run_validation("dvf.yaml", fail_on="nope")
    assert fake_run.calls == []


def test_enforce_gate_raises_with_the_summary() -> None:
    failed = summarize(sample_result())
    with pytest.raises(QualityGateError) as info:
        enforce_gate(failed)
    assert info.value.summary is failed and info.value.exit_code == 1
    passing = summarize(sample_result(), fail_on="critical")
    assert enforce_gate(passing) is passing


def test_problem_rows_are_sorted_and_truncated() -> None:
    long = "x" * 400
    result = make_result(
        {
            "s": [
                dataset(
                    "t",
                    [
                        check("s.t.regex.a", Status.WARNING, Severity.WARNING),
                        check("s.t.not_null.b", Status.FAILED, message=long),
                        check("s.t.unique.c", Status.ERROR),
                        check("s.t.unique.d", Status.PASSED),
                    ],
                )
            ]
        }
    )
    rows = problem_rows(result)
    assert [r["status"] for r in rows] == ["error", "failed", "warning"]
    assert rows[1]["message"].endswith("...") and len(rows[1]["message"]) == 300
    assert problem_rows(result, limit=1) == rows[:1]


def test_run_validation_end_to_end_on_the_seeded_example(
    example_project: Path, tmp_path: Path
) -> None:
    output, summary = run_validation(
        example_project, only="tag:pr", output_dir=tmp_path / "out", history=False
    )
    assert summary.exit_code == 1 and summary.status == "failed"
    assert summary.results_path is not None and Path(summary.results_path).is_file()
    assert "customers.customers.compare.row_count" in summary.failed_checks
    assert summary.quality_score == output.result.summary.quality_score
    assert "| " in summary.markdown()  # the real summary.md, with tables
    assert common.EXIT_OUTCOMES[summary.exit_code] == "validation_failed"
