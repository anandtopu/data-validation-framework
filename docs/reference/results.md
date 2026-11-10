# Results schema

!!! note "Generated page"
    This page is generated from the result models by `uv run nox -s docs_reference`. Do not edit it by hand; change the source and regenerate.

Every report is rendered from one results document, `results.json`, written by every run (see
[ADR 0004](../adr/0004-canonical-result-model.md)). This page lists its fields; the JSON Schema is
committed as `schemas/results.schema.json` and printed by `dvf schema results`. The schema
version (currently `1.1.0`, in `schema_version`) follows SemVer independently
of the dvf version: fields may be added in a minor version, never removed or changed.

Read a results document in Python with `dvf.api.load_run(...)` or
`dvf.models.RunResult.model_validate_json(...)`.

## Top level {#runresult}

| Key | Type | Default | Description |
|---|---|---|---|
| `schema_version` | string | `1.1.0` |  |
| `run_id` | string | **required** |  |
| `project` | string | **required** |  |
| `status` | one of `passed`, `warning`, `failed`, `error`, `skipped`, `waived` | **required** |  |
| `started_at` | date-time (ISO 8601) | **required** |  |
| `ended_at` | date-time (ISO 8601) | `null` |  |
| `duration_ms` | number | `null` |  |
| `environment` | [object](#runenvironment) | **required** |  |
| `summary` | [object](#runsummary) | see section |  |
| `suites` | list of [object](#suiteresult) | `[]` |  |
| `config_snapshot` | mapping | `{}` | Resolved configuration with secrets redacted. |
| `metadata` | mapping | `{}` |  |
| `waivers` | list of [object](#waiverstatus) | `[]` |  |
| `baseline` | [object](#baselinecomparison) | `null` |  |
| `trend` | list of [object](#trendpoint) | `[]` | Earlier runs of this project, oldest first. |
| `interrupted` | boolean | `false` | The run stopped early; resume it with 'dvf run --resume'. |
| `resumed_from_checkpoint` | boolean | `false` | Some tables were loaded from this run's checkpoints. |

## `environment` {#runenvironment}

Everything needed to reproduce a run.

| Key | Type | Default | Description |
|---|---|---|---|
| `dvf_version` | string | **required** |  |
| `python_version` | string | **required** |  |
| `platform` | string | **required** |  |
| `hostname` | string | `null` |  |
| `profile` | string | `null` |  |
| `git_sha` | string | `null` |  |
| `config_files` | list of string | `[]` |  |
| `config_hash` | string | `null` |  |
| `cli_args` | list of string | `[]` |  |
| `plugins` | mapping of string | `{}` | Plugin distribution name to version. |
| `engines` | mapping of string | `{}` | Engine or driver name to version. |

## `summary` {#runsummary}

| Key | Type | Default | Description |
|---|---|---|---|
| `checks_by_status` | mapping of integer | `{}` |  |
| `suites` | integer | `0` |  |
| `datasets` | integer | `0` |  |
| `rows_compared` | integer | `0` |  |
| `quality_score` | number | `null` | Share of evaluated checks that passed, from 0 to 100. |

## `suites[]` {#suiteresult}

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | **required** |  |
| `status` | one of `passed`, `warning`, `failed`, `error`, `skipped`, `waived` | **required** |  |
| `tags` | list of string | `[]` |  |
| `datasets` | list of [object](#datasetpairresult) | `[]` |  |
| `duration_ms` | number | `null` |  |

## `waivers[]` {#waiverstatus}

How one configured waiver was used in a run.

| Key | Type | Default | Description |
|---|---|---|---|
| `selector` | string | **required** |  |
| `column` | string | `null` |  |
| `reason` | string | **required** |  |
| `ticket` | string | `null` |  |
| `approved_by` | string | `null` |  |
| `expires` | string | `null` |  |
| `status` | string | **required** | 'applied', 'expired', or 'unused'. |
| `checks` | list of string | `[]` | IDs of the checks it matched. |

## `baseline` {#baselinecomparison}

Failures compared with a baseline run (run-to-run diff).

| Key | Type | Default | Description |
|---|---|---|---|
| `baseline_run_id` | string | **required** |  |
| `baseline_started_at` | date-time (ISO 8601) | `null` |  |
| `policy` | string | **required** | 'last_run', 'last_passed', or an explicit run ID. |
| `new_failures` | list of string | `[]` | Check IDs failing now only. |
| `resolved` | list of string | `[]` | Check IDs that failed before only. |
| `persisting` | list of string | `[]` | Check IDs failing in both. |

## `trend[]` {#trendpoint}

One earlier run of the same project, for trends.

| Key | Type | Default | Description |
|---|---|---|---|
| `run_id` | string | **required** |  |
| `started_at` | date-time (ISO 8601) | **required** |  |
| `status` | one of `passed`, `warning`, `failed`, `error`, `skipped`, `waived` | **required** |  |
| `quality_score` | number | `null` |  |
| `checks` | integer | `0` |  |
| `failed` | integer | `0` |  |
| `errors` | integer | `0` |  |

## `suites[].datasets[]` {#datasetpairresult}

Results for one table (or dataset pair) in a suite.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | **required** |  |
| `source` | [object](#datasetref) | `null` |  |
| `target` | [object](#datasetref) | `null` |  |
| `status` | one of `passed`, `warning`, `failed`, `error`, `skipped`, `waived` | **required** |  |
| `row_counts` | [object](#rowcounts) | see section |  |
| `reconciliation` | [object](#reconciliationsummary) | `null` |  |
| `schema_diff` | list of [object](#columnschemadiff) | `[]` |  |
| `profiles` | list of [object](#profilecomparison) | `[]` |  |
| `checks` | list of [object](#checkresult) | `[]` |  |
| `duration_ms` | number | `null` |  |
| `details` | mapping | `{}` |  |

## `suites[].datasets[].source` {#datasetref}

Where a dataset lives: a table, a query, a file path, or an in-process DataFrame.

Also used at: `suites[].datasets[].target`.

| Key | Type | Default | Description |
|---|---|---|---|
| `connection` | string | **required** |  |
| `kind` | one of `table`, `query`, `path`, `dataframe` | **required** |  |
| `name` | string | **required** | Qualified table name, file path, query label, or DataFrame name. |
| `query` | string | `null` |  |

## `suites[].datasets[].row_counts` {#rowcounts}

| Key | Type | Default | Description |
|---|---|---|---|
| `source` | integer | `null` |  |
| `target` | integer | `null` |  |

## `suites[].datasets[].reconciliation` {#reconciliationsummary}

Key-level reconciliation totals for a dataset pair.

| Key | Type | Default | Description |
|---|---|---|---|
| `matched` | integer | `0` |  |
| `mismatched` | integer | `0` |  |
| `missing_in_target` | integer | `0` |  |
| `extra_in_target` | integer | `0` |  |
| `duplicate_keys_source` | integer | `0` |  |
| `duplicate_keys_target` | integer | `0` |  |
| `null_keys_source` | integer | `0` |  |
| `null_keys_target` | integer | `0` |  |

## `suites[].datasets[].schema_diff[]` {#columnschemadiff}

One row of a schema comparison.

| Key | Type | Default | Description |
|---|---|---|---|
| `source_column` | string | **required** |  |
| `target_column` | string | **required** |  |
| `status` | string | **required** | 'matched', 'missing_in_target', or 'extra_in_target'. |
| `source_type` | [object](#canonicaltype) | `null` |  |
| `target_type` | [object](#canonicaltype) | `null` |  |
| `source_native_type` | string | `null` |  |
| `target_native_type` | string | `null` |  |
| `compatibility` | one of `exact`, `compatible_widening`, `lossy`, `unknown`, `incompatible` | `null` |  |
| `reasons` | list of string | `[]` |  |
| `nullable_change` | string | `null` |  |

## `suites[].datasets[].profiles[]` {#profilecomparison}

Source and target profiles of a column, side by side.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** |  |
| `source_column` | string | `null` |  |
| `source` | [object](#columnprofile) | `null` |  |
| `target` | [object](#columnprofile) | `null` |  |
| `differences` | list of string | `[]` | Human-readable differences between the profiles. |

## `suites[].datasets[].checks[]` {#checkresult}

The outcome of one check, comparison, or schema validation.

| Key | Type | Default | Description |
|---|---|---|---|
| `check_id` | string | **required** |  |
| `name` | string | **required** |  |
| `type` | string | **required** |  |
| `dimension` | one of `completeness`, `uniqueness`, `validity`, `consistency`, `referential_integrity`, `accuracy`, `timeliness`, `volume`, `distribution`, `schema`, `reconciliation`, `custom` | **required** |  |
| `severity` | one of `info`, `warning`, `error`, `critical` | **required** |  |
| `status` | one of `passed`, `warning`, `failed`, `error`, `skipped`, `waived` | **required** |  |
| `side` | one of `source`, `target`, `both` | `null` |  |
| `tags` | list of string | `[]` |  |
| `owner` | string | `null` |  |
| `columns` | list of string | `[]` |  |
| `message` | string | `null` |  |
| `observed` | any | `null` |  |
| `expected` | any | `null` |  |
| `unexpected_count` | integer | `null` |  |
| `total_count` | integer | `null` |  |
| `unexpected_pct` | number | `null` |  |
| `mostly` | number | `null` |  |
| `samples` | [object](#sampleset) | `null` |  |
| `column_mismatches` | list of [object](#columnmismatch) | `[]` |  |
| `metrics` | list of [object](#metriccomparison) | `[]` |  |
| `hints` | list of string | `[]` |  |
| `artifacts` | list of [object](#artifactref) | `[]` |  |
| `queries` | list of [object](#queryrecord) | `[]` |  |
| `normalization` | list of string | `[]` |  |
| `engine` | string | `null` |  |
| `partition` | string | `null` |  |
| `started_at` | date-time (ISO 8601) | `null` |  |
| `ended_at` | date-time (ISO 8601) | `null` |  |
| `duration_ms` | number | `null` |  |
| `timing` | [object](#timingbreakdown) | `null` |  |
| `error` | [object](#errorinfo) | `null` |  |
| `waiver` | [object](#waiverapplied) | `null` |  |
| `details` | mapping | `{}` | Custom details from configuration or plugins. |
| `metadata` | mapping | `{}` |  |

## `suites[].datasets[].schema_diff[].source_type` {#canonicaltype}

An engine-independent column type.

Attributes that do not apply to a logical type stay `None` (or their default).
`length=None` on strings and binaries means unbounded; `precision=None` on decimals means
the engine's maximum or a floating decimal.

Also used at: `suites[].datasets[].schema_diff[].target_type`, `suites[].datasets[].schema_diff[].source_type.element`, `suites[].datasets[].schema_diff[].source_type.key`, `suites[].datasets[].schema_diff[].source_type.value`, `suites[].datasets[].schema_diff[].source_type.fields[].type`.

| Key | Type | Default | Description |
|---|---|---|---|
| `logical` | one of `BOOLEAN`, `INTEGER`, `DECIMAL`, `FLOAT`, `STRING`, `BINARY`, `DATE`, `TIME`, `TIMESTAMP`, `TIMESTAMP_TZ`, `INTERVAL`, `UUID`, `JSON`, `ARRAY`, `MAP`, `STRUCT`, `SPATIAL`, `UNKNOWN` | **required** |  |
| `bits` | integer | `null` |  |
| `unsigned` | boolean | `false` |  |
| `precision` | integer | `null` |  |
| `scale` | integer | `null` |  |
| `length` | integer | `null` |  |
| `fixed_length` | boolean | `false` |  |
| `unicode` | boolean | `true` |  |
| `element` | [object](#canonicaltype) | `null` |  |
| `key` | [object](#canonicaltype) | `null` |  |
| `value` | [object](#canonicaltype) | `null` |  |
| `fields` | list of [object](#structfield) | `[]` |  |

## `suites[].datasets[].profiles[].source` {#columnprofile}

Summary statistics of one column on one side.

Also used at: `suites[].datasets[].profiles[].target`.

| Key | Type | Default | Description |
|---|---|---|---|
| `count` | integer | **required** |  |
| `null_count` | integer | **required** |  |
| `null_pct` | number | `null` |  |
| `distinct_count` | integer | `null` |  |
| `min` | any | `null` |  |
| `max` | any | `null` |  |
| `mean` | number | `null` |  |
| `min_length` | integer | `null` |  |
| `max_length` | integer | `null` |  |
| `avg_length` | number | `null` |  |

## `suites[].datasets[].checks[].samples` {#sampleset}

Sample rows captured as evidence. Values are masked according to privacy settings.

Also used at: `suites[].datasets[].checks[].column_mismatches[].samples`.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of string | **required** |  |
| `rows` | list of list of any | **required** |  |
| `masked` | boolean | `false` |  |
| `truncated` | boolean | `false` |  |

## `suites[].datasets[].checks[].column_mismatches[]` {#columnmismatch}

Per-column mismatch summary for a source-vs-target comparison.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** |  |
| `source_column` | string | `null` |  |
| `mismatch_count` | integer | **required** |  |
| `mismatch_pct` | number | **required** |  |
| `classifications` | mapping of integer | `{}` | Mismatch class (for example 'whitespace_only') to number of rows. |
| `hints` | list of string | `[]` |  |
| `samples` | [object](#sampleset) | `null` |  |
| `waived` | boolean | `false` | An active waiver covers this column. |

## `suites[].datasets[].checks[].metrics[]` {#metriccomparison}

One metric compared between source and target: a row count or a control total.

Grouped comparisons produce one entry per group, with `group` holding the group's values.

| Key | Type | Default | Description |
|---|---|---|---|
| `metric` | string | **required** | For example 'row_count', 'sum', 'min', or 'null_count'. |
| `column` | string | `null` | Target column name, if any. |
| `source_column` | string | `null` |  |
| `group` | mapping | `null` |  |
| `source` | any | `null` |  |
| `target` | any | `null` |  |
| `difference` | number | `null` | Target minus source, for numeric metrics. |
| `tolerance` | number | `null` |  |
| `passed` | boolean | **required** |  |
| `note` | string | `null` |  |

## `suites[].datasets[].checks[].artifacts[]` {#artifactref}

A file produced alongside the report, such as a mismatch extract or repro SQL.

| Key | Type | Default | Description |
|---|---|---|---|
| `kind` | string | **required** |  |
| `path` | string | **required** |  |
| `format` | string | `null` |  |
| `rows` | integer | `null` |  |

## `suites[].datasets[].checks[].queries[]` {#queryrecord}

A query executed for a check, so failures can be reproduced exactly.

| Key | Type | Default | Description |
|---|---|---|---|
| `side` | one of `source`, `target`, `both` | **required** |  |
| `dialect` | string | `null` |  |
| `sql` | string | **required** |  |
| `parameters` | mapping | `{}` |  |
| `engine` | string | `null` |  |
| `duration_ms` | number | `null` |  |
| `rows_returned` | integer | `null` |  |

## `suites[].datasets[].checks[].timing` {#timingbreakdown}

Where time went, in milliseconds.

| Key | Type | Default | Description |
|---|---|---|---|
| `source_query_ms` | number | `0.0` |  |
| `target_query_ms` | number | `0.0` |  |
| `transfer_ms` | number | `0.0` |  |
| `compare_ms` | number | `0.0` |  |
| `render_ms` | number | `0.0` |  |
| `total_ms` | number | `0.0` |  |

## `suites[].datasets[].checks[].error` {#errorinfo}

An execution error, with a remediation hint when one is known.

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | string | **required** |  |
| `message` | string | **required** |  |
| `traceback` | string | `null` |  |
| `hint` | string | `null` |  |

## `suites[].datasets[].checks[].waiver` {#waiverapplied}

A waiver that matched a check: it turned a failure into an accepted difference, or it has
expired and the failure stands.

| Key | Type | Default | Description |
|---|---|---|---|
| `selector` | string | **required** |  |
| `column` | string | `null` |  |
| `reason` | string | **required** |  |
| `ticket` | string | `null` |  |
| `expires` | string | `null` |  |
| `approved_by` | string | `null` |  |
| `expired` | boolean | `false` |  |

## `suites[].datasets[].schema_diff[].source_type.fields[]` {#structfield}

A named field of a `STRUCT` type.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | **required** |  |
| `type` | [object](#canonicaltype) | **required** |  |
