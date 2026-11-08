"""Synthetic benchmark data: ``rows`` x 30 columns of mixed types, and a target copy in which
every ``1 / rate``-th row differs in one text column (0.01% by default).

Files are cached by size under the benchmark data folder, because writing 10 million rows takes
about half a minute.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

COLUMNS = 30


def _select(rows: int) -> str:
    cols = ["i AS id"]
    cols += [f"'name_' || CAST(hash(i * {j + 3}) % 100000 AS VARCHAR) AS s{j}" for j in range(10)]
    cols += [
        f"CAST((hash(i * {j + 17}) % 10000000) / 100.0 AS DECIMAL(18, 2)) AS d{j}" for j in range(6)
    ]
    cols += [f"(hash(i * {j + 29}) % 1000000) / 7.0 AS f{j}" for j in range(4)]
    cols += [
        f"DATE '2000-01-01' + CAST(hash(i * {j + 41}) % 9000 AS INTEGER) AS dt{j}" for j in range(3)
    ]
    cols += [
        f"TIMESTAMP '2020-01-01' + to_microseconds(CAST(hash(i * {j + 53}) % 100000000000 AS BIGINT)) AS ts{j}"
        for j in range(3)
    ]
    cols += ["(i % 3 = 0) AS b0", "(i % 7 = 0) AS b1", "CAST(hash(i) % 1000 AS INTEGER) AS q0"]
    assert len(cols) == COLUMNS
    return "SELECT " + ", ".join(cols) + f" FROM range({rows}) t(i)"


def differences(rows: int, every: int) -> int:
    """Rows that differ between source and target."""
    return len(range(7, rows, every))


def generate(folder: Path, rows: int, every: int = 10_000) -> tuple[Path, Path]:
    """Source and target Parquet files (cached)."""
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / f"source_{rows}.parquet"
    target = folder / f"target_{rows}_{every}.parquet"
    con = duckdb.connect()
    try:
        if not source.exists():
            temp = source.with_suffix(".tmp")
            con.execute(f"COPY ({_select(rows)}) TO '{temp.as_posix()}' (FORMAT parquet)")
            temp.replace(source)
        if not target.exists():
            temp = target.with_suffix(".tmp")
            con.execute(
                f"COPY (SELECT * REPLACE (CASE WHEN id % {every} = 7 THEN s0 || 'x' ELSE s0 END AS s0) "
                f"FROM read_parquet('{source.as_posix()}')) TO '{temp.as_posix()}' (FORMAT parquet)"
            )
            temp.replace(target)
    finally:
        con.close()
    return source, target


def to_duckdb(parquet: Path, database: Path) -> Path:
    """A DuckDB database holding ``parquet`` as table ``wide`` (cached)."""
    if not database.exists():
        temp = database.with_suffix(".tmp")
        con = duckdb.connect(str(temp))
        try:
            con.execute(f"CREATE TABLE wide AS SELECT * FROM read_parquet('{parquet.as_posix()}')")
        finally:
            con.close()
        temp.replace(database)
    return database
