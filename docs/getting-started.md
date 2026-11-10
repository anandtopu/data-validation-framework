# Quickstart

Install dvf, run the example project, and open its report: about five minutes, most of it the download. You need Python 3.11 or later on Windows, macOS, or Linux.

## 1. Install

dvf is published on PyPI as `dvf-framework`; the command it installs is `dvf`. Install it in a virtual environment:

=== "Linux and macOS"

    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install dvf-framework
    dvf --version
    ```

=== "Windows (PowerShell)"

    ```powershell
    py -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install dvf-framework
    dvf --version
    ```

=== "uv or pipx"

    ```bash
    uv tool install dvf-framework     # or: pipx install dvf-framework
    dvf --version
    ```

The core package includes DuckDB and PyArrow, which is all the example needs. Drivers for PostgreSQL, SQL Server, Snowflake, Spark, and the rest are extras, such as `pip install 'dvf-framework[postgres]'`; see [Installation](installation.md).

## 2. Create the example project

```bash
dvf init demo --example
```

This writes a small project: a CSV "landing" extract and a curated Parquet copy of the same customers and orders, a `dvf.yaml` that compares them, and the configuration's JSON Schema for editor autocomplete. The synthetic data contains seeded defects, listed in `demo/data/seeded_defects.json`.

## 3. Run it

```bash
dvf run demo/dvf.yaml
```

The run takes a few seconds. It fails on purpose (exit code 1), because the target data has defects, and it reports exactly what was injected:

- 3 customers missing in the target, 2 extra, and 2 duplicated;
- 24 changed rows, each classified (`whitespace_only`, `case_only`, `encoding`, `leading_zeros`, `numeric_rounding`, `string_truncation`, `timezone_shift`, `null_vs_value`, `value_changed`), with a suggested fix per column;
- a type change (`quantity` stored as text), 2 NULL e-mails, 2 duplicate keys, and 3 orphaned orders.

The last lines name the reports it wrote:

```text
FAILED 13 passed, 2 warning, 11 failed, 2 skipped; quality score 50.0; 1.6s
waivers: 1 applied
wrote demo/reports/2026-11-08/20261108T201634Z-ca32/results.json
wrote demo/reports/2026-11-08/20261108T201634Z-ca32/report.html
wrote demo/reports/2026-11-08/20261108T201634Z-ca32/junit.xml
wrote demo/reports/2026-11-08/20261108T201634Z-ca32/summary.md
```

## 4. Open the report

Open `report.html` from the folder the run printed. It is a single file that works offline:

=== "Linux"

    ```bash
    xdg-open demo/reports/*/*/report.html
    ```

=== "macOS"

    ```bash
    open demo/reports/*/*/report.html
    ```

=== "Windows (PowerShell)"

    ```powershell
    Invoke-Item (Get-ChildItem demo\reports -Recurse -Filter report.html | Select-Object -First 1).FullName
    ```

The report shows the quality score, every table and check with its status, the evidence for each failure (counts, masked sample rows, side-by-side values with a classification per column), the SQL that ran on each side, and the waiver that accepted one known difference. The other files are `results.json` (the canonical results document every report is rendered from), `junit.xml` (for CI test tabs), and `summary.md` (for pull requests).

That is the whole loop: describe the datasets in a configuration, run it, read the report.

## Next steps

- [Your first migration comparison](tutorials/first-migration.md) builds a configuration for two databases step by step.
- [Configuration](configuration.md) explains the file format; the [configuration reference](reference/configuration.md) lists every key.
- [Checks and comparisons](validations.md) describes every validation.

### Compare two datasets without a configuration

```bash
dvf compare data/old.csv data/new.parquet --key customer_id
```

Sources and targets can be file paths or database locations: `duckdb:///path/db.duckdb::schema.table`, `sqlite:///path/db.sqlite::table`, or `postgresql://user@host/db::schema.table`. Keep passwords out of the command line; use a configuration with `${secret:...}` references for real credentials. `dvf schema-diff SOURCE TARGET` compares only the two schemas.

### Write your own configuration

```bash
dvf init my-project
```

This writes a commented starter `dvf.yaml` and its JSON Schema. Edit the connections and tables, then check, plan, and run:

```bash
dvf validate-config my-project/dvf.yaml
dvf connections test my-project/dvf.yaml
dvf plan my-project/dvf.yaml
dvf run my-project/dvf.yaml
```

`dvf discover` can generate the table and column mappings from two databases' metadata, and `dvf import-mapping` from a source-to-target mapping sheet ([Discovery and generation](discovery.md)).

### Run a subset

```bash
dvf run dvf.yaml --only table:customers --only check:not_null
```

Selectors are `suite:`, `table:`, `check:` (type or check ID), `tag:`, and `severity:` (minimum), with glob patterns. Selectors of the same kind are alternatives; different kinds must all match.

### Investigate a failure

```bash
dvf explain dvf.yaml --table customers          # the plan and SQL, without running (alias: dvf plan)
dvf trace dvf.yaml --table customers --key 1011 # one record, step by step, on both sides
dvf debug RUN_ID --config dvf.yaml              # failed checks of a run, then one in full
dvf rerun RUN_ID                                # run only what failed
```

Each failed check also gets an extract of its mismatched values and repro files (the exact SQL per side and a one-check configuration) next to the reports. See [Debugging and operations](debugging.md).

### Use dvf in CI

```bash
dvf run dvf.yaml --fail-on error --min-score 95 --format junit --format markdown
```

| Exit code | Meaning |
|---|---|
| 0 | Passed (warnings allowed unless `--strict`) |
| 1 | Validation failures at or above `--fail-on` (default `error`), or a quality score below `--min-score` |
| 2 | Execution error: a connection, query, or engine failed, or the run was interrupted |
| 3 | Configuration or usage error |

[CI/CD](ci.md) has the GitHub Action and templates for other CI systems.

### Use dvf from Python

```python
from dvf import api

output = api.run("demo/dvf.yaml", only=["table:customers"])
result = output.result
print(result.status, result.summary.quality_score)
for check in result.iter_checks():
    print(check.check_id, check.status, check.message)
print(api.exit_code(result))
```

`api.compare(source, target, keys=[...])` runs an ad hoc comparison, `api.render_reports("results.json", ["html"], "out")` re-renders reports, and `dvf.validate_dataframe(df, checks)` validates a DataFrame inside a pipeline ([In-pipeline validation](pipelines.md)). See the [Python API](reference.md).

### Get autocomplete in your editor

Add this first line to a YAML configuration so editors with YAML language support (for example VS Code with the Red Hat YAML extension) validate and complete it:

```yaml
# yaml-language-server: $schema=./dvf.schema.json
```

Regenerate the schema with your own plugins loaded:

```bash
dvf --plugins ./plugins schema config --output dvf.schema.json
```

### Run from source

To work on dvf itself, clone the repository and use [uv](https://docs.astral.sh/uv/): `uv sync` installs every development dependency, and `uv run dvf ...` runs the command from the checkout. `uv run nox` runs the linters, type checks, and tests; see `CONTRIBUTING.md`.
