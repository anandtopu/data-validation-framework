# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-11-10

### Added

- Phase 4 integrations and the 1.0 release:
  - Distribution name `dvf-framework` on PyPI (`dvf` is taken). The import package and the `dvf` command are unchanged; install hints use the new name.
  - Container images on GHCR for linux/amd64 and arm64: `slim` (DuckDB, files, SQLite) and `full` (open-source drivers; SQL Server is left out because its wheel bundles Microsoft's proprietary ODBC driver). Non-root, `dvf` entrypoint, `/work` workdir, built from `uv.lock`, with SBOM, provenance, and attestations.
  - Automated releases with release-please: PyPI Trusted Publishing with attestations, CycloneDX SBOMs, GitHub Release notes from CHANGELOG.md, multi-arch images, and docs on GitHub Pages. A pip-audit CI job, an OpenSSF Scorecard workflow, and a Docker smoke job. nox sessions `dist`, `quickstart`, `docker`, `sbom`, `audit`, and `release_notes`.
  - GitHub Action (`action.yml`) that installs `dvf-framework`, runs `dvf run`, publishes the Markdown summary, annotations, and an artifact, sets outputs, and enforces `--fail-on` and `--min-score`. CI templates for GitLab, Azure DevOps, Jenkins, and GitHub Actions with PR (`tag:pr`) and nightly (`tag:nightly`) modes.
  - `dvf.integrations`: an Airflow `DvfOperator`, Dagster asset checks, a Prefect task and flow, Databricks and AWS Glue entry points, and examples for Azure Data Factory, Synapse, Databricks Workflows, and Step Functions. `dvf dbt import` generates a configuration from a dbt `manifest.json`.
  - Notifications: Slack (Block Kit), Microsoft Teams Workflows (Adaptive Card), email (SMTP with STARTTLS or TLS), and generic JSON webhooks, triggered by run status, with retries, secret redaction, and privacy filtering; `dvf notify test`; an example ticketing notifier plugin.
  - OpenLineage run events with data quality assertion and metric facets (HTTP, file, or console transports), OpenTelemetry spans and metrics (`dvf-framework[otel]`), and an AI-assisted triage plugin interface (`triage_failures` hook), off by default, that receives only masked, aggregated summaries.
  - Secret providers `vault` (verified against HashiCorp Vault) and, as beta, `keyring`, `awssm`, `azurekv`, `gcpsm`, and `databricks`; each secret is resolved once per run and redacted everywhere, including encoded forms.
  - Privacy: PII pattern detection (emails, phones, national IDs, card numbers, IBANs), masking modes `mask`, `hash`, and `tokenize`, per-check `sample_size`, and strict mode covering recorded SQL, profiles, aggregates, and repro files.
  - REC-07 `sample` comparison: a seeded-hash sample by key (identical keys on both sides in every dialect), sized by margin of error (Cochran), fraction, or rows, optionally stratified, with an estimated mismatch rate, confidence interval, and extrapolated counts.
  - Schema checks `physical_design` (SCH-07), `column_order` (SCH-08), `collation` (SCH-09), `object_inventory` (SCH-10), `schema_drift` (SCH-11), and `naming_standards` (SCH-14), with `schema_options` and optional connector catalog-metadata methods.
  - Result sinks `delta` and, as beta, `snowflake`; generic `sqlalchemy` and `odbc` connectors, verified against SQLite, PostgreSQL, MySQL, and Oracle.
  - Documentation site restructured for 1.0: reference pages for configuration, components, results, and the command line generated from the models, the registry, and the CLI (`nox -s docs_reference`), a PyPI-first quickstart, installation and releasing guides, two tutorials, and a complete plugin authoring guide. Every configuration field is documented.
  - Commands `dvf plan`, `dvf schema-diff`, `dvf waivers list`, `dvf config migrate` (a configuration migration framework), and `dvf plugin new` (plugin package scaffolding for seven component kinds).
  - `dvf.plugins` exports `StagedConnector`, `StagedData`, `NotificationMessage`, and `NotificationError`; notifier plugins implement `Notifier.send`.
  - ADR 0010 (Phase 4 integrations and the 1.0 release).

- Phase 3 big data and specialized sources:
  - Spark: the `spark` SQL dialect, connector (a running session, local, cluster, or Databricks Connect; catalog tables, Spark SQL, and Parquet, ORC, CSV, JSON, and with packages Avro, Delta, and Iceberg paths, with `as_of` time travel), and engine (the two-phase diff in Spark; other databases' projections are staged as Parquet). `auto` picks Spark when both sides are Spark datasets in one application. The seeded-defect harness passes on Spark, and the canonicalization parity tests run against local Spark in unit CI.
  - In-pipeline validation: `dvf.validate_dataframe(df, checks, on_fail=...)` for pandas, Polars, Arrow, and Spark DataFrames returns the valid rows, the quarantined rows with a `dvf_reasons` column, and the check results. Each check's `action` (`quarantine`, `drop`, `warn`, `fail`) routes failing rows, and `dvf.foreach_batch_validator` handles Structured Streaming micro-batches. Check errors raise `DataValidationError`, so a broken check never lets rows through.
  - Files connector: ORC, Delta Lake and Iceberg (with `as_of` versions, snapshots, or timestamps), Avro, Excel, fixed-width, and XML; bz2, xz, and non-UTF-8 text; `skip_footer`; Hive-partitioned folders; URLs through fsspec. `FileConnector.list_files()`.
  - Nine file-level checks: `file_exists`, `file_count`, `file_name_pattern`, `file_size`, `file_checksum` (including file-vs-file), `file_freshness`, `control_totals` (header and trailer records, REC-09), `schema_on_read`, and `bad_records`, with samples and extracts.
  - NoSQL pack: the `mongodb` connector (verified with MongoDB 7) and beta `cassandra`, `dynamodb`, and `cosmos` connectors. Documents are staged for SQL with native filters and pipelines, dot-path `fields`, `unwind`, array strategies (`json`, `hash`, `unordered_hash`, `count`, `index`, `explode`), forced `types`, and exact type unification that keeps native type names.
  - `nested` comparison strategy: child rows per parent key (counts, exact totals, an order-independent hash of canonical child rows), naming each parent that differs and why. The MongoDB-to-PostgreSQL example (`examples/mongodb_to_postgres`) reconciles embedded arrays against child tables.
  - `document_schema` check (SCH-13) and `infer_document_schema()` for sampled document schemas.
  - SAP pack: normalizers `sap_alpha`, `sap_matnr_length`, `sap_dats`, `sap_tims`, `sap_bool`, `sap_curr` (TCURX decimals from the row's currency), `sap_quan`, `sap_numc`, and `sap_spras`, parity-tested on DuckDB, SQLite, PostgreSQL, MySQL, SQL Server, Oracle, Polars, and Spark. Beta `sap_hana` connector (HANA dialect, automatic client filtering, DDIC schemas, catalog keys) and beta `sap_rfc` connector with pluggable backends (`pyrfc`, `custom_fm`, `odata`; `nwrfcsdk` declared), RFC_READ_TABLE field chunking for the 512-byte limit, paging, and 72-character WHERE lines. Business object templates and `dvf sap templates`.
  - Migration validations MIG-01 to MIG-14: checks `scope_reconciliation`, `cdc_applied`, `late_arriving`, `idempotency`, `scd2`, `delete_propagation`, `key_map`, `audit_columns`, `charset_conversion`, `dst_edge_cases`, `substitution_rules`, and `baseline_metrics`; table options `window` (delta loads) and `in_flight` (live replication); `dvf baseline capture|list` and `dvf history compare RUN_A RUN_B`; the `signoff` report format.
  - Pair checks (`PairCheck`): checks that read both datasets of a table by key, in the database when both share a connection, otherwise in a local DuckDB that spills to disk.
  - Discovery and generation: `dvf discover` (exact, case-insensitive, and fuzzy table and column matching, type classification, key detection), `dvf import-mapping` (Excel or CSV mapping sheets, with row-level errors), `dvf import-contract` and `dvf export-contract` (ODCS v3; unsupported rules are listed), `dvf profile`, and `dvf suggest`. API: `discover`, `import_mapping`, `import_contract`, `export_contract`, `profile`, `suggest`, `compare_runs`, `capture_baseline`, `list_baselines`.
  - `contract` check (SCH-12): schema conformance against an ODCS contract or an expected-schema file.
  - Staged connectors (`dvf.connectors.staged.StagedConnector`) for systems without SQL: datasets are fetched once as Arrow batches, written to Parquet, and queried with DuckDB.
  - Databricks dialect: `UPPER` falls back to one character at a time for strings it lengthens, verified on Apache Spark 4.
  - Packs (`dvf.packs`) register Phase 3 components; normalizers may read another column (`input_columns`) and apply to one side only (`for_side`); new dialect primitives `substring`, `date_from_iso`, and `time_from_iso`.
  - Schema checks compare types after each column's normalizers, so a normalized SAP `DATS` string is compatible with a `DATE`.
  - Connection names in nested check settings (parent tables, change tables, key maps, reference files) are validated when the configuration loads.
  - `dvf explain` prints the `nested` strategy's SQL and lists pair checks once.
  - New extras: `spark`, `pandas`, `delta`, `iceberg`, `avro`, `mongodb`, `cassandra`, `dynamodb`, `cosmos`. The dev environment includes pyspark and the open-source server drivers; CI installs Java 21 and runs MongoDB in the integration job.
  - ADR 0009 (Phase 3 big data and specialized sources).

- Phase 2 scale and debugging:
  - Two-phase row diffs: keys and row hashes for every row, then full values only for differing rows. Duplicate keys are probed before de-duplicating. The 10-million-row, 30-column local benchmark dropped from more than ten minutes to 36 to 76 seconds (target: two minutes).
  - Comparison strategies `hash_bucket` (nested bucket checksums by key hash) and `bisect` (nested integer key ranges, index friendly), with drill-down settings `buckets`, `drilldown_fanout`, and `drilldown_threshold`. Rows moved between databases grow with the number of differences.
  - Partitions (`comparison.partitions`): by values, ranges, key-hash buckets, or predicates, compared and checkpointed one at a time.
  - Parallel executor (`max_parallel_tables`) with per-connection caps (`max_parallel_queries`, `max_queries_per_connection`) and one session per worker (`Connector.fork`).
  - Checkpoints and `dvf run --resume RUN_ID` (with `--force-resume`). Interrupted runs write partial results and exit with code 2.
  - Retries with exponential backoff for transient errors, and `query_timeout_s` applied to every connection that sets none.
  - Waivers at run time (`waived` status, column waivers, expired waivers flagged), run history in DuckDB or SQLite, baselines (`last_run`, `last_passed`, a run ID) with new, resolved, and persisting failures, and trends.
  - Extracts of mismatched values and failing rows (Parquet or CSV), and repro files per failed check: per-side SQL and a one-check configuration.
  - Excel report (`dvf[excel]`) and result sinks (`duckdb`, `sqlite`, `postgres`). HTML, Markdown, and JUnit reports show waivers, baselines, trends, partitions, and artifacts.
  - Commands `explain`, `trace`, `debug`, `rerun`, and `history`; `run` options `--resume`, `--failed-from`, `--parallel`, `--keep-temp`, `--no-history`, and `--baseline`. Python API `api.explain`, `api.trace`, `api.load_run`, `api.run_history`, and the notebook helper `dvf.debug.load`.
  - Connectors: SQL Server (`dvf[mssql]`), Oracle (`dvf[oracle]`), MySQL 8.0.17 or later (`dvf[mysql]`), and, as beta (implemented and unit-tested, not yet verified against a live service), Snowflake, BigQuery, Redshift, and Databricks SQL. Each has its own SQL dialect module.
  - Component maturity: `dvf plugins list` shows `ready`, `beta`, or `declared`.
  - Portable diff SQL: counts in cross-joined derived tables (Oracle), an emulated full join (MySQL), emptiness tested by length (Oracle, SQL Server, MySQL), and the simple upper-case mapping in the reference (for example U+1F80).
  - JSON logs carry `run_id`, `suite`, and `table`.
  - Benchmarks (`uv run nox -s benchmark`) with a 15% regression check.
  - ADR 0008 (Phase 2 scale and debugging). Results schema 1.1.0 adds waivers, baseline, trend, and interruption fields.

- Phase 1 local MVP:
  - Connectors: DuckDB, SQLite, PostgreSQL (`dvf[postgres]`), and files (CSV, TSV, Parquet, JSON, NDJSON) read through DuckDB.
  - Canonicalization with a Python reference and SQL (DuckDB, PostgreSQL, SQLite) and Polars implementations, verified by Hypothesis parity tests; length-prefixed MD5 row hashes.
  - Normalizers: `trim`, `rtrim`, `ltrim`, `lower`, `upper`, `collapse_whitespace`, `unicode_nfc`, `null_if_empty`, `empty_if_null`, `round`, `strip_leading_zeros`, `truncate_timestamp`, `to_utc`.
  - Data quality checks: `not_null`, `not_blank`, `null_ratio`, `unique`, `regex`, `allowed_values`, `range`, `length`, `referential_integrity` (same or cross connection), `custom_sql`, `expression`, `row_count_range`, `freshness`; all with `where`, `mostly`, severities, masked samples, and the exact SQL run.
  - Schema checks SCH-01 to SCH-06, including conversion of missing target primary keys into data checks.
  - Comparisons: `row_count` (total and grouped), `aggregate` (control totals, overall and grouped), `profile`, `row_hash`, and `full_diff` with key reconciliation, per-column mismatch counts, side-by-side samples, and mismatch classification with fix hints.
  - Engines: `pushdown`, `duckdb`, `polars` (`dvf[polars]`), and `auto`.
  - Reports: JSON (canonical), self-contained HTML, JUnit XML, and Markdown.
  - CLI: `run`, `compare`, `report`, `init` (with `--example`), and `connections test`; exit codes, `--fail-on`, `--min-score`, `--strict`, and `--only` selectors.
  - Python API: `dvf.api.run`, `compare`, `render_reports`, and `exit_code`.
  - Plugin API: `RowCheck`, `CheckContext`, `CheckOutcome`, and the Phase 1 execution methods for connectors, comparators, normalizers, and reporters. All lifecycle hooks are called.
  - Privacy: PII column masking, e-mail detection, and a strict mode without raw values.
  - Seeded-defect generator and end-to-end acceptance tests for CSV, DuckDB, SQLite, and PostgreSQL sources against Parquet targets.
  - ADR 0007 (Phase 1 execution model).

- Phase 0 foundations:
  - Configuration system: YAML, JSON, TOML, `.properties`, and `.ini`; layering with `extends`, profiles, local overrides, environment variables, and `--set`; interpolation of environment variables, variables, and secrets; inheritance of defaults; diagnostics with file, line, and column.
  - Canonical type system with per-dialect native type parsing (through sqlglot), SAP ABAP dictionary type mapping, and a five-level compatibility classifier.
  - Versioned result model (`RunResult`, `SuiteResult`, `DatasetPairResult`, `CheckResult`) with JSON Schema.
  - Plugin registry with entry points, local plugins, decorators, API version checks, failure isolation, and declared built-ins.
  - pluggy lifecycle hookspecs, with `on_config_loaded` called after configuration loads.
  - Secret providers for environment variables and `.env` files; process-wide redaction.
  - CLI: `version`, `validate-config`, `config show`, `schema config`, `schema results`, `plugins list`.
  - JSON Schemas for configuration and results in `schemas/`.
  - Architecture document and ADRs 0001 to 0006.
  - CI on Windows, macOS, and Linux with Python 3.11 to 3.14; CodeQL; Dependabot.

### Security

- Basic-auth credentials of the SAP OData backend are no longer sent to other hosts through absolute next links or cross-origin redirects.
- Strict privacy mode no longer leaks raw key values through recorded SQL literals, profile and aggregate minimums and maximums, or observed values; minimums and maximums of PII-tagged columns are masked in standard mode.
- Configuration diagnostics no longer echo resolved secrets; `--set` values for password and token settings are redacted in recorded command lines.
- Run IDs given on the command line are validated, so they cannot traverse paths, and artifact file names can no longer be `.` or `..`.

### Fixed

- On Windows, the first Spark query in a folder without a `spark-warehouse` failed with "HADOOP_HOME and hadoop.home.dir are unset", because Spark created the warehouse through Hadoop, which needs `winutils.exe`. dvf now creates the warehouse folder before it starts a local session.
- Local Spark sessions run their Python workers with dvf's interpreter. On Windows PySpark used `python3` from `PATH`, which is another installation, because a virtual environment there has no `python3.exe`.
- Spark error messages include the Java exception instead of only "An error occurred while calling ...".
- The `sample` strategy's Wilson interval now has exactly 0 as its lower bound when no differences were sampled (and exactly 1 as its upper bound when all were); before, a rounding error could depend on the platform.
- The dev dependency group lacked the MySQL, SQL Server, and Oracle drivers, so a unit test that imports `oracledb` failed after a plain `uv sync`.
