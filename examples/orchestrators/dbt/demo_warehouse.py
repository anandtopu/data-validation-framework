"""Create the synthetic DuckDB warehouse that the example dbt manifest describes.

``target/manifest.json`` and ``target/run_results.json`` in this folder are synthetic dbt
artifacts for a small jaffle-shop project built into ``jaffle.duckdb`` (dbt adapter ``duckdb``).
This script creates that database without dbt, so the dbt integration can be tried end to end:

    uv run python demo_warehouse.py
    uv run dvf dbt import target/manifest.json -o dvf.yaml
    uv run dvf run dvf.yaml

The data carries seeded defects that the generated checks report: an order with status
``lost``, an order of a customer that does not exist, a negative amount on an order that was not
returned, and a customer without an email (a warning, as in the dbt test).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent

CUSTOMERS = [
    (1, "Ada", "Lovelace", "ada@example.com", 120.50),
    (2, "Grace", "Hopper", "grace@example.com", 80.00),
    (3, "Alan", "Turing", None, 15.25),
    (4, "Edsger", "Dijkstra", "edsger@example.com", 0.00),
    (5, "Barbara", "Liskov", "barbara@example.com", 42.10),
]

ORDERS = [
    (101, 1, "completed", 60.25, "2026-02-01 10:00:00"),
    (102, 1, "completed", 60.25, "2026-02-03 11:30:00"),
    (103, 2, "shipped", 80.00, "2026-03-10 09:15:00"),
    (104, 3, "placed", 15.25, "2026-03-11 16:45:00"),
    (105, 5, "returned", -12.00, "2026-04-02 08:00:00"),
    (106, 5, "lost", 54.10, "2026-04-05 12:00:00"),  # status not in accepted_values
    (107, 99, "placed", 10.00, "2026-04-06 13:00:00"),  # customer 99 does not exist
    (108, 4, "completed", -5.00, "2026-04-07 14:00:00"),  # negative amount, not returned
]


def create(path: Path) -> None:
    """Create (or replace) the example tables and view in ``path``."""
    path.unlink(missing_ok=True)
    with duckdb.connect(str(path)) as con:
        con.execute(
            "CREATE TABLE main.customers (customer_id INTEGER PRIMARY KEY, first_name VARCHAR, "
            "last_name VARCHAR, email VARCHAR, lifetime_value DECIMAL(10, 2))"
        )
        con.executemany("INSERT INTO main.customers VALUES (?, ?, ?, ?, ?)", CUSTOMERS)
        con.execute(
            "CREATE TABLE main.orders (order_id INTEGER, customer_id INTEGER, status VARCHAR, "
            "amount DECIMAL(10, 2), ordered_at TIMESTAMP)"
        )
        con.executemany("INSERT INTO main.orders VALUES (?, ?, ?, ?, ?)", ORDERS)
        con.execute(
            "CREATE VIEW main.stg_customers AS "
            "SELECT customer_id, first_name, last_name, email FROM main.customers"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, default=HERE / "jaffle.duckdb")
    args = parser.parse_args()
    create(args.database)
    sys.stdout.write(f"created {args.database}\n")


if __name__ == "__main__":
    main()
