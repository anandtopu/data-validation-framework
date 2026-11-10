# Observability

This page covers what dvf tells other systems about its own runs: structured logs, OpenLineage events, OpenTelemetry traces and metrics, and the interface for AI-assisted triage plugins. Everything except logs is off until you configure it, and nothing is sent anywhere you did not configure.

## Structured logs

`--log-level debug` adds detail; `--log-format json` writes one JSON object per line. Records from a table's work include `run_id`, `suite`, and `table`, so logs from parallel tables can be told apart. Registered secret values are masked in every record. Failed notifications, unreachable lineage backends, and telemetry problems are logged as warnings and never fail a run. See [Debugging and operations](debugging.md#logs).

## OpenLineage

dvf can emit [OpenLineage](https://openlineage.io) run events, so lineage tools such as Marquez show which datasets a validation read and how its checks went.

```yaml
lineage:
  enabled: true
  namespace: dvf                  # namespace of dvf's jobs (default)
  job_name: sales_migration       # default: the project name
  transport:
    type: http
    url: http://localhost:5000    # Marquez
    api_key: ${secret:env:OPENLINEAGE_API_KEY}   # optional; sent as a bearer token
```

### Events

| Event | When |
|---|---|
| `START` | The run starts |
| `COMPLETE` | The run finished, also when checks failed: the assertions carry the results |
| `FAIL` | The run's status is in `fail_on` (default `[error]`), or the run stopped with an error |
| `ABORT` | The run was interrupted |

Each run is one job run. Its `runId` is a UUID derived from the dvf run ID, so a resumed run (`--resume`) continues the same OpenLineage run. With `granularity: table`, each table is also a child job (`<job_name>.<suite>.<table>`) with its own `START` and `COMPLETE` or `FAIL`, linked to the run with the `parent` facet; the datasets are then reported on the table jobs only.

### Datasets and facets

Every dataset a table reads, source and target, is an input dataset with these input facets:

- `dataQualityAssertions` ([facet schema 1-1-0](https://openlineage.io/spec/facets/1-1-0/DataQualityAssertionsDatasetFacet.json)): one assertion per check, with `assertion` (the check type), `success`, `name` (the check ID), `severity` (`error` for error and critical checks, `warn` otherwise), `column` (for single-column checks), `description` (dvf's message), `actual` (the failing row count), and `params` (status, dimension, counts). Comparisons and pair checks are reported on the target dataset; checks on one side on that side's dataset.
- `dataQualityMetrics` ([facet schema 1-0-2](https://openlineage.io/spec/facets/1-0-2/DataQualityMetricsInputDatasetFacet.json)): `rowCount` where dvf counted rows, and per-column `count`, `nullCount`, `distinctCount`, `min`, and `max` (numbers only) where the `profile` strategy ran.

Run facets: `processing_engine` (dvf and its version), `tags` (`dvf.run_id`, `dvf.project`, `dvf.status`, `dvf.quality_score`, and the profile), `errorMessage` on `FAIL`, and `parent` for table jobs. The job facet `jobType` says `BATCH`, integration `DVF`. Events follow the [OpenLineage 2-0-2 spec](https://openlineage.io/spec/2-0-2/OpenLineage.json); every facet names its schema in `_schemaURL`.

Dataset namespaces and names follow the [OpenLineage naming conventions](https://openlineage.io/docs/spec/naming) where they define one:

| Connection | Namespace | Name |
|---|---|---|
| PostgreSQL, SQL Server, Oracle, MySQL, Redshift | `postgres://host:5432` (scheme by type; default ports) | `database.schema.table` (`database.table` for MySQL) |
| Snowflake | `snowflake://<account>` | `database.schema.table` |
| BigQuery | `bigquery` | `project.dataset.table` |
| Databricks | `databricks://<server hostname>` | `catalog.schema.table` |
| Files on S3, GCS, ADLS | `s3://bucket`, `gs://bucket`, `abfss://container@account.dfs.core.windows.net` | the object path |
| Local files | `file` | the absolute path |
| DuckDB, SQLite | `duckdb://<absolute file path>` (or `duckdb://memory`) | the table |
| Others | `<type>://<connection name>` | the table |

Unqualified table names get the connection's database and schema (PostgreSQL and Redshift default to `public`, SQL Server to `dbo`). Hosts and ports are taken from the connection settings or its URL; credentials in URLs are never used. When another producer names the same database differently, set the namespace per connection:

```yaml
lineage:
  dataset_namespaces:
    warehouse: postgres://warehouse.internal.example.com:5432
```

### Transports

| `transport.type` | Settings | Behavior |
|---|---|---|
| `http` | `url`, `endpoint` (default `api/v1/lineage`), `api_key`, `timeout_s` (5), `retries` (2) | Posts each event as JSON. Transient failures are retried; if the backend stays unreachable, dvf logs one warning and sends no more events in that run |
| `file` | `path` (relative to the configuration; `${run.*}` allowed) | Appends one JSON event per line |
| `console` | | Writes one JSON event per line to standard error (the default transport) |

In strict privacy mode, assertions have no `description` and metrics no `min` or `max`. Events never contain sample values or queries.

dvf writes the events itself, without the OpenLineage client library. The tests validate every event against a JSON Schema that encodes the requirements of the spec and of the facet schemas above, and post them to a local HTTP server; they were not sent to a running Marquez.

## OpenTelemetry

With `pip install 'dvf-framework[otel]'`, dvf records traces and metrics of its runs.

```yaml
telemetry:
  enabled: true
  exporter: global      # or console
  metrics: true
```

### Spans

| Span | Attributes |
|---|---|
| `dvf.run` | `dvf.run_id`, `dvf.project`, `dvf.profile`, `dvf.status`, `dvf.quality_score`, `dvf.rows_compared`, `dvf.checks`, `dvf.checks.failed`, `dvf.checks.error`, `dvf.interrupted` |
| `dvf.suite` | `dvf.suite`, `dvf.status`, `dvf.tables` |
| `dvf.table` | `dvf.suite`, `dvf.table`, `dvf.status`, `dvf.rows.source`, `dvf.rows.target`, `dvf.source`, `dvf.target`, `dvf.checks` |
| `dvf.check` | `dvf.check.id`, `dvf.check.type`, `dvf.check.status`, `dvf.check.severity`, `dvf.check.dimension`, `dvf.check.rows`, `dvf.check.unexpected_rows`, `dvf.check.columns`, `dvf.engine`, `dvf.partition`, `dvf.query_ms.source`, `dvf.query_ms.target`, `dvf.retries` |

Spans nest run, suite, table, check, also when tables run in parallel. A table span is current while the table runs, so spans of instrumented database drivers nest under it. Check spans are recorded when their table finishes, with the checks' own start and end times. Errored checks, tables, and runs get the span status `ERROR`; a failed check is not a span error (the check ran; the data failed it), and carries `dvf.check.status: failed`.

### Metrics

| Metric | Unit | Attributes |
|---|---|---|
| `dvf.rows.scanned` | `{row}` | |
| `dvf.table.rows_per_second` | `{row}/s` | |
| `dvf.query.duration` | `ms` | `dvf.side` (`source`, `target`) |
| `dvf.query.retries` | `{retry}` | |
| `dvf.checks` | `{check}` | `dvf.check.status`, `dvf.check.type` |
| `dvf.run.duration` | `ms` | `dvf.status` |

### Exporters

- `global` (default): dvf uses the tracer and meter providers configured in the process, so you choose exporters the usual OpenTelemetry way. From the command line, install the distribution and an exporter (`pip install opentelemetry-distro opentelemetry-exporter-otlp`) and run `opentelemetry-instrument dvf run dvf.yaml` with the standard variables, such as `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_EXPORTER=otlp`, and `OTEL_METRICS_EXPORTER=otlp`. From Python, set the providers before calling `dvf.run`. Without configured providers, OpenTelemetry's defaults record nothing.
- `console`: dvf creates its own SDK providers (`service.name` from `telemetry.service_name`) that print spans and metrics to standard error, for debugging. They are shut down at the end of the run.

When `telemetry.enabled` is false, nothing is recorded. When OpenTelemetry is not installed, dvf logs one warning and runs without telemetry.

The tests record spans and metrics with the SDK's in-memory exporters. Export over OTLP is the standard OpenTelemetry mechanism and is not exercised by dvf's tests.

## AI-assisted triage interface

dvf defines an interface for plugins that suggest causes of failures, for example with a language model. It is off by default, dvf ships no implementation, and nothing in dvf calls an external service.

Opt in per configuration:

```yaml
triage:
  enabled: true
  max_checks: 25              # failed and errored checks summarized, worst first
  max_columns_per_check: 20
```

Then implement the `triage_failures` hook in a plugin (`plugins.paths`, or the `dvf.hooks` entry point group):

```python
from dvf.plugins import hookimpl


@hookimpl
def triage_failures(summary):
    suggestions = []
    for check in summary.checks:
        for column in check.column_mismatches:
            if column.classifications.get("whitespace_only", 0) > column.mismatch_count / 2:
                suggestions.append(
                    {
                        "check_id": check.check_id,
                        "summary": f"{column.column}: mostly whitespace",
                        "source": "rules",
                    }
                )
    return suggestions
```

The hook is called after the run and before reports render, only when `triage.enabled` is true and checks failed or errored. Its `summary` is masked and aggregated by construction. It holds:

- the run ID, project, run status, quality score, and check counts by status;
- for each failed or errored check: check ID, type, dimension, severity, status, column names, failing and total row counts, engine, the error type (the exception class only), the table's key-level reconciliation totals, and per column the mismatch count, percentage, and counts by mismatch class (such as `whitespace_only` or `numeric_rounding`).

It never holds data values, samples, queries, check messages, hints, or error messages. `summary.to_dict()` gives it as plain data.

Return a list of suggestions, each `{"summary": str, "check_id": str | None, "source": str | None}` (or plain strings). They appear in the reports as a "Triage suggestions" section, marked as suggestions to review. A plugin that raises is logged as a warning and ignored.

A plugin that sends the summary to an external service moves information about your data (names, counts, and failure patterns) outside your environment. Check that against your data protection rules before enabling it.
