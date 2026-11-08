"""Load the synthetic orders of the MongoDB-to-PostgreSQL example into both databases.

PostgreSQL gets the relational model (``orders`` and ``order_lines``); MongoDB gets one ``orders``
document per order with its lines embedded in a ``lines`` array. The data is synthetic, and the
MongoDB side carries seeded defects that ``dvf run dvf.yaml`` reports:

1. order 1002 is missing line 3;
2. order 1003, line 2 has quantity 6 instead of 5;
3. order 1004 has an extra line 3;
4. order 1005 has status ``pending`` instead of ``shipped``;
5. order 1007 stores its total as a double instead of a Decimal128 (a type quirk: the values
   still reconcile, and the ``document_schema`` check reports the type).

Everything else must reconcile despite the type differences: Decimal128 prices against
``numeric(10,2)``, BSON dates against ``date``, a 64-bit ``line_no`` in order 1008, and the nested
``customer.name`` against ``customer_name``.

Usage (Windows, macOS, Linux)::

    set DVF_EXAMPLE_MONGODB_URL=mongodb://localhost:27017
    set DVF_EXAMPLE_POSTGRES_URL=postgresql://user:password@localhost:5432/dvf
    uv run python load_data.py            # create or replace the example data
    uv run python load_data.py --drop     # remove it again

``--mongo-db`` and ``--pg-schema`` (or ``DVF_EXAMPLE_MONGO_DB`` and ``DVF_EXAMPLE_PG_SCHEMA``)
choose where the data goes; both default to ``dvf_example``.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

ORDER_IDS = range(1001, 1011)
STATUSES = ("open", "shipped", "invoiced")
SKUS = ("PEN-01", "PAD-02", "INK-03", "CLIP-04", "TAPE-05", "GLUE-06")


def relational_orders() -> list[dict[str, Any]]:
    """The orders as the source system holds them (header fields plus lines)."""
    orders = []
    for order_id in ORDER_IDS:
        n = order_id - 1000
        line_count = 2 if order_id == 1003 else 1 + n % 3  # 1002 has 3 lines, 1004 has 2
        lines = []
        for line_no in range(1, line_count + 1):
            qty = 1 + (n * line_no) % 5
            price = Decimal(f"{2 + (n + line_no) % 7}.{(n * 7 + line_no * 13) % 100:02d}")
            lines.append(
                {
                    "line_no": line_no,
                    "sku": SKUS[(n + line_no) % len(SKUS)],
                    "qty": qty,
                    "price": price,
                }
            )
        if order_id == 1003:
            lines[1]["qty"] = 5
        total = sum((line["price"] * line["qty"] for line in lines), Decimal("0.00"))
        orders.append(
            {
                "order_id": order_id,
                "customer_name": f"Customer {n:02d}",
                "order_date": date(2024, 3, n),
                "status": "shipped" if order_id == 1005 else STATUSES[n % 3],
                "total": total,
                "lines": lines,
            }
        )
    return orders


def documents() -> list[dict[str, Any]]:
    """The orders as MongoDB documents, with the seeded defects."""
    from bson import Decimal128, Int64

    docs = []
    for order in relational_orders():
        order_id = order["order_id"]
        lines = [
            {
                "line_no": Int64(line["line_no"]) if order_id == 1008 else line["line_no"],
                "sku": line["sku"],
                "qty": line["qty"],
                "price": Decimal128(line["price"]),
            }
            for line in order["lines"]
        ]
        if order_id == 1002:
            del lines[2]
        if order_id == 1003:
            lines[1]["qty"] = 6
        if order_id == 1004:
            lines.append({"line_no": 3, "sku": "EXTRA-99", "qty": 1, "price": Decimal128("1.00")})
        total: Any = Decimal128(order["total"])
        if order_id == 1007:
            total = float(order["total"])
        d = order["order_date"]
        docs.append(
            {
                "order_id": order_id,
                "customer": {"name": order["customer_name"], "segment": "retail"},
                "order_date": datetime(d.year, d.month, d.day, tzinfo=UTC),
                "status": "pending" if order_id == 1005 else order["status"],
                "total": total,
                "lines": lines,
            }
        )
    return docs


def load_postgres(url: str, schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(url, autocommit=True) as con:
        ident = sql.Identifier(schema)
        con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident))
        con.execute(sql.SQL("DROP TABLE IF EXISTS {}.order_lines, {}.orders").format(ident, ident))
        con.execute(
            sql.SQL(
                "CREATE TABLE {}.orders (order_id integer PRIMARY KEY, customer_name text NOT NULL, "
                "order_date date NOT NULL, status text NOT NULL, total numeric(12, 2) NOT NULL)"
            ).format(ident)
        )
        con.execute(
            sql.SQL(
                "CREATE TABLE {}.order_lines (order_id integer NOT NULL REFERENCES {}.orders, "
                "line_no integer NOT NULL, sku text NOT NULL, qty integer NOT NULL, "
                "price numeric(10, 2) NOT NULL, PRIMARY KEY (order_id, line_no))"
            ).format(ident, ident)
        )
        with con.cursor() as cur:
            for order in relational_orders():
                cur.execute(
                    sql.SQL("INSERT INTO {}.orders VALUES (%s, %s, %s, %s, %s)").format(ident),
                    (
                        order["order_id"],
                        order["customer_name"],
                        order["order_date"],
                        order["status"],
                        order["total"],
                    ),
                )
                for line in order["lines"]:
                    cur.execute(
                        sql.SQL("INSERT INTO {}.order_lines VALUES (%s, %s, %s, %s, %s)").format(
                            ident
                        ),
                        (
                            order["order_id"],
                            line["line_no"],
                            line["sku"],
                            line["qty"],
                            line["price"],
                        ),
                    )


def load_mongodb(url: str, database: str) -> None:
    import pymongo

    client: Any = pymongo.MongoClient(url, tz_aware=True, serverSelectionTimeoutMS=10_000)
    try:
        collection = client[database]["orders"]
        collection.drop()
        collection.insert_many(documents())
        collection.create_index("order_id", unique=True)
    finally:
        client.close()


def drop(mongodb_url: str, postgres_url: str, mongo_db: str, pg_schema: str) -> None:
    import psycopg
    import pymongo
    from psycopg import sql

    client: Any = pymongo.MongoClient(mongodb_url, serverSelectionTimeoutMS=10_000)
    try:
        client.drop_database(mongo_db)
    finally:
        client.close()
    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(pg_schema)))


def load(mongodb_url: str, postgres_url: str, mongo_db: str, pg_schema: str) -> None:
    load_postgres(postgres_url, pg_schema)
    load_mongodb(mongodb_url, mongo_db)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mongodb-url", default=os.environ.get("DVF_EXAMPLE_MONGODB_URL"))
    parser.add_argument("--postgres-url", default=os.environ.get("DVF_EXAMPLE_POSTGRES_URL"))
    parser.add_argument("--mongo-db", default=os.environ.get("DVF_EXAMPLE_MONGO_DB", "dvf_example"))
    parser.add_argument(
        "--pg-schema", default=os.environ.get("DVF_EXAMPLE_PG_SCHEMA", "dvf_example")
    )
    parser.add_argument("--drop", action="store_true", help="remove the example data")
    args = parser.parse_args(argv)
    if not args.mongodb_url or not args.postgres_url:
        parser.error(
            "set DVF_EXAMPLE_MONGODB_URL and DVF_EXAMPLE_POSTGRES_URL "
            "(or pass --mongodb-url and --postgres-url)"
        )
    if args.drop:
        drop(args.mongodb_url, args.postgres_url, args.mongo_db, args.pg_schema)
        sys.stdout.write(f"Removed {args.mongo_db} (MongoDB) and {args.pg_schema} (PostgreSQL)\n")
    else:
        load(args.mongodb_url, args.postgres_url, args.mongo_db, args.pg_schema)
        sys.stdout.write(
            f"Loaded {len(ORDER_IDS)} orders into MongoDB {args.mongo_db}.orders and "
            f"PostgreSQL {args.pg_schema}.orders and {args.pg_schema}.order_lines\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
