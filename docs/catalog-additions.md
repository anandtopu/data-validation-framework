# Sampling, schema design checks, sinks, and generic connectors

This page covers the catalog entries completed for 1.0: the `sample` comparison (REC-07), the schema checks SCH-07 to SCH-11 and SCH-14, the `delta` and `snowflake` result sinks, and the generic `sqlalchemy` and `odbc` connectors.

## Sample comparison (REC-07)

`sample` compares a statistically sized sample of rows by key, for tables too large to compare in full. It reports how many keys it compared, the estimated share of rows that differ with a confidence interval, and how many rows that is in the whole table.

```yaml
comparison:
  strategies: [row_count, sample]
  on_mismatch: null                 # or full_diff, to compare everything when the sample fails
  sample:
    margin_of_error: 0.01           # plus or minus one percentage point...
    confidence: 0.95                # ...at 95% confidence
    seed: 42                        # the same seed always selects the same keys
    stratify_by: [company_code]     # optional
    min_per_stratum: 200            # optional: oversample small strata
    max_mismatch_rate: 0.0          # pass only when no sampled row differs
```

Choose the sample size in exactly one way:

| Setting | Sample size |
|---|---|
| `margin_of_error` (with `confidence`, default 0.95, and `expected_rate`, default 0.5) | Cochran's formula with the finite population correction: `n0 = z² p (1 - p) / e²`, `n = n0 / (1 + (n0 - 1) / N)` |
| `fraction` | That share of the keys |
| `rows` | That many keys (expected) |

Without `comparison.sample`, `sample` uses `margin_of_error: 0.01` at 95% confidence (9,604 keys for a large table).

How it works:

1. Each side counts its rows with non-NULL keys, per stratum when `stratify_by` is set. A stratum's population is the larger of the two counts.
2. A key is sampled when the first 32 bits of `MD5('dvf-sample:<seed>' | canonical key)` are below `fraction × 2³²`. The hash is dvf's row hash, which is the same in every database, so both sides select the same keys without exchanging them.
3. Strata get the same fraction (proportional allocation). `min_per_stratum` raises small strata, rounded up to a few fractions, so a few keyed diffs cover all strata.
4. The sampled rows are compared by key through the table's engine, with column mismatches and their classification.
5. The mismatch rate is estimated per sampling class and weighted by each class's estimated number of keys. The confidence interval is a Wilson score interval with the effective sample size, so it stays useful when the sample finds no difference. Extrapolated counts divide each class's sampled differences by its sampling fraction. A fraction of 1 compares every key and gives exact counts.

The result's `details` hold `sample` (method, sizing, population, target and realized size, seed, confidence, margin, and the margin the realized size achieves), `estimate` (rate, interval, effective size), `extrapolated` (rows with differences and their interval, and estimated mismatched, missing, and extra rows), `sampled` (counts in the sample), and `classes`.

Things to know:

- Selection is Bernoulli sampling, so the realized size varies around the target by about its square root. Estimates use the realized size.
- Rows with a NULL key are never sampled. `comparison.partitions` are ignored (a note says so).
- Keys only in the target are sampled at the same rate, so extra rows are estimated too.
- `on_mismatch` escalates as usual. Set `on_mismatch: null` when a failed sample should not start a full comparison.

## Schema checks SCH-07 to SCH-11 and SCH-14

List them under `schema_checks`; none is on by default. Like the other schema checks, they run in comparison suites.

| Check | ID | Result |
|---|---|---|
| `physical_design` | SCH-07 | Informational (at most a warning): source indexes with no target index on the same (paired) columns, unique indexes that are not unique in the target, and partitioning that exists on one side only. Clustering, distribution, and sort keys are shown side by side |
| `column_order` | SCH-08 | Fails when paired columns are in a different order (extra target columns and derived columns are ignored) |
| `collation` | SCH-09 | Fails when a paired text column compares differently (case- or accent-sensitivity) or the target cannot store every Unicode character the source can; collation names that differ with the same behavior are listed but pass |
| `object_inventory` | SCH-10 | Informational (at most a warning): views, procedures, functions, triggers, sequences, synonyms (and Oracle packages) of the source schema with no target object of the same type and name |
| `schema_drift` | SCH-11 | Fails when a dataset's columns, types, nullability, or primary key changed since a baseline snapshot |
| `naming_standards` | SCH-14 | Fails when table or column names break the configured rules |

`contract_conformance` (SCH-12) and `nested_schema` (SCH-13) are check types (`contract`, `document_schema`); listing them under `schema_checks` reports `skipped` with a pointer.

`physical_design`, `collation`, and `object_inventory` read the database catalog. They work for DuckDB, SQLite, PostgreSQL, MySQL, SQL Server, and Oracle connections, and for `sqlalchemy` and `odbc` connections to those databases. Elsewhere, and for query and file datasets, they report `skipped` with "not available". The object inventory reports the schema of the table's qualified name, or the session's default schema; enable it on one table per schema pair, because it lists the whole schema.

What each database reports:

| Database | Physical design | Collations | Object inventory |
|---|---|---|---|
| PostgreSQL | indexes (method, partial predicates), `PARTITION BY` key and partition count, `CLUSTER` index | database collation and encoding; per-column collations, with nondeterministic ICU collations read as case- or accent-insensitive | views, materialized views, functions, procedures, triggers, sequences (not those owned by identity or serial columns, nor extension members) |
| MySQL | indexes, partitioning, InnoDB clustering by primary key | table and column collations and character sets (`_ci`, `_cs`, `_ai`, `_as`, `_bin`) | views, procedures, functions, triggers |
| SQL Server | indexes (clustered, columnstore, filtered), partition scheme and column | database and column collations; `nvarchar` is UTF-16, `varchar` uses the collation's code page unless `_UTF8` | views, procedures, functions, triggers, sequences, synonyms |
| Oracle | indexes, partitioning and key columns, index-organized tables | `NLS_CHARACTERSET` and `NLS_NCHAR_CHARACTERSET`, column collations (12.2+), `NLS_COMP`/`NLS_SORT` | views, materialized views, procedures, functions, packages, triggers, sequences, synonyms |
| DuckDB | primary key, unique constraints, and indexes | default collation and per-column `COLLATE` | views, macros, sequences |
| SQLite | indexes (partial predicates), rowid or `WITHOUT ROWID` clustering | per-column `COLLATE` (`BINARY`, `NOCASE`, `RTRIM`) | views, triggers |

### Options

```yaml
defaults:
  schema_checks: [object_exists, columns_present, type_compatibility, physical_design,
                  collation, object_inventory, schema_drift, naming_standards]
  schema_options:
    naming:
      side: target                      # source, target, or both
      rules:
        - { preset: snake_case, applies_to: all, exclude: [MANDT] }
        - { name: oracle-length, max_length: 30 }
        - { pattern: "^(dim|fct|stg)_[a-z0-9_]+$", applies_to: tables }
    drift:
      baseline: previous_run            # or a committed file: schemas/orders.json
      side: both                        # source, target, or both
      ignore_columns: ["_etl_*"]
      compare: [columns, types, nullability, primary_key]   # add 'order' to track column order
```

Naming rules take a regular expression that must match the whole name (`pattern`), a `preset` (`snake_case`, `upper_snake_case`, `camel_case`, `pascal_case`), a `max_length`, or a pattern with a maximum length. `applies_to` is `tables`, `columns`, or `all`; `exclude` lists exempt names (glob patterns, case-insensitive).

`schema_options` is inherited like `schema_checks`: from `defaults` to suites and tables.

### Drift baselines

With `baseline: previous_run`, each run compares with the schema the previous run saved, then saves its own under `<state_dir>/schemas/<suite>/<table>.json` (by default `.dvf/schemas/`). The first run reports `skipped` ("no baseline yet"). A run that does not select the check (`--only`, `dvf rerun`) leaves the snapshot alone.

To compare with a reviewed schema instead, copy a snapshot into the repository and point `baseline` at it (relative to the configuration). A committed baseline is never overwritten. It may hold one table, or many under `tables` keyed by `<suite>.<table>` or `<table>`; JSON and YAML both work:

```yaml
tables:
  orders:
    target:
      columns:
        - { name: id, native_type: INTEGER, nullable: false }
        - { name: amount, native_type: "NUMERIC(12,2)" }
      primary_key: [id]
```

Types are compared by `native_type` when both snapshots have one (ignoring case and spaces), otherwise by dvf's canonical `type`.

## Result sinks `delta` and `snowflake`

Both write the same three tables as the other sinks (`<prefix>runs`, `<prefix>checks`, `<prefix>column_mismatches`), and writing a run again replaces its rows.

```yaml
sinks:
  - type: delta
    path: ./results/delta               # or s3://bucket/dvf, abfss://..., gs://...
    options:
      storage_options: { AWS_REGION: eu-west-1 }   # credentials as ${secret:...} references
  - type: snowflake                     # beta
    url: ${secret:env:DVF_RESULTS_SNOWFLAKE}       # snowflake://user:password@account/DB/SCHEMA?warehouse=WH&role=ROLE
```

| Sink | Install | Status | Notes |
|---|---|---|---|
| `delta` | `dvf-framework[delta]` | ready | One Delta table per sink table under `path`. Each table is replaced for the run with one atomic commit (an overwrite restricted to its `run_id`); columns added by newer dvf versions are merged into existing tables. `schema` is not supported |
| `snowflake` | `dvf-framework[snowflake]` | beta | Verified with a fake driver only. Connect with `url`, or with `options` holding `account`, `user`, `password`, `authenticator`, `private_key_path`, `private_key_passphrase`, `warehouse`, `database`, and `role` (authenticated like the `snowflake` connector); other options go to `snowflake.connector.connect`. Tables and columns are upper case (`DVF_RUNS.RUN_ID`). The tables are created first (DDL commits in Snowflake), then the run's rows are deleted and inserted in one transaction |

## Generic connectors `sqlalchemy` and `odbc`

Use these for databases that dvf has a SQL dialect for but no native connector configuration you want to use, or to reuse an existing SQLAlchemy URL or ODBC DSN.

```yaml
connections:
  warehouse:
    type: sqlalchemy
    url: ${secret:env:WAREHOUSE_URL}     # postgresql+psycopg://user:password@host/db
    schema: sales                        # optional: schema of unqualified names
    options: { connect_args: { sslmode: require } }   # passed to create_engine
  legacy:
    type: odbc
    connection_string: ${secret:env:LEGACY_ODBC}       # Driver={PostgreSQL Unicode};Server=...;UseDeclareFetch=1
    # dsn: LEGACY                                      # or a DSN
    dialect: postgres                                  # optional: override the detected dialect
```

| Type | Install | Status | Dialect | Verified with |
|---|---|---|---|---|
| `sqlalchemy` | `dvf-framework[sqlalchemy]` and the database's driver | ready | from the URL's SQLAlchemy dialect: `postgresql`, `sqlite`, `mysql`, `mssql`, `oracle`, `duckdb`, `snowflake`, `bigquery`, `redshift`, `databricks`, `hana`; or `dialect:` | SQLite and PostgreSQL (psycopg 3): parity cases and seeded defects; MySQL 8.4 (PyMySQL) and Oracle 23ai Free (python-oracledb): seeded defects |
| `odbc` | `dvf-framework[odbc]` and the database's ODBC driver | ready | from the driver's DBMS name (`SQL_DBMS_NAME`); or `dialect:` | PostgreSQL with psqlODBC |

Both connectors:

- prepare every session like dvf's native connector for the dialect: UTC everywhere; read-only sessions on PostgreSQL and MySQL; SQL Server date and arithmetic settings; Oracle NLS settings; and, on SQLite through SQLAlchemy, dvf's SQL functions and read-only file access;
- read table schemas from the catalog (SQLAlchemy's inspector; ODBC `SQLColumns` and `SQLPrimaryKeys`) and query schemas from the result description;
- stream rows in batches (SQLAlchemy uses server-side cursors where the driver has them);
- support the schema checks above for PostgreSQL, MySQL, SQL Server, Oracle, DuckDB, and SQLite (SQLAlchemy only).

They pass dvf's canonical parity cases and the seeded-defect suite on the verified databases. Other databases (SQL Server through SQLAlchemy, anything but PostgreSQL through ODBC) work when dvf has their dialect, but their behavior is verified only through dvf's native connectors.

Limits:

- MariaDB is not supported (set `dialect: mysql` at your own risk). SQLite through ODBC is rejected, because dvf's SQL functions cannot be registered there.
- `as_of` is not supported; write the time-travel clause in a `query`.
- psqlODBC reads whole results into memory unless the connection string sets `UseDeclareFetch=1` (and `Fetch=10000`); set it for large tables. psqlODBC also returns booleans as `1`/`0` text by default (`BoolsAsChar`), which matters only to the `polars` engine.
- ODBC catalog lookups need the schema of unqualified names: dvf asks the database (PostgreSQL, SQL Server, Oracle, DuckDB, Snowflake, HANA) or uses `schema:`.
