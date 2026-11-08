# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
  - Connectors: SQL Server (`dvf[mssql]`), Oracle (`dvf[oracle]`), MySQL and MariaDB (`dvf[mysql]`), and, as beta (implemented and unit-tested, not yet verified against a live service), Snowflake, BigQuery, Redshift, and Databricks SQL. Each has its own SQL dialect module.
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
