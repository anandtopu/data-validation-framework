"""Airflow DAG: validate a migration with dvf, then branch on the quality score.

Copy this file into your DAGs folder and install dvf in the Airflow environment
(``pip install dvf-framework`` plus the extras your connections need). Put the dvf project next
to the DAG, or point ``DVF_CONFIG`` at it. Credentials come from the worker's environment or a
secrets backend through ``${secret:...}`` references in the configuration.

- ``validate_pr_checks`` runs the fast checks (``tag:pr``) and fails without retries when the
  quality gates fail.
- ``validate_nightly`` runs everything tagged ``nightly`` and only reports (``fail_task=False``);
  ``decide`` reads its summary from XCom and picks the next task.

Verified only by dvf's unit tests with a stand-in Airflow module, not on a running Airflow.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:  # Airflow 3
    from airflow.providers.standard.operators.empty import EmptyOperator
    from airflow.providers.standard.operators.python import BranchPythonOperator
    from airflow.sdk import DAG
except ImportError:  # Airflow 2
    from airflow import DAG
    from airflow.operators.empty import EmptyOperator
    from airflow.operators.python import BranchPythonOperator

from dvf.integrations.airflow import DvfOperator

CONFIG = os.environ.get("DVF_CONFIG", str(Path(__file__).parent / "dvf" / "dvf.yaml"))
REPORTS = os.environ.get("DVF_REPORTS", "/opt/airflow/dvf-reports/{{ ds }}/{{ run_id }}")


def choose(ti: Any, **_: Any) -> str:
    """Publish when the nightly run passed its gates, else open an incident."""
    summary = ti.xcom_pull(task_ids="validate_nightly")
    return "publish" if summary and summary["passed"] else "open_incident"


with DAG(
    dag_id="dvf_migration_validation",
    start_date=datetime(2026, 1, 1),
    schedule="0 2 * * *",
    catchup=False,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},
    tags=["dvf", "data-quality"],
) as dag:
    pr_checks = DvfOperator(
        task_id="validate_pr_checks",
        config=CONFIG,
        only=["tag:pr"],
        output_dir=REPORTS + "/pr",
        fail_on="error",
    )
    nightly = DvfOperator(
        task_id="validate_nightly",
        config=CONFIG,
        only="tag:nightly",
        output_dir=REPORTS + "/nightly",
        min_score=95,
        fail_task=False,
    )
    decide = BranchPythonOperator(task_id="decide", python_callable=choose)
    publish = EmptyOperator(task_id="publish")
    incident = EmptyOperator(task_id="open_incident")

    pr_checks >> nightly >> decide >> [publish, incident]
