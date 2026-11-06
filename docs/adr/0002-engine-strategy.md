# ADR 0002: Pushdown-first engine strategy

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

Migration reconciliation compares tables with millions to billions of rows that live in different systems. Pulling both tables into one process is slow, expensive, and often impossible. Existing tools show the trade-offs: Google's Data Validation Tool loads rows into memory for row validation and documents memory errors on large tables; reladiff (the fork of data-diff) compares segment checksums inside each database and transfers only differing segments.

## Decision

1. **SQL pushdown is the default.** Counts, aggregates, profiles, and bucket checksums are generated as SQL for each dialect and run inside the source and target engines.
2. **Escalating strategies.** A comparison runs cheap strategies first (row count, aggregates, bucket hashes) and fetches row-level data only for mismatched buckets or segments.
3. **Same-engine fast path.** When source and target are reachable from one engine, the comparison runs as a single in-database join.
4. **Local engines** are DuckDB (out-of-core, spill to disk) and Polars (lazy and streaming). DuckDB is the zero-setup default.
5. **Spark** is the distributed engine for very large data and Databricks.
6. **Engines are plugins** (`dvf.engines`), so Ray or Dask can be added later without core changes.
7. **Arrow** is the in-memory interchange format between connectors and engines.

## Consequences

- Every normalizer needs SQL, Polars, and Spark implementations, and parity tests must prove they agree. This is the main cost of the approach and is non-negotiable for correctness.
- Connectors must declare capabilities (hash functions, Arrow fetch, time travel) so the planner can choose a strategy.
- Phase 1 delivers pushdown, DuckDB, and Polars; Spark arrives in Phase 3.
