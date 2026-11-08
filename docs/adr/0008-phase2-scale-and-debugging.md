# ADR 0008: Phase 2 scale and debugging

- **Status:** Accepted
- **Date:** 2026-11-07

## Context

Phase 2 (BUILD_PROMPT.md section 18) must compare large tables quickly, move rows between databases only in proportion to their differences, survive interruptions, and make failures easy to explain. Its acceptance criteria are:

- a 10-million-row local comparison under two minutes;
- a 0.01% seeded difference found exactly, with transfer proportional to the difference;
- a killed run that can be resumed.

It also adds history, baselines, waivers, Excel reports, extracts, result sinks, and seven connectors. ADR 0007 set the Phase 1 execution model; this ADR records what Phase 2 changes and why.

## Decision

1. **Two-phase row diffs.**
   - Phase A compares canonical keys, row hashes, and tolerance columns for every row.
   - Phase B fetches canonical and raw values only for the rows phase A found different, and counts mismatches per column there.
   - The local DuckDB engine filters phase B rows in the source database. DuckDB-based connectors receive the key table through Arrow. Other connectors get literal key lists (up to 2,000 keys) or every row, which is then filtered locally.
2. **Linear row serialization.**
   - The canonical row is built with `concat()` in DuckDB, because a chain of `||` is quadratic there (21 s against 1 s for 25 columns over 10 million rows).
   - Each canonical value is computed once, in an inner query that the row hash references.
3. **Duplicates are probed, not assumed.** A cheap `GROUP BY key HAVING COUNT(*) > 1 LIMIT 1` query runs first. The `ROW_NUMBER` de-duplication (about 19 s on 10 million rows) runs only when a key repeats. The row kept per duplicated key is the one with the smallest row hash, in every engine.
4. **Arrow export without order.** DuckDB connectors stream projections with `preserve_insertion_order` off; with it on, DuckDB exports Arrow on one thread, about six times slower. dvf never depends on the order of bulk streams.
5. **Checksum search** (`hash_bucket`, `bisect`).
   - Each database summarizes buckets with:
     - `COUNT(*)` and `COUNT(DISTINCT key hash)`;
     - two independent sums of 32-bit row-hash prefixes;
     - per floating-point column, the non-NULL count, the sum and the sum of absolute values.
   - XOR is never used, because duplicates would cancel out. The sums fit in a signed 64-bit integer up to about two billion rows per bucket.
   - `hash_bucket` buckets by a 32-bit hash of the canonical key. Level `L` has `buckets * fanout**L` buckets, each nested in one bucket of the level above, and bucket numbers come from `FLOOR(x * G / 2**32)` in every dialect.
   - `bisect` segments an integer key by ranges whose widths are powers of the fanout, so segments nest too. Range predicates let databases use an index on the key.
   - The search stops drilling when any of these holds:
     - the differing buckets hold at most `drilldown_threshold` rows;
     - drilling no longer narrows (90% of children differ);
     - children would hold fewer than four rows;
     - the next predicate would name more than 4,000 buckets.
   - The configured engine then diffs only the remaining rows, exactly. Matched buckets contribute their distinct keys and duplicates to the summary.
   - Floating-point columns are compared per bucket by sums within tolerance, which is approximate, and exactly per row in differing buckets.
6. **Partitions are relation filters.** A partition is a predicate per side: values, ranges, key-hash buckets, or user predicates. Value and range partitions add an "other" and a NULL partition, so they are complete. Partition results add up. A changed partition column shows as a missing row in one partition and an extra row in another, and dvf warns when that can happen.
7. **Parallel tables, capped connections.**
   - A thread pool runs `max_parallel_tables` tables.
   - Each task first leases a slot of every connection it may use (suite, table, and check connections, in sorted order, so leases never deadlock), up to `max_parallel_queries` or `max_queries_per_connection`.
   - Each worker gets its own session through `Connector.fork()`. DuckDB forks share one database through cursors.
   - Partitions run one after another within a table.
8. **Checkpoints and resume.**
   - Finished tables and partitions are written atomically under `.dvf/runs/<run_id>/`, with a manifest holding the config hash and selectors.
   - `--resume RUN_ID` reuses the run ID and refuses a changed configuration unless `--force-resume` is given.
   - An interrupted run (Ctrl+C) finishes the running tables, marks the rest skipped, sets `interrupted`, writes partial results, and exits with code 2. A killed run leaves its checkpoints behind.
9. **Waivers** apply when a result is recorded, before hooks see it.
   - Column waivers waive a column comparison only when every differing column is waived.
   - Expired waivers never apply: the failure stands, the result says the waiver expired, and the run lists it as expired.
10. **History** is a local DuckDB (or SQLite) file, written once at the end of a run and best effort.
    - It provides the baseline for the run-to-run diff (`last_run`, `last_passed`, or a run ID) and the trend of earlier runs.
    - Retention prunes both history rows and checkpoint folders.
11. **Debugging tools reuse the run's planning.**
    - `explain` resolves datasets with catalog lookups only and prints the SQL each step would run.
    - `trace` shows one record through every normalizer on both sides.
    - `debug` and `rerun` (or `--failed-from`) read the saved results.
    - Failed checks get Parquet or CSV extracts and repro files: per-side SQL, and a `check.yaml` that extends the original configuration and runs only that check.
12. **Connector maturity.** A component is `ready` when it is implemented and verified against a live system (integration tests), and `beta` when it is implemented and unit-tested but not yet verified. Snowflake, BigQuery, Redshift, and Databricks are beta until their live integration tests run.
13. **Portable diff SQL for more servers.**
    - `SqlDiff` takes its counts from cross-joined derived tables, because Oracle rejects scalar subqueries next to aggregates.
    - On MySQL, which has no `FULL OUTER JOIN`, the join is a left join plus the target rows that have no source row.
    - Emptiness (`null_equals_empty`, `null_if_empty`, `strip_leading_zeros`, `not_blank`) is tested by length, not with `= ''`. Oracle stores `''` as NULL, SQL Server's `= ''` ignores trailing spaces, and MySQL's default collation treats ignorable characters as equal to `''`.
    - The referential integrity check flags orphans in a derived table, because SQL Server rejects `EXISTS` inside an aggregate.
    - Oracle `NUMBER(p, 0)` with up to 18 digits is an integer type.
14. **Retries.**
    - Queries that fail with a transient error (lost connection, deadlock, serialization failure) are retried with exponential backoff (`retries`, `retry_backoff_s`), after reconnecting.
    - Timeouts and bad queries are not retried.
    - `defaults.execution.query_timeout_s` applies to every connection that does not set its own.

## Consequences

- The 10-million-row, 30-column local benchmark ran in 36 seconds on an idle 24-core laptop, and in 76 seconds while other work shared the machine (target: two minutes); it took more than ten minutes before these changes.
- With 1,000 differences in 10 million rows, a DuckDB database against Parquet files moved 133,500 rows with `hash_bucket` and 103,192 rows with `bisect` (0.5% to 0.7% of the rows of both sides), against 20 million for a full transfer.
- Repeated scans replace data movement in the checksum search: each level reads the remaining buckets once per side. This favors columnar warehouses and indexed keys (`bisect`), and is slower than a full local diff when both sides are local files.
- Bucket-level comparisons are probabilistic: two different bucket contents with equal counts and both 32-bit sums collide with a probability of about 2^-64 per bucket.
- Rows whose partition value changes are reported as missing and extra. Partition by stable columns.
- Beta connectors are usable, but their canonical SQL is verified only by parsing and by unit tests until someone runs the live tests.
- Notifications remain Phase 4; `sample` (REC-07) remains declared.
