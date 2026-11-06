# dvf: Data Validation Framework

**Configuration-driven data and schema validation for data migrations and data engineering.**

`dvf` validates schemas and data quality, reconciles source and target datasets at scale, and produces detailed, auditable reports. It is built for migration projects (SAP, on-premise databases to cloud warehouses, SQL to NoSQL) and for data engineering pipelines (PySpark, Databricks, Parquet, Avro, JSON, CSV).

> **Project status: Phase 1 (local MVP), pre-alpha.**
> dvf reads, checks, and compares data from DuckDB, SQLite, PostgreSQL, and CSV, Parquet, and JSON files, and writes HTML, JSON, JUnit, and Markdown reports.
> Scale features (bucketed hashing, parallel execution, history, waivers at run time) arrive in Phase 2; Spark, SAP, and NoSQL in Phase 3.
> See [ARCHITECTURE.md](ARCHITECTURE.md) for the roadmap and [BUILD_PROMPT.md](BUILD_PROMPT.md) for the full specification.

## What works today

| Command | What it does |
|---|---|
| `dvf init <dir> --example` | Creates a runnable local project with synthetic data and seeded discrepancies |
| `dvf run <file>` | Runs schema checks, comparisons, and data quality checks, and writes reports |
| `dvf compare <source> <target> -k <key>` | Compares two datasets without a configuration file |
| `dvf report --from results.json -f html -o <dir>` | Re-renders reports from a results document, without touching data |
| `dvf connections test <file>` | Opens every connection and runs a trivial query |
| `dvf validate-config <file>` | Validates a configuration, reporting errors with file, line, and column |
| `dvf config show <file>` | Prints the fully resolved configuration, with secrets redacted |
| `dvf schema config` / `dvf schema results` | Exports JSON Schemas for IDE autocomplete and integrations |
| `dvf plugins list` | Lists components and whether each is `ready` or `declared` (planned) |

| Area | Ready in Phase 1 |
|---|---|
| Connectors | `duckdb`, `sqlite`, `postgres` (`pip install 'dvf[postgres]'`), `files` (CSV, TSV, Parquet, JSON, NDJSON) |
| Engines | `auto`, `pushdown` (diff inside one database), `duckdb` (local, spills to disk), `polars` (`dvf[polars]`) |
| Schema checks | Object existence, column presence, type compatibility, length and precision, nullability, constraints (SCH-01 to SCH-06) |
| Data quality checks | `not_null`, `not_blank`, `null_ratio`, `unique`, `regex`, `allowed_values`, `range`, `length`, `referential_integrity`, `custom_sql`, `expression`, `row_count_range`, `freshness` |
| Comparisons | `row_count` (grouped too), `aggregate` control totals, `profile`, `row_hash`, `full_diff` with mismatch classification and fix hints |
| Reports | Self-contained HTML, canonical JSON, JUnit XML, Markdown |

Exit codes: `0` passed, `1` validation failures (at or above `--fail-on`, or below `--min-score`), `2` execution errors, `3` configuration errors.

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
