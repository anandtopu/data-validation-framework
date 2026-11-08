# Configuration

A dvf configuration describes connections, suites of tables to validate, checks, comparisons, waivers, reports, and notifications. The decisions behind the format are in [ADR 0006](adr/0006-configuration-model.md).

## File formats

| Format | Extensions | Error positions |
|---|---|---|
| YAML (recommended) | `.yaml`, `.yml` | File, line, and column |
| JSON | `.json` | File (line and column for syntax errors) |
| TOML | `.toml` | File (line and column for syntax errors) |
| Java-style properties | `.properties` | File and line |
| INI | `.ini`, `.cfg` | File |

YAML is read with YAML 1.2 style scalars: only `true` and `false` are booleans (so `on:` and `yes` stay strings), numbers with leading zeros stay strings (SAP codes such as `0100` survive), and there is no base-60 arithmetic. Duplicate keys are errors.

In `.properties` and `.ini` files, keys are paths: `suites[0].tables[1].name`, or `suites.0.tables.1.name`. Values are strings, except `null`, booleans, numbers, and flow collections such as `[a, b]`. In INI files, the `[dvf]` section holds top-level keys and other section names are paths (`[defaults.normalization]`).

## Layering

Layers merge from lowest to highest precedence. Mappings merge key by key; lists and scalars replace.

1. Built-in defaults (the model defaults)
2. Files listed in `extends:` (relative to the file that lists them; nested and cycle-checked)
3. The main file
4. The profile chosen with `--profile NAME`: first `profiles.NAME` inside the file, then a sibling `<stem>.NAME.<ext>`
5. A git-ignored sibling `<stem>.local.<ext>`, if present
6. Environment variables `DVF__SECTION__KEY=value` (double underscore separates segments, for example `DVF__DEFAULTS__SAMPLE_SIZE=10`)
7. `--set path=value` on the command line

`dvf validate-config --format json` lists the files that were merged.

## Interpolation

| Placeholder | Meaning |
|---|---|
| `${env:NAME}` | Environment variable, falling back to the `.env` file next to the config (or `--env-file`) |
| `${env:NAME:-default}` | The same, with a default |
| `${var:name}` | A value from the `vars:` section or `--var name=value` |
| `${secret:provider:path#key}` | A secret from a registered provider: `env`, `dotenv` today; `azurekv`, `awssm`, `gcpsm`, `vault`, `keyring`, `databricks` are declared for later phases |
| `${run.id}`, `${run.date}`, `${run.timestamp}`, `${run.profile}` | Kept as written and resolved when a run starts |
| `$${` | A literal `${` |

A value that is exactly one `${var:...}` keeps the variable's type, so `port: ${var:port}` stays an integer. Variables may use environment variables and secrets, but not other variables.

Resolved secret values are registered for redaction: they never appear in logs, `dvf config show`, or reports.

## Inheritance

Settings flow from `defaults` to each suite (`suites[].defaults`) and from the suite to each table. Tables carry their effective `engine`, `severity`, `sample_size`, `normalization`, `comparison`, and `schema_checks`. Checks without `severity` take the table's; checks without `side` default to `target` in comparison suites and `source` in single-dataset suites.

## Sections

| Section | Purpose |
|---|---|
| `version` | Required. Use `1`. |
| `project` | Name, description, owner, and metadata copied into reports |
| `vars` | Variables for `${var:...}` |
| `defaults` | Engine, severity, sample size, normalization, privacy, execution limits, comparison, schema checks |
| `connections` | Named connections. `type` selects the connector and its fields (see `dvf plugins list --kind connector`) |
| `suites` | Groups of tables with a `source` connection and an optional `target` connection |
| `waivers` | Accepted differences with a reason, ticket, approver, and expiry date |
| `reports` | Formats, output folder, branding, extracts, template overrides |
| `history` | Run history store and baseline |
| `sinks` | Databases that receive every run's results as tables, for dashboards |
| `notifications` | Teams, Slack, webhook, or email notifications by run status |
| `plugins` | Local plugin `paths` (relative to the config) and `modules` |
| `extends`, `profiles` | Layering, applied by the loader |

!!! note "What applies at run time"
    Every section takes effect except `notifications`, which arrive in Phase 4 (they are validated). `as_of` raises a clear error on connectors without time travel. See [Checks and comparisons](validations.md) for every validation and [Debugging and operations](debugging.md) for checkpoints, history, extracts, and repro files.

### Execution

`defaults.execution` is read at project level:

```yaml
defaults:
  execution:
    max_parallel_tables: 4          # tables validated at the same time
    max_queries_per_connection: 2   # default cap per connection (a connection's max_parallel_queries wins)
    query_timeout_s: 1800           # applied to connections that set none
    retries: 2                      # transient errors (lost connection, deadlock) are retried
    retry_backoff_s: 2.0            # first delay; it doubles on each retry
    memory_limit: 4GB               # local DuckDB engine; it spills to disk beyond it
    threads: 8                      # local DuckDB engine threads (default: all cores)
    temp_dir: ./.dvf/spill          # local engine spill folder
    checkpoints: true               # save finished tables so --resume can continue a run
    state_dir: .dvf                 # checkpoints, saved results, and the default history store
```

Each table holds one slot of every connection it uses while it runs, so `max_parallel_queries: 1` on a production source makes tables take turns on it while other connections keep working.

### Waivers

```yaml
waivers:
  - check: finance.customers.compare.column_mismatch   # check ID; glob patterns allowed
    column: customer_name                               # optional: one column of a comparison
    reason: Names longer than 35 characters are truncated by design
    ticket: CR-1042
    approved_by: finance-ba
    expires: 2027-01-31
```

An active waiver turns a failed or warning result into `waived` (counted as passed in the quality score). A column waiver waives a column comparison only when every differing column is waived; otherwise the waived columns are marked and the result still fails. Expired waivers never apply: the failure stands and the reports flag the expired waiver.

### History

```yaml
history:
  enabled: true
  store: duckdb:///./.dvf/history.duckdb   # or sqlite:///path/history.sqlite
  baseline: last_passed                    # last_run, last_passed, none, or a run ID
  retention_runs: 50                       # keep the newest runs only (optional)
```

### Result sinks

```yaml
sinks:
  - { type: duckdb, path: ./results/dvf.duckdb }
  - { type: postgres, url: "${secret:env:RESULTS_DB_URL}", schema: dvf, table_prefix: dvf_ }
```

Sinks (`duckdb`, `sqlite`, `postgres`) append each run to `<prefix>runs`, `<prefix>checks`, and `<prefix>column_mismatches`, replacing the rows of a run written twice. A failing sink is logged and noted in the results' metadata; it never fails the run.

### Reports and extracts

```yaml
reports:
  formats: [json, html, junit, markdown, excel]   # excel needs pip install 'dvf[excel]'
  output_dir: ./reports/${run.date}/${run.id}
  extracts: { enabled: true, format: parquet, max_rows_per_check: 100000 }
```

### Tables

A table has either a `name` with `source` (and `target` in comparison suites), or a `pattern` that selects many tables by glob:

```yaml
tables:
  - name: customers
    source: { table: KNA1, filter: "MANDT = '100'" }
    target: { table: CORE.CUSTOMER, as_of: "2026-11-01T00:00:00Z" }
    keys:
      - { source: KUNNR, target: CUSTOMER_ID, normalize: [sap_alpha] }
    columns:
      exclude: [MANDT]
      mappings:
        - { source: ERDAT, target: CREATED_DATE, normalize: [sap_dats] }
        - { source_expr: "TRIM(STRAS) || ' ' || TRIM(ORT01)", target: FULL_ADDRESS }
  - pattern: { source: "SAPSR3.ZFI_*", target: "FINANCE.ZFI_*" }
    keys: auto
```

Each side is exactly one of `table`, `query`, or `path`, plus an optional `filter` and `as_of`. Normalizers accept `rtrim`, `{round: 2}`, `{round: {digits: 2}}`, or `{name: round, params: {digits: 2}}`.

### Checks

Every check has `type` plus common fields: `name`, `description`, `severity`, `side`, `mostly` (share of rows that must pass), `where`, `tags`, `owner`, `enabled`, and `details` (custom fields copied into results). Run `dvf plugins list --kind check` for the available types; each type's fields are in the JSON Schema.

Suite and table names may contain letters, digits, underscores, and hyphens, because they form check IDs: `suite.table.type[.columns][.name]`.

## Validating configuration

`dvf validate-config` is lenient by default so it can run in CI without credentials: missing environment variables are warnings, and secrets are not fetched. Use `--resolve-secrets` to prove secrets resolve, and `--strict` to fail on warnings.

## Diagnostic codes

| Code | Meaning |
|---|---|
| DVF-C001 | File not found or unreadable |
| DVF-C002 | Syntax error |
| DVF-C003 | Unsupported file format |
| DVF-C004 | Duplicate key |
| DVF-C005 | Problem with `extends` (missing file, cycle, wrong type) |
| DVF-C006 | Profile not found |
| DVF-C007 | Invalid `--set` or `DVF__` override |
| DVF-C010 | Invalid interpolation syntax |
| DVF-C011 | Environment variable not set |
| DVF-C012 | Variable not defined |
| DVF-C013 | Unknown secret provider |
| DVF-C014 | Secret could not be resolved |
| DVF-C015 | Unknown `${run.*}` placeholder |
| DVF-C100 | Invalid value |
| DVF-C101 | Missing required field |
| DVF-C102 | Unknown field (with a "did you mean" hint) |
| DVF-C103 | Unsupported configuration version |
| DVF-C110 | Unknown component (connector, check, comparator, normalizer, reporter, engine, notifier) |
| DVF-C120 | Unknown connection |
| DVF-C121 | Duplicate suite or table name |
| DVF-C124 | Source/target mismatch between suite and table or check |
| DVF-C125 | Two checks would have the same ID |
| DVF-C130 | Waiver expired (warning) |
| DVF-C131 | Waiver matches no suite and table (warning) |
| DVF-C140 | Plugin failed to load |
