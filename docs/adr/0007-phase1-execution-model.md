# ADR 0007: Phase 1 execution model

- **Status:** Accepted
- **Date:** 2026-11-06

## Context

Phase 1 turns the configuration contract into a working local MVP: DuckDB, SQLite, PostgreSQL, and file connectors; the core data quality checks; schema checks SCH-01 to SCH-06; comparisons REC-01 to REC-05 and REC-12; and JSON, HTML, JUnit, and Markdown reports. ADR 0002 set the strategy (pushdown first, escalating comparisons, Arrow interchange). This ADR records how Phase 1 implements it and which trade-offs it accepts.

## Decision

1. **DuckDB and PyArrow are core dependencies.** DuckDB is the zero-setup local engine and also reads CSV, Parquet, and JSON files (the `files` connector is DuckDB-backed). PyArrow is the interchange format. Polars (`dvf[polars]`) and psycopg (`dvf[postgres]`) are optional extras.
2. **Canonicalization has one reference and several implementations.** `dvf.normalize.canonical` defines the canonical string of every value in pure Python. SQL per dialect (DuckDB, PostgreSQL, SQLite) and Polars implement the same rules. Hypothesis parity tests compare every implementation with the reference, including NULLs, Unicode, negative zero, half-way rounding, pre-1970 timestamps, and DST transitions. SQLite calls the reference through registered functions, so it agrees by construction.
3. **Case folding is per character.** Databases lower-case one code point at a time. Python and Polars apply context rules (final sigma) and multi-character mappings (dotted capital I). The reference therefore defines lower case per character, and Polars pre-maps the two special characters.
4. **Trimming removes spaces only.** DuckDB's one-argument `TRIM` removes all Unicode whitespace, unlike PostgreSQL and SQLite, so dvf always passes the character list.
5. **Floating-point columns are compared numerically, never hashed.** Decimals and integers are compared as fixed-scale text (rounded half away from zero). Decimals of unknown scale use 10 places unless `normalization.decimal_scale` is set.
6. **Each side is projected in its own database.** Keys and columns are normalized and canonicalized by SQL generated for that side's dialect, producing key text, raw text, canonical text, numeric values, and a row hash (MD5 of length-prefixed fields).
7. **Row-level engines** consume the projections:
   - `pushdown` runs the diff as one SQL statement set inside the database, when both datasets share a connection;
   - `duckdb` streams both projections into a local DuckDB (spilling to disk) and diffs there;
   - `polars` fetches raw typed values and canonicalizes and diffs with Polars expressions;
   - `auto` picks `pushdown` for a shared connection and `duckdb` otherwise.
8. **Escalation.** Strategies run in order; `on_mismatch` (default `full_diff`) runs only when an earlier strategy did not pass. `row_hash` transfers only keys and hashes; `full_diff` transfers canonical and raw values for all rows in Phase 1. Bucketed drill-down that transfers only mismatched rows is Phase 2.
9. **Duplicates are compared once per key.** Duplicate keys are counted per side (rows beyond the first), and the row with the smallest canonical content per key takes part in the comparison.
10. **Mismatches are classified in Python** on the raw values of at most `max_mismatch_rows` mismatches. Exact per-column counts always come from the engine; hints state when classification was based on a sample. The classifier moved forward from Phase 2 because Phase 1's acceptance requires correct classification.
11. **Failures are contained.** Every check runs inside an error boundary. A failed query becomes an `error` result with a hint and a redacted traceback, and the run continues. Results JSON is always written.
12. **Execution is sequential in Phase 1.** `execution.max_parallel_tables` and per-connection caps take effect with the parallel executor in Phase 2.

## Consequences

- One `pip install dvf` runs the complete local workflow (`dvf init --example`, `dvf run`, `dvf compare`) on Windows, macOS, and Linux.
- The wheel is larger because of DuckDB and PyArrow (about 70 MB installed). This is the price of a zero-setup engine.
- Cross-database `full_diff` moves canonical values for every row. Until Phase 2, use `row_hash` for very large tables and keep `full_diff` as the escalation.
- New dialects need SQL implementations of the canonical forms and must pass the parity suite before they are declared ready.
- Seeded-defect end-to-end tests (`tests/e2e`) are the acceptance backbone: they must find every injected defect exactly and classify it correctly on every engine and backend.
