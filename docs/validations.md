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

Other catalog entries (SCH-07 to SCH-14) report `skipped` until their phase.

## Comparisons

Configured under `comparison` on a table (or in `defaults`). Strategies run in order; `on_mismatch` (default `full_diff`) runs only when an earlier strategy did not pass.

```yaml
comparison:
  strategies: [row_count, aggregate, row_hash]
  on_mismatch: full_diff
  group_by: [company_code]          # grouped counts and control totals
  aggregates: [count, sum, min, max, null_count, count_distinct]
  tolerance: { numeric_abs: 0.005, row_count_pct: 0.1, timestamp: 1s }
  max_mismatch_rows: 100000         # mismatched values fetched for classification
```

| Strategy | Catalog | What it compares |
|---|---|---|
| `row_count` | REC-01 | total rows, and rows per group with `group_by` |
| `aggregate` | REC-02 | per column: `count`, `sum`, `min`, `max`, `avg`, `count_distinct`, `null_count`, `sum_length`, overall or per group |
| `profile` | REC-12 | per column profiles side by side (nulls, distinct values, min, max, mean, lengths); differences are warnings |
| `row_hash` | REC-03, REC-04 | keys (missing, extra, duplicate, NULL) and a hash of each row's canonical columns |
| `full_diff` | REC-03, REC-05 | keys, per-column mismatch counts and percentages, side-by-side samples, and classification |

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

`trim`, `rtrim`, `ltrim`, `lower`, `upper`, `collapse_whitespace`, `unicode_nfc`, `null_if_empty`, `empty_if_null`, `strip_leading_zeros`, `round` (`{round: 2}`), `truncate_timestamp` (`{truncate_timestamp: minute}`), and `to_utc` (`{to_utc: Europe/Berlin}`). SAP normalizers arrive with the SAP pack.

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
| `auto` (default) | `pushdown` when source and target share a connection, otherwise `duckdb` |
| `pushdown` | Inside the database; requires both datasets on the same connection |
| `duckdb` | Each side is canonicalized in its own database and streamed into a local DuckDB that spills to disk |
| `polars` | Raw values are fetched and canonicalized and diffed with Polars (`pip install 'dvf[polars]'`) |
