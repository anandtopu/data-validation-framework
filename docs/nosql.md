# NoSQL sources

dvf reads MongoDB, Cassandra, DynamoDB, and Cosmos DB collections, turns their documents into rows, and validates them like any other dataset: schema checks, data quality checks, and every comparison strategy work unchanged. This page explains how documents become rows, how their types are handled, how to check a document schema, and how to reconcile embedded arrays against relational child tables.

## Connectors

| Type | Systems | Install | Status |
|---|---|---|---|
| `mongodb` | MongoDB 4.4+ (verified with 7.0) | `dvf-framework[mongodb]` | ready |
| `cassandra` | Apache Cassandra, ScyllaDB, DataStax Astra | `dvf-framework[cassandra]` | beta |
| `dynamodb` | Amazon DynamoDB, DynamoDB Local | `dvf-framework[dynamodb]` | beta |
| `cosmos` | Azure Cosmos DB for NoSQL | `dvf-framework[cosmos]` | beta |

`beta` connectors are complete and unit-tested against fake drivers; DynamoDB has also been run against DynamoDB Local. None of them can read a dataset `as_of` a point in time.

```yaml
connections:
  shop:
    type: mongodb
    uri: ${secret:env:SHOP_MONGODB_URI}       # mongodb+srv://user:password@cluster/...
    database: shop
    read_preference: secondaryPreferred       # keep load off the primary
    read_concern: majority
  events:
    type: cassandra
    contact_points: [cass1.example.internal, cass2.example.internal]
    keyspace: events
    username: dvf_reader
    password: ${secret:env:CASSANDRA_PASSWORD}
    local_datacenter: dc1
  carts:
    type: dynamodb
    region: eu-west-1                         # credentials from the default AWS chain
  catalog:
    type: cosmos
    endpoint: https://catalog.documents.azure.com:443/
    key: ${secret:env:COSMOS_KEY}             # or credential: default (Microsoft Entra ID)
    database: catalog
```

All four connections are read-only: dvf only runs reads (`find`, `aggregate`, `SELECT`, `Query`, `Scan`) and catalog lookups.

### Connection settings

| Connector | Setting | Meaning |
|---|---|---|
| `mongodb` | `uri` (secret), `database` | Connection string and database. |
| | `read_preference` | `primary`, `primaryPreferred`, `secondary`, `secondaryPreferred`, or `nearest`; the connection string's setting when omitted. |
| | `read_concern` | `local`, `available`, `majority`, `linearizable`, or `snapshot`; the server default when omitted. |
| | `uuid_representation` | How binary UUIDs are decoded: `standard` (default, subtype 4), or `pythonLegacy`, `javaLegacy`, `csharpLegacy`, `unspecified` for subtype 3 UUIDs written by older drivers. |
| | `server_selection_timeout_ms` | How long to wait for a reachable server (default 10,000). |
| | `batch_size` | Documents per round trip (default 1,000). |
| `cassandra` | `contact_points`, `port`, `keyspace` | Hosts (default `127.0.0.1`), port (9042), and the keyspace for table names without one. |
| | `username`, `password` (secret) | Plain-text authentication. |
| | `local_datacenter` | Data center whose nodes serve reads. |
| | `consistency_level` | Default `LOCAL_QUORUM`, so replicas that have not caught up do not cause false mismatches. |
| | `secure_connect_bundle`, `ssl`, `protocol_version`, `connect_timeout_s`, `fetch_size` | Astra bundle, TLS, protocol, connect timeout (10 s), and rows per page (1,000). |
| `dynamodb` | `region`, `endpoint_url`, `profile` | AWS region, a custom endpoint (DynamoDB Local), and a named profile. |
| | `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token` (secrets) | Explicit credentials; omit them to use the default credential chain. |
| | `page_size` | Items per page (default 1,000). |
| `cosmos` | `endpoint`, `database` | Account URI and database. |
| | `key` (secret) or `credential: default` | Account key, or Microsoft Entra ID through `azure-identity`. |
| | `consistency_level` | `Strong`, `BoundedStaleness`, `Session`, `ConsistentPrefix`, or `Eventual` (can only relax the account default). |
| | `preferred_locations`, `page_size` | Regions to read from, and items per page (1,000). |

Every connection also accepts the common settings (`max_parallel_queries`, `query_timeout_s`, and `options`, which are passed to the driver's client unchanged).

## Datasets

A dataset names its collection, table, or container with `table`. The native read goes into `options`, next to the flattening settings described below. Unknown options are errors, so a misspelled filter never silently reads the whole collection.

```yaml
target:
  table: orders
  options:
    filter: { status: { $ne: cancelled }, order_date: { $gte: { $date: "2024-01-01T00:00:00Z" } } }
    projection: { audit: 0 }
```

| Connector | Option | Meaning |
|---|---|---|
| `mongodb` | `filter` | Query document for `find`. MongoDB Extended JSON is accepted (`$oid`, `$date`, `$numberDecimal`, `$numberLong`). |
| | `pipeline` | Aggregation pipeline, instead of `filter`, `projection`, `sort`, and `hint`. |
| | `projection` | Projection document, or a list of fields. With `fields` and no projection, dvf reads only the top-level fields it needs. |
| | `sort`, `hint`, `collation` | As in `find` (`sort: {_id: 1}`, an index name or key pattern, a collation document). |
| | `read_preference`, `read_concern` | Override the connection's settings for this dataset. |
| | `max_time_ms`, `allow_disk_use` | Server-side time limit (default: the connection's `query_timeout_s`), and disk use for aggregations. |
| `cassandra` | `where` | CQL predicate, for example `customer_id = 42 AND day >= '2024-01-01'`. |
| | `columns`, `allow_filtering`, `consistency_level` | Columns to read, `ALLOW FILTERING`, and a per-dataset consistency level. |
| `dynamodb` | `key_condition` | `KeyConditionExpression`; with it dvf runs a Query, without it a Scan. |
| | `filter`, `values`, `names` | `FilterExpression`, `ExpressionAttributeValues` (plain values, `{":s": open}`), and `ExpressionAttributeNames`. |
| | `index`, `projection`, `consistent_read` | A secondary index, a `ProjectionExpression`, and strongly consistent reads. |
| `cosmos` | `query`, `parameters` | Cosmos DB SQL (default `SELECT * FROM c`) and its parameters (`{min: 10}` binds `@min`). |
| | `partition_key` | Read one logical partition instead of querying across partitions. |
| | `include_system_properties` | Keep `_rid`, `_self`, `_etag`, `_attachments`, and `_ts` (dropped by default). |
| all | `limit` | Read at most this many documents. |
| all | `batch_size` | Documents per round trip for this dataset. |

The dataset `filter` (outside `options`) is still SQL, applied by DuckDB to the rows dvf read. Prefer native filters: they reduce what is read.

## From documents to rows

Without `fields`, every nested document is flattened into one column per leaf, named by its dot path:

```json
{"_id": {"$oid": "65f1..."}, "order_id": 1001, "customer": {"name": "Ada", "address": {"city": "Leeds"}}, "tags": ["b", "a"]}
```

becomes the columns `_id`, `order_id`, `customer.name`, `customer.address.city`, and `tags` (an array, as canonical JSON `["b","a"]`).

With `fields`, you choose the columns and their names:

```yaml
options:
  fields:
    order_id: order_id
    customer_name: customer.name
    city: customer.address.city
    first_sku: lines.0.sku          # a numeric part indexes an array
    skus: lines.sku                 # a path through an array collects every element's value
```

| Option | Default | Meaning |
|---|---|---|
| `fields` | all fields | Output column to dot path. Documents without the path give NULL. A nested document mapped as a whole becomes canonical JSON. |
| `flatten` | `true` | Flatten nested documents; when `false`, they become canonical JSON text. |
| `max_depth` | 16 | Nesting depth beyond which documents become canonical JSON text. |
| `separator` | `.` | Joins the parts of flattened column names (for example `_` gives `customer_name`). |
| `types` | none | Force columns to `string`, `integer`, `decimal`, `double`, `boolean`, `timestamp`, `date`, or `json`, for example ISO date strings in Cosmos DB or DynamoDB to `timestamp`. A value that cannot be converted is an error that names the row and column. |
| `primary_key` | the source's key | Columns that identify a row, used by `keys: auto`. |
| `sample_size` | 1,000 | Documents sampled by schema inference. |

Column names with dots are quoted automatically in the SQL dvf generates. In your own SQL (a dataset `filter`, a `where`, a `source_expr`), quote them: `"customer.name" = 'Ada'`.

### Arrays

An array that stays in a row becomes a value according to its strategy. Set it per path in `arrays`, or for every other array with `default_array`:

| Strategy | Value |
|---|---|
| `json` (default) | Canonical JSON of the array. |
| `hash` | SHA-256 of the canonical JSON. Order matters. |
| `unordered_hash` | SHA-256 of the canonical JSON with the elements sorted, for arrays whose order carries no meaning (tags, roles). |
| `count` | The number of elements, for example to compare with a child row count. |
| `index` | One column per position (`tags.0`, `tags.1`, ...), up to `index_limit` (default 20). Longer arrays are truncated, and the column comment and the log say how many. |
| `explode` | One row per element; the same as `unwind`. |

```yaml
options:
  arrays: { tags: unordered_hash, phones: index, history: count }
  default_array: hash
```

Canonical JSON sorts object keys, removes insignificant whitespace, normalizes numbers (`1`, `1.0`, and `NumberDecimal("1.00")` are all `1`), writes timestamps in UTC (`2024-03-05T10:00:00.000000Z`), binary values as lower-case hexadecimal, and ObjectId and UUID values as their canonical strings. Two arrays with the same content therefore get the same text and hash in every connector.

### Unwinding arrays

`unwind` produces one row per element of an array, like MongoDB's `$unwind`. Each row has the kept parent fields, the element's position, and the element's fields:

```yaml
options:
  unwind:
    path: lines
    keep: [order_id]       # parent fields copied to every row
    index: line_index      # the element's 0-based position (default: lines_index)
```

`{"order_id": 1001, "lines": [{"line_no": 1, "sku": "A"}, {"line_no": 2, "sku": "B"}]}` becomes two rows with the columns `order_id`, `line_index`, `line_no`, and `sku`.

| Option | Default | Meaning |
|---|---|---|
| `path` | required | Dot path of the array. `unwind: lines` is short for `unwind: {path: lines}`. |
| `keep` | the document's identity | Parent fields to copy: `_id` for MongoDB, `id` for Cosmos DB, the key attributes for DynamoDB, the primary key for Cassandra. |
| `index` | `<path>_index` | Name of the position column. Exclude it with `columns.exclude` when the other side has no such column. |
| `prefix` | none | Prepended to element column names, for example `line_` when an element field has the same name as a kept parent field. |
| `preserve_empty` | `false` | Emit one row with NULL element columns for documents whose array is missing, NULL, or empty. |

With `fields`, the unwound array's path refers to the element, as after `$unwind`: `lines.sku` is the current line's `sku`. A value that is not an array is unwound as one element without a position, as MongoDB does. Only one array can be unwound per dataset; compare other arrays in their own table. When rows are unwound and `keep` is set, the primary key is the kept fields plus the position.

## Types

Values keep their source system's type names in schema checks (`objectId`, `long`, `N`, `timeuuid`, ...), so a type difference is reported in the terms you know. Each column then gets one type, chosen from every value dvf read:

- integers (`int` and `long`, DynamoDB whole numbers) become 64-bit integers;
- decimals take the largest scale seen (`decimal(34, s)` for MongoDB's Decimal128); integers mixed with decimals become decimals;
- any mix that includes floating-point values becomes `double`, with a note in the column comment;
- timestamps become UTC timestamps (BSON dates have millisecond precision); a mix of dates and timestamps becomes timestamps;
- ObjectId and UUID values become their canonical strings, with canonical types `string(24)` and `UUID`;
- nested documents and arrays left in a column become canonical JSON (canonical type `JSON`);
- any other mix becomes canonical text, and the native type shows every type seen (`int|string`) with a note.

Canonical text is the same text the comparison engine uses, so a key stored as the number `1007` in one document and the string `"1007"` in another still pairs with the relational `1007`.

| BSON type | Native name | Staged as |
|---|---|---|
| ObjectId | `objectId` | 24-character lower-case hexadecimal string |
| 32-bit, 64-bit integer | `int`, `long` | 64-bit integer |
| double | `double` | double |
| Decimal128 | `decimal` | decimal; NaN and infinities as text |
| date | `date` | UTC timestamp; dates outside years 1 to 9999 as text |
| UUID (binary subtype 4) | `uuid` | canonical UUID string |
| other binary | `binData` | binary |
| string, boolean | `string`, `bool` | string, boolean |
| document, array | `object`, `array` | flattened or an array strategy |
| timestamp, regex, JavaScript | `timestamp`, `regex`, `javascript` | text: `Timestamp(<seconds>, <increment>)`, `/<pattern>/<flags>`, the code |
| DBRef | `dbRef` | a document with `$ref`, `$id`, and `$db` |
| MinKey, MaxKey | `minKey`, `maxKey` | text |

DynamoDB values keep their attribute types (`S`, `N`, `B`, `BOOL`, `NULL`, `M`, `L`, `SS`, `NS`, `BS`); sets are sorted. Cosmos DB values use JSON's names (`string`, `number`, `boolean`, `null`, `object`, `array`). Cassandra columns keep their CQL types (`int`, `timeuuid`, `list<frozen<item>>`), and their canonical types follow them.

## Checking a document schema

The `document_schema` check samples the documents of the checked dataset (its native filter applies, its flattening does not) and compares what it finds with your expectations (SCH-13):

```yaml
checks:
  - type: document_schema
    side: target
    severity: warning
    sample_size: 5000
    fields:
      order_id: integer
      customer.name: string
      total: { type: decimal, nullable: false }
      email: { type: string, min_presence: 95 }
      lines: array
      lines[].qty: integer
      lines[].price: [decimal, double]
    max_types: 1
    allow_extra_fields: false
    ignore: ["audit.*", "_id"]
```

Paths use dots for nested documents and `[]` for array elements (`lines[]` is the elements themselves). Presence is a percentage of the sampled documents, or of the array's elements for paths inside an array.

| Setting | Default | Meaning |
|---|---|---|
| `fields` | none | Expected fields. A type name or a list is short for `{type: ...}`. |
| `fields.<path>.type` | any | Allowed types: native names (`objectId`, `int`, `long`, `double`, `decimal`, `string`, `date`, `bool`, `binData`, `uuid`, `object`, `array`, or `S`, `N`, ...) or the aliases `integer`, `number`, `float`, `decimal`, `string`, `boolean`, `timestamp`, `date`, `time`, `binary`, `uuid`, `objectid`, `object`, `array`. `int` means a 32-bit BSON integer; use `integer` for any integer. |
| `fields.<path>.min_presence` | 100 | Lowest accepted presence in percent; 0 makes a field optional. |
| `fields.<path>.nullable` | `true` | Whether NULL values are accepted. |
| `fields.<path>.max_types` | the check's | Most distinct non-NULL types for this field. |
| `sample_size` | the dataset's (1,000) | Documents sampled. |
| `sampling` | `first` | `first` reads the first documents in the source's natural order, so results repeat; `random` uses MongoDB's `$sample` (other systems fall back to `first` and say so). |
| `max_types` | unlimited | Most distinct non-NULL types per field, for every field not ignored. |
| `allow_extra_fields` | `true` | When `false`, fields that are not listed, not parents of listed fields, and not ignored are problems. |
| `ignore` | none | Glob patterns of paths skipped by `max_types` and `allow_extra_fields`. |

Each problem is a sample row: the field, the problem (`missing`, `low presence`, `unexpected type`, `null values`, `mixed types`, `unexpected field`), what was observed (`double in 1 of 10 values`), and what was expected. The result's details hold the whole inferred schema: per path, presence, NULL share, type distribution, and array lengths. The check also works on other connectors (for example nested Parquet or JSON files through DuckDB), sampling their first rows. It does not accept `where`; filter with the dataset's options.

Profiling code can call the same inference directly:

```python
from dvf.packs.nosql.inference import infer_document_schema

report = infer_document_schema(connector, DatasetSpec(table="orders"), sample_size=1000)
for field in report.fields:
    print(field.path, field.presence_pct, dict(field.types))
report.to_dict()  # JSON-ready
```

## Reconciling embedded arrays with child tables

A migration from PostgreSQL to MongoDB often turns a parent and a child table (`orders`, `order_lines`) into one document with an embedded array (`orders.lines`). Compare the two shapes in two tables: the headers, and the child rows.

```yaml
suites:
  - name: orders
    source: pg
    target: mongo
    tables:
      - name: orders                       # headers: one row per document
        source: { table: orders }
        target:
          table: orders
          options:
            fields: { order_id: order_id, customer_name: customer.name, status: status, total: total }
        keys: [order_id]
        comparison: { strategies: [row_count, full_diff] }

      - name: order_lines                  # child rows: the array unwound
        source: { table: order_lines }
        target:
          table: orders
          options:
            unwind: { path: lines, keep: [order_id], index: line_index }
        keys: [order_id, line_no]
        columns: { exclude: [line_index] }
        comparison:
          strategies: [nested]
          group_by: [order_id]             # the parent key
          on_mismatch: full_diff
```

The `nested` strategy reconciles child rows per parent key. Each database computes, for every value of `group_by`:

- the number of child rows;
- the sum of every integer and decimal child column (exact, as canonical text), when `comparison.aggregates` includes `sum` (the default);
- the count and sum of floating-point child columns, compared within the column's tolerance;
- an order-independent hash of the canonical child rows (two independent sums of 32-bit prefixes of each row's hash, so reordering the array changes nothing and duplicates never cancel out).

Only one small summary row per parent leaves each database; dvf compares them in a local DuckDB database, so memory stays bounded. The result names every parent key that differs and why, in this order of precedence:

| Issue | Meaning |
|---|---|
| `missing in target` / `extra in target` | The parent has child rows on one side only. |
| `child rows differ` | Different numbers of child rows, with the totals that changed. |
| `totals differ` | Same number of child rows, different sums (for example a changed quantity). |
| `content differs` | Same count and totals, different child rows (for example swapped SKUs). |

For the example in `examples/mongodb_to_postgres`, the message reads:

```text
3 of 10 parent keys differ (2 child rows differ, 1 totals differ): order_id=1002: child rows 3 vs 2;
sum(qty) 10 vs 8; sum(price) 19.20 vs 11.67 | order_id=1003: sum(qty) 9 vs 10 | order_id=1004:
child rows 2 vs 3; sum(qty) 9 vs 10; sum(price) 15.95 vs 16.95
```

The samples list each differing parent with its issue, both child counts, and the details; the metrics hold the child counts and sums per parent, and an extract file holds every differing parent (up to `max_mismatch_rows`). Because `nested` failed, `on_mismatch: full_diff` then pairs the child rows by `(order_id, line_no)` and reports the missing line of 1002, the extra line of 1004, and the changed quantity of 1003, with side-by-side values.

To run the example:

```bash
cd examples/mongodb_to_postgres
set DVF_EXAMPLE_MONGODB_URL=mongodb://localhost:27017
set DVF_EXAMPLE_POSTGRES_URL=postgresql://user:password@localhost:5432/dvf
uv run python load_data.py
dvf run dvf.yaml
uv run python load_data.py --drop
```

(On macOS and Linux, use `export` instead of `set`.)

Things to know:

- **Parents without children** appear on neither side of the child comparison (an order with no lines has no `order_lines` rows and no unwound rows). The header table catches missing parents. To count children on the header table, use `arrays: {lines: count}` on the document side and a query with `COUNT(*)` per order on the relational side.
- **Position and line numbers.** The array position is rarely the relational line number; exclude the `index` column and pair on the child table's own key (`line_no`), or name a position column with `index` and map it when positions are meaningful.
- **Decimals** reconcile exactly: Decimal128 `19.990` and `numeric(10,2)` `19.99` compare at the larger scale. A document that stores a double where others store Decimal128 turns the column into `double` (compared within tolerance), and `schema.type_compatibility` and `document_schema` report the type.
- `nested` works with any connectors, not only NoSQL ones: any two child-grain datasets with a parent key, for example a child table against a denormalized extract.

## How staging works

NoSQL connectors cannot run SQL, so dvf stages each dataset the first time a check or comparison needs it: it reads the documents with the native read, shapes them into rows, writes the rows to a temporary Parquet file, and exposes it as a view in an embedded DuckDB database. Rows are spooled to a temporary file while their types are worked out, so memory stays bounded by a batch. Datasets with identical specifications (table and options) share one staged file, also across parallel tables. The files are removed when the run ends; `defaults.execution.temp_dir` chooses their folder.

## Limits

- Every staged dataset is read in full (after its native filter) once per run. Use native filters, `projection`, and `limit` to bound large collections; checksum strategies (`hash_bucket`, `bisect`) do not reduce reads from NoSQL sources.
- Shaping runs in Python: expect roughly 10,000 to 30,000 documents per second, depending on their size.
- `as_of` time travel is not supported. For live sources, compare during a quiet period, or restrict both sides with native filters on a change timestamp.
- Random sampling (`sampling: random`) is available for MongoDB only. DynamoDB parallel scans are not used.
- Canonical JSON is computed in Python, so JSON columns compare reliably only between NoSQL datasets. Against a relational JSON column (PostgreSQL `jsonb`), flatten the fields you need instead.
- Decimals wider than 38 digits are compared as canonical text.
- Cassandra, DynamoDB, and Cosmos DB are beta: their tests use fake drivers (DynamoDB has also run against DynamoDB Local).
