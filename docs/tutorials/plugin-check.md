# Write a plugin check

dvf's built-in checks cover the common rules, and `custom_sql` and `expression` checks cover one-off rules in configuration. When a rule recurs across tables and projects, make it a check of its own: a few lines of Python that every configuration can use by name, with validated settings, autocomplete, masked samples, and recorded SQL like any built-in.

This tutorial writes a check in a project folder, tests it, and then turns it into an installable package. It continues from [Your first migration comparison](first-migration.md), but any project works.

## 1. Write the check

The rule: text values must not start or end with spaces, a common symptom of blank-padded legacy columns. Create `plugins/acme_checks.py` next to `dvf.yaml`:

```python
"""Our team's checks, loaded from the configuration's plugins.paths."""

from dvf.plugins import CheckConfig, CheckContext, Dimension, RowCheck, register_check


@register_check("acme.trimmed")
class Trimmed(RowCheck):
    """Text values must not start or end with spaces."""

    dimension = Dimension.VALIDITY
    dvf_plugin_api = ">=0.1,<0.2"

    class Config(CheckConfig):
        column: str
        """Text column to check."""

    def predicate(self, ctx: CheckContext) -> str:
        column = ctx.col(self.config.column)
        return f"{column} = TRIM({column})"
```

- `@register_check("acme.trimmed")` registers the class under a namespaced name, so it never collides with a future built-in.
- `RowCheck` is for "every row must satisfy a condition" rules. `predicate` returns a SQL condition that is true for valid rows. dvf counts the rows where it is not, applies `where` and `mostly`, captures masked sample rows, and records the SQL. NULLs pass; `not_null` is the check for those.
- `Config` declares the check's settings on top of the common ones (`severity`, `side`, `mostly`, `where`, `name`, `details`, ...). dvf validates them, reports mistakes with line numbers, and adds them to the JSON Schema.
- `ctx.col()` quotes the column for the dataset's SQL dialect and matches its name case-insensitively. `TRIM` exists in every dialect dvf supports; for anything dialect-specific, use the context helpers listed in [Plugins](../plugins.md#checks).
- `dvf_plugin_api` states which plugin API versions the check was written for; dvf refuses to load it, with a clear message, outside that range.

## 2. Use it in the configuration

Tell dvf where the plugin is, and use the check like a built-in. In `dvf.yaml`:

```yaml
plugins:
  paths: [plugins]

suites:
  - name: crm
    # ...
    tables:
      - name: customers
        # ...
        checks:
          - { type: acme.trimmed, column: customer_name }
          - { type: acme.trimmed, name: legacy_padding, side: source, column: name, severity: warning }
```

The second entry checks the legacy side, where names are padded, as a warning. The `name` keeps the two check IDs distinct.

```bash
dvf validate-config dvf.yaml
dvf run dvf.yaml
```

```text
WARNING crm.customers.acme.trimmed.name.legacy_padding: 1,000 of 1,000 rows (100%) fail
WARNING 10 passed, 1 warning, 1 waived; quality score 91.7; 1.3s
```

The warning confirms every legacy name is padded, which is why the comparison normalizes them with `rtrim`, while the warehouse column passes. `dvf plugins list --kind check` (with `--plugins plugins`, or from a configuration that names the folder) shows the check with origin `local`.

## 3. Test it

A check is code; test it like code. `dvf.validate_dataframe` runs checks against an in-memory table, so tests need no database. Create `tests/test_acme_checks.py`:

```python
from pathlib import Path

import pyarrow as pa

import dvf
from dvf import api
from dvf.models import Status

PROJECT = Path(__file__).resolve().parents[1]


def registry():
    """The default registry with our plugins folder loaded, as dvf run would load it."""
    config = {"version": 1, "project": {"name": "plugin-tests"}, "plugins": {"paths": ["plugins"]}}
    return api.load_config_data(config, base_dir=PROJECT).registry


def test_padded_values_fail():
    table = pa.table({"name": ["Ada", " Grace", "Hopper  ", None]})
    output = dvf.validate_dataframe(
        table, [{"type": "acme.trimmed", "column": "name"}], registry=registry()
    )
    (check,) = output.result.checks
    assert check.status is Status.FAILED
    assert check.unexpected_count == 2
    assert output.quarantine.column("name").to_pylist() == [" Grace", "Hopper  "]


def test_trimmed_values_pass():
    table = pa.table({"name": ["Ada", "Grace"]})
    output = dvf.validate_dataframe(
        table, [{"type": "acme.trimmed", "column": "name"}], registry=registry()
    )
    assert output.result.checks[0].status is Status.PASSED
```

```bash
pip install pytest
pytest
```

## 4. Package it for other projects

A plugins folder suits one project. To share the check across projects and teams, publish it as a package that registers through an entry point. `dvf plugin new` writes one, with a working example check, tests, and a README:

```bash
dvf plugin new check acme.trimmed --package dvf-acme-checks
```

```text
created dvf-acme-checks/pyproject.toml
created dvf-acme-checks/README.md
created dvf-acme-checks/.gitignore
created dvf-acme-checks/src/dvf_acme_checks/__init__.py
created dvf-acme-checks/src/dvf_acme_checks/py.typed
created dvf-acme-checks/src/dvf_acme_checks/checks.py
created dvf-acme-checks/tests/test_checks.py
```

The package's `pyproject.toml` declares the entry point that makes dvf find the check once the package is installed:

```toml
[project.entry-points."dvf.checks"]
"acme.trimmed" = "dvf_acme_checks.checks:Trimmed"
```

The generated `checks.py` holds a working example (a regular expression check) with the same structure as yours. Replace its `Config` fields and `predicate` with the ones from step 1, without the decorator (the entry point registers the class), and adapt the generated tests. Then install it in development mode and run its tests:

```bash
cd dvf-acme-checks
pip install -e ".[test]"
pytest
dvf plugins list --kind check
```

The check is now listed with origin `entry-point` and the package's name and version. Remove `plugins: paths: [plugins]` from the project's configuration; every project with the package installed can use `type: acme.trimmed`.

Publish the package to your internal index or PyPI with `python -m build` and `twine upload`, or from CI with Trusted Publishing. [Plugins](../plugins.md) covers every component kind, hooks, compatibility, and publishing.
