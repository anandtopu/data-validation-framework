# dvf: Data Validation Framework

**Configuration-driven data and schema validation for data migrations and data engineering.**

dvf validates schemas and data quality, reconciles source and target datasets at scale, and produces detailed, auditable reports. It is built for migration projects (SAP, on-premise databases to cloud warehouses, SQL to NoSQL) and for data engineering pipelines (PySpark, Databricks, Parquet, Avro, JSON, CSV).

```bash
pip install dvf-framework
dvf init demo --example
dvf run demo/dvf.yaml
```

[Quickstart](getting-started.md){ .md-button .md-button--primary } [Your first migration comparison](tutorials/first-migration.md){ .md-button }

## What it does

- **Reconciles source and target** with escalating strategies: row counts, control totals, column profiles, bucketed checksums computed inside each database, and row-level diffs that move only the rows that differ. Every mismatch is classified (whitespace, case, encoding, rounding, truncation, time zone shift, NULL substitution, ...) with a suggested fix.
- **Compares schemas**: missing and extra columns, type compatibility across dialects, lengths, precision, nullability, constraints, collations, and schema drift.
- **Checks data quality**: completeness, uniqueness, validity, referential integrity, freshness, custom SQL and expressions, file-level checks, and the migration checklist (scope, deltas, idempotency, SCD Type 2, deletes, key maps, audit columns, cutover sign-off).
- **Scales**: computation is pushed to the data, memory stays bounded, tables run in parallel with per-connection limits, and interrupted runs resume from checkpoints.
- **Explains failures**: the exact SQL per side, masked samples, `dvf trace` for one record through every normalizer, extracts and repro files, run history and baselines.
- **Reports** in HTML, JSON, JUnit, Markdown, Excel, and PDF, all rendered from one versioned results document, plus notifications, OpenLineage events, OpenTelemetry, and result sinks for dashboards.
- **Runs anywhere**: the command line, Python (`dvf.run`, `dvf.validate_dataframe` inside a pipeline), Docker, the GitHub Action and CI templates, and orchestrators such as Airflow, Dagster, Prefect, Databricks Workflows, and Azure Data Factory.
- **Extends** through plugins: checks, connectors, normalizers, report formats, notification channels, secret providers, and sinks register exactly like the built-ins.

## Sources

| Kind | Systems |
|---|---|
| Files and lakehouse tables | CSV, Parquet, JSON, NDJSON, ORC, Avro, Excel, fixed width, XML, Delta Lake, Iceberg, on local disks or S3, ADLS, and GCS |
| Databases | DuckDB, SQLite, PostgreSQL, SQL Server, Oracle, MySQL, plus any SQLAlchemy dialect or ODBC driver |
| Cloud warehouses | Snowflake, BigQuery, Redshift, Databricks SQL |
| Big data | Spark (local, cluster, Databricks Connect), pandas, Polars, and Arrow DataFrames |
| NoSQL | MongoDB, Cassandra, DynamoDB, Cosmos DB |
| SAP | SAP HANA, and ECC or S/4HANA through RFC or OData |

Each connector has a maturity level: `ready` (verified against the live system) or `beta` (implemented and tested, not yet verified against a live system). The [components reference](reference/components.md) lists them all, and `dvf plugins list` shows the same in your installation.

## Find your way

| You want to | Read |
|---|---|
| Install dvf and run the example | [Installation](installation.md), [Quickstart](getting-started.md) |
| Learn by doing | [Your first migration comparison](tutorials/first-migration.md), [Write a plugin check](tutorials/plugin-check.md) |
| Write a configuration | [Configuration](configuration.md), the [configuration reference](reference/configuration.md) |
| Choose checks and comparisons | [Checks and comparisons](validations.md), [Migration validations](migration.md) |
| Connect a system | [Connectors](connectors.md), [Files](files.md), [NoSQL](nosql.md), [SAP](sap.md), [Spark](spark.md) |
| Generate a configuration | [Discovery and generation](discovery.md) |
| Investigate a failure | [Debugging and operations](debugging.md) |
| Automate it | [CI/CD](ci.md), [Orchestrators](orchestrators.md), [Notifications](notifications.md), [In-pipeline validation](pipelines.md) |
| Operate it safely | [Security](security.md), [Observability](observability.md) |
| Look something up | [Command line](cli.md), [Python API](reference.md), [Results schema](reference/results.md) |
| Extend it | [Plugins](plugins.md) |

## Design principles

1. Push computation to the data; move rows only to explain a mismatch.
2. Never require a whole table in memory.
3. Configuration first; Python for extending.
4. Everything is a plugin, including the built-ins.
5. One canonical, versioned result model behind every report.
6. Read-only and safe by default; secrets never appear in logs or reports.

The decisions behind the design are recorded in the [architecture decision records](adr/README.md).
