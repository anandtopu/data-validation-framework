"""``dvf init --example``: a runnable local project with seeded discrepancies."""

from __future__ import annotations

import json
from pathlib import Path

from dvf.core.errors import DvfError
from dvf.scaffold.seeded import generate, write_csv, write_parquet

EXAMPLE_CONFIG = """\
# yaml-language-server: $schema={schema}
#
# Compare a CSV landing extract with the curated Parquet output, and check data quality.
# The data is synthetic and contains seeded defects, so the run reports failures on purpose:
# missing, extra, and duplicate rows; whitespace, case, leading-zero, rounding, truncation,
# time zone, NULL, encoding, and value changes; a type change; and orphaned orders.
#
#   dvf run dvf.yaml
version: 1

project:
  name: local-files-demo
  description: Landing CSV to curated Parquet, all on the local machine
  metadata:
    owner: data-engineering

defaults:
  engine: duckdb
  sample_size: 20
  normalization:
    timestamp_precision: ms

connections:
  landing:
    type: files
    path: ./data/landing
    format: csv
    options:
      header: true
  curated:
    type: files
    path: ./data/curated
    format: parquet

suites:
  - name: customers
    description: Customer master data
    source: landing
    target: curated
    tags: [master-data, pr]
    tables:
      - name: customers
        source:
          path: customers.csv
          options:
            types: {{ customer_code: VARCHAR, balance: "DECIMAL(12,3)" }}
        target: {{ path: customers.parquet }}
        keys: [customer_id]
        columns:
          exclude: [ingested_at]
          mappings:
            - {{ source: email_address, target: email }}
            - {{ source_expr: "first_name || ' ' || last_name", target: full_name }}
        comparison:
          strategies: [row_count, aggregate, row_hash]
          on_mismatch: full_diff
        checks:
          - {{ type: not_null, columns: [customer_id, email] }}
          - {{ type: unique, columns: [customer_id] }}
          - type: regex
            column: email
            pattern: "^[^@\\\\s]+@[^@\\\\s]+\\\\.[a-z]{{2,}}$"
            mostly: 0.99
            details: {{ requirement: DQ-EMAIL-01 }}
          - {{ type: allowed_values, column: country_code, values: [DE, FR, GB, IN, US] }}
          - {{ type: row_count_range, min: 1 }}

      - name: orders
        source:
          path: orders.csv
          options:
            types: {{ amount: "DECIMAL(10,2)" }}
        target: {{ path: orders.parquet }}
        keys: [order_id]
        comparison:
          strategies: [row_count, aggregate]
          group_by: [order_date]
        checks:
          - type: referential_integrity
            column: customer_id
            parent: {{ table: customers.parquet, column: customer_id }}
          - type: expression
            name: shipped_after_ordered
            expression: shipped_at IS NULL OR shipped_at >= ordered_at
          - {{ type: freshness, column: ordered_at, max_age: 2d, severity: warning }}

reports:
  formats: [html, json, junit, markdown]
  output_dir: ./reports/${{run.date}}/${{run.id}}
  branding:
    title: Local files demo
"""


def example_config(schema_path: str = "./dvf.schema.json") -> str:
    return EXAMPLE_CONFIG.format(schema=schema_path)


def write_example_data(directory: Path, *, rows: int = 200, seed: int = 7) -> list[Path]:
    """Write the example's landing CSV and curated Parquet files under ``directory/data``."""
    data = generate(rows=rows, seed=seed)
    landing, curated = directory / "data" / "landing", directory / "data" / "curated"
    customers = data.source_customers.rename_columns(
        ["email_address" if n == "email" else n for n in data.source_customers.column_names]
    )
    files = {
        landing / "customers.csv": (write_csv, customers),
        landing / "orders.csv": (write_csv, data.source_orders),
        curated / "customers.parquet": (write_parquet, data.target_customers),
        curated / "orders.parquet": (write_parquet, data.target_orders),
    }
    for path, (writer, table) in files.items():
        writer(table, path)
    expected = data.expected
    manifest = {
        "customers": {
            "missing_in_target": expected.customers_missing,
            "extra_in_target": expected.customers_extra,
            "duplicate_keys_target": expected.customers_duplicates,
            "mismatched_rows": expected.customers_mismatched_rows,
            "column_classifications": expected.columns,
            "type_changes": list(expected.type_changes),
        },
        "orders": {"extra_in_target": expected.orders_extra, "orphans": expected.orphans},
    }
    manifest_path = directory / "data" / "seeded_defects.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return [*files, manifest_path]


def write_example(
    directory: Path, *, force: bool = False, schema: dict[str, object] | None = None
) -> list[Path]:
    """Create a runnable example project in ``directory``."""
    config_path = directory / "dvf.yaml"
    if config_path.exists() and not force:
        raise DvfError(f"{config_path} already exists; pass --force to overwrite it.")
    directory.mkdir(parents=True, exist_ok=True)
    written = write_example_data(directory)
    config_path.write_text(example_config(), encoding="utf-8", newline="\n")
    written.append(config_path)
    if schema is not None:
        schema_path = directory / "dvf.schema.json"
        schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8", newline="\n")
        written.append(schema_path)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("reports/\n.dvf/\n", encoding="utf-8", newline="\n")
        written.append(gitignore)
    return written


__all__ = ["example_config", "write_example", "write_example_data", "write_starter"]


STARTER_CONFIG = """\
# yaml-language-server: $schema=./dvf.schema.json
#
# A starting point. Replace the connections and tables with your own, then run:
#   dvf validate-config dvf.yaml
#   dvf connections test dvf.yaml
#   dvf run dvf.yaml
version: 1

project:
  name: my-migration

connections:
  source:
    type: duckdb
    database: ./source.duckdb
  target:
    type: postgres
    url: ${secret:env:TARGET_DATABASE_URL}

suites:
  - name: main
    source: source
    target: target
    tables:
      - name: customers
        source: { table: customers }
        target: { table: public.customers }
        keys: [customer_id]
        comparison:
          strategies: [row_count, aggregate, row_hash]
          on_mismatch: full_diff
        checks:
          - { type: not_null, columns: [customer_id] }
          - { type: unique, columns: [customer_id] }

reports:
  formats: [html, json, junit, markdown]
"""


def write_starter(
    directory: Path, *, force: bool = False, schema: dict[str, object] | None = None
) -> list[Path]:
    """Create a starter ``dvf.yaml`` (and its JSON Schema) in ``directory``."""
    config_path = directory / "dvf.yaml"
    if config_path.exists() and not force:
        raise DvfError(f"{config_path} already exists; pass --force to overwrite it.")
    directory.mkdir(parents=True, exist_ok=True)
    config_path.write_text(STARTER_CONFIG, encoding="utf-8", newline="\n")
    written = [config_path]
    if schema is not None:
        schema_path = directory / "dvf.schema.json"
        schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8", newline="\n")
        written.append(schema_path)
    return written
