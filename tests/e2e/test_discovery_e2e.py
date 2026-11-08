"""Discovery and generation end to end on DuckDB, SQLite, and files: discover, import-mapping,
import-contract and the contract check, export-contract, profile, and suggest."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import yaml
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.core.enums import Status
from dvf.plugins.registry import Registry
from tests.conftest import EXAMPLES, WriteFile

runner = CliRunner()


def _invoke(*args: str) -> Any:
    return runner.invoke(app, [str(a) for a in args])


def _assert_ran(result: Any) -> None:
    """The run finished with checks evaluated (passed or failed), never with errors."""
    errors = [c for c in result.iter_checks() if c.status is Status.ERROR]
    assert not errors, [(c.check_id, c.message) for c in errors]


def _legacy_duckdb(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, email_address VARCHAR, "
        "country VARCHAR, balance DECIMAL(12,3), created_at TIMESTAMP, status VARCHAR)"
    )
    con.execute(
        "INSERT INTO customers SELECT i, 'user' || i || '@example.com', "
        "CASE WHEN i % 3 = 0 THEN 'DE' WHEN i % 3 = 1 THEN 'FR' ELSE 'US' END, i * 1.125, "
        "TIMESTAMP '2026-01-01 08:00:00' + INTERVAL (i) HOUR, "
        "CASE WHEN i % 2 = 0 THEN 'active' ELSE 'closed' END FROM range(1, 41) t(i)"
    )
    con.execute(
        "CREATE TABLE order_lines (order_id INTEGER, line_no INTEGER, amount DECIMAL(10,2))"
    )
    con.execute("INSERT INTO order_lines SELECT i // 2, i % 2, i * 2.5 FROM range(0, 30) t(i)")
    con.execute("CREATE TABLE products (product_code VARCHAR, label VARCHAR)")
    con.execute("INSERT INTO products SELECT 'P' || i, 'Product ' || i FROM range(1, 11) t(i)")
    con.execute("CREATE TABLE legacy_only (x INTEGER)")
    con.close()


def _target_sqlite(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE CUSTOMERS (CUSTOMER_ID INTEGER PRIMARY KEY, EMAIL TEXT, COUNTRY TEXT, "
        "BALANCE NUMERIC(12,1), CREATED_AT TIMESTAMP, STATUS TEXT, LOADED_AT TIMESTAMP)"
    )
    con.executemany(
        "INSERT INTO CUSTOMERS VALUES (?, ?, ?, ?, ?, ?, '2026-11-01 00:00:00')",
        [
            (
                i,
                f"user{i}@example.com",
                ["FR", "US", "DE"][i % 3 - 1 if i % 3 else 2],
                round(i * 1.125, 1),
                f"2026-01-01 {8 + i // 24:02d}:00:00",
                "active" if i % 2 == 0 else "closed",
            )
            for i in range(1, 41)
        ],
    )
    con.execute("CREATE TABLE OrderLines (order_id INTEGER, line_no INTEGER, amount NUMERIC(10,2))")
    con.executemany(
        "INSERT INTO OrderLines VALUES (?, ?, ?)", [(i // 2, i % 2, i * 2.5) for i in range(30)]
    )
    con.execute("CREATE TABLE product (product_code TEXT, label TEXT)")
    con.executemany(
        "INSERT INTO product VALUES (?, ?)", [(f"P{i}", f"Product {i}") for i in range(1, 11)]
    )
    con.execute("CREATE TABLE new_only (y INTEGER)")
    con.commit()
    con.close()


# ---------------------------------------------------------------------- discover


def test_discover_duckdb_to_sqlite(tmp_path: Path, default_registry: Registry) -> None:
    _legacy_duckdb(tmp_path / "legacy.duckdb")
    _target_sqlite(tmp_path / "target.sqlite")
    out = tmp_path / "gen" / "dvf.yaml"
    result = _invoke(
        "discover",
        "--source",
        tmp_path / "legacy.duckdb",
        "--target",
        tmp_path / "target.sqlite",
        "--probe-keys",
        "-o",
        out,
    )
    assert result.exit_code == 0, result.output
    assert "matched 3 table(s) (2 fuzzy)" in result.output
    text = out.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["connections"]["source"] == {"type": "duckdb", "database": "../legacy.duckdb"}
    tables = {t["name"]: t for t in data["suites"][0]["tables"]}
    assert set(tables) == {"customers", "order_lines", "products"}
    customers = tables["customers"]
    assert customers["keys"] == ["customer_id"]  # names differ only in case
    assert customers["target"] == {"table": "CUSTOMERS"}
    assert tables["products"]["keys"] == [
        {"source": "product_code", "target": "product_code"}
    ] or tables["products"]["keys"] == ["product_code"]
    assert "keys" not in tables["order_lines"]
    assert tables["order_lines"]["comparison"] == {
        "strategies": ["row_count", "aggregate"],
        "on_mismatch": None,
    }
    assert "products -> product: fuzzy match" in text and "REVIEW" in text
    assert "BALANCE: DECIMAL(12,3) -> NUMERIC(12,1) is lossy (scale reduced from 3 to 1)" in text
    assert "Columns only in the source: email_address" in text
    assert "Columns only in the target: EMAIL, LOADED_AT" in text
    assert "#   - legacy_only" in text and "#   - new_only" in text
    assert "unique and never NULL in the source data (probed)" in text
    assert _invoke("validate-config", out).exit_code == 0
    output = api.run(out, write_reports=False, history=False)
    _assert_ran(output.result)
    again = _invoke(
        "discover",
        "--source",
        tmp_path / "legacy.duckdb",
        "--target",
        tmp_path / "target.sqlite",
        "-o",
        out,
    )
    assert again.exit_code == 3 and "already exists" in again.output


def test_discover_from_a_config_and_files(
    tmp_path: Path, write: WriteFile, default_registry: Registry
) -> None:
    landing, curated = tmp_path / "landing", tmp_path / "curated"
    landing.mkdir()
    curated.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE c AS SELECT i AS id, 'n' || i AS name FROM range(1, 21) t(i)")
    con.execute(f"COPY c TO '{(landing / 'customers.csv').as_posix()}' (HEADER)")
    con.execute(f"COPY c TO '{(curated / 'Customers.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"COPY c TO '{(landing / 'extra.csv').as_posix()}' (HEADER)")
    con.close()
    (landing / "notes.md").write_text("not data", encoding="utf-8")
    config = write(
        "project/dvf.yaml",
        f"""
        version: 1
        project: {{name: files}}
        connections:
          landing: {{type: files, path: "{landing.as_posix()}", format: csv}}
          curated: {{type: files, path: ../curated}}
        """,
    )
    out = tmp_path / "project" / "discovered.yaml"
    result = _invoke(
        "discover", config, "--probe-keys", "--format", "json", "-o", out, "--source-schema", "x"
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["source_connection"] == "landing" and summary["unmatched_source_tables"] == [
        "extra.csv"
    ]
    assert summary["tables"][0]["keys"] == [{"source": "id", "target": "id"}]
    assert any("schema filter is ignored" in n for n in summary["notes"])
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["connections"]["curated"]["path"] == "../curated"
    assert data["suites"][0]["tables"][0]["source"] == {"path": "customers.csv"}
    output = api.run(out, write_reports=False, history=False)
    _assert_ran(output.result)
    assert output.result.status is Status.PASSED
    stdout = _invoke("discover", config, "--no-fuzzy")
    assert stdout.exit_code == 0 and "version: 1" in stdout.output
    assert "matched 1 table(s) (0 fuzzy)" in stdout.output  # customers.csv ~ Customers.parquet


def test_discover_errors(tmp_path: Path, write: WriteFile, default_registry: Registry) -> None:
    assert _invoke("discover").exit_code == 3
    assert _invoke("discover", "--source", "oracle://x", "--target", tmp_path).exit_code == 3
    missing = _invoke(
        "discover",
        "--source",
        "sqlite:///" + (tmp_path / "missing.sqlite").as_posix(),
        "--target",
        tmp_path,
    )
    assert missing.exit_code == 2, missing.output
    config = write(
        "three.yaml",
        """
        version: 1
        project: {name: x}
        connections: {a: {type: duckdb}, b: {type: duckdb}, c: {type: duckdb}}
        """,
    )
    ambiguous = _invoke("discover", config)
    assert ambiguous.exit_code == 3 and "--source NAME" in ambiguous.output
    assert _invoke("discover", config, "--source", "a", "--target", "nope").exit_code == 3


# ---------------------------------------------------------------------- import-mapping


def test_imported_mapping_runs(
    tmp_path: Path, write: WriteFile, default_registry: Registry
) -> None:
    con = duckdb.connect(str(tmp_path / "src.duckdb"))
    con.execute("CREATE TABLE kna1 (kunnr VARCHAR, name1 VARCHAR, land1 VARCHAR, mandt VARCHAR)")
    con.execute("INSERT INTO kna1 VALUES ('1', 'Alpha  ', 'DE', '100'), ('2', 'Beta', 'FR', '100')")
    con.close()
    con = duckdb.connect(str(tmp_path / "tgt.duckdb"))
    con.execute(
        "CREATE TABLE customer (customer_id VARCHAR, customer_name VARCHAR, country_code VARCHAR, load_ts TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO customer VALUES ('1', 'Alpha', 'DE', now()), ('2', 'Beta', 'FR', now())"
    )
    con.close()
    sheet = write(
        "mapping.csv",
        """
        source_table,source_column,target_table,target_column,key,normalizers,checks,notes
        kna1,kunnr,customer,customer_id,Y,,not_null; unique,
        kna1,name1,customer,customer_name,,rtrim,not_blank; length:1..35,
        kna1,land1,customer,country_code,,,allowed_values:DE|FR|GB,
        kna1,mandt,customer,,,,,client
        ,,customer,load_ts,,,,audit
        """,
    )
    out = tmp_path / "mapping.yaml"
    result = _invoke(
        "import-mapping",
        sheet,
        "--source",
        tmp_path / "src.duckdb",
        "--target",
        tmp_path / "tgt.duckdb",
        "-o",
        out,
    )
    assert result.exit_code == 0, result.output
    output = api.run(out, write_reports=False, history=False)
    _assert_ran(output.result)
    assert output.result.status is Status.PASSED, [
        (c.check_id, c.message) for c in output.result.iter_checks()
    ]


# ---------------------------------------------------------------------- contracts


def _shop(path: Path, *, broken: bool = False) -> None:
    con = duckdb.connect(str(path))
    email = "VARCHAR(254)" if not broken else "INTEGER"
    con.execute(
        f"CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, email {email} NOT NULL, "
        "country_code VARCHAR, created_at TIMESTAMP NOT NULL)"
    )
    if broken:
        con.execute(
            "INSERT INTO customers SELECT i, i, 'DE', TIMESTAMP '2020-01-01' FROM range(1, 6) t(i)"
        )
    else:
        con.execute(
            "INSERT INTO customers SELECT i, 'c' || i || '@shop.example', "
            "CASE WHEN i % 2 = 0 THEN 'DE' ELSE 'US' END, TIMESTAMP '2020-01-01' + INTERVAL (i) DAY "
            "FROM range(1, 21) t(i)"
        )
    con.execute(
        "CREATE TABLE orders (order_id BIGINT PRIMARY KEY, customer_id INTEGER NOT NULL, status VARCHAR, "
        "amount DECIMAL(12,2), ordered_at TIMESTAMP"
        + (", surprise INTEGER" if broken else "")
        + ")"
    )
    con.execute(
        "INSERT INTO orders SELECT i, 1 + i % 5, CASE WHEN i % 3 = 0 THEN 'paid' ELSE 'new' END, "
        "i * 10.5, TIMESTAMP '2021-01-01' + INTERVAL (i) DAY"
        + (", 1" if broken else "")
        + " FROM range(1, 51) t(i)"
    )
    con.close()


def test_imported_contract_runs_and_conformance(tmp_path: Path, default_registry: Registry) -> None:
    work = tmp_path / "contracts"
    work.mkdir()
    shutil.copy(EXAMPLES / "discovery" / "orders.odcs.yaml", work / "orders.odcs.yaml")
    _shop(work / "shop.duckdb")
    out = tmp_path / "checks.yaml"
    result = _invoke("import-contract", work / "orders.odcs.yaml", "--server", "local", "-o", out)
    assert result.exit_code == 0, result.output
    assert "not converted" in result.output and "engine 'soda'" in result.output
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["connections"]["local"]["database"] == "contracts/shop.duckdb"
    output = api.run(out, write_reports=False, history=False)
    _assert_ran(output.result)
    checks = {c.check_id: c for c in output.result.iter_checks()}
    conform = checks["shop_orders.customers.contract"]
    assert conform.status is Status.PASSED, conform.message
    orders_conform = checks["shop_orders.orders.contract"]
    assert orders_conform.status is Status.PASSED, orders_conform.message
    failed = [c.check_id for c in output.result.iter_checks() if c.status is Status.FAILED]
    assert failed == [], failed

    _shop(work / "broken.duckdb", broken=True)
    data["connections"]["local"]["database"] = "contracts/broken.duckdb"
    broken_config = tmp_path / "broken.yaml"
    broken_config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    broken = api.run(broken_config, write_reports=False, history=False)
    _assert_ran(broken.result)
    checks = {c.check_id: c for c in broken.result.iter_checks()}
    customers = checks["shop_orders.customers.contract"]
    assert customers.status is Status.FAILED and "type email" in (customers.message or "")
    assert customers.samples is not None and customers.samples.rows[0][:2] == ["email", "type"]
    orders = checks["shop_orders.orders.contract"]
    assert orders.status is Status.WARNING and "extra surprise" in (orders.message or "")


def test_contract_check_on_sqlite_with_an_expected_schema(
    tmp_path: Path, write: WriteFile, default_registry: Registry
) -> None:
    con = sqlite3.connect(tmp_path / "app.sqlite")
    con.execute(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name VARCHAR(40), balance NUMERIC(10,2) NOT NULL)"
    )
    con.execute("INSERT INTO accounts VALUES (1, 'a', 1.5)")
    con.commit()
    con.close()
    write(
        "schemas/accounts.yaml",
        """
        columns:
          - {name: id, type: INTEGER, nullable: false}
          - {name: name, type: VARCHAR(40), nullable: false}
          - {name: balance, type: "DECIMAL(10,2)"}
          - {name: opened, logical_type: date}
        primary_key: [id]
        """,
    )
    config = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: sch12}
        connections: {app: {type: sqlite, database: app.sqlite}}
        suites:
          - name: app
            source: app
            tables:
              - name: accounts
                source: {table: accounts}
                checks:
                  - {type: contract, contract: schemas/accounts.yaml, nullability: error}
                  - {type: contract, name: relaxed, contract: schemas/accounts.yaml, nullability: ignore, case_sensitive: true}
                  - {type: contract, name: missing, contract: schemas/nope.yaml}
        """,
    )
    output = api.run(config, write_reports=False, history=False)
    checks = {c.check_id: c for c in output.result.iter_checks()}
    strict = checks["app.accounts.contract"]
    assert strict.status is Status.FAILED
    rows = {(r[0], r[1]) for r in strict.samples.rows}  # type: ignore[union-attr]
    assert rows == {("opened", "missing"), ("name", "nullability")}
    assert strict.details["format"] == "expected_schema"
    assert checks["app.accounts.contract.relaxed"].status is Status.FAILED
    assert checks["app.accounts.contract.missing"].status is Status.ERROR
    listed = _invoke("plugins", "list", "--kind", "check", "--format", "json")
    entry = next(c for c in json.loads(listed.output)["components"] if c["name"] == "contract")
    assert entry["status"] == "ready"


def test_export_contract_round_trip(
    tmp_path: Path, write: WriteFile, default_registry: Registry
) -> None:
    _shop(tmp_path / "shop.duckdb")
    config = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: shop, description: Web shop}
        connections: {shop: {type: duckdb, database: shop.duckdb}}
        suites:
          - name: shop
            source: shop
            tables:
              - name: orders
                source: {table: orders}
                checks:
                  - {type: not_null, columns: [status]}
                  - {type: unique, columns: [order_id, customer_id]}
                  - {type: allowed_values, column: status, values: [new, paid], mostly: 0.99}
                  - {type: regex, column: status, pattern: "[a-z]+"}
                  - {type: range, column: amount, min: 0, max: 1000}
                  - {type: length, column: status, max: 10}
                  - {type: null_ratio, column: amount, max_ratio: 0.1}
                  - {type: row_count_range, min: 1, max: 100}
                  - {type: custom_sql, name: positive, sql: "SELECT COUNT(*) FROM {table} WHERE amount < 0", returns: scalar, expect: {operator: "==", value: 0}}
                  - {type: custom_sql, name: no_rows, sql: "SELECT * FROM {table} WHERE amount < 0"}
                  - {type: referential_integrity, column: customer_id, parent: {table: customers, column: customer_id}}
                  - {type: expression, name: sane, expression: "amount >= 0"}
                  - {type: not_null, name: filtered, columns: [amount], where: "status = 'paid'"}
        """,
    )
    contract = tmp_path / "out" / "shop.odcs.yaml"
    result = _invoke(
        "export-contract", config, "-o", contract, "--id", "shop-1", "--version", "2.0.0"
    )
    assert result.exit_code == 0, result.output
    assert "not exported" in result.output and "'expression'" in result.output
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    assert data["apiVersion"] == "v3.1.0" and data["id"] == "shop-1" and data["version"] == "2.0.0"
    assert data["servers"] == [{"server": "shop", "type": "duckdb", "database": "shop.duckdb"}]
    orders = data["schema"][0]
    props = {p["name"]: p for p in orders["properties"]}
    assert props["order_id"]["primaryKey"] is True and props["order_id"]["required"] is True
    assert "required" not in props["status"] and props["status"]["logicalTypeOptions"] == {
        "maxLength": 10
    }
    assert {"metric": "nullValues", "mustBe": 0} in props["status"]["quality"]
    assert props["amount"]["logicalTypeOptions"] == {"minimum": 0, "maximum": 1000}
    assert props["customer_id"]["relationships"] == [{"to": "customers.customer_id"}]
    shutil.copy(tmp_path / "shop.duckdb", contract.parent / "shop.duckdb")
    imported = tmp_path / "imported.yaml"
    assert _invoke("import-contract", contract, "-o", imported).exit_code == 0
    output = api.run(imported, write_reports=False, history=False)
    _assert_ran(output.result)
    bad = [
        (c.check_id, c.message)
        for c in output.result.iter_checks()
        if c.status is not Status.PASSED
    ]
    assert bad == [], bad
    both = _invoke("export-contract", config, "--side", "both")
    assert both.exit_code == 3


# ---------------------------------------------------------------------- profile and suggest


def test_profile_and_suggest_on_the_example(tmp_path: Path, default_registry: Registry) -> None:
    project = tmp_path / "demo"
    assert _invoke("init", project, "--example").exit_code == 0
    config = project / "dvf.yaml"
    text = _invoke("profile", config, "--table", "customers", "--top", "3")
    assert text.exit_code == 0, text.output
    assert "customers.customers (target" in text.output and "rows" in text.output
    profile_json = tmp_path / "profile.json"
    result = _invoke("profile", config, "--side", "both", "--format", "json", "-o", profile_json)
    assert result.exit_code == 0, result.output
    document = json.loads(profile_json.read_text(encoding="utf-8"))
    sides = {(t["table"], t["side"]) for t in document["tables"]}
    assert ("customers", "source") in sides and ("orders", "target") in sides
    customers = next(
        t for t in document["tables"] if t["table"] == "customers" and t["side"] == "target"
    )
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert email["formats"].get("email") and email["masked"] is True
    assert all("***" in str(v) for v, _ in email["top_values"])
    suggested = tmp_path / "suggested.yaml"
    result = _invoke("suggest", config, "-o", suggested, "--margin", "0.05")
    assert result.exit_code == 0, result.output
    fragment = yaml.safe_load(suggested.read_text(encoding="utf-8"))
    assert "# evidence (target):" in suggested.read_text(encoding="utf-8")
    # Merge the suggestions into the example and run only them: they must pass on today's data.
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    by_table = {
        (s["name"], t["name"]): t["checks"] for s in fragment["suites"] for t in s["tables"]
    }
    for suite in data["suites"]:
        for table in suite["tables"]:
            table["checks"] = by_table.get((suite["name"], table["name"]), [])
            table["comparison"] = {"enabled": False}
            table["schema_checks"] = []
    data["waivers"] = []
    merged = project / "merged.yaml"
    merged.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert _invoke("validate-config", merged).exit_code == 0
    output = api.run(merged, write_reports=False, history=False)
    types = {c.type for c in output.result.iter_checks()}
    assert {"not_null", "allowed_values", "regex", "range", "length"} <= types
    bad = [
        (c.check_id, c.message)
        for c in output.result.iter_checks()
        if c.status is not Status.PASSED
    ]
    assert bad == [], bad
    from_file = _invoke("suggest", "--from-profile", profile_json, "--format", "json")
    assert from_file.exit_code == 0 and json.loads(from_file.output)["suggestions"]
    assert _invoke("suggest").exit_code == 3
    assert _invoke("profile", config, "--table", "nope").exit_code == 3


def test_profile_sqlite_privacy_and_missing_tables(
    tmp_path: Path, write: WriteFile, default_registry: Registry
) -> None:
    con = sqlite3.connect(tmp_path / "p.sqlite")
    con.execute(
        "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, zip TEXT, joined DATE, score REAL)"
    )
    con.executemany(
        "INSERT INTO people VALUES (?, ?, ?, ?, ?)",
        [
            (i, f"Name {i % 4}", f"{10000 + i}", f"2026-0{1 + i % 9}-1{i % 9}", i / 3)
            for i in range(1, 31)
        ],
    )
    con.commit()
    con.close()
    config = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: people}
        defaults: {privacy: {pii_columns: [name]}}
        connections: {db: {type: sqlite, database: p.sqlite}}
        suites:
          - name: s
            source: db
            tables:
              - {name: people, source: {table: people}}
              - {name: ghost, source: {table: ghost}}
        """,
    )
    report = api.profile(config, top=10)
    people = next(t for t in report.tables if t.table == "people")
    name = people.column("name")
    assert name is not None and name.masked and all(v == "***" for v, _ in name.top_values)
    zip_code = people.column("zip")
    assert zip_code is not None and zip_code.format_of_all() == "us_zip"
    assert next(t for t in report.tables if t.table == "ghost").error
    result = api.suggest(profile_report=report)
    checks = [s.check for s in result.suggestions]
    assert not any(c["type"] == "allowed_values" and c["column"] == "name" for c in checks)
    assert any(c["type"] == "regex" and c["column"] == "zip" for c in checks)
    assert result.skipped and "ghost" in result.skipped[0]
    cli = _invoke("profile", config)
    assert cli.exit_code == 2 and "error" in cli.output
    strict = write(
        "strict.yaml",
        (tmp_path / "dvf.yaml")
        .read_text(encoding="utf-8")
        .replace("privacy: {pii_columns: [name]}", "privacy: {mode: strict}"),
    )
    hidden = api.profile(strict, tables=["people"])
    assert all(c.min is None and not c.top_values for c in hidden.tables[0].columns)
    assert _invoke("suggest", strict, "--table", "people").exit_code == 0
