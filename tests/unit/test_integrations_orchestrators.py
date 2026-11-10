"""Airflow, Dagster, and Prefect integrations, with stand-in modules for the orchestrators.

None of the orchestrators is installed in the test environment. Each test installs a small
stand-in module that mimics the orchestrator's API surface used by dvf, then checks that the
integration calls dvf.api.run correctly, maps the results, and honors the quality gates.
"""

from __future__ import annotations

import inspect
import logging
import runpy
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dvf.core.enums import Severity, Status
from dvf.core.errors import ConfigError
from dvf.integrations.common import QualityGateError
from tests.conftest import EXAMPLES
from tests.unit.test_integrations_common import (
    FakeRun,
    StubInstaller,
    check,
    dataset,
    example_project,
    fake_run,
    import_with_stubs,
    make_result,
    module,
)

__all__ = ["example_project", "fake_run", "import_with_stubs"]  # fixtures used here

# ---------------------------------------------------------------------- Airflow stand-in


class AirflowException(Exception):  # noqa: N818 - the name Airflow uses
    pass


class AirflowFailException(AirflowException):
    pass


class BaseOperator:
    """The parts of Airflow's BaseOperator that DvfOperator and the example DAG use."""

    def __init__(self, *, task_id: str, **kwargs: Any) -> None:
        self.task_id = task_id
        self.kwargs = kwargs
        self.log = logging.getLogger(f"airflow.task.{task_id}")
        self.downstream: list[BaseOperator] = []
        if DAG.current is not None:
            DAG.current.tasks.append(self)

    def __rshift__(self, other: Any) -> Any:
        self.downstream.extend(other if isinstance(other, list) else [other])
        return other


class DAG:
    current: DAG | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.tasks: list[BaseOperator] = []

    def __enter__(self) -> DAG:
        DAG.current = self
        return self

    def __exit__(self, *exc: object) -> None:
        DAG.current = None


class TaskInstance:
    def __init__(self) -> None:
        self.xcoms: dict[str, Any] = {}

    def xcom_push(self, key: str, value: Any) -> None:
        self.xcoms[key] = value


def airflow_stubs(version: int = 2) -> dict[str, types.ModuleType]:
    exceptions = module(
        "airflow.exceptions",
        AirflowException=AirflowException,
        AirflowFailException=AirflowFailException,
    )
    stubs = {"airflow": module("airflow", DAG=DAG), "airflow.exceptions": exceptions}
    if version == 3:
        stubs["airflow"].__path__ = []  # type: ignore[attr-defined]  # a package
        stubs["airflow.sdk"] = module("airflow.sdk", BaseOperator=BaseOperator, DAG=DAG)
    else:
        stubs["airflow.models"] = module("airflow.models", BaseOperator=BaseOperator)
    return stubs


def test_airflow_operator_runs_pushes_xcoms_and_fails_by_gate(
    import_with_stubs: StubInstaller, fake_run: FakeRun, tmp_path: Path
) -> None:
    airflow = import_with_stubs("dvf.integrations.airflow", airflow_stubs())
    operator = airflow.DvfOperator(
        task_id="validate",
        config="dvf.yaml",
        only="tag:pr",
        overrides=["defaults.sample_size=5"],
        variables={"env": "qa"},
        output_dir=str(tmp_path / "out"),
        dvf_run_id="R1",
        retries=3,
    )
    assert operator.kwargs == {"retries": 3}
    assert "config" in operator.template_fields and operator.template_ext == ()
    ti = TaskInstance()
    with pytest.raises(AirflowFailException, match="quality gates failed"):
        operator.execute({"ti": ti})
    call = fake_run.calls[0]
    assert call["config"] == "dvf.yaml" and call["only"] == ["tag:pr"]
    assert call["overrides"] == ["defaults.sample_size=5"] and call["variables"] == {"env": "qa"}
    assert call["run_id"] == "R1" and call["output_dir"] == str(tmp_path / "out")
    assert ti.xcoms["dvf_exit_code"] == 1
    assert ti.xcoms["dvf_quality_score"] == fake_run.result.summary.quality_score
    assert ti.xcoms["dvf_results_path"] == str(tmp_path / "out" / "results.json")
    assert set(ti.xcoms) == {f"dvf_{k}" for k in airflow.XCOM_KEYS}


def test_airflow_operator_returns_the_summary_when_gates_pass(
    import_with_stubs: StubInstaller, fake_run: FakeRun
) -> None:
    airflow = import_with_stubs("dvf.integrations.airflow", airflow_stubs(version=3))
    assert airflow.BaseOperator is BaseOperator
    lenient = airflow.DvfOperator(task_id="v", config="dvf.yaml", fail_on="critical")
    summary = lenient.execute({"ti": TaskInstance()})
    assert summary["passed"] is True and summary["exit_code"] == 0
    report_only = airflow.DvfOperator(task_id="r", config="dvf.yaml", fail_task=False)
    assert report_only.execute({})["passed"] is False  # no task instance: no XComs, no error


def test_airflow_execution_errors_stay_retryable(
    import_with_stubs: StubInstaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    airflow = import_with_stubs("dvf.integrations.airflow", airflow_stubs())
    result = make_result({"s": [dataset("t", [check("s.t.not_null.a", Status.ERROR)])]})
    monkeypatch.setattr("dvf.api.run", FakeRun(result))
    operator = airflow.DvfOperator(task_id="v", config="dvf.yaml")
    with pytest.raises(AirflowException) as info:
        operator.execute({"ti": TaskInstance()})
    assert not isinstance(info.value, AirflowFailException)


def test_airflow_operator_rejects_a_bad_gate_when_the_dag_is_parsed(
    import_with_stubs: StubInstaller,
) -> None:
    airflow = import_with_stubs("dvf.integrations.airflow", airflow_stubs())
    with pytest.raises(ConfigError):
        airflow.DvfOperator(task_id="v", config="dvf.yaml", fail_on="sometimes")


def test_airflow_module_needs_airflow(import_with_stubs: StubInstaller) -> None:
    with pytest.raises(ImportError, match="pip install apache-airflow"):
        import_with_stubs("dvf.integrations.airflow", {"airflow": module("airflow")})


def test_airflow_operator_end_to_end(
    import_with_stubs: StubInstaller, example_project: Path, tmp_path: Path
) -> None:
    airflow = import_with_stubs("dvf.integrations.airflow", airflow_stubs())
    operator = airflow.DvfOperator(
        task_id="v",
        config=str(example_project),
        only=["check:row_count_range"],
        output_dir=str(tmp_path / "out"),
        history=False,
    )
    ti = TaskInstance()
    summary = operator.execute({"ti": ti})
    assert summary["passed"] is True and summary["quality_score"] == 100.0
    assert Path(ti.xcoms["dvf_results_path"]).is_file()


def test_airflow_example_dag_builds(
    import_with_stubs: StubInstaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs = airflow_stubs()
    stubs["airflow.operators"] = module("airflow.operators")
    stubs["airflow.operators.empty"] = module("airflow.operators.empty", EmptyOperator=BaseOperator)
    stubs["airflow.operators.python"] = module(
        "airflow.operators.python", BranchPythonOperator=BaseOperator
    )
    import_with_stubs("dvf.integrations.airflow", stubs)
    monkeypatch.setenv("DVF_CONFIG", "dvf.yaml")
    namespace = runpy.run_path(str(EXAMPLES / "orchestrators" / "airflow" / "dvf_dag.py"))
    dag = namespace["dag"]
    ids = [t.task_id for t in dag.tasks]
    assert ids == ["validate_pr_checks", "validate_nightly", "decide", "publish", "open_incident"]
    nightly = dag.tasks[1]
    assert nightly.only == "tag:nightly" and nightly.min_score == 95 and not nightly.fail_task
    ti = types.SimpleNamespace(xcom_pull=lambda task_ids: {"passed": True})
    assert namespace["choose"](ti) == "publish"


# ---------------------------------------------------------------------- Dagster stand-in


class AssetKey:
    def __init__(self, path: Any) -> None:
        self.path = [path] if isinstance(path, str) else list(path)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AssetKey) and other.path == self.path

    def __hash__(self) -> int:
        return hash(tuple(self.path))


class Recorded:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def multi_asset_check(*, specs: list[Recorded], name: str) -> Callable[[Any], Any]:
    def decorate(fn: Callable[..., Any]) -> Any:
        # Dagster accepts an unannotated context (or its own context classes) only.
        context = inspect.signature(fn).parameters["context"]
        assert context.annotation is inspect.Parameter.empty
        return types.SimpleNamespace(specs=specs, name=name, fn=fn)

    return decorate


def dagster_stub() -> dict[str, types.ModuleType]:
    return {
        "dagster": module(
            "dagster",
            AssetKey=AssetKey,
            AssetCheckSpec=Recorded,
            AssetCheckResult=Recorded,
            AssetCheckSeverity=types.SimpleNamespace(ERROR="ERROR", WARN="WARN"),
            multi_asset_check=multi_asset_check,
            asset=lambda fn: fn,
            Definitions=Recorded,
        )
    }


def run_checks(definition: Any) -> dict[str, Recorded]:
    context = types.SimpleNamespace(log=logging.getLogger("dagster"))
    return {r.kwargs["check_name"]: r for r in definition.fn(context)}


def test_dagster_table_checks(
    import_with_stubs: StubInstaller, example_project: Path, fake_run: FakeRun
) -> None:
    dagster = import_with_stubs("dvf.integrations.dagster", dagster_stub())
    customers = dataset(
        "customers",
        [
            check("customers.customers.compare.row_count", Status.FAILED),
            check("customers.customers.regex.email", Status.FAILED, Severity.WARNING),
        ],
    )
    orders = dataset("orders", [check("customers.orders.unique.order_id", Status.PASSED)])
    fake_run.result = make_result({"customers": [customers, orders]})
    definition = dagster.build_dvf_asset_checks(
        example_project, asset_key={"orders": ["sales", "orders"]}, only="tag:pr", blocking=True
    )
    assert definition.name == "dvf_checks"
    specs = {s.args[0]: s.kwargs for s in definition.specs}
    assert set(specs) == {"dvf__customers__customers", "dvf__customers__orders"}
    assert specs["dvf__customers__orders"]["asset"] == AssetKey(["sales", "orders"])
    assert specs["dvf__customers__customers"]["blocking"] is True
    results = run_checks(definition)
    assert fake_run.calls[0]["only"] == ["tag:pr"]
    failed = results["dvf__customers__customers"].kwargs
    assert failed["passed"] is False and failed["severity"] == "ERROR"
    assert failed["asset_key"] == AssetKey(["customers"])
    assert failed["metadata"]["dvf_status"] == "failed"
    assert failed["metadata"]["dvf_failed_checks"] == [
        "customers.customers.compare.row_count",
        "customers.customers.regex.email",
    ]
    assert "dvf_row_counts" in failed["metadata"]
    passed = results["dvf__customers__orders"].kwargs
    assert passed["passed"] is True and passed["metadata"]["dvf_quality_score"] == 100.0


def test_dagster_check_granularity_and_gates(
    import_with_stubs: StubInstaller, example_project: Path, fake_run: FakeRun
) -> None:
    dagster = import_with_stubs("dvf.integrations.dagster", dagster_stub())
    customers = dataset(
        "customers",
        [
            check("customers.customers.schema.columns_present", Status.PASSED),
            check("customers.customers.compare.row_count", Status.PASSED),
            check("customers.customers.not_null.customer_id+email", Status.PASSED),
            check("customers.customers.unique.customer_id", Status.FAILED, Severity.WARNING),
            check("customers.customers.regex.email", Status.WARNING, Severity.WARNING),
        ],
    )
    fake_run.result = make_result({"customers": [customers]})
    definition = dagster.build_dvf_asset_checks(
        example_project, granularity="check", fail_on="error", strict=True
    )
    names = [s.args[0] for s in definition.specs]
    assert "dvf__customers__schema" in names and "dvf__customers__comparison" in names
    assert "dvf__customers__not_null_customer_id_email" in names
    assert "dvf__orders__expression_shipped_after_ordered" in names
    results = run_checks(definition)
    assert set(results) == set(names)
    assert results["dvf__customers__schema"].kwargs["passed"] is True
    soft = results["dvf__customers__unique_customer_id"].kwargs
    assert soft["passed"] is False and soft["severity"] == "WARN"
    strict = results["dvf__customers__regex_email"].kwargs
    assert strict["passed"] is False and strict["severity"] == "WARN"
    not_run = results["dvf__orders__comparison"].kwargs
    assert not_run["passed"] is True and not_run["metadata"]["dvf_status"] == "not_run"


def test_dagster_min_score_and_errors(
    import_with_stubs: StubInstaller, example_project: Path, fake_run: FakeRun
) -> None:
    dagster = import_with_stubs("dvf.integrations.dagster", dagster_stub())
    customers = dataset(
        "customers",
        [
            check("customers.customers.compare.row_count", Status.PASSED),
            check("customers.customers.regex.email", Status.FAILED, Severity.INFO),
        ],
    )
    orders = dataset("orders", [check("customers.orders.unique.order_id", Status.ERROR)])
    fake_run.result = make_result({"customers": [customers, orders]})
    definition = dagster.build_dvf_asset_checks(example_project, min_score=90)
    results = run_checks(definition)
    low = results["dvf__customers__customers"].kwargs
    assert low["passed"] is False and low["severity"] == "ERROR"
    assert "quality score 50.0 is below 90" in low["description"]
    errored = results["dvf__customers__orders"].kwargs
    assert errored["passed"] is False and errored["severity"] == "ERROR"


def test_dagster_pattern_tables_and_bad_options(
    import_with_stubs: StubInstaller, write: Callable[..., Path], fake_run: FakeRun
) -> None:
    dagster = import_with_stubs("dvf.integrations.dagster", dagster_stub())
    config = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: p}
        connections:
          a: {type: duckdb}
          b: {type: duckdb}
        suites:
          - name: s
            source: a
            target: b
            tables:
              - pattern: {source: "sales.*"}
              - name: fixed
                source: {table: x}
                target: {table: x}
              - name: off
                enabled: false
                source: {table: y}
                target: {table: y}
        """,
    )
    fake_run.result = make_result(
        {
            "s": [
                dataset("orders", [check("s.orders.compare.row_count", Status.PASSED)]),
                dataset("fixed", [check("s.fixed.compare.row_count", Status.FAILED)]),
            ]
        }
    )
    definition = dagster.build_dvf_asset_checks(config, asset_key=lambda suite, table: table)
    results = run_checks(definition)
    assert set(results) == {"dvf__s__sales", "dvf__s__fixed"}
    assert results["dvf__s__sales"].kwargs["passed"] is True
    assert results["dvf__s__sales"].kwargs["metadata"]["dvf_checks"] == {"passed": 1}
    assert results["dvf__s__fixed"].kwargs["passed"] is False
    with pytest.raises(ConfigError, match="granularity"):
        dagster.build_dvf_asset_checks(config, granularity="column")
    empty = write(
        "empty.yaml",
        """
        version: 1
        project: {name: p}
        connections: {a: {type: duckdb}}
        suites: [{name: s, source: a, tables: []}]
        """,
    )
    with pytest.raises(ConfigError, match="no enabled tables"):
        dagster.build_dvf_asset_checks(empty)
    assert dagster.check_name("dvf", "a.b", "c d") == "dvf__a_b__c_d"


def test_dagster_example_definitions_load(
    import_with_stubs: StubInstaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_with_stubs("dvf.integrations.dagster", dagster_stub())
    monkeypatch.delenv("DVF_CONFIG", raising=False)
    namespace = runpy.run_path(str(EXAMPLES / "orchestrators" / "dagster" / "definitions.py"))
    checks = namespace["dvf_checks"]
    assert [s.args[0] for s in checks.specs] == [
        "dvf__fast__customers",
        "dvf__fast__orders",
        "dvf__full__customers",
        "dvf__full__orders",
    ]
    assert namespace["defs"].kwargs["asset_checks"] == [checks]


# ---------------------------------------------------------------------- Prefect stand-in


class Artifacts:
    def __init__(self) -> None:
        self.markdown: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []

    def create_markdown_artifact(self, **kwargs: Any) -> None:
        self.markdown.append(kwargs)

    def create_table_artifact(self, **kwargs: Any) -> None:
        self.tables.append(kwargs)


def decorator(kind: str) -> Callable[..., Any]:
    def make(*args: Any, **options: Any) -> Any:
        def wrap(fn: Callable[..., Any]) -> Any:
            return _Callable(fn, options, kind)

        if args and callable(args[0]):
            return wrap(args[0])
        return wrap

    return make


class _Callable:
    """A decorated task or flow: callable, with the decorator's options."""

    def __init__(self, fn: Callable[..., Any], options: dict[str, Any], kind: str) -> None:
        self.fn, self.options, self.kind = fn, options, kind

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)


def prefect_stubs(artifacts: Artifacts) -> dict[str, types.ModuleType]:
    artifacts_module = module(
        "prefect.artifacts",
        create_markdown_artifact=artifacts.create_markdown_artifact,
        create_table_artifact=artifacts.create_table_artifact,
    )

    def get_run_logger() -> logging.Logger:
        raise RuntimeError("no active run")  # dvf falls back to its own logger

    prefect = module(
        "prefect",
        task=decorator("task"),
        flow=decorator("flow"),
        get_run_logger=get_run_logger,
        artifacts=artifacts_module,
    )
    return {"prefect": prefect, "prefect.artifacts": artifacts_module}


def test_prefect_task_publishes_artifacts_and_fails_by_gate(
    import_with_stubs: StubInstaller, fake_run: FakeRun
) -> None:
    artifacts = Artifacts()
    prefect = import_with_stubs("dvf.integrations.prefect", prefect_stubs(artifacts))
    assert prefect.dvf_validate.kind == "task"
    assert prefect.dvf_validate.options["name"] == "dvf-validate"
    with pytest.raises(QualityGateError) as info:
        prefect.dvf_validate("dvf.yaml", only=["tag:nightly"], min_score=50)
    assert info.value.summary.exit_code == 1
    assert fake_run.calls[0]["only"] == ["tag:nightly"]
    assert artifacts.markdown[0]["key"] == "dvf-demo-summary"
    assert artifacts.markdown[0]["markdown"] == "# dvf run R1\n"
    table = artifacts.tables[0]
    assert table["key"] == "dvf-demo-problems"
    assert [row["status"] for row in table["table"]] == ["failed", "warning"]


def test_prefect_task_can_only_report(import_with_stubs: StubInstaller, fake_run: FakeRun) -> None:
    artifacts = Artifacts()
    prefect = import_with_stubs("dvf.integrations.prefect", prefect_stubs(artifacts))
    summary = prefect.dvf_validate(
        "dvf.yaml", raise_on_failure=False, artifact_prefix="Orders Migration!"
    )
    assert summary["passed"] is False
    assert artifacts.markdown[0]["key"] == "orders-migration-summary"
    quiet = prefect.validate("dvf.yaml", fail_on="critical", create_artifacts=False)
    assert quiet["passed"] is True and len(artifacts.markdown) == 1
    flow_summary = prefect.dvf_validation_flow("dvf.yaml", only="tag:pr", fail_on="critical")
    assert prefect.dvf_validation_flow.kind == "flow" and flow_summary["exit_code"] == 0
    assert fake_run.calls[-1]["only"] == ["tag:pr"]
    assert prefect.artifact_key("***") == "dvf"


def test_prefect_example_flow_runs(
    import_with_stubs: StubInstaller, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = Artifacts()
    import_with_stubs("dvf.integrations.prefect", prefect_stubs(artifacts))
    monkeypatch.setenv("DVF_CONFIG", "dvf.yaml")
    namespace = runpy.run_path(str(EXAMPLES / "orchestrators" / "prefect" / "dvf_flow.py"))
    with pytest.raises(QualityGateError):
        namespace["orders_migration"]("nightly", min_score=95)
    assert fake_run.calls[0]["config"] == "dvf.yaml"
    assert fake_run.calls[0]["only"] == ["tag:nightly"]
    assert len(artifacts.markdown) == 1
