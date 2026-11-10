"""The seeded-defect harness through the generic ``sqlalchemy`` and ``odbc`` connectors.

Every injected defect must be found exactly and classified as with the native connectors: the
source is SQLite (through SQLAlchemy) or PostgreSQL (through SQLAlchemy with psycopg 3, and through
the psqlODBC driver); the target is Parquet.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from dvf.scaffold.seeded import load_postgres, load_sqlite
from tests.e2e.test_seeded_defects import DATA, _config, _run, _target_files, assert_exact
from tests.unit.test_catalog_generic import odbc_connection_string, sqlalchemy_postgres_url

_KEYS = {"customers": "customer_id", "orders": "order_id"}
_TABLES = {"customers": DATA.source_customers, "orders": DATA.source_orders}


@pytest.mark.parametrize("engine", ["duckdb", "polars", "auto"])
def test_seeded_defects_sqlalchemy_sqlite(engine: str, tmp_path: Path) -> None:
    target = _target_files(tmp_path, DATA)
    load_sqlite(_TABLES, tmp_path / "source.sqlite", _KEYS)
    source = {"type": "sqlalchemy", "url": "sqlite:///source.sqlite"}
    output = _run(
        _config(source, {"table": "customers"}, {"table": "orders"}, target, engine), tmp_path
    )
    assert_exact(output.result, DATA)


@pytest.fixture(scope="module")
def pg_seeded(postgres_url: str) -> Iterator[str]:
    import psycopg

    schema = "p4_cat_seeded"
    load_postgres(_TABLES, postgres_url, schema, _KEYS)
    try:
        yield schema
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.mark.integration
@pytest.mark.parametrize("engine", ["duckdb", "polars"])
@pytest.mark.parametrize("kind", ["sqlalchemy", "odbc"])
def test_seeded_defects_generic_postgres(
    kind: str, engine: str, pg_seeded: str, postgres_url: str, tmp_path: Path
) -> None:
    target = _target_files(tmp_path, DATA)
    source = (
        {"type": "sqlalchemy", "url": sqlalchemy_postgres_url(postgres_url)}
        if kind == "sqlalchemy"
        else {"type": "odbc", "connection_string": odbc_connection_string(postgres_url)}
    )
    config = _config(
        source,
        {"table": f"{pg_seeded}.customers"},
        {"table": f"{pg_seeded}.orders"},
        target,
        engine,
    )
    assert_exact(_run(config, tmp_path).result, DATA)


def _sqlalchemy_url(server: str, url: str) -> str:
    """A dvf server URL (tests/server_backends.py) as a SQLAlchemy URL."""
    parts = urlsplit(url)
    auth = f"{parts.username}:{parts.password}@{parts.hostname}:{parts.port}"
    if server == "mysql":
        return f"mysql+pymysql://{auth}{parts.path}"
    return f"oracle+oracledb://{auth}/?service_name={parts.path.lstrip('/')}"


@pytest.mark.integration
@pytest.mark.parametrize("server", ["mysql", "oracle"])
def test_seeded_defects_sqlalchemy_servers(server: str, tmp_path: Path) -> None:
    """MySQL (PyMySQL) and Oracle (python-oracledb) through SQLAlchemy (DVF_TEST_*_URL)."""
    import secrets

    from tests.server_backends import drop_seeded, load_seeded, make_backend, server_url

    backend = make_backend(server)
    namespace = f"p4_cat_{secrets.token_hex(2)}"
    try:
        names = load_seeded(backend, namespace, _TABLES, _KEYS)
        target = _target_files(tmp_path, DATA)
        source = {"type": "sqlalchemy", "url": _sqlalchemy_url(server, server_url(server))}
        config = _config(
            source, {"table": names["customers"]}, {"table": names["orders"]}, target, "duckdb"
        )
        assert_exact(_run(config, tmp_path).result, DATA)
    finally:
        drop_seeded(backend, namespace, list(_TABLES))
        backend.close()
