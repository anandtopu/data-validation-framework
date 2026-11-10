# Spark

dvf works with Apache Spark in three ways:

- the `spark` **connector** reads tables, Spark SQL queries, and files through a Spark session;
- the `spark` **engine** computes row-level diffs in Spark, for very large data;
- the `spark` **SQL dialect** renders every check, comparison, and normalizer as Spark SQL.

All three are verified on a local Spark session (Apache Spark 4.3 in local mode) by dvf's parity tests and seeded-defect harness, and show as `ready` in `dvf plugins list`. Cluster masters and Databricks Connect run the same code but are not covered by dvf's tests.

## Requirements

```bash
pip install 'dvf-framework[spark]'      # pyspark 4.0 or later
```

Spark needs Java 17 or 21 (`java` on `PATH`, or `JAVA_HOME`). dvf imports pyspark only when a Spark connection opens or the Spark engine runs, so other connections work without it.

## Connections

```yaml
connections:
  lake:
    type: spark                    # the running session, or a new local[*] session
  local_lake:
    type: spark
    master: local[8]
    conf:
      spark.sql.shuffle.partitions: "64"
      spark.driver.memory: 8g
  cluster:
    type: spark
    master: spark://spark-master.example.internal:7077
    catalog: spark_catalog
    schema: sales
  workspace:
    type: spark
    databricks_connect: true
    options: {profile: qa}         # a Databricks configuration profile
```

| Setting | Default | Meaning |
|---|---|---|
| `master` | unset | Spark master URL (`local[*]`, `local[4]`, `spark://host:7077`, `yarn`, `k8s://...`). Used when dvf starts the session; unset means `local[*]`. |
| `app_name` | `dvf` | Application name of a session dvf starts. |
| `conf` | `{}` | Spark settings. Runtime SQL settings (such as `spark.sql.shuffle.partitions` or `spark.sql.ansi.enabled`) apply to dvf's session in every case; static settings (such as `spark.driver.memory` or `spark.jars.packages`) apply only when dvf starts the session, and dvf logs a warning when it cannot apply one. |
| `catalog` | unset | Current catalog of dvf's session, for unqualified names in `table` and `query` datasets. |
| `schema` | unset | Current schema (database) of dvf's session. dvf fails at connect time if it does not exist. |
| `databricks_connect` | `false` | Open a Databricks Connect session instead (see below). |
| `query_timeout_s` | `defaults.execution.query_timeout_s` | Cancels the Spark jobs of a query that runs longer. |
| `max_parallel_queries` | `defaults.execution.max_queries_per_connection` | Tables using this connection at the same time. |
| `options.log_level` | unset | Spark log level to set on the application (`WARN`, `ERROR`, ...). |
| `options.profile`, `options.cluster_id`, `options.serverless` | unset | Databricks Connect only: configuration profile, cluster, or serverless compute. |

### Which session dvf uses

Spark allows one application per Python process.

- If a Spark session is already running (a notebook, a `spark-submit` job, an in-pipeline call), dvf uses it, whatever `master` says, and logs a warning when the masters differ.
- Otherwise dvf starts one with `master` (or `local[*]`), `app_name`, and `conf`. Local sessions dvf starts have no web UI and no console progress bars.

Either way, dvf works in a **child session** (`SparkSession.newSession()`). It shares the application's executors, catalogs, and cache, but has its own settings and temporary views, so nothing dvf sets changes your session. Your session's runtime settings are not inherited: put the ones dvf needs in `conf`. dvf's child session always uses:

- `spark.sql.session.timeZone=UTC`: timestamp canonicalization relies on it, so a different value in `conf` is ignored with a warning;
- adaptive query execution with partition coalescing and skew-join handling (`spark.sql.adaptive.*`), which `conf` may override;
- the default string-literal parsing (`spark.sql.parser.escapedStringLiterals=false`).

ANSI mode (`spark.sql.ansi.enabled`, on by default in Spark 4) is left as configured: dvf's SQL gives the same results with it on or off. With it on, a value that does not fit the canonical decimal (`DECIMAL(38, scale)`) raises an error instead of silently becoming NULL.

dvf never stops a Spark session. A session dvf started stays running for later runs in the same process and ends with the process; stop it yourself with `SparkSession.active().stop()`.

### Databricks Connect

With `databricks_connect: true`, dvf opens `databricks.connect.DatabricksSession` with the profile, cluster, or serverless option you give, and otherwise the SDK's usual configuration (environment variables or `~/.databrickscfg`). Install Databricks Connect in an environment **without** pyspark (it replaces it):

```bash
pip install dvf-framework 'databricks-connect==15.4.*'   # match your cluster's runtime
```

Spark Connect cannot create child sessions, so dvf applies its settings to the Databricks session itself and restores them when the connection closes. This mode is not verified by dvf's tests. To compare data in Databricks SQL warehouses without Spark Connect, use the `databricks` connector instead.

## Datasets

```yaml
tables:
  - name: orders
    source: {table: sales.orders}                         # a catalog table or view
    target: {query: "SELECT * FROM curated.orders WHERE region = 'EU'"}
  - name: customers
    source:
      path: landing/customers.csv                         # read by Spark
      options:
        schema: "customer_id BIGINT, name STRING, balance DECIMAL(12,3), created_at TIMESTAMP_NTZ"
    target: {path: s3a://curated/customers/, options: {format: parquet}}
```

- `table` names are passed to Spark as written (`schema.table`, `catalog.schema.table`, `global_temp.view`). Spark SQL also reads files directly: ``parquet.`s3a://bucket/path` `` works anywhere a table name is expected, for example as a referential integrity `parent.table`.
- `query` is any Spark SQL query.
- `path` reads files. Relative paths are relative to the configuration's folder; URLs (`s3a://`, `abfss://`, `gs://`, `hdfs://`, `dbfs:/`, `file:/`) are passed to Spark unchanged, and globs work.
- `filter` is a Spark SQL predicate on the dataset.

File datasets become temporary views of dvf's session the first time a query uses them. Dataset `options` are passed to Spark's reader, except:

| Option | Meaning |
|---|---|
| `format` | The file format (otherwise inferred from the extension, ignoring compression suffixes). |
| `schema` | A DDL schema (`"id BIGINT, name STRING"`). Recommended for CSV and JSON: it gives exact types and avoids a schema-inference pass. |

| Format | Spark data source | Needs | Notes |
|---|---|---|---|
| `parquet` | `parquet` | nothing | Timestamps without a time zone read as `TIMESTAMP_NTZ`. |
| `orc` | `orc` | nothing | |
| `csv`, `tsv` | `csv` | nothing | dvf sets `header: true`, and `inferSchema: true` unless you give a `schema`; `tsv` sets `sep` to a tab. Other options (`nullValue`, `timestampNTZFormat`, ...) pass through. |
| `json`, `ndjson` | `json` | nothing | One JSON document per line; set `multiLine: true` for files holding one array. |
| `avro` | `avro` | `org.apache.spark:spark-avro_2.13:<Spark version>` | Not bundled with pyspark. |
| `delta` | `delta` | `io.delta:delta-spark_2.13`, plus the Delta SQL extension and catalog settings | |
| `iceberg` | `iceberg` | `org.apache.iceberg:iceberg-spark-runtime-<Spark minor>_2.13`, plus a configured catalog | |

Add packages through `conf` when dvf starts the session (they are downloaded by Spark at startup), or install them on the cluster:

```yaml
lake:
  type: spark
  master: local[*]
  conf:
    spark.jars.packages: io.delta:delta-spark_2.13:4.0.0
    spark.sql.extensions: io.delta.sql.DeltaSparkSessionExtension
    spark.sql.catalog.spark_catalog: org.apache.spark.sql.delta.catalog.DeltaCatalog
```

When a format's data source is missing, the error names the package to add.

### Schemas and keys

Column types come from Spark's schema (`decimal(12,3)`, `timestamp_ntz`, `array<string>`, ...) and map to dvf's canonical types: `TIMESTAMP` is an instant (time zone aware), `TIMESTAMP_NTZ` a wall-clock time. Nullability is reported for catalog tables only. Spark declares no primary keys, so `keys: auto` does not work on Spark datasets; list the keys.

### Time travel

`as_of` reads a version or a point in time:

| Dataset | `as_of: 12` (a whole number) | `as_of: "2026-10-01 00:00:00"` |
|---|---|---|
| `table` (Delta or Iceberg table) | `VERSION AS OF 12` | `TIMESTAMP AS OF '2026-10-01 00:00:00'` |
| `path` with `format: delta` | `versionAsOf` | `timestampAsOf` |
| `path` with `format: iceberg` | `snapshot-id` | `as-of-timestamp` (converted to epoch milliseconds; UTC unless the value has an offset) |

Other formats and `query` datasets reject `as_of`; write `VERSION AS OF` inside the query instead.

## The Spark engine

Set `engine: spark` on the defaults, a suite, or a table:

```yaml
defaults:
  engine: spark
  execution:
    temp_dir: ./spill          # where non-Spark sides are staged (default: the system temp folder)
```

The engine runs the same two-phase diff as the other engines (keys and row hashes for every row, then full values for the differing rows only), in Spark:

1. **Spark sides** are projected (keys, row hash, tolerance columns) in Spark SQL in their own connection's session. Nothing moves: when both sides are on Spark connections in the same application, the whole diff is one Spark SQL job graph, and the result reports `rows_transferred: 0`.
2. **Other sides** (PostgreSQL, DuckDB, files, ...) are projected in their own database. The projection streams into a Parquet file under `temp_dir` (memory stays bounded by one batch), and Spark reads it. Only canonical keys and hashes move in phase A; `rows_transferred` counts the staged rows.
3. Each projection is cached by Spark (memory, spilling to disk) and exposed as a global temporary view, and the diff statements run there. Phase B projects only the mismatched rows: Spark sides by a semi-join with the mismatched keys; other sides in their own database, filtered by a key table (DuckDB) or a literal key list (up to 2,000 keys), or else in Spark after the transfer.
4. Views, cached data, and staged files are removed after each diff (`--keep-temp` keeps the files).

The diff runs in the session of the first Spark side. With no Spark side, it runs in a child of the running Spark session, or of a `local[*]` session dvf starts (and leaves running).

Tune it with the Spark connection's `conf`: `spark.sql.shuffle.partitions` (with adaptive execution on, a generous value is coalesced automatically), executor memory and cores, and `spark.sql.adaptive.skewJoin.*`. The checksum strategies (`hash_bucket`, `bisect`) and partitions work with the Spark engine: bucket checksums run in each side's database, and the engine diffs the remaining rows.

`engine: auto` chooses Spark when both datasets are Spark datasets in the same Spark application, so no row leaves Spark; for any other pair, select it explicitly. Spark has a fixed cost of a few seconds per diff (job scheduling and caching), so for data that fits on one machine the `duckdb` engine is usually faster; Spark pays off for data that does not fit on one machine, and when both sides already live in Spark.

**Clusters.** Staged Parquet files are written on the machine running dvf. With a local master they are read directly. On a cluster, set `temp_dir` to a folder mounted at the same path on the driver and every executor; otherwise dvf sends the staged projection to Spark through the driver (`createDataFrame`, in chunks), which works everywhere but holds that side's projection in driver memory. Comparisons whose sides are both Spark tables need neither.

## Canonicalization on Spark

Canonical forms follow the rules in [Configuration](configuration.md) and are identical to the other engines', with these documented exceptions (see `dvf.sql.spark`):

- Spark has no Unicode normalization function, so text is compared without it on Spark sides, and the comparison's notes say so. Set `normalization.unicode_form: none` (and drop the `unicode_nfc` normalizer) when comparing Spark with another database whose text may differ only in Unicode composition.
- Characters whose case pairs were added in a newer Unicode version than Python's tables can lower- or upper-case differently (Spark 4.3 uses ICU 78, Unicode 17).
- Floating-point keys: Spark writes `1.0E10` where Python writes `10000000000.0`. Floating-point columns are compared numerically, so this matters only when a key column is floating point.

## Normalizers in DataFrame code

The specification's `to_spark(col)` is realized as Spark SQL: each normalizer renders itself in the `spark` dialect, and the expression works with `F.expr` in DataFrame code:

```python
from pyspark.sql import functions as F
from dvf.core.types import CanonicalType
from dvf.normalize.normalizers import BUILTIN_NORMALIZERS
from dvf.sql.dialects import get_dialect

spark_sql = get_dialect("spark")
trimmed = BUILTIN_NORMALIZERS["trim"]().to_sql("customer_code", spark_sql, CanonicalType.string())
df = df.withColumn("customer_code", F.expr(trimmed))
```

## DataFrames from Python

`SparkConnector.from_dataframe(df, name)` wraps a DataFrame you already have. It returns the connector, open on a child of the DataFrame's session, and a relation for the DataFrame (registered as a global temporary view, which is dropped when the connector closes). `dvf.validate_dataframe` uses it for Spark DataFrames.

## Windows

- Spark reads local files without `winutils.exe`; the "Did not find winutils.exe" warning at startup is harmless. dvf never writes files with Spark (it stages with pyarrow), so Hadoop's native Windows binaries are not needed.
- PySpark turns collected timestamps into Python `datetime` objects with a call that fails before 1970 on Windows; dvf collects timestamps as epoch microseconds instead, so all dates work.
- dvf does not use Python UDFs, so it does not depend on Spark's Python workers.
- When the process exits, Spark may log errors about deleting its temporary folders; they are harmless.

## Limits

- `keys: auto` is not available (Spark has no primary keys).
- Unicode normalization is skipped on Spark sides (see above).
- Rows read through the connector by other engines (`engine: duckdb`, or `auto` with one Spark side) are collected one partition at a time through Python, which is bounded in memory but slower than the Spark engine for large datasets.
- Cluster masters, Databricks Connect, Avro, Delta, and Iceberg reads are implemented but not exercised by dvf's tests (they need a cluster or downloaded packages).
