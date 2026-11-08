"""dvf import-mapping: S2T sheets (CSV and Excel) to configuration, with row-level errors."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.core.errors import ConfigError
from dvf.discovery.mapping import HEADER_ALIASES, iter_aliases, read_sheet
from dvf.plugins.registry import Registry
from tests.conftest import EXAMPLES, WriteFile

runner = CliRunner()
MAPPING_CSV = EXAMPLES / "discovery" / "s2t_mapping.csv"
MAPPING_XLSX = EXAMPLES / "discovery" / "s2t_mapping.xlsx"


def _tables(text: str) -> dict[str, dict]:  # type: ignore[type-arg]
    data = yaml.safe_load(text)
    return {t["name"]: t for s in data["suites"] for t in s["tables"]}


def test_example_csv_converts_and_validates(tmp_path: Path, default_registry: Registry) -> None:
    out = tmp_path / "mapping.yaml"
    result = api.import_mapping(MAPPING_CSV, output=out)
    assert result.tables == 2 and result.columns == 13 and result.output == out
    assert api.validate_config(out).ok
    tables = _tables(out.read_text(encoding="utf-8"))
    customer = tables["CUSTOMER"]
    assert customer["source"] == {"table": "KNA1"} and customer["target"] == {
        "table": "CORE.CUSTOMER"
    }
    assert customer["keys"] == [
        {"source": "KUNNR", "target": "CUSTOMER_ID", "normalize": ["sap_alpha"]}
    ]
    assert customer["columns"]["exclude"] == ["MANDT", "LOAD_TS"]
    mappings = {m["target"]: m for m in customer["columns"]["mappings"]}
    assert mappings["CUSTOMER_NAME"] == {
        "source": "NAME1",
        "target": "CUSTOMER_NAME",
        "normalize": ["rtrim"],
        "pii": True,
    }
    assert mappings["FULL_ADDRESS"]["source_expr"] == "TRIM(STRAS) || ' ' || TRIM(ORT01)"
    checks = {(c["type"], c.get("column") or "+".join(c["columns"])): c for c in customer["checks"]}
    assert checks[("regex", "VAT_ID")]["pattern"] == "^[A-Z]{2}[0-9A-Z]{2,12}$"
    assert checks[("length", "CUSTOMER_NAME")] == {
        "type": "length",
        "column": "CUSTOMER_NAME",
        "min": 1,
        "max": 35,
    }
    order = tables["SALES_ORDER"]
    assert {"type": "not_null", "columns": ["ORDER_ID", "CUSTOMER_ID"]} in order["checks"]
    assert {"source": "NETWR", "target": "NET_VALUE", "normalize": [{"round": 2}]} in order[
        "columns"
    ]["mappings"]
    assert {"type": "range", "column": "NET_VALUE", "min": 0} in order["checks"]
    text = out.read_text(encoding="utf-8")
    assert "# row 8: source column MANDT is not migrated" in text
    assert "REVIEW: placeholder" in text and "transformation: Trim trailing blanks" in text


def test_example_xlsx_finds_the_mapping_sheet(tmp_path: Path, default_registry: Registry) -> None:
    result = api.import_mapping(MAPPING_XLSX)
    assert result.sheet == "Mapping" and result.tables == 2
    assert _tables(result.text) == _tables(api.import_mapping(MAPPING_CSV).text)
    named = api.import_mapping(MAPPING_XLSX, sheet="mapping")
    assert named.sheet == "Mapping"
    with pytest.raises(ConfigError, match="sheet 'Nope' not found"):
        api.import_mapping(MAPPING_XLSX, sheet="Nope")
    with pytest.raises(ConfigError, match="no header row"):
        api.import_mapping(MAPPING_XLSX, sheet="About")


def test_aliases_semicolons_and_scope(write: WriteFile, default_registry: Registry) -> None:
    sheet = write(
        "s2t.csv",
        """
        Src Table;Src Column;Tgt Table;Tgt Column;PK;Compare;Rules;Remarks;Owner
        legacy.customers;id;crm.customer;customer_id;yes;;not_null;
        legacy.customers;name;crm.customer;name;;;length:..80;Display name
        legacy.customers;email;crm.customer;email;;;regex:[^@]+@[^@]+;
        legacy.customers;notes;crm.customer;notes;;N;;Free text
        """,
    )
    result = api.import_mapping(sheet, suite_name="crm")
    data = yaml.safe_load(result.text)
    table = data["suites"][0]["tables"][0]
    assert data["suites"][0]["name"] == "crm"
    assert table["keys"] == [{"source": "id", "target": "customer_id"}]
    assert table["columns"]["include"] == ["email", "name"]
    assert {"source": "notes", "target": "notes", "compare": False} in table["columns"]["mappings"]
    assert {"type": "length", "column": "name", "max": 80} in table["checks"]
    assert "Columns not used: Owner" in result.notes
    unlisted = yaml.safe_load(api.import_mapping(sheet, include_unlisted=True).text)
    assert "include" not in unlisted["suites"][0]["tables"][0]["columns"]


def test_every_problem_is_reported_with_its_row(
    write: WriteFile, default_registry: Registry
) -> None:
    sheet = write(
        "bad.csv",
        """
        source_table,source_column,source_expr,target_table,target_column,key,normalizers,compare,checks
        a,x,,b,x,maybe,,,
        a,y,,b,y,,rtirm,,
        a,z,,b,z,,,,between:1..2
        a,w,UPPER(w),b,w,,,,
        a,v,,,v,,,,
        a,k,,b,,Y,,,
        a,,,b,only_target,,,Y,
        a,r,,b,r,,round(1;2),,range:x
        a,s,,b,s,,,,length:a..b
        a,t,,b,t,,,,regex:(unclosed
        """,
    )
    with pytest.raises(ConfigError) as caught:
        api.import_mapping(sheet)
    messages = [d.message for d in caught.value.diagnostics]
    text = "\n".join(messages)
    assert "row 2: 'key' must be Y or N, got 'maybe'" in text
    assert "row 3: unknown normalizer 'rtirm'" in text
    assert any(d.hint == "Did you mean 'rtrim'?" for d in caught.value.diagnostics)
    assert "row 4: unknown rule 'between'" in text
    assert "row 5: give 'source_column' or 'source_expr', not both" in text
    assert "row 6: 'target_table' is empty" in text
    assert "row 7: a key row needs both" in text
    assert "row 8: 'only_target' is marked compare but has no source" in text
    assert "row 9: normalizer 'round(1;2)'" in text or "row 9: rule 'range:x'" in text
    assert "row 10: rule 'length:a..b'" in text
    assert "row 11: rule 'regex:(unclosed'" in text
    assert all(d.location is not None and d.location.line for d in caught.value.diagnostics)


def test_reading_errors(write: WriteFile, tmp_path: Path, default_registry: Registry) -> None:
    with pytest.raises(ConfigError, match="not found"):
        read_sheet(tmp_path / "missing.csv")
    with pytest.raises(ConfigError, match="Unsupported mapping file type"):
        read_sheet(write("m.json", "{}"))
    with pytest.raises(ConfigError, match="applies to Excel"):
        read_sheet(MAPPING_CSV, "Mapping")
    with pytest.raises(ConfigError, match="no header row"):
        read_sheet(write("nohead.csv", "a,b,c\n1,2,3\n"))
    with pytest.raises(ConfigError, match="no mapping rows"):
        api.import_mapping(write("empty.csv", "source_table,target_table,target_column\n"))
    with pytest.raises(ConfigError, match="Cannot read the workbook"):
        read_sheet(write("broken.xlsx", "not a workbook"))
    assert dict(iter_aliases()) == HEADER_ALIASES


def test_connections_from_a_config_are_copied_as_written(
    write: WriteFile, tmp_path: Path, default_registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEGACY_PW", "s3cret-value")
    config = write(
        "base/dvf.yaml",
        """
        version: 1
        project: {name: base}
        connections:
          legacy: {type: postgres, host: db, user: me, password: "${secret:env:LEGACY_PW}"}
          lake: {type: duckdb, database: ./lake.duckdb}
        """,
    )
    out = tmp_path / "gen" / "dvf.yaml"
    api.import_mapping(MAPPING_CSV, config=config, output=out)
    text = out.read_text(encoding="utf-8")
    assert "s3cret-value" not in text and "${secret:env:LEGACY_PW}" in text
    data = yaml.safe_load(text)
    assert data["connections"]["lake"]["database"] == "../base/lake.duckdb"
    assert data["suites"][0]["source"] == "legacy" and data["suites"][0]["target"] == "lake"
    with pytest.raises(ConfigError, match="not defined"):
        api.import_mapping(MAPPING_CSV, config=config, source="nope")
    with pytest.raises(ConfigError, match="both --source and --target"):
        api.import_mapping(MAPPING_CSV, source=str(tmp_path))
    located = yaml.safe_load(
        api.import_mapping(MAPPING_CSV, source=str(tmp_path), target=str(tmp_path)).text
    )
    assert located["connections"]["source"]["type"] == "files"


def test_cli_import_mapping(tmp_path: Path, default_registry: Registry) -> None:
    out = tmp_path / "m.yaml"
    result = runner.invoke(app, ["import-mapping", str(MAPPING_XLSX), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists() and "converted 13 mapping row(s) into 2 table(s)" in result.output
    again = runner.invoke(app, ["import-mapping", str(MAPPING_XLSX), "-o", str(out)])
    assert again.exit_code == 3 and "already exists" in again.output
    stdout = runner.invoke(app, ["import-mapping", str(MAPPING_CSV)])
    assert stdout.exit_code == 0 and "version: 1" in stdout.output
    summary = runner.invoke(app, ["import-mapping", str(MAPPING_CSV), "--format", "json"])
    assert summary.exit_code == 0 and '"tables": 2' in summary.output
    bad = runner.invoke(app, ["import-mapping", str(tmp_path / "missing.csv")])
    assert bad.exit_code == 3
