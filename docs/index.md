# dvf: Data Validation Framework

**Configuration-driven data and schema validation for data migrations and data engineering.**

`dvf` validates schemas and data quality, reconciles source and target datasets at scale, and produces detailed, auditable reports. It targets migration projects (SAP, on-premise databases to cloud warehouses, SQL to NoSQL) and data engineering pipelines (PySpark, Databricks, Parquet, Avro, JSON, CSV).

!!! warning "Pre-alpha: Phase 3"
    dvf validates and compares data in files and lakehouse tables (Parquet, CSV, JSON, ORC, Avro, Excel, fixed width, XML, Delta Lake, Iceberg), databases (DuckDB, SQLite, PostgreSQL, SQL Server, Oracle, MySQL), MongoDB, and Spark, plus beta Snowflake, BigQuery, Redshift, Databricks, Cassandra, DynamoDB, Cosmos DB, SAP HANA, and SAP RFC connectors. It scales to large tables with bucketed checksums and parallel execution, explains every failure, covers the migration checklist (MIG-01 to MIG-14), and validates DataFrames inside pipelines. Docker images, notifications, and the 1.0 release arrive in Phase 4.

## What works today

| Command | What it does |
|---|---|
| `dvf init <dir> --example` | A runnable local project with synthetic data and seeded discrepancies |
| `dvf run <file>` | Schema checks, comparisons, and data quality checks, with HTML, JSON, JUnit, and Markdown reports |
| `dvf compare <source> <target> -k <key>` | An ad hoc comparison of two datasets, without a configuration file |
| `dvf report --from results.json` | Re-render reports from a results document |
| `dvf connections test <file>` | Open every connection and run a trivial query |
| `dvf run <file> --resume <run_id>` | Continue an interrupted or killed run from its checkpoints |
| `dvf explain`, `dvf trace`, `dvf debug`, `dvf rerun`, `dvf history` | See the plan and SQL, follow one record, drill into a run, rerun failures, list runs ([Debugging](debugging.md)) |
| `dvf discover`, `dvf import-mapping`, `dvf import-contract`, `dvf export-contract` | Generate a configuration from two databases' metadata, a source-to-target mapping sheet, or an ODCS contract ([Discovery](discovery.md)) |
| `dvf profile`, `dvf suggest` | Column profiles, and candidate checks from them |
| `dvf baseline capture`, `dvf history compare` | Pre-migration baselines and rehearsal-to-rehearsal comparisons ([Migration validations](migration.md)) |
| `dvf sap templates` | SAP business object templates ([SAP](sap.md)) |
| `dvf.validate_dataframe(df, checks)` | Validate a pandas, Polars, Arrow, or Spark DataFrame in a pipeline and quarantine failing rows ([In-pipeline validation](pipelines.md)) |
| `dvf --version`, `dvf version` | Versions of dvf, its schemas, the plugin API, Python, and the platform |
| `dvf validate-config <file>` | Load and validate a configuration, with file, line, and column for every problem |
| `dvf config show <file>` | The fully resolved configuration, with secrets redacted |
| `dvf schema config`, `dvf schema results` | JSON Schemas for IDE autocomplete and integrations |
| `dvf plugins list` | Every registered component and whether it is ready, beta, or declared |

## Design principles

1. Push computation to the data; move rows only to explain a mismatch.
2. Never require a whole table in memory.
3. Configuration first; Python for extending.
4. Everything is a plugin, including the built-ins.
5. One canonical, versioned result model behind every report.
6. Read-only and safe by default; secrets never appear in logs or reports.

Start with [Getting started](getting-started.md), then read [Configuration](configuration.md).
