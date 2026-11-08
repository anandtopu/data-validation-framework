"""Phase 3 acceptance: a MongoDB-to-PostgreSQL example reconciles embedded arrays against child
tables, against live servers.

Runs ``examples/mongodb_to_postgres`` end to end: its loader writes the synthetic orders into
MongoDB (lines embedded) and PostgreSQL (``orders`` and ``order_lines``) under fresh names, and
every seeded defect must be reported exactly, with no false positives from the type differences
(Decimal128 against ``numeric``, BSON dates against ``date``, ``int`` against ``long``).

Set ``DVF_TEST_MONGODB_URL`` and ``DVF_TEST_POSTGRES_URL`` to run it.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dvf import api
from tests.conftest import EXAMPLES

pytestmark = pytest.mark.integration

EXAMPLE = EXAMPLES / "mongodb_to_postgres"


def _loader() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dvf_example_loader", EXAMPLE / "load_data.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example_data() -> Iterator[dict[str, str]]:
    mongodb_url = os.environ.get("DVF_TEST_MONGODB_URL")
    postgres_url = os.environ.get("DVF_TEST_POSTGRES_URL")
    if not mongodb_url or not postgres_url:
        pytest.skip("set DVF_TEST_MONGODB_URL and DVF_TEST_POSTGRES_URL to run this test")
    name = f"p3_nosql_{secrets.token_hex(3)}"
    loader = _loader()
    loader.load(mongodb_url, postgres_url, name, name)
    try:
        yield {
            "DVF_EXAMPLE_MONGODB_URL": mongodb_url,
            "DVF_EXAMPLE_POSTGRES_URL": postgres_url,
            "DVF_EXAMPLE_MONGO_DB": name,
            "DVF_EXAMPLE_PG_SCHEMA": name,
        }
    finally:
        loader.drop(mongodb_url, postgres_url, name, name)


def _run(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return api.run(
        EXAMPLE / "dvf.yaml",
        overrides=[f"defaults.execution.state_dir={tmp_path.as_posix()}"],
        output_dir=tmp_path / "reports",
        formats=["json", "html"],
        history=False,
    )


def test_example_reconciles_embedded_arrays_against_child_tables(
    example_data: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _run(example_data, tmp_path, monkeypatch)
    checks = {c.check_id: c for s in out.result.suites for d in s.datasets for c in d.checks}
    status = {check_id: c.status.value for check_id, c in checks.items()}
    assert status == {
        "orders.orders.schema.object_exists": "passed",
        "orders.orders.schema.columns_present": "passed",
        "orders.orders.schema.type_compatibility": "failed",  # total holds a double (1007)
        "orders.orders.schema.length_precision": "passed",
        "orders.orders.schema.nullability": "skipped",
        "orders.orders.compare.row_count": "passed",
        "orders.orders.compare.keys": "passed",
        "orders.orders.compare.column_mismatch": "failed",
        "orders.orders.document_schema": "warning",
        "orders.order_lines.schema.object_exists": "passed",
        "orders.order_lines.schema.columns_present": "passed",
        "orders.order_lines.schema.type_compatibility": "passed",
        "orders.order_lines.schema.length_precision": "passed",
        "orders.order_lines.schema.nullability": "skipped",
        "orders.order_lines.compare.nested": "failed",
        "orders.order_lines.compare.keys": "failed",
        "orders.order_lines.compare.column_mismatch": "failed",
    }

    # Headers: only the changed status differs (Decimal128 totals, BSON dates, and the nested
    # customer name reconcile); the double total is a type finding, not a value mismatch.
    header = checks["orders.orders.compare.column_mismatch"]
    assert [(m.column, m.mismatch_count) for m in header.column_mismatches] == [("status", 1)]
    assert header.column_mismatches[0].samples is not None
    assert header.column_mismatches[0].samples.rows[0][:3] == ["1005", "shipped", "pending"]
    assert "total" in (checks["orders.orders.schema.type_compatibility"].message or "")
    schema = checks["orders.orders.document_schema"]
    assert schema.samples is not None
    assert schema.samples.rows == [
        ["total", "unexpected type", "double in 1 of 10 values", "decimal"]
    ]
    assert schema.details["documents_sampled"] == 10

    # Lines: three orders differ, each for the seeded reason.
    nested = checks["orders.order_lines.compare.nested"]
    assert (nested.unexpected_count, nested.total_count) == (3, 10)
    assert nested.samples is not None
    issues = {row[0]: (row[1], row[2], row[3]) for row in nested.samples.rows}
    assert issues == {
        "1002": ("child rows differ", 3, 2),
        "1003": ("totals differ", 2, 2),
        "1004": ("child rows differ", 2, 3),
    }
    assert "sum(qty) 9 vs 10" in nested.samples.rows[1][4]
    assert nested.details["hashed_columns"] == ["line_no", "sku", "qty", "price"]
    keys = checks["orders.order_lines.compare.keys"]
    assert keys.observed["missing_in_target"] == 1 and keys.observed["extra_in_target"] == 1
    assert keys.samples is not None
    assert sorted(tuple(r[:3]) for r in keys.samples.rows) == [
        ("extra_in_target", "1004", "3"),
        ("missing_in_target", "1002", "3"),
    ]
    lines = checks["orders.order_lines.compare.column_mismatch"]
    assert [(m.column, m.mismatch_count) for m in lines.column_mismatches] == [("qty", 1)]
    assert lines.column_mismatches[0].samples is not None
    assert lines.column_mismatches[0].samples.rows[0][:4] == ["1003", "2", "5", "6"]

    assert (tmp_path / "reports" / "report.html").exists()
    assert (
        tmp_path / "reports" / "extracts" / "orders.order_lines.compare.nested.parquet"
    ).exists()
