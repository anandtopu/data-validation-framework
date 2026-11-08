# Files and lakehouse tables

A `files` connection reads files and lakehouse tables from a local folder or object storage, and lets you validate the files themselves: whether they arrived, how many there are, their names, sizes, checksums, age, header and trailer control records, schema, and unreadable records.

Every dataset is queried with an embedded DuckDB database, so every check, comparison strategy, and engine works on files the same way it works on a database table.

## Formats

| Format | `format` | Detected from | Read by | Time travel |
|---|---|---|---|---|
| CSV, TSV | `csv`, `tsv` | `.csv`, `.txt`, `.tsv` | DuckDB `read_csv` | no |
| JSON, NDJSON | `json`, `ndjson` | `.json`, `.jsonl`, `.ndjson` | DuckDB `read_json` | no |
| Parquet | `parquet` | `.parquet`, `.pq` | DuckDB `read_parquet` | no |
| ORC | `orc` | `.orc` | PyArrow dataset | no |
| Delta Lake | `delta` | a folder with `_delta_log/` | `deltalake`, as a PyArrow dataset | version or timestamp |
| Apache Iceberg | `iceberg` | a folder with `metadata/*.metadata.json`, or a `.metadata.json` file | `pyiceberg` | snapshot ID or timestamp |
| Avro | `avro` | `.avro` | `fastavro`, converted to Parquet | no |
| Excel | `excel` | `.xlsx`, `.xlsm` | `openpyxl`, converted to Parquet | no |
| Fixed width | `fixed_width` | never: set `format` | converted to Parquet | no |
| XML | `xml` | `.xml` | the standard library, converted to Parquet | no |

The format comes from the dataset's `options.format`, then the connection's `format`, then the path. Compression suffixes (`.gz`, `.zst`, `.bz2`, `.xz`) are ignored when detecting it. Hudi tables are read through Spark, not through this connection.

No format downloads anything at run time: DuckDB extensions are not needed.

## Connecting

```yaml
connections:
  landing:
    type: files
    path: ./landing              # relative to the configuration file, or absolute
  lake:
    type: files
    path: s3://acme-lake/curated
    storage_options:             # passed to fsspec (and to deltalake for Delta tables)
      key: ${secret:env:AWS_ACCESS_KEY_ID}
      secret: ${secret:env:AWS_SECRET_ACCESS_KEY}
  legacy:
    type: files
    path: ./exports
    format: csv                  # default format of the connection's datasets
    options: {delim: ";"}        # reader options applied to every dataset
```

| Setting | Meaning |
|---|---|
| `path` | Base folder or URL. Dataset paths are relative to it. |
| `format` | Default format; detected per dataset when omitted. |
| `options` | Reader options for every dataset of the connection. Dataset `options` override them. For non-text formats, connection options that the format does not know are ignored. `options.extensions` lists DuckDB extensions to load (see below). |
| `storage_options` | Options for URLs: fsspec options (listing, checksums, converted formats, DuckDB reads) and deltalake storage options (Delta tables). Keep credentials in secret references. |

**URLs.** A URL such as `s3://`, `abfs://`, or `gs://` is read through [fsspec](https://filesystem-spec.readthedocs.io), which dvf registers as a DuckDB file system. Install the fsspec implementation for the storage (`s3fs`, `adlfs`, or `gcsfs`); none is installed by default. Alternatively, list a DuckDB extension, such as `options: {extensions: [httpfs]}`. DuckDB then reads the URLs itself, but downloads the extension on first use. File-level checks on URLs always use fsspec.

## Datasets

A dataset's `path` (or `table`) is:

- a file: `orders.csv`;
- a glob: `incoming/orders_*.csv`, or `sales/**/*.parquet` (`**` matches folders recursively);
- a folder: its data files are read. Names starting with `_` or `.` (`_SUCCESS`, `_delta_log/`, `.part-0.crc`) and `.crc` files are skipped, and only files of the dataset's format are used. A folder whose path holds `key=value` parts is read with Hive partitioning, so `year=2026` becomes a `year` column;
- a Delta or Iceberg table folder, or an Iceberg metadata file;
- with an Iceberg catalog, a table identifier such as `sales.orders`.

```yaml
tables:
  - name: orders
    source:
      path: incoming/orders_${run.date}.csv
      options: {delim: "|", header: true}
      filter: "status <> 'TEST'"
```

`${run.date}` and the other run placeholders are replaced in paths.

### CSV, TSV, JSON, and NDJSON

Dataset `options` are passed to DuckDB's [`read_csv`](https://duckdb.org/docs/data/csv/overview) or [`read_json`](https://duckdb.org/docs/data/json/overview) unchanged (`delimiter` and `separator` are accepted for `delim`), for example `header`, `delim`, `quote`, `escape`, `skip`, `columns`, `types`, `dateformat`, `ignore_errors`, `union_by_name`, `filename`, or `hive_partitioning`.

dvf adds:

| Option | Meaning |
|---|---|
| `skip_footer` | Lines to drop at the end of every file, such as a trailer control record. DuckDB's `skip` drops lines at the start. |
| `encoding` | Any Python encoding (`cp1252`, `cp037`, `iso-8859-15`, ...). DuckDB reads `utf-8`, `utf-16`, and `latin-1` itself. |

Gzip (`.gz`) and Zstandard (`.zst`) files are read directly. For bz2 (`.bz2`) and xz (`.xz`) files, other encodings, and `skip_footer`, dvf first copies the files to UTF-8 text in the connection's temporary folder. For bz2 and xz, the path or glob must end with the suffix.

### Parquet

Parquet is read by DuckDB, with projection and filter pushdown. Reader options such as `union_by_name` and `hive_partitioning` are passed through.

### ORC

ORC files, globs, and folders are opened as a PyArrow dataset. DuckDB pushes column selection and filters into the scan, so memory stays bounded.

| Option | Meaning |
|---|---|
| `hive_partitioning` | Read `key=value` folder names as columns. Detected from the paths when omitted. |

On Windows, ORC timestamp columns need a time zone database. dvf points the ORC reader at the `tzdata` package's files when `TZDIR` is not set.

### Delta Lake

```yaml
source: {path: lake/orders}                          # current version
source: {path: lake/orders, as_of: "12"}             # version 12
source: {path: lake/orders, as_of: "2026-01-31T23:59:59Z"}   # latest version at that time
```

The table is opened with `deltalake` and scanned as a PyArrow dataset. Partition columns are ordinary columns, and filters on them prune files. `deltalake` reads local paths, `s3://`, `az://`, `abfss://`, and `gs://` with the connection's `storage_options`.

Tables that use deletion vectors cannot be scanned as PyArrow datasets. dvf reads them through deltalake's query engine instead and stages the rows as Parquet in the connection's temporary folder, so deleted rows stay deleted.

### Apache Iceberg

From a table folder (newest metadata, or `metadata/version-hint.text`) or a metadata file:

```yaml
source: {path: warehouse/sales/orders}
source: {path: warehouse/sales/orders/metadata/00004-6c1e.metadata.json}
```

Through a catalog, with the table identifier as `table`:

```yaml
connections:
  iceberg:
    type: files
    path: .
    format: iceberg
    options:
      catalog:
        name: prod
        type: rest                           # rest, glue, sql, hive, dynamodb, ...
        uri: https://catalog.example.com
        token: ${secret:env:ICEBERG_TOKEN}
suites:
  - name: lake
    source: iceberg
    tables:
      - name: orders
        source: {table: sales.orders, as_of: "2026-01-31T00:00:00Z"}
```

| Option | Meaning |
|---|---|
| `catalog` | pyiceberg catalog properties, passed to `pyiceberg.catalog.load_catalog`. `name` is optional. Each catalog type needs its pyiceberg extra; the `sql` catalog needs SQLAlchemy (`pip install 'pyiceberg[sql-sqlite]'`). |
| `properties` | File IO properties for a table read from its metadata file (`s3.endpoint`, `s3.region`, ...). |
| `row_filter` | A pyiceberg row filter pushed into the scan, such as `"region = 'eu'"`. |

`as_of` is a snapshot ID (digits) or a timestamp: the snapshot current at that time.

When the snapshot's data files are local Parquet files without delete files, and the table never changed its schema, DuckDB scans them in place. Otherwise (delete files, schema evolution, remote files, or a `row_filter`), pyiceberg reads the scan and dvf stages it as Parquet.

### Avro

Avro types map to these types:

| Avro | Read as |
|---|---|
| `int`, `long` | INTEGER, BIGINT |
| `float`, `double` | FLOAT, DOUBLE |
| `bytes`, `fixed` | BLOB |
| `string`, `enum`, `uuid` | VARCHAR |
| `decimal` | DECIMAL(p, s) |
| `date` | DATE |
| `time-millis`, `time-micros` | TIME |
| `timestamp-millis`, `-micros`, `-nanos` | TIMESTAMP WITH TIME ZONE (UTC) |
| `local-timestamp-*` | TIMESTAMP |
| `array`, `map`, `record` | LIST, MAP, STRUCT |
| `["null", T]` | T |
| other unions | VARCHAR (JSON text for non-string values) |

Avro files compressed with `null`, `deflate`, `bzip2`, `xz`, or `zstandard` are read; `snappy` and `lz4` need `cramjam` or `lz4` installed.

### Excel

```yaml
source:
  path: finance/trial_balance.xlsx
  options:
    sheet: TB                  # name, or position from 0; default: the first sheet
    cell_range: B4:H500        # default: the used area
    header_row: 4              # sheet row holding the column names
    types: {amount: DECIMAL(18,2)}
```

| Option | Meaning |
|---|---|
| `sheet` | Worksheet name, or its position starting at 0. |
| `cell_range` | Cells to read, such as `B3:F200` or `A:D`. |
| `header_row` | Sheet row (1-based) with the column names; data starts below it. Defaults to the first row of the range. `0` means no header: columns are named by their letters. |
| `skip_empty_rows` | Ignore rows whose cells are all empty (default `true`). |
| `types`, `ignore_errors` | See [Typed columns](#typed-columns). |

Cached values are read, not formulas. A column of whole numbers is BIGINT, and one mixing whole and decimal numbers is DOUBLE. Dates at midnight in cells without a time format are DATE, other dates are TIMESTAMP, and booleans are BOOLEAN. A column mixing kinds (text and numbers) is VARCHAR. Cells holding Excel errors (`#N/A`, `#DIV/0!`, ...) are read as NULL and counted by `bad_records`.

### Fixed width

```yaml
source:
  path: mainframe/ORDERS.DAT
  options:
    format: fixed_width
    encoding: cp037            # EBCDIC
    record_length: 120         # records without line breaks
    skip_header: 1
    columns:
      - {name: order_id, start: 1, width: 10, type: BIGINT}
      - {name: customer, start: 11, width: 30}
      - {name: amount, start: 41, width: 12, type: DECIMAL(12,2)}
```

| Option | Meaning |
|---|---|
| `columns` | The layout: `name`, `start` (first character, counting from 1), `width`, and an optional `type`. |
| `widths`, `names` | The layout as consecutive widths, an alternative to `columns`. Names default to `column1`, `column2`, ... |
| `encoding` | Text encoding (default `utf-8`). Positions count characters after decoding. |
| `trim` | `both` (default), `left`, `right`, or `none`. |
| `empty_as_null` | Empty values become NULL (default `true`). |
| `skip_header`, `skip_footer` | Lines skipped at the start and end of every file. |
| `skip_pattern` | Regular expression: matching lines are skipped (control records, comments). |
| `record_length` | Read records of this many characters instead of lines. |
| `strict` | Records whose length differs from the layout are errors, or are skipped with `ignore_errors`. Otherwise short records are padded and extra characters are ignored. |
| `types`, `ignore_errors` | See [Typed columns](#typed-columns). |

Values are text unless a type is given. Implied decimals (`0000012345` meaning 123.45) are not scaled; divide in a `query` dataset or compare with a `source_expr` mapping.

### XML

```yaml
source:
  path: exports/orders.xml.gz
  options:
    row_path: orders/order
    fields:
      order_id: "@id"
      customer: customer/name
      customer_id: customer/@id
      amount: amount
    types: {order_id: BIGINT, amount: DECIMAL(12,2)}
```

| Option | Meaning |
|---|---|
| `row_path` | Element path of one row. It matches the end of an element's path; start it with `/` to match from the root element. |
| `fields` | Columns and their paths relative to the row: `@attr`, `child`, `child/grandchild`, `child/@attr`, or `.` (the row's own text). When omitted, columns are the row's attributes and the text of its direct children. |
| `empty_as_null` | Empty values become NULL (default `true`). |
| `types`, `ignore_errors` | See [Typed columns](#typed-columns). |

Namespaces are ignored: names match on their local part. Files are parsed incrementally, and each row is discarded once read. External entities are never fetched. Repeated child elements contribute their first occurrence.

### Typed columns

Avro, Excel, fixed-width, and XML datasets accept `types`, a mapping of column to DuckDB type, applied with `CAST` when the data is read. A value that does not convert fails the query and names the value. With `ignore_errors: true`, such values become NULL (`TRY_CAST`) instead, and `bad_records` counts them.

## Time travel

`as_of` works on Delta and Iceberg datasets of a `files` connection. Other formats reject it with an error. Quote numbers in YAML (`as_of: "12"`), because `as_of` is a string. A timestamp without a time zone is UTC.

A run can compare two versions of the same table, or a lakehouse table with its source:

```yaml
tables:
  - name: orders_rehearsal
    source: {path: lake/orders, as_of: "41"}
    target: {path: lake/orders, as_of: "42"}
    keys: [order_id]
```

## How data is read

Nothing is read when a dataset is resolved. A dataset is prepared the first time a query reads it:

- ORC, Delta, and Iceberg are opened (metadata only) and registered in DuckDB;
- Avro, Excel, fixed-width, and XML files are converted to Parquet, one file per input file, batch by batch, so memory stays bounded;
- text that DuckDB cannot read directly is copied to UTF-8.

Converted files live in a temporary folder of the connection (`dvf-files-*` in the system temporary folder). Sessions that parallel tables fork from the connection share it, so each dataset is converted once per run, and the folder is deleted when the connection closes. Converted data is read with `read_parquet` and is as fast as Parquet. `dvf explain` and schema checks read each dataset's schema, so they prepare datasets too.

## File-level checks

These check types run on datasets of a `files` connection. In comparison suites, `side` chooses the dataset (`target` by default, `source`, or `both`). Most of them take `path`, a file, folder, or glob relative to the connection's folder (or a URL). It defaults to the dataset's files; for Delta and Iceberg datasets, those are the data files of the version read.

| Type | Dimension | Passes when |
|---|---|---|
| `file_exists` | completeness | at least one file matches, or exactly `count` files |
| `file_count` | volume | the number of files is within `min` and `max` |
| `file_name_pattern` | validity | every file name matches `pattern` (or a share given by `mostly`) |
| `file_size` | volume | every file is within `min_bytes` and `max_bytes`, and none is empty unless `allow_empty` |
| `file_checksum` | reconciliation | every file's MD5 or SHA-256 matches `expected` values, sidecars, a manifest, or the files of another location |
| `file_freshness` | timeliness | the newest file (or, with `which: oldest`, every file) was modified within `max_age` |
| `control_totals` | reconciliation | header or trailer control records match the data's record count and column sums (REC-09) |
| `schema_on_read` | schema | the columns and types read match an expected list or schema file |
| `bad_records` | validity | unreadable or malformed records do not exceed `max_count` (none by default) |

Results list the files they looked at in `details` (up to 50), show offending files as samples, and say which files failed and why.

File checks run even when the dataset's path matches no file, so `file_exists` and `file_count` fail when nothing has arrived. The dataset's other checks are skipped in that case.

### Existence, counts, names, sizes, and freshness

```yaml
checks:
  - {type: file_exists}                                  # the dataset's files
  - {type: file_exists, path: "incoming/orders_${run.date}*.csv", count: 4}
  - {type: file_count, path: incoming/*.csv, min: 1, max: 24}
  - {type: file_name_pattern, pattern: 'orders_\d{8}_\d{2}\.csv(\.gz)?'}
  - {type: file_name_pattern, pattern: '\d{4}/\d{2}/.*', match_on: path}
  - {type: file_size, min_bytes: 100, max_bytes: 2000000000}
  - {type: file_size, allow_empty: true, path: "markers/*"}
  - {type: file_freshness, max_age: 26h}
  - {type: file_freshness, max_age: 7d, which: oldest}
```

| Check | Options |
|---|---|
| `file_exists` | `path`; `count` (exact number of files). |
| `file_count` | `path`; `min`, `max`. |
| `file_name_pattern` | `path`; `pattern` (Python regular expression); `match_on`: `name` (default) or `path` (relative to the connection's folder); `full_match` (default `true`); `mostly`. |
| `file_size` | `path`; `min_bytes`, `max_bytes`; `allow_empty` (default `false`); `mostly`. |
| `file_freshness` | `path`; `max_age` (`30m`, `26h`, `2d`); `which`: `newest` (default) or `oldest`. Uses the storage's modification times. |

### Checksums

Set exactly one of `expected`, `sidecar`, `manifest`, or `compare_to`:

```yaml
checks:
  - type: file_checksum
    algorithm: md5                          # or sha256 (default)
    expected: {"orders_20260131.csv": 9e107d9d372bb6826bd81d3542a419d6}
  - type: file_checksum
    sidecar: true                           # orders.csv.sha256 next to orders.csv
  - type: file_checksum
    sidecar: .sha256sum                     # another sidecar suffix
  - type: file_checksum
    manifest: incoming/MANIFEST.sha256      # "<digest>  <file>" lines, or CSV "file,digest"
  - type: file_checksum                     # file-vs-file: every file identical in both places
    compare_to: {connection: archive, path: "2026-01-31/*.csv"}
    match_by: name                          # or relative_path
```

| Option | Meaning |
|---|---|
| `algorithm` | `sha256` (default) or `md5`. Files are hashed as stored: a compressed file's digest is that of its compressed bytes. |
| `expected` | Digests by file name, path relative to the connection's folder, or glob. |
| `sidecar` | `true` reads `<file>.sha256` or `<file>.md5`; a string gives the suffix. Sidecar files are not checked themselves. |
| `manifest` | A file relative to the connection's folder, in the `sha256sum`/`md5sum` format or as CSV rows `file,digest` (a header row is skipped). |
| `compare_to` | `connection` (defaults to the checked one) and `path` of reference files. Files are paired by `match_by`; a file missing on either side, or with a different digest, fails the check. |

A file with no expected digest fails the check. `compare_to` is the file-vs-file reconciliation of REC-09: it proves a transfer or archive copied the bytes unchanged. To compare the content of two files whose bytes may legitimately differ (another compression, row order, or encoding), compare them as datasets with keys instead.

### Control records

Header and trailer records carry record counts and control totals. `control_totals` reads them, adds them up across the matched files, and compares them with `COUNT(*)` and `SUM(column)` of the dataset (with the check's `where`):

```yaml
- name: orders
  source:
    path: incoming/orders_*.csv
    options: {delim: "|", skip: 1, skip_footer: 1, header: true}   # keep control lines out of the data
  checks:
    - type: control_totals
      trailer:
        pattern: '^T\|(?P<count>\d+)\|(?P<amount_total>-?[\d.]+)$'
      record_count: count
      sums: {amount: amount_total}
    - type: control_totals                  # fixed positions in a fixed-width header
      name: header
      header:
        line: 1
        fields:
          count: {start: 4, width: 9}
          amount_total: {start: 13, width: 15, scale: 2}   # implied decimals
      record_count: count
      sums: {amount: amount_total}
      tolerance: 0.01
    - type: control_totals                  # a separate control file
      name: ctl
      path: "incoming/orders_${run.date}.ctl"
      header: {pattern: 'records=(?P<n>\d+)'}
      record_count: n
```

| Option | Meaning |
|---|---|
| `header`, `trailer` | Control records: `line` (1 is the first non-empty line, -1 the last; defaults 1 and -1), and either `pattern` (a regular expression with named groups) or `fields` (`start`, `width`, and `scale` for implied decimals). |
| `record_count` | The control value holding the number of data records. |
| `sums` | Data column to the control value holding its sum. |
| `tolerance` | Largest absolute difference accepted for sums (default 0). |
| `path` | Where the control records are, when not in the dataset's files (a `.ctl` or `.trg` file). |
| `encoding` | Encoding of the files holding the records (default `utf-8`). |

Control values may end with a sign (`150-`) and may hold thousands separators. Exclude header and trailer lines from the dataset with `skip`, `skip_footer` (CSV), `skip_header`, `skip_footer` or `skip_pattern` (fixed width), or a `filter`; otherwise they are counted as data.

### Schema on read

```yaml
checks:
  - type: schema_on_read
    columns: {order_id: BIGINT, customer: VARCHAR, amount: DECIMAL(12,2), booked: DATE}
  - type: schema_on_read
    schema_file: contracts/orders.schema.yaml   # relative to the configuration file
    allow_extra_columns: true
    type_match: exact
```

| Option | Meaning |
|---|---|
| `columns` | Expected columns: a mapping of name to type, a list of `{name, type}`, or a list of names (any type). |
| `schema_file` | A YAML or JSON file with the same forms, optionally under a `columns` key. |
| `allow_extra_columns` | Accept columns that are not expected (default `false`). |
| `ordered` | Require the expected columns in the given order (default `false`). |
| `case_sensitive` | Compare names case-sensitively (default `false`). |
| `type_match` | `logical` (default) compares type families: any integer width, decimal precision, or string length matches. `exact` also compares widths, precision, scale, and lengths. |

Types are SQL type names parsed into dvf's canonical types. The schema compared is the one the reader produced, so CSV columns have the types DuckDB inferred unless the dataset gives `columns` or `types`. `schema_on_read` works on any dataset, not only files.

### Bad records

```yaml
- name: orders
  source:
    path: incoming/orders_*.csv
    options: {header: true, columns: {id: INTEGER, amount: "DECIMAL(10,2)"}, ignore_errors: true}
  checks:
    - type: bad_records
      max_count: 10                     # or mostly: 0.999
```

What counts as a bad record depends on the format:

| Format | Bad records |
|---|---|
| CSV, TSV | Rows DuckDB rejects with the dataset's options: cast errors, too many or missing columns, unquoted values, invalid encoding, oversized lines (DuckDB's reject tables). |
| NDJSON | Lines that are not JSON objects. |
| Fixed width | Records shorter than the layout (or of another length with `strict`), and values that do not convert to their `type`. |
| Excel | Cells holding error values, and values that do not convert to their `type`. |
| Avro, XML | Values that do not convert to their `type`, and files that cannot be read. |
| Parquet, ORC, JSON, Delta, Iceberg | Files that cannot be read in full (corrupt files). |

Samples show the file, line number (where the format has lines), column, error type, message, and the raw record. When [extracts](debugging.md#extracts-and-repro-files) are enabled, every bad record up to `max_rows_per_check` is written to the check's extract file. Values are masked like other samples, and strict privacy mode keeps them out of results and extracts.

Set `ignore_errors: true` in the dataset's options so other checks read the good rows; `bad_records` still finds and reports the bad ones. With `ignore_errors`, DuckDB drops a row with a conversion error only when a query reads the failing column, so `COUNT(*)` can include rows that `SUM(amount)` skips. Declare `columns` or `types` for the columns you need, and rely on `bad_records` for the count of bad rows.

## Limits

- fsspec implementations for cloud storage (`s3fs`, `adlfs`, `gcsfs`) are not installed by default. The Iceberg REST, Glue, Hive, and SQL catalogs and object storage have been verified with unit tests only: local files, local Delta and Iceberg tables, and fsspec's in-memory file system were tested end to end.
- Hudi tables are read through a Spark connection.
- Whole-file Snappy compression (`.snappy` text files) is not supported. Parquet, ORC, and Avro files with internal Snappy compression are (Avro needs `cramjam`).
- Excel `.xls` (BIFF) workbooks are not supported; save them as `.xlsx`.
- XML attributes and children are flattened by path; nested repeating groups need one dataset per level.
- Iceberg tables with delete files or schema changes, and Delta tables with deletion vectors, are copied to Parquet before they are read, which takes time and disk space proportional to the data read.
- Checksums read every byte of every file, so they take as long as reading the files.
