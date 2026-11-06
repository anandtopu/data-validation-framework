# Plugins

Everything in dvf is a component in one registry, including the built-ins. The design is recorded in [ADR 0005](adr/0005-plugin-api.md).

!!! note "Plugin API 0.1"
    Until dvf 1.0 the plugin API may change between minor versions. Phase 1 adds the execution methods (`plan()` and `evaluate()` for checks, data access methods for connectors). Declare the versions you support with `dvf_plugin_api`.

## Component kinds

| Kind | Entry point group | Base config model |
|---|---|---|
| `connector` | `dvf.connectors` | `ConnectionConfig` |
| `check` | `dvf.checks` | `CheckConfig` |
| `comparator` | `dvf.comparators` | none |
| `normalizer` | `dvf.normalizers` | none |
| `reporter` | `dvf.reporters` | none |
| `notifier` | `dvf.notifiers` | `NotificationConfig` |
| `secret_provider` | `dvf.secret_providers` | none |
| `engine` | `dvf.engines` | none |
| `sink` | `dvf.sinks` | none |
| lifecycle hooks | `dvf.hooks` | not applicable |

Names are lowercase and may be namespaced with dots. Use your organization as the namespace (`acme.vat_number_valid`) so names never collide with future built-ins.

## A project-local check

Put a Python file in a folder and list the folder under `plugins.paths`:

```python
# plugins/acme_checks.py
from dvf.plugins import Check, CheckConfig, Dimension, register_check


@register_check("acme.vat_number_valid")
class VatNumberValid(Check):
    """VAT numbers must be valid for their country."""

    dimension = Dimension.VALIDITY
    dvf_plugin_api = ">=0.1,<0.2"

    class Config(CheckConfig):
        column: str
        """Column holding the VAT number."""
        country_column: str | None = None
```

```yaml
plugins:
  paths: [./plugins]

suites:
  - name: finance
    # ...
    tables:
      - name: customers
        # ...
        checks:
          - { type: acme.vat_number_valid, column: VAT_ID, country_column: COUNTRY }
```

dvf validates the check's fields with your `Config` model, reports errors with line numbers, and includes the model in the exported JSON Schema.

## A distributable plugin package

Register components through entry points in your package's `pyproject.toml`:

```toml
[project.entry-points."dvf.checks"]
"acme.vat_number_valid" = "dvf_acme.checks:VatNumberValid"

[project.entry-points."dvf.hooks"]
acme = "dvf_acme.hooks"
```

An entry point may reference a class (registered under the entry point name, or the class's `plugin_name`) or a module (importing it runs its registration decorators). If a plugin fails to import, dvf records the failure, shows it in `dvf plugins list`, and keeps loading the others.

## Lifecycle hooks

Implement any subset of the hooks with `@hookimpl`, in a module or class:

```python
from dvf.plugins import hookimpl


@hookimpl
def on_config_loaded(config):
    if not config.project.metadata.get("release"):
        raise ValueError("project.metadata.release is required by our team policy")
```

| Hook | Called | Phase |
|---|---|---|
| `on_config_loaded(config)` | After configuration loads and validates | 0 |
| `modify_plan(plan)` | Before the plan runs | 1 |
| `on_run_start(run_id, config)` | When a run starts | 1 |
| `on_suite_start(run_id, suite)` | Before each suite | 1 |
| `on_check_start(run_id, check_id)` | Before each check | 1 |
| `enrich_result(result)` | After each check; add custom `details` | 1 |
| `on_check_complete(run_id, result)` | After each check is enriched | 1 |
| `on_failure(run_id, result)` | For failed or errored checks | 1 |
| `on_suite_end(run_id, result)` | After each suite | 1 |
| `on_run_end(result)` | After the run, before reports | 1 |
| `report_sections(result, report_format)` | While rendering reports; return extra sections | 1 |

## Secret providers

```python
from dvf.plugins import SecretProvider, SecretRef, register_secret_provider


@register_secret_provider("acme_vault")
class AcmeVault(SecretProvider):
    """Secrets from the ACME vault."""

    def resolve(self, ref: SecretRef) -> str:
        return fetch_from_acme_vault(ref.path, ref.key)  # your client code
```

Use it as `${secret:acme_vault:payments/db#password}`. Resolved values are redacted automatically.

## Security

Loading a plugin runs its code. Treat configuration that names plugins (and the plugin folders themselves) as code: review changes, and restrict who can modify them in shared environments.
