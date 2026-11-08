"""SAP business object templates: each loads through ``extends:`` with its variables filled, and
the GL, open item, and CVI templates run end to end on synthetic DuckDB data."""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.packs.sap.catalog import TEMPLATES, copy_template, template_path

VARIABLES: dict[str, dict[str, Any]] = {
    "gl_balances": {
        "source_table": "FAGLFLEXA",
        "target_table": "ACDOCA",
        "ledger": "0L",
        "company_codes": "'1000', '2000'",
        "fiscal_year": "2025",
        "amount_tolerance": 0.005,
    },
    "open_items": {
        "target_prefix": "",
        "company_codes_filter": "BUKRS IN ('1000', '2000')",
        "amount_side": "both",
    },
    "material_master": {
        "target_prefix": "",
        "plants_filter": "WERKS IN ('1000')",
        "sales_orgs_filter": "VKORG IN ('1000')",
    },
    "cvi_business_partner": {},
}


def _main_config(folder: Path, name: str, source_db: Path, target_db: Path) -> Path:
    template = copy_template(name, folder / "sap")
    variables = {"sap_connection": "ecc", "target_connection": "s4", **VARIABLES[name]}
    lines = "\n".join(
        f"  {k}: {v!r}" if isinstance(v, str) else f"  {k}: {v}" for k, v in variables.items()
    )
    main = folder / f"{name}.yaml"
    main.write_text(
        textwrap.dedent(
            f"""\
            version: 1
            extends: [sap/{template.name}]
            project: {{name: sap-template-{name}}}
            vars:
            """
        )
        + lines
        + textwrap.dedent(
            f"""
            connections:
              ecc: {{type: duckdb, database: "{source_db.as_posix()}"}}
              s4: {{type: duckdb, database: "{target_db.as_posix()}"}}
            """
        ),
        encoding="utf-8",
    )
    return main


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_template_loads_with_variables_filled(name: str, tmp_path: Path) -> None:
    template = TEMPLATES[name]
    assert set(template.variables) == {"sap_connection", "target_connection", *VARIABLES[name]}
    main = _main_config(tmp_path, name, tmp_path / "src.duckdb", tmp_path / "tgt.duckdb")
    loaded = api.load_config(main)
    suite = loaded.config.suites[0]
    assert suite.source == "ecc" and suite.target == "s4"
    assert suite.tables
    text = loaded.config.model_dump_json()
    assert "${var:" not in text


def test_template_lookup_and_copy(tmp_path: Path) -> None:
    assert template_path("gl_balances").is_file()
    with pytest.raises(KeyError, match="available"):
        template_path("nope")
    copy_template("open_items", tmp_path)
    with pytest.raises(FileExistsError):
        copy_template("open_items", tmp_path)
    assert copy_template("open_items", tmp_path, force=True).is_file()


def _run(main: Path) -> dict[str, dict[str, Any]]:
    out = api.run(api.load_config(main), write_reports=False, history=False)
    checks: dict[str, dict[str, Any]] = {}
    for dataset in out.result.suites[0].datasets:
        checks[dataset.name] = {
            (c.name if c.type == "custom_sql" else c.type): c for c in dataset.checks
        }
    return checks


def test_gl_balances_template_runs(tmp_path: Path) -> None:
    src, tgt = tmp_path / "ecc.duckdb", tmp_path / "s4.duckdb"
    lines = [
        ("1000", "2025", "001", "0000400000", Decimal("100.00")),
        ("1000", "2025", "001", "0000113100", Decimal("-100.00")),
        ("1000", "2025", "002", "0000400000", Decimal("55.50")),
        ("1000", "2025", "002", "0000113100", Decimal("-55.50")),
        ("2000", "2025", "001", "0000400000", Decimal("10.00")),
        ("2000", "2025", "001", "0000113100", Decimal("-10.00")),
        ("3000", "2025", "001", "0000400000", Decimal("999.00")),  # not selected
    ]
    with duckdb.connect(str(src)) as db:
        db.execute(
            "CREATE TABLE FAGLFLEXA (RLDNR VARCHAR, RBUKRS VARCHAR, RYEAR VARCHAR, POPER VARCHAR, "
            "RACCT VARCHAR, HSL DECIMAL(23, 2))"
        )
        db.executemany("INSERT INTO FAGLFLEXA VALUES ('0L', ?, ?, ?, ?, ?)", lines)
    with duckdb.connect(str(tgt)) as db:
        db.execute(
            "CREATE TABLE ACDOCA (RLDNR VARCHAR, RBUKRS VARCHAR, GJAHR VARCHAR, POPER VARCHAR, "
            "RACCT VARCHAR, HSL DECIMAL(23, 2))"
        )
        changed = [list(r) for r in lines]
        changed[2][4] = Decimal("55.51")  # one balance differs by more than the tolerance
        changed[3][4] = Decimal("-55.51")
        changed[4][4] = Decimal("10.004")  # within the tolerance (DECIMAL(23, 2) stores 10.00)
        db.executemany("INSERT INTO ACDOCA VALUES ('0L', ?, ?, ?, ?, ?)", changed)
    checks = _run(_main_config(tmp_path, "gl_balances", src, tgt))["gl_balances"]
    assert checks["compare.row_count"].status.value == "passed"
    assert checks["compare.column_mismatch"].status.value == "failed"
    assert checks["compare.column_mismatch"].column_mismatches[0].mismatch_count == 2
    assert checks["trial_balance_source"].status.value == "passed"
    assert checks["trial_balance_target"].status.value == "passed"


def test_open_items_template_runs(tmp_path: Path) -> None:
    src, tgt = tmp_path / "ecc.duckdb", tmp_path / "s4.duckdb"
    columns = (
        "BUKRS VARCHAR, BELNR VARCHAR, GJAHR VARCHAR, BUZEI VARCHAR, {party} VARCHAR, "
        "BUDAT VARCHAR, ZFBDT VARCHAR, WAERS VARCHAR, WRBTR DECIMAL(13, 2), SHKZG VARCHAR"
    )
    rows = [
        (
            "1000",
            "0100000001",
            "2025",
            "001",
            "0000001000",
            "20250115",
            "20250115",
            "EUR",
            Decimal("10.00"),
            "S",
        ),
        (
            "1000",
            "0100000002",
            "2025",
            "001",
            "0000001001",
            "20250116",
            "00000000",
            "JPY",
            Decimal("1.00"),
            "S",
        ),
        (
            "2000",
            "0100000003",
            "2025",
            "002",
            "0000001002",
            "20250117",
            "20250117",
            "KWD",
            Decimal("12.34"),
            "H",
        ),
    ]
    for path, party_rows in ((src, rows), (tgt, rows)):
        with duckdb.connect(str(path)) as db:
            for table, party in (("BSID", "KUNNR"), ("BSIK", "LIFNR")):
                db.execute(f"CREATE TABLE {table} ({columns.format(party=party)})")
                db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * 10)})", party_rows)
    with duckdb.connect(str(tgt)) as db:
        db.execute("UPDATE BSIK SET LIFNR = '1001' WHERE BELNR = '0100000002'")  # ALPHA-stripped
        db.execute("UPDATE BSID SET WRBTR = 1.01 WHERE BELNR = '0100000002'")  # 101 JPY, not 100
    checks = _run(_main_config(tmp_path, "open_items", src, tgt))
    vendor = checks["vendor_open_items"]
    assert vendor["compare.column_mismatch"].status.value == "passed"
    customer = checks["customer_open_items"]
    mismatch = customer["compare.column_mismatch"]
    assert mismatch.status.value == "failed"
    assert [m.column for m in mismatch.column_mismatches] == ["WRBTR"]


def test_cvi_template_runs(tmp_path: Path) -> None:
    src, tgt = tmp_path / "ecc.duckdb", tmp_path / "s4.duckdb"
    with duckdb.connect(str(src)) as db:
        db.execute("CREATE TABLE KNA1 (KUNNR VARCHAR, NAME1 VARCHAR)")
        db.execute(
            "INSERT INTO KNA1 VALUES ('0000001000', 'A'), ('0000001001', 'B'), ('0000001002', 'C')"
        )
        db.execute("CREATE TABLE LFA1 (LIFNR VARCHAR, NAME1 VARCHAR)")
        db.execute("INSERT INTO LFA1 VALUES ('0000002000', 'V')")
    with duckdb.connect(str(tgt)) as db:
        db.execute("CREATE TABLE CVI_CUST_LINK (PARTNER_GUID VARCHAR, CUSTOMER VARCHAR)")
        db.execute("INSERT INTO CVI_CUST_LINK VALUES ('g1', '0000001000'), ('g2', '1001')")
        db.execute("CREATE TABLE CVI_VEND_LINK (PARTNER_GUID VARCHAR, VENDOR VARCHAR)")
        db.execute("INSERT INTO CVI_VEND_LINK VALUES ('g3', '0000002000')")
        db.execute("CREATE TABLE BUT000 (PARTNER VARCHAR, PARTNER_GUID VARCHAR)")
        db.execute("INSERT INTO BUT000 VALUES ('1000', 'g1'), ('2000', 'g3')")
    checks = _run(_main_config(tmp_path, "cvi_business_partner", src, tgt))
    customers = checks["customers_to_bp"]
    assert customers["compare.row_count"].status.value == "failed"
    assert customers["compare.keys"].unexpected_count == 1  # customer 1002 has no BP
    assert customers["customer_links_without_bp"].status.value == "failed"  # g2 not in BUT000
    vendors = checks["vendors_to_bp"]
    assert vendors["compare.row_count"].status.value == "passed"
    assert vendors["vendor_links_without_bp"].status.value == "passed"


runner = CliRunner()


def test_cli_lists_and_copies_templates(tmp_path: Path) -> None:
    listed = runner.invoke(app, ["sap", "templates"])
    assert listed.exit_code == 0, listed.output
    assert "gl_balances" in listed.output and "vars:" in listed.output
    copied = runner.invoke(app, ["sap", "copy-template", "gl_balances", "--to", str(tmp_path)])
    assert copied.exit_code == 0, copied.output
    assert (tmp_path / "gl_balances.yaml").is_file()
    again = runner.invoke(app, ["sap", "copy-template", "gl_balances", "--to", str(tmp_path)])
    assert again.exit_code == 2
    unknown = runner.invoke(app, ["sap", "copy-template", "nope", "--to", str(tmp_path)])
    assert unknown.exit_code == 2
    backends = runner.invoke(app, ["sap", "backends"])
    assert backends.exit_code == 0
    assert "nwrfcsdk: declared" in backends.output and "odata: ready" in backends.output
