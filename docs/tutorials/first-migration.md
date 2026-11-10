# Your first migration comparison

This tutorial compares a table in a legacy database with its migrated copy in a new one, the everyday job dvf was built for. You will write a configuration, see why a first run reports every row as missing, fix the key mapping, accept one deliberate difference with a waiver, find the two real defects, and finish with a passing run you can put in CI. It takes about 20 minutes.

Both databases are local files, so you need nothing but dvf (see the [Quickstart](../getting-started.md)): the legacy CRM is a SQLite database, and the new analytics warehouse is a DuckDB database. With PostgreSQL, SQL Server, Oracle, or Snowflake, only the `connections` section changes ([Connectors](../connectors.md)).

## 1. Create the two databases

Make a folder for the project and save this script in it as `make_data.py`. It creates 1,000 synthetic customers in the legacy database, and a migrated copy in the warehouse with the usual changes of a migration: the zero-padded text key `cust_no` became the integer `customer_id`, the blank-padded `CHAR(30)` name became a trimmed `customer_name`, and two columns were renamed. It also plants two defects for you to find.

```python
"""Create a legacy SQLite database and its migrated copy in DuckDB (synthetic data)."""

import sqlite3
from datetime import date, timedelta

import duckdb

COUNTRIES = ["DE", "FR", "US", "IN", "BR"]
START = date(2020, 1, 1)


def customers():
    for i in range(1, 1001):
        yield (
            f"{i:06d}",  # legacy key: zero-padded text
            f"Customer {i}".ljust(30),  # legacy CHAR(30): padded with spaces
            COUNTRIES[i % len(COUNTRIES)],
            round(i * 1.25, 2),
            (START + timedelta(days=i)).isoformat(),
        )


with sqlite3.connect("legacy.sqlite") as legacy:
    legacy.execute("DROP TABLE IF EXISTS customers")
    legacy.execute(
        "CREATE TABLE customers (cust_no TEXT NOT NULL PRIMARY KEY, name CHAR(30),"
        " country TEXT, balance NUMERIC(12,2), created DATE)"
    )
    legacy.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers())

with duckdb.connect("warehouse.duckdb") as warehouse:
    warehouse.execute(
        "CREATE OR REPLACE TABLE dim_customer (customer_id INTEGER PRIMARY KEY,"
        " customer_name VARCHAR, country_code VARCHAR, balance DECIMAL(12,2),"
        " created_date DATE)"
    )
    rows = []
    for cust_no, name, country, balance, created in customers():
        customer_id = int(cust_no)
        if customer_id == 500:
            continue  # defect: one customer was not migrated
        if customer_id == 42:
            balance += 10  # defect: one balance changed
        rows.append((customer_id, name.rstrip(), country, balance, created))
    warehouse.executemany("INSERT INTO dim_customer VALUES (?, ?, ?, ?, ?)", rows)
```

Run it with the Python that has dvf installed (DuckDB comes with dvf; `sqlite3` is part of Python):

```bash
python make_data.py
```

## 2. Describe the comparison

Save this as `dvf.yaml` next to the databases:

```yaml
version: 1
project:
  name: crm-migration
  description: Legacy CRM (SQLite) to the analytics warehouse (DuckDB)

connections:
  legacy: { type: sqlite, database: legacy.sqlite }
  warehouse: { type: duckdb, database: warehouse.duckdb }

suites:
  - name: crm
    source: legacy
    target: warehouse
    tables:
      - name: customers
        source: { table: customers }
        target: { table: dim_customer }
        keys:
          - { source: cust_no, target: customer_id }
        columns:
          mappings:
            - { source: name, target: customer_name, normalize: [rtrim] }
            - { source: country, target: country_code }
            - { source: created, target: created_date }
        comparison:
          strategies: [row_count, aggregate]
          on_mismatch: full_diff
        checks:
          - { type: not_null, columns: [customer_id, customer_name] }
          - { type: unique, columns: [customer_id] }
          - { type: regex, column: country_code, pattern: "^[A-Z]{2}$" }
```

What each part does:

- **connections** name the two databases. Paths are relative to the configuration file.
- **suites** group tables that share a source and a target connection. A suite without a `target` checks one dataset only.
- **keys** pair rows across the two sides. The key is renamed, so both names are given.
- **columns.mappings** pair renamed columns; `balance` has the same name on both sides and pairs by itself. `rtrim` removes the legacy padding before values are compared, on both sides.
- **comparison** escalates: row counts, then control totals per column (`aggregate`), and only if they differ, a row-by-row diff (`full_diff`) that fetches just the differing rows.
- **checks** are data quality checks on the target (the default side in a comparison suite).

Every key is listed in the [configuration reference](../reference/configuration.md). Check the file before running anything:

```bash
dvf validate-config dvf.yaml
```

```text
OK dvf.yaml is valid: 1 suite, 1 table, 3 checks, 2 connections.
```

Mistakes are reported with file, line, and column, and a "did you mean" for misspelled keys.

## 3. Look before you run

Two commands show what dvf will do without comparing any data. `dvf schema-diff` compares the column schemas:

```bash
dvf schema-diff dvf.yaml
```

It flags `cust_no` (`TEXT`) to `customer_id` (`INTEGER`) as lossy, since text must be parsed into numbers, and `name` (`CHAR(30)`) to `customer_name` (`VARCHAR`) as a widening where trailing spaces may differ. Both are expected in this migration; you will deal with the first one in step 5.

`dvf plan` (an alias of `dvf explain`) shows the paired columns, each column's normalization, the strategies, and the SQL each step will run on each side:

```bash
dvf plan dvf.yaml --table customers
```

## 4. Run it, and read the first failure

```bash
dvf run dvf.yaml
```

The run fails, and the summary looks alarming:

```text
FAILED crm.customers.compare.keys: 1,000 missing in target; 999 extra in target
```

No row paired. The report's evidence shows why: the legacy keys are `'000042'` and the warehouse keys are `42`, and those are different values. When every row is missing on one side and extra on the other, suspect the key, not the data.

## 5. Normalize the key, and accept a deliberate change

Strip the leading zeros from the legacy key before pairing. Change the key line to:

```yaml
        keys:
          - { source: cust_no, target: customer_id, normalize: [strip_leading_zeros] }
```

Run again. Rows now pair, and the result is much more specific:

```text
FAILED crm.customers.schema.type_compatibility: 1 column(s) have incompatible or lossy type changes: customer_id
FAILED crm.customers.compare.row_count: source 1,000 rows, target 999 rows (difference -1)
FAILED crm.customers.compare.keys: 1 missing in target
FAILED crm.customers.compare.column_mismatch: 1 of 999 paired rows differ in 1 column(s): balance (1)
    balance: 1 (value_changed 1)
```

The type change of the key is a design decision, not a defect. Record it as a **waiver**, with a reason, a ticket, an approver, and an expiry date, by adding this at the end of `dvf.yaml`:

```yaml
waivers:
  - check: crm.customers.schema.type_compatibility
    column: customer_id
    reason: Customer numbers become integers in the warehouse by design
    ticket: MIG-101
    approved_by: data-architect
    expires: 2027-06-30
```

A waived check shows as `waived` in every report and counts as passed. When the waiver expires, the failure comes back so the decision gets reviewed. List the waivers and their status at any time:

```bash
dvf waivers list dvf.yaml
```

## 6. Investigate the real defects

Run once more:

```bash
dvf run dvf.yaml
```

```text
FAILED crm.customers.compare.row_count: source 1,000 rows, target 999 rows (difference -1)
FAILED crm.customers.compare.aggregate: 6 of 15 control totals differ: count(customer_id), ...
FAILED crm.customers.compare.keys: 1 missing in target
FAILED crm.customers.compare.column_mismatch: 1 of 999 paired rows differ in 1 column(s): balance (1)
FAILED 7 passed, 4 failed, 1 waived; quality score 66.7; 1.1s
waivers: 1 applied
wrote reports/2026-11-08/20261108T213503Z-fda7/results.json
wrote reports/2026-11-08/20261108T213503Z-fda7/report.html
```

Two defects remain, exactly the two that were planted. Open `report.html`: the `keys` check lists the missing customer (`500`), and the `column_mismatch` check shows the changed balance side by side, classified as `value_changed`. Follow that record through both sides with `dvf trace`:

```bash
dvf trace dvf.yaml --table customers --key 42
```

```text
crm.customers key 42 (canonical 42): 1 source row(s), 1 target row(s)
| Column        | Step                | Source         | Target        |         |
| customer_id   | raw                 | '000042'       | '42'          |         |
|               | strip_leading_zeros | '42'           | '42'          |         |
|               | canonical           | '42'           |               | =       |
| customer_name | raw                 | 'Customer 42 ' | 'Customer 42' |         |
|               | rtrim               | 'Customer 42'  | 'Customer 42' |         |
| ...           |                     |                |               |         |
| balance       | raw                 | '52.5'         | '62.50'       |         |
|               | canonical           | '52.50'        | '62.50'       | differs |
```

The trace shows each normalizer's effect and the canonical values dvf compares, which is how you tell a real difference from a formatting one. Each failed check also gets an extract of the mismatched rows and repro files (the SQL that ran on each side) next to the reports; see [Debugging and operations](../debugging.md).

## 7. Fix the data and get a clean run

In a real project the migration team fixes the load. Here, save this as `fix_data.py` and run it:

```python
import duckdb

with duckdb.connect("warehouse.duckdb") as warehouse:
    warehouse.execute("UPDATE dim_customer SET balance = 52.50 WHERE customer_id = 42")
    warehouse.execute(
        "INSERT INTO dim_customer VALUES (500, 'Customer 500', 'DE', 625.00, DATE '2021-05-15')"
    )
```

```bash
python fix_data.py
dvf run dvf.yaml
```

```text
PASSED 9 passed, 1 waived; quality score 100.0; 1.0s
```

The exit code is now 0. `dvf history dvf.yaml` lists both runs, and `dvf history compare RUN_A RUN_B` shows what changed between them, which is how mock cutover rehearsals are compared ([Migration validations](../migration.md)).

## Where to go next

- Gate a pipeline on the result: `dvf run dvf.yaml --fail-on error --format junit --format markdown`, then publish `junit.xml` ([CI/CD](../ci.md)).
- Scale up: for large tables in different databases, use `strategies: [row_count, hash_bucket]`, which compares checksums inside each database and moves only rows in differing buckets ([Checks and comparisons](../validations.md#checksum-search)).
- Generate the configuration instead of writing it: `dvf discover` matches tables and columns from two databases' metadata ([Discovery and generation](../discovery.md)).
- Cover the rest of the migration checklist: scope, deltas, SCD Type 2, deletes, key maps, audit columns ([Migration validations](../migration.md)).
- Write your own check: [Write a plugin check](plugin-check.md).
