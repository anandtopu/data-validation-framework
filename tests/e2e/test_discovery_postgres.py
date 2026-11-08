"""Discovery tools against a live PostgreSQL server (DVF_TEST_POSTGRES_URL).

Uses the schemas p3_disc_src and p3_disc_tgt, created and dropped by the tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.core.enums import Status
from dvf.plugins.registry import Registry
from tests.conftest import WriteFile

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DVF_TEST_POSTGRES_URL"), reason="DVF_TEST_POSTGRES_URL is not set"
    ),
]
runner = CliRunner()
SCHEMAS = ("p3_disc_src", "p3_disc_tgt")


@pytest.fixture
def pg(postgres_url: str) -> Iterator[str]:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(postgres_url, autocommit=True) as con:
        for schema in SCHEMAS:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            con.execute(f"CREATE SCHEMA {schema}")
        con.execute(
            "CREATE TABLE p3_disc_src.customers (customer_id integer PRIMARY KEY, "
            "email varchar(120) NOT NULL, full_name varchar(50), country char(2), "
            "balance numeric(12,2), created_at timestamp)"
        )
        con.execute(
            "INSERT INTO p3_disc_src.customers SELECT i, 'user' || i || '@example.com', "
            "'Name ' || i, (ARRAY['DE','FR','US'])[1 + i % 3], i * 1.25, "
            "timestamp '2026-01-01' + i * interval '1 hour' FROM generate_series(1, 30) i"
        )
        con.execute("CREATE TABLE p3_disc_src.order_lines (order_id int, line_no int, qty int)")
        con.execute(
            "INSERT INTO p3_disc_src.order_lines SELECT i / 2, i % 2, i FROM generate_series(0, 19) i"
        )
        con.execute("CREATE TABLE p3_disc_src.legacy_only (x int)")
        con.execute(
            'CREATE TABLE p3_disc_tgt."Customers" ("CustomerId" bigint PRIMARY KEY, '
            "email varchar(120) NOT NULL, fullname varchar(20), country char(2), "
            "balance numeric(12,2), created_at timestamp, loaded_at timestamp)"
        )
        con.execute(
            'INSERT INTO p3_disc_tgt."Customers" SELECT customer_id, email, left(full_name, 20), '
            "country, balance, created_at, now() FROM p3_disc_src.customers"
        )
        con.execute("CREATE TABLE p3_disc_tgt.orderlines (order_id int, line_no int, qty int)")
        con.execute("INSERT INTO p3_disc_tgt.orderlines SELECT * FROM p3_disc_src.order_lines")
    try:
        yield postgres_url
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as con:
            for schema in SCHEMAS:
                con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _no_errors(result: Any) -> None:
    errors = [(c.check_id, c.message) for c in result.iter_checks() if c.status is Status.ERROR]
    assert not errors, errors


def test_discover_postgres_schemas(
    pg: str,
    write: WriteFile,
    tmp_path: Path,
    default_registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P3_DISC_PG_URL", pg)
    config = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: pg}
        connections:
          legacy: {type: postgres, url: "${secret:env:P3_DISC_PG_URL}", schema: p3_disc_src}
          modern: {type: postgres, url: "${secret:env:P3_DISC_PG_URL}", schema: p3_disc_tgt}
        """,
    )
    out = tmp_path / "out" / "dvf.yaml"
    result = runner.invoke(
        app,
        [
            "discover",
            str(config),
            "--source",
            "legacy",
            "--target",
            "modern",
            "--source-schema",
            "p3_disc_src",
            "--target-schema",
            "p3_disc_tgt",
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert pg.split("@")[0] not in text and "${secret:env:P3_DISC_PG_URL}" in text
    tables = {t["name"]: t for t in yaml.safe_load(text)["suites"][0]["tables"]}
    customers = tables["customers"]
    assert customers["source"] == {"table": "p3_disc_src.customers"}
    assert customers["target"] == {"table": 'p3_disc_tgt."Customers"'}
    assert customers["keys"] == [{"source": "customer_id", "target": "CustomerId"}]
    assert {"source": "full_name", "target": "fullname"} in customers["columns"]["mappings"]
    assert "full_name: character varying(50) -> character varying(20) is lossy" in text or (
        "fullname: character varying(50) -> character varying(20) is lossy" in text
    )
    assert "order_lines -> p3_disc_tgt.orderlines: fuzzy match" in text
    assert "p3_disc_src.legacy_only" in text
    assert runner.invoke(app, ["validate-config", str(out)]).exit_code == 0
    output = api.run(out, write_reports=False, history=False)
    _no_errors(output.result)
    checks = {c.check_id: c for c in output.result.iter_checks()}
    assert checks["discovered.customers.schema.columns_present"].status is Status.PASSED


def test_discover_duckdb_to_postgres_with_a_password_url(
    pg: str, tmp_path: Path, default_registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = duckdb.connect(str(tmp_path / "legacy.duckdb"))
    con.execute("CREATE TABLE orderlines (order_id INTEGER, line_no INTEGER, qty INTEGER)")
    con.execute("INSERT INTO orderlines SELECT i // 2, i % 2, i FROM range(0, 20) t(i)")
    con.close()
    out = tmp_path / "dvf.yaml"
    result = api.discover(
        source=str(tmp_path / "legacy.duckdb"),
        target=pg,
        target_schema="p3_disc_tgt",
        output=out,
    )
    assert [t.target for t in result.tables] == ["p3_disc_tgt.orderlines"]
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    if "@" in pg and ":" in pg.split("@")[0].split("//")[-1]:
        assert data["connections"]["target"]["url"] == "${secret:env:TARGET_DATABASE_URL}"
        monkeypatch.setenv("TARGET_DATABASE_URL", pg)
    output = api.run(out, write_reports=False, history=False)
    _no_errors(output.result)
    assert output.result.status is Status.PASSED


def test_profile_suggest_and_contract_on_postgres(
    pg: str,
    write: WriteFile,
    tmp_path: Path,
    default_registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P3_DISC_PG_URL", pg)
    write(
        "customers.schema.yaml",
        """
        dialect: postgres
        columns:
          - {name: customer_id, type: integer, nullable: false}
          - {name: email, type: varchar(120), nullable: false}
          - {name: full_name, type: varchar(50), nullable: false}
          - {name: country, type: char(2)}
          - {name: balance, type: "numeric(12,2)"}
          - {name: created_at, type: timestamp}
        primary_key: [customer_id]
        """,
    )
    config = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: pg-profile}
        connections:
          pg: {type: postgres, url: "${secret:env:P3_DISC_PG_URL}"}
        suites:
          - name: crm
            source: pg
            tables:
              - name: customers
                source: {table: p3_disc_src.customers}
                checks:
                  - {type: contract, contract: customers.schema.yaml}
        """,
    )
    report = api.profile(config, top=5)
    customers = report.tables[0]
    assert customers.row_count == 30 and customers.dialect == "postgres"
    country = customers.column("country")
    assert country is not None and country.distinct_count == 3 and len(country.top_values) == 3
    email = customers.column("email")
    assert email is not None and email.format_of_all() == "email"
    suggestions = api.suggest(profile_report=report)
    checks = [s.check for s in suggestions.suggestions]
    assert {"type": "allowed_values", "column": "country", "values": ["DE", "FR", "US"]} in checks
    assert {"type": "unique", "columns": ["customer_id"]} in checks
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["suites"][0]["tables"][0]["checks"] += checks
    merged = tmp_path / "merged.yaml"
    merged.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    output = api.run(merged, write_reports=False, history=False)
    _no_errors(output.result)
    results = {c.check_id: c for c in output.result.iter_checks()}
    contract = results.pop("crm.customers.contract")
    assert contract.status is Status.WARNING and "nullability full_name" in (contract.message or "")
    failing = [(k, c.message) for k, c in results.items() if c.status is not Status.PASSED]
    assert failing == [], failing
    exported = tmp_path / "contract.yaml"
    assert runner.invoke(app, ["export-contract", str(config), "-o", str(exported)]).exit_code == 0
    contract_doc = yaml.safe_load(exported.read_text(encoding="utf-8"))
    server = contract_doc["servers"][0]
    assert server["type"] == "postgresql" and "password" not in server and "user" not in server
    props = {p["name"]: p for p in contract_doc["schema"][0]["properties"]}
    assert props["customer_id"]["primaryKey"] is True and props["email"]["required"] is True
    assert props["full_name"]["logicalTypeOptions"] == {"maxLength": 50}
