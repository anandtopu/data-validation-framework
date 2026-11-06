"""Synthetic datasets with seeded defects of known type and count.

The generator builds a ``customers`` and an ``orders`` table, then a target copy with injected
defects: missing, extra, and duplicate rows; whitespace, case, leading-zero, rounding, truncation,
time zone, NULL, encoding, and value changes; a type change; and orphaned orders. Every defect
hits a different row, so the expected outcome is exact. All data is synthetic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

FIRST_NAMES = [
    "Alexander",
    "Beatrice",
    "Charlotte",
    "Dmitri",
    "Eleanor",
    "Frederik",
    "Gabriela",
    "Henrietta",
    "Ana",
    "Bo",
    "Li",
    "Max",
    "Noah",
    "Olga",
    "Priya",
    "Quentin",
    "Rafael",
    "Sofia",
    "Tomasz",
    "Uma",
]
ASCII_LAST_NAMES = [
    "Smith",
    "Garcia",
    "Kowalski",
    "Patel",
    "Johnson",
    "Okafor",
    "Rossi",
    "Larsen",
    "Chen",
]
ACCENTED_LAST_NAMES = ["Müller", "Gonçalves", "Nuñez"]
CITIES = ["Berlin", "Paris", "London", "Mumbai", "Austin", "Leeds", "Lyon", "Pune", "Denver"]
COUNTRIES = ["DE", "FR", "GB", "IN", "US"]
STATUSES = ["active", "blocked", "prospect"]

CUSTOMER_DEFECTS: dict[str, tuple[str, str, int]] = {
    "leading_zeros": ("customer_code", "leading_zeros", 3),
    "whitespace": ("city", "whitespace_only", 4),
    "case": ("last_name", "case_only", 3),
    "encoding": ("last_name", "encoding", 2),
    "rounding": ("balance", "numeric_rounding", 3),
    "truncation": ("first_name", "string_truncation", 2),
    "timezone": ("created_at", "timezone_shift", 3),
    "null": ("email", "null_vs_value", 2),
    "value_change": ("status", "value_changed", 2),
}
"""Defect name -> (column, expected classification, rows affected)."""


@dataclass
class Expected:
    """The exact outcome a correct comparison must report."""

    customers_missing: int = 3
    customers_extra: int = 2
    customers_duplicates: int = 2
    columns: dict[str, dict[str, int]] = field(default_factory=dict)
    type_changes: tuple[str, ...] = ("quantity",)
    orders_extra: int = 3
    orphans: int = 3

    @property
    def customers_mismatched_rows(self) -> int:
        return sum(sum(v.values()) for v in self.columns.values())


@dataclass
class SeededData:
    source_customers: pa.Table
    target_customers: pa.Table
    source_orders: pa.Table
    target_orders: pa.Table
    expected: Expected


CUSTOMER_SOURCE_SCHEMA = pa.schema(
    [
        ("customer_id", pa.int64()),
        ("customer_code", pa.string()),
        ("first_name", pa.string()),
        ("last_name", pa.string()),
        ("email", pa.string()),
        ("country_code", pa.string()),
        ("city", pa.string()),
        ("balance", pa.decimal128(12, 3)),
        ("created_at", pa.timestamp("us")),
        ("quantity", pa.int64()),
        ("status", pa.string()),
        ("ingested_at", pa.timestamp("us")),
    ]
)
ORDER_SCHEMA = pa.schema(
    [
        ("order_id", pa.int64()),
        ("customer_id", pa.int64()),
        ("order_date", pa.date32()),
        ("ordered_at", pa.timestamp("us")),
        ("shipped_at", pa.timestamp("us")),
        ("amount", pa.decimal128(10, 2)),
    ]
)


def _customer(rng: random.Random, customer_id: int, base: datetime) -> dict[str, Any]:
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(ASCII_LAST_NAMES + ACCENTED_LAST_NAMES)
    cents = rng.randint(1, 9_999_999) * 10 + rng.randint(1, 9)
    return {
        "customer_id": customer_id,
        "customer_code": f"{customer_id:06d}",
        "first_name": first,
        "last_name": last,
        "email": f"{first}.{last}.{customer_id}@example.com".lower()
        .encode("ascii", "ignore")
        .decode(),
        "country_code": rng.choice(COUNTRIES),
        "city": rng.choice(CITIES),
        "balance": Decimal(cents).scaleb(-3),
        "created_at": base + timedelta(minutes=rng.randint(0, 500_000), seconds=rng.randint(0, 59)),
        "quantity": rng.randint(0, 500),
        "status": rng.choice(STATUSES),
        "ingested_at": base + timedelta(days=400),
    }


def _apply(defect: str, row: dict[str, Any]) -> None:
    if defect == "leading_zeros":
        row["customer_code"] = str(int(row["customer_code"]))
    elif defect == "whitespace":
        row["city"] = row["city"] + "   "
    elif defect == "case":
        row["last_name"] = row["last_name"].upper()
    elif defect == "encoding":
        row["last_name"] = row["last_name"].encode("utf-8").decode("latin-1")
    elif defect == "rounding":
        row["balance"] = row["balance"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    elif defect == "truncation":
        row["first_name"] = row["first_name"][:4]
    elif defect == "timezone":
        row["created_at"] = row["created_at"] + timedelta(hours=2)
    elif defect == "null":
        row["email"] = None
    elif defect == "value_change":
        row["status"] = "inactive"


def _eligible(defect: str, row: dict[str, Any]) -> bool:
    if defect == "truncation":
        return bool(len(row["first_name"]) >= 6)
    if defect == "encoding":
        return bool(row["last_name"] in ACCENTED_LAST_NAMES)
    if defect == "case":
        return bool(row["last_name"] in ASCII_LAST_NAMES)
    if defect == "value_change":
        return bool(row["status"] == "active")
    return True


def generate(rows: int = 200, seed: int = 7, orders_per_customer: int = 3) -> SeededData:
    """Generate source and target tables with seeded defects."""
    if rows < 60:
        raise ValueError("generate() needs at least 60 rows to place every defect on its own row")
    rng = random.Random(seed)  # noqa: S311 - synthetic test data, not security
    base = datetime(2025, 1, 6, 8, 0, 0)
    source = [_customer(rng, 1000 + i, base) for i in range(rows)]
    target = [dict(r) for r in source]
    expected = Expected()
    available = list(range(rows))
    rng.shuffle(available)
    used: set[int] = set()

    def take(predicate: Any, count: int) -> list[int]:
        chosen = [i for i in available if i not in used and predicate(source[i])][:count]
        if len(chosen) < count:
            raise ValueError(f"not enough eligible rows for a defect (seed {seed}); use more rows")
        used.update(chosen)
        return chosen

    for defect, (column, label, count) in CUSTOMER_DEFECTS.items():
        for i in take(lambda r, d=defect: _eligible(d, r), count):
            _apply(defect, target[i])
        expected.columns.setdefault(column, {})[label] = count
    missing = set(take(lambda r: True, expected.customers_missing))
    duplicates = take(lambda r: True, expected.customers_duplicates)
    target_rows = [r for i, r in enumerate(target) if i not in missing]
    target_rows += [dict(target[i]) for i in duplicates]
    target_rows += [_customer(rng, 900_000 + i, base) for i in range(expected.customers_extra)]
    for row in target_rows:
        row["full_name"] = None
        row.pop("ingested_at", None)
    source_by_id = {r["customer_id"]: r for r in source}
    for row in target_rows:
        original = source_by_id.get(row["customer_id"])
        names = original if original is not None else row
        row["full_name"] = f"{names['first_name']} {names['last_name']}"
        row["quantity"] = str(row["quantity"])

    orders: list[dict[str, Any]] = []
    order_id = 50_000
    for i, customer in enumerate(source):
        if i in missing:
            continue
        for _ in range(rng.randint(1, orders_per_customer)):
            ordered = customer["created_at"] + timedelta(
                days=rng.randint(1, 60), minutes=rng.randint(0, 600)
            )
            shipped = None if rng.random() < 0.15 else ordered + timedelta(hours=rng.randint(2, 96))
            orders.append(
                {
                    "order_id": order_id,
                    "customer_id": customer["customer_id"],
                    "order_date": ordered.date(),
                    "ordered_at": ordered,
                    "shipped_at": shipped,
                    "amount": Decimal(rng.randint(100, 500_000)).scaleb(-2),
                }
            )
            order_id += 1
    target_orders = [dict(o) for o in orders]
    for k in range(expected.orphans):
        ordered = base + timedelta(days=30 + k)
        target_orders.append(
            {
                "order_id": 990_000 + k,
                "customer_id": 777_000 + k,
                "order_date": ordered.date(),
                "ordered_at": ordered,
                "shipped_at": ordered + timedelta(hours=5),
                "amount": Decimal("12.50"),
            }
        )
    target_schema = pa.schema(
        [f for f in CUSTOMER_SOURCE_SCHEMA if f.name not in ("ingested_at", "quantity")]
        + [pa.field("quantity", pa.string()), pa.field("full_name", pa.string())]
    )
    return SeededData(
        source_customers=pa.Table.from_pylist(source, schema=CUSTOMER_SOURCE_SCHEMA),
        target_customers=pa.Table.from_pylist(target_rows, schema=target_schema),
        source_orders=pa.Table.from_pylist(orders, schema=ORDER_SCHEMA),
        target_orders=pa.Table.from_pylist(target_orders, schema=ORDER_SCHEMA),
        expected=expected,
    )


def write_csv(table: pa.Table, path: Path) -> None:
    """Write ``table`` as CSV with ISO timestamps (``YYYY-MM-DD HH:MM:SS``)."""
    import pyarrow.csv as pacsv

    path.parent.mkdir(parents=True, exist_ok=True)
    pacsv.write_csv(table, str(path))


def write_parquet(table: pa.Table, path: Path) -> None:
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path))


def load_duckdb(tables: dict[str, pa.Table], database: Path) -> None:
    """Create (or replace) tables in a DuckDB file from Arrow tables."""
    import duckdb

    database.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(database))
    try:
        for name, table in tables.items():
            con.register("dvf_tmp", table)
            con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM dvf_tmp')
            con.unregister("dvf_tmp")
    finally:
        con.close()


_SQL_TYPES = {
    "sqlite": {
        "int64": "INTEGER",
        "string": "TEXT",
        "date32[day]": "DATE",
        "timestamp[us]": "TIMESTAMP",
    },
    "postgres": {
        "int64": "BIGINT",
        "string": "TEXT",
        "date32[day]": "DATE",
        "timestamp[us]": "TIMESTAMP",
    },
}


def _sql_type(field: pa.Field, dialect: str) -> str:
    if pa.types.is_decimal(field.type):
        return (
            f"NUMERIC({field.type.precision},{field.type.scale})"
            if dialect == "postgres"
            else f"DECIMAL({field.type.precision},{field.type.scale})"
        )
    return _SQL_TYPES[dialect][str(field.type)]


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def create_table_sql(
    name: str, table: pa.Table, dialect: str, primary_key: str | None = None
) -> str:
    columns = [f'"{f.name}" {_sql_type(f, dialect)}' for f in table.schema]
    if primary_key:
        columns.append(f'PRIMARY KEY ("{primary_key}")')
    return f'CREATE TABLE "{name}" ({", ".join(columns)})'


def load_sqlite(
    tables: dict[str, pa.Table], database: Path, primary_keys: dict[str, str] | None = None
) -> None:
    import sqlite3

    database.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(database)
    try:
        for name, table in tables.items():
            con.execute(f'DROP TABLE IF EXISTS "{name}"')
            con.execute(create_table_sql(name, table, "sqlite", (primary_keys or {}).get(name)))
            placeholders = ", ".join("?" for _ in table.schema)
            rows = [tuple(_sqlite_value(v) for v in r.values()) for r in table.to_pylist()]
            con.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
        con.commit()
    finally:
        con.close()


def load_postgres(
    tables: dict[str, pa.Table],
    conninfo: str,
    schema: str,
    primary_keys: dict[str, str] | None = None,
) -> None:
    """Load tables into a PostgreSQL schema (replacing it). Needs psycopg."""
    import psycopg

    with psycopg.connect(conninfo, autocommit=True) as con:
        con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        con.execute(f'CREATE SCHEMA "{schema}"')
        for name, table in tables.items():
            con.execute(
                create_table_sql(
                    f"{name}", table, "postgres", (primary_keys or {}).get(name)
                ).replace(f'CREATE TABLE "{name}"', f'CREATE TABLE "{schema}"."{name}"', 1)
            )
            columns = ", ".join(f'"{f.name}"' for f in table.schema)
            with (
                con.cursor() as cur,
                cur.copy(f'COPY "{schema}"."{name}" ({columns}) FROM STDIN') as copy,
            ):
                for row in table.to_pylist():
                    copy.write_row(tuple(row.values()))
