# ADR 0009: Phase 3 big data and specialized sources

- **Status:** Accepted
- **Date:** 2026-11-08

## Context

Phase 3 (BUILD_PROMPT.md section 18) adds:

- the Spark engine (local and Databricks Connect) and the lakehouse formats Delta, Iceberg, Avro, and ORC;
- in-pipeline `validate_dataframe` with quarantine;
- the NoSQL pack, the SAP pack, and file-level checks;
- the migration validations MIG-01 to MIG-14;
- `discover`, `import-mapping`, `import-contract` (ODCS), `profile`, and `suggest`.

It is accepted when:

- the seeded-defect harness passes on Spark;
- the SAP normalizers pass the parity tests;
- a MongoDB-to-PostgreSQL example reconciles embedded arrays against child tables.

ADR 0008 set the Phase 2 execution model. This ADR records what Phase 3 adds and why.

## Decision

1. **Packs.**
   - Phase 3 components live in packs under `dvf.packs` (`spark`, `files`, `nosql`, `sap`, `migration`, `discovery`).
   - Each pack has a `register(registry)` function that attaches implementations to components declared in `dvf.plugins.builtins` and registers new ones.
   - Packs never import optional drivers at import time, so a missing extra affects only the connections that need it.
   - CLI commands added by later phases live in their own modules (`dvf.cli.discovery`, `dvf.cli.migration`, `dvf.cli.packs`), each with a `register(app)` function.
2. **Staged connectors for systems without SQL.**
   - Document stores and SAP RFC cannot run the SQL that checks and comparisons generate. `StagedConnector` fetches a dataset once, as Arrow record batches, writes it to a Parquet file in a temporary folder, and exposes it as a view in an embedded DuckDB database.
   - Every check, strategy, and engine then works unchanged, in the `duckdb` dialect.
   - Staging is lazy: resolving a dataset only names its view. The data is fetched the first time a query mentions the view, and connectors may describe a schema without reading the data.
   - Forked sessions share the staged files, and memory is bounded by one batch.
   - Source-side filters are pushed down through dataset `options`. The SQL `filter` applies to the staged rows.
3. **In-pipeline validation runs through the normal runner.**
   - `validate_dataframe` registers the frame under a process-local handle and runs a one-table configuration against an in-process `dataframe` connection (DuckDB over the Arrow data, without copying), or against the Spark session for Spark DataFrames. Results therefore have the same check IDs, severities, samples, and SQL as `dvf run`.
   - Row-level checks and `unique` also mark failing rows. Each check's `action` (`quarantine`, `drop`, `warn`, `fail`, defaulting to the call's `on_fail`) decides the row's fate.
   - Rows are split with boolean masks on the original frame, so pandas indexes and dtypes are kept.
   - A check that cannot be evaluated raises `DataValidationError`, so a broken check never lets rows through silently.
   - Nothing is written to disk.
4. **Spark is a dialect, a connector, and an engine.**
   - Normalizers are Spark SQL expressions rendered by the dialect, not separate `to_spark` implementations: one rendering per dialect keeps parity provable, and DataFrame users can apply the SQL with `F.expr`.
   - The `spark` dialect subclasses the Databricks dialect (Spark SQL is the Databricks runtime's language) and overrides only what real Spark showed to differ. Running it found one bug that also applied to Databricks: `UPPER` lengthens some strings. Both now upper-case those strings one character at a time.
   - Spark has no Unicode normalization function. ICU through `reflect` matched Python exactly, but `reflect` is non-deterministic and fails inside join and `EXISTS` conditions, so canonicalization skips the step on Spark and the plan notes say so.
   - dvf works in a child session (`newSession()`) with UTC, adaptive execution, and skew joins. It never changes or stops the user's session.
   - The engine projects Spark datasets in Spark SQL. Other sides are streamed from their own databases, staged as Parquet, and read by Spark; the same two-phase diff SQL then runs in Spark. `auto` chooses Spark only when both sides are Spark datasets in one application.
   - No Python UDFs are used, so no data passes through Python workers.
5. **Lakehouse tables and other file formats.**
   - Delta, Iceberg, and ORC tables are registered in DuckDB as PyArrow datasets, which keeps projection and filter pushdown and bounded memory.
   - Iceberg uses this fast path only for local tables without delete files and with a single schema. Delta tables with deletion vectors are staged.
   - Avro, Excel, fixed-width, and XML files are converted to Parquet once per connection, lazily, and shared by forked sessions.
   - File checks set `requires_dataset = False`, so `file_exists` can report that nothing arrived.
6. **NoSQL: exact type unification and nested reconciliation.**
   - Documents are staged with a two-pass disk spool, so column types are unified over every document rather than inferred from a sample. Mixed types fall back to canonical text with a note, and native type names (BSON types) are kept for schema checks.
   - The `nested` strategy pushes per-parent summaries down to each database (child count, canonical sums, and two sums of 32-bit hash prefixes of the child rows) and compares them in a local DuckDB, so memory stays bounded and reordering an array changes nothing.
7. **SAP: normalizers that read the row, physical DDIC types.**
   - `Normalizer.input_columns()` lets a normalizer read other columns of the row (`sap_curr` reads the currency), and `Normalizer.for_side(side)` restricts it to one side (the SAP side's decimal shift). Normalizers stay shared, immutable instances; the Polars engine fetches the extra columns.
   - DDIC schemas report physical types (a `DATS` field stays an 8-character string), because that is what the database stores. Schema checks compare types after each column's normalizers, so `sap_dats` makes a `DATS` field compatible with a target `DATE`.
   - New dialect primitives `substring`, `date_from_iso`, and `time_from_iso` build the SAP forms portably, because SQLite keeps dates as text and Oracle's `CAST` depends on session settings.
8. **Migration checks are pair checks and table options.**
   - Checks that need both sides by key subclass `PairCheck`. They run in the database when both sides share a connection, otherwise in a local DuckDB workspace that spills to disk.
   - The runner builds the comparison inputs even when comparison strategies are disabled.
   - `window` and `in_flight` are relation restrictions applied when datasets are resolved. In-flight keys are excluded from both sides through canonical key lists (`max_keys`).
   - Named baselines are JSON files under `<state_dir>/baselines`.
   - Window functions order NULLs explicitly, because MySQL and SQL Server sort them first and the others last.
9. **Discovery writes reviewable configuration.** Generated configurations are written with dvf's own comment-preserving YAML writer, so fuzzy matches, type risks, and missing keys appear as review comments next to the entries they concern. Contract conformance (SCH-12) is a check type (`contract`) rather than a `schema_checks` entry, because it needs a contract file.
10. **Maturity rules carry over from Phase 2.**
    - Components verified against a live system are `ready`. Implemented components that have not run against a live system are `beta`: Cassandra, DynamoDB (verified against DynamoDB Local only), Cosmos DB, SAP HANA, and SAP RFC.
    - Normalizers count as verified when they pass parity on real engines.
    - Proprietary drivers (SAP `hdbcli`, `pyrfc`, the NW RFC SDK) are never dependencies: users install them, and dvf imports them lazily with an actionable message.

## Consequences

- **Acceptance criteria met.**
  - The seeded-defect harness passes on Spark, including mixed DuckDB/Spark and PostgreSQL/Spark cases, with the duckdb engine's counts and classifications.
  - The SAP normalizers pass parity on DuckDB, SQLite, PostgreSQL, MySQL, SQL Server, Oracle, Polars, and Spark.
  - `examples/mongodb_to_postgres` reconciles MongoDB orders with embedded line arrays against PostgreSQL `orders` and `order_lines`, and reports exactly the seeded defects.
- **Spark costs.** The dev environment includes `pyspark`, so the Spark parity tests run in unit CI, which installs Java 21. Spark has a fixed cost of seconds per diff. On one machine, 1 million rows with 1,000 differences took 17 seconds with Spark and 4 seconds with DuckDB, so Spark pays off for data that does not fit on one machine or already lives in Spark.
- **Full reads.** NoSQL and RFC datasets are copied to local Parquet before checks run. Transfer is the full selected dataset, and checksum strategies do not reduce it, so push filters and projections down through `options` for large collections.
- **Per-call overhead.** `validate_dataframe` adds a configuration load and a run per call: milliseconds for batch pipelines and micro-batches, not a per-row API.
- **Unverified paths.** Cluster masters and Databricks Connect, object storage, Iceberg REST, Glue, and Hive catalogs, and the beta connectors are verified with unit tests only.
