# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
