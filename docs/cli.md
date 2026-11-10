# Command line

!!! note "Generated page"
    This page is generated from the `dvf` command line application by `uv run nox -s docs_reference`. Do not edit it by hand; change the source and regenerate.

Every command and option. `dvf COMMAND --help` prints the same information. Options that take a value accept `--option value` or `--option=value`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Passed (warnings allowed unless `--strict`). |
| `1` | Validation failures at or above `--fail-on`, a quality score below `--min-score`, or a `--check` that found work to do. |
| `2` | Execution error: a connection, query, or engine failed. |
| `3` | Configuration or usage error. |

## Commands

| Command | Purpose |
|---|---|
| [`dvf baseline capture`](#dvf-baseline-capture) | Snapshot per-table metrics (row counts, control totals, profiles, grouped counts) before a migration, for the baseline_metrics check and go/no-go decisions. |
| [`dvf baseline list`](#dvf-baseline-list) | List the named baselines in the configuration's state folder. |
| [`dvf compare`](#dvf-compare) | Compare two datasets without a configuration file. |
| [`dvf config migrate`](#dvf-config-migrate) | Upgrade a configuration file to the current format version. |
| [`dvf config show`](#dvf-config-show) | Print the fully resolved configuration with secrets redacted. |
| [`dvf connections test`](#dvf-connections-test) | Open every connection in a configuration and run a trivial query. |
| [`dvf dbt import`](#dvf-dbt-import) | Generate a configuration with one table per dbt model, from a dbt manifest. |
| [`dvf debug`](#dvf-debug) | Drill into a check of an earlier run: evidence, mismatches, SQL, timing, and repro files. |
| [`dvf discover`](#dvf-discover) | Introspect a source and a target, match tables and columns, and generate a configuration. |
| [`dvf explain`](#dvf-explain) | Show the plan and the SQL each step would run, without running validations. |
| [`dvf export-contract`](#dvf-export-contract) | Write an ODCS v3.1 data contract from a configuration's datasets and checks. |
| [`dvf history compare`](#dvf-history-compare) | Compare two runs, such as two mock cutover rehearsals: new, resolved, and persisting failures, status changes, and metric deltas from RUN_A to RUN_B. |
| [`dvf history list`](#dvf-history-list) | List recent runs of the configuration's project. |
| [`dvf import-contract`](#dvf-import-contract) | Convert an ODCS v3 data contract into schema and quality checks. |
| [`dvf import-mapping`](#dvf-import-mapping) | Convert a source-to-target mapping sheet into a configuration. |
| [`dvf init`](#dvf-init) | Create a dvf project. With --example, a runnable local demo with seeded defects. |
| [`dvf notify test`](#dvf-notify-test) | Send a test message to the configured notification channels. |
| [`dvf plan`](#dvf-plan) | Alias of 'dvf explain': show the plan and the SQL each step would run, without running validations. |
| [`dvf plugin new`](#dvf-plugin-new) | Create an installable plugin package: pyproject.toml with the entry point, the component, tests, and a README. The generated component works as written; replace its logic with your own. |
| [`dvf plugins list`](#dvf-plugins-list) | List components: 'ready' ones are implemented and verified, 'beta' ones are implemented but not yet verified against a live system, and 'declared' ones arrive in a later phase. |
| [`dvf profile`](#dvf-profile) | Profile columns (NULLs, distinct values, min, max, lengths, top values) inside each database. Exit code 2 when a dataset could not be profiled. |
| [`dvf report`](#dvf-report) | Re-render reports from a results document, without touching any data. |
| [`dvf rerun`](#dvf-rerun) | Run again only the checks that failed or errored in an earlier run. |
| [`dvf run`](#dvf-run) | Run the validations in a configuration and write reports. |
| [`dvf sap backends`](#dvf-sap-backends) | List the sap_rfc backends: ready, or declared without an implementation. |
| [`dvf sap copy-template`](#dvf-sap-copy-template) | Copy a template next to your configuration, to use with 'extends:'. |
| [`dvf sap templates`](#dvf-sap-templates) | List the SAP business object templates and the variables each one needs. |
| [`dvf schema config`](#dvf-schema-config) | Export the configuration JSON Schema (including types from loaded plugins). |
| [`dvf schema results`](#dvf-schema-results) | Export the results JSON Schema. |
| [`dvf schema-diff`](#dvf-schema-diff) | Compare the column schemas of configured table pairs, or of two datasets, without running checks: missing and extra columns, type compatibility, and nullability. |
| [`dvf suggest`](#dvf-suggest) | Suggest candidate checks from column profiles, as a YAML fragment with evidence. |
| [`dvf trace`](#dvf-trace) | Follow one record through both sides: raw value, each normalizer, canonical value, hash. |
| [`dvf validate-config`](#dvf-validate-config) | Validate a configuration file and report every problem with its file, line, and column. |
| [`dvf version`](#dvf-version) | Show versions of dvf, its schemas, and its environment. |
| [`dvf waivers list`](#dvf-waivers-list) | List waivers with their ticket, approver, expiry date, and status: active, expiring soon, or expired. Secrets are not fetched. |

## `dvf` {#dvf}

Data and schema validation for data migrations and data engineering.

Global options go before the command: `dvf --plugins ./plugins schema config`.

```text
dvf [OPTIONS] COMMAND [ARGS]...
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `--version` | flag |  | Show the version and exit. |
| `--log-level` | one of `debug`, `info`, `warning`, `error` | `warning` | Logging level. Environment variable: `DVF_LOG_LEVEL`. |
| `--log-format` | one of `text`, `json` | `text` | Log output format. Environment variable: `DVF_LOG_FORMAT`. |
| `--plugins` | text, repeatable |  | Plugin file, folder, or module to load. Repeatable. |
| `--install-completion` | flag |  | Install completion for the current shell. |
| `--show-completion` | flag |  | Show completion for the current shell, to copy it or customize the installation. |

## `dvf baseline` {#dvf-baseline}

Capture and list pre-migration metric baselines (MIG-14).

Subcommands: [`dvf baseline capture`](#dvf-baseline-capture), [`dvf baseline list`](#dvf-baseline-list).

```text
dvf baseline [OPTIONS] COMMAND [ARGS]...
```

### `dvf baseline capture` {#dvf-baseline-capture}

Snapshot per-table metrics (row counts, control totals, profiles, grouped counts)
before a migration, for the baseline_metrics check and go/no-go decisions.

```text
dvf baseline capture [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--name`, `-n` | text | **required** | Baseline name, for example pre_cutover. |
| `--side` | text | `source` | Dataset to capture: source or target. |
| `--only` | text, repeatable |  | Selector such as table:customers. Repeatable. |
| `--overwrite` | flag |  | Replace an existing baseline of that name. |
| `--no-distinct` | flag |  | Skip distinct counts (faster on large tables). |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--format` | one of `text`, `json` | `text` | text or json. |

### `dvf baseline list` {#dvf-baseline-list}

List the named baselines in the configuration's state folder.

```text
dvf baseline list [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--format` | one of `text`, `json` | `text` | text or json. |

## `dvf compare` {#dvf-compare}

Compare two datasets without a configuration file.

```text
dvf compare [OPTIONS] SOURCE TARGET
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `SOURCE` | text | **required** | A file path, or duckdb:///db::table, sqlite:///db::table, postgresql://...::schema.table. |
| `TARGET` | text | **required** | The target, in the same forms as the source. |
| `--key`, `-k` | text, repeatable |  | Key column (repeat for composite keys). |
| `--strategy`, `-s` | text, repeatable |  | Comparison strategy. Repeatable. |
| `--engine` | text | `auto` | auto, pushdown, duckdb, or polars. |
| `--format`, `-f` | text, repeatable |  | Report format (json is always written). Repeatable. |
| `--output-dir`, `-o` | path |  | Write reports here instead of reports.output_dir. |
| `--no-reports` | flag |  | Do not write report files. |
| `--sample-size` | integer | `20` | Sample rows per difference. |
| `--fail-on` | one of `info`, `warning`, `error`, `critical` | `error` | Exit 1 when a check at or above this severity fails. |
| `--verbose`, `-v` | flag |  | List every problem. |

## `dvf config` {#dvf-config}

Inspect and upgrade configuration.

Subcommands: [`dvf config migrate`](#dvf-config-migrate), [`dvf config show`](#dvf-config-show).

```text
dvf config [OPTIONS] COMMAND [ARGS]...
```

### `dvf config migrate` {#dvf-config-migrate}

Upgrade a configuration file to the current format version.

The original is kept next to it as <file>.v<N>.bak (comments are not carried over to the
upgraded file). Files listed in 'extends:' are migrated separately.

```text
dvf config migrate [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file to upgrade. |
| `--check` | flag |  | Only report whether the file needs migrating (exit 1 if it does). |
| `--output`, `-o` | path |  | Write the upgraded configuration here (YAML or JSON) instead of in place. |
| `--no-backup` | flag |  | Do not keep a copy of the original file. |
| `--from-version` | integer (>= 0) |  | Version of a file without 'version:' (an 'extends:' fragment). |
| `--format` | one of `text`, `json` | `text` | text or json. |

### `dvf config show` {#dvf-config-show}

Print the fully resolved configuration with secrets redacted.

```text
dvf config show [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--env-file` | path |  | A .env file (default: .env next to the config). |
| `--resolve-secrets` / `--no-resolve-secrets` | flag | `--no-resolve-secrets` | Fetch secret values to prove they resolve. |
| `--strict` | flag |  | Treat warnings (such as missing environment variables) as errors. |
| `--format` | one of `yaml`, `json` | `yaml` | Output format. |

## `dvf connections` {#dvf-connections}

Work with connections.

Subcommands: [`dvf connections test`](#dvf-connections-test).

```text
dvf connections [OPTIONS] COMMAND [ARGS]...
```

### `dvf connections test` {#dvf-connections-test}

Open every connection in a configuration and run a trivial query.

```text
dvf connections test [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--env-file` | path |  | A .env file (default: .env next to the config). |

## `dvf dbt` {#dvf-dbt}

Work with dbt projects.

Subcommands: [`dvf dbt import`](#dvf-dbt-import).

```text
dvf dbt [OPTIONS] COMMAND [ARGS]...
```

### `dvf dbt import` {#dvf-dbt-import}

Generate a configuration with one table per dbt model, from a dbt manifest.

dbt generic tests (not_null, unique, accepted_values, relationships, and common
dbt_utils and dbt_expectations tests) become checks; tests without an equivalent are
listed at the end of the file. Run it after 'dbt build' or 'dbt compile'.

```text
dvf dbt import [OPTIONS] MANIFEST
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `MANIFEST` | path | **required** | dbt manifest.json (in the dbt project's target folder). |
| `--config`, `-c` | path |  | Copy the connection from this configuration file. |
| `--connection` | text |  | With --config: the connection to copy (if it has several). |
| `--location` | text |  | The warehouse as a location (postgresql://..., duckdb:///file) instead of a connection derived from the manifest's adapter. |
| `--model`, `-m` | text, repeatable |  | Only models whose name matches this glob. Repeatable. |
| `--tag` | text, repeatable |  | Only models with this dbt tag. Repeatable. |
| `--exclude` | text, repeatable |  | Leave out models whose name matches this glob. Repeatable. |
| `--all-packages` | flag |  | Include models of installed dbt packages. |
| `--no-tests` | flag |  | Do not convert dbt tests into checks. |
| `--no-row-count` | flag |  | Do not add a row_count_range (min 1) check. |
| `--suite-name` | text |  | Name of the generated suite (dbt_models). |
| `--project-name` | text |  | Project name (default: the dbt project's name). |
| `--output`, `-o` | path |  | Write the configuration here (default: standard output). |
| `--force` | flag |  | Replace an existing output file. |
| `--format` | one of `text`, `json` | `text` | Summary format: text, or json (on standard output). |

## `dvf debug` {#dvf-debug}

Drill into a check of an earlier run: evidence, mismatches, SQL, timing, and repro files.

```text
dvf debug [OPTIONS] RUN_ID [CHECK_ID]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `RUN_ID` | text | **required** | Run ID, or the path of a results.json. |
| `CHECK_ID` | text |  | Check ID (or its end); omit to list failed checks. |
| `--config`, `-c` | path |  | Configuration (to find its .dvf folder). |
| `--format` | one of `text`, `json` | `text` | text or json. |

## `dvf discover` {#dvf-discover}

Introspect a source and a target, match tables and columns, and generate a configuration.

Names are matched exactly, then case-insensitively, then fuzzily. Primary keys come from
the catalogs. Review the comments marked REVIEW in the generated file.

```text
dvf discover [OPTIONS] [CONFIG]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `CONFIG` | path |  | Configuration holding the two connections (optional). |
| `--source` | text |  | With a configuration: the source connection's name. Without: a location (postgresql://..., duckdb:///file, sqlite:///file, or a folder of files). |
| `--target` | text |  | The target connection's name, or a location (as --source). |
| `--source-schema` | text |  | Only tables in this source schema. |
| `--target-schema` | text |  | Only tables in this target schema. |
| `--source-pattern` | text | `*` | Glob over source table names or files. |
| `--target-pattern` | text | `*` | Glob over target table names or files. |
| `--threshold` | number (>= 0.01, <= 1.0) | `0.8` | Lowest fuzzy match score accepted (0-1). |
| `--no-fuzzy` | flag |  | Match names exactly and case-insensitively only. |
| `--probe-keys` | flag |  | Infer keys of tables without a primary key by testing candidate columns for uniqueness in the source (one query per such table). |
| `--suite-name` | text | `discovered` | Name of the generated suite. |
| `--project-name` | text | `discovered` | Project name of the generated configuration. |
| `--profile`, `-p` | text |  | Environment profile of the configuration. |
| `--output`, `-o` | path |  | Write the generated file here (default: standard output). |
| `--force` | flag |  | Replace an existing output file. |
| `--format` | one of `text`, `json` | `text` | Summary format: text, or json (on standard output). |

## `dvf explain` {#dvf-explain}

Show the plan and the SQL each step would run, without running validations.

```text
dvf explain [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--table`, `-t` | text |  | Only this table. |
| `--suite` | text |  | Only tables of this suite. |
| `--only` | text, repeatable |  | Selectors, as for dvf run. Repeatable. |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--format` | one of `text`, `json` | `text` | text or json. |
| `--no-sql` | flag |  | Leave out the generated SQL. |

## `dvf export-contract` {#dvf-export-contract}

Write an ODCS v3.1 data contract from a configuration's datasets and checks.

```text
dvf export-contract [OPTIONS] CONFIG
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `CONFIG` | path | **required** | Configuration whose datasets to describe. |
| `--table`, `-t` | text, repeatable |  | Only tables matching this glob. Repeatable. |
| `--suite` | text |  | Only tables of this suite. |
| `--side` | one of `auto`, `source`, `target`, `both` | `auto` | auto (target of comparison suites), source, or target. |
| `--id` | text |  | Contract id. |
| `--version` | text | `1.0.0` | Contract version. |
| `--status` | text | `draft` | Contract status. |
| `--profile`, `-p` | text |  | Environment profile of the configuration. |
| `--output`, `-o` | path |  | Write the generated file here (default: standard output). |
| `--force` | flag |  | Replace an existing output file. |
| `--format` | one of `text`, `json` | `text` | Summary format: text, or json (on standard output). |

## `dvf history` {#dvf-history}

Run history: list recent runs, or compare two runs (mock cutover rehearsals).

Subcommands: [`dvf history compare`](#dvf-history-compare), [`dvf history list`](#dvf-history-list).

```text
dvf history [OPTIONS] COMMAND [ARGS]...
```

### `dvf history compare` {#dvf-history-compare}

Compare two runs, such as two mock cutover rehearsals: new, resolved, and persisting
failures, status changes, and metric deltas from RUN_A to RUN_B.

```text
dvf history compare [OPTIONS] RUN_A RUN_B
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `RUN_A` | text | **required** | The earlier run: run ID or results.json. |
| `RUN_B` | text | **required** | The later run: run ID or results.json. |
| `--config`, `-c` | path |  | Configuration (to find its .dvf folder). |
| `--fail-on-new` | flag |  | Exit with 1 when run B has new failures. |
| `--format` | one of `text`, `json` | `text` | text or json. |

### `dvf history list` {#dvf-history-list}

List recent runs of the configuration's project.

```text
dvf history list [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--limit`, `-n` | integer (>= 1) | `20` | Runs to show. |
| `--format` | one of `text`, `json` | `text` | text or json. |

## `dvf import-contract` {#dvf-import-contract}

Convert an ODCS v3 data contract into schema and quality checks.

Rules dvf cannot run are listed with the reason, never dropped silently.

```text
dvf import-contract [OPTIONS] CONTRACT
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `CONTRACT` | path | **required** | ODCS v3 contract (YAML or JSON). |
| `--server` | text |  | Server of the contract to connect to (default: the first). |
| `--config`, `-c` | path |  | Copy connections from this configuration file. |
| `--connection` | text |  | With --config: the connection to use. |
| `--source` | text |  | A location instead of the contract's servers. |
| `--suite-name` | text |  | Name of the generated suite. |
| `--project-name` | text |  | Project name of the generated configuration. |
| `--no-conformance` | flag |  | Leave out the SCH-12 'contract' checks. |
| `--between` | one of `exclusive`, `inclusive` | `exclusive` | How to read mustBeBetween bounds. |
| `--output`, `-o` | path |  | Write the generated file here (default: standard output). |
| `--force` | flag |  | Replace an existing output file. |
| `--format` | one of `text`, `json` | `text` | Summary format: text, or json (on standard output). |

## `dvf import-mapping` {#dvf-import-mapping}

Convert a source-to-target mapping sheet into a configuration.

Every problem is reported with its row; nothing is written until the sheet is valid.

```text
dvf import-mapping [OPTIONS] MAPPING
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `MAPPING` | path | **required** | Mapping sheet: .xlsx, .xlsm, or .csv. |
| `--sheet` | text |  | Excel sheet (default: the first with a header row). |
| `--config`, `-c` | path |  | Copy connections from this configuration file. |
| `--source` | text |  | With a configuration: the source connection's name. Without: a location (postgresql://..., duckdb:///file, sqlite:///file, or a folder of files). |
| `--target` | text |  | The target connection's name, or a location (as --source). |
| `--suite-name` | text |  | Name of the generated suite. |
| `--project-name` | text |  | Project name of the generated configuration. |
| `--include-unlisted` | flag |  | Also compare columns the sheet does not list (default: only listed ones). |
| `--output`, `-o` | path |  | Write the generated file here (default: standard output). |
| `--force` | flag |  | Replace an existing output file. |
| `--format` | one of `text`, `json` | `text` | Summary format: text, or json (on standard output). |

## `dvf init` {#dvf-init}

Create a dvf project. With --example, a runnable local demo with seeded defects.

```text
dvf init [OPTIONS] [DIRECTORY]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `DIRECTORY` | path | `.` | Project folder to create. |
| `--example` | flag |  | Include synthetic data with seeded discrepancies. |
| `--force` | flag |  | Overwrite an existing dvf.yaml. |

## `dvf notify` {#dvf-notify}

Work with notification channels.

Subcommands: [`dvf notify test`](#dvf-notify-test).

```text
dvf notify [OPTIONS] COMMAND [ARGS]...
```

### `dvf notify test` {#dvf-notify-test}

Send a test message to the configured notification channels.

The message says it is a test and carries no run data. Exit codes: 0 every message was
delivered, 2 a delivery failed, 3 configuration errors (or no channels).

```text
dvf notify test [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--channel`, `-c` | text |  | Send only to this channel (its name, or its type when unnamed), even if it is disabled. |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--env-file` | path |  | A .env file (default: .env next to the config). |
| `--format` | one of `text`, `json` | `text` | Output format. |

## `dvf plan` {#dvf-plan}

Alias of 'dvf explain': show the plan and the SQL each step would run, without running validations.

```text
dvf plan [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--table`, `-t` | text |  | Only this table. |
| `--suite` | text |  | Only tables of this suite. |
| `--only` | text, repeatable |  | Selectors, as for dvf run. Repeatable. |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--format` | one of `text`, `json` | `text` | text or json. |
| `--no-sql` | flag |  | Leave out the generated SQL. |

## `dvf plugin` {#dvf-plugin}

Create plugin packages.

Subcommands: [`dvf plugin new`](#dvf-plugin-new).

```text
dvf plugin [OPTIONS] COMMAND [ARGS]...
```

### `dvf plugin new` {#dvf-plugin-new}

Create an installable plugin package: pyproject.toml with the entry point, the
component, tests, and a README. The generated component works as written; replace its
logic with your own.

```text
dvf plugin new [OPTIONS] KIND NAME
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `KIND` | one of `check`, `connector`, `normalizer`, `reporter`, `notifier`, `secret_provider`, `sink` | **required** | Kind of component. |
| `NAME` | text | **required** | Component name, namespaced with dots: acme.vat_number_valid (secret providers: acme_vault). |
| `--output`, `-o` | path |  | Folder to create (default: ./<package name>). |
| `--package` | text |  | Distribution name (default: dvf-<name>). |
| `--force` | flag |  | Write into an existing, non-empty folder. |

## `dvf plugins` {#dvf-plugins}

Inspect installed plugins and built-in components.

Subcommands: [`dvf plugins list`](#dvf-plugins-list).

```text
dvf plugins [OPTIONS] COMMAND [ARGS]...
```

### `dvf plugins list` {#dvf-plugins-list}

List components: 'ready' ones are implemented and verified, 'beta' ones are implemented but not
yet verified against a live system, and 'declared' ones arrive in a later phase.

```text
dvf plugins list [OPTIONS]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `--kind`, `-k` | one of `connector`, `check`, `comparator`, `normalizer`, `reporter`, `notifier`, `secret_provider`, `engine`, `sink` |  | Only this kind of component. |
| `--format` | one of `text`, `json` | `text` | Output format. |

## `dvf profile` {#dvf-profile}

Profile columns (NULLs, distinct values, min, max, lengths, top values) inside each
database. Exit code 2 when a dataset could not be profiled.

```text
dvf profile [OPTIONS] CONFIG
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `CONFIG` | path | **required** | Configuration whose datasets to profile. |
| `--table`, `-t` | text, repeatable |  | Only tables matching this glob. Repeatable. |
| `--suite` | text |  | Only tables of this suite. |
| `--side` | one of `auto`, `source`, `target`, `both` | `auto` | auto (target of comparison suites, source otherwise), source, target, or both. |
| `--top` | integer (>= 0) | `5` | Most frequent values to show (0: none). |
| `--no-formats` | flag |  | Skip value format detection (one query per table). |
| `--profile`, `-p` | text |  | Environment profile of the configuration. |
| `--format` | one of `text`, `json` | `text` | text or json. |
| `--output`, `-o` | path |  | Write the JSON profile to this file. |

## `dvf report` {#dvf-report}

Re-render reports from a results document, without touching any data.

```text
dvf report [OPTIONS]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `--from` | path | **required** | A results.json written by dvf run. |
| `--format`, `-f` | text, repeatable | **required** | Format to render. Repeatable. |
| `--output-dir`, `-o` | path | **required** | Where to write the reports. |
| `--title` | text |  | Report title. |

## `dvf rerun` {#dvf-rerun}

Run again only the checks that failed or errored in an earlier run.

```text
dvf rerun [OPTIONS] RUN_ID
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `RUN_ID` | text | **required** | The run whose failed and errored checks to rerun. |
| `--config`, `-c` | path |  | Configuration file (default: the one the run used). |
| `--output-dir`, `-o` | path |  | Write reports here instead of reports.output_dir. |
| `--no-reports` | flag |  | Do not write report files. |
| `--fail-on` | one of `info`, `warning`, `error`, `critical` | `error` | Exit 1 when a check at or above this severity fails. |
| `--verbose`, `-v` | flag |  | List every problem. |

## `dvf run` {#dvf-run}

Run the validations in a configuration and write reports.

Exit codes: 0 passed, 1 validation failures, 2 execution errors (or an interrupted run),
3 configuration errors.

```text
dvf run [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--env-file` | path |  | A .env file (default: .env next to the config). |
| `--only` | text, repeatable |  | Run a subset: suite:NAME, table:GLOB, check:TYPE, tag:TAG, severity:LEVEL. Repeatable. |
| `--format`, `-f` | text, repeatable |  | Report format (json is always written). Repeatable. |
| `--output-dir`, `-o` | path |  | Write reports here instead of reports.output_dir. |
| `--run-id` | text |  | Use this run ID. |
| `--no-reports` | flag |  | Do not write report files. |
| `--fail-on` | one of `info`, `warning`, `error`, `critical` | `error` | Exit 1 when a check at or above this severity fails. |
| `--min-score` | number |  | Exit 1 when the quality score is below this. |
| `--strict` | flag |  | Warnings also fail the run (exit 1). |
| `--verbose`, `-v` | flag |  | List every problem. |
| `--resume` | text |  | Continue an interrupted or killed run from its checkpoints. |
| `--force-resume` | flag |  | Resume even if the configuration changed. |
| `--failed-from` | text |  | Run only the checks that failed or errored in this run. |
| `--parallel`, `-j` | integer (>= 1) |  | Tables to run at the same time. |
| `--keep-temp` | flag |  | Keep the local engine's temporary files. |
| `--no-history` | flag |  | Do not read or record run history. |
| `--baseline` | text |  | Compare with: last_run, last_passed, none, or a run ID. |

## `dvf sap` {#dvf-sap}

SAP pack: business object templates and RFC backends.

Subcommands: [`dvf sap backends`](#dvf-sap-backends), [`dvf sap copy-template`](#dvf-sap-copy-template), [`dvf sap templates`](#dvf-sap-templates).

```text
dvf sap [OPTIONS] COMMAND [ARGS]...
```

### `dvf sap backends` {#dvf-sap-backends}

List the sap_rfc backends: ready, or declared without an implementation.

```text
dvf sap backends [OPTIONS]
```

### `dvf sap copy-template` {#dvf-sap-copy-template}

Copy a template next to your configuration, to use with 'extends:'.

```text
dvf sap copy-template [OPTIONS] NAME
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `NAME` | text | **required** | Template name (see 'dvf sap templates'). |
| `--to` | directory | `sap` | Folder to copy the template into. |
| `--force` | flag |  | Replace an existing file. |

### `dvf sap templates` {#dvf-sap-templates}

List the SAP business object templates and the variables each one needs.

```text
dvf sap templates [OPTIONS]
```

## `dvf schema` {#dvf-schema}

Export JSON Schemas.

Subcommands: [`dvf schema config`](#dvf-schema-config), [`dvf schema results`](#dvf-schema-results).

```text
dvf schema [OPTIONS] COMMAND [ARGS]...
```

### `dvf schema config` {#dvf-schema-config}

Export the configuration JSON Schema (including types from loaded plugins).

```text
dvf schema config [OPTIONS]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `--output`, `-o` | path |  | Write to this file instead of stdout. |

### `dvf schema results` {#dvf-schema-results}

Export the results JSON Schema.

```text
dvf schema results [OPTIONS]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `--output`, `-o` | path |  | Write to this file instead of stdout. |

## `dvf schema-diff` {#dvf-schema-diff}

Compare the column schemas of configured table pairs, or of two datasets, without
running checks: missing and extra columns, type compatibility, and nullability.

```text
dvf schema-diff [OPTIONS] CONFIG_OR_SOURCE [TARGET]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `CONFIG_OR_SOURCE` | text | **required** | A configuration file, or the source dataset when TARGET is given. |
| `TARGET` | text |  | The target dataset for an ad hoc diff, in the forms 'dvf compare' accepts (a file path, duckdb:///db::table, sqlite:///db::table, postgresql://...::table). |
| `--table`, `-t` | text |  | Only this table. |
| `--suite` | text |  | Only tables of this suite. |
| `--only` | text, repeatable |  | Selectors, as for dvf run. Repeatable. |
| `--key`, `-k` | text, repeatable |  | Key column of an ad hoc diff. Repeatable. |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--fail-on` | one of `never`, `incompatible`, `lossy`, `any` | `incompatible` | Exit 1 on: incompatible (missing columns, incompatible types), lossy (also narrowing and unclassified types), any difference, or never. |
| `--all` | flag |  | Also list columns that match exactly. |
| `--format` | one of `text`, `json` | `text` | text or json. |

## `dvf suggest` {#dvf-suggest}

Suggest candidate checks from column profiles, as a YAML fragment with evidence.

```text
dvf suggest [OPTIONS] [CONFIG]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `CONFIG` | path |  | Configuration to profile (or use --from-profile). |
| `--from-profile` | path |  | A JSON profile written by 'dvf profile --format json'. |
| `--table`, `-t` | text, repeatable |  | Only tables matching this glob. Repeatable. |
| `--suite` | text |  | Only tables of this suite. |
| `--side` | one of `auto`, `source`, `target`, `both` | `auto` | auto (target of comparison suites, source otherwise), source, target, or both. |
| `--margin` | number (>= 0.0) | `0.0` | Widen ranges and lengths by this share of the observed span. |
| `--max-values` | integer (>= 1) | `10` | Most distinct values for an allowed_values check. |
| `--no-freshness` | flag |  | Do not suggest freshness checks. |
| `--profile`, `-p` | text |  | Environment profile of the configuration. |
| `--output`, `-o` | path |  | Write the generated file here (default: standard output). |
| `--force` | flag |  | Replace an existing output file. |
| `--format` | one of `text`, `json` | `text` | Summary format: text, or json (on standard output). |

## `dvf trace` {#dvf-trace}

Follow one record through both sides: raw value, each normalizer, canonical value, hash.

```text
dvf trace [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--table`, `-t` | text | **required** | Table name. |
| `--key`, `-k` | text, repeatable | **required** | Key value (repeat for composite keys). |
| `--suite` | text |  | Suite of the table. |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--format` | one of `text`, `json` | `text` | text or json. |

## `dvf validate-config` {#dvf-validate-config}

Validate a configuration file and report every problem with its file, line, and column.

```text
dvf validate-config [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--env-file` | path |  | A .env file (default: .env next to the config). |
| `--resolve-secrets` / `--no-resolve-secrets` | flag | `--no-resolve-secrets` | Fetch secret values to prove they resolve. |
| `--strict` | flag |  | Treat warnings (such as missing environment variables) as errors. |
| `--format` | one of `text`, `json` | `text` | Output format. |

## `dvf version` {#dvf-version}

Show versions of dvf, its schemas, and its environment.

```text
dvf version [OPTIONS]
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `--format` | one of `text`, `json` | `text` | Output format. |

## `dvf waivers` {#dvf-waivers}

Review accepted differences (waivers).

Subcommands: [`dvf waivers list`](#dvf-waivers-list).

```text
dvf waivers [OPTIONS] COMMAND [ARGS]...
```

### `dvf waivers list` {#dvf-waivers-list}

List waivers with their ticket, approver, expiry date, and status: active, expiring
soon, or expired. Secrets are not fetched.

```text
dvf waivers list [OPTIONS] PATH
```

| Argument or option | Type | Default | Description |
|---|---|---|---|
| `PATH` | path | **required** | Configuration file (YAML, JSON, TOML, .properties, .ini). |
| `--soon-days` | integer (>= 0) | `14` | Waivers expiring within this many days are 'expiring soon'. |
| `--as-of` | text |  | Evaluate expiry on this date instead of today (for example the cutover date). |
| `--fail-on` | one of `never`, `expired`, `expiring` | `never` | Exit 1 when a waiver is expired, also when one is expiring soon, or never. |
| `--profile`, `-p` | text |  | Environment profile to apply. |
| `--set` | text, repeatable |  | Override a value, for example defaults.sample_size=10. Repeatable. |
| `--var` | text, repeatable |  | Set a ${var:NAME} value. Repeatable. |
| `--env-file` | path |  | A .env file (default: .env next to the config). |
| `--format` | one of `text`, `json` | `text` | text or json. |
