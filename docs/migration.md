# Migration validations

Row counts and row diffs tell you whether two tables match. A migration needs more: rows left out on purpose must be accounted for, incremental loads must apply every change, history must stay well formed, keys must map one to one, and the business must sign off on the result. This page covers the migration checks MIG-01 to MIG-14 and the commands that go with them.

## Overview

| ID | Validation | In dvf |
|---|---|---|
| MIG-01 | Scope and exclusion reconciliation | Check `scope_reconciliation` |
| MIG-02 | Delta and incremental loads | Table option `window`; checks `cdc_applied` and `late_arriving` |
| MIG-03 | Idempotency | Check `idempotency` (compares with the previous run) |
| MIG-04 | SCD Type 2 | Check `scd2` |
| MIG-05 | Soft and hard deletes | Check `delete_propagation` |
| MIG-06 | Surrogate and key maps | Check `key_map` |
| MIG-07 | Audit and lineage columns | Check `audit_columns` |
| MIG-08 | Mock cutover comparison | `dvf history compare RUN_A RUN_B`; report format `signoff` |
| MIG-09 | Consistent snapshot | Dataset option `as_of`; table option `in_flight` |
| MIG-10 | Character set conversion | Check `charset_conversion` |
| MIG-11 | Precision and time zone conversion | Check `dst_edge_cases`; `to_utc` and canonical comparisons |
| MIG-12 | Default and sentinel substitution | Check `substitution_rules` |
| MIG-13 | Business parity | Query datasets with grouped comparisons (example in `examples/migration/dvf.yaml`) |
| MIG-14 | Pre-migration baseline | `dvf baseline capture`; check `baseline_metrics` |

Checks are configured under a table's `checks:` like any other check. `severity`, `mostly`, `where`, `tags`, `owner`, `enabled`, and `details` work as for the data quality checks.

## Checks that compare both datasets

`scope_reconciliation`, `delete_propagation`, `key_map`, and `substitution_rules` read the source and the target of a table together. They:

- run once per table (their `side` is ignored, and results show side `both`);
- are skipped, with a message, in suites without a target;
- see both datasets with the table's `filter`, `window`, and `in_flight` applied, even when `comparison.enabled` is false;
- pair rows by the table's `keys`, canonicalized exactly as comparisons do (normalizers included). `delete_propagation` and `substitution_rules` are skipped when the table has no keys.

Where the rule runs:

- **Both datasets on one connection:** the rule runs as SQL in that database. Only counts and samples leave it.
- **Different connections:** the needed keys and columns of each side are streamed, batch by batch, into a local DuckDB database. It spills to `defaults.execution.temp_dir` (the system temporary folder by default) and respects `defaults.execution.memory_limit`, so memory stays bounded.

Besides `where` (applied to both sides), these checks take:

| Option | Meaning |
|---|---|
| `source_where` | SQL predicate restricting the source rows |
| `target_where` | SQL predicate restricting the target rows |

Values from different systems are compared as text where noted below. Cast them in a query dataset when their text forms differ (for example `10.5` against `10.50`).

## MIG-01: scope and exclusion reconciliation

Records excluded by design (archived, closed, out of scope) are counted, and the source total must equal migrated plus excluded rows:

```yaml
tables:
  - name: accounts
    source: { table: legacy.accounts, filter: "status <> 'ARCHIVED'" }
    target: { table: crm.accounts }
    keys: [account_id]
    checks:
      - type: scope_reconciliation
        excluded_where: "status = 'ARCHIVED'"
        expected_excluded: { min: 1200, max: 1300 }
        excluded_absent: true
```

| Option | Default | Meaning |
|---|---|---|
| `excluded_where` | required | SQL predicate on the source selecting the excluded rows |
| `expected_excluded` | none | An exact count, or `{min, max}` |
| `population` | `unfiltered` | Count the source before its `source.filter` (`unfiltered`), which is usually the scope filter, or after it (`filtered`). `window` and `in_flight` always apply |
| `tolerance_rows` | 0 | Accepted difference between the source total and migrated plus excluded rows |
| `tolerance_pct` | 0 | The same as a percentage of the source total; the larger allowance wins |
| `excluded_absent` | false | Also fail when an excluded row's key exists in the target (needs keys) |

The result's metrics show the balance (`source rows = migrated + excluded`), the excluded count against the expectation, and the migrated count. `observed` is the number of unaccounted rows: positive when rows were lost, negative when the target holds too many.

## MIG-02: delta and incremental loads

### Watermark window

`window` restricts both datasets of a table, for every comparison and check, to the rows of one load:

```yaml
tables:
  - name: orders_delta
    source: { table: sales.orders }
    target: { table: dwh.orders }
    keys: [order_id]
    window:
      source_column: changed_at
      target_column: src_changed_at
      start: "${var:last_watermark}"
      end: "${run.date}"
```

| Option | Default | Meaning |
|---|---|---|
| `column` | none | Watermark column on both sides |
| `source_column` | `column` | Watermark column on the source |
| `target_column` | `column`, then `source_column` | Watermark column on the target |
| `start` | none | Inclusive lower bound |
| `end` | none | Upper bound, exclusive unless `end_inclusive` |
| `end_inclusive` | false | Make `end` inclusive |

Set `start`, `end`, or both. Bounds accept `${var:*}` (pass them with `--var last_watermark=...`) and `${run.date}` or `${run.timestamp}`. ISO dates and timestamps become date and timestamp literals (offsets are converted to UTC); numbers stay numbers, so a change sequence number works as a watermark. Rows whose watermark is NULL are left out. The predicates used are listed in the table's `details.row_filters`.

### Change data capture

`cdc_applied` checks that the latest change of every key in a change table is reflected in the dataset (by default the target):

```yaml
checks:
  - type: cdc_applied
    changes:
      connection: legacy        # defaults to the checked dataset's connection
      table: cdc.customer_changes
      key: [customer_id]
      operation: op
      sequence: [commit_lsn, seq]
    soft_delete: { column: is_deleted }
    compare_columns:
      - { change: name }
      - { change: city, target: city_name }
```

The latest change per key is chosen in the change table's database with `ROW_NUMBER()` over the `sequence` columns (descending; keep them non-NULL). Then:

- inserts and updates must be present (and not flagged deleted);
- deletes must be absent, or flagged when `soft_delete` is set;
- with `compare_columns`, the dataset must hold the latest after-image values (compared as text).

| Option | Default | Meaning |
|---|---|---|
| `changes.connection`, `table` or `query`, `filter` | | Where the change table is |
| `changes.key` | required | Key columns in the change table, in the order of the dataset's key |
| `changes.operation` | required | Operation code column |
| `changes.sequence` | required | Columns ordering the changes of one key |
| `key_columns` | the table's keys | Key columns of the checked dataset |
| `insert_values` | `i, insert, c, create, r, read` | Insert codes (case-insensitive) |
| `update_values` | `u, update` | Update codes |
| `delete_values` | `d, delete` | Delete codes |
| `soft_delete` | none | How the dataset flags deleted rows (see [flags](#flags)) |
| `compare_columns` | none | `{change, target}` column pairs compared with the latest change |

For SQL Server change tables, set `insert_values: ['2']`, `update_values: ['4']`, `delete_values: ['1']`, and `changes.filter: "__$operation <> 3"` to skip before-images. Changes with an operation code in no list are reported as `unknown_operation`.

### Late-arriving data

`late_arriving` finds rows that arrived after their period closed, or later than allowed:

```yaml
checks:
  - type: late_arriving
    event_column: posting_date
    load_column: loaded_at
    period_start: "${var:period_start}"   # events before it, loaded on or after it
    max_lateness: 2d                      # or loaded more than 2 days after the event
    severity: warning
```

| Option | Default | Meaning |
|---|---|---|
| `event_column` | required | When the business event happened |
| `load_column` | required | When the row was loaded |
| `period_start` | none | Rows with an earlier event, loaded on or after `loaded_since`, are late |
| `loaded_since` | `period_start` | Load time from which such rows count as late |
| `max_lateness` | none | Rows loaded more than this after their event are late |

Set `period_start`, `max_lateness`, or both. Rows missing either timestamp are not judged.

## MIG-03: idempotency

Re-running a load must not create duplicates or change the data:

```yaml
checks:
  - type: idempotency
    exclude: [loaded_at, batch_id]
```

The check counts duplicate keys, then records the row count and an order-independent checksum of the canonical content (two sums of row-hash prefixes; duplicates add up, they never cancel out). It compares them with the same check in the baseline run from the run history. Without a baseline run it passes and records its values for the next run.

| Option | Default | Meaning |
|---|---|---|
| `key_columns` | the table's keys | Columns that must be unique |
| `checksum_columns` | every column | Columns in the checksum |
| `exclude` | none | Columns left out of the checksum (load timestamps, batch IDs) |
| `baseline` | `last_run` | Run to compare with: `last_run`, `last_passed`, or a run ID |
| `compare` | `[row_count, checksum]` | What must be unchanged |

Run the load twice and dvf after each run; the second run fails if the load is not idempotent. It needs `history.enabled` (the default). When the checksum columns change between runs, the checksums are not compared.

## MIG-04: SCD Type 2

```yaml
checks:
  - type: scd2
    business_key: [customer_id]
    valid_from: valid_from
    valid_to: valid_to
    open_end: "9999-12-31"
    current_flag: { column: is_current, values: [true] }
    history:                      # optional version-count reconciliation
      connection: legacy
      table: legacy.customer_history
      keys: [cust_no]
```

Rules, each counted separately:

| Rule | Meaning |
|---|---|
| `null_valid_from` | Rows without `valid_from` |
| `invalid_range` | Closed rows whose `valid_from` is not before `valid_to` |
| `no_current` | Business keys without a current row |
| `multiple_current` | Business keys with more than one current row |
| `overlap` | A version starts before the previous one ends, or an open version is followed by another |
| `gap` | A version starts after the previous one ends (unless `allow_gaps`) |
| `flag_inconsistent` | The current flag contradicts `valid_to` (only with `current_flag`) |
| `history_count` | Business keys whose number of versions differs from `history` |

| Option | Default | Meaning |
|---|---|---|
| `business_key` | required | Business key columns |
| `valid_from`, `valid_to` | required | Validity columns |
| `current_flag` | none | How current rows are marked; without it, rows with an open end are current |
| `open_end` | none | A `valid_to` meaning "still valid"; NULL always counts as open |
| `end` | `exclusive` | `exclusive`: the next version starts at `valid_to`. `inclusive`: it starts `granularity` later |
| `granularity` | `1d` | Step between an inclusive end and the next start |
| `allow_gaps` | false | Do not report gaps |
| `history` | none | Dataset with one row per expected version; `keys` are its business key columns |

The SQL uses `ROW_NUMBER`, `LEAD`, and windowed sums, which DuckDB, SQLite, PostgreSQL, MySQL 8, SQL Server, Oracle, Snowflake, BigQuery, and Spark accept. Versions are ordered by `valid_from`, with open-ended versions after closed ones that start on the same date, and NULLs placed explicitly, so every database orders them the same way. The rules are verified on DuckDB, SQLite, PostgreSQL, MySQL, SQL Server, and Oracle.

## MIG-05: soft and hard deletes

```yaml
checks:
  - type: delete_propagation
    source_flag: { column: deleted, values: ['Y'] }
    target_flag: { expression: "deleted_at IS NOT NULL" }
    soft_deleted_rows: flagged
```

| Rule | Meaning |
|---|---|
| `hard_delete_not_propagated` | Target rows whose key is gone from the source and that are not flagged deleted |
| `soft_delete_not_propagated` | Soft-deleted source rows present but not flagged in the target |
| `wrongly_deleted` | Active source rows flagged deleted in the target |
| `soft_deleted_present` | Soft-deleted source rows present in the target (`soft_deleted_rows: absent`) |
| `soft_deleted_missing` | Soft-deleted source rows missing from the target (`soft_deleted_rows: flagged`) |

| Option | Default | Meaning |
|---|---|---|
| `hard_deletes` | true | Check that hard deletes reached the target |
| `source_flag` | none | How the source marks soft-deleted rows |
| `target_flag` | none | How the target marks deleted rows |
| `soft_deleted_rows` | `either` | Where soft-deleted source rows must be: `flagged`, `absent`, or `either` |

### Flags

A flag is a column holding one of `values` (compared as trimmed, case-insensitive text; `true` also matches `1`, `t`, `y`, and `yes`, and `false` matches `0`, `f`, `n`, and `no`), or an `expression` that is true when the flag is set. `values` defaults to `[true]`.

## MIG-06: surrogate and key maps

```yaml
tables:
  - name: customers
    source: { table: legacy.customer }
    target: { table: crm.customer }
    keys: [{ source: cust_no, target: legacy_cust_no }]
    checks:
      - type: key_map
        map:
          connection: crm
          table: xref.customer_map
          legacy_columns: [legacy_cust_no]
          new_columns: [customer_id]
        target_key: [customer_id]
```

| Rule | Meaning |
|---|---|
| `map_null_key` | Map entries with an empty legacy or new key |
| `legacy_multi` | Legacy keys mapped to more than one new key |
| `new_multi` | New keys mapped from more than one legacy key (with `reverse`) |
| `unmapped_source` | Source keys without a mapping (with `complete_source`) |
| `unknown_target` | Target keys missing from the map (with `complete_target`) |
| `dangling` | Map entries whose new key is not in the target (with `dangling`) |

| Option | Default | Meaning |
|---|---|---|
| `map` | required | The key map: `connection`, `table` or `query`, `filter`, `legacy_columns`, `new_columns` |
| `source_key` | the table's source keys | Legacy key columns in the source dataset |
| `target_key` | the table's target keys | New key columns in the target dataset |
| `reverse` | true | Each new key must come from exactly one legacy key |
| `complete_source` | true | Every source key must be mapped |
| `complete_target` | true | Every target key must be in the map |
| `dangling` | false | Report map entries pointing at missing target keys |

The map, the source, and the target may each be on a different connection. Keys are compared as text.

## MIG-07: audit and lineage columns

```yaml
checks:
  - type: audit_columns
    load_timestamp: dwh_loaded_at
    batch_id: dwh_batch_id
    source_system: dwh_source
    not_before: "${var:load_start}"
    max_batches: 1
    allowed_source_systems: [SAP_ECC, LEGACY_CRM]
```

| Option | Default | Meaning |
|---|---|---|
| `load_timestamp` | none | Load timestamp column: must be set, not in the future, and not before `not_before` |
| `batch_id` | none | Batch ID column: must be set |
| `source_system` | none | Source system column: must be set |
| `not_before` | none | Earliest acceptable load timestamp |
| `max_future` | `5m` | How far in the future load timestamps may be (clock skew) |
| `max_batches` | none | Most distinct batch IDs (`1`: one batch per load) |
| `allowed_batches` | none | Accepted batch IDs (case-insensitive) |
| `allowed_source_systems` | none | Accepted source systems (case-insensitive) |

Set at least one column. "Now" is taken in `normalization.timezone`, the time zone of timestamps without an offset. The result lists the most frequent batch IDs in `details.batches`.

## MIG-08: mock cutover comparison and sign-off

Compare two rehearsal runs:

```bash
dvf history compare 20261101T200000Z-1a2b 20261108T200000Z-3c4d --config dvf.yaml
dvf history compare reports/r1/results.json reports/r2/results.json --format json
dvf history compare RUN_A RUN_B -c dvf.yaml --fail-on-new   # exit code 1 on new failures
```

The output lists, from run A to run B:

- new, resolved, and persisting failures, and checks only in one run;
- status changes;
- run metrics (quality score, checks per status, rows compared, duration);
- per-table row counts and key reconciliation totals that changed;
- checks whose unexpected count changed.

`dvf history CONFIG` still lists recent runs (it is short for `dvf history list CONFIG`).

The `signoff` report format writes `signoff.md` and a printable `signoff.html` for the go/no-go meeting:

```yaml
reports:
  formats: [json, html, signoff]
project:
  metadata: { release: "Mock cutover 3", environment: UAT }
```

It holds the run identity, a recommendation, the scope, results by table, open failures (marked `new` or `persisting` against the run history's baseline), waivers, the run-to-run comparison, and an approvals table to fill in. The recommendation is:

- **NO-GO** on execution errors, failures at `error` severity or above, expired waivers, or an interrupted run;
- **CONDITIONAL** on lower-severity failures or warnings;
- **GO** otherwise.

Render it later from any results file: `dvf report --from results.json --format signoff -o out/`. The Excel report (`excel`) also has a sign-off sheet.

## MIG-09: consistent snapshots

Where the source supports time travel, compare a fixed point in time with `as_of` on the dataset (Snowflake, BigQuery, Databricks Delta, SQL Server temporal tables, Oracle Flashback; see [connectors](connectors.md)).

During live replication, rows changed in the last minutes have not reached the target yet. `in_flight` leaves them out:

```yaml
tables:
  - name: orders
    source: { table: sales.orders }
    target: { table: replica.orders }
    keys: [order_id]
    in_flight:
      column: last_modified
      window: 15m
      cutoff: "2026-11-30T22:00:00Z"
```

- Rows whose change timestamp is within `window` before the cutoff, or after it, are excluded on each side.
- With `exclude_keys_on_both_sides` (the default), the keys of those rows are excluded from the other side too. A row that changed on one side only is then neither "missing" nor "different".
- Rows with a NULL change timestamp are kept.

| Option | Default | Meaning |
|---|---|---|
| `column` | none | Change timestamp column on both sides |
| `source_column` | `column` | Change timestamp column on the source |
| `target_column` | `column`, then `source_column` | Change timestamp column on the target |
| `window` | required | How long before the cutoff changes count as in flight (`15m`, `1h`) |
| `cutoff` | the run's start | The cutoff: an ISO timestamp, `${run.*}`, or `${var:*}` |
| `exclude_keys_on_both_sides` | true | Exclude in-flight keys from both sides (needs keys) |
| `max_keys` | 10000 | Most in-flight keys; more is an error, so narrow the window |

The cutoff and the column are compared in `normalization.timezone` (a cutoff with an offset is converted to it). The number of excluded keys is in the table's `details.in_flight_keys_excluded`.

## MIG-10: character set conversion

```yaml
checks:
  - type: charset_conversion
    columns: [name, street, city]
    code_page: cp037
```

| Rule | Meaning |
|---|---|
| `replacement_char` | U+FFFD, left by a decoder that met an invalid byte |
| `mojibake` | UTF-8 text decoded as cp1252 or Latin-1, such as `CafÃ©` for `Café` |
| `control_chars` | Control characters other than those in `allowed_control` |
| `round_trip` | Characters the legacy code page cannot represent, so they cannot have come from it unchanged |

| Option | Default | Meaning |
|---|---|---|
| `columns` | required | Text columns to check |
| `code_page` | none | Legacy code page for `round_trip`: `cp037` or `cp500` (EBCDIC), `latin-1`, `cp1252`, or any Python codec |
| `rules` | all; `round_trip` only with `code_page` | Rules to apply |
| `allowed_control` | tab, line feed, carriage return | Acceptable control characters |
| `mojibake_patterns` | none | Extra substrings that mark mojibake |
| `unicode_form` | `none` | Normalize to `NFC` or `NFKC` before the round trip |
| `max_distinct` | 10000000 | Most distinct values examined per column; the rest are reported as not checked |

SQL cannot tell mojibake or code page fit, so the database groups and counts each column's distinct values, and dvf streams them to Python in batches. Counts are per row; samples show the value, with control characters written as `\xNN`.

## MIG-11: precision and time zone conversion

`dst_edge_cases` finds local timestamps that do not exist (clocks moved forward) or are ambiguous (clocks moved back) in a time zone. Converting them to UTC silently invents or picks an instant, differently in each system:

```yaml
checks:
  - type: dst_edge_cases
    side: source
    columns: [created_local]
    timezone: Europe/Berlin
```

| Option | Default | Meaning |
|---|---|---|
| `columns` | required | Local timestamp (or date) columns |
| `timezone` | `normalization.timezone` | Time zone of the local values |
| `rules` | `[nonexistent, ambiguous]` | Rules to apply |
| `min_year`, `max_year` | 1970, 2100 | Years whose transitions are examined (sentinel dates fall outside) |

Transitions come from the IANA time zone database, and the counting runs in the database as range predicates. For DATE columns, only dates whose midnight is skipped or repeated count (in zones that switch at midnight, a DATE converted to a TIMESTAMP lands on a nonexistent time). Timestamps with an offset are skipped: they are instants already.

To verify the conversions themselves, compare the source and target with the normal comparison strategies:

- **Local time to UTC:** set the source column's time zone with a normalizer, `columns.mappings: [{source: created_local, target: created_utc, normalize: [{to_utc: Europe/Berlin}]}]`, or set `normalization.timezone` for all naive timestamps. Both sides are canonicalized as UTC, so a wrong offset shows as a `time zone shift` mismatch.
- **DATE to TIMESTAMP:** a DATE compared with a TIMESTAMP is canonicalized as midnight, so a target holding a different time of day (or a shifted day) shows as a mismatch.
- **Precision:** `normalization.timestamp_precision` (default `us`) truncates both sides; use `truncate_timestamp` on one column, or `tolerance.timestamp`, for agreed precision loss.

## MIG-12: default and sentinel substitution

Substitutions such as NULL to `'N/A'` or `'00000000'` to NULL should be verified, not hidden by normalizers:

```yaml
checks:
  - type: substitution_rules
    rules:
      - { name: middle_name_na, column: middle_name, source_null: true, source_blank: true, target_value: "N/A" }
      - { name: dob_unknown, column: ERDAT, target_column: created_date, source_values: ["00000000"], target_null: true }
```

For each rule, every paired row whose source matches the rule's condition must hold the target value (or NULL). With `unmatched_sentinels`, target rows holding a rule's value whose source matches no rule for that value, and is not that value itself, are reported too: they show substitutions nobody specified.

Rule fields:

| Field | Meaning |
|---|---|
| `name` | Name used in the result (`rule_<name>`); defaults to the rule's position |
| `column` | Source column |
| `target_column` | Target column; defaults to the column paired with `column`, else the same name |
| `source_null`, `source_blank`, `source_values` | When the rule applies (any of them) |
| `target_null` or `target_value` | What the target must then hold (exactly one) |

| Option | Default | Meaning |
|---|---|---|
| `rules` | required | The substitutions |
| `trim` | false | Compare values after removing leading and trailing spaces |
| `unmatched_sentinels` | true | Report target sentinel values without a matching source condition |

Values are compared as text on rows paired by key. The metrics show, per rule, the source rows the rule applies to and the target rows that are correct.

## MIG-13: business parity

KPI and report queries (balances, open items, inventory valuation) are compared as query datasets (REC-08) with grouped control totals and tolerances. `examples/migration/dvf.yaml` compares a trial balance by company code, period, and account between a legacy ledger (debit/credit indicator, zero-padded accounts) and a new journal (signed amounts):

```yaml
tables:
  - name: trial_balance
    source:
      query: |
        SELECT bukrs AS company_code, gjahr || '-' || monat AS period, hkont AS gl_account,
               SUM(CASE WHEN shkzg = 'H' THEN -dmbtr ELSE dmbtr END) AS balance
        FROM gl_items GROUP BY bukrs, gjahr, monat, hkont
    target:
      query: |
        SELECT company_code, fiscal_period AS period, gl_account, SUM(amount) AS balance
        FROM journal_lines GROUP BY company_code, fiscal_period, gl_account
    keys:
      - company_code
      - period
      - { source: gl_account, normalize: [strip_leading_zeros] }
    comparison:
      strategies: [row_count, aggregate, full_diff]
      group_by: [company_code, period]
      aggregates: [count, sum]
      tolerance: { numeric_abs: 0.01 }
```

`aggregate` compares the balance totals per company code and period (the tolerance is per row, so it scales with the rows in a group); `full_diff` compares each account's balance within 0.01 and names the accounts that differ. Run the example from its folder: `cd examples/migration && dvf run dvf.yaml`.

## MIG-14: pre-migration baseline

Before cutover, snapshot the source:

```bash
dvf baseline capture dvf.yaml --name pre_cutover
dvf baseline capture dvf.yaml --name pre_cutover_target --side target --only table:customers
dvf baseline list dvf.yaml
```

For every selected table, the baseline records the row count; per column the NULL count, distinct count (skip with `--no-distinct` on very large tables), sum (numbers), and minimum and maximum (numbers, dates, timestamps); and row counts per `comparison.group_by` group (up to 10,000 groups). It is stored in `<state_dir>/baselines/<name>.json` (`.dvf/baselines/` by default), with the capture time and configuration hash. `--overwrite` replaces a baseline of the same name.

After the migration, compare current metrics with it. A failure is a no-go signal, and the baseline is the evidence for a rollback decision:

```yaml
checks:
  - type: baseline_metrics
    baseline: pre_cutover
    side: target
    metrics: [row_count, null_count, sum, groups]
    tolerance: { row_count_pct: 0, numeric_abs: 0.01 }
```

| Option | Default | Meaning |
|---|---|---|
| `baseline` | required | The name given to `dvf baseline capture --name` |
| `metrics` | `[row_count, null_count, sum]` | Any of `row_count`, `null_count`, `count_distinct`, `sum`, `min`, `max`, `groups` |
| `columns` | every captured column | Baseline columns to compare |
| `tolerance.row_count_pct` | 0 | Accepted row count difference in percent (also per group) |
| `tolerance.numeric_abs` | 0 | Accepted absolute difference of sums |
| `tolerance.numeric_rel` | 0 | Accepted relative difference of sums |
| `baseline_table` | this table | The `suite.table` entry of the baseline to use |

A source baseline can be compared with the target: column names are translated through the table's keys and `columns.mappings`. Minimum and maximum dates and timestamps are compared as canonical text, so a DATE source against a TIMESTAMP target differs; leave `min` and `max` out for such columns.

From Python: `dvf.api.capture_baseline(config, "pre_cutover")`, `dvf.api.list_baselines(config)`, and `dvf.api.compare_runs(run_a, run_b, config=config)`.

## Limits

- Pair checks pair rows by canonical key; duplicate keys on one side are counted once per matching row.
- `cdc_applied`, `key_map`, `substitution_rules`, and the `history` option of `scd2` compare keys and values as text.
- `in_flight` and `window` literals assume the columns hold timestamps (or numbers for numeric bounds); SQLite timestamps must be stored as `YYYY-MM-DD HH:MM:SS` text.
- Each check's evidence is capped by `sample_size`; failing rows beyond it go to the extract file when extracts are enabled.
