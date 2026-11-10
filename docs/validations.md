# Checks and comparisons

Every validation produces a `CheckResult` with a stable ID (`suite.table.type[.columns][.name]`), a status, the evidence, and the exact SQL that ran on each side.

## Statuses and severities

A check that does not pass gets status `failed` when its severity is `error` or `critical`, and `warning` when its severity is `warning` or `info`. Severity comes from the check, otherwise the table, otherwise `defaults.severity`. Queries that cannot run produce `error` with a hint; checks that cannot apply (for example, a missing dataset) produce `skipped`.

## Data quality checks

All row-level checks accept `where` (a SQL predicate), `mostly` (the share of rows that must pass, default 1.0), `severity`, `tags`, `owner`, `details` (copied into results), and `side` (`source`, `target`, or `both`; comparison suites default to `target`). Failing rows are sampled (`sample_size`) and masked according to `defaults.privacy`.

| Type | Dimension | Passes when | Example |
|---|---|---|---|
| `not_null` | completeness | no NULLs in any listed column | `{type: not_null, columns: [id, email]}` |
| `not_blank` | completeness | no NULL, empty, or space-only values | `{type: not_blank, columns: [name]}` |
| `null_ratio` | completeness | NULL share at most `max_ratio` | `{type: null_ratio, column: phone, max_ratio: 0.2}` |
| `unique` | uniqueness | no duplicated values or combinations | `{type: unique, columns: [order_id, line]}` |
| `regex` | validity | values match (`full_match: false` for a substring) | `{type: regex, column: email, pattern: "^[^@]+@[^@]+$"}` |
| `allowed_values` | validity | values in a fixed set (`case_sensitive: false` optional) | `{type: allowed_values, column: status, values: [A, B]}` |
| `range` | validity | values between `min` and `max` (`inclusive: false` optional) | `{type: range, column: age, min: 0, max: 120}` |
| `length` | validity | text length between `min` and `max` | `{type: length, column: code, min: 2, max: 3}` |
| `referential_integrity` | referential integrity | every value exists in the parent (same or another connection) | `{type: referential_integrity, column: customer_id, parent: {table: customers, column: id}}` |
| `custom_sql` | custom | the query returns no rows, or a scalar meets `expect` | `{type: custom_sql, name: no_future, sql: "SELECT * FROM {table} WHERE d > CURRENT_DATE"}` |
| `expression` | consistency | the SQL condition is true for every row (NULL fails) | `{type: expression, name: dates, expression: "end_date >= start_date"}` |
| `row_count_range` | volume | the row count is within `min` and `max` | `{type: row_count_range, min: 1}` |
| `freshness` | timeliness | the newest value is younger than `max_age` | `{type: freshness, column: loaded_at, max_age: 1d}` |

NULL values pass value checks (`regex`, `allowed_values`, `range`, `length`, `referential_integrity`); use `not_null` to require values. In `custom_sql`, `{table}` stands for the checked dataset (with its filter applied) and already carries an alias, so do not alias it again. Custom SQL is trusted configuration.

In in-pipeline mode (`dvf.validate_dataframe`), each check's `action` (`quarantine`, `drop`, `warn`, or `fail`) decides what happens to the rows that fail it; see [In-pipeline validation](pipelines.md).

### File-level checks

These run on datasets of a `files` connection; see [Files and lakehouse tables](files.md) for their options.

| Type | Dimension | Passes when |
|---|---|---|
| `file_exists` | completeness | at least one file matches, or exactly `count` files |
| `file_count` | volume | the number of files is within `min` and `max` |
| `file_name_pattern` | validity | every file name matches `pattern` (or a share given by `mostly`) |
| `file_size` | volume | every file is within `min_bytes` and `max_bytes`, and none is empty unless `allow_empty` |
| `file_checksum` | reconciliation | every file's MD5 or SHA-256 matches `expected` values, sidecars, a manifest, or the files of another location (REC-09) |
| `file_freshness` | timeliness | the newest file (or, with `which: oldest`, every file) was modified within `max_age` |
| `control_totals` | reconciliation | header or trailer control records match the data's record count and column sums (REC-09) |
| `schema_on_read` | schema | the columns and types read match an expected list or schema file |
| `bad_records` | validity | unreadable or malformed records do not exceed `max_count` (none by default) |

### Migration checks

The migration catalog MIG-01 to MIG-14 is covered by these checks, table options, and commands; [Migration validations](migration.md) has the details and examples.

| Catalog | Validation | dvf feature |
|---|---|---|
| MIG-01 | Scope and exclusion reconciliation | check `scope_reconciliation` |
| MIG-02 | Delta and incremental loads | table option `window`; checks `cdc_applied` and `late_arriving` |
| MIG-03 | Idempotency | check `idempotency` (against the previous run) |
| MIG-04 | SCD Type 2 | check `scd2` |
| MIG-05 | Soft and hard deletes | check `delete_propagation` |
| MIG-06 | Surrogate and key maps | check `key_map` |
| MIG-07 | Audit and lineage columns | check `audit_columns` |
| MIG-08 | Mock cutover comparison | `dvf history compare RUN_A RUN_B`; report format `signoff` |
| MIG-09 | Consistent snapshot | dataset option `as_of`; table option `in_flight` |
| MIG-10 | Character set conversion | check `charset_conversion` |
| MIG-11 | Precision and time zone conversion | check `dst_edge_cases`; the `to_utc` normalizer in comparisons |
| MIG-12 | Default and sentinel substitution | check `substitution_rules` |
| MIG-13 | Business parity | query datasets on each side (REC-08) with tolerances |
| MIG-14 | Pre-migration baseline capture | `dvf baseline capture`; check `baseline_metrics` |

`scope_reconciliation`, `delete_propagation`, `key_map`, and `substitution_rules` are *pair checks*: they read the source and the target of a table together, once per table, paired by the table's keys.

## Schema checks

Listed under `schema_checks` (default: the first five). They run for comparison suites; single-dataset suites report `object_exists` only.

| Check | ID | Fails when |
|---|---|---|
| `object_exists` | SCH-01 | the source or target dataset does not exist |
| `columns_present` | SCH-02 | in-scope source columns have no target column, or a mapping names a missing column (extra target columns are listed but do not fail) |
| `type_compatibility` | SCH-03 | a type change is incompatible, or lossy across type families; other type-family changes (such as integers stored as text) are warnings |
| `length_precision` | SCH-04 | a length, precision, or scale shrinks within the same type family |
| `nullability` | SCH-05 | warns when a column is NOT NULL in the target but nullable in the source (skipped for files and queries) |
| `constraints` | SCH-06 | the target primary key differs; when the target declares none, a data check proves the key unique and not NULL |

Two more schema validations are check types, listed under `checks:`:

| Check | ID | Fails when |
|---|---|---|
| `contract` | SCH-12 | the dataset's schema differs from an ODCS v3 contract or an expected-schema file ([Discovery](discovery.md)) |
| `document_schema` | SCH-13 | sampled documents lack expected fields, hold unexpected types, or fall below a presence threshold ([NoSQL](nosql.md)) |

These schema checks are off by default; list them under `schema_checks` and configure them with `schema_options` ([details](catalog-additions.md#schema-checks-sch-07-to-sch-11-and-sch-14)):

| Check | ID | Result |
|---|---|---|
| `physical_design` | SCH-07 | Informational: source indexes with no matching target index, unique indexes not unique in the target, partitioning on one side only; clustering, distribution, and sort keys side by side |
| `column_order` | SCH-08 | Fails when paired columns are in a different order |
| `collation` | SCH-09 | Fails when a paired text column compares differently (case or accent sensitivity) or the target cannot store every character the source can |
| `object_inventory` | SCH-10 | Informational: views, procedures, functions, triggers, sequences, and synonyms of the source schema with no target counterpart |
| `schema_drift` | SCH-11 | Fails when columns, types, nullability, or the primary key changed since the previous run or a committed baseline |
| `naming_standards` | SCH-14 | Fails when table or column names break the configured rules (patterns, presets such as `snake_case`, maximum length) |

`physical_design`, `collation`, and `object_inventory` read the database catalog (DuckDB, SQLite, PostgreSQL, MySQL, SQL Server, Oracle, and `sqlalchemy` or `odbc` connections to them); elsewhere they report `skipped`.

## Comparisons

Configured under `comparison` on a table (or in `defaults`). Strategies run in order; `on_mismatch` (default `full_diff`) runs only when an earlier strategy did not pass.

```yaml
comparison:
  strategies: [row_count, aggregate, hash_bucket]
  on_mismatch: full_diff
  group_by: [company_code]          # grouped counts and control totals
  aggregates: [count, sum, min, max, null_count, count_distinct]
  tolerance: { numeric_abs: 0.005, row_count_pct: 0.1, timestamp: 1s }
  max_mismatch_rows: 100000         # mismatched values fetched for classification
  buckets: 256                      # first-level buckets (hash_bucket, bisect)
  drilldown_fanout: 16              # sub-buckets per differing bucket
  drilldown_threshold: 10000        # rows per side at which drilling stops
  partitions: { column: posting_date, ranges: ["2024-01-01", "2025-01-01"] }
```

| Strategy | Catalog | What it compares |
|---|---|---|
| `row_count` | REC-01 | total rows, and rows per group with `group_by` |
| `aggregate` | REC-02 | per column: `count`, `sum`, `min`, `max`, `avg`, `count_distinct`, `null_count`, `sum_length`, overall or per group |
| `profile` | REC-12 | per column profiles side by side (nulls, distinct values, min, max, mean, lengths); differences are warnings |
| `row_hash` | REC-03, REC-04 | keys (missing, extra, duplicate, NULL) and a hash of each row's canonical columns |
| `hash_bucket` | REC-03, REC-04 | bucket checksums inside each database, drilling into differing buckets; only their rows are diffed |
| `bisect` | REC-03, REC-06 | the same over ranges of an integer key, so databases can use an index on the key |
| `full_diff` | REC-03, REC-05 | keys, per-column mismatch counts and percentages, side-by-side samples, and classification |
| `sample` | REC-07 | a seeded-hash sample by key (the same keys on both sides, in every dialect), sized by margin of error and confidence (Cochran), a fraction, or a number of rows, optionally stratified; reports the estimated mismatch rate with a confidence interval and extrapolated counts ([details](catalog-additions.md#sample-comparison-rec-07)) |
| `nested` | REC-10 | child rows per parent key (`group_by`): counts, totals, and an order-independent hash of the child rows, for example embedded arrays against a child table ([NoSQL](nosql.md)) |

`row_hash` (and `full_diff`) compare every row: the local engine moves each row's key and hash. Use `hash_bucket` or `bisect` for large tables in different databases that are mostly identical: rows moved grow with the number of differences, not with the table size. When `full_diff` follows any of them, only the differing rows' values are fetched.

### Checksum search

`hash_bucket` and `bisect` work in rounds:

1. Each database summarizes every bucket: row count, distinct keys, two sums of 32-bit row-hash prefixes, and, for floating-point columns, the non-NULL count, sum, and sum of absolute values.
2. dvf compares the summaries and splits each differing bucket into `drilldown_fanout` sub-buckets.
3. It stops when the differing buckets hold at most `drilldown_threshold` rows per side, or when splitting no longer narrows them down.
4. The configured engine then diffs just those rows, exactly.

`hash_bucket` buckets by a hash of the canonical key, so it works with any key types, including composite keys. `bisect` needs one integer key; each round reads only the key ranges still in question, which an index makes cheap.

The comparison result reports the buckets compared at each level, the rows transferred, and the share of all rows they represent. Floating-point columns are compared per bucket by sums within tolerance (a near-certain test, not a proof), and exactly per row in differing buckets.

### Partitions

`comparison.partitions` splits row-level strategies into slices that are compared, and checkpointed, one at a time. The results add up to the table's result.

| Setting | Partitions |
|---|---|
| `{column: country, values: auto}` | one per value found in the source, one for other values, one for NULL |
| `{column: country, values: [DE, FR]}` | one per listed value, other values, and NULL |
| `{column: posting_date, ranges: ["2024-01-01", "2025-01-01"]}` | below the first boundary, between boundaries, from the last, and NULL |
| `{hash_buckets: 8}` | eight slices by a hash of the key, plus NULL keys |
| `{predicates: [{name: emea, source: "region = 'EMEA'", target: "region_code = 'E'"}]}` | your predicates (make them cover every row) |

Partition by a column whose value is the same on both sides, such as a key or a load date. A row whose partition value changed is counted as missing in one partition and extra in another, and the result warns about it.

Row-level strategies need keys: `keys: [id]`, composite keys, renamed keys (`{source: KUNNR, target: customer_id, normalize: [strip_leading_zeros]}`), or `keys: auto` for the source primary key. Duplicate keys are counted per side and compared once.

### Column mapping

Columns pair by name (`columns.match: case_insensitive` by default). Use `columns.include` and `columns.exclude` (glob patterns) to scope the comparison, and `columns.mappings` for renames, derived columns, per-column normalizers and tolerances, and PII tagging:

```yaml
columns:
  exclude: [MANDT, "*_AUDIT"]
  mappings:
    - { source: NAME1, target: customer_name, normalize: [rtrim] }
    - { source_expr: "first_name || ' ' || last_name", target: full_name }
    - { source: AMOUNT, target: amount, tolerance: { numeric_abs: 0.01 } }
    - { source: SSN, target: ssn, pii: true }
    - { source: NOTES, target: notes, compare: false }
```

### Canonicalization

Before comparing, both sides are converted to one canonical form, identically in every engine:

| Values | Canonical form |
|---|---|
| Text | Unicode NFC, then `trim` (spaces), `collapse_whitespace`, lower case (`case_sensitive: false`), and empty-as-NULL (`null_equals_empty`), as configured |
| Integers and decimals | Fixed scale (the larger of the two sides, or `decimal_scale`), rounded half away from zero; unknown scales use 10 places |
| Floating point | Compared numerically within `float_compare` (`rel_tol` 1e-9 by default) and `tolerance`; never hashed |
| Dates and timestamps | ISO 8601 in UTC, truncated to `timestamp_precision`; timestamps without an offset are read in `normalization.timezone` |
| Booleans, binary | `1`/`0`; lower-case hexadecimal |

### Normalizers

Applied per key or mapped column, on both sides, wherever the column type fits:

`trim`, `rtrim`, `ltrim`, `lower`, `upper`, `collapse_whitespace`, `unicode_nfc`, `null_if_empty`, `empty_if_null`, `strip_leading_zeros`, `round` (`{round: 2}`), `truncate_timestamp` (`{truncate_timestamp: minute}`), and `to_utc` (`{to_utc: Europe/Berlin}`).

SAP: `sap_alpha`, `sap_matnr_length`, `sap_numc`, `sap_dats`, `sap_tims`, `sap_bool`, `sap_spras`, `sap_curr`, and `sap_quan` (see [SAP](sap.md)). Schema checks compare a column's types after its normalizers, so an SAP `DATS` string normalized with `sap_dats` is compatible with a target `DATE`.

### Mismatch classification

Each mismatched value is labeled, and the dominant labels per column produce a hint:

| Label | Meaning |
|---|---|
| `whitespace_only`, `case_only`, `case_and_whitespace` | Same text apart from spaces or letter case |
| `leading_zeros` | Same number with different zero padding |
| `numeric_rounding`, `precision_truncation` | Same number rounded or cut to fewer digits (or fractional seconds) |
| `string_truncation` | The target is a prefix of the source |
| `timezone_shift` | Timestamps differ by a whole number of hours (or quarter hours) |
| `date_format`, `type_coercion` | Same value in a different representation |
| `null_vs_empty`, `null_vs_value` | NULL on one side, empty text or a value on the other |
| `sentinel_substitution` | A default such as `N/A`, `00000000`, or `9999-12-31` replaced the value |
| `encoding` | Characters damaged by a code page conversion (mojibake or replacement characters) |
| `value_changed` | Anything else |

## Engines

`defaults.engine` or a table's `engine` chooses where row-level comparisons run. Counts, control totals, profiles, and data quality checks always run inside each database.

| Engine | Where the diff runs |
|---|---|
| `auto` (default) | `pushdown` when source and target share a connection, `spark` when both are Spark datasets in one Spark application, otherwise `duckdb` |
| `pushdown` | Inside the database; requires both datasets on the same connection |
| `duckdb` | Each side is canonicalized in its own database and streamed into a local DuckDB that spills to disk |
| `polars` | Raw values are fetched and canonicalized and diffed with Polars (`pip install 'dvf-framework[polars]'`) |
| `spark` | Spark datasets are projected in Spark SQL; other sides are staged as Parquet and read by Spark, which runs the diff ([Spark](spark.md)) |

Full diffs run in two phases: keys and row hashes of every row first, then the full values of only the rows that differ. Duplicate keys are probed with a cheap query, and de-duplication runs only when a key repeats. `defaults.execution.memory_limit` (for example `4GB`), `threads`, and `temp_dir` bound the local DuckDB engine, which spills to disk beyond its memory limit.
