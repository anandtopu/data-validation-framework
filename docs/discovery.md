# Discovery and generation

Writing a configuration by hand for hundreds of tables is slow and error-prone. These commands generate configuration from what already exists:

| Command | Starts from | Produces |
|---|---|---|
| `dvf discover` | Two databases or folders | A comparison configuration: matched tables, keys, renamed columns |
| `dvf import-mapping` | A source-to-target (S2T) mapping sheet (Excel or CSV) | A comparison configuration with keys, mappings, normalizers, and checks |
| `dvf import-contract` | An ODCS v3 data contract | A configuration with a contract conformance check and quality checks |
| `dvf export-contract` | A configuration | An ODCS v3.1 data contract |
| `dvf profile` | A configuration | Column profiles: NULLs, distinct values, ranges, lengths, top values |
| `dvf suggest` | A configuration or a saved profile | Candidate checks, with the evidence for each |

Every generated file is a normal configuration (or, for `suggest`, a fragment of one). Anything dvf guessed carries a YAML comment, and comments that need a decision start with `REVIEW`. Check a generated file with `dvf validate-config` and `dvf explain` before running it.

Generated files never contain resolved secrets: connections copied from a configuration are copied as written (with their `${secret:...}` references), and a database URL given on the command line with a password is written as `${secret:env:SOURCE_DATABASE_URL}` (or `TARGET_DATABASE_URL`). Relative file paths are rewritten so they still point at the same files from the folder the new file is written to.

All commands print the generated YAML on standard output unless you pass `--output FILE`. They refuse to replace an existing file unless you pass `--force`. Summaries and notes go to standard error, so `dvf discover ... > dvf.yaml` works.

## Discover tables and columns

`dvf discover` lists the tables of a source and a target, matches them by name, matches their columns, and writes a configuration.

```bash
# Connections defined in a configuration (the first comparison suite's, or the only two)
dvf discover dvf.yaml -o discovered.yaml
dvf discover dvf.yaml --source legacy --target lakehouse --source-schema SALES -o sales.yaml

# Connections given as locations
dvf discover --source duckdb:///legacy.duckdb --target postgresql://qa@db/warehouse \
    --target-schema core -o dvf.yaml
dvf discover --source ./landing --target ./curated -o files.yaml
```

Locations are `postgresql://...`, `mysql://...`, `duckdb:///path`, `sqlite:///path` (three slashes before a relative path, four before an absolute one, as in SQLAlchemy), a `.duckdb` or `.sqlite`/`.db` file, or a folder of data files (CSV, TSV, Parquet, JSON, NDJSON). For other systems, define the connections in a configuration file.

How it matches:

1. **Tables**: by name without the schema (for files, the file name without extension). Exact matches first, then case-insensitive ones, then fuzzy ones.
2. **Fuzzy scores** compare names lower-cased with separators removed, so `order_lines` and `OrderLines` score 1.0 and `customer` and `customers` about 0.94. Pairs scoring below `--threshold` (default 0.8) are not matched. Each table and column is matched at most once; the best scores win.
3. **Columns** of each matched pair are matched the same way. Columns whose names differ only in case need no mapping; other matches are written under `columns.mappings`.
4. **Types** of each column pair are classified with the canonical type system. Lossy, incompatible, and unclassifiable pairs are listed as `REVIEW type risks` above the table.
5. **Keys** come from the source's declared primary key, else the target's (mapped back to source names). With `--probe-keys`, a table without either gets its first candidate column (`id`, `<table>_id`, or names ending in `_id`, `_key`, `_code`, `_no`, `_number`) that is unique and never NULL in the source, tested with one query per table. Otherwise the candidates are listed in a comment and the table compares counts and control totals only.

A generated table looks like this:

```yaml
      # customers -> CUST_MASTER: fuzzy match (score 0.86). REVIEW: confirm these are the same table
      # REVIEW type risks:
      #   BALANCE: DECIMAL(18,4) -> NUMBER(18,2) is lossy (scale reduced from 4 to 2)
      # Columns only in the target: LOADED_AT
      - name: customers
        source: {table: customers}
        target: {table: CUST_MASTER}
        keys: [customer_id]  # primary key declared in the source
        columns:
          mappings:
            - {source: email_address, target: EMAIL}  # fuzzy match (score 0.83). REVIEW
        comparison:
          strategies: [row_count, aggregate, row_hash]
          on_mismatch: full_diff
```

Tables without a match on the other side are listed in comments at the end of the file. Without a schema filter, database tables are listed by name without their schema, from every schema the connection can see; give `--source-schema` and `--target-schema` when the same table name exists in several schemas. In PostgreSQL and Redshift (which fold names to lower case) and Snowflake and Oracle (upper case), names that would be folded are quoted, for example `core."OrderLines"`.

| Option | Meaning |
|---|---|
| `CONFIG` | Configuration holding the connections (optional) |
| `--source`, `--target` | Connection names in `CONFIG`, or locations without it |
| `--source-schema`, `--target-schema` | Only tables in this schema (ignored for SQLite and files, with a note) |
| `--source-pattern`, `--target-pattern` | Glob over table names, or file paths for file connections (default `*`) |
| `--threshold` | Lowest fuzzy score accepted, 0 to 1 (default 0.8) |
| `--no-fuzzy` | Match exactly and case-insensitively only |
| `--probe-keys` | Infer missing keys from data (one query per table without a primary key) |
| `--suite-name`, `--project-name` | Names in the generated file (default `discovered`) |
| `--profile` | Environment profile of `CONFIG` |
| `--output`, `--force` | Where to write, and whether to replace an existing file |
| `--format json` | Print a JSON summary of the matches instead of the YAML (use with `--output`) |

## Import a source-to-target mapping sheet

`dvf import-mapping` converts a mapping sheet, one row per target column, into a configuration. Excel workbooks (`.xlsx`, `.xlsm`) and CSV files (comma, semicolon, tab, or pipe separated, UTF-8) are read. The files in `examples/discovery` (`s2t_mapping.csv` and `s2t_mapping.xlsx`) show the layout; `orders.odcs.yaml` there is a sample contract.

```bash
dvf import-mapping mapping.xlsx -o dvf.yaml
dvf import-mapping mapping.xlsx --sheet "Customer S2T" --config dvf.yaml --source legacy --target lake -o customers.yaml
dvf import-mapping mapping.csv --source duckdb:///legacy.duckdb --target duckdb:///lake.duckdb
```

### Layout

The header row is found in the first 20 rows; headers are matched ignoring case, spaces, and punctuation. The required columns are `source_table`, `target_table`, and `target_column`.

| Field | Accepted headers (besides the field name) | Meaning |
|---|---|---|
| `source_table` | source table, source table name, src table, source object, source entity, source dataset, from table | Source table (may be qualified) |
| `source_column` | source column, source column name, src column, source field, source attribute, from column | Source column |
| `source_expr` | source expr, source expression, source sql, sql expression, expression, derivation sql | A SQL expression on the source the target column is derived from (instead of `source_column`) |
| `target_table` | target table, target table name, tgt table, target object, target entity, target dataset, destination table, to table | Target table |
| `target_column` | target column, target column name, tgt column, target field, target attribute, destination column, to column | Target column |
| `key` | key flag, is key, pk, primary key, business key, key column | `Y` for key columns |
| `transformation` | transformation rule, transformation logic, business rule, mapping rule, logic | Prose, copied into a comment (`Direct`, `1:1`, `as is`, and `none` are ignored) |
| `normalizers` | normalizer, normalize, normalization | Normalizers, separated by `;` or `,`: `rtrim; round(2); sap_alpha` |
| `compare` | compare flag, in scope, include, validate | `N` to map the column but not compare its values (default `Y`) |
| `pii` | pii flag, personal data, sensitive, gdpr | `Y` to mask the column's values in reports |
| `notes` | note, comment, comments, remarks, description | Copied into a comment |
| `checks` | check, rules, rule, dq rule, dq rules, dq checks, validation rules, validations | Data checks on the target column (see below) |
| `suite` | subject area, module, domain | Suite name for the row (default `--suite-name`, else `mapping`) |
| `source_filter`, `target_filter` | source where, source scope; target where, target scope | A table's filter (give it on one row of the table, or the same on all) |

Flags accept `Y`, `Yes`, `True`, `1`, `X` and `N`, `No`, `False`, `0`, or empty. Other columns are ignored and listed in a note.

Rows are grouped into tables by source and target table. The generated table:

- **keys**: the key rows, with their normalizers;
- **columns.mappings**: rows whose column is renamed, derived (`source_expr`), normalized, not compared, or PII;
- **columns.include**: the other listed columns. Only listed columns are compared; pass `--include-unlisted` to compare every column with the same name on both sides;
- **columns.exclude**: source columns with an empty `target_column` (not migrated) and target columns with neither a source column nor an expression (for example audit columns). A target-only row may leave `source_table` empty;
- **checks**: from the checks column.

### Rules in the checks column

Separate rules with `;`. A `regex` rule takes the rest of the cell, so put it last.

| Rule | Check |
|---|---|
| `not_null`, `not_blank` | One `not_null` (or `not_blank`) check per table over all its columns with the rule |
| `unique` | `unique` on the column |
| `regex:PATTERN` | `regex` (whole value) |
| `allowed_values:A\|B\|C` | `allowed_values` (compared as text) |
| `range:MIN..MAX` | `range`; either bound may be empty (`range:0..`) |
| `length:MIN..MAX`, `max_length:N` | `length` |
| `null_ratio:MAX` | `null_ratio` with `max_ratio` MAX (0 to 1) |

Checks run on the target (the default side of comparison suites).

### Connections and errors

Connections come from `--config` (copied as written; `--source` and `--target` name them, defaulting to the first two), from `--source` and `--target` locations, or are placeholders marked `REVIEW: placeholder` that you replace.

Nothing is written until the whole sheet is valid. Every problem is reported at once with its row, for example:

```text
error DVF-C100 mapping.xlsx:7
    sheet 'Mapping', row 7: unknown normalizer 'rtirm'
    hint: Did you mean 'rtrim'?
```

## Import an ODCS data contract

`dvf import-contract` reads an [ODCS](https://bitol-io.github.io/open-data-contract-standard/) v3 contract (v3.0.x, v3.1, and v3.2 are read) and writes a configuration with one single-dataset suite. Each schema object becomes a table.

```bash
dvf import-contract orders.odcs.yaml -o contract-checks.yaml
dvf import-contract orders.odcs.yaml --server staging -o staging.yaml
dvf import-contract orders.odcs.yaml --config dvf.yaml --connection lake
dvf import-contract orders.odcs.yaml --source duckdb:///orders.duckdb --between inclusive
```

What becomes what:

| Contract element | Configuration |
|---|---|
| object `physicalName` (else `name`) | the table's `source.table` (for file servers, `source.path`) |
| `primaryKey`, `primaryKeyPosition` | `keys`, plus `not_null` and `unique` checks on the key |
| `required: true` | `not_null` (one check per table) |
| `unique: true` | `unique` |
| `logicalTypeOptions` `minLength`, `maxLength` | `length` |
| `logicalTypeOptions` `pattern` | `regex` |
| `logicalTypeOptions` `format` of strings: `email`, `uuid`, `date`, `date-time`, `ipv4` | `regex` |
| `logicalTypeOptions` `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` | `range` (two checks when one bound is inclusive and the other exclusive) |
| `logicalTypeOptions` `multipleOf` (whole numbers) | `expression` |
| `enum` (v3.2) | `allowed_values` |
| `relationships` of type `foreignKey` (v3.1) | `referential_integrity` |
| quality `nullValues` | `not_null` for "must be 0", `null_ratio` for "at most N percent", else `custom_sql` |
| quality `missingValues` with `arguments.missingValues` | `custom_sql` counting NULLs and the listed values |
| quality `invalidValues` with `arguments.validValues` or `arguments.pattern` | `allowed_values` or `regex`; "at most N percent" becomes `mostly`; other thresholds use `custom_sql` |
| quality `duplicateValues` (property, or schema level with `arguments.properties`) | `unique`; thresholds use `custom_sql` (duplicate rows beyond the first) |
| quality `rowCount` | `row_count_range`; `mustNotBe` and `mustNotBeBetween` use `custom_sql` |
| v3.0 `rule` names `nullCheck`, `duplicateCount`, `validValues`, `rowCount` | as `nullValues`, `duplicateValues`, `invalidValues`, `rowCount` |
| quality `type: sql` with `query` and an operator | `custom_sql` returning a scalar; `{object}` becomes the table and `{property}` the quoted column |
| rule `id` (or `name`), `description`, `severity`, `dimension`, `businessImpact` | check `name`, `description`, `severity` (when it is a dvf severity), and `details` |

Operators map to comparisons: `mustBe` (==), `mustNotBe` (!=), `mustBeGreaterThan`, `mustBeGreaterOrEqualTo`, `mustBeLessThan`, `mustBeLessOrEqualTo`, `mustBeBetween`, and `mustNotBeBetween`. A metric rule without an operator means "must be 0" (`rowCount` needs an operator). `unit` is `rows` (default) or `percent`.

ODCS defines `mustBeBetween` as `mustBeGreaterThan` plus `mustBeLessThan`, so its bounds are exclusive by default. Some tools treat them as inclusive; pass `--between inclusive` to do the same.

Elements dvf cannot run are never dropped silently. They are printed, returned in the result, and written as comments in the generated file with the reason: `text` rules (prose), `custom` rules (tool-specific, such as Soda or Great Expectations), unknown metrics, nested object and array properties, array and object options, and date `format` options.

The connection comes from `--config` and `--connection`, from a `--source` location, or from the contract's `servers` (`--server` picks one, default the first). Server types `postgresql`, `mysql`, `sqlserver`, `oracle`, `redshift`, `snowflake`, `bigquery`, `databricks`, `duckdb`, and `local`, `s3`, `gcs`, `azure` (files) are mapped; credentials are written as `${env:<SERVER>_USER}` and `${secret:env:<SERVER>_PASSWORD}` references for you to set. Unsupported server types produce a placeholder connection and a listed item. File paths in `servers` are read relative to the contract file.

Each table also gets a `contract` check that compares the dataset's schema with the contract object (leave it out with `--no-conformance`).

## Check contract conformance (SCH-12)

The `contract` check compares the checked dataset's schema with one object of an ODCS v3 contract or a dvf expected-schema file.

```yaml
checks:
  - type: contract
    contract: contracts/orders.odcs.yaml   # relative to the configuration file
    object: orders                         # default: the only object, or the one named like the table
    extra_columns: warning                 # error | warning | ignore
    types: error
    nullability: warning
    primary_key: warning
```

| Field | Default | Meaning |
|---|---|---|
| `contract` | required | ODCS v3 contract or expected-schema file (YAML or JSON) |
| `object` | the only object, or the one named like the dataset | Schema object to compare with |
| `extra_columns` | `warning` | Columns in the dataset that the contract does not list |
| `types` | `error` | A different logical type, or a physical type the dataset holds only with loss. A dataset type wider than the contract's is at most a warning |
| `nullability` | `warning` | Columns the contract marks `required` that the dataset declares nullable (primary key columns count as not nullable) |
| `primary_key` | `warning` | A dataset primary key that differs from the contract's, or none |
| `case_sensitive` | `false` | Match column names case-sensitively |
| `type_dialect` | the dataset's dialect | Dialect used to read the contract's physical types |

Missing columns are always errors. `error` problems fail the check, `warning` problems make it a warning, and `ignore` skips the aspect. The samples list every problem with the column, aspect, expected and actual value, and level. Nullability that the dataset does not declare (files and queries) is listed under `details.not_checked`.

Physical types are compared with the canonical type system when the contract gives them (`physicalType: varchar(50)`); otherwise the logical type is compared (`integer` accepts integer columns, `number` accepts integers and decimals, and `date` also accepts timestamps, as ODCS v3.0 has no timestamp type).

An expected-schema file is a smaller format:

```yaml
dialect: postgres            # optional: dialect of the 'type' strings
columns:
  - {name: order_id, type: BIGINT, nullable: false}
  - {name: amount, type: "DECIMAL(10,2)"}
  - {name: status, logical_type: string}
primary_key: [order_id]
```

## Export a contract

`dvf export-contract` writes an ODCS v3.1 contract describing a configuration's datasets: one schema object per table, with properties from the catalog (logical and physical type, `required` for NOT NULL columns, primary key and positions, `maxLength` of bounded strings) and the table's checks translated where ODCS can express them.

```bash
dvf export-contract dvf.yaml -o contracts/finance.odcs.yaml --id finance-v1 --version 1.0.0
dvf export-contract dvf.yaml --table customers --side source
```

| Check | ODCS |
|---|---|
| `not_null` | property quality `nullValues` must be 0 (or at most N percent with `mostly`) |
| `unique` (one column) | `unique: true` |
| `unique` (several columns) | schema quality `duplicateValues` with `arguments.properties` |
| `allowed_values`, `regex` | property quality `invalidValues` with `validValues` or `pattern` |
| `range`, `length` | `logicalTypeOptions` |
| `null_ratio` | `nullValues` at most N percent |
| `row_count_range` | `rowCount` rules |
| `custom_sql` | `type: sql` rules |
| `referential_integrity` | `relationships` |

Checks with a `where` filter, `mostly` on uniqueness, ranges, and lengths, substring regexes, and check types without an ODCS equivalent (`expression`, `freshness`, `contract`, plugin checks) are listed as not exported. `servers` lists the DuckDB, file, and database connections used, with host, port, database, and schema only: credentials are never exported. `--side` is `auto` (the target of comparison suites), `source`, or `target`.

## Profile columns

`dvf profile` computes column profiles inside each database, with the same SQL as the `profile` comparison strategy:

- rows, NULL count and percentage, distinct values;
- minimum, maximum, and mean of numbers, dates, and timestamps;
- minimum, maximum, and average length of text;
- the most frequent values (one query per column; skipped for columns whose values are all distinct);
- recognized formats of text columns (one query per table): UUID, email, ISO date and date-time, E.164 phone number, US ZIP, UK postcode, and Canadian postal code.

Nested, spatial, JSON, and unknown types are listed as not profiled.

```bash
dvf profile dvf.yaml                          # target of comparison suites, source of the others
dvf profile dvf.yaml --table customers --side both --top 10
dvf profile dvf.yaml --format json -o profile.json
```

| Option | Meaning |
|---|---|
| `--table`, `--suite` | Only matching tables (globs, repeatable) and this suite |
| `--side` | `auto`, `source`, `target`, or `both` |
| `--top N` | Most frequent values per column (default 5; 0 for none) |
| `--no-formats` | Skip format detection |
| `--format json`, `--output FILE` | Print JSON, and/or write it to a file |

Values in profiles follow `defaults.privacy`: PII columns (`pii_columns` and mappings marked `pii`) are masked, email addresses are partly masked, and strict mode leaves minimums, maximums, and top values out. A dataset that cannot be read is reported and the command exits with code 2.

## Suggest checks

`dvf suggest` turns profiles into candidate checks. It profiles the configuration, or reads a saved profile with `--from-profile`.

```bash
dvf suggest dvf.yaml -o suggested.yaml
dvf suggest dvf.yaml --table orders --margin 0.1 --max-values 20
dvf suggest --from-profile profile.json
```

| Suggestion | When | Evidence comment |
|---|---|---|
| `not_null` | Columns without NULLs (one check per table) | `no NULLs in 1,200 rows` |
| `unique` | Integer, decimal, text, and UUID columns whose non-NULL values are all distinct | `1,200 distinct values in 1,200 non-NULL rows` |
| `allowed_values` | Text and integer columns with at most `--max-values` distinct values, each seen twice on average, none masked | `3 distinct values in 1,200 non-NULL rows: paid (800), new (300), cancelled (100)` |
| `range` | Numbers (not unique integer columns such as IDs), from minimum to maximum | `observed minimum 0.50 and maximum 99.50` |
| `range` with `min` only | Dates and timestamps; no maximum, as new data keeps arriving | `observed minimum 2026-01-01` |
| `regex` | Text columns whose values all match a recognized format | `all 1,200 non-NULL values look like a email address` |
| `length` | Other text columns, from the shortest to the longest value | `observed lengths 2 to 35` |
| `freshness` | The timestamp column with the newest value (future values are ignored), severity `warning` | `newest loaded_at 2026-11-07T22:00:00 is 0.6d old` |

`--margin 0.1` widens ranges and length bounds by 10% of the observed span (integer bounds are rounded outward) and adds 10% to freshness ages. `--no-freshness` leaves freshness checks out.

The output is a fragment shaped like a configuration:

```yaml
suites:
  - name: sales
    tables:
      - name: orders
        checks:
          # evidence (target): no NULLs in 1,200 rows
          - {type: not_null, columns: [order_id, status]}
          # evidence (target): 3 distinct values in 1,200 non-NULL rows: paid (800), new (300), cancelled (100)
          - {type: allowed_values, column: status, values: [paid, new, cancelled]}
```

Copy the checks you want into the matching tables. When a side is not the suite's default, the check says so (`side: source`); with `--side both`, checks are named after their side so their IDs stay unique. Tables expanded from a `pattern` appear under their expanded names: add a named table to use their checks. Suggestions describe the data as it is today, including its defects; review them before adopting them as rules.

## Python API

Each command has a function in `dvf.api`:

```python
from dvf import api

result = api.discover(source="duckdb:///legacy.duckdb", target="./curated", probe_keys=True)
print(result.text)  # the YAML; result.tables, result.unmatched_source, ...

api.import_mapping("mapping.xlsx", output="dvf.yaml")
contract = api.import_contract("orders.odcs.yaml", server="local")
for item in contract.unsupported:
    print(item.where, item.item, item.reason)

report = api.profile("dvf.yaml", tables=["customers"], side="both")
suggestions = api.suggest(profile_report=report, margin=0.1)
api.export_contract("dvf.yaml", output="contract.odcs.yaml")
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The file was generated (or the profile taken) |
| `2` | A connection, query, or dataset failed (for `profile` and `suggest`, a dataset could not be profiled) |
| `3` | A configuration or usage problem: invalid sheet or contract, unknown connection, existing output file without `--force` |
