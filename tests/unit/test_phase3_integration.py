"""Cross-pack behavior added while integrating Phase 3."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf import api
from dvf.core.enums import Status
from dvf.core.errors import ConfigError


def _duckdb(tmp_path: Path, name: str, sql: str) -> str:
    path = tmp_path / f"{name}.duckdb"
    con = duckdb.connect(str(path))
    con.execute(sql)
    con.close()
    return path.as_posix()


def _checks(output: Any) -> dict[str, Any]:
    return {c.check_id.split(".", 2)[2]: c for c in output.result.suites[0].datasets[0].checks}


def test_type_compatibility_uses_normalized_types(tmp_path: Path) -> None:
    source = _duckdb(
        tmp_path,
        "sap",
        "CREATE TABLE kna1 (kunnr VARCHAR, erdat VARCHAR);"
        "INSERT INTO kna1 VALUES ('0000001001', '20240131'), ('0000001002', '00000000');",
    )
    target = _duckdb(
        tmp_path,
        "dwh",
        "CREATE TABLE customer (customer_id VARCHAR, created DATE);"
        "INSERT INTO customer VALUES ('1001', DATE '2024-01-31'), ('1002', NULL);",
    )
    config = {
        "version": 1,
        "project": {"name": "sap"},
        "connections": {
            "sap": {"type": "duckdb", "database": source},
            "dwh": {"type": "duckdb", "database": target},
        },
        "suites": [
            {
                "name": "s",
                "source": "sap",
                "target": "dwh",
                "tables": [
                    {
                        "name": "customers",
                        "source": {"table": "kna1"},
                        "target": {"table": "customer"},
                        "keys": [
                            {"source": "kunnr", "target": "customer_id", "normalize": ["sap_alpha"]}
                        ],
                        "columns": {
                            "mappings": [
                                {"source": "erdat", "target": "created", "normalize": ["sap_dats"]}
                            ]
                        },
                        "comparison": {"strategies": ["full_diff"]},
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    checks = _checks(api.run(loaded, write_reports=False, history=False))
    compat = checks["schema.type_compatibility"]
    assert compat.status is Status.PASSED, compat.message
    assert checks["compare.column_mismatch"].status is Status.PASSED
    assert checks["compare.keys"].status is Status.PASSED


def test_unknown_nested_connection_is_a_config_error(tmp_path: Path) -> None:
    config = {
        "version": 1,
        "project": {"name": "x"},
        "connections": {"a": {"type": "duckdb"}},
        "suites": [
            {
                "name": "s",
                "source": "a",
                "tables": [
                    {
                        "name": "t",
                        "source": {"table": "t"},
                        "checks": [
                            {
                                "type": "referential_integrity",
                                "column": "id",
                                "parent": {"connection": "nope", "table": "p", "column": "id"},
                            }
                        ],
                    }
                ],
            }
        ],
    }
    with pytest.raises(ConfigError) as info:
        api.load_config_data(config, base_dir=tmp_path, strict=False)
    assert any("Unknown connection 'nope'" in d.message for d in info.value.diagnostics)


def test_every_pack_registers_and_plugins_exports() -> None:
    from dvf.plugins import PairCheck, PairContext
    from dvf.plugins.registry import PluginKind, new_registry

    registry = new_registry()
    ready = {(e.kind, e.name): e.status for e in registry.entries()}
    for kind, name in [
        (PluginKind.CONNECTOR, "mongodb"),
        (PluginKind.CONNECTOR, "dataframe"),
        (PluginKind.CHECK, "file_exists"),
        (PluginKind.CHECK, "scd2"),
        (PluginKind.CHECK, "contract"),
        (PluginKind.CHECK, "document_schema"),
        (PluginKind.COMPARATOR, "nested"),
        (PluginKind.NORMALIZER, "sap_alpha"),
        (PluginKind.REPORTER, "signoff"),
    ]:
        assert ready[(kind, name)] == "ready", (kind, name)
    for name in ("sap_hana", "sap_rfc", "cassandra", "dynamodb", "cosmos"):
        assert ready[(PluginKind.CONNECTOR, name)] == "beta", name
    assert PairCheck is not None and PairContext is not None


def test_explain_shows_nested_sql_and_pair_checks_once(tmp_path: Path) -> None:
    db = _duckdb(
        tmp_path,
        "lines",
        "CREATE TABLE a (order_id INT, line_no INT, qty INT);"
        "CREATE TABLE b (order_id INT, line_no INT, qty INT);"
        "INSERT INTO a VALUES (1, 1, 5), (1, 2, 3);"
        "INSERT INTO b VALUES (1, 1, 5), (1, 2, 4);",
    )
    config = {
        "version": 1,
        "project": {"name": "x"},
        "connections": {"db": {"type": "duckdb", "database": db}},
        "suites": [
            {
                "name": "s",
                "source": "db",
                "target": "db",
                "tables": [
                    {
                        "name": "lines",
                        "source": {"table": "a"},
                        "target": {"table": "b"},
                        "keys": ["order_id", "line_no"],
                        "comparison": {"strategies": ["nested"], "group_by": ["order_id"]},
                        "checks": [{"type": "scope_reconciliation", "excluded_where": "qty < 0"}],
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    report = api.explain(loaded).to_dict()
    table = report["tables"][0]
    nested = [q for q in table["queries"] if q["purpose"].startswith("nested")]
    assert {q["side"] for q in nested} == {"source", "target"}
    assert all("GROUP BY" in q["sql"].upper() for q in nested)
    pair = [c for c in table["checks"] if c["type"] == "scope_reconciliation"]
    assert [c["side"] for c in pair] == ["both"]
