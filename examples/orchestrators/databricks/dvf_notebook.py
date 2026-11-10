# Databricks notebook source
# MAGIC %md
# MAGIC # dvf validation
# MAGIC
# MAGIC Runs a dvf configuration as a notebook task. Widgets (job parameters, or Data Factory
# MAGIC `baseParameters`): `config`, `only`, `output_dir`, `fail_on`, `min_score`, `raise_on_failure`.
# MAGIC
# MAGIC The run sets the task values `dvf_passed`, `dvf_exit_code`, `dvf_quality_score`,
# MAGIC `dvf_status`, `dvf_run_id`, and `dvf_results_path`, shows the Markdown summary, fails the
# MAGIC task when the quality gates fail (unless `raise_on_failure` is `false`), and otherwise
# MAGIC ends with the summary as JSON (the `runOutput` of a Data Factory Databricks Notebook
# MAGIC activity). Not verified on a Databricks workspace.

# COMMAND ----------

# MAGIC %pip install dvf-framework==1.0.0

# COMMAND ----------

dbutils.library.restartPython()  # noqa: F821 - dbutils is defined in Databricks notebooks

# COMMAND ----------

from dvf.integrations.databricks import run_notebook  # noqa: E402

widgets = dbutils.widgets  # noqa: F821
widgets.text("config", "/Volumes/main/qa/dvf/dvf.yaml")
widgets.text("only", "tag:nightly")
widgets.text("output_dir", "")
widgets.dropdown("fail_on", "error", ["info", "warning", "error", "critical"])
widgets.text("min_score", "")
widgets.dropdown("raise_on_failure", "true", ["true", "false"])

min_score = widgets.get("min_score")
summary = run_notebook(
    widgets.get("config"),
    only=widgets.get("only"),
    output_dir=widgets.get("output_dir") or None,
    fail_on=widgets.get("fail_on"),
    min_score=float(min_score) if min_score else None,
    raise_on_failure=widgets.get("raise_on_failure") == "true",
    exit_notebook=True,
)
