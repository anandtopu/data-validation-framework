# Getting started

## Install

You need Python 3.11 or later. From a clone of the repository, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

```bash
uv run dvf --version
```

DuckDB and PyArrow are included. Add the PostgreSQL connector or the Polars engine with extras: `pip install 'dvf[postgres]'`, `pip install 'dvf[polars]'`, or `pip install 'dvf[all]'`.

## Run the example in one minute

```bash
uv run dvf init demo --example
```

```bash
uv run dvf run demo/dvf.yaml
```

The example compares a CSV landing extract with curated Parquet files. Its synthetic data contains seeded defects, so the run fails on purpose (exit code 1) and reports exactly what was injected:

- 3 customers missing in the target, 2 extra, and 2 duplicated;
- 24 changed rows, each classified: `whitespace_only`, `case_only`, `encoding`, `leading_zeros`, `numeric_rounding`, `string_truncation`, `timezone_shift`, `null_vs_value`, and `value_changed`, with a suggested fix per column;
- a type change (`quantity` stored as text), 2 NULL e-mails, 2 duplicate keys, and 3 orphaned orders.

Reports land in `demo/reports/<date>/<run id>/`: `report.html` (open it in a browser; it works offline), `results.json` (the canonical document), `junit.xml` (for CI test tabs), and `summary.md` (for pull requests). The list of injected defects is in `demo/data/seeded_defects.json`.

## Compare two datasets without a configuration

```bash
uv run dvf compare data/old.csv data/new.parquet --key customer_id
```

Sources and targets can be file paths or database locations: `duckdb:///path/db.duckdb::schema.table`, `sqlite:///path/db.sqlite::table`, or `postgresql://user@host/db::schema.table` (keep passwords out of the command line; use a configuration with `${secret:...}` for real credentials).

## Write your own configuration

```bash
uv run dvf init my-project
```

This writes a commented starter `dvf.yaml` and its JSON Schema. Edit the connections and tables, then:

```bash
uv run dvf validate-config my-project/dvf.yaml
```

```bash
uv run dvf connections test my-project/dvf.yaml
```

```bash
uv run dvf run my-project/dvf.yaml
```

See [Configuration](configuration.md) for the file format and [Checks and comparisons](validations.md) for every validation.

## Run a subset

```bash
uv run dvf run dvf.yaml --only table:customers --only check:not_null
```

Selectors are `suite:`, `table:`, `check:` (type or check ID), `tag:`, and `severity:` (minimum), with glob patterns. Selectors of the same kind are alternatives; different kinds must all match.

## Use dvf in CI

```bash
uv run dvf run dvf.yaml --fail-on error --min-score 95 --format junit --format markdown
```

| Exit code | Meaning |
|---|---|
| 0 | Passed (warnings allowed unless `--strict`) |
| 1 | Validation failures at or above `--fail-on` (default `error`), or a quality score below `--min-score` |
| 2 | Execution error: a connection, query, or engine failed |
| 3 | Configuration or usage error |

Publish `junit.xml` to your CI's test tab and append `summary.md` to `$GITHUB_STEP_SUMMARY`.

## Use dvf from Python

```python
from dvf import api

output = api.run("demo/dvf.yaml", only=["table:customers"])
result = output.result
print(result.status, result.summary.quality_score)
for check in result.iter_checks():
    print(check.check_id, check.status, check.message)
print(api.exit_code(result))
```

`api.compare(source, target, keys=[...])` runs an ad hoc comparison, and `api.render_reports("results.json", ["html"], "out")` re-renders reports.

## Get autocomplete in your editor

Add this first line to a YAML configuration so editors with YAML language support (for example VS Code with the Red Hat YAML extension) validate and complete it:

```yaml
# yaml-language-server: $schema=./dvf.schema.json
```

Regenerate the schema with your own plugins loaded:

```bash
uv run dvf --plugins ./plugins schema config --output dvf.schema.json
```
