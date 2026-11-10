"""Databricks Workflows: entry points for wheel, Python script, and notebook tasks.

* :func:`main` takes ``dvf run``-style arguments (``CONFIG [--profile P] [--only S] ...``), runs
  the configuration, sets job task values, and exits with ``dvf run``'s exit code when the run
  fails its gates (a successful run returns normally, as Databricks expects). Use it from a
  ``spark_python_task`` script, or as a ``python_wheel_task`` entry point once one is declared.
* :func:`run_notebook` does the same from a notebook and returns the summary; with
  ``exit_notebook=True`` it ends the notebook with the summary as JSON, which a Data Factory
  Databricks Notebook activity reads as ``runOutput``.

Task values (``dbutils.jobs.taskValues``) let later tasks branch on the result:
``dvf_exit_code``, ``dvf_passed``, ``dvf_status``, ``dvf_quality_score``, ``dvf_run_id``, and
``dvf_results_path``. They are set only when ``dbutils`` is available (on a Databricks cluster).

Spark connections use the cluster's running Spark session. Install ``dvf-framework`` on the
cluster without the ``spark`` extra: the runtime provides Spark. These entry points are verified
by unit tests with a stand-in ``dbutils``, not on a Databricks workspace.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from dvf.core.enums import Severity
from dvf.core.errors import ConfigError, DvfError
from dvf.integrations.common import DvfRunSummary, enforce_gate, run_validation

log = logging.getLogger(__name__)

TASK_VALUES = ("exit_code", "passed", "status", "quality_score", "run_id", "results_path")
"""Summary fields set as ``dvf_<field>`` task values."""


def _dbutils() -> Any:
    """The Databricks ``dbutils`` object, or None outside Databricks."""
    try:
        runtime = importlib.import_module("databricks.sdk.runtime")
    except ImportError:
        pass
    else:
        found = getattr(runtime, "dbutils", None)
        if found is not None:
            return found
    try:  # a notebook defines dbutils in the IPython namespace
        ipython = importlib.import_module("IPython")
        shell = ipython.get_ipython()
        if shell is not None:
            return shell.user_ns.get("dbutils")
    except ImportError:
        pass
    return None


def set_task_values(summary: DvfRunSummary, dbutils: Any = None) -> bool:
    """Set the :data:`TASK_VALUES` of ``summary``; False when ``dbutils`` is unavailable."""
    dbutils = dbutils if dbutils is not None else _dbutils()
    if dbutils is None:
        log.debug("dbutils is not available; task values are not set")
        return False
    data = summary.to_dict()
    try:
        for key in TASK_VALUES:
            dbutils.jobs.taskValues.set(key=f"dvf_{key}", value=data[key])
    except Exception as exc:  # task values exist only in job runs
        log.warning("could not set Databricks task values: %s", exc)
        return False
    return True


def parser() -> argparse.ArgumentParser:
    """The arguments of :func:`main` (a subset of ``dvf run``)."""
    p = argparse.ArgumentParser(
        prog="dvf-databricks", description="Run a dvf configuration in a Databricks job."
    )
    p.add_argument("config", help="Configuration file (a workspace, Volumes, or DBFS path)")
    p.add_argument("--profile", "-p", help="Environment profile")
    p.add_argument("--only", action="append", default=[], help="Selector; repeatable")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="PATH=VALUE")
    p.add_argument("--var", dest="variables", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--output-dir", "-o", help="Report folder (for example a Volumes path)")
    p.add_argument("--format", "-f", dest="formats", action="append", help="Report format")
    p.add_argument(
        "--fail-on", default="error", choices=[s.value for s in Severity], help="Gate severity"
    )
    p.add_argument("--min-score", type=float, help="Minimum quality score")
    p.add_argument("--strict", action="store_true", help="Warnings also fail")
    p.add_argument("--run-id", help="dvf run ID")
    p.add_argument("--no-history", action="store_true", help="Do not read or record history")
    return p


def _variables(items: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise ConfigError(f"--var expects NAME=VALUE, got '{item}'.")
        result[name.strip()] = value
    return result


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for a wheel or Python script task; parameters are ``dvf run`` arguments.

    A leading ``run`` is accepted, so ``["run", "dvf.yaml"]`` and ``["dvf.yaml"]`` are the same.
    Exits with code 1 when the gates fail, 2 on execution errors, 3 on configuration errors.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "run":
        args = args[1:]
    options = parser().parse_args(args)
    try:
        _, summary = run_validation(
            options.config,
            profile=options.profile,
            only=options.only,
            overrides=options.overrides,
            variables=_variables(options.variables),
            output_dir=options.output_dir,
            formats=options.formats,
            run_id=options.run_id,
            history=not options.no_history,
            fail_on=options.fail_on,
            min_score=options.min_score,
            strict=options.strict,
        )
    except ConfigError as exc:
        _exit(3, f"configuration error: {exc}")
    except DvfError as exc:
        _exit(2, f"execution error: {exc}")
    set_task_values(summary)
    sys.stdout.write(summary.message() + "\n")
    if summary.results_path:
        sys.stdout.write(f"results: {summary.results_path}\n")
    if not summary.passed:
        raise SystemExit(summary.exit_code)


def _exit(code: int, message: str) -> NoReturn:
    sys.stderr.write(f"dvf: {message}\n")
    raise SystemExit(code)


def run_notebook(
    config: str,
    *,
    profile: str | None = None,
    only: str | Sequence[str] | None = None,
    overrides: Sequence[str] = (),
    variables: Mapping[str, str] | None = None,
    output_dir: str | None = None,
    formats: Sequence[str] | None = None,
    fail_on: str = "error",
    min_score: float | None = None,
    strict: bool = False,
    history: bool = True,
    raise_on_failure: bool = True,
    exit_notebook: bool = False,
    dbutils: Any = None,
) -> DvfRunSummary:
    """Run ``config`` from a notebook task and return the summary.

    Sets task values, displays the Markdown summary when the notebook can render it, and raises
    :class:`~dvf.integrations.common.QualityGateError` (failing the task) when the gates fail.
    With ``exit_notebook`` a passing run ends the notebook with ``dbutils.notebook.exit(json)``.
    """
    _, summary = run_validation(
        config,
        profile=profile,
        only=only,
        overrides=overrides,
        variables=variables,
        output_dir=output_dir,
        formats=formats,
        history=history,
        fail_on=fail_on,
        min_score=min_score,
        strict=strict,
    )
    utils = dbutils if dbutils is not None else _dbutils()
    set_task_values(summary, utils)
    _display_markdown(summary.markdown())
    if raise_on_failure:
        enforce_gate(summary)
    if exit_notebook and utils is not None:
        utils.notebook.exit(json.dumps(summary.to_dict()))
    return summary


def _display_markdown(text: str) -> None:
    try:
        display = importlib.import_module("IPython.display")
    except ImportError:
        sys.stdout.write(text)
        return
    display.display(display.Markdown(text))


__all__ = ["TASK_VALUES", "main", "parser", "run_notebook", "set_task_values"]
