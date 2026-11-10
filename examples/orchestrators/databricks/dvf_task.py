"""Databricks Python script task: run dvf with ``dvf run``-style parameters.

Upload this file to the workspace (or reference it from a Git folder) and use it as a
``spark_python_task`` with parameters such as::

    ["/Volumes/main/qa/dvf/dvf.yaml", "--only", "tag:nightly",
     "--output-dir", "/Volumes/main/qa/dvf/reports/{{job.run_id}}", "--min-score", "95"]

Install ``dvf-framework`` on the job cluster (without the ``spark`` extra; the runtime provides
Spark). The task fails when the quality gates fail and sets the task values ``dvf_passed``,
``dvf_exit_code``, ``dvf_quality_score``, ``dvf_status``, ``dvf_run_id``, and
``dvf_results_path`` for later tasks. Not verified on a Databricks workspace.
"""

from __future__ import annotations

from dvf.integrations.databricks import main

if __name__ == "__main__":
    main()
