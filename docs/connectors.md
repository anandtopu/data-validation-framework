# Connectors

A connection's `type` selects its connector. `dvf plugins list --kind connector` shows each connector's status:

- `ready`: implemented and verified against a live system by integration tests;
- `beta`: implemented and unit-tested, but not yet verified against a live system;
- `declared`: planned for a later phase.

Proprietary drivers are never bundled: install the extra for the systems you use.

| Type | Systems | Install | Status | Time travel (`as_of`) |
|---|---|---|---|---|
| `files` | CSV, TSV, Parquet, JSON, NDJSON, ORC, Avro, Excel, fixed width, XML, Delta Lake, Iceberg; gz, zst, bz2, xz; Hive-partitioned folders; local, or object storage through fsspec or DuckDB extensions ([details](files.md)) | included (`dvf[delta]`, `dvf[iceberg]`, `dvf[avro]`, `dvf[excel]` for those formats) | ready | Delta (version or timestamp), Iceberg (snapshot or timestamp) |
| `duckdb` | DuckDB, in memory or on disk | included | ready | no |
| `sqlite` | SQLite files | included | ready | no |
| `postgres` | PostgreSQL 13+ and compatible | `dvf[postgres]` | ready | no |
| `mssql` | SQL Server 2019+, Azure SQL (verified with SQL Server 2022) | `dvf[mssql]` | ready | `FOR SYSTEM_TIME AS OF` (temporal tables) |
| `oracle` | Oracle Database (verified with 23ai Free; thin mode, no client libraries) | `dvf[oracle]` | ready | `AS OF TIMESTAMP` (Flashback) |
| `mysql` | MySQL 8.0.17+ (verified with 8.4); MariaDB is not supported yet | `dvf[mysql]` | ready | no |
| `snowflake` | Snowflake | `dvf[snowflake]` | beta | `AT(TIMESTAMP => ...)` |
| `bigquery` | Google BigQuery | `dvf[bigquery]` | beta | `FOR SYSTEM_TIME AS OF` |
| `redshift` | Amazon Redshift | `dvf[redshift]` | beta | no |
| `databricks` | Databricks SQL warehouses and clusters | `dvf[databricks]` | beta | `TIMESTAMP AS OF` / `VERSION AS OF` (Delta) |
| `spark` | Apache Spark 4: a running session, local, cluster, or Databricks Connect (unverified); catalog tables, Spark SQL, Parquet, ORC, CSV, JSON (Avro, Delta, Iceberg with their packages); see [Spark](spark.md) | `dvf[spark]` and Java 17 or later | ready | `VERSION AS OF` / `TIMESTAMP AS OF` (Delta, Iceberg) |
| `mongodb` | MongoDB 4.4+ (verified with 7.0); see [NoSQL](nosql.md) | `dvf[mongodb]` | ready | no |
| `cassandra` | Apache Cassandra, ScyllaDB, DataStax Astra | `dvf[cassandra]` | beta | no |
| `dynamodb` | Amazon DynamoDB (run against DynamoDB Local) | `dvf[dynamodb]` | beta | no |
| `cosmos` | Azure Cosmos DB for NoSQL | `dvf[cosmos]` | beta | no |
| `sap_hana` | SAP HANA 2.0 and HANA Cloud at database level; see [SAP](sap.md) | install `hdbcli` yourself | beta | no |
| `sap_rfc` | SAP ECC and S/4HANA through RFC (PyRFC, a custom read module) or OData | PyRFC and the NW RFC SDK yourself; nothing for OData | beta | no |

`pip install 'dvf[all]'` installs every open-source extra. SAP drivers are never installed by dvf.

NoSQL and SAP RFC datasets are copied once per run into local Parquet files and queried with DuckDB, so every check and comparison works on them; filter them at the source through their dataset `options` ([NoSQL](nosql.md), [SAP](sap.md)). DataFrames passed to `dvf.validate_dataframe` use an in-process `dataframe` connection that cannot be configured in a file ([In-pipeline validation](pipelines.md)).

## Connecting

Server databases take either a `url` (treated as a secret) or `host`, `port`, `database`, `user`, and `password`:

```yaml
connections:
  erp:
    type: mssql
    url: ${secret:env:ERP_URL}              # mssql://user:password@host:1433/erp
    max_parallel_queries: 1                  # protect a production source
    query_timeout_s: 900
  dwh:
    type: oracle
    host: dwh.example.internal
    port: 1521
    database: DWHPDB                         # service name
    user: dvf_reader
    password: ${secret:env:DWH_PASSWORD}
  shop:
    type: mysql
    url: ${secret:env:SHOP_URL}              # mysql://user:password@host:3306/shop
  lake:
    type: snowflake
    account: acme-xy12345
    user: DVF_READER
    authenticator: externalbrowser           # or a password, key pair, or OAuth
    warehouse: QA_WH
    database: ANALYTICS
    role: QA_READONLY
```

Driver suffixes such as `mysql+pymysql://` are accepted. For SQL Server on a local or test instance, add `?TrustServerCertificate=yes` to the URL and use `127.0.0.1` rather than `localhost`. Every connector sets a UTC session time zone and, where the database supports it, a read-only session; elsewhere, give the dvf login read permissions only. dvf never writes to sources or targets.

## Database behavior that affects comparisons

dvf compares canonical text built inside each database. Most differences between systems are hidden by canonicalization; these remain:

- **Oracle** stores empty strings as NULL. A source empty string compared with an Oracle target shows as `null_vs_empty`; set `normalization.null_equals_empty: true` when that difference is expected. Canonical text is limited to 4,000 characters per value and per serialized row (ORA-01489 beyond). Oracle's `COMPOSE` composes only adjacent characters, so rare sequences of combining marks can differ from NFC.
- **Time zones**: MySQL converts zones only between 1970 and 2037, and SQL Server maps IANA zones to Windows zones (extend with `dvf.sql.mssql.register_windows_zone`) whose rules are today's for every year. Timestamps outside those ranges in non-UTC zones may convert differently from other databases.
- **Case-insensitive comparison** (`case_sensitive: false`) follows each database's case tables. MySQL (Unicode 9) and SQL Server (older tables) miss some recent characters.
- **SQL Server** has no regular expressions before SQL Server 2025, so `regex` checks raise a clear error there; the `truncate_timestamp` normalizer needs SQL Server 2022.
- **MySQL**, **Snowflake**, **Redshift**, and **Databricks** have no Unicode normalization function. dvf compares their text without NFC normalization and says so in the result's normalization notes. Text that differs only in Unicode composition then shows as a mismatch, never as a false match.
- **SQL Server** `LEN` ignores trailing spaces and `HASHBYTES` hashes UTF-16; dvf measures and hashes UTF-8 text so results match other databases. Hashing UTF-8 needs SQL Server 2019 or later.
- **Default collations** compare case- and accent-insensitively in MySQL and SQL Server. dvf compares canonical text with binary semantics, so `A` and `a` differ unless `normalization.case_sensitive: false`.
- **Large hash sums**: bucket checksums sum 32-bit integers per bucket. Every supported system holds the sums of about two billion rows per bucket; raise `comparison.buckets` beyond that.

## Beta connectors

Cassandra, DynamoDB, Cosmos DB, SAP HANA, and SAP RFC are covered on their own pages ([NoSQL](nosql.md), [SAP](sap.md)): they are tested with fake drivers (DynamoDB also against DynamoDB Local), not against the real services.

Snowflake, BigQuery, Redshift, and Databricks SQL are complete, but have only been verified with unit tests: fake drivers, every generated query parsed in its dialect with sqlglot, and some expressions evaluated locally. Their integration tests run against a real account when an environment variable holds a JSON object of connection settings:

```bash
DVF_TEST_SNOWFLAKE='{"account": "...", "user": "...", "password": "...", "warehouse": "..."}' uv run pytest -m integration tests/unit/test_cloud_connectors.py
```

Use `DVF_TEST_BIGQUERY`, `DVF_TEST_REDSHIFT`, or `DVF_TEST_DATABRICKS` in the same way. They run only `SELECT` statements. A connector becomes `ready` once its live tests, the canonicalization parity suite, and the seeded-defect suite pass against the service.

Known limits:

- **Snowflake**: regular expressions are POSIX ERE (no back-references or lazy quantifiers); decimals are limited to `NUMBER(38, s)`.
- **BigQuery**: `ARRAY` and `STRUCT` columns cannot be compared as text yet; `options` are passed to the query job configuration (for example `maximum_bytes_billed`).
- **Redshift**: rows whose canonical text exceeds 65,535 bytes cannot be hashed; set `options.dvf_fetch_size` above the default 1,000 rows for faster streaming on multi-node clusters.
- **Databricks**: needs Databricks Runtime 13.1 or later; the legacy flags `spark.sql.legacy.castDecimalToString.enabled` and `spark.sql.parser.escapedStringLiterals` must be off. Hive metastore tables report no nullability or primary keys.
- **Time travel** on Snowflake, BigQuery, and Databricks reads the time-travel relation, so `keys: auto` cannot find a primary key for those datasets; name the keys.

## Writing your own connector

Subclass `dvf.plugins.Connector` (see [Plugins](plugins.md)) and, for a new SQL dialect, `dvf.sql.dialects.Dialect`. A dialect must produce the canonical forms of the Python reference in `dvf.normalize.canonical` exactly; the parity tests in `tests/parity` show how to prove it. Register the dialect with `register_dialect` and the connector with `@register_connector`.
