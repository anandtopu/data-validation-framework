# Components

!!! note "Generated page"
    This page is generated from the component registry by `uv run nox -s docs_reference`. Do not edit it by hand; change the source and regenerate.

Every built-in component, as `dvf plugins list` shows it (plugin API 0.1.0):
120 components, 102 ready, 15 beta, and
3 declared.

| Status | Meaning |
|---|---|
| ready | Implemented and verified. |
| beta | Implemented, but not yet verified against a live system. Feedback is welcome. |
| declared | Configurations validate, but the implementation is not in this version; using it is an error that says so. |

**Extra** is the optional dependency group that provides the driver:
`pip install 'dvf-framework[<extra>]'`. Installed plugins add their own components; see
[Plugins](../plugins.md) to write one.

## Connectors {#connector}

Values of `type` in `connections`. Settings: [connection types](configuration.md#connection-types). Kind `connector`; plugins register through the entry point group `dvf.connectors`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `bigquery` | beta | `bigquery` | Google BigQuery | [settings](configuration.md#connection-bigquery) |
| `cassandra` | beta | `cassandra` | Apache Cassandra and compatible systems (ScyllaDB, DataStax Astra) | [settings](configuration.md#connection-cassandra) |
| `cosmos` | beta | `cosmos` | Azure Cosmos DB for NoSQL | [settings](configuration.md#connection-cosmos) |
| `databricks` | beta | `databricks` | Databricks SQL warehouses and clusters | [settings](configuration.md#connection-databricks) |
| `dataframe` | ready |  | In-process pandas, Polars, or Arrow data (dvf.validate_dataframe) | [settings](configuration.md#connection-dataframe) |
| `db2` | declared | `db2` | IBM Db2 | [settings](configuration.md#connection-db2) |
| `duckdb` | ready |  | DuckDB database, in memory or on disk | [settings](configuration.md#connection-duckdb) |
| `dynamodb` | beta | `dynamodb` | Amazon DynamoDB | [settings](configuration.md#connection-dynamodb) |
| `files` | ready |  | CSV, Parquet, JSON, Avro, ORC, Excel, and more on local or cloud storage | [settings](configuration.md#connection-files) |
| `mongodb` | ready | `mongodb` | MongoDB | [settings](configuration.md#connection-mongodb) |
| `mssql` | ready | `mssql` | SQL Server, Azure SQL, Synapse, and Fabric SQL | [settings](configuration.md#connection-mssql) |
| `mysql` | ready | `mysql` | MySQL 8.0.17 or later | [settings](configuration.md#connection-mysql) |
| `odbc` | ready | `odbc` | Any database with an ODBC driver | [settings](configuration.md#connection-odbc) |
| `oracle` | ready | `oracle` | Oracle Database | [settings](configuration.md#connection-oracle) |
| `postgres` | ready | `postgres` | PostgreSQL | [settings](configuration.md#connection-postgres) |
| `redshift` | beta | `redshift` | Amazon Redshift | [settings](configuration.md#connection-redshift) |
| `sap_hana` | beta | `sap-hana` | SAP HANA at database level (user-installed hdbcli) | [settings](configuration.md#connection-sap_hana) |
| `sap_rfc` | beta | `sap-rfc` | SAP ECC and S/4HANA through RFC or OData (backends: pyrfc, custom_fm, odata; nwrfcsdk declared, not implemented) | [settings](configuration.md#connection-sap_rfc) |
| `snowflake` | beta | `snowflake` | Snowflake | [settings](configuration.md#connection-snowflake) |
| `spark` | ready | `spark` | Apache Spark session: local, cluster, or Databricks Connect | [settings](configuration.md#connection-spark) |
| `spark_dataframe` | ready | `spark` | In-process Spark DataFrame (dvf.validate_dataframe) | [settings](configuration.md#connection-spark_dataframe) |
| `sqlalchemy` | ready | `sqlalchemy` | Any database with a SQLAlchemy dialect | [settings](configuration.md#connection-sqlalchemy) |
| `sqlite` | ready |  | SQLite database file | [settings](configuration.md#connection-sqlite) |
| `teradata` | declared | `teradata` | Teradata Vantage | [settings](configuration.md#connection-teradata) |

## Checks {#check}

Values of `type` in a table's `checks`. Settings: [check types](configuration.md#check-types). Schema checks (`schema_checks`) are listed in [Checks and comparisons](../validations.md#schema-checks). Kind `check`; plugins register through the entry point group `dvf.checks`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `allowed_values` | ready |  | Values must belong to a fixed set. | [settings](configuration.md#check-allowed_values) |
| `audit_columns` | ready |  | Audit and lineage columns are populated and consistent (MIG-07). | [settings](configuration.md#check-audit_columns) |
| `bad_records` | ready |  | Unreadable and malformed records (CSV parse and quoting errors, invalid JSON lines, fixed-width length errors, unconvertible values, corrupt files) must not exceed a limit. | [settings](configuration.md#check-bad_records) |
| `baseline_metrics` | ready |  | Current metrics match a named pre-migration baseline within tolerances (MIG-14). | [settings](configuration.md#check-baseline_metrics) |
| `cdc_applied` | ready |  | The latest change of every key in a CDC change table is reflected in the dataset (MIG-02). | [settings](configuration.md#check-cdc_applied) |
| `charset_conversion` | ready |  | Text survived the character set conversion: no U+FFFD replacement characters, mojibake, or control characters, and every value fits the legacy code page (MIG-10). | [settings](configuration.md#check-charset_conversion) |
| `contract` | ready |  | Schema conforms to an ODCS v3 contract or an expected-schema file (SCH-12) | [settings](configuration.md#check-contract) |
| `control_totals` | ready |  | Header or trailer control records (record counts, column sums) must match the data. | [settings](configuration.md#check-control_totals) |
| `custom_sql` | ready |  | A SQL query that returns failing rows, or a scalar compared with an expectation. | [settings](configuration.md#check-custom_sql) |
| `delete_propagation` | ready |  | Source deletes are reflected in the target (MIG-05). | [settings](configuration.md#check-delete_propagation) |
| `document_schema` | ready |  | Inferred document schema: expected fields, types, presence, and heterogeneity (SCH-13) | [settings](configuration.md#check-document_schema) |
| `dst_edge_cases` | ready |  | Local timestamps that do not exist (spring forward) or are ambiguous (fall back) in a time zone (MIG-11). | [settings](configuration.md#check-dst_edge_cases) |
| `expression` | ready |  | A boolean SQL expression every row must satisfy, such as `end_date >= start_date`. | [settings](configuration.md#check-expression) |
| `file_checksum` | ready |  | File checksums (MD5 or SHA-256) must match expected values, sidecar files, a manifest, or the files of another dataset (file-vs-file reconciliation). | [settings](configuration.md#check-file_checksum) |
| `file_count` | ready |  | The number of files must fall within a range. | [settings](configuration.md#check-file_count) |
| `file_exists` | ready |  | At least one file must exist, or exactly `count` files. | [settings](configuration.md#check-file_exists) |
| `file_freshness` | ready |  | The newest (or oldest) file must have been modified within `max_age`. | [settings](configuration.md#check-file_freshness) |
| `file_name_pattern` | ready |  | File names must match a regular expression. | [settings](configuration.md#check-file_name_pattern) |
| `file_size` | ready |  | File sizes must fall within a range; empty files fail unless allowed. | [settings](configuration.md#check-file_size) |
| `freshness` | ready |  | The most recent value of a timestamp column must be newer than `max_age`. | [settings](configuration.md#check-freshness) |
| `idempotency` | ready |  | Re-running a load creates no duplicates and changes neither the row count nor the content (MIG-03). | [settings](configuration.md#check-idempotency) |
| `key_map` | ready |  | Every legacy key maps to exactly one new key, the reverse where required, and the map covers every source key and every target key (MIG-06). | [settings](configuration.md#check-key_map) |
| `late_arriving` | ready |  | Rows that arrived after their period closed, or later than allowed (MIG-02). | [settings](configuration.md#check-late_arriving) |
| `length` | ready |  | String lengths must fall within a range. | [settings](configuration.md#check-length) |
| `not_blank` | ready |  | Columns must not contain NULL, empty, or whitespace-only strings. | [settings](configuration.md#check-not_blank) |
| `not_null` | ready |  | Columns must not contain NULL. | [settings](configuration.md#check-not_null) |
| `null_ratio` | ready |  | The share of NULL values must not exceed a threshold. | [settings](configuration.md#check-null_ratio) |
| `range` | ready |  | Values must fall within a range. Dates and timestamps may be given as ISO strings. | [settings](configuration.md#check-range) |
| `referential_integrity` | ready |  | Every value must exist in a parent table (no orphans). | [settings](configuration.md#check-referential_integrity) |
| `regex` | ready |  | Values must match a regular expression. | [settings](configuration.md#check-regex) |
| `row_count_range` | ready |  | The number of rows must fall within a range. | [settings](configuration.md#check-row_count_range) |
| `scd2` | ready |  | SCD Type 2 history is well formed: one current row per business key, valid and non-overlapping validity ranges, optionally no gaps, and reconciled version counts (MIG-04). | [settings](configuration.md#check-scd2) |
| `schema_on_read` | ready |  | The columns and types read from the dataset must match an expected schema. | [settings](configuration.md#check-schema_on_read) |
| `scope_reconciliation` | ready |  | Source rows reconcile to migrated rows plus rows excluded by design (MIG-01). | [settings](configuration.md#check-scope_reconciliation) |
| `substitution_rules` | ready |  | Default and sentinel substitutions are applied exactly as specified (MIG-12). | [settings](configuration.md#check-substitution_rules) |
| `unique` | ready |  | Values (or combinations of values) must be unique. | [settings](configuration.md#check-unique) |

## Comparison strategies {#comparator}

Values of `comparison.strategies` and `comparison.on_mismatch`. Kind `comparator`; plugins register through the entry point group `dvf.comparators`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `aggregate` | ready |  | Compare control totals per column (sum, min, max, distinct, nulls), overall or grouped |  |
| `bisect` | ready |  | Recursive segment checksums for mostly identical large tables |  |
| `full_diff` | ready |  | Row and column level diff with side-by-side values |  |
| `hash_bucket` | ready |  | Compare bucketed checksums in each database, then drill into mismatched buckets |  |
| `nested` | ready |  | Compare child rows per parent key: counts, totals, and an order-independent hash of canonical child rows (embedded arrays vs child tables) |  |
| `profile` | ready |  | Compare column profiles side by side |  |
| `row_count` | ready |  | Compare total and grouped row counts |  |
| `row_hash` | ready |  | Compare a hash of canonicalized columns per key |  |
| `sample` | ready |  | Statistical sample comparison with a fixed seed |  |

## Normalizers {#normalizer}

Values of `normalize` on keys and column mappings: `rtrim`, `{round: 2}`, or `{name: round, params: {digits: 2}}`. Kind `normalizer`; plugins register through the entry point group `dvf.normalizers`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `collapse_whitespace` | ready |  | Replace runs of whitespace with a single space |  |
| `empty_if_null` | ready |  | Treat NULL as an empty string |  |
| `lower` | ready |  | Lower-case strings |  |
| `ltrim` | ready |  | Remove leading whitespace |  |
| `null_if_empty` | ready |  | Treat empty strings as NULL |  |
| `round` | ready |  | Round numbers to a number of decimal digits | `digits` (default `0`) |
| `rtrim` | ready |  | Remove trailing whitespace (blank-padded CHAR columns) |  |
| `sap_alpha` | ready |  | SAP ALPHA conversion exit: pad or strip leading zeros on keys | `mode` (default `strip`), `length` (default `10`) |
| `sap_bool` | ready |  | SAP flag: 'X' to true, blank to false | `true_values` (default `["X"]`), `false_values` (default `[""]`), `other` (default `null`), `null_as_false` (default `false`) |
| `sap_curr` | ready |  | SAP currency amount: decimal shift from TCURX | `currency` (default `null`), `currency_column` (default `null`), `target_currency_column` (default `null`), `side` (default `source`), `decimals` (default `{}`), `default_decimals` (default `2`), `field_decimals` (default `2`) |
| `sap_dats` | ready |  | SAP DATS: '00000000' or blank to NULL, YYYYMMDD to DATE | `invalid` (default `null`), `sentinel` (default `0001-01-01`) |
| `sap_matnr_length` | ready |  | SAP material number length change from 18 to 40 characters | `length` (default `18`), `mode` (default `pad`) |
| `sap_numc` | ready |  | SAP NUMC: digits-only, zero-padded | `length` (default `null`), `invalid` (default `keep`) |
| `sap_quan` | ready |  | SAP quantity with unit reference | `unit` (default `null`), `unit_column` (default `null`), `target_unit_column` (default `null`), `side` (default `both`), `decimals` (default `{}`), `default_decimals` (default `null`) |
| `sap_spras` | ready |  | SAP language key: 1-character internal to 2-character ISO | `direction` (default `to_iso`), `overrides` (default `{}`), `unknown` (default `keep`) |
| `sap_tims` | ready |  | SAP TIMS: '000000' to NULL, HHMMSS to TIME | `zero_as_null` (default `true`) |
| `strip_leading_zeros` | ready |  | Remove leading zeros from numeric strings |  |
| `to_utc` | ready |  | Convert timestamps to UTC | `from_tz` (default `UTC`) |
| `trim` | ready |  | Remove leading and trailing whitespace |  |
| `truncate_timestamp` | ready |  | Truncate timestamps to a precision | `unit` (default `second`) |
| `unicode_nfc` | ready |  | Unicode NFC normalization |  |
| `upper` | ready |  | Upper-case strings |  |

## Report formats {#reporter}

Values of `reports.formats` and `dvf run --format`. Kind `reporter`; plugins register through the entry point group `dvf.reporters`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `excel` | ready | `excel` | Excel workbook for business sign-off |  |
| `html` | ready |  | Self-contained interactive HTML report |  |
| `json` | ready |  | Canonical results document (versioned JSON) |  |
| `junit` | ready |  | JUnit XML for CI test tabs |  |
| `markdown` | ready |  | Markdown summary for pull requests and CI summaries |  |
| `pdf` | declared |  | PDF rendered from the HTML report |  |
| `signoff` | ready |  | Cutover sign-off document (Markdown and printable HTML) with a go/no-go recommendation, open failures, waivers, the run-to-run comparison, and an approvals block. |  |

## Notification channels {#notifier}

Values of `type` in `notifications`. Settings: [notification types](configuration.md#notification-types). Kind `notifier`; plugins register through the entry point group `dvf.notifiers`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `email` | ready |  | Email through SMTP (STARTTLS, implicit TLS, or a plain local relay). | [settings](configuration.md#notification-email) |
| `slack` | ready |  | Slack incoming webhook (Block Kit message). | [settings](configuration.md#notification-slack) |
| `teams` | ready |  | Microsoft Teams Workflows webhook (Adaptive Card); also accepted by legacy connectors. | [settings](configuration.md#notification-teams) |
| `webhook` | ready |  | Generic HTTP webhook: the run summary as JSON (`schema: dvf.notification/v1`). | [settings](configuration.md#notification-webhook) |

## Secret providers {#secret_provider}

Providers of `${secret:<provider>:<path>#<key>}` references. Kind `secret_provider`; plugins register through the entry point group `dvf.secret_providers`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `awssm` | beta | `aws` | AWS Secrets Manager |  |
| `azurekv` | beta | `azure` | Azure Key Vault |  |
| `databricks` | beta |  | Databricks secret scopes |  |
| `dotenv` | ready |  | A .env file: `${secret:dotenv:NAME}` (project .env) or `${secret:dotenv:path/.env#NAME}`. |  |
| `env` | ready |  | Environment variables, falling back to the project's .env file: `${secret:env:NAME}`. |  |
| `gcpsm` | beta | `gcp` | Google Cloud Secret Manager |  |
| `keyring` | beta | `keyring` | Operating system keyring |  |
| `vault` | ready |  | HashiCorp Vault |  |

## Engines {#engine}

Values of `engine` in `defaults`, suites, and tables. Kind `engine`; plugins register through the entry point group `dvf.engines`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `auto` | ready |  | Choose the engine from connector capabilities and data size |  |
| `duckdb` | ready |  | Local out-of-core engine with spill to disk |  |
| `polars` | ready |  | Local DataFrame engine (lazy and streaming) |  |
| `pushdown` | ready |  | SQL generated per dialect and run inside the source and target databases |  |
| `spark` | ready | `spark` | Distributed engine for very large data and Databricks |  |

## Result sinks {#sink}

Values of `type` in `sinks`. Settings: [sinks](configuration.md#sinkconfig). Kind `sink`; plugins register through the entry point group `dvf.sinks`.

| Name | Status | Extra | Description | Settings |
|---|---|---|---|---|
| `delta` | ready | `delta` | Delta Lake table |  |
| `duckdb` | ready |  | DuckDB file |  |
| `postgres` | ready | `postgres` | PostgreSQL table |  |
| `snowflake` | beta | `snowflake` | Snowflake table |  |
| `sqlite` | ready |  | SQLite file |  |

## Lifecycle hooks {#hooks}

Plugins implement any of these with `@dvf.plugins.hookimpl` and register through the entry point group `dvf.hooks` or a local plugin path. Arguments are passed by keyword, so an implementation may accept only the ones it needs. See [Plugins](../plugins.md#lifecycle-hooks).

| Hook | Called |
|---|---|
| `enrich_result(result)` | Called after a check completes; may add entries to `details` or `metadata`. (Phase 1) |
| `modify_plan(plan)` | Called with the execution plan before it runs; may reorder or filter tasks. (Phase 1) |
| `on_check_complete(run_id, result)` | Called after a check completes and has been enriched. (Phase 1) |
| `on_check_start(run_id, check_id)` | Called before a check runs. (Phase 1) |
| `on_config_loaded(config)` | Called once the configuration is loaded and validated. |
| `on_failure(run_id, result)` | Called for checks that failed or errored, for example to open a ticket. (Phase 1) |
| `on_run_end(result)` | Called after the run completes and before reports render. (Phase 1) |
| `on_run_start(run_id, config)` | Called when a run starts. (Phase 1) |
| `on_suite_end(run_id, result)` | Called after a suite completes. (Phase 1) |
| `on_suite_start(run_id, suite)` | Called before a suite runs. (Phase 1) |
| `report_sections(result, report_format)` | Return extra report sections, each `{"title": str, "content": str}`. (Phase 1) |
| `triage_failures(summary)` | Suggest causes of a run's failures: the AI-assisted triage interface. (Phase 4) |
