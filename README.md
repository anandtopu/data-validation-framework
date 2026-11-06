# dvf: Data Validation Framework

**Configuration-driven data and schema validation for data migrations and data engineering.**

`dvf` validates schemas and data quality, reconciles source and target datasets at scale, and produces detailed, auditable reports. It is built for migration projects (SAP, on-premise databases to cloud warehouses, SQL to NoSQL) and for data engineering pipelines (PySpark, Databricks, Parquet, Avro, JSON, CSV).

> **Project status: Phase 0 (foundations), pre-alpha.**
> The configuration system, canonical type system, result model, plugin registry, and CLI skeleton are in place.
> **No data is read or compared yet.** Connectors, checks, comparisons, and reports arrive in Phase 1.
> See [ARCHITECTURE.md](ARCHITECTURE.md) for the roadmap and [BUILD_PROMPT.md](BUILD_PROMPT.md) for the full specification.

## What works today

| Command | What it does |
|---|---|
| `dvf --version` / `dvf version` | Shows the version, plus Python, platform, schema, and plugin API versions |
| `dvf validate-config <file>` | Loads and validates a configuration, reporting errors with file, line, and column |
| `dvf config show <file>` | Prints the fully resolved configuration (layers, profiles, inheritance applied), with secrets redacted |
| `dvf schema config` / `dvf schema results` | Exports JSON Schemas for IDE autocomplete and integrations |
| `dvf plugins list` | Lists registered connectors, checks, comparators, normalizers, reporters, notifiers, engines, and secret providers, with their status |

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
uv run dvf validate-config examples/local_files/dvf.yaml
```

```bash
uv run dvf plugins list
```

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
