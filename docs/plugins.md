# Plugins

Everything in dvf is a component in one registry, including the built-ins: connectors, checks, comparison strategies, normalizers, report formats, notification channels, secret providers, engines, and result sinks. Your own components register the same way and work everywhere a built-in does: in configuration, in the JSON Schema, in `dvf plugins list`, and in every report. The design is recorded in [ADR 0005](adr/0005-plugin-api.md).

There are three ways to customize dvf:

| Tier | When | How |
|---|---|---|
| Configuration only | A one-off rule | `custom_sql` and `expression` checks, thresholds, severities, tolerances, normalizers per column, report templates, custom `details` ([Checks and comparisons](validations.md)) |
| Project-local Python | Rules for one project | Python files in a folder listed under `plugins.paths`, or modules under `plugins.modules`, registered with decorators |
| Installable package | Rules shared across projects and teams | A package that registers components through entry points, published to an internal index or PyPI |

[Write a plugin check](tutorials/plugin-check.md) walks through the second and third tiers end to end.

## Start a plugin package

```bash
dvf plugin new check acme.vat_number_valid
```

This writes an installable package (`dvf-acme-vat-number-valid/`): a `pyproject.toml` with the entry point for the component's kind, the component class, tests, and a README. Every kind below except comparators and engines can be scaffolded: `check`, `connector`, `normalizer`, `reporter`, `notifier`, `secret_provider`, and `sink`. The generated component works as written (a regular expression check, a JSON Lines connector, a character-stripping normalizer, a CSV report, a file notifier, a file-based secret provider, a JSON Lines sink), so you start from passing tests and replace the logic:

```bash
cd dvf-acme-vat-number-valid
pip install -e ".[test]"
pytest
dvf plugins list --kind check
```

Options: `--package NAME` sets the distribution name (default `dvf-<name>`), `--output DIR` the folder, and `--force` writes into an existing folder.

## Component kinds

| Kind | Entry point group | Base class (`dvf.plugins`) | Settings model | `dvf plugin new` |
|---|---|---|---|---|
| `check` | `dvf.checks` | `RowCheck`, `Check`, `PairCheck` | nested `Config(CheckConfig)` | yes |
| `connector` | `dvf.connectors` | `Connector` (or `StagedConnector`) | nested `Config(ConnectionConfig)` | yes |
| `normalizer` | `dvf.normalizers` | `Normalizer` | `params_model` (any Pydantic model) | yes |
| `reporter` | `dvf.reporters` | `Reporter` | none (`ReportOptions` at render time) | yes |
| `notifier` | `dvf.notifiers` | `Notifier` | nested `Config(NotificationConfig)` | yes |
| `secret_provider` | `dvf.secret_providers` | `SecretProvider` | none (reads `SecretContext`) | yes |
| `sink` | `dvf.sinks` | `ResultSink` | the common `sinks[]` settings | yes |
| `comparator` | `dvf.comparators` | `Comparator` | the table's `comparison` settings | no |
| `engine` | `dvf.engines` | `Engine` (internal module) | none | no |
| lifecycle hooks | `dvf.hooks` | `@hookimpl` functions | not applicable | no |
| SAP RFC backends | `dvf.sap_rfc_backends` | `SapRfcBackend` ([SAP](sap.md#adding-a-backend)) | backend options | no |

The [components reference](reference/components.md) lists every built-in of each kind, and the [configuration reference](reference/configuration.md) every setting.

### Names

Component names are lowercase letters, digits, and underscores, optionally namespaced with dots. Use your organization as the namespace (`acme.vat_number_valid`) so names never collide with future built-ins. Secret provider names appear in `${secret:<provider>:...}` references, so they cannot contain dots: use `acme_vault`.

## How dvf finds plugins

**Installed packages** contribute components through entry points in their `pyproject.toml`:

```toml
[project.entry-points."dvf.checks"]
"acme.vat_number_valid" = "dvf_acme.checks:VatNumberValid"

[project.entry-points."dvf.hooks"]
acme = "dvf_acme.hooks"
```

An entry point may reference a class, registered under the entry point name (or the class's `plugin_name`), or a module, whose import runs its registration decorators.

**Project-local plugins** are loaded from the configuration, with paths relative to the configuration file:

```yaml
plugins:
  paths: [./plugins]          # Python files, or folders of them
  modules: [acme_dq.checks]   # importable modules
```

The global `--plugins` option does the same for one command: `dvf --plugins ./plugins schema config`. Local plugins register with decorators: `register_check`, `register_connector`, `register_comparator`, `register_normalizer`, `register_reporter`, `register_notifier`, `register_secret_provider`, `register_engine`, `register_sink`, or the generic `register(kind, name)`.

**Failure isolation.** If a plugin fails to import or declares an incompatible plugin API, dvf records the failure, shows it in `dvf plugins list` (and its JSON output), and keeps loading the others. A component name that is already registered by another source is an error unless the registration passes `replace_existing=True`.

## Settings and the JSON Schema

Checks, connectors, and notifiers declare their settings as a nested `Config` class that subclasses `CheckConfig`, `ConnectionConfig`, or `NotificationConfig`. These are Pydantic models; unknown keys are errors, and a docstring under a field becomes its description:

```python
from dvf.plugins import CheckConfig


class Config(CheckConfig):
    column: str
    """Column holding the VAT number."""
    country_column: str | None = None
    """Column holding the ISO country code; defaults to the VAT number's prefix."""
```

dvf validates each entry with the model registered for its `type`, reports mistakes with file, line, and column, and includes the model in the exported JSON Schema, so editors autocomplete your settings too:

```bash
dvf --plugins ./plugins schema config --output dvf.schema.json
```

## Checks

Most checks are "every row must satisfy a condition": subclass `RowCheck` and return a SQL condition that is true for valid rows. dvf counts the rows where it is not true, applies `where` and `mostly`, samples failing rows (masked), and records the SQL.

```python
from dvf.plugins import CheckConfig, CheckContext, Dimension, RowCheck, register_check


@register_check("acme.vat_number_valid")
class VatNumberValid(RowCheck):
    """VAT numbers must be valid for their country."""

    dimension = Dimension.VALIDITY
    dvf_plugin_api = ">=0.1,<0.2"

    class Config(CheckConfig):
        column: str
        """Column holding the VAT number."""

    def predicate(self, ctx: CheckContext) -> str:
        vat = ctx.text(ctx.col(self.config.column))
        return ctx.regex_match(vat, r"[A-Z]{2}[0-9A-Z]{8,12}")
```

```yaml
checks:
  - { type: acme.vat_number_valid, column: VAT_ID, mostly: 0.999 }
```

The `CheckContext` helpers produce SQL for the dataset's dialect, so one check runs on DuckDB, SQLite, PostgreSQL, Snowflake, Spark, files, and DataFrames:

| Helper | Returns |
|---|---|
| `ctx.col(name)` | The column as a quoted identifier (matched case-insensitively; a clear error lists the available columns) |
| `ctx.text(expr)`, `ctx.literal(value)` | A cast to text; a safely quoted literal |
| `ctx.regex_match(expr, pattern, full=True)` | A regular expression match in the dialect's syntax |
| `ctx.from_where(extra)` | `FROM <dataset> WHERE <where> AND <extra>` |
| `ctx.query(sql)`, `ctx.scalar(sql)`, `ctx.row(sql)` | Run SQL on the dataset's connection (recorded for reproducibility) |
| `ctx.sample(sql)` | Masked sample rows |
| `ctx.evaluate_predicate(condition, columns=[...])` | The full row-check evaluation |

Set `null_passes = False` on a `RowCheck` to fail rows whose checked columns are NULL, and `dimension` to the data quality dimension the check measures (`COMPLETENESS`, `UNIQUENESS`, `VALIDITY`, `CONSISTENCY`, `ACCURACY`, `TIMELINESS`, ...).

For anything else, subclass `Check` and implement `evaluate(ctx)`, returning a `CheckOutcome` (`passed`, `message`, `observed`, `expected`, `unexpected_count`, `total_count`, `samples`, `details`). dvf adds the check ID, severity, timing, queries, waivers, and hooks.

A check that needs both datasets of a comparison table, paired by key, subclasses `PairCheck` and implements `evaluate_pair(ctx)`. Its `PairContext` holds both sides' connectors, relations, schemas, and the comparison plan, plus helpers that bring selected keys and columns of both sides together (inside the database when both sides share a connection, otherwise in a local DuckDB that spills to disk), so the rule is written as SQL. Pair checks run once per table and are skipped in suites without a target. The checks in [Migration validations](migration.md) are examples.

## Connectors

A connector opens a system, resolves datasets, reads schemas, and runs queries. For a system with SQL, subclass `Connector` and implement `open`, `close`, `resolve`, `get_schema`, and `query` (plus `iter_batches`, `get_primary_key`, `list_datasets`, and `test_connection` where the system supports them), and set `dialect` to a sqlglot dialect name. The built-in connectors in `dvf.connectors` are complete examples.

For a system without SQL (an API, a document store, an application), subclass `StagedConnector` (from `dvf.plugins`) and implement `fetch(spec)` to return Arrow record batches. dvf stages them in a local Parquet file and runs every check, strategy, and engine on it with DuckDB, with bounded memory. `dvf plugin new connector` generates one that reads JSON Lines files.

```python
from typing import Any, ClassVar

import pyarrow.json as pa_json
from dvf.plugins import ConnectionConfig, StagedConnector, StagedData


class JsonLinesConnector(StagedConnector):
    """Newline-delimited JSON files in a folder, one file per dataset."""

    plugin_name: ClassVar[str] = "acme.jsonl"

    class Config(ConnectionConfig):
        path: str
        """Folder holding the <table>.jsonl files."""

    def fetch(self, spec: Any) -> StagedData:
        table = pa_json.read_json(self.base_dir / self.config.path / f"{spec.table}.jsonl")
        return StagedData(schema=table.schema, batches=table.to_batches())
```

Connector settings inherit `read_only`, `max_parallel_queries`, `query_timeout_s`, and `options` from `ConnectionConfig`. Keep connectors read-only, import drivers inside `open()` rather than at module level (so a missing driver only affects connections that use it), and raise errors that say how to fix the problem.

## Normalizers

A normalizer rewrites values before they are canonicalized and compared, on both sides of a comparison. It has three implementations that must agree: `to_sql(expr, dialect, ctype)` (pushed down to each database), `to_polars(expr, ctype)` (the `polars` engine), and `to_python(value, ctype)` (the reference, used by `dvf trace`). `applies_to(ctype)` limits it to some types; other columns pass through unchanged. Parameters are a Pydantic `params_model`:

```python
from typing import Any, ClassVar

from dvf.models import CanonicalType, LogicalType
from dvf.plugins import Normalizer, register_normalizer
from pydantic import BaseModel


@register_normalizer("acme.strip_chars")
class StripChars(Normalizer):
    """Remove characters before comparing."""

    class Params(BaseModel):
        characters: str = " -"

    params_model: ClassVar[type[BaseModel] | None] = Params

    def applies_to(self, ctype: CanonicalType) -> bool:
        return ctype.logical is LogicalType.STRING

    def to_sql(self, expr: str, dialect: Any, ctype: CanonicalType) -> str:
        for char in self.params.characters:
            expr = f"REPLACE({expr}, '{char}', '')"
        return expr

    def to_polars(self, expr: Any, ctype: CanonicalType) -> Any:
        for char in self.params.characters:
            expr = expr.str.replace_all(char, "", literal=True)
        return expr

    def to_python(self, value: Any, ctype: CanonicalType) -> Any:
        if value is None:
            return None
        for char in self.params.characters:
            value = str(value).replace(char, "")
        return value
```

```yaml
columns:
  mappings:
    - { source: phone, target: phone, normalize: [acme.strip_chars] }
    - { source: iban, target: iban, normalize: [{ name: acme.strip_chars, params: { characters: " " } }] }
```

A normalizer that reads another column of the row (as `sap_curr` reads the currency) declares it with `input_columns()`; `for_side(side)` restricts it to one side or adapts it per side; `output_type(ctype)` declares a type change. Test that the SQL and Python implementations agree on tricky values (empty strings, NULLs, Unicode, quotes); the generated package's tests show how.

## Reporters

A reporter renders the results document into files. It must not read data sources: everything it shows comes from the `RunResult`, so `dvf report --from results.json` can render the format again later.

```python
from pathlib import Path
from typing import ClassVar

from dvf.models import ArtifactRef, RunResult
from dvf.plugins import Reporter, ReportOptions, register_reporter


@register_reporter("acme.csv")
class CsvReporter(Reporter):
    """One CSV row per check."""

    format: ClassVar[str] = "acme.csv"

    def render(self, run: RunResult, out_dir: Path, options: ReportOptions) -> list[ArtifactRef]:
        path = out_dir / "checks.csv"
        lines = ["check_id,status"] + [f"{c.check_id},{c.status.value}" for c in run.iter_checks()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return [ArtifactRef(kind="report", path=str(path), format=self.format)]
```

Use it with `reports: { formats: [json, html, acme.csv] }` or `dvf run --format acme.csv`. Messages and samples in the result are already masked according to the configuration's privacy settings. The [results schema](reference/results.md) documents every field.

## Notifiers

A notification channel takes its validated settings in `__init__` and implements `send(message)`. dvf calls it after a run whose status is in the channel's `on` list (and for `dvf notify test`), with a privacy-filtered `NotificationMessage`: no sample values unless `include_samples` is set, and counts only in strict privacy mode. Raise an exception when delivery fails; an exception with `transient = True` is retried with backoff. A failing channel is reported but never fails the run, and `SecretStr` fields are redacted from errors and logs.

```python
from pydantic import SecretStr

from dvf.plugins import NotificationConfig, Notifier, register_notifier


@register_notifier("acme.pager")
class PagerNotifier(Notifier):
    """Page the on-call engineer when a run fails."""

    class Config(NotificationConfig):
        service_key: SecretStr
        """Integration key. Use a ${secret:...} reference."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def send(self, message) -> None:
        page(self.config.service_key.get_secret_value(), message.title, message.to_dict())
```

The common settings (`on`, `name`, `report_url`, `max_failures`, `counts_only`, `include_samples`, `timeout_s`, `retries`, ...) come from `NotificationConfig`; see [Notifications](notifications.md). `examples/plugins/ticket_notifier.py` opens Jira issues and ServiceNow incidents.

## Secret providers

A secret provider resolves `${secret:<provider>:<path>#<key>}` references. It receives a `SecretContext` (the process environment, the project's `.env` values, the configuration's folder, provider `options`, and `env(*names)` and `protect(value)` helpers) and implements `resolve(ref)`:

```python
from dvf.plugins import SecretProvider, SecretRef, register_secret_provider


@register_secret_provider("acme_vault")
class AcmeVault(SecretProvider):
    """Secrets from the ACME vault."""

    def resolve(self, ref: SecretRef) -> str:
        token = self.context.env("ACME_VAULT_TOKEN")
        self.context.protect(token)  # never show the provider's own credential
        return fetch_from_acme_vault(token, ref.path, ref.key)  # your client code
```

Use it as `${secret:acme_vault:payments/db#password}`. Every resolved value is registered for redaction, so it never appears in logs, `dvf config show`, or reports. Exceptions become secret errors with a redacted message; never put a secret value in an error message. `dvf validate-config` does not call providers unless `--resolve-secrets` is given. The built-in providers are listed in [Security](security.md).

## Result sinks

A sink receives every finished run, for dashboards and audit stores. It takes the common `sinks[]` settings (`path`, `url`, `schema`, `table_prefix`, `options`) and the configuration's folder, and implements `write(run)`:

```python
import json
from pathlib import Path

from dvf.models import RunResult
from dvf.plugins import ResultSink, register_sink


@register_sink("acme.jsonl")
class JsonLinesSink(ResultSink):
    """Append every check result to a JSON Lines file."""

    def write(self, run: RunResult) -> None:
        path = self.base_dir / Path(self.config.path or "results.jsonl")
        with path.open("a", encoding="utf-8") as handle:
            for check in run.iter_checks():
                row = {"run_id": run.run_id, "check_id": check.check_id, "status": check.status}
                handle.write(json.dumps(row, default=str) + "\n")
```

A failing sink is logged and noted in the results' metadata; it never fails the run.

## Comparison strategies and engines

These are advanced extension points. A `Comparator` implements `compare(ctx)` and returns a list of `CheckOutcome`; its context (`dvf.compare.context.ComparisonContext`) holds both sides, the column pairing, and the comparison plan. An engine subclasses `dvf.engines.base.Engine` and implements `diff(job)` for row-level comparisons. Both depend on types from internal modules, which follow the plugin API version but may grow fields in a minor release; read the built-in implementations before writing one, and prefer a `PairCheck` when the goal is a new rule rather than a new algorithm.

## Lifecycle hooks

Implement any subset of the hooks with `@hookimpl`, in a module or a class, and register it through the `dvf.hooks` entry point group or a local plugin path. Arguments are passed by keyword, so an implementation may accept only the ones it needs:

```python
from dvf.plugins import hookimpl


@hookimpl
def on_config_loaded(config):
    if not config.project.metadata.get("release"):
        raise ValueError("project.metadata.release is required by our team policy")


@hookimpl
def enrich_result(result):
    result.details["owner_team"] = "finance-dq"


@hookimpl
def on_failure(run_id, result):
    open_ticket(run_id, result.check_id, result.message)
```

| Hook | Called |
|---|---|
| `on_config_loaded(config)` | After configuration loads and validates |
| `modify_plan(plan)` | Before the plan runs; may reorder or filter tasks |
| `on_run_start(run_id, config)` | When a run starts |
| `on_suite_start(run_id, suite)` | Before each suite |
| `on_check_start(run_id, check_id)` | Before each check |
| `enrich_result(result)` | After each check; add entries to `details` or `metadata` |
| `on_check_complete(run_id, result)` | After each check is enriched |
| `on_failure(run_id, result)` | For failed or errored checks |
| `on_suite_end(run_id, result)` | After each suite |
| `on_run_end(result)` | After the run, before reports |
| `report_sections(result, report_format)` | While rendering reports; return `[{"title": ..., "content": ...}]` |
| `triage_failures(summary)` | After a failed run when `triage.enabled` is set; return suggested causes ([Observability](observability.md)) |

The [components reference](reference/components.md#hooks) lists the hooks with their signatures, generated from the hook specifications.

## Compatibility

- **Public API.** Plugins import only from `dvf.plugins`, `dvf.models`, and `dvf.api` (plus the documented exceptions above for staged connectors, comparators, and engines). Everything else is internal.
- **Plugin API version.** The plugin contract has its own SemVer version, shown by `dvf version` and `dvf plugins list`. Declare the versions a component supports with `dvf_plugin_api`, for example `dvf_plugin_api = ">=0.1,<0.2"`; dvf refuses to load a component outside its range and says why. `dvf plugin new` fills in the current range. Within a major version (or a minor version before 1.0), changes are additive.
- **Deprecations** emit warnings for at least one minor release before anything is removed, and are listed in the changelog.
- **Package dependency.** Require a compatible range of the distribution, such as `dependencies = ["dvf-framework>=1.0,<2"]`, and widen it after testing with a new release.

## Testing

Test plugins through the public API, with synthetic data:

| To test | Use |
|---|---|
| That an installed package registers | `new_registry(load_entry_points=True).get(PluginKind.CHECK, "acme.x")`, and that `registry.load_failures` is empty |
| A check | `dvf.validate_dataframe(pa.table({...}), [{"type": "acme.x", ...}])`: no database needed |
| A local plugin folder | `api.load_config_data({..., "plugins": {"paths": ["plugins"]}}, base_dir=...)` and its `.registry` |
| A connector, normalizer, notifier, or sink end to end | `api.load_config_data(config, base_dir=tmp_path)` then `api.run(loaded, write_reports=False, history=False)` |
| A reporter | `api.compare(a, b, keys=[...], formats=["acme.x"])`, or `api.render_reports(results_json, ["acme.x"], out_dir)` |
| A secret provider | `MyProvider(SecretContext(environ={...}, base_dir=tmp_path)).resolve(SecretRef.parse("acme_vault:path#key"))` |
| In isolation from other plugins | `with activate(new_registry()):` around imports that use decorators |

The packages generated by `dvf plugin new` contain these tests for their kind.

## Publishing

1. Name the distribution `dvf-<organization>-<purpose>` (for example `dvf-acme-checks`) and add the keywords `dvf` and `dvf-plugin` so others can find it.
2. Declare the entry points and the `dvf-framework` dependency range in `pyproject.toml`.
3. Build and check: `python -m build`, then `twine check dist/*`.
4. Publish to your internal index, or to PyPI with `twine upload dist/*` or, better, from CI with [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no long-lived tokens). [Releasing](releasing.md) shows how dvf itself does it.
5. Version with SemVer, and keep a changelog.

## Security

Loading a plugin runs its code. Treat configuration that names plugins (and the plugin folders themselves) as code: review changes, pin plugin package versions, and restrict who can modify them in shared environments. See [Security](security.md).
