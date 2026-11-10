# Orchestrators and platforms

dvf runs wherever Python 3.11 runs, so any scheduler can call `dvf run` and act on its exit code (see [CI/CD](ci.md#exit-codes-and-quality-gates)). For the common orchestrators, `dvf.integrations` adds thin wrappers around `dvf.api.run` that return a run summary, publish results in the orchestrator's own UI, and fail the task by the quality gates. Runnable examples are in `examples/orchestrators/`.

| Platform | dvf provides | Example |
|---|---|---|
| Airflow | `dvf.integrations.airflow.DvfOperator` | `airflow/dvf_dag.py` |
| Dagster | `dvf.integrations.dagster.build_dvf_asset_checks` | `dagster/definitions.py` |
| Prefect | `dvf.integrations.prefect.dvf_validate` (task), `dvf_validation_flow` | `prefect/dvf_flow.py` |
| Databricks Workflows | `dvf.integrations.databricks.main` and `run_notebook` | `databricks/job.json`, `dvf_task.py`, `dvf_notebook.py` |
| Azure Data Factory, Synapse | pipeline JSON (container or Databricks activity) | `azure/adf_pipeline_container.json`, `adf_pipeline_databricks.json` |
| AWS Glue, Step Functions | `dvf.integrations.aws.glue_main`; state machines | `aws/glue_dvf_job.py`, `glue_job.json`, `step_functions_*.asl.json` |
| dbt | `dvf dbt import`, `dvf.integrations.dbt` | `dbt/run_after_dbt_build.py` |

!!! warning "What is verified"
    Airflow, Dagster, Prefect, dbt, and the Databricks SDK are not installed in dvf's test environment. The integration modules are verified by unit tests with small stand-in modules for each orchestrator (checking that they call `dvf.api.run` correctly, map the results, and honor the gates), and the Airflow, Dagster, and Prefect examples are loaded against the same stand-ins. The Databricks, Glue, and S3 code is tested with stand-ins for `dbutils` and the S3 client. The dbt import and the dbt example run end to end on synthetic dbt artifacts and a local DuckDB file. Nothing on this page has been run on a real Airflow, Dagster, Prefect, Databricks, Azure, or AWS installation; the JSON definitions are checked only for structure.

## The run summary

Every wrapper runs the configuration once and reduces the result to a `DvfRunSummary` (from `dvf.integrations.common`), whose dictionary form is what lands in XComs, task values, and flow results:

| Field | Meaning |
|---|---|
| `run_id`, `project`, `status` | The run |
| `exit_code`, `passed`, `outcome` | `dvf run`'s exit code with the given gates; `passed` is true for 0; `outcome` is `passed`, `validation_failed`, or `execution_error` |
| `quality_score`, `checks_by_status` | Quality score and check counts |
| `failed_checks`, `error_checks`, `warning_checks` | Check IDs |
| `results_path`, `report_dir`, `reports` | Where the reports are |

The gates are the same as on the command line: `fail_on` (default `error`), `min_score`, and `strict`. Use the summary in your own code:

```python
from dvf.integrations.common import enforce_gate, run_validation

output, summary = run_validation("dvf.yaml", only="tag:nightly", min_score=95)
print(summary.message())
enforce_gate(summary)  # raises QualityGateError when the gates failed
```

`only` takes a list or one string of selectors separated by commas or spaces, so orchestrator parameters can pass `"tag:pr, table:orders"`.

## Airflow

```bash
pip install dvf-framework apache-airflow   # Airflow 2.7 or later, or 3.x
```

```python
from dvf.integrations.airflow import DvfOperator

validate = DvfOperator(
    task_id="validate_orders",
    config="/opt/airflow/dags/dvf/dvf.yaml",
    only=["tag:pr"],
    output_dir="/opt/airflow/dvf-reports/{{ ds }}/{{ run_id }}",
    fail_on="error",
    min_score=95,
)
```

- The operator runs in the worker and returns the summary dictionary, which Airflow pushes as the `return_value` XCom. It also pushes `dvf_run_id`, `dvf_status`, `dvf_exit_code`, `dvf_quality_score`, and `dvf_results_path`.
- Failed gates raise `AirflowFailException`, so the task fails without retries; execution errors raise `AirflowException`, so the task's `retries` apply.
- `fail_task=False` always succeeds and only reports; branch on the XCom, as `examples/orchestrators/airflow/dvf_dag.py` does with a `BranchPythonOperator`.
- `config`, `profile`, `only`, `overrides`, `variables`, `output_dir`, and `dvf_run_id` are templated. Configuration files are passed to dvf as paths and never rendered by Jinja.
- Other parameters: `formats`, `env_file`, `strict`, `history`.

Limits: the operator is synchronous and holds a worker slot for the length of the run; there is no deferrable mode. Run long comparisons on a dedicated pool or queue, or start them with the `KubernetesPodOperator` and the dvf container image.

## Dagster

```bash
pip install dvf-framework dagster   # Dagster 1.7 or later
```

```python
import dagster as dg
from dvf.integrations.dagster import build_dvf_asset_checks

dvf_checks = build_dvf_asset_checks(
    "dvf/dvf.yaml",
    asset_key={"customers": ["crm", "customers"]},  # or a function (suite, table) -> key
    granularity="table",
    only="tag:pr",
    blocking=True,
)
defs = dg.Definitions(assets=[...], asset_checks=[dvf_checks])
```

- The configuration is read when the code location loads, without fetching secrets, and declares the asset checks; it runs when the checks execute. One `dvf.api.run` serves every check.
- `granularity="table"` declares one asset check per dvf table (named `dvf__<suite>__<table>`); `granularity="check"` declares, per table, one for its schema checks, one for its comparison, and one per configured check.
- By default a table's checks attach to the asset named after the table; pass `asset_key` as a mapping (keyed by `"suite.table"` or `"table"`) or a function.
- Each result carries metadata: `dvf_status`, `dvf_checks` (counts by status), `dvf_failed_checks`, `dvf_quality_score`, `dvf_run_id`, `dvf_results_path`, and, for tables and comparisons, `dvf_row_counts` and `dvf_reconciliation`.
- Severity: errored checks, failed checks at or above `fail_on`, and a table score below `min_score` give `ERROR`; failed checks below `fail_on`, and warnings with `strict`, give `WARN`. With `blocking=True`, `ERROR` results stop downstream assets.

Limits: a table that produced no results (filtered out with `only`, or skipped) reports a passing check with `dvf_status: not_run`, because Dagster expects a result for every declared check. Pattern tables (`pattern:`) report all the tables they matched under one check. Selecting a subset of the checks in Dagster still runs the whole configuration.

## Prefect

```bash
pip install dvf-framework prefect   # Prefect 2.14 or later, or 3.x
```

```python
from prefect import flow
from dvf.integrations.prefect import dvf_validate


@flow
def nightly_migration_checks() -> None:
    dvf_validate("dvf/dvf.yaml", only="tag:nightly", min_score=95)
```

- `dvf_validate` is a task. It creates a Markdown artifact with the run's `summary.md` (key `dvf-<project>-summary`) and a table artifact of the failed, errored, and warning checks (`dvf-<project>-problems`); `artifact_prefix` changes the keys and `create_artifacts=False` turns them off.
- Failed gates raise `QualityGateError`, which fails the task and the flow; `raise_on_failure=False` only returns the summary.
- `dvf_validation_flow(config, profile, only, fail_on, min_score, strict)` is a ready-made flow for deployments; `dvf.integrations.prefect.validate` is the plain function behind the task, to wrap in your own task.

## Databricks Workflows

Install `dvf-framework` on the job cluster (a PyPI library) without the `spark` extra: the runtime provides Spark, and Spark connections use the cluster's session. dvf's Spark support is tested with Spark 4 (Databricks Runtime 17 and later ship it); Databricks clusters themselves are not covered by dvf's tests ([Spark](spark.md)). Write reports to a Unity Catalog volume, for example `--output-dir /Volumes/main/qa/dvf/reports/{{job.run_id}}`.

`examples/orchestrators/databricks/job.json` (for `databricks jobs create --json @job.json`) has three ways to run dvf:

| Task type | How | Notes |
|---|---|---|
| Python wheel | `package_name: dvf_framework`, `entry_point: dvf`, parameters `["run", "dvf.yaml", ...]` | The `dvf` command itself; exit codes as on the command line |
| Python script | `examples/orchestrators/databricks/dvf_task.py` with `dvf run` parameters | `dvf.integrations.databricks.main`: also sets task values |
| Notebook | `examples/orchestrators/databricks/dvf_notebook.py` with widgets | `run_notebook`: shows the summary, sets task values, can end the notebook with the summary as JSON |

`main` and `run_notebook` set the task values `dvf_passed`, `dvf_exit_code`, `dvf_quality_score`, `dvf_status`, `dvf_run_id`, and `dvf_results_path`. Later tasks read them as `{{tasks.<task>.values.dvf_passed}}`, for example in a condition task, as the example job does before its `publish` task. `main` returns normally when the run passes and exits with code 1, 2, or 3 otherwise; `run_notebook` raises `QualityGateError` unless `raise_on_failure=False`.

## Azure Data Factory, Synapse, and Fabric

Two pipeline definitions are in `examples/orchestrators/azure/`:

- **Container** (`adf_pipeline_container.json`): a Web activity starts an Azure Container Apps job running the dvf image (`ghcr.io/<org>/dvf:latest-full`) with `dvf run` arguments, an Until loop polls the execution, and a Fail activity fails the pipeline unless it succeeded. dvf exits non-zero when the gates fail, so the execution fails. Mount the project (configuration and any files) at `/work`, for example from an Azure Files share, and give the factory's managed identity the Container Apps Jobs Operator role (or Contributor) on the job.
- **Databricks** (`adf_pipeline_databricks.json`): a Databricks Notebook activity runs `dvf_notebook.py` with `raise_on_failure=false`, and an If Condition reads `runOutput.passed` and fails the pipeline with the quality score in the message.

Synapse pipelines use the same activity types. Fabric data pipelines can run dvf with a Notebook activity (`%pip install dvf-framework` in the notebook, then `run_notebook`); Fabric's pipeline JSON is not provided.

## AWS Glue and Step Functions

`dvf.integrations.aws.glue_main` is the body of a Glue job script (`examples/orchestrators/aws/glue_dvf_job.py`). It downloads the project from S3, runs it, uploads the reports to `<reports_s3_uri>/<run_id>/`, and then fails the job if the gates failed.

| Job argument | Meaning |
|---|---|
| `--project_s3_uri` | A project folder (configuration, data files, plugins) to download |
| `--config` | Configuration file within the project (default `dvf.yaml`) |
| `--config_s3_uri` | A single configuration file instead of a project |
| `--reports_s3_uri` | Where to upload the reports |
| `--profile`, `--only`, `--fail_on`, `--min_score` | As in `dvf run`; `--only` separates selectors with commas |

Install dvf with `--additional-python-modules dvf-framework==1.0.0` (plus extras). dvf needs Python 3.11 or later, so use a Glue 5.0 job (`glue_job.json` defines a `glueetl` job); Glue Python shell jobs run an older Python. History is not kept between Glue runs.

Two Step Functions state machines are included:

- `step_functions_glue.asl.json` runs the Glue job with `glue:startJobRun.sync`; a failed job (failed gates or an error) goes to a Fail state. Input: `{"only": "tag:nightly", "min_score": "95"}`.
- `step_functions_ecs.asl.json` runs the dvf container as an ECS Fargate task with `ecs:runTask.sync` and branches on the container's exit code: 0 succeeds, 1 fails as `DvfQualityGatesFailed`, anything else as `DvfExecutionError`.

## dbt

Run dvf after `dbt build`, against the models dbt just built. `dvf dbt import` generates the configuration from dbt's `manifest.json`:

```bash
dbt build
dvf dbt import target/manifest.json -o dvf.yaml      # once; review and commit it
dvf validate-config dvf.yaml
dvf run dvf.yaml
```

The generated file has one data quality suite (`dbt_models`, tagged `dbt`) with one table per model of the dbt project: its relation (`schema.name`, with the database when it differs from the most common one), description, tags, and keys from a primary-key constraint, an incremental model's `unique_key`, or a `unique` test. dbt generic tests become checks:

| dbt test | dvf check |
|---|---|
| `not_null`, `dbt_expectations.expect_column_values_to_not_be_null` | `not_null` (columns of one model merged) |
| `unique`, `dbt_utils.unique_combination_of_columns`, `dbt_expectations.expect_column_values_to_be_unique` | `unique` |
| `accepted_values`, `dbt_expectations.expect_column_values_to_be_in_set` | `allowed_values` |
| `relationships` | `referential_integrity` (parent model, seed, snapshot, or source) |
| `dbt_utils.accepted_range`, `dbt_expectations.expect_column_values_to_be_between` | `range` |
| `dbt_utils.expression_is_true` | `expression` |
| `dbt_expectations.expect_column_values_to_match_regex` | `regex` with `full_match: false` |
| `dbt_expectations.expect_table_row_count_to_be_between` | `row_count_range` |

A test's `severity: warn` becomes `severity: warning` and its `where` is kept. A test with a threshold (`error_if: "> 10"`) gets a `REVIEW` comment, because dvf fails on the first failing row unless you add `mostly`. Each table also gets `row_count_range` with `min: 1` (`--no-row-count` leaves it out). Singular tests, tests on column expressions, and other generic tests are listed at the end of the file as not converted; dbt still runs them.

| Option | Meaning |
|---|---|
| `--model`, `--exclude` | Globs over model names (repeatable) |
| `--tag` | Only models with this dbt tag (repeatable) |
| `--all-packages` | Include models of installed packages (default: the root project's only) |
| `--no-tests`, `--no-row-count` | Leave out converted tests, or the row count check |
| `--config FILE --connection NAME` | Copy the connection from a configuration |
| `--location URL` | The warehouse as a location (`postgresql://...`, `duckdb:///file`) |
| `--suite-name`, `--project-name` | Names in the generated file |
| `--output`, `--force`, `--format json` | As in the other generation commands ([Discovery and generation](discovery.md)) |

Without `--config` or `--location`, the connection is derived from the manifest's adapter (PostgreSQL, Redshift, Snowflake, BigQuery, Databricks, DuckDB, SQL Server, Fabric, Synapse, MySQL, Oracle, Spark) with `${env:WAREHOUSE_...}` and `${secret:env:WAREHOUSE_PASSWORD}` placeholders; other adapters get a placeholder to replace. Ephemeral and disabled models are skipped.

To validate only what dbt built in this run, read `target/run_results.json`:

```python
from dvf.integrations.common import run_validation
from dvf.integrations.dbt import built_model_selectors

selectors = built_model_selectors("target/run_results.json")  # ["table:customers", ...]
output, summary = run_validation("dvf.yaml", only=selectors)
```

`examples/orchestrators/dbt/run_after_dbt_build.py` puts it together (dbt build, generate the configuration on the first run, validate the built models, exit with dvf's code). It runs without dbt on the synthetic artifacts in that folder:

```bash
cd examples/orchestrators/dbt
python demo_warehouse.py                          # the DuckDB file the manifest describes
python run_after_dbt_build.py --project-dir . --skip-dbt
```

The demo data has seeded defects, so four checks fail and the script exits with 1.

The generated suite checks the dbt models themselves. To reconcile them with a legacy source, add a comparison suite, by hand or with `dvf discover` ([Discovery and generation](discovery.md)).
