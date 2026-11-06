# Getting started

## Install from source

You need Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run dvf --version
```

## Validate an example

```bash
uv run dvf validate-config examples/local_files/dvf.yaml
```

```bash
uv run dvf validate-config examples/sap_to_snowflake/dvf.yaml --profile uat
```

The SAP example uses environment variables with defaults, a secret reference, a profile overlay (`dvf.uat.yaml`), and a project-local plugin check (`plugins/acme_checks.py`).

## See the resolved configuration

```bash
uv run dvf config show examples/sap_to_snowflake/dvf.yaml --profile uat
```

Every table shows its effective settings after inheritance. Secret values are replaced with `***`.

## Get autocomplete in your editor

Add this first line to a YAML configuration so editors with YAML language support (for example VS Code with the Red Hat YAML extension) validate and complete it:

```yaml
# yaml-language-server: $schema=../../schemas/config.schema.json
```

Regenerate the schema with your own plugins loaded:

```bash
uv run dvf --plugins ./plugins schema config --output .dvf/config.schema.json
```

## Use dvf from Python

```python
from dvf.api import validate_config

report = validate_config("examples/local_files/dvf.yaml")
print(report.ok, report.summary())
for diagnostic in report.diagnostics:
    print(diagnostic.format())
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (warnings allowed unless `--strict`) |
| 1 | Validation failures at or above the fail threshold (from Phase 1) |
| 2 | Execution error: connection, query, or timeout (from Phase 1) |
| 3 | Configuration or usage error |
