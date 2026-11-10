# Configuration reference

!!! note "Generated page"
    This page is generated from the configuration models and the component registry by `uv run nox -s docs_reference`. Do not edit it by hand; change the source and regenerate.

Every key a dvf configuration file may contain (configuration version 1), with its
type, default, allowed values, and limits. [Configuration](../configuration.md) explains the file
formats, layering, interpolation, and inheritance; this page is the exhaustive list.

- **Key** is the name in the file. Unknown keys are errors, with a "did you mean" suggestion.
- **Type** links to the section describing an object. "one of" lists the allowed values.
- **Default** is used when the key is omitted; **required** keys have none. Table and suite
  settings left out inherit from `defaults` (see [Inheritance](../configuration.md#inheritance)).
- Numbers, booleans, and strings with a pattern may also be written as a `${env:...}`,
  `${var:...}`, or `${secret:...}` placeholder; it is resolved before validation.

The same information drives editor autocomplete: `dvf schema config` writes the JSON Schema, and
`dvf init` saves it next to a new configuration.

## Top level {#dvfconfig}

| Key | Type | Default | Description |
|---|---|---|---|
| `version` | integer | **required** | Configuration format version. Use `1`. |
| `project` | [object](#projectconfig) | **required** | Project name, description, owner, and metadata. |
| `vars` | mapping | `{}` | Variables available as `${var:name}`. |
| `defaults` | [object](#defaultsconfig) | see section | Settings inherited by every suite and table. |
| `connections` | mapping of [connection](#connection-types) (by `type`) | `{}` |  |
| `suites` | list of [object](#suiteconfig) | `[]` |  |
| `waivers` | list of [object](#waiverconfig) | `[]` | Accepted, documented differences that do not fail the run until they expire. |
| `reports` | [object](#reportsconfig) | see section | Report formats, output folder, branding, and extracts. |
| `history` | [object](#historyconfig) | see section |  |
| `sinks` | list of [object](#sinkconfig) | `[]` | Result sinks that receive every run's results as tables. |
| `notifications` | list of [notification](#notification-types) (by `type`) | `[]` |  |
| `lineage` | [object](#lineageconfig) | see section | OpenLineage run events with data quality facets. |
| `telemetry` | [object](#telemetryconfig) | see section | OpenTelemetry traces and metrics of dvf itself. |
| `triage` | [object](#triageconfig) | see section | The AI-assisted triage plugin interface. |
| `plugins` | [object](#pluginsconfig) | see section | Project-local plugin files and modules; loading them executes their code. |
| `extends` | list of string | `[]` | Files merged beneath this one (applied by the loader). |
| `profiles` | mapping of mapping | `{}` | Named overlays selected with `--profile` (applied by the loader). |

## `project` {#projectconfig}

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty | **required** | Project name, recorded with every run; run history is kept per project. |
| `description` | string | `null` | Free-text description of the project. |
| `owner` | string | `null` | Team or person responsible for the project. |
| `metadata` | mapping | `{}` | Free-form metadata copied into every report (release, environment, approver). |

## `defaults` {#defaultsconfig}

Settings inherited by suites and tables.

Also used at: `suites[].defaults`.

| Key | Type | Default | Description |
|---|---|---|---|
| `engine` | string; matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$` | `auto` | Where row-level comparisons run: `auto`, `pushdown`, `duckdb`, `polars`, `spark`, or an engine added by a plugin. |
| `severity` | one of `info`, `warning`, `error`, `critical` | `error` | Severity of checks that set none. |
| `sample_size` | integer; >= 0, <= 10000 | `50` | Failing rows captured per check as evidence (masked by privacy settings). |
| `normalization` | [object](#normalizationconfig) | see section | Canonicalization settings inherited by suites and tables. |
| `privacy` | [object](#privacyconfig) | see section | Masking of personal data in samples, reports, and extracts. |
| `execution` | [object](#executionconfig) | see section | Concurrency, timeouts, retries, local engine resources, and checkpoints; read from the project-level `defaults` only. |
| `comparison` | [object](#comparisonconfig) | see section | Source-vs-target comparison settings inherited by suites and tables. |
| `schema_checks` | list of one of `object_exists`, `columns_present`, `type_compatibility`, `length_precision`, `nullability`, `constraints`, `physical_design`, `column_order`, `collation`, `object_inventory`, `schema_drift`, `contract_conformance`, `nested_schema`, `naming_standards` | `["object_exists", "columns_present", "type_compatibility", "length_precision", "nullability"]` | Schema checks run for comparison tables; single-dataset suites report `object_exists` only. |
| `schema_options` | [object](#schemaoptionsconfig) | see section | Naming rules (SCH-14) and the drift baseline (SCH-11). |

## `suites[]` {#suiteconfig}

A group of tables validated together, usually one source and one target connection.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | **required** | Suite name, part of every check ID: letters, digits, underscores, and hyphens. |
| `description` | string | `null` | Free-text description of the suite. |
| `tags` | list of string | `[]` | Labels for selecting with `dvf run --only tag:NAME`, added to every check result of the suite. |
| `owner` | string | `null` | Team or person responsible for the suite; the default owner of its tables' checks. |
| `enabled` | boolean | `true` | Set to false to skip every table of this suite. |
| `source` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | **required** | Connection name of the source (or the only) dataset. |
| `target` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Connection name of the target; omit for single-dataset data quality suites. |
| `defaults` | [object](#defaultsconfig) | see section | Effective defaults for this suite's tables (project defaults with suite overrides). |
| `tables` | list of [object](#tableconfig) | `[]` |  |

## `waivers[]` {#waiverconfig}

An accepted, documented difference that should not fail the run until it expires.

| Key | Type | Default | Description |
|---|---|---|---|
| `check` | string; not empty | **required** | Check ID selector; glob patterns allowed (`suite.table.*`). |
| `column` | string | `null` | Waive differences in this column only (glob patterns allowed). A column comparison is waived only when every differing column is waived. |
| `reason` | string; not empty | **required** | Why the difference is accepted; recorded with the waived result. |
| `ticket` | string | `null` | Change request or issue reference, such as `CR-1042`. |
| `approved_by` | string | `null` | Who approved the waiver. |
| `expires` | date (`YYYY-MM-DD`) | `null` | Expired waivers fail the run so they are reviewed. |

## `reports` {#reportsconfig}

| Key | Type | Default | Description |
|---|---|---|---|
| `formats` | list of string; at least one item | `["json", "html"]` | Report formats, such as `json`, `html`, `junit`, `markdown`, or `excel`. `json` is always written, and `dvf run --format` replaces this list. |
| `output_dir` | string | `./reports/${run.date}/${run.id}` | Folder for reports and extracts, relative to the configuration. `${run.*}` placeholders are resolved when the run starts; `--output-dir` overrides it. |
| `branding` | [object](#brandingconfig) | see section | Title, logo, and accent color of the reports. |
| `extracts` | [object](#extractsconfig) | see section | Full mismatch extracts written next to the reports for failed checks. |
| `templates_dir` | string | `null` | Folder of Jinja2 templates overriding the built-in report templates. |
| `include_passed` | boolean | `true` | List passed checks in the detailed report listings; set to false to list only the other checks. |
| `max_samples_per_check` | integer; >= 0 | `50` | Most sample rows shown per check or column in the reports. |

## `history` {#historyconfig}

Run history used for trends, baselines, and run-to-run comparison.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | Record each run in the history store and compare it with the baseline. `--no-history` turns history off for one run. |
| `store` | string | `duckdb:///./.dvf/history.duckdb` | History database URL: `duckdb:///path` or `sqlite:///path`. Relative paths are relative to the configuration's folder. |
| `baseline` | string | `last_passed` | `last_run`, `last_passed`, `none`, or a specific run ID. |
| `retention_runs` | integer; > 0 | `null` | Keep only this many newest runs (history rows and checkpoint folders); omit to keep every run. |

## `sinks[]` {#sinkconfig}

Where run and check results are written as tables, for dashboards.

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | string; matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$` | **required** | Sink type, for example `duckdb`, `sqlite`, or `postgres`. |
| `enabled` | boolean | `true` | Set to false to stop writing to this sink without removing it. |
| `path` | string | `null` | Database file for file-based sinks (relative to the configuration). |
| `url` | secret string | `null` | Connection URL for server sinks. Treated as a secret. |
| `schema` | string | `null` | Schema for the result tables, created when missing; the `sqlite` sink does not support it. |
| `table_prefix` | string; matching `^[A-Za-z_][A-Za-z0-9_]*$` | `dvf_` | Prefix of the result table names: `<prefix>runs`, `<prefix>checks`, and `<prefix>column_mismatches`. |
| `options` | mapping | `{}` | Sink settings: `batch_size` (rows per insert, default 1,000) and, for `postgres`, extra `psycopg.connect` arguments such as `sslmode`. |

## `lineage` {#lineageconfig}

OpenLineage run events with data quality facets (off by default).

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Send OpenLineage events for each run. |
| `namespace` | string; not empty | `dvf` | Namespace of dvf's jobs. |
| `job_name` | string | `null` | Name of the run's job; defaults to the project name. |
| `granularity` | one of `run`, `table` | `run` | `run`: one job per run, with every dataset as an input. `table`: also one child job per table (`<job_name>.<suite>.<table>`), linked to the run with the parent facet. |
| `transport` | [object](#lineagetransportconfig) | see section | Where the events are sent: the console (the default), a file, or an HTTP backend. |
| `dataset_namespaces` | mapping of string | `{}` | Dataset namespace per connection name, replacing the one dvf derives from the connection (for example `postgres://db.example.com:5432`). |
| `fail_on` | list of one of `passed`, `warning`, `failed`, `error`, `skipped`, `waived` | `["error"]` | Run statuses sent as `FAIL` events; other finished runs are `COMPLETE` (the assertions carry the check results) and interrupted runs are `ABORT`. |

## `telemetry` {#telemetryconfig}

OpenTelemetry traces and metrics of dvf itself (`dvf-framework[otel]`, off by default).

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Record OpenTelemetry traces and metrics. Needs `dvf-framework[otel]`; without it, dvf logs a warning and records nothing. |
| `exporter` | one of `global`, `console` | `global` | `global` uses the tracer and meter providers configured in the process (for example by `opentelemetry-instrument` from `OTEL_*` variables). `console` prints spans and metrics to standard error, for debugging. |
| `service_name` | string | `dvf` | `service.name` of the providers dvf creates for the `console` exporter. |
| `metrics` | boolean | `true` | Also record metrics (rows per second, query time per side, retries, check counts). |

## `triage` {#triageconfig}

The AI-assisted triage plugin interface (off by default).

When enabled, plugins implementing the `triage_failures` hook receive masked, aggregated
summaries of the run's failures: statuses, counts, and mismatch classes, never values.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Opt in to calling triage plugins. |
| `max_checks` | integer; > 0, <= 500 | `25` | Failed and errored checks included in the summary (the worst first). |
| `max_columns_per_check` | integer; > 0, <= 500 | `20` | Column mismatch summaries included per check. |

## `plugins` {#pluginsconfig}

Project-local plugins. Loading them executes their code.

| Key | Type | Default | Description |
|---|---|---|---|
| `paths` | list of string | `[]` | Python files or folders, relative to the config file. |
| `modules` | list of string | `[]` | Importable module names. |

## `defaults.normalization` {#normalizationconfig}

How values are canonicalized before comparison or hashing.

Also used at: `suites[].tables[].normalization`.

| Key | Type | Default | Description |
|---|---|---|---|
| `trim` | one of `none`, `rtrim`, `ltrim`, `both` | `none` | Whitespace trimming. `rtrim` suits blank-padded CHAR columns. |
| `case_sensitive` | boolean | `true` | Set to false to compare text case-insensitively (both sides are lower-cased). |
| `null_equals_empty` | boolean | `false` | Treat NULL and empty string as equal. |
| `unicode_form` | one of `NFC`, `NFD`, `NFKC`, `NFKD`, `none` | `NFC` | Unicode normalization applied to text, or `none`. Databases without a normalization function compare text without it, and the result says so. |
| `collapse_whitespace` | boolean | `false` | Replace each run of whitespace in text with a single space. |
| `timestamp_precision` | one of `s`, `ms`, `us`, `ns` | `us` | Timestamps are truncated to this precision on both sides. |
| `timezone` | string | `UTC` | Time zone that timestamps without an offset are assumed to be in. |
| `decimal_scale` | `auto` or integer; >= 0 | `auto` | Fixed scale for decimals, or `auto` to use the larger scale of the two sides. |
| `float_compare` | [object](#floatcompare) | see section | Tolerance for floating-point values, which are compared numerically and never hashed. |

## `defaults.privacy` {#privacyconfig}

Protection of personal data in samples and reports.

| Key | Type | Default | Description |
|---|---|---|---|
| `mask_pii` | boolean | `true` | Mask values in columns tagged or detected as personal data. |
| `mode` | one of `standard`, `strict` | `standard` | `strict` keeps every raw value out of results, reports, extracts, repro files, and notifications: no samples or extracts, literals removed from recorded SQL, minimum and maximum values hidden. |
| `pii_columns` | list of string | `[]` | Column names (glob patterns allowed) always treated as personal data. |
| `masking` | one of `mask`, `hash`, `tokenize` | `mask` | How protected sample values are written: `mask` (`***`, keeping a hint such as `j***@example.com`), `hash` (keyed HMAC-SHA256 digest, equal values give equal digests), or `tokenize` (`tok_000042`, consistent within a run). |
| `detect_patterns` | boolean | `true` | Also protect values that look like email addresses, phone numbers, national IDs, payment card numbers, or IBANs, in any column. |
| `hash_key` | secret string | `null` | Key for `masking: hash` (use a `${secret:...}` reference). Without it, a random key per run is used, so digests cannot be compared across runs. |

## `defaults.execution` {#executionconfig}

Concurrency, timeouts, retries, local engine resources, and checkpoints.

| Key | Type | Default | Description |
|---|---|---|---|
| `max_parallel_tables` | integer; > 0 | `4` | Tables validated at the same time (project-level setting). |
| `max_queries_per_connection` | integer; > 0 | `2` | Default cap on tables using one connection at the same time; a connection's own `max_parallel_queries` overrides it. |
| `query_timeout_s` | integer; > 0 | `1800` | Query timeout in seconds for connections that set no `query_timeout_s` of their own. |
| `retries` | integer; >= 0 | `2` | Retries of a query that failed with a transient error (lost connection, deadlock, timeout). |
| `retry_backoff_s` | number; >= 0 | `2.0` | First retry delay in seconds; it doubles on each retry. |
| `memory_limit` | string; matching `^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$` | `null` | Memory limit of the local DuckDB engine, such as `4GB`. It spills to disk beyond it. |
| `threads` | integer; > 0 | `null` | Threads of the local DuckDB engine (default: all cores). |
| `temp_dir` | string | `null` | Folder for the local engine's spill files (default: the system temporary folder). |
| `checkpoints` | boolean | `true` | Save finished tables and partitions so `dvf run --resume RUN_ID` can continue a run. |
| `state_dir` | string | `.dvf` | Folder (relative to the configuration) for checkpoints, run results, and history. |

## `defaults.comparison` {#comparisonconfig}

Source-vs-target comparison settings.

Also used at: `suites[].tables[].comparison`.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | Set to false to skip the comparison strategies; schema checks and the table's checks still run. |
| `strategies` | list of string; at least one item | `["row_count", "aggregate"]` | Comparison strategies, cheapest first. They escalate: count, aggregate, then hash. |
| `on_mismatch` | string; matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$` | `full_diff` | Strategy used to explain mismatches found by the strategies above (or none). |
| `buckets` | integer; > 0, <= 1000000 | `256` | Number of first-level hash buckets for `hash_bucket`. |
| `drilldown_fanout` | integer; >= 2, <= 1024 | `16` | Sub-buckets (`hash_bucket`) or key segments (`bisect`) per mismatched bucket or segment. |
| `drilldown_threshold` | integer; > 0 | `10000` | Stop drilling down once the mismatched buckets or segments hold at most this many rows, and fetch their row hashes. |
| `partitions` | [object](#partitionconfig) | `null` | Compare row-level strategies partition by partition. |
| `group_by` | list of string | `[]` | Columns for grouped counts and control totals (for example company code, period). |
| `aggregates` | list of one of `count`, `sum`, `min`, `max`, `avg`, `count_distinct`, `null_count`, `sum_length` | `["count", "sum", "min", "max", "null_count"]` | Aggregates the `aggregate` strategy compares per column, overall or per `group_by` group. |
| `tolerance` | [object](#toleranceconfig) | see section | Differences accepted as equal; a column mapping's `tolerance` replaces it for that column. |
| `sample` | [object](#sampleconfig) | `null` | Sample size and strata of the `sample` strategy (REC-07). Without it, the strategy sizes the sample for a margin of error of one percentage point. |
| `max_mismatch_rows` | integer; > 0 | `100000` | Upper bound on mismatched rows fetched for diagnosis. |

## `defaults.schema_options` {#schemaoptionsconfig}

Settings of the schema checks that need more than the check name.

Also used at: `suites[].tables[].schema_options`.

| Key | Type | Default | Description |
|---|---|---|---|
| `naming` | [object](#namingconfig) | see section |  |
| `drift` | [object](#driftconfig) | see section | The baseline compared by `schema_drift` (SCH-11). |

## `suites[].tables[]` {#tableconfig}

One table (or dataset pair) to validate, or a pattern selecting many.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Table name, part of every check ID: letters, digits, underscores, and hyphens. Set exactly one of `name` and `pattern`. |
| `pattern` | [object](#patternspec) | `null` | Glob patterns selecting many tables, each validated with this entry's settings. Set exactly one of `name` and `pattern`. |
| `description` | string | `null` | Free-text description of the table. |
| `tags` | list of string | `[]` | Labels for selecting with `dvf run --only tag:NAME`, added to every check result of the table. |
| `owner` | string | `null` | Team or person responsible for the table; the default owner of its checks. |
| `enabled` | boolean | `true` | Set to false to skip this table. |
| `source` | [object](#datasetspec) | `null` | Source dataset (the only one in single-dataset suites). A named table needs one of `table`, `query`, or `path`; a pattern entry takes its tables from the pattern. |
| `target` | [object](#datasetspec) | `null` | Target dataset; required for named tables in comparison suites and not allowed in single-dataset suites. |
| `keys` | `auto` or list of string or [object](#keymapping) | `[]` | Key columns used to pair rows, or `auto` to discover primary keys. |
| `discover_keys` | boolean | `false` | Use the source dataset's primary key as the keys; `keys: auto` sets this. Do not combine it with explicit `keys`. |
| `columns` | [object](#columnsconfig) | see section | Which columns to compare and how source and target columns map. |
| `engine` | string; matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$` | `auto` | Where row-level comparisons run; inherited from the suite's defaults when omitted. |
| `severity` | one of `info`, `warning`, `error`, `critical` | `error` | Severity of checks that set none; inherited from the suite's defaults when omitted. |
| `sample_size` | integer; >= 0, <= 10000 | `50` | Failing rows captured per check as evidence; inherited from the suite's defaults when omitted. |
| `normalization` | [object](#normalizationconfig) | see section | Canonicalization settings, merged key by key over the suite's defaults. |
| `comparison` | [object](#comparisonconfig) | see section | Comparison settings, merged key by key over the suite's defaults. |
| `schema_checks` | list of one of `object_exists`, `columns_present`, `type_compatibility`, `length_precision`, `nullability`, `constraints`, `physical_design`, `column_order`, `collation`, `object_inventory`, `schema_drift`, `contract_conformance`, `nested_schema`, `naming_standards` | `["object_exists", "columns_present", "type_compatibility", "length_precision", "nullability"]` | Schema checks to run (comparison suites); inherited from the suite's defaults when omitted. |
| `schema_options` | [object](#schemaoptionsconfig) | see section | Naming rules (SCH-14) and the drift baseline (SCH-11), merged key by key over the suite's defaults. |
| `checks` | list of [check](#check-types) (by `type`) | `[]` |  |
| `details` | mapping | `{}` | Custom details copied into the table's results (for example requirement IDs). |
| `window` | [object](#windowconfig) | `null` | Validate only rows inside a watermark window (delta and incremental loads, MIG-02). |
| `in_flight` | [object](#inflightconfig) | `null` | Exclude rows changed shortly before a cutoff (live replication, MIG-09). |

## `reports.branding` {#brandingconfig}

| Key | Type | Default | Description |
|---|---|---|---|
| `title` | string | `null` | Report title; defaults to the project name. |
| `logo` | string | `null` | Logo image file, relative to the configuration; HTML reports embed it. |
| `primary_color` | string; matching `^#[0-9A-Fa-f]{6}$` | `null` | Accent color of the reports, as `#RRGGBB`. |

## `reports.extracts` {#extractsconfig}

Full mismatch extracts written next to the reports.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | Write extracts for failed checks. Strict privacy mode writes none even when this is on. |
| `format` | one of `parquet`, `csv` | `parquet` | File format of the extract files. |
| `max_rows_per_check` | integer; > 0 | `100000` | Most failing rows written to one check's extract. Fetched mismatched values are capped by `comparison.max_mismatch_rows` instead. |

## `lineage.transport` {#lineagetransportconfig}

Where OpenLineage events are sent.

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | one of `http`, `file`, `console` | `console` | `http` (an OpenLineage backend such as Marquez), `file` (JSON Lines), or `console` (standard error, for debugging). |
| `url` | string | `null` | `http`: the backend's base URL, for example `http://localhost:5000`. |
| `endpoint` | string | `api/v1/lineage` | `http`: the path events are posted to, relative to `url`. |
| `api_key` | secret string | `null` | `http`: sent as `Authorization: Bearer <api_key>`. Use a `${secret:...}` reference. |
| `timeout_s` | number; > 0, <= 300 | `5.0` | `http`: timeout of one request, in seconds. |
| `retries` | integer; >= 0, <= 10 | `2` | `http`: retries after a network error, HTTP 429, or a 5xx response. |
| `path` | string | `null` | `file`: the JSON Lines file events are appended to (relative to the configuration; `${run.*}` placeholders allowed). |

## `defaults.normalization.float_compare` {#floatcompare}

Tolerance for comparing floating-point values (they are never hashed directly).

| Key | Type | Default | Description |
|---|---|---|---|
| `abs_tol` | number; >= 0 | `0.0` | Absolute difference accepted between floating-point values; the larger of this and `tolerance.numeric_abs` applies. |
| `rel_tol` | number; >= 0 | `1e-09` | Difference accepted between floating-point values, relative to the larger magnitude. `tolerance.numeric_rel` replaces it when set. |

## `defaults.comparison.partitions` {#partitionconfig}

Split row-level comparisons into independent partitions.

Each partition is compared on its own (and checkpointed), and the partition results are
combined. Use exactly one of `values`, `ranges`, `hash_buckets`, or `predicates`.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | `null` | Paired column (target or source name) for `values` and `ranges`. |
| `values` | `auto` or list of any | `null` | One partition per value; `auto` reads the distinct values from the source. Rows with other values or NULL form one more partition. |
| `ranges` | list of any; at least one item | `null` | Ascending boundaries; partitions are below the first, between neighbors, from the last upward, and NULL. |
| `hash_buckets` | integer; > 0, <= 4096 | `null` | Split by a hash of the key columns into this many partitions. |
| `predicates` | list of [object](#partitionpredicate); at least one item | `null` | Named partitions given as a SQL predicate per side; make them cover every row. |
| `max_partitions` | integer; > 0, <= 100000 | `1000` | Most partitions allowed, not counting the extra partitions for other values and NULL; more is an error. |

## `defaults.comparison.tolerance` {#toleranceconfig}

Differences that are accepted as equal.

Also used at: `suites[].tables[].columns.mappings[].tolerance`.

| Key | Type | Default | Description |
|---|---|---|---|
| `numeric_abs` | number; >= 0 | `null` | Accepted absolute difference between numeric values; sums may differ by it once per row. |
| `numeric_rel` | number; >= 0 | `null` | Accepted difference between numeric values relative to the larger one, such as `0.001` for 0.1 percent. |
| `timestamp` | string; matching `^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$` | `null` | Accepted difference between dates or timestamps, such as `1s`. |
| `row_count_pct` | number; >= 0, <= 100 | `null` | Accepted row count difference, as a percentage of the source count. |

## `defaults.comparison.sample` {#sampleconfig}

Statistical sample comparison (the `sample` strategy, REC-07) for tables too large to
compare in full.

Size the sample in exactly one way: `margin_of_error` (with `confidence`, by Cochran's
formula with the finite population correction), `fraction`, or `rows`. Rows are chosen
by a seeded hash of the canonical key, so both sides (in any database) select the same keys.

| Key | Type | Default | Description |
|---|---|---|---|
| `fraction` | number; > 0, <= 1 | `null` | Share of the keys to compare. |
| `rows` | integer; > 0 | `null` | Number of keys to compare (expected; the realized size varies slightly). |
| `margin_of_error` | number; > 0, < 1 | `null` | Wanted half-width of the confidence interval of the mismatch rate (`0.01` is plus or minus one percentage point); the sample size follows from Cochran's formula. |
| `confidence` | number; > 0, < 1 | `0.95` | Confidence level of the reported interval (and of `margin_of_error`). |
| `expected_rate` | number; > 0, < 1 | `0.5` | Mismatch rate assumed when sizing by `margin_of_error`; 0.5 is the most conservative. |
| `seed` | integer | `42` | Changes which keys are sampled; the same seed always selects the same keys. |
| `stratify_by` | list of string | `[]` | Paired columns whose values form strata, each sampled in proportion to its size. |
| `min_per_stratum` | integer; >= 0 | `0` | Expected sampled keys per stratum at least (small strata are oversampled and reweighted). |
| `max_strata` | integer; > 0, <= 100000 | `1000` | Most strata allowed; more is an error (stratify by fewer or coarser columns). |
| `max_mismatch_rate` | number; >= 0, <= 1 | `0.0` | The comparison passes when the estimated mismatch rate is at most this (0: no sampled row may differ). |

## `defaults.schema_options.naming` {#namingconfig}

Naming standards checked by `naming_standards` (SCH-14).

| Key | Type | Default | Description |
|---|---|---|---|
| `rules` | list of [object](#namingrule) | `[]` | Rules the checked names must follow; without any, `naming_standards` is skipped. |
| `side` | one of `source`, `target`, `both` | `target` | Which dataset's names are checked. |

## `defaults.schema_options.drift` {#driftconfig}

The baseline of `schema_drift` (SCH-11).

| Key | Type | Default | Description |
|---|---|---|---|
| `baseline` | string | `previous_run` | `previous_run` (the schema saved by the last run under the state folder) or the path of a committed snapshot file (JSON or YAML, relative to the configuration). |
| `side` | one of `source`, `target`, `both` | `both` | Which datasets are compared with their baseline. |
| `ignore_columns` | list of string | `[]` | Columns ignored by the comparison (glob patterns, case-insensitive). |
| `compare` | list of one of `columns`, `types`, `nullability`, `primary_key`, `order` | `["columns", "types", "nullability", "primary_key"]` | What counts as drift: added or removed columns, type changes, nullability changes, primary key changes, and (opt-in) column order changes. |

## `suites[].tables[].pattern` {#patternspec}

Glob patterns selecting many tables at once; tables are paired by name.

| Key | Type | Default | Description |
|---|---|---|---|
| `source` | string; not empty | **required** | Glob pattern (`*`, `?`) matched case-insensitively against the source connection's table names. |
| `target` | string | `null` | Target table name, with each `*` or `?` replaced by what it matched in the source name; defaults to the source table's name. |

## `suites[].tables[].source` {#datasetspec}

One side of a table comparison: a table, a query, or a file path.

Also used at: `suites[].tables[].target`.

| Key | Type | Default | Description |
|---|---|---|---|
| `connection` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Overrides the suite's connection for this side. |
| `table` | string | `null` | Table or view name, optionally qualified (`schema.table`). |
| `query` | string | `null` | A SQL query used as the dataset. |
| `path` | string | `null` | File or folder path, relative to a file connection's base path. |
| `filter` | string | `null` | SQL predicate applied to this side (scope and exclusions). |
| `as_of` | string | `null` | Time travel: read the dataset as of this timestamp or version, where supported. |
| `options` | mapping | `{}` | Connector-specific dataset settings, such as file reader options, NoSQL filters and projections, or RFC read settings. |

## `suites[].tables[].keys[]` {#keymapping}

A key column, optionally renamed and normalized on the target side.

| Key | Type | Default | Description |
|---|---|---|---|
| `source` | string | **required** | Source key column; also the target column unless `target` is set. |
| `target` | string | `null` | Target column name; defaults to the source name. |
| `normalize` | list of string or mapping or [object](#normalizerref) | `[]` | Normalizers applied to the key on both sides before rows are paired, for example `sap_alpha`. |

## `suites[].tables[].columns` {#columnsconfig}

Which columns to compare and how they map.

| Key | Type | Default | Description |
|---|---|---|---|
| `include` | string or list of string | `["*"]` | Columns to compare; `*` means all mapped columns. Glob patterns are allowed. |
| `exclude` | list of string | `[]` | Columns left out of the comparison, by source or target name. Glob patterns are allowed, and exclusions win over `include`. |
| `mappings` | list of [object](#columnmapping) | `[]` | Renamed, derived, or normalized columns, with optional per-column tolerances and PII tags. |
| `match` | one of `exact`, `case_insensitive` | `case_insensitive` | How unmapped source and target column names are paired. |

## `suites[].tables[].window` {#windowconfig}

Compare only rows inside a watermark window, for delta and incremental loads (MIG-02).

Each side keeps the rows with `start <= column < end` (`end_inclusive` makes the end
inclusive). Bounds may use `${run.*}` and `${var:*}`; ISO dates and timestamps become
date and timestamp literals (offsets are converted to UTC), and numbers stay numbers, so
integer watermarks such as change sequence numbers work too.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | `null` | Watermark column on both sides. |
| `source_column` | string | `null` | Watermark column on the source, if it differs from `column`. |
| `target_column` | string | `null` | Watermark column on the target; defaults to `column`, then `source_column`. |
| `start` | date-time (ISO 8601) or date (`YYYY-MM-DD`) or integer or number or string | `null` | Inclusive lower bound. |
| `end` | date-time (ISO 8601) or date (`YYYY-MM-DD`) or integer or number or string | `null` | Upper bound, exclusive unless `end_inclusive` is true. |
| `end_inclusive` | boolean | `false` | Make `end` inclusive (`start <= column <= end`). |

## `suites[].tables[].in_flight` {#inflightconfig}

Exclude rows that changed shortly before a cutoff, so data still moving through live
replication does not show as mismatches (MIG-09). Combine it with `as_of` time travel on
sources that support it.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | `null` | Change timestamp column on both sides. |
| `source_column` | string | `null` | Change timestamp column on the source, if it differs from `column`. |
| `target_column` | string | `null` | Change timestamp column on the target; defaults to `column`, then `source_column`. |
| `window` | string; matching `^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$` | **required** | Rows changed within this long before the cutoff, or after it, are excluded (`15m`). |
| `cutoff` | date-time (ISO 8601) or string | `null` | The cutoff instant: an ISO timestamp, `${run.*}`, or `${var:*}`. Defaults to the run's start. Values without an offset are in `normalization.timezone`; the column's values are assumed to be in that time zone too. |
| `exclude_keys_on_both_sides` | boolean | `true` | Also exclude, on the other side, the keys of rows excluded on either side, so a row that changed on one side only is not reported as missing or different (needs keys). |
| `max_keys` | integer; > 0, <= 1000000 | `10000` | Most in-flight keys excluded on both sides; more is an error (narrow the window). |

## `defaults.comparison.partitions.predicates[]` {#partitionpredicate}

A named partition given as a SQL predicate per side.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 64 characters, matching `^[A-Za-z0-9_.=-]+$` | **required** | Unique name of the partition: letters, digits, and `_.=-`. |
| `source` | string; not empty | **required** | SQL predicate selecting the partition's source rows. |
| `target` | string | `null` | Predicate on the target; defaults to the source predicate. |

## `defaults.schema_options.naming.rules[]` {#namingrule}

A naming standard for table or column names (schema check `naming_standards`).

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | `null` | Label shown in results; defaults to the pattern or preset. |
| `pattern` | string | `null` | Regular expression every name must match in full (Python syntax). |
| `preset` | one of `snake_case`, `upper_snake_case`, `camel_case`, `pascal_case` | `null` | A predefined pattern instead of `pattern`. |
| `max_length` | integer; > 0 | `null` | Longest allowed name, in characters. |
| `applies_to` | one of `tables`, `columns`, `all` | `all` | Which names the rule checks: table names, column names, or `all`. |
| `exclude` | list of string | `[]` | Names exempt from the rule (glob patterns, case-insensitive). |

## `suites[].tables[].keys[].normalize[]` {#normalizerref}

A normalizer applied to a column before comparison.

Also used at: `suites[].tables[].columns.mappings[].normalize[]`.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$` | **required** | Registered normalizer name, for example `rtrim` or `sap_alpha`. |
| `params` | mapping | `{}` | Normalizer parameters, for example `{digits: 2}` for `round`. |

## `suites[].tables[].columns.mappings[]` {#columnmapping}

How a target column relates to the source: renamed, derived, or normalized.

| Key | Type | Default | Description |
|---|---|---|---|
| `source` | string | `null` | Source column name. Set exactly one of `source` and `source_expr`. |
| `source_expr` | string | `null` | A source-side SQL expression the target column is derived from. |
| `target` | string | **required** | Target column name; each target column may be mapped only once. |
| `normalize` | list of string or mapping or [object](#normalizerref) | `[]` | Normalizers applied to this column on both sides before comparison, in order. |
| `compare` | boolean | `true` | Set to false to exclude this column from value comparison. |
| `tolerance` | [object](#toleranceconfig) | `null` | Tolerance for this column; replaces `comparison.tolerance`. |
| `pii` | boolean | `false` | Treat this column as personal data: its values are masked when `privacy.mask_pii` is on. |

## Connection types {#connection-types}

Each entry of `connections` is selected by its `type`. The connector guides explain how to connect: [Connectors](../connectors.md), [Files](../files.md), [NoSQL](../nosql.md), [SAP](../sap.md), and [Spark](../spark.md). Keep credentials in `${secret:...}` references.

### Common fields {#connection-common}

Every connection type accepts these fields.

| Key | Type | Default | Description |
|---|---|---|---|
| `description` | string | `null` | Free-text description of the connection. |
| `read_only` | boolean | `true` | dvf never writes to read-only connections. Keep this on for source and target systems. |
| `max_parallel_queries` | integer; > 0 | `null` | Upper bound on concurrent queries against this connection (protects production sources). |
| `query_timeout_s` | integer; > 0 | `null` | Per-query timeout in seconds; overrides `defaults.execution.query_timeout_s`. |
| `options` | mapping | `{}` | Driver-specific options passed through unchanged. |

### `bigquery` {#connection-bigquery}

Google BigQuery.

Status: **beta**; install: `pip install 'dvf-framework[bigquery]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `project` | string | **required** | Google Cloud project that runs the query jobs and qualifies dataset names given without one. |
| `dataset` | string | `null` | Default dataset for unqualified table names, as `dataset` or `project.dataset`. |
| `location` | string | `null` | Location of the query jobs, such as `US` or `europe-west3`; the client's default when omitted. |
| `credentials_path` | string | `null` | Service account key file; omit to use application default credentials. |

### `cassandra` {#connection-cassandra}

Apache Cassandra and compatible systems (ScyllaDB, DataStax Astra).

Apache Cassandra, ScyllaDB, or DataStax Astra through the DataStax Python driver.

Status: **beta**; install: `pip install 'dvf-framework[cassandra]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `contact_points` | list of string; at least one item | `["127.0.0.1"]` | Hosts to connect to first. |
| `port` | integer; >= 1, <= 65535 | `9042` | CQL native protocol port of the contact points; not used with `secure_connect_bundle`. |
| `keyspace` | string | `null` | Keyspace for table names without one. |
| `username` | string | `null` | User name for plain-text authentication. |
| `password` | secret string | `null` | Password of `username`. Use a `${secret:...}` reference; it is treated as a secret. |
| `local_datacenter` | string | `null` | Data center whose nodes serve reads (`DCAwareRoundRobinPolicy`). |
| `consistency_level` | one of `ANY`, `ONE`, `TWO`, `THREE`, `QUORUM`, `ALL`, `LOCAL_QUORUM`, `EACH_QUORUM`, `SERIAL`, `LOCAL_SERIAL`, `LOCAL_ONE` | `LOCAL_QUORUM` | Read consistency level. `LOCAL_QUORUM` avoids false mismatches from replicas that have not caught up; datasets may override it. |
| `secure_connect_bundle` | string | `null` | DataStax Astra secure connect bundle (zip file). |
| `ssl` | boolean | `false` | Use TLS with the system's default certificate store. |
| `protocol_version` | integer; >= 3, <= 6 | `null` | Native protocol version, from 3 to 6; the driver negotiates one when omitted. |
| `connect_timeout_s` | integer; > 0 | `10` | Seconds to wait for a connection to a node. |
| `fetch_size` | integer; > 0, <= 100000 | `1000` | Rows per page; datasets may override it with `options.batch_size`. |

### `cosmos` {#connection-cosmos}

Azure Cosmos DB for NoSQL.

Azure Cosmos DB for NoSQL (formerly the SQL API).

Status: **beta**; install: `pip install 'dvf-framework[cosmos]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `endpoint` | string | **required** | Account URI, for example `https://myaccount.documents.azure.com:443/`. |
| `database` | string | **required** | ID of the database holding the containers that datasets name. |
| `key` | secret string | `null` | Account key (or a resource token). Use a secret reference. |
| `credential` | one of `key`, `default` | `key` | `key` uses `key`; `default` uses Microsoft Entra ID through azure-identity's `DefaultAzureCredential`. |
| `consistency_level` | one of `Strong`, `BoundedStaleness`, `Session`, `ConsistentPrefix`, `Eventual` | `null` | Read consistency; the account default when omitted (it can only be relaxed). |
| `preferred_locations` | list of string | `[]` | Regions to read from, in order of preference. |
| `page_size` | integer; > 0, <= 100000 | `1000` | Items per page; datasets may override it with `options.batch_size`. |

### `databricks` {#connection-databricks}

Databricks SQL warehouses and clusters.

Databricks SQL warehouse or cluster through the SQL connector.

Status: **beta**; install: `pip install 'dvf-framework[databricks]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `server_hostname` | string | **required** | Host name from the SQL warehouse's or cluster's connection details; a pasted `https://` URL is accepted. |
| `http_path` | string | **required** | HTTP path from the SQL warehouse's or cluster's connection details. |
| `access_token` | secret string | `null` | Personal access token. Use a `${secret:...}` reference; it is treated as a secret. |
| `auth_type` | string | `null` | Another authentication type supported by the driver, such as `databricks-oauth` (browser sign-in), instead of `access_token`. |
| `catalog` | string | `null` | Initial catalog of the session, for unqualified table names. |
| `schema` | string | `null` | Initial schema of the session, for unqualified table names. |

### `dataframe` {#connection-dataframe}

In-process pandas, Polars, or Arrow data (dvf.validate_dataframe).

An in-process DataFrame passed through the Python API (`dvf.validate_dataframe`).

Created by dvf; not meant to be written in configuration files.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `handle` | string | **required** | Identifier of the DataFrame registered by the Python API for this run. |
| `label` | string | `dataframe` | Name of the DataFrame shown in results. |

### `db2` {#connection-db2}

IBM Db2.

A relational database reached by host and port or by URL.

Status: **declared**; install: `pip install 'dvf-framework[db2]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | `null` | A SQLAlchemy-style URL. Treated as a secret because it may contain a password. |
| `host` | string | `null` | Server host name, used when `url` is not set (one of the two is required). |
| `port` | integer; >= 1, <= 65535 | `null` | Server port; the connector's standard port when omitted (for example 5432 for `postgres`). |
| `database` | string | `null` | Database name (the service name for `oracle`). |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `driver` | string | `null` | ODBC driver name or driver variant, where the connector supports a choice. |

### `duckdb` {#connection-duckdb}

DuckDB database, in memory or on disk.

An embedded DuckDB database (in memory by default).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `database` | string | `:memory:` | Database file path, or `:memory:`. |
| `extensions` | list of string | `[]` | DuckDB extensions to load, for example `httpfs` or `spatial`. |

### `dynamodb` {#connection-dynamodb}

Amazon DynamoDB.

Amazon DynamoDB (or DynamoDB Local through `endpoint_url`).

Status: **beta**; install: `pip install 'dvf-framework[dynamodb]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `region` | string | `null` | AWS region, for example `eu-west-1`; the AWS configuration's default when omitted. |
| `endpoint_url` | string | `null` | Custom endpoint, for example `http://localhost:8000` for DynamoDB Local. |
| `profile` | string | `null` | Named profile from the AWS configuration files. |
| `aws_access_key_id` | secret string | `null` | Access key; omit to use the default credential chain (environment, profile, role). |
| `aws_secret_access_key` | secret string | `null` | Secret access key that goes with `aws_access_key_id`. Use a `${secret:...}` reference; it is treated as a secret. |
| `aws_session_token` | secret string | `null` | Session token for temporary credentials. Use a `${secret:...}` reference; it is treated as a secret. |
| `page_size` | integer; > 0 | `1000` | Items requested per page (DynamoDB returns at most 1 MB per page). |

### `files` {#connection-files}

CSV, Parquet, JSON, Avro, ORC, Excel, and more on local or cloud storage.

Files on a local disk or object store (S3, ADLS, GCS) read as datasets.

`options` hold reader options applied to every dataset of the connection (dataset
`options` override them), plus `extensions`: DuckDB extensions to load (for example
`httpfs`) instead of reading URLs through fsspec.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | **required** | Base folder or URL. Dataset `path` values are relative to it. |
| `format` | one of `csv`, `tsv`, `parquet`, `json`, `ndjson`, `avro`, `orc`, `excel`, `xml`, `fixed_width`, `delta`, `iceberg` | `null` | Default file format; inferred from the extension (or a Delta or Iceberg table folder) when omitted. |
| `storage_options` | mapping | `{}` | Storage options for URLs: passed to fsspec (file listing, checksums, converted formats, and DuckDB through a registered fsspec file system) and to deltalake for Delta tables. Credentials should be secret references. |

### `mongodb` {#connection-mongodb}

MongoDB.

MongoDB (4.4 or later; verified with 7.0).

Status: **ready**; install: `pip install 'dvf-framework[mongodb]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `uri` | secret string | **required** | Connection string. Treated as a secret because it usually contains credentials. |
| `database` | string | **required** | Database holding the collections that datasets name. |
| `read_preference` | one of `primary`, `primaryPreferred`, `secondary`, `secondaryPreferred`, `nearest` | `null` | Which replica set members serve reads; the connection string's setting (or `primary`) when omitted. Datasets may override it. |
| `read_concern` | one of `local`, `available`, `majority`, `linearizable`, `snapshot` | `null` | Read concern level (consistency); the server default when omitted. Datasets may override it. |
| `uuid_representation` | one of `standard`, `pythonLegacy`, `javaLegacy`, `csharpLegacy`, `unspecified` | `standard` | How binary UUIDs are decoded. `standard` reads subtype 4; choose a legacy value for subtype 3 UUIDs written by older drivers. |
| `server_selection_timeout_ms` | integer; > 0 | `10000` | How long to wait for a reachable server before failing. |
| `batch_size` | integer; > 0, <= 100000 | `1000` | Documents fetched per round trip; datasets may override it with `options.batch_size`. |

### `mssql` {#connection-mssql}

SQL Server, Azure SQL, Synapse, and Fabric SQL.

A relational database reached by host and port or by URL.

Status: **ready**; install: `pip install 'dvf-framework[mssql]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | `null` | A SQLAlchemy-style URL. Treated as a secret because it may contain a password. |
| `host` | string | `null` | Server host name, used when `url` is not set (one of the two is required). |
| `port` | integer; >= 1, <= 65535 | `null` | Server port; the connector's standard port when omitted (for example 5432 for `postgres`). |
| `database` | string | `null` | Database name (the service name for `oracle`). |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `driver` | string | `null` | ODBC driver name or driver variant, where the connector supports a choice. |

### `mysql` {#connection-mysql}

MySQL 8.0.17 or later.

A relational database reached by host and port or by URL.

Status: **ready**; install: `pip install 'dvf-framework[mysql]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | `null` | A SQLAlchemy-style URL. Treated as a secret because it may contain a password. |
| `host` | string | `null` | Server host name, used when `url` is not set (one of the two is required). |
| `port` | integer; >= 1, <= 65535 | `null` | Server port; the connector's standard port when omitted (for example 5432 for `postgres`). |
| `database` | string | `null` | Database name (the service name for `oracle`). |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `driver` | string | `null` | ODBC driver name or driver variant, where the connector supports a choice. |

### `odbc` {#connection-odbc}

Any database with an ODBC driver.

Any database with an ODBC driver.

`options` are passed to `pyodbc.connect` as keyword arguments (for example `timeout`,
the login timeout in seconds).

Status: **ready**; install: `pip install 'dvf-framework[odbc]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `dsn` | string | `null` | ODBC data source name. Set exactly one of `dsn` and `connection_string`. |
| `connection_string` | secret string | `null` | Full ODBC connection string, treated as a secret because it may contain a password. Set exactly one of `dsn` and `connection_string`. |
| `dialect` | string | `null` | dvf SQL dialect; detected from the driver's DBMS name when omitted. |
| `schema` | string | `null` | Schema of unqualified table names in catalog lookups (default: the session's). |

### `oracle` {#connection-oracle}

Oracle Database.

A relational database reached by host and port or by URL.

Status: **ready**; install: `pip install 'dvf-framework[oracle]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | `null` | A SQLAlchemy-style URL. Treated as a secret because it may contain a password. |
| `host` | string | `null` | Server host name, used when `url` is not set (one of the two is required). |
| `port` | integer; >= 1, <= 65535 | `null` | Server port; the connector's standard port when omitted (for example 5432 for `postgres`). |
| `database` | string | `null` | Database name (the service name for `oracle`). |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `driver` | string | `null` | ODBC driver name or driver variant, where the connector supports a choice. |

### `postgres` {#connection-postgres}

PostgreSQL.

A relational database reached by host and port or by URL.

Status: **ready**; install: `pip install 'dvf-framework[postgres]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | `null` | A SQLAlchemy-style URL. Treated as a secret because it may contain a password. |
| `host` | string | `null` | Server host name, used when `url` is not set (one of the two is required). |
| `port` | integer; >= 1, <= 65535 | `null` | Server port; the connector's standard port when omitted (for example 5432 for `postgres`). |
| `database` | string | `null` | Database name (the service name for `oracle`). |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `driver` | string | `null` | ODBC driver name or driver variant, where the connector supports a choice. |

### `redshift` {#connection-redshift}

Amazon Redshift.

A relational database reached by host and port or by URL.

Status: **beta**; install: `pip install 'dvf-framework[redshift]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | `null` | A SQLAlchemy-style URL. Treated as a secret because it may contain a password. |
| `host` | string | `null` | Server host name, used when `url` is not set (one of the two is required). |
| `port` | integer; >= 1, <= 65535 | `null` | Server port; the connector's standard port when omitted (for example 5432 for `postgres`). |
| `database` | string | `null` | Database name (the service name for `oracle`). |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `driver` | string | `null` | ODBC driver name or driver variant, where the connector supports a choice. |

### `sap_hana` {#connection-sap_hana}

SAP HANA at database level (user-installed hdbcli).

SAP HANA at database level (requires the user-installed `hdbcli` driver).

Status: **beta**; install: `pip install 'dvf-framework[sap-hana]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `host` | string | **required** | HANA host name. |
| `port` | integer; >= 1, <= 65535 | `30015` | SQL port: `3<instance>15` for single-container systems, `3<instance>13` (system database) or `3<instance>41` and up (tenants) for multitenant ones, 443 for HANA Cloud. |
| `user` | string | `null` | Database user; give it read privileges on the SAP schema only. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `schema` | string | `null` | The SAP schema (for example `SAPSR3` or `SAPHANADB`) for unqualified table names. |
| `client` | string; matching `^\d{3}$` | `null` | SAP client (MANDT), applied as a filter to client-dependent tables. |
| `encrypt` | boolean | `true` | Connect with TLS (hdbcli `encrypt`). |
| `database_name` | string | `null` | Tenant database to connect to on a multitenant system (hdbcli `databaseName`). |
| `ssl_validate_certificate` | boolean | `true` | Validate the server's TLS certificate when `encrypt` is on. |
| `client_column` | string | `null` | Name of the client column to filter on, for every table that has it. By default dvf finds it: the client-type key field from the dictionary (`ddic`), or else a first column named MANDT, CLIENT, RCLNT, or MANDANT. |
| `ddic` | boolean | `false` | Describe tables from the ABAP dictionary (DD02L, DD03L, DD04L, DD04T, DD05S, DD08L): ABAP types and lengths, keys, and field texts, instead of the HANA catalog alone. |
| `ddic_schema` | string | `null` | Schema of the dictionary tables, when it is not `schema`. |
| `ddic_language` | string; not empty, at most 1 characters | `E` | SAP language key of dictionary texts (`E` for English, `D` for German). |

### `sap_rfc` {#connection-sap_rfc}

SAP ECC and S/4HANA through RFC or OData (backends: pyrfc, custom_fm, odata; nwrfcsdk declared, not implemented).

SAP ECC or S/4HANA through RFC or OData, with a pluggable backend.

Status: **beta**; install: `pip install 'dvf-framework[sap-rfc]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `ashost` | string | `null` | Application server host (direct connection). |
| `mshost` | string | `null` | Message server host (load-balanced connection). |
| `sysnr` | string; matching `^\d{2}$` | `null` | Two-digit instance number of the application server, for direct logon with `ashost`. |
| `sysid` | string | `null` | System ID, for load-balanced connections through a message server. |
| `msserv` | string | `null` | Message server service or port, when it is not the default for `sysid`. |
| `group` | string | `null` | Logon group, for load-balanced connections. |
| `saprouter` | string | `null` | SAProuter string, when the system is reached through one. |
| `client` | string; matching `^\d{3}$` | **required** | SAP client to log on to. Client-dependent tables are read in this client. |
| `user` | string | `null` | SAP user to log on with (HTTP basic authentication for the `odata` backend). |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `lang` | string | `EN` | Logon language, such as `EN`; also used for dictionary texts and sent to OData services as `sap-language`. |
| `backend` | string | `pyrfc` | How dvf talks to SAP: `pyrfc` (RFC through the user-installed, no longer maintained PyRFC and NW RFC SDK), `custom_fm` (PyRFC with your own read function module), `odata` (OData v2 or v4 services and CDS views over HTTP), or a backend added by a plugin. `nwrfcsdk` (a direct SDK binding) is declared but not implemented. |
| `read_function` | string | `RFC_READ_TABLE` | Function module used to read tables. `custom_fm` needs your module here: it must take RFC_READ_TABLE's parameters and may return rows wider than 512 characters. |
| `row_width` | integer; > 0, <= 1000000 | `512` | Characters per data row of the read function: 512 for RFC_READ_TABLE; set your custom module's width for `custom_fm`. Wider tables are read in chunks of fields. |
| `delimiter` | string; at most 1 characters | `""` | Field delimiter passed to the read function. dvf cuts rows by the field offsets the function returns, so it is needed only by custom modules that require one. |
| `page_size` | integer; > 0, <= 10000000 | `50000` | Rows per call (RFC_READ_TABLE `ROWCOUNT`, OData `$top`). |
| `ddic_source` | one of `fieldinfo`, `dd03l` | `fieldinfo` | How tables are described: the function module DDIF_FIELDINFO_GET, or DD03L read with the read function (for systems where DDIF_FIELDINFO_GET is not released for RFC). |
| `foreign_keys` | boolean | `false` | Also read foreign keys (DD05S and DD08L) when describing tables. |
| `url` | string | `null` | OData service root for the `odata` backend, for example `https://host/sap/opu/odata/sap/API_BUSINESS_PARTNER`. |
| `odata_version` | one of `2`, `4` | `null` | OData protocol version; detected from the service metadata when unset. |
| `verify_tls` | boolean | `true` | Validate the TLS certificate of the OData service. |

### `snowflake` {#connection-snowflake}

Snowflake.

Status: **beta**; install: `pip install 'dvf-framework[snowflake]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `account` | string | **required** | Account identifier, such as `myorg-myaccount`. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`; with `authenticator: oauth`, the access token when `options.token` is not set. Use a `${secret:...}` reference; it is treated as a secret. |
| `authenticator` | string | `null` | For example `externalbrowser`, `oauth`, or `snowflake_jwt`. |
| `private_key_path` | string | `null` | Private key file for key pair authentication, relative to the configuration's folder. The authenticator then defaults to `SNOWFLAKE_JWT`. |
| `private_key_passphrase` | secret string | `null` | Passphrase of an encrypted private key. Use a `${secret:...}` reference; it is treated as a secret. |
| `warehouse` | string | `null` | Virtual warehouse that runs the queries. |
| `database` | string | `null` | Default database for unqualified table names. |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `role` | string | `null` | Role of the session. Use one with read-only grants: Snowflake has no read-only session mode. |

### `spark` {#connection-spark}

Apache Spark session: local, cluster, or Databricks Connect.

A Spark session: local, a cluster, or Databricks Connect.

Status: **ready**; install: `pip install 'dvf-framework[spark]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `master` | string | `null` | Spark master URL, for example `local[*]` or `spark://host:7077`. Omit to use the running session (a notebook or job), or a new `local[*]` session when none is running. |
| `app_name` | string | `dvf` | Application name of a session dvf starts. |
| `conf` | mapping of string | `{}` | Spark settings, such as `spark.sql.shuffle.partitions` or `spark.jars.packages`. Static settings apply only when dvf starts the session. |
| `databricks_connect` | boolean | `false` | Use a Databricks Connect session (`databricks-connect` installed instead of pyspark). |
| `catalog` | string | `null` | Current catalog of dvf's session, for unqualified table names. |
| `schema` | string | `null` | Current schema (database) of dvf's session, for unqualified table names. |

### `spark_dataframe` {#connection-spark_dataframe}

In-process Spark DataFrame (dvf.validate_dataframe).

An in-process DataFrame passed through the Python API (`dvf.validate_dataframe`).

Created by dvf; not meant to be written in configuration files.

Status: **ready**; install: `pip install 'dvf-framework[spark]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `handle` | string | **required** | Identifier of the DataFrame registered by the Python API for this run. |
| `label` | string | `dataframe` | Name of the DataFrame shown in results. |

### `sqlalchemy` {#connection-sqlalchemy}

Any database with a SQLAlchemy dialect.

Any database with a SQLAlchemy dialect.

`options` are passed to `sqlalchemy.create_engine` (for example `connect_args`).

Status: **ready**; install: `pip install 'dvf-framework[sqlalchemy]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | **required** | SQLAlchemy database URL, such as `dialect+driver://user:password@host/database`. Use a `${secret:...}` reference; it is treated as a secret. |
| `dialect` | string | `null` | dvf SQL dialect (`postgres`, `sqlite`, `mysql`, `mssql`, `oracle`, `duckdb`, ...); detected from the URL's SQLAlchemy dialect when omitted. |
| `schema` | string | `null` | Schema of unqualified table names in catalog lookups (default: the session's). |

### `sqlite` {#connection-sqlite}

SQLite database file.

A SQLite database file.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `database` | string | **required** | Database file path, or `:memory:`. |

### `teradata` {#connection-teradata}

Teradata Vantage.

A relational database reached by host and port or by URL.

Status: **declared**; install: `pip install 'dvf-framework[teradata]'`.

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | secret string | `null` | A SQLAlchemy-style URL. Treated as a secret because it may contain a password. |
| `host` | string | `null` | Server host name, used when `url` is not set (one of the two is required). |
| `port` | integer; >= 1, <= 65535 | `null` | Server port; the connector's standard port when omitted (for example 5432 for `postgres`). |
| `database` | string | `null` | Database name (the service name for `oracle`). |
| `schema` | string | `null` | Default schema for unqualified table names. |
| `user` | string | `null` | Login name. |
| `password` | secret string | `null` | Password of `user`. Use a `${secret:...}` reference; it is treated as a secret. |
| `driver` | string | `null` | ODBC driver name or driver variant, where the connector supports a choice. |

## Check types {#check-types}

Each entry of a table's `checks` list is selected by its `type`. [Checks and comparisons](../validations.md) explains what each one verifies; [Migration validations](../migration.md) and [Files](../files.md) cover the pack checks.

### Common fields {#check-common}

Every check type accepts these fields.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Optional name; becomes part of the check ID. Required when a table has two checks of the same type and columns. |
| `description` | string | `null` | Free-text description of what the check verifies. |
| `severity` | one of `info`, `warning`, `error`, `critical` | `null` | Inherited from the table when omitted. |
| `side` | one of `source`, `target`, `both` | `null` | Dataset to check: `source`, `target`, or `both`. Defaults to `target` for comparison suites and `source` otherwise. |
| `mostly` | number; > 0, <= 1 | `1.0` | Pass when at least this share of rows satisfies the check (1.0 means every row). |
| `where` | string | `null` | SQL predicate restricting the rows checked. |
| `tags` | list of string | `[]` | Labels for selecting checks with `dvf run --only tag:NAME`. Results carry them together with the table's and the suite's tags. |
| `owner` | string | `null` | Team or person responsible for the check, reported with its results. Defaults to the table's owner, then the suite's. |
| `enabled` | boolean | `true` | Set to false to skip this check without removing it. |
| `details` | mapping | `{}` | Custom details copied into every result of this check (for example requirement IDs). |
| `sample_size` | integer; >= 0, <= 10000 | `null` | Failing rows this check captures as evidence; `0` disables its samples. Defaults to the table's `sample_size`. |
| `action` | one of `fail`, `warn`, `drop`, `quarantine` | `null` | In-pipeline mode (`dvf.validate_dataframe`): what happens to rows failing this check. `quarantine` moves them to the quarantine frame, `drop` removes them, `warn` keeps them and caps the result at a warning, and `fail` keeps them and raises when the check fails. Defaults to the call's `on_fail`. `dvf run` reports failures by severity and ignores it. |

### `allowed_values` {#check-allowed_values}

Values must belong to a fixed set.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** | Column to check. |
| `values` | list of string or integer or number or boolean; at least one item | **required** | Accepted values. They are compared as text unless all of them are numbers or all are booleans. |
| `case_sensitive` | boolean | `true` | Set to false to compare text values case-insensitively. |

### `audit_columns` {#check-audit_columns}

Audit and lineage columns are populated and consistent (MIG-07).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `load_timestamp` | string | `null` | Load timestamp column: it must be set, at most `max_future` ahead of now, and not before `not_before`. |
| `batch_id` | string | `null` | Batch ID column: it must be set, and `max_batches` and `allowed_batches` may limit its values. |
| `source_system` | string | `null` | Source system column: it must be set, and `allowed_source_systems` may limit its values. |
| `not_before` | date-time (ISO 8601) or date (`YYYY-MM-DD`) or string | `null` | Load timestamps must be at or after this (for example the load window start). |
| `max_future` | string; matching `^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$` | `5m` | Load timestamps may be at most this far in the future (clock skew). |
| `max_batches` | integer; > 0 | `null` | Most distinct batch IDs (`1`: one batch per load). |
| `allowed_batches` | list of string or integer | `[]` | Accepted batch IDs, compared as trimmed, case-insensitive text; needs `batch_id`. |
| `allowed_source_systems` | list of string | `[]` | Accepted source systems, compared as trimmed, case-insensitive text; needs `source_system`. |

### `bad_records` {#check-bad_records}

Unreadable and malformed records (CSV parse and quoting errors, invalid JSON lines, fixed-width length errors, unconvertible values, corrupt files) must not exceed a limit.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `max_count` | integer; >= 0 | `null` | Largest number of bad records accepted. With neither this nor `mostly`, any bad record fails the check. |

### `baseline_metrics` {#check-baseline_metrics}

Current metrics match a named pre-migration baseline within tolerances (MIG-14).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `baseline` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | **required** | Name given to `dvf baseline capture --name`. |
| `metrics` | list of one of `row_count`, `null_count`, `count_distinct`, `sum`, `min`, `max`, `groups`; at least one item | `["row_count", "null_count", "sum"]` | Metrics compared with the baseline; `groups` compares the row counts per `comparison.group_by` group. |
| `columns` | list of string | `[]` | Baseline columns to compare (default: every captured column). |
| `tolerance` | [object](#baselinetolerance) | see section | Accepted differences between the current metrics and the baseline. |
| `baseline_table` | string | `null` | The `suite.table` entry of the baseline to compare with (default: this table). |

#### BaselineTolerance {#baselinetolerance}

Used by check `baseline_metrics`: tolerance.

Accepted differences from the baseline.

| Key | Type | Default | Description |
|---|---|---|---|
| `row_count_pct` | number; >= 0, <= 100 | `0.0` | Accepted row count difference, as a percentage of the baseline (also for groups). |
| `numeric_abs` | number; >= 0 | `0.0` | Accepted absolute difference of sums. |
| `numeric_rel` | number; >= 0 | `0.0` | Accepted relative difference of sums. |

### `cdc_applied` {#check-cdc_applied}

The latest change of every key in a CDC change table is reflected in the dataset (MIG-02).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `changes` | [object](#changetableref) | **required** | The CDC change table: where it is, and its key, operation, and sequence columns. |
| `key_columns` | list of string | `[]` | Key columns of the checked dataset; defaults to the table's keys on that side. |
| `insert_values` | list of string | `["i", "insert", "c", "create", "r", "read"]` | Operation codes of inserts (case-insensitive). |
| `update_values` | list of string | `["u", "update"]` | Operation codes of updates (case-insensitive). |
| `delete_values` | list of string | `["d", "delete"]` | Operation codes of deletes (case-insensitive). |
| `soft_delete` | [object](#flagconfig) | `null` | How the checked dataset marks deleted rows; deleted keys may then remain, flagged. |
| `compare_columns` | list of [object](#changecolumn) | `[]` | After-image columns whose latest values must equal the dataset's (compared as text). |

#### ChangeTableRef {#changetableref}

Used by check `cdc_applied`: changes.

A CDC change table: one row per change, with key, operation, and ordering columns.

| Key | Type | Default | Description |
|---|---|---|---|
| `connection` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Connection holding the dataset; defaults to the checked dataset's connection. |
| `table` | string | `null` | Table or view name, optionally qualified (`schema.table`). Set exactly one of `table` and `query`. |
| `query` | string | `null` | SQL query used as the dataset. Set exactly one of `table` and `query`. |
| `filter` | string | `null` | SQL predicate applied to this dataset. |
| `key` | string or list of string | **required** | Key columns of the changed record, in the order of the checked dataset's key. |
| `operation` | string | **required** | Column holding the operation code. |
| `sequence` | string or list of string | **required** | Columns ordering the changes of one key (for example commit LSN, then sequence). |

#### FlagConfig {#flagconfig}

Used by check `cdc_applied`: soft_delete.

A marker such as a soft-delete or current-row flag: a column holding one of `values`,
or a SQL boolean expression.

Also used at: check `delete_propagation`: source_flag, check `delete_propagation`: target_flag, check `scd2`: current_flag.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | `null` | Column holding the flag; it is set when the column holds one of `values`. Set exactly one of `column` and `expression`. |
| `values` | list of string or integer or boolean; at least one item | `[true]` | Values that mean the flag is set (compared as trimmed, case-insensitive text). |
| `expression` | string | `null` | A SQL condition that is true when the flag is set, for example `deleted_at IS NOT NULL`. |

#### ChangeColumn {#changecolumn}

Used by check `cdc_applied`: compare_columns[].

A column of the change table's after-image compared with the checked dataset.

| Key | Type | Default | Description |
|---|---|---|---|
| `change` | string | **required** | Column of the change table's after-image. |
| `target` | string | `null` | Column of the checked dataset; defaults to the same name. |

### `charset_conversion` {#check-charset_conversion}

Text survived the character set conversion: no U+FFFD replacement characters, mojibake, or control characters, and every value fits the legacy code page (MIG-10).

Text survived the character set conversion: no replacement characters, mojibake, or
control characters, and every value fits the legacy code page (MIG-10).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of string; at least one item | **required** | Columns to check (at least one). |
| `code_page` | string | `null` | Legacy code page values must round-trip through, such as `cp037` (EBCDIC), `cp500`, `latin-1`, or `cp1252`. |
| `rules` | list of one of `replacement_char`, `mojibake`, `control_chars`, `round_trip` | `null` | Rules to apply; defaults to all, with `round_trip` only when `code_page` is set. |
| `allowed_control` | list of string | `["\t", "\n", "\r"]` | Control characters that are acceptable. |
| `mojibake_patterns` | list of string | `[]` | Extra substrings that mark mojibake, in addition to the built-in detection. |
| `unicode_form` | one of `none`, `NFC`, `NFKC` | `none` | Normalize values before the round trip (decomposed accents do not fit most code pages). |
| `max_distinct` | integer; > 0 | `10000000` | Most distinct values examined per column; the rest are reported as not checked. |

### `contract` {#check-contract}

Schema conforms to an ODCS v3 contract or an expected-schema file (SCH-12).

The dataset must conform to an ODCS v3 contract or an expected-schema file (SCH-12).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `contract` | string; not empty | **required** | Contract or expected-schema file (YAML or JSON), relative to the configuration file. |
| `object` | string | `null` | Schema object of the contract to check against; defaults to the only object, or the one named like the dataset's table. |
| `extra_columns` | one of `error`, `warning`, `ignore` | `warning` | Columns in the dataset that the contract does not list. |
| `types` | one of `error`, `warning`, `ignore` | `error` | Type mismatches: a different logical type, or a physical type the dataset cannot hold without loss. A dataset type wider than the contract's is always at most a warning. |
| `nullability` | one of `error`, `warning`, `ignore` | `warning` | Columns the contract marks `required` that the dataset declares nullable. |
| `primary_key` | one of `error`, `warning`, `ignore` | `warning` | A dataset primary key that differs from the contract's (or is not declared). |
| `case_sensitive` | boolean | `false` | Match column names case-sensitively. |
| `type_dialect` | string | `null` | Dialect of the contract's physical types (default: the dataset's dialect). |

### `control_totals` {#check-control_totals}

Header or trailer control records (record counts, column sums) must match the data.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `null` | Files to check: a file, folder, or glob relative to the connection's folder (`**` matches folders recursively), or a URL. Defaults to the dataset's own files. `${run.date}` and the other run placeholders are replaced. |
| `header` | [object](#controlrecord) | `null` | Header control record: which line holds it and how to read its values. |
| `trailer` | [object](#controlrecord) | `null` | Trailer control record: which line holds it and how to read its values. |
| `record_count` | string | `null` | Name of the control value holding the number of data records. |
| `sums` | mapping of string | `{}` | Data column to the name of the control value holding its sum, such as `{amount: amount_total}`. |
| `tolerance` | number; >= 0 | `0.0` | Largest absolute difference accepted for sums. |
| `encoding` | string | `utf-8` | Encoding of the files holding the control records. |

#### ControlRecord {#controlrecord}

Used by check `control_totals`: header.

Where a header or trailer control record is, and how to read its values.

Also used at: check `control_totals`: trailer.

| Key | Type | Default | Description |
|---|---|---|---|
| `line` | integer | `null` | Which non-empty line holds the record: 1 is the first, -1 the last. Defaults to 1 for a header and -1 for a trailer. |
| `pattern` | string | `null` | Regular expression with named groups, one per control value, such as `^T\|(?P<count>\d+)\|(?P<amount>-?[\d.]+)$`. The record must match it. |
| `fields` | mapping of [object](#controlfield) | `{}` | Control values at fixed positions (an alternative to `pattern`). |

#### ControlField {#controlfield}

Used by check `control_totals`: header.fields.<name>.

A control value at fixed positions of a control record.

| Key | Type | Default | Description |
|---|---|---|---|
| `start` | integer; > 0 | **required** | Position of the first character, starting at 1. |
| `width` | integer; > 0 | **required** | Number of characters the value occupies. |
| `scale` | integer; >= 0 | `0` | Implied decimal places: `000012345` with scale 2 is 123.45. |

### `custom_sql` {#check-custom_sql}

A SQL query that returns failing rows, or a scalar compared with an expectation.

A SQL query that returns failing rows, or a scalar compared with an expectation.

`{table}` in the SQL is replaced with the checked dataset.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | **required** | Name of the check, required for this type; it becomes part of the check ID. |
| `sql` | string; not empty | **required** | SQL query to run. `{table}` stands for the checked dataset, with its filter applied and already aliased. |
| `returns` | one of `failing_rows`, `scalar` | `failing_rows` | `failing_rows`: every row the query returns is a failure. `scalar`: the query returns one number, which is compared with `expect`. |
| `expect` | [object](#scalarexpectation) | `null` | The expectation the scalar result must meet; required when `returns` is `scalar` and not allowed otherwise. |

#### ScalarExpectation {#scalarexpectation}

Used by check `custom_sql`: expect.

An assertion on a scalar query result.

| Key | Type | Default | Description |
|---|---|---|---|
| `operator` | one of `==`, `!=`, `<`, `<=`, `>`, `>=` | **required** | How the query result is compared with `value`: `<result> <operator> <value>`. |
| `value` | number | **required** | Number the query result is compared with. |

### `delete_propagation` {#check-delete_propagation}

Source deletes are reflected in the target (MIG-05).

Source deletes are reflected in the target: hard deletes propagated and soft-delete flags
consistent (MIG-05).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `source_where` | string | `null` | SQL predicate restricting the source rows (`where` applies to both sides). |
| `target_where` | string | `null` | SQL predicate restricting the target rows (`where` applies to both sides). |
| `hard_deletes` | boolean | `true` | Target keys missing from the source must not remain as active rows. |
| `source_flag` | [object](#flagconfig) | `null` | How the source marks soft-deleted rows. |
| `target_flag` | [object](#flagconfig) | `null` | How the target marks deleted rows. |
| `soft_deleted_rows` | one of `either`, `flagged`, `absent` | `either` | Where soft-deleted source rows must end up: `flagged` in the target, `absent` from it, or `either`. |

### `document_schema` {#check-document_schema}

Inferred document schema: expected fields, types, presence, and heterogeneity (SCH-13).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `sample_size` | integer; > 0, <= 1000000 | `null` | Documents sampled; defaults to the dataset's `options.sample_size` (1,000). |
| `fields` | mapping of string or list of string or [object](#fieldexpectation) | `{}` | Expected fields by path (`customer.name`, `lines[].qty`), each with its allowed types, minimum presence, and nullability. |
| `sampling` | one of `first`, `random` | `first` | `first` reads the first documents (repeatable); `random` samples at random (MongoDB only). |
| `max_types` | integer; > 0 | `null` | Most distinct non-NULL types accepted per field (unlisted fields too); unlimited when omitted. |
| `allow_extra_fields` | boolean | `true` | Accept fields that are not listed (and are not parents of listed fields). |
| `ignore` | list of string | `[]` | Glob patterns of paths ignored by `max_types` and `allow_extra_fields`. |

#### FieldExpectation {#fieldexpectation}

Used by check `document_schema`: fields.<name>.

What a field must look like. A string or list is shorthand for `type`.

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | string or list of string | `null` | Allowed types: native names or aliases (see the check's description). |
| `min_presence` | number; >= 0, <= 100 | `100.0` | Lowest accepted presence, in percent of sampled documents (or array elements). |
| `nullable` | boolean | `true` | Whether NULL values are accepted. |
| `max_types` | integer; > 0 | `null` | Most distinct non-NULL types accepted (overrides the check's `max_types`). |

### `dst_edge_cases` {#check-dst_edge_cases}

Local timestamps that do not exist (spring forward) or are ambiguous (fall back) in a time zone (MIG-11).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of string; at least one item | **required** | Columns to check (at least one). |
| `timezone` | string | `null` | Time zone of the local values; defaults to `normalization.timezone`. |
| `rules` | list of one of `nonexistent`, `ambiguous`; at least one item | `["nonexistent", "ambiguous"]` | Rules to apply: `nonexistent` finds local times skipped when clocks spring forward, `ambiguous` finds local times repeated when they fall back. |
| `min_year` | integer; >= 1, <= 9999 | `1970` | Earliest year whose transitions are examined (sentinel dates are ignored). |
| `max_year` | integer; >= 1, <= 9999 | `2100` | Latest year whose transitions are examined (sentinel dates are ignored). |

### `expression` {#check-expression}

A boolean SQL expression every row must satisfy, such as `end_date >= start_date`.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | **required** | Name of the check, required for this type; it becomes part of the check ID. |
| `expression` | string; not empty | **required** | Boolean SQL expression every row must satisfy; rows where it is false or NULL fail. |

### `file_checksum` {#check-file_checksum}

File checksums (MD5 or SHA-256) must match expected values, sidecar files, a manifest, or the files of another dataset (file-vs-file reconciliation).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `null` | Files to check: a file, folder, or glob relative to the connection's folder (`**` matches folders recursively), or a URL. Defaults to the dataset's own files. `${run.date}` and the other run placeholders are replaced. |
| `algorithm` | one of `md5`, `sha256` | `sha256` | Digest algorithm. Files are hashed as stored, so a compressed file's digest is that of its compressed bytes. |
| `expected` | mapping of string | `{}` | Expected digests by file: a file name, a path relative to the connection's folder, or a glob. |
| `sidecar` | boolean or string | `false` | Read each file's digest from a sidecar file next to it: `true` for `<file>.md5` or `<file>.sha256` (after the algorithm), or the sidecar suffix, such as `.sha256sum`. |
| `manifest` | string | `null` | A manifest file, relative to the connection's folder: lines of `<digest> <file>` (the `sha256sum`/`md5sum` format) or CSV rows `file,digest` (an optional header is skipped). |
| `compare_to` | [object](#filesetref) | `null` | Compare with the files of another location, matched by name (`match_by`). Every file must exist on both sides with the same digest. |
| `match_by` | one of `name`, `relative_path` | `name` | How `compare_to` pairs files: by file name, or by path relative to each side's folder or glob base. |

#### FileSetRef {#filesetref}

Used by check `file_checksum`: compare_to.

Files on a connection, used as the reference of a checksum comparison.

| Key | Type | Default | Description |
|---|---|---|---|
| `connection` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | A files connection; defaults to the checked dataset's connection. |
| `path` | string; not empty | **required** | File, folder, or glob relative to that connection's folder. |

### `file_count` {#check-file_count}

The number of files must fall within a range.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `null` | Files to check: a file, folder, or glob relative to the connection's folder (`**` matches folders recursively), or a URL. Defaults to the dataset's own files. `${run.date}` and the other run placeholders are replaced. |
| `min` | integer; >= 0 | `null` | Fewest files accepted; omit for no lower bound. |
| `max` | integer; >= 0 | `null` | Most files accepted; omit for no upper bound. |

### `file_exists` {#check-file_exists}

At least one file must exist, or exactly `count` files.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `null` | Files to check: a file, folder, or glob relative to the connection's folder (`**` matches folders recursively), or a URL. Defaults to the dataset's own files. `${run.date}` and the other run placeholders are replaced. |
| `count` | integer; >= 0 | `null` | Exact number of files expected; omit to require at least one. |

### `file_freshness` {#check-file_freshness}

The newest (or oldest) file must have been modified within `max_age`.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `null` | Files to check: a file, folder, or glob relative to the connection's folder (`**` matches folders recursively), or a URL. Defaults to the dataset's own files. `${run.date}` and the other run placeholders are replaced. |
| `max_age` | string; matching `^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$` | **required** | Such as `26h` or `2d`. |
| `which` | one of `newest`, `oldest` | `newest` | `newest`: some file is recent enough. `oldest`: every file is recent enough. |

### `file_name_pattern` {#check-file_name_pattern}

File names must match a regular expression.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `null` | Files to check: a file, folder, or glob relative to the connection's folder (`**` matches folders recursively), or a URL. Defaults to the dataset's own files. `${run.date}` and the other run placeholders are replaced. |
| `pattern` | string | **required** | Python regular expression, such as `orders_\d{8}\.csv(\.gz)?`. |
| `match_on` | one of `name`, `path` | `name` | `name` matches the file name; `path` matches the path relative to the connection's folder (`2026/11/orders.csv`). |
| `full_match` | boolean | `true` | Require the whole name to match, rather than any part of it. |

### `file_size` {#check-file_size}

File sizes must fall within a range; empty files fail unless allowed.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `null` | Files to check: a file, folder, or glob relative to the connection's folder (`**` matches folders recursively), or a URL. Defaults to the dataset's own files. `${run.date}` and the other run placeholders are replaced. |
| `min_bytes` | integer; >= 0 | `null` | Smallest accepted file size in bytes; omit for no lower bound. |
| `max_bytes` | integer; > 0 | `null` | Largest accepted file size in bytes; omit for no upper bound. |
| `allow_empty` | boolean | `false` | Accept zero-byte files (they fail by default, even without `min_bytes`). |

### `freshness` {#check-freshness}

The most recent value of a timestamp column must be newer than `max_age`.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** | Column to check. |
| `max_age` | string; matching `^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$` | **required** | Largest accepted age of the column's newest value when the check runs, such as `6h` or `1d`. |

### `idempotency` {#check-idempotency}

Re-running a load creates no duplicates and changes neither the row count nor the content (MIG-03).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `key_columns` | list of string | `[]` | Columns that must be unique; defaults to the table's keys on the checked side. |
| `checksum_columns` | list of string | `[]` | Columns in the content checksum; defaults to every column not in `exclude`. |
| `exclude` | list of string | `[]` | Columns left out of the checksum, such as load timestamps and batch IDs. |
| `baseline` | string | `null` | Run to compare with: `last_run` (default), `last_passed`, or a run ID. |
| `compare` | list of one of `row_count`, `checksum` | `["row_count", "checksum"]` | What must be unchanged since the baseline run: the row count, the content checksum, or both. |

### `key_map` {#check-key_map}

Every legacy key maps to exactly one new key, the reverse where required, and the map covers every source key and every target key (MIG-06).

Every legacy key maps to exactly one new key (and the reverse, where required), and the
map covers every source and target key (MIG-06).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `source_where` | string | `null` | SQL predicate restricting the source rows (`where` applies to both sides). |
| `target_where` | string | `null` | SQL predicate restricting the target rows (`where` applies to both sides). |
| `map` | [object](#keymapref) | **required** | The key map: where it is, and its legacy and new key columns. |
| `source_key` | list of string | `[]` | Legacy key columns in the source dataset; defaults to the table's source keys. |
| `target_key` | list of string | `[]` | New key columns in the target dataset; defaults to the table's target keys. |
| `reverse` | boolean | `true` | Every new key must map from exactly one legacy key. |
| `complete_source` | boolean | `true` | Every source key must have a mapping. |
| `complete_target` | boolean | `true` | Every target key must appear in the map. |
| `dangling` | boolean | `false` | Map entries whose new key is not in the target count as violations. |

#### KeyMapRef {#keymapref}

Used by check `key_map`: map.

A surrogate key map: legacy key columns and the new key columns they map to.

| Key | Type | Default | Description |
|---|---|---|---|
| `connection` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Connection holding the dataset; defaults to the checked dataset's connection. |
| `table` | string | `null` | Table or view name, optionally qualified (`schema.table`). Set exactly one of `table` and `query`. |
| `query` | string | `null` | SQL query used as the dataset. Set exactly one of `table` and `query`. |
| `filter` | string | `null` | SQL predicate applied to this dataset. |
| `legacy_columns` | string or list of string | **required** | Legacy key columns in the map, in the order of `source_key`. |
| `new_columns` | string or list of string | **required** | New key columns in the map, in the order of `target_key`. |

### `late_arriving` {#check-late_arriving}

Rows that arrived after their period closed, or later than allowed (MIG-02).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `event_column` | string | **required** | When the business event happened (for example a posting date). |
| `load_column` | string | **required** | When the row was loaded. |
| `max_lateness` | string; matching `^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$` | `null` | Rows loaded more than this long after their event are late. |
| `period_start` | date-time (ISO 8601) or date (`YYYY-MM-DD`) or string | `null` | Start of the open period: rows whose event is before it but that were loaded on or after `loaded_since` arrived late, into a closed period. |
| `loaded_since` | date-time (ISO 8601) or date (`YYYY-MM-DD`) or string | `null` | Defaults to `period_start`. |

### `length` {#check-length}

String lengths must fall within a range.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** | Column to check. |
| `min` | integer; >= 0 | `null` | Shortest accepted length in characters; omit for no lower bound. |
| `max` | integer; > 0 | `null` | Longest accepted length in characters; omit for no upper bound. |

### `not_blank` {#check-not_blank}

Columns must not contain NULL, empty, or whitespace-only strings.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of string; at least one item | **required** | Columns to check (at least one). |

### `not_null` {#check-not_null}

Columns must not contain NULL.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of string; at least one item | **required** | Columns to check (at least one). |

### `null_ratio` {#check-null_ratio}

The share of NULL values must not exceed a threshold.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** | Column to check. |
| `max_ratio` | number; >= 0, <= 1 | **required** | Largest accepted share of NULL values, from 0 to 1 (`0.2` allows 20 percent). |

### `range` {#check-range}

Values must fall within a range. Dates and timestamps may be given as ISO strings.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** | Column to check. |
| `min` | number or integer or string | `null` | Lowest accepted value: a number, or a date or timestamp as an ISO string. Omit for no lower bound. |
| `max` | number or integer or string | `null` | Highest accepted value: a number, or a date or timestamp as an ISO string. Omit for no upper bound. |
| `inclusive` | boolean | `true` | Accept values equal to `min` or `max`. Set to false for strict bounds. |

### `referential_integrity` {#check-referential_integrity}

Every value must exist in a parent table (no orphans).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of string | `[]` | Columns of the checked dataset whose values must exist in the parent, paired by position with `parent.columns`. |
| `column` | string | `null` | Shorthand for a single-column key; becomes `columns`. |
| `parent` | [object](#parentref) | **required** | The parent table and its key columns. |

#### ParentRef {#parentref}

Used by check `referential_integrity`: parent.

The parent side of a referential integrity check.

| Key | Type | Default | Description |
|---|---|---|---|
| `connection` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Connection holding the parent table; defaults to the checked dataset's connection. |
| `table` | string | **required** | Parent table holding the referenced key, optionally qualified (`schema.table`). |
| `columns` | list of string | `[]` | Key columns of the parent table, paired by position with the check's `columns`. |
| `column` | string | `null` | Shorthand for a single-column key; becomes `columns`. |

### `regex` {#check-regex}

Values must match a regular expression.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `column` | string | **required** | Column to check. |
| `pattern` | string | **required** | Regular expression the values must match. It runs in the checked database, so use syntax that database supports. |
| `full_match` | boolean | `true` | Require the whole value to match, rather than any substring. |

### `row_count_range` {#check-row_count_range}

The number of rows must fall within a range.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `min` | integer; >= 0 | `null` | Fewest rows accepted; omit for no lower bound. |
| `max` | integer; >= 0 | `null` | Most rows accepted; omit for no upper bound. |

### `scd2` {#check-scd2}

SCD Type 2 history is well formed: one current row per business key, valid and non-overlapping validity ranges, optionally no gaps, and reconciled version counts (MIG-04).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `business_key` | string or list of string | **required** | Business key columns identifying one entity across its versions. |
| `valid_from` | string | **required** | Column holding the start of each version's validity. |
| `valid_to` | string | **required** | Column holding the end of each version's validity; NULL, or `open_end`, marks a version that is still valid. |
| `current_flag` | [object](#flagconfig) | `null` | How current rows are marked. Without it, rows with an open end are current. |
| `open_end` | date-time (ISO 8601) or date (`YYYY-MM-DD`) or string | `null` | A `valid_to` meaning "still valid", such as `9999-12-31` (NULL always counts). |
| `end` | one of `exclusive`, `inclusive` | `exclusive` | `exclusive`: a version is valid up to, not including, `valid_to`, so the next one starts at `valid_to`. `inclusive`: the next one starts `granularity` later. |
| `granularity` | string; matching `^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$` | `1d` | For inclusive ends: the step between one version's end and the next one's start. |
| `allow_gaps` | boolean | `false` | Do not report gaps between one version's end and the next version's start. |
| `history` | [object](#historycountref) | `null` | Reconcile the number of versions per business key with this dataset. |

#### HistoryCountRef {#historycountref}

Used by check `scd2`: history.

A dataset with one row per expected version of each business key.

| Key | Type | Default | Description |
|---|---|---|---|
| `connection` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Connection holding the dataset; defaults to the checked dataset's connection. |
| `table` | string | `null` | Table or view name, optionally qualified (`schema.table`). Set exactly one of `table` and `query`. |
| `query` | string | `null` | SQL query used as the dataset. Set exactly one of `table` and `query`. |
| `filter` | string | `null` | SQL predicate applied to this dataset. |
| `keys` | string or list of string | **required** | Business key columns in this dataset, in the order of `business_key`. |

### `schema_on_read` {#check-schema_on_read}

The columns and types read from the dataset must match an expected schema.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of [object](#schemacolumn) | `[]` | Expected columns, as a list of `{name, type}` (or names), or a mapping of name to type. |
| `schema_file` | string | `null` | A YAML or JSON file with the expected columns (the same forms as `columns`, optionally under a `columns` key), relative to the configuration's folder. |
| `allow_extra_columns` | boolean | `false` | Accept columns that are not expected. |
| `ordered` | boolean | `false` | Require the expected columns in the given order. |
| `case_sensitive` | boolean | `false` | Compare column names case-sensitively. |
| `type_match` | one of `logical`, `exact` | `logical` | `logical` compares type families (any integer width, any decimal precision, any string length); `exact` also compares widths, precision, scale, and lengths. |

#### SchemaColumn {#schemacolumn}

Used by check `schema_on_read`: columns[].

An expected column.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty | **required** | Expected column name, compared case-insensitively unless `case_sensitive` is set. |
| `type` | string | `null` | Expected type in SQL terms (`INTEGER`, `DECIMAL(12,2)`, `VARCHAR`, `DATE`, `TIMESTAMP`); omit to accept any type. |

### `scope_reconciliation` {#check-scope_reconciliation}

Source rows reconcile to migrated rows plus rows excluded by design (MIG-01).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `source_where` | string | `null` | SQL predicate restricting the source rows (`where` applies to both sides). |
| `target_where` | string | `null` | SQL predicate restricting the target rows (`where` applies to both sides). |
| `excluded_where` | string; not empty | **required** | SQL predicate on the source selecting the rows excluded by design (archived, closed, out of scope). |
| `expected_excluded` | integer or [object](#excludedrange); >= 0 | `null` | The expected number of excluded rows: an exact count, or `{min, max}`. |
| `population` | one of `unfiltered`, `filtered` | `unfiltered` | Count the source before its `source.filter` (`unfiltered`, the usual case when that filter is the scope) or after it (`filtered`). Window and in-flight filters always apply. |
| `tolerance_rows` | integer; >= 0 | `0` | Accepted difference between the source total and migrated plus excluded rows. |
| `tolerance_pct` | number; >= 0, <= 100 | `0.0` | The same, as a percentage of the source total (the larger allowance wins). |
| `excluded_absent` | boolean | `false` | Also require that no excluded source row's key exists in the target (needs keys). |

#### ExcludedRange {#excludedrange}

Used by check `scope_reconciliation`: expected_excluded.

An accepted range of excluded rows.

| Key | Type | Default | Description |
|---|---|---|---|
| `min` | integer; >= 0 | `null` | Fewest excluded rows accepted; omit for no lower bound. |
| `max` | integer; >= 0 | `null` | Most excluded rows accepted; omit for no upper bound. |

### `substitution_rules` {#check-substitution_rules}

Default and sentinel substitutions are applied exactly as specified (MIG-12).

Default and sentinel substitutions are applied exactly as specified, and target sentinel
values do not appear without a matching source condition (MIG-12).

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `source_where` | string | `null` | SQL predicate restricting the source rows (`where` applies to both sides). |
| `target_where` | string | `null` | SQL predicate restricting the target rows (`where` applies to both sides). |
| `rules` | list of [object](#substitutionrule); at least one item | **required** | The substitutions to verify. |
| `trim` | boolean | `false` | Compare values after removing leading and trailing spaces. |
| `unmatched_sentinels` | boolean | `true` | Report target sentinel values whose source matches no rule (and is not the sentinel). |

#### SubstitutionRule {#substitutionrule}

Used by check `substitution_rules`: rules[].

A substitution such as source NULL to target `'N/A'`, or `'00000000'` to NULL.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Name used in the result (`rule_<name>`); defaults to the rule's position. |
| `column` | string | **required** | Source column. |
| `target_column` | string | `null` | Target column; defaults to the column paired with `column`, else the same name. |
| `source_null` | boolean | `false` | The rule applies when the source is NULL. |
| `source_blank` | boolean | `false` | The rule applies when the source is empty or spaces only. |
| `source_values` | list of string or integer or number | `[]` | The rule applies when the source holds one of these values (compared as text). |
| `target_null` | boolean | `false` | The target must then be NULL. |
| `target_value` | string or integer or number | `null` | The target must then hold this value (compared as text). |

### `unique` {#check-unique}

Values (or combinations of values) must be unique.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `columns` | list of string; at least one item | **required** | Columns to check (at least one). |
| `ignore_nulls` | boolean | `true` | Rows where any of the columns is NULL are not counted as duplicates. |

## Notification types {#notification-types}

Each entry of `notifications` is selected by its `type`. See [Notifications](../notifications.md).

### Common fields {#notification-common}

Every notification type accepts these fields.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string; not empty, at most 128 characters, matching `^[A-Za-z0-9_-]+$` | `null` | Channel name, used by `dvf notify test --channel` and in logs. Defaults to the type, with `-2`, `-3`, ... appended when a type is used more than once. |
| `on` | list of one of `passed`, `warning`, `failed`, `error`, `skipped`, `waived`; at least one item | `["failed", "error"]` | Run statuses that trigger a notification. An interrupted run counts as `error`. |
| `enabled` | boolean | `true` | Set to false to stop notifying this channel without removing it. |
| `options` | mapping | `{}` | Additional channel-specific settings. |
| `report_url` | string | `null` | Link to the report shown in messages, for example `https://ci.example.com/dvf/${run.id}/report.html`. Without it, messages show the local report path as text. |
| `max_failures` | integer; >= 0, <= 50 | `5` | Failed and errored checks listed in a message (the worst first). |
| `counts_only` | boolean | `false` | Send statuses, check IDs, and counts only: no messages, hints, or samples. Always the case in strict privacy mode (`defaults.privacy.mode: strict`). |
| `include_samples` | boolean | `false` | Include masked sample rows of the listed failures. Off by default; never in strict privacy mode or with `counts_only`. |
| `max_sample_rows` | integer; > 0, <= 20 | `3` | Sample rows per failure when `include_samples` is on. |
| `timeout_s` | number; > 0, <= 300 | `10.0` | Timeout of one delivery attempt, in seconds. |
| `retries` | integer; >= 0, <= 10 | `2` | Retries after a transient failure (a network error, HTTP 429, or a 5xx response). |
| `retry_backoff_s` | number; >= 0, <= 60 | `1.0` | First retry delay in seconds; it doubles on each retry. A `Retry-After` header wins, up to 30 seconds. |
| `ca_file` | string | `null` | A PEM file of certificate authorities to trust for HTTPS or SMTP TLS (relative to the configuration), for servers with an internal CA. |

### `email` {#notification-email}

Email through SMTP (STARTTLS, implicit TLS, or a plain local relay).

Email through SMTP, with HTML and plain-text bodies.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `recipients` | list of string; at least one item | **required** | Email addresses the message is sent to. |
| `sender` | string | `null` | `From` address; defaults to `username`, or `dvf@<host name>`. |
| `smtp_host` | string | **required** | SMTP server host name. |
| `smtp_port` | integer; >= 1, <= 65535 | `587` | SMTP server port. The default suits `starttls`; implicit TLS (`ssl`) usually uses 465. |
| `username` | string | `null` | Log in with this user (and `password`) after the connection is secured. |
| `password` | secret string | `null` | Password of `username`, never sent over an unencrypted connection. Use a `${secret:...}` reference; it is treated as a secret. |
| `use_tls` | boolean | `true` | Upgrade the connection with STARTTLS. Kept for compatibility; `security` wins when set. |
| `security` | one of `starttls`, `ssl`, `none` | `null` | `starttls` (port 587), `ssl` (implicit TLS, port 465), or `none` (unencrypted, for local relays only). Defaults to `starttls`, or `none` when `use_tls` is false. |
| `subject_prefix` | string | `[dvf]` | The subject is `<prefix> <project>: <STATUS>`. |
| `attach_summary` | boolean | `false` | Attach the Markdown summary (`summary.md`) of the run. |

### `slack` {#notification-slack}

Slack incoming webhook (Block Kit message).

A Slack incoming webhook; messages use Block Kit.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `webhook` | secret string | **required** | Webhook URL. Treated as a secret because the URL grants posting rights. |
| `mention` | string | `null` | Text put before the message, for example `<!here>` or `<!subteam^S0123ABC>`. |

### `teams` {#notification-teams}

Microsoft Teams Workflows webhook (Adaptive Card); also accepted by legacy connectors.

A Microsoft Teams Workflows (Power Automate) webhook; messages are Adaptive Cards.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `webhook` | secret string | **required** | Webhook URL. Treated as a secret because the URL grants posting rights. |

### `webhook` {#notification-webhook}

Generic HTTP webhook: the run summary as JSON (`schema: dvf.notification/v1`).

Any HTTP endpoint that accepts a JSON run summary.

Status: **ready**.

| Key | Type | Default | Description |
|---|---|---|---|
| `webhook` | secret string | **required** | Webhook URL. Treated as a secret because the URL grants posting rights. |
| `method` | one of `POST`, `PUT` | `POST` | HTTP method used to send the JSON run summary. |
| `headers` | mapping of secret string | `{}` | Extra HTTP headers, such as `Authorization`. Values are treated as secrets. |
