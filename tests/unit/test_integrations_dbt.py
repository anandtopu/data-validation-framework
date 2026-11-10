"""dbt: generating dvf tables from a synthetic manifest, run results, and ``dvf dbt import``."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.core.errors import ConfigError
from dvf.integrations.common import run_validation
from dvf.integrations.dbt import built_model_selectors, import_manifest, read_manifest
from tests.conftest import EXAMPLES

DBT = EXAMPLES / "orchestrators" / "dbt"
MANIFEST = DBT / "target" / "manifest.json"
RUN_RESULTS = DBT / "target" / "run_results.json"
runner = CliRunner()


def manifest_data() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data


def write_manifest(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def tables(text: str) -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(text)
    return {t["name"]: t for t in doc["suites"][0]["tables"]}


def test_import_the_example_manifest(tmp_path: Path) -> None:
    output = tmp_path / "dvf.yaml"
    result = import_manifest(MANIFEST, output=output)
    assert result.models == 3 and result.output == output
    doc = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert doc["project"]["name"] == "jaffle_shop"
    assert doc["connections"]["warehouse"] == {"type": "duckdb", "database": "./jaffle.duckdb"}
    by_name = tables(result.text)
    assert list(by_name) == ["customers", "orders", "stg_customers"]  # no ephemeral, disabled,
    customers, orders = by_name["customers"], by_name["orders"]  # or package models
    assert customers["source"] == {"table": "main.customers"}
    assert customers["keys"] == ["customer_id"] and orders["keys"] == ["order_id"]
    assert customers["tags"] == ["dbt", "marts", "finance"]
    assert {"type": "not_null", "columns": ["email"], "severity": "warning"} in customers["checks"]
    assert {
        "type": "regex",
        "column": "email",
        "pattern": "@",
        "full_match": False,
        "severity": "warning",
    } in customers["checks"]
    checks = {c["type"]: c for c in orders["checks"]}
    assert checks["not_null"]["columns"] == ["order_id", "customer_id"]  # merged
    assert checks["allowed_values"]["values"] == ["placed", "shipped", "completed", "returned"]
    assert checks["referential_integrity"]["parent"] == {
        "table": "main.customers",
        "column": "customer_id",
    }
    assert checks["range"] == {"type": "range", "column": "amount", "min": 0}
    assert checks["expression"]["where"] == "ordered_at >= '2026-01-01'"
    assert checks["row_count_range"] == {"type": "row_count_range", "min": 1}
    assert len(result.unsupported) == 3
    assert any("singular test" in u for u in result.unsupported)
    assert any("lower(status)" in u for u in result.unsupported)
    assert "REVIEW" in result.text and "dbt test unique_orders_order_id" in result.text
    report = api.validate_config(output)
    assert report.ok, report.errors
    assert result.to_dict()["models"] == 3


def test_selection_options(tmp_path: Path) -> None:
    assert list(tables(import_manifest(MANIFEST, models=["cust*"]).text)) == ["customers"]
    assert list(tables(import_manifest(MANIFEST, tags=["staging"]).text)) == ["stg_customers"]
    assert list(tables(import_manifest(MANIFEST, exclude=["stg_*", "orders"]).text)) == [
        "customers"
    ]
    everything = tables(import_manifest(MANIFEST, all_packages=True).text)
    assert "calendar" in everything
    bare = tables(import_manifest(MANIFEST, tests=False, row_count=False).text)
    assert all("checks" not in t for t in bare.values())
    named = yaml.safe_load(
        import_manifest(MANIFEST, suite_name="marts", project_name="warehouse-qa").text
    )
    assert named["project"]["name"] == "warehouse-qa" and named["suites"][0]["name"] == "marts"
    with pytest.raises(ConfigError, match="No dbt models"):
        import_manifest(MANIFEST, models=["nothing*"])


def test_connection_from_a_location_or_a_configuration(tmp_path: Path) -> None:
    located = import_manifest(
        MANIFEST,
        location=f"duckdb:///{(tmp_path / 'w.duckdb').as_posix()}",
        output=tmp_path / "a.yaml",
    )
    assert yaml.safe_load(located.text)["connections"]["warehouse"] == {
        "type": "duckdb",
        "database": "w.duckdb",
    }
    config = tmp_path / "existing.yaml"
    config.write_text(
        "version: 1\nproject: {name: x}\n"
        "connections:\n  wh: {type: postgres, url: '${secret:env:WH_URL}'}\n"
        "  other: {type: duckdb}\n"
        "suites: [{name: s, source: wh, tables: []}]\n",
        encoding="utf-8",
    )
    loaded = api.load_config(config, strict=False, resolve_secrets=False)
    copied = import_manifest(MANIFEST, loaded=loaded, config_path=config, connection="wh")
    doc = yaml.safe_load(copied.text)
    assert doc["connections"] == {"wh": {"type": "postgres", "url": "${secret:env:WH_URL}"}}
    assert doc["suites"][0]["source"] == "wh"
    with pytest.raises(ConfigError, match="choose one with --connection"):
        import_manifest(MANIFEST, loaded=loaded, config_path=config)
    with pytest.raises(ConfigError, match="path is needed"):
        import_manifest(MANIFEST, loaded=loaded, connection="wh")


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        ("postgres", {"type": "postgres", "database": "jaffle"}),
        ("sqlserver", {"type": "mssql", "database": "jaffle"}),
        ("snowflake", {"type": "snowflake", "database": "jaffle"}),
        ("bigquery", {"type": "bigquery", "project": "jaffle"}),
        ("databricks", {"type": "databricks", "catalog": "jaffle"}),
        ("spark", {"type": "spark"}),
        ("trino", {"type": "duckdb", "database": "./warehouse.duckdb"}),
    ],
)
def test_connection_from_the_adapter(
    tmp_path: Path, adapter: str, expected: dict[str, str]
) -> None:
    data = manifest_data()
    data["metadata"]["adapter_type"] = adapter
    result = import_manifest(write_manifest(tmp_path, data))
    connection = yaml.safe_load(result.text)["connections"]["warehouse"]
    assert expected.items() <= connection.items()
    assert all("${" in str(v) or k in expected for k, v in connection.items())
    assert "password" not in connection or connection["password"].startswith("${secret:env:")
    if adapter == "trino":
        assert "REVIEW: placeholder" in result.text and "trino" in result.notes[0]


def test_older_manifests_and_test_details(tmp_path: Path) -> None:
    data = manifest_data()
    nodes = data["nodes"]
    for node in nodes.values():
        node.pop("attached_node", None)  # manifests before dbt 1.5
    customers = nodes["model.jaffle_shop.customers"]
    customers["constraints"] = []
    customers["columns"]["customer_id"]["constraints"] = [{"type": "primary_key"}]
    orders = nodes["model.jaffle_shop.orders"]
    orders["config"]["unique_key"] = ["order_id", "customer_id"]
    stg = nodes["model.jaffle_shop.stg_customers"]
    stg["database"] = "raw_db"
    extra = {
        "test.jaffle_shop.combo": (
            "unique_combination_of_columns",
            "dbt_utils",
            {"combination_of_columns": ["customer_id", "email"]},
            None,
        ),
        "test.jaffle_shop.rows": (
            "expect_table_row_count_to_be_between",
            "dbt_expectations",
            {"min_value": 1, "max_value": 1000},
            None,
        ),
        "test.jaffle_shop.between": (
            "expect_column_values_to_be_between",
            "dbt_expectations",
            {"min_value": 0, "max_value": 1e6, "strictly": True},
            "lifetime_value",
        ),
        "test.jaffle_shop.in_set": (
            "expect_column_values_to_be_in_set",
            "dbt_expectations",
            {"value_set": ["a", "b"]},
            "first_name",
        ),
        "test.jaffle_shop.true": (
            "expression_is_true",
            "dbt_utils",
            {"expression": ">= 0"},
            "lifetime_value",
        ),
        "test.jaffle_shop.to_source": ("relationships", None, {"field": "id"}, "customer_id"),
        "test.jaffle_shop.bad_values": ("accepted_values", None, {"values": []}, "first_name"),
    }
    for uid, (name, namespace, kwargs, column) in extra.items():
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "test",
            "name": uid.rsplit(".", 1)[-1],
            "test_metadata": {
                "name": name,
                "namespace": namespace,
                "kwargs": {**kwargs, "column_name": column, "model": "{{ ref('customers') }}"},
            },
            "depends_on": {"nodes": ["model.jaffle_shop.customers"]},
            "config": {"enabled": True, "severity": "ERROR", "error_if": "> 10"},
        }
    nodes["test.jaffle_shop.to_source"]["depends_on"]["nodes"].append(
        "source.jaffle_shop.raw.customers"
    )
    nodes["test.jaffle_shop.off"] = copy.deepcopy(nodes["test.jaffle_shop.combo"])
    nodes["test.jaffle_shop.off"]["config"]["enabled"] = False
    result = import_manifest(write_manifest(tmp_path, data))
    by_name = tables(result.text)
    customers_t = by_name["customers"]
    assert customers_t["keys"] == ["customer_id"]  # column-level primary key constraint
    assert by_name["orders"]["keys"] == ["order_id", "customer_id"]
    assert by_name["stg_customers"]["source"] == {"table": "raw_db.main.stg_customers"}
    checks = customers_t["checks"]
    assert {"type": "unique", "columns": ["customer_id", "email"]} in checks
    assert {"type": "row_count_range", "min": 1, "max": 1000} in checks
    assert sum(1 for c in checks if c["type"] == "row_count_range") == 1  # no extra min 1
    assert {
        "type": "range",
        "column": "lifetime_value",
        "min": 0,
        "max": 1000000,
        "inclusive": False,
    } in checks
    assert {"type": "allowed_values", "column": "first_name", "values": ["a", "b"]} in checks
    assert any(c.get("expression") == "lifetime_value >= 0" for c in checks)
    assert {
        "type": "referential_integrity",
        "column": "customer_id",
        "parent": {"table": "raw.raw_customers", "column": "id"},
    } in checks
    assert "dbt fails when failures > 10" in result.text
    assert any("bad_values" in u for u in result.unsupported)
    assert api.validate_config(write_out(tmp_path, result.text)).ok


def write_out(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "generated.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_keys_from_unique_tests(tmp_path: Path) -> None:
    data = manifest_data()
    customers = data["nodes"]["model.jaffle_shop.customers"]
    customers["constraints"] = []
    result = import_manifest(write_manifest(tmp_path, data))
    assert "from dbt test unique_customers_customer_id" in result.text


def test_read_manifest_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist") as info:
        read_manifest(tmp_path / "missing.json")
    assert info.value.hint is not None and "dbt build" in info.value.hint
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not be read"):
        read_manifest(bad)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"results": []}), encoding="utf-8")
    with pytest.raises(ConfigError, match="not a dbt manifest"):
        read_manifest(other)


def test_built_model_selectors(tmp_path: Path) -> None:
    assert built_model_selectors(RUN_RESULTS) == [
        "table:stg_customers",
        "table:customers",
        "table:orders",
    ]
    assert built_model_selectors(RUN_RESULTS, statuses=["error"]) == []
    with pytest.raises(ConfigError):
        built_model_selectors(tmp_path / "missing.json")


def load_demo() -> Any:
    spec = importlib.util.spec_from_file_location("demo_warehouse", DBT / "demo_warehouse.py")
    assert spec is not None and spec.loader is not None
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    return demo


def test_end_to_end_on_the_demo_warehouse(tmp_path: Path) -> None:
    load_demo().create(tmp_path / "jaffle.duckdb")
    config = tmp_path / "dvf.yaml"
    import_manifest(MANIFEST, output=config)
    _, summary = run_validation(
        config,
        only=built_model_selectors(RUN_RESULTS),
        output_dir=tmp_path / "reports",
        history=False,
    )
    assert summary.exit_code == 1
    assert set(summary.failed_checks) == {
        "dbt_models.orders.allowed_values.status",
        "dbt_models.orders.referential_integrity.customer_id",
        "dbt_models.orders.range.amount",
        "dbt_models.orders.expression.dbt_utils_expression_is_true_orders_amount_0",
    }
    assert summary.warning_checks == ("dbt_models.customers.not_null.email",)


def test_cli_dbt_import(tmp_path: Path) -> None:
    output = tmp_path / "dvf.yaml"
    result = runner.invoke(app, ["dbt", "import", str(MANIFEST), "-o", str(output), "-m", "orders"])
    assert result.exit_code == 0, result.output
    assert "converted 1 dbt model(s)" in result.output and output.is_file()
    again = runner.invoke(app, ["dbt", "import", str(MANIFEST), "-o", str(output)])
    assert again.exit_code == 3 and "already exists" in again.output
    forced = runner.invoke(
        app, ["dbt", "import", str(MANIFEST), "-o", str(output), "--force", "--format", "json"]
    )
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.stdout)["models"] == 3
    printed = runner.invoke(app, ["dbt", "import", str(MANIFEST), "--tag", "staging"])
    assert printed.exit_code == 0 and "name: stg_customers" in printed.stdout
    missing = runner.invoke(app, ["dbt", "import", str(tmp_path / "none.json")])
    assert missing.exit_code == 3
    both = runner.invoke(
        app, ["dbt", "import", str(MANIFEST), "-c", str(output), "--location", "x.duckdb"]
    )
    assert both.exit_code == 3 and "not both" in both.output


def test_cli_dbt_import_with_a_configuration(tmp_path: Path) -> None:
    config = tmp_path / "base.yaml"
    config.write_text(
        "version: 1\nproject: {name: x}\nconnections: {wh: {type: duckdb, database: ./w.duckdb}}\n"
        "suites: [{name: s, source: wh, tables: []}]\n",
        encoding="utf-8",
    )
    output = tmp_path / "out" / "dvf.yaml"
    result = runner.invoke(
        app, ["dbt", "import", str(MANIFEST), "-c", str(config), "-o", str(output)]
    )
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert doc["connections"]["wh"] == {"type": "duckdb", "database": "../w.duckdb"}
