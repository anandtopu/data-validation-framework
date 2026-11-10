"""Apache Airflow: :class:`DvfOperator` runs a dvf configuration as a task.

.. code-block:: python

    from dvf.integrations.airflow import DvfOperator

    validate = DvfOperator(
        task_id="validate_orders",
        config="/opt/airflow/dags/dvf/dvf.yaml",
        only=["tag:pr"],
        fail_on="error",
        min_score=95,
    )

The operator runs :func:`dvf.api.run` in the worker, returns the run summary (pushed as the
``return_value`` XCom), and pushes ``dvf_quality_score``, ``dvf_exit_code``, ``dvf_run_id``,
``dvf_status``, and ``dvf_results_path`` as separate XComs. When the run fails its quality gates
the task fails without retries (``AirflowFailException``); execution errors raise
``AirflowException``, so the task's ``retries`` apply to them.

Importing this module needs ``apache-airflow`` (2.7 or later, or 3.x); ``import dvf`` does not.
It is verified by unit tests with a stand-in ``airflow`` module, not on a running Airflow.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any

from dvf.integrations.common import DvfRunSummary, parse_severity, run_validation


def _import(*candidates: str) -> Any:
    """The first importable ``module:attribute`` of ``candidates``."""
    missing: ModuleNotFoundError | None = None
    for candidate in candidates:
        module_name, _, attribute = candidate.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc
            continue
        if hasattr(module, attribute):
            return getattr(module, attribute)
    raise ImportError(
        "dvf.integrations.airflow needs Apache Airflow 2.7 or later: pip install apache-airflow"
    ) from missing


# Airflow 3 exposes the task SDK in airflow.sdk; Airflow 2 in airflow.models.
BaseOperator: Any = _import("airflow.sdk:BaseOperator", "airflow.models:BaseOperator")
AirflowException: Any = _import(
    "airflow.sdk.exceptions:AirflowException", "airflow.exceptions:AirflowException"
)
AirflowFailException: Any = _import(
    "airflow.sdk.exceptions:AirflowFailException", "airflow.exceptions:AirflowFailException"
)

XCOM_KEYS = ("run_id", "status", "exit_code", "quality_score", "results_path")
"""Summary fields also pushed as separate XComs, as ``dvf_<field>``."""


class DvfOperator(BaseOperator):  # type: ignore[misc]
    """Run a dvf configuration; push the summary to XCom; fail the task by the quality gates.

    :param config: Configuration file (templated).
    :param profile: Environment profile (templated).
    :param only: Selectors such as ``tag:pr`` or ``table:orders``, as a list or one string
        separated by commas or spaces (templated).
    :param overrides: ``PATH=VALUE`` overrides, like ``--set`` (templated).
    :param variables: ``${var:NAME}`` values (templated).
    :param output_dir: Report folder; default ``reports.output_dir`` of the configuration
        (templated).
    :param formats: Report formats besides JSON; default ``reports.formats``.
    :param dvf_run_id: The dvf run ID; default a new one (templated).
    :param fail_on: Fail when a check at or above this severity fails (default ``error``).
    :param min_score: Fail when the quality score is below this.
    :param strict: Warnings also fail the task.
    :param fail_task: Set to False to always succeed and only push the summary.
    :param history: Read and record run history (baselines, trends).
    """

    template_fields: Sequence[str] = (
        "config",
        "profile",
        "only",
        "overrides",
        "variables",
        "output_dir",
        "dvf_run_id",
    )
    # Configuration files are passed to dvf as paths, never rendered as Jinja templates.
    template_ext: Sequence[str] = ()
    ui_color = "#e8f1fb"

    def __init__(
        self,
        *,
        config: str,
        profile: str | None = None,
        only: str | Sequence[str] | None = None,
        overrides: Sequence[str] = (),
        variables: Mapping[str, str] | None = None,
        env_file: str | None = None,
        output_dir: str | None = None,
        formats: Sequence[str] | None = None,
        dvf_run_id: str | None = None,
        fail_on: str = "error",
        min_score: float | None = None,
        strict: bool = False,
        fail_task: bool = True,
        history: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        parse_severity(fail_on)  # reject a bad value when the DAG is parsed
        self.config = config
        self.profile = profile
        self.only = only
        self.overrides = list(overrides)
        self.variables = dict(variables or {})
        self.env_file = env_file
        self.output_dir = output_dir
        self.formats = list(formats) if formats is not None else None
        self.dvf_run_id = dvf_run_id
        self.fail_on = fail_on
        self.min_score = min_score
        self.strict = strict
        self.fail_task = fail_task
        self.history = history

    def execute(self, context: Any) -> dict[str, Any]:
        """Run the configuration and return the summary (the ``return_value`` XCom)."""
        _, summary = run_validation(
            self.config,
            profile=self.profile or None,
            only=self.only,
            overrides=self.overrides,
            variables=self.variables,
            env_file=self.env_file,
            output_dir=self.output_dir or None,
            formats=self.formats,
            run_id=self.dvf_run_id or None,
            history=self.history,
            fail_on=self.fail_on,
            min_score=self.min_score,
            strict=self.strict,
        )
        push_xcoms(context, summary)
        log = getattr(self, "log", None)
        if log is not None:
            log.info("%s", summary.message())
            if summary.results_path:
                log.info("results: %s", summary.results_path)
        if self.fail_task and not summary.passed:
            if summary.exit_code == 1:
                raise AirflowFailException(summary.message())
            raise AirflowException(summary.message())
        return summary.to_dict()


def push_xcoms(context: Any, summary: DvfRunSummary) -> None:
    """Push the fields in :data:`XCOM_KEYS` as ``dvf_<field>`` XComs of the task instance."""
    ti = context.get("ti") if isinstance(context, Mapping) else None
    if ti is None:
        return
    data = summary.to_dict()
    for key in XCOM_KEYS:
        ti.xcom_push(key=f"dvf_{key}", value=data[key])


__all__ = ["XCOM_KEYS", "DvfOperator", "push_xcoms"]
