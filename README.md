# dvf: Data Validation Framework

**Configuration-driven data and schema validation for data migrations and data engineering.**

`dvf` validates schemas and data quality, reconciles source and target datasets at scale, and produces detailed, auditable reports. It is built for migration projects (SAP, on-premise databases to cloud warehouses, SQL to NoSQL) and for data engineering pipelines (PySpark, Databricks, Parquet, Avro, JSON, CSV).

> **Project status: Phase 2 (scale and debugging), pre-alpha.**
> dvf checks and compares data in local files and databases (DuckDB, SQLite, PostgreSQL, SQL Server, Oracle, MySQL 8, and the beta Snowflake, BigQuery, Redshift, and Databricks connectors), scales to large tables with bucketed checksums and parallel execution, and explains every failure.
> Spark, lakehouse formats, SAP, and NoSQL arrive in Phase 3.
> See [ARCHITECTURE.md](ARCHITECTURE.md) for the roadmap and [BUILD_PROMPT.md](BUILD_PROMPT.md) for the full specification.

## What works today

| Command | What it does |
|---|---|
| `dvf init <dir> --example` | Creates a runnable local project with synthetic data and seeded discrepancies |
| `dvf run <file>` | Runs schema checks, comparisons, and data quality checks, and writes reports |
| `dvf compare <source> <target> -k <key>` | Compares two datasets without a configuration file |
| `dvf run <file> --resume <run_id>` | Continues an interrupted or killed run from its checkpoints |
| `dvf explain <file>` | Shows the plan and the SQL each step would run, without running it |
| `dvf trace <file> --table <t> --key <k>` | Follows one record through every normalization step on both sides |
| `dvf debug <run_id> [check]`, `dvf rerun <run_id>`, `dvf history <file>` | Drills into an earlier run, reruns what failed, lists runs |
| `dvf report --from results.json -f html -o <dir>` | Re-renders reports from a results document, without touching data |
| `dvf connections test <file>` | Opens every connection and runs a trivial query |
| `dvf validate-config <file>` | Validates a configuration, reporting errors with file, line, and column |
| `dvf config show <file>` | Prints the fully resolved configuration, with secrets redacted |
| `dvf schema config` / `dvf schema results` | Exports JSON Schemas for IDE autocomplete and integrations |
| `dvf plugins list` | Lists components: `ready`, `beta` (implemented, not yet verified against a live system), or `declared` (planned) |

| Area | Ready |
|---|---|
| Connectors | `duckdb`, `sqlite`, `files` (CSV, TSV, Parquet, JSON, NDJSON), `postgres`, `mssql`, `oracle`, `mysql` (each with its pip extra, for example `pip install 'dvf[mssql]'`); beta: `snowflake`, `bigquery`, `redshift`, `databricks` |
| Engines | `auto`, `pushdown` (diff inside one database), `duckdb` (local, spills to disk, memory limit), `polars` (`dvf[polars]`); full diffs fetch values only for rows that differ |
| Schema checks | Object existence, column presence, type compatibility, length and precision, nullability, constraints (SCH-01 to SCH-06) |
| Data quality checks | `not_null`, `not_blank`, `null_ratio`, `unique`, `regex`, `allowed_values`, `range`, `length`, `referential_integrity`, `custom_sql`, `expression`, `row_count_range`, `freshness` |
| Comparisons | `row_count` (grouped too), `aggregate` control totals, `profile`, `row_hash`, `hash_bucket` and `bisect` (checksum search that moves only differing rows), `full_diff` with mismatch classification and fix hints; partitions |
| Execution | Parallel tables with per-connection caps, checkpoints and resume, retries, waivers, run history with baselines and trends |
| Reports | Self-contained HTML, canonical JSON, JUnit XML, Markdown, Excel (`dvf[excel]`); extracts and repro files per failed check; result sinks (DuckDB, SQLite, PostgreSQL) |

Exit codes: `0` passed, `1` validation failures (at or above `--fail-on`, or below `--min-score`), `2` execution errors or an interrupted run, `3` configuration errors.

## Quickstart (from source)

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repository-url> dvf
```

```bash
cd dvf
```

```bash
uv sync
```

```bash
uv run dvf init demo --example
```

```bash
uv run dvf run demo/dvf.yaml
```

The run finds the seeded defects (exit code 1) and writes `report.html`, `results.json`, `junit.xml`, and `summary.md` under `demo/reports/`. Each mismatched column is classified, for example as `whitespace_only`, `numeric_rounding`, or `timezone_shift`, with a suggested fix.

## Configuration at a glance

```yaml
# yaml-language-server: $schema=../../schemas/config.schema.json
version: 1
project:
  name: local-files-demo

connections:
  landing: { type: files, path: ./data/landing, format: csv }
  curated: { type: files, path: ./data/curated, format: parquet }

suites:
  - name: customers
    source: landing
    target: curated
    tables:
      - name: customers
        source: { path: customers.csv }
        target: { path: customers.parquet }
        keys: [customer_id]
        comparison: { strategies: [row_count, aggregate, row_hash] }
        checks:
          - { type: not_null, columns: [customer_id, email] }
          - { type: unique, columns: [customer_id] }
          - { type: regex, column: email, pattern: "^[^@]+@[^@]+$", mostly: 0.99 }
```

Configuration supports YAML, JSON, TOML, `.properties`, and `.ini`, with:

- `extends:` for shared fragments
- environment profiles (`--profile uat`)
- `DVF__SECTION__KEY` environment overrides and `--set path=value`
- `${env:NAME}`, `${var:name}`, and `${secret:provider:reference}` interpolation
- defaults inherited from project to suite to table

See [docs/configuration.md](docs/configuration.md).

## Extending dvf

Everything is a plugin, including the built-ins. Register a custom check in a local `plugins/` folder:

```python
from dvf.plugins import Check, CheckConfig, Dimension, register_check


@register_check("acme.vat_number_valid")
class VatNumberValid(Check):
    """VAT numbers must be valid for their country."""

    dimension = Dimension.VALIDITY

    class Config(CheckConfig):
        column: str
        country_column: str | None = None
```

Or publish it as a package with an entry point in the `dvf.checks` group. See [docs/plugins.md](docs/plugins.md).

## Development

```bash
uv sync --all-groups
```

```bash
uv run nox
```

The default `nox` sessions run lint, type checks, and tests. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
