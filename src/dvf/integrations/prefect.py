"""Prefect: a task and a flow that run a dvf configuration and publish result artifacts.

.. code-block:: python

    from prefect import flow
    from dvf.integrations.prefect import dvf_validate

    @flow
    def nightly_migration_checks() -> None:
        dvf_validate("dvf/dvf.yaml", only="tag:nightly", min_score=95)

``dvf_validate`` is a Prefect task wrapping :func:`dvf.api.run`. It creates a Markdown artifact
(the run's ``summary.md``) and a table artifact of the failed, errored, and warning checks, and
returns the run summary as a dictionary. When the run fails its quality gates the task raises
:class:`~dvf.integrations.common.QualityGateError`, which fails the task and its flow; pass
``raise_on_failure=False`` to only return the summary. ``dvf_validation_flow`` is a ready-made
flow around the task, for deployments.

Importing this module needs ``prefect`` (2.14 or later, or 3.x); ``import dvf`` does not. It is
verified by unit tests with a stand-in ``prefect`` module, not on a Prefect server.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from dvf.integrations.common import enforce_gate, problem_rows, run_validation

try:
    import prefect  # type: ignore[import-not-found, unused-ignore]
    from prefect import artifacts as prefect_artifacts  # type: ignore[import-not-found, unused-ignore]
except ImportError as exc:  # pragma: no cover - exercised only without the stand-in module
    raise ImportError(
        "dvf.integrations.prefect needs Prefect 2.14 or later: pip install prefect"
    ) from exc

_KEY_UNSAFE = re.compile(r"[^a-z0-9-]+")


def artifact_key(text: str) -> str:
    """A Prefect artifact key (lower-case letters, digits, and dashes) from ``text``."""
    return _KEY_UNSAFE.sub("-", text.lower()).strip("-")[:100] or "dvf"


def _logger() -> logging.Logger | logging.LoggerAdapter[logging.Logger]:
    try:
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger] = prefect.get_run_logger()
    except Exception:  # outside a flow or task run
        logger = logging.getLogger(__name__)
    return logger


def validate(
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
    create_artifacts: bool = True,
    artifact_prefix: str | None = None,
    raise_on_failure: bool = True,
) -> dict[str, Any]:
    """Run ``config``, publish artifacts, apply the gates, and return the summary dictionary.

    This is the function behind the :data:`dvf_validate` task; call it directly to wrap it in
    your own task. Artifact keys are ``<prefix>-summary`` and ``<prefix>-problems``, where the
    prefix defaults to ``dvf-<project>``.
    """
    output, summary = run_validation(
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
    logger = _logger()
    logger.info("%s", summary.message())
    if create_artifacts:
        prefix = artifact_key(artifact_prefix or f"dvf-{summary.project}")
        prefect_artifacts.create_markdown_artifact(
            markdown=summary.markdown(),
            key=f"{prefix}-summary",
            description=f"dvf run {summary.run_id}: {summary.status}",
        )
        rows = problem_rows(output.result)
        if rows:
            prefect_artifacts.create_table_artifact(
                table=rows,
                key=f"{prefix}-problems",
                description=f"Failed, errored, and warning checks of dvf run {summary.run_id}",
            )
    if raise_on_failure:
        enforce_gate(summary)
    return summary.to_dict()


dvf_validate: Any = prefect.task(name="dvf-validate", task_run_name="dvf {config}")(validate)
"""Prefect task: run a dvf configuration (see :func:`validate` for the parameters)."""


def validation_flow(
    config: str,
    profile: str | None = None,
    only: str | None = None,
    fail_on: str = "error",
    min_score: float | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Run one configuration as a flow (parameters are plain values, for deployments)."""
    result: dict[str, Any] = dvf_validate(
        config,
        profile=profile,
        only=only,
        fail_on=fail_on,
        min_score=min_score,
        strict=strict,
    )
    return result


dvf_validation_flow: Any = prefect.flow(name="dvf-validation")(validation_flow)
"""Prefect flow around :data:`dvf_validate`, ready for ``flow.serve()`` or a deployment."""


__all__ = ["artifact_key", "dvf_validate", "dvf_validation_flow", "validate", "validation_flow"]
