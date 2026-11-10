# Roadmap

This is the public roadmap after 1.0. It lists what is planned, not what is promised, and has no dates. Ideas and use cases are welcome as GitHub issues or discussions; contributions are welcome too (see CONTRIBUTING.md).

`dvf plugins list` is the source of truth for components. Anything it shows as `declared` is planned: its name and configuration already validate, but it cannot run yet. Anything shown as `beta` works and is unit-tested, but has not yet been verified against a live system.

## Planned components

These are declared in the registry today:

| Kind | Name | What it will do |
|---|---|---|
| connector | `db2` | IBM Db2 |
| connector | `teradata` | Teradata Vantage |
| reporter | `pdf` | A PDF rendered from the HTML report |
| SAP RFC backend | `nwrfcsdk` | Direct calls to the SAP NW RFC SDK, without PyRFC (SAP stopped maintaining PyRFC in January 2026) |

Proprietary drivers, for example for Db2 or Teradata where no open-source driver exists, will stay user-installed, as they are for SAP today.

## Verifying beta components against live systems

These work and are unit-tested, often against local emulators, but have not run against the real service in CI. Each moves to `ready` once integration tests run against a live system:

- connectors `snowflake`, `bigquery`, `redshift`, `databricks`, `cassandra`, `dynamodb` (verified against DynamoDB Local), `cosmos`, `sap_hana`, and `sap_rfc`;
- Spark on clusters and through Databricks Connect, object storage, and Iceberg REST, Glue, and Hive catalogs;
- the secret providers `keyring`, `awssm`, `azurekv`, `gcpsm`, and `databricks` (`vault` is verified against HashiCorp Vault), and the `snowflake` result sink;
- the orchestrator integrations (Airflow, Dagster, Prefect, Databricks, AWS Glue), tested with stand-in modules, and real Slack, Teams, and OTLP endpoints.

Reports from people who run these against real systems help most here.

## Beyond 1.0

These are out of scope for 1.0 and may come later, roughly in order of how often they are asked for:

- **Adapters for other validation tools**: run existing Great Expectations suites, Soda checks, or Pandera schemas and merge their results into the dvf report.
- **Catalog publishing**: send results to DataHub, OpenMetadata, and Unity Catalog.
- **More executors**: Ray and Dask, alongside the local executor and Spark.
- **Stream validation**: Kafka and other real-time sources, beyond Spark Structured Streaming's `foreachBatch`.
- **Anomaly detection on run history**: flag metrics that drift from their history, beyond fixed thresholds and baselines.
- **Rule suggestions with a language model**: an optional plugin on top of `dvf suggest`, disabled by default, never sending data without explicit configuration.
- **A web UI and dashboard server** for browsing runs, trends, and waivers.
- **Automatic remediation**: generating fix scripts for classified mismatches. dvf will stay read-only by default.

## How the roadmap changes

Maintainers review this file at each minor release. Significant decisions are recorded as ADRs in `docs/adr/`.
