"""Thin integrations of dvf with orchestrators and data platforms.

Each submodule wraps :func:`dvf.api.run` for one platform and imports that platform only when the
submodule itself is imported, so ``import dvf`` never needs Airflow, Dagster, Prefect, or dbt:

=========================== ====================================================================
``dvf.integrations.common``  :func:`run_validation`, :class:`DvfRunSummary`, quality gates
``dvf.integrations.airflow`` ``DvfOperator`` (needs ``apache-airflow``)
``dvf.integrations.dagster`` ``build_dvf_asset_checks`` (needs ``dagster``)
``dvf.integrations.prefect`` ``dvf_validate`` task and ``dvf_validation_flow`` (needs ``prefect``)
``dvf.integrations.databricks`` wheel and notebook task entry points, job task values
``dvf.integrations.aws``     AWS Glue job entry point (S3 in and out, needs ``boto3``)
``dvf.integrations.dbt``     generate a configuration from a dbt ``manifest.json``
=========================== ====================================================================

The integrations are verified by unit tests with stand-in modules for the orchestrators, not
against running Airflow, Dagster, Prefect, Databricks, Glue, or dbt installations.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DvfRunSummary", "QualityGateError", "run_validation", "summarize"]


def __getattr__(name: str) -> Any:
    """The helpers of :mod:`dvf.integrations.common`, imported on first use."""
    if name in __all__:
        from dvf.integrations import common

        return getattr(common, name)
    raise AttributeError(f"module 'dvf.integrations' has no attribute {name!r}")
