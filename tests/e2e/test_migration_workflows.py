"""Migration workflows end to end: business parity (MIG-13), baseline capture and the
``baseline_metrics`` gate (MIG-14), run-to-run comparison and the sign-off report (MIG-08),
including the CLI commands. Synthetic data only."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import ExitCode, app
from dvf.core.enums import Status
from tests.conftest import EXAMPLES

cli = CliRunner()


def _duckdb(path: Path, statements: list[str]) -> None:
    con = duckdb.connect(str(path))
    try:
        for statement in statements:
            con.execute(statement)
    finally:
        con.close()


# ---------------------------------------------------------------------- MIG-13


@pytest.fixture
def parity_example(tmp_path: Path) -> Path:
    folder = tmp_path / "example"
    shutil.copytree(EXAMPLES / "migration", folder)
    return folder


def _run_example(folder: Path) -> api.RunOutput:
    return api.run(
        folder / "dvf.yaml",
        variables={"data": (folder / "data").as_posix()},
        output_dir=folder / "reports",
        history=False,
    )


def test_trial_balance_parity_within_tolerance(parity_example: Path) -> None:
    output = _run_example(parity_example)
    dataset = output.result.suites[0].datasets[0]
    assert dataset.status is Status.PASSED, [(c.check_id, c.message) for c in dataset.checks]
    assert dataset.row_counts.source == dataset.row_counts.target == 10
    assert dataset.reconciliation is not None and dataset.reconciliation.matched == 10
    signoff = (output.output_dir or parity_example) / "signoff.md"
    assert "Recommendation | GO:" in signoff.read_text(encoding="utf-8")


def test_trial_balance_difference_beyond_tolerance_fails(parity_example: Path) -> None:
    journal = parity_example / "data" / "target_journal_lines.csv"
    text = journal.read_text(encoding="utf-8").replace(
        "2026-02,J-1000-0003,120000,333.330", "2026-02,J-1000-0003,120000,338.330"
    )
    journal.write_text(text, encoding="utf-8")
    output = _run_example(parity_example)
    checks = {c.type: c for c in output.result.iter_checks()}
    assert checks["compare.aggregate"].status is Status.FAILED
    group = next(m for m in checks["compare.aggregate"].metrics if not m.passed)
    assert group.group == {"company_code": "1000", "period": "2026-02"}
    mismatch = checks["compare.column_mismatch"]
    assert mismatch.status is Status.FAILED
    assert mismatch.column_mismatches[0].column == "balance"
    signoff = (output.output_dir or parity_example) / "signoff.md"
    assert "NO-GO" in signoff.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- MIG-14


def _baseline_project(tmp_path: Path) -> Path:
    db = tmp_path / "db.duckdb"
    _duckdb(
        db,
        [
            "CREATE SCHEMA s",
            "CREATE SCHEMA t",
            "CREATE TABLE s.cust AS SELECT * FROM (VALUES "
            "(1, 10.50, 'EU', DATE '2020-01-01'), (2, 20.25, 'EU', DATE '2021-05-01'), "
            "(3, 5.00, 'US', DATE '2019-03-03'), (4, NULL, 'US', DATE '2022-12-31'), "
            "(5, 1.25, 'APAC', NULL)) v(id, amount, region, created)",
            "CREATE TABLE t.customers AS SELECT id AS cust_id, CAST(amount AS DECIMAL(12,4)) "
            "AS amt, region, created FROM s.cust",
        ],
    )
    config = {
        "version": 1,
        "project": {"name": "baseline-demo"},
        "history": {"enabled": False},
        "connections": {"db": {"type": "duckdb", "database": db.as_posix()}},
        "suites": [
            {
                "name": "m",
                "source": "db",
                "target": "db",
                "tables": [
                    {
                        "name": "cust",
                        "source": {"table": "s.cust"},
                        "target": {"table": "t.customers"},
                        "keys": [{"source": "id", "target": "cust_id"}],
                        "columns": {"mappings": [{"source": "amount", "target": "amt"}]},
                        "schema_checks": [],
                        "comparison": {"enabled": False, "group_by": ["region"]},
                        "checks": [
                            {
                                "type": "baseline_metrics",
                                "baseline": "pre_cutover",
                                "metrics": [
                                    "row_count",
                                    "null_count",
                                    "count_distinct",
                                    "sum",
                                    "min",
                                    "max",
                                    "groups",
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    path = tmp_path / "dvf.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_baseline_capture_then_go_no_go(tmp_path: Path) -> None:
    config = _baseline_project(tmp_path)
    file, document = api.capture_baseline(config, "pre_cutover")
    assert file == tmp_path / ".dvf" / "baselines" / "pre_cutover.json"
    entry = document["tables"]["m.cust"]
    assert entry["row_count"] == 5
    assert entry["columns"]["amount"] == {
        "type": "DECIMAL",
        "null_count": 1,
        "count_distinct": 4,
        "sum": "37.00",
        "min": "1.25",
        "max": "20.25",
    }
    assert entry["columns"]["created"]["min"] == "2019-03-03"
    assert entry["group_by"] == ["region"]
    assert sorted(entry["groups"]) == [[["APAC"], 1], [["EU"], 2], [["US"], 2]]

    check = next(api.run(config, write_reports=False).result.iter_checks())
    assert check.status is Status.PASSED, check.message
    assert check.details["baseline_side"] == "source"

    _duckdb(tmp_path / "db.duckdb", ["DELETE FROM t.customers WHERE cust_id = 2"])
    check = next(api.run(config, write_reports=False).result.iter_checks())
    assert check.status is Status.FAILED
    failed = {(m.metric, m.column) for m in check.metrics if not m.passed}
    assert ("row_count", None) in failed
    assert ("sum", "amt") in failed
    assert ("group_row_count", None) in failed
    group = next(m for m in check.metrics if m.metric == "group_row_count" and not m.passed)
    assert group.group == {"region": "EU"} and (group.source, group.target) == (2, 1)


def test_baseline_tolerance_and_missing_baseline(tmp_path: Path) -> None:
    config = _baseline_project(tmp_path)
    api.capture_baseline(config, "pre_cutover", distinct=False)
    _duckdb(tmp_path / "db.duckdb", ["UPDATE t.customers SET amt = amt + 0.001 WHERE cust_id = 1"])
    loaded = api.load_config(
        config,
        overrides=[
            'suites.0.tables.0.checks.0.metrics=["row_count","sum"]',
        ],
    )
    check = next(api.run(loaded, write_reports=False).result.iter_checks())
    assert check.status is Status.FAILED
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    rule = data["suites"][0]["tables"][0]["checks"][0]
    rule.update({"metrics": ["row_count", "sum"], "tolerance": {"numeric_abs": 0.01}})
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    check = next(api.run(config, write_reports=False).result.iter_checks())
    assert check.status is Status.PASSED, check.message
    rule["baseline"] = "nope"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    check = next(api.run(config, write_reports=False).result.iter_checks())
    assert check.status is Status.ERROR and "not found" in (check.message or "")


def test_baseline_cli(tmp_path: Path) -> None:
    config = _baseline_project(tmp_path)
    result = cli.invoke(app, ["baseline", "capture", str(config), "--name", "pre_cutover"])
    assert result.exit_code == ExitCode.OK, result.output
    assert "baseline 'pre_cutover' (source) written to" in result.output
    again = cli.invoke(app, ["baseline", "capture", str(config), "--name", "pre_cutover"])
    assert again.exit_code == ExitCode.CONFIG_ERROR
    assert "already exists" in again.output
    target = cli.invoke(
        app,
        [
            "baseline",
            "capture",
            str(config),
            "-n",
            "target_copy",
            "--side",
            "target",
            "--no-distinct",
            "--format",
            "json",
        ],
    )
    assert target.exit_code == ExitCode.OK, target.output
    assert json.loads(target.output)["tables"]["m.cust"]["row_count"] == 5
    listing = cli.invoke(app, ["baseline", "list", str(config), "--format", "json"])
    names = {b["name"]: b["side"] for b in json.loads(listing.output)}
    assert names == {"pre_cutover": "source", "target_copy": "target"}


# ---------------------------------------------------------------------- MIG-08


def _rehearsal_project(tmp_path: Path) -> Path:
    db = tmp_path / "db.duckdb"
    _duckdb(
        db,
        [
            "CREATE SCHEMA s",
            "CREATE SCHEMA t",
            "CREATE TABLE s.cust AS SELECT i AS id, 'n' || i AS name FROM range(1, 11) r(i)",
            "CREATE TABLE t.cust AS SELECT * FROM s.cust",
        ],
    )
    config: dict[str, Any] = {
        "version": 1,
        "project": {"name": "rehearsals", "metadata": {"release": "mock cutover"}},
        "connections": {"db": {"type": "duckdb", "database": db.as_posix()}},
        "reports": {"formats": ["json", "signoff"]},
        "suites": [
            {
                "name": "m",
                "source": "db",
                "target": "db",
                "tables": [
                    {
                        "name": "cust",
                        "source": {"table": "s.cust"},
                        "target": {"table": "t.cust"},
                        "keys": ["id"],
                        "schema_checks": [],
                        "comparison": {"strategies": ["row_count"], "on_mismatch": None},
                        "checks": [{"type": "not_null", "columns": ["name"]}],
                    }
                ],
            }
        ],
    }
    path = tmp_path / "dvf.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_rehearsal_comparison_and_signoff(tmp_path: Path) -> None:
    config = _rehearsal_project(tmp_path)
    first = api.run(config, output_dir=tmp_path / "r1")
    assert first.result.status is Status.PASSED
    first_signoff = (tmp_path / "r1" / "signoff.md").read_text(encoding="utf-8")
    assert "Recommendation | GO:" in first_signoff
    _duckdb(
        tmp_path / "db.duckdb",
        ["DELETE FROM t.cust WHERE id = 10", "UPDATE t.cust SET name = NULL WHERE id = 1"],
    )
    second = api.run(config, output_dir=tmp_path / "r2")
    assert second.result.status is Status.FAILED
    signoff = (tmp_path / "r2" / "signoff.md").read_text(encoding="utf-8")
    assert "NO-GO" in signoff
    assert "m.cust.compare.row_count" in signoff and "| new |" in signoff
    assert "mock cutover" in signoff
    assert (tmp_path / "r2" / "signoff.html").read_text(encoding="utf-8").startswith("<!doctype")

    a, b = first.result.run_id, second.result.run_id
    diff = api.compare_runs(a, b, config=config)
    assert diff.new_failures == ["m.cust.compare.row_count", "m.cust.not_null.name"]
    assert diff.resolved == [] and diff.persisting == []
    target_rows = next(d for d in diff.dataset_deltas if d["metric"] == "target_rows")
    assert (target_rows["before"], target_rows["after"]) == (10, 9)

    text = cli.invoke(app, ["history", "compare", a, b, "--config", str(config)])
    assert text.exit_code == ExitCode.OK, text.output
    assert "2 new, 0 resolved, 0 persisting failures" in text.output
    gate = cli.invoke(app, ["history", "compare", a, b, "-c", str(config), "--fail-on-new"])
    assert gate.exit_code == ExitCode.VALIDATION_FAILED
    as_json = cli.invoke(app, ["history", "compare", a, b, "-c", str(config), "--format", "json"])
    assert json.loads(as_json.output)["new_failures"] == diff.new_failures
    results_a = tmp_path / "r1" / "results.json"
    by_path = cli.invoke(app, ["history", "compare", str(results_a), b, "-c", str(config)])
    assert by_path.exit_code == ExitCode.OK, by_path.output

    # The run listing keeps working, with and without the explicit subcommand.
    for args in (
        ["history", str(config), "--format", "json"],
        ["history", "list", str(config), "--format", "json"],
    ):
        listing = cli.invoke(app, args)
        assert listing.exit_code == ExitCode.OK, listing.output
        assert b in listing.output

    # The sign-off document can be rendered later from the results file.
    rendered = cli.invoke(
        app,
        [
            "report",
            "--from",
            str(tmp_path / "r2" / "results.json"),
            "--format",
            "signoff",
            "--output-dir",
            str(tmp_path / "later"),
        ],
    )
    assert rendered.exit_code == ExitCode.OK, rendered.output
    assert (tmp_path / "later" / "signoff.md").exists()
