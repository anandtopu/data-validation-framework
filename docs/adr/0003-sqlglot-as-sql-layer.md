# ADR 0003: sqlglot as the SQL and type layer; Ibis optional

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

`dvf` must generate SQL for many dialects (PostgreSQL, SQL Server, Oracle, Snowflake, BigQuery, Databricks, DuckDB, and others) and parse native column types from each of them. Two Python options exist:

- **sqlglot**: a dependency-free SQL parser, transpiler, and builder with broad dialect coverage, including a data type parser.
- **Ibis**: a dataframe-style expression API over about 20 backends. Its SQL backends use sqlglot to produce SQL, and it brings backend-specific dependencies and its own release cadence. Recent releases have needed upper pins on DuckDB and sqlglot.

The framework needs fine control over generated SQL: hash expressions, canonical string casts, bucket arithmetic, and dialect workarounds.

## Decision

- **sqlglot is a core dependency.** It parses native type strings for the canonical type system (Phase 0) and builds pushdown SQL (Phase 1).
- **Ibis is not a core dependency.** We will run a spike during Phase 1. If Ibis simplifies multi-backend expression building without reducing control, it can be adopted behind the engine interface in a later ADR.
- sqlglot is pinned to a tested version range, with the upper bound raised only after the type-mapping and parity tests pass.

## Consequences

- We own the mapping from sqlglot types to canonical types, including dialect corrections (for example, Oracle `DATE` includes a time component, and SQL Server `DATETIME` has millisecond precision).
- Dialects sqlglot does not support (SAP HANA, DB2) fall back to the generic parser plus overrides.
- sqlglot's fast release cadence requires CI to catch regressions before an upper bound is raised.
