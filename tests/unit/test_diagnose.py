"""explain, trace, and loading earlier runs, across strategies, normalizers, and tolerances."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.core.enums import Status
from dvf.core.errors import ConfigError, DvfError
from dvf.plugins.registry import new_registry
from dvf.scaffold.seeded import load_sqlite, write_parquet

cli = CliRunner()


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    write_parquet(
        pa.table(
            {
                "id": [1, 2, 3, 3],
                "code": ["001", "002", "003", "003"],
                "name": ["Ann ", "bob", "Cy", "Cy"],
                "amount": [1.0, 2.0, 3.0, 3.0],
            }
        ),
        data / "s.parquet",
    )
    write_parquet(
        pa.table(
            {
                "id": [1, 2, 3],
                "code": ["1", "2", "3"],
                "name": ["Ann", "BOB", "Cy"],
                "amount": [1.0000001, 2.5, 3.0],
            }
        ),
        data / "t.parquet",
    )
    return data


def _config(tmp_path: Path, **table: Any) -> dict[str, Any]:
    data = _data(tmp_path)
    return {
        "version": 1,
        "project": {"name": "diag"},
        "connections": {"f": {"type": "files", "path": data.as_posix()}},
        "suites": [
            {
                "name": "s",
                "source": "f",
                "target": "f",
                "tables": [
                    {
                        "name": "t",
                        "source": {"path": "s.parquet"},
                        "target": {"path": "t.parquet"},
                        "keys": ["id"],
                        "columns": {
                            "mappings": [
                                {
                                    "source": "code",
                                    "target": "code",
                                    "normalize": ["strip_leading_zeros"],
                                },
                                {
                                    "source": "name",
                                    "target": "name",
                                    "normalize": ["rtrim", "lower"],
                                },
                            ]
                        },
                        "comparison": {
                            "strategies": ["row_count", "hash_bucket", "bisect"],
                            "on_mismatch": "full_diff",
                            "tolerance": {"numeric_abs": 0.001},
                            "partitions": {"hash_buckets": 2},
                        },
                        "checks": [
                            {"type": "not_null", "columns": ["id"]},
                            {
                                "type": "custom_sql",
                                "name": "big",
                                "sql": "SELECT * FROM {table} WHERE amount > 100",
                            },
                            {"type": "unique", "columns": ["id"]},
                        ],
                        **table,
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }


def _loaded(config: dict[str, Any], base: Path) -> api.LoadedConfig:
    return api.load_config_data(config, base_dir=base, strict=False, registry=new_registry())


def test_explain_covers_strategies_partitions_and_checks(tmp_path: Path) -> None:
    report = api.explain(_loaded(_config(tmp_path), tmp_path))
    table = report.tables[0]
    purposes = [q.purpose for q in table.queries]
    assert any(p.startswith("hash_bucket level 0") for p in purposes)
    assert any(p.startswith("bisect key range") for p in purposes)
    assert [p["name"] for p in table.partitions] == ["hash1of2", "hash2of2", "null_keys"]
    sql = {c.type: c.sql for c in table.checks}
    assert "unexpected_count" in (sql["not_null"] or "")
    assert "amount > 100" in (sql["custom_sql"] or "")
    assert sql["unique"] is None
    assert any(
        "strip_leading_zeros" in k or "strip_leading_zeros" in c
        for k in table.keys
        for c in table.columns
    )
    data = report.to_dict()
    assert data["tables"][0]["row_engine"] == "duckdb"


def test_explain_reports_problems(tmp_path: Path) -> None:
    config = _config(tmp_path, keys=["code"])
    config["suites"][0]["tables"][0]["comparison"]["partitions"] = {
        "column": "name",
        "values": "auto",
    }
    config["suites"][0]["tables"].append(
        {"name": "missing", "source": {"path": "s.parquet"}, "target": {"path": "nope.parquet"}}
    )
    report = api.explain(_loaded(config, tmp_path))
    first, missing = report.tables
    assert any("integer key" in p for p in first.problems)
    assert first.partitions and "read from the source" in first.partitions[0]["name"]
    assert any("does not exist" in p for p in missing.problems)
    only = api.explain(_loaded(config, tmp_path), table="missing")
    assert [t.table for t in only.tables] == ["missing"]


def test_explain_single_dataset_suite(tmp_path: Path) -> None:
    config = _config(tmp_path)
    suite = config["suites"][0]
    del suite["target"]
    del suite["tables"][0]["target"]
    report = api.explain(_loaded(config, tmp_path))
    assert report.tables[0].row_engine is None and report.tables[0].checks


def test_trace_steps_tolerance_and_duplicates(tmp_path: Path) -> None:
    loaded = _loaded(_config(tmp_path), tmp_path)
    report = api.trace(loaded, table="t", keys=["3"])
    columns = {c.column: c for c in report.columns}
    assert report.source_rows == 2 and report.target_rows == 1
    assert any("duplicated" in n for n in report.notes)
    assert columns["code"].steps[:2] == ["raw", "strip_leading_zeros"]
    assert columns["code"].equal is True
    assert columns["name"].steps[:3] == ["raw", "rtrim", "lower"]
    assert columns["amount"].comparison != "exact"
    two = api.trace(loaded, table="t", keys=["2"])
    amounts = {c.column: c for c in two.columns}
    assert amounts["amount"].equal is False and amounts["name"].equal is True
    one = api.trace(loaded, table="t", keys=["1"])
    assert {c.column: c.equal for c in one.columns}["amount"] is True
    none = api.trace(loaded, table="t", keys=["99"])
    assert none.source_rows == 0 and len(none.notes) == 2
    with pytest.raises(DvfError, match="has no target"):
        config = _config(tmp_path / "single")
        del config["suites"][0]["target"]
        del config["suites"][0]["tables"][0]["target"]
        api.trace(_loaded(config, tmp_path / "single"), table="t", keys=["1"])


def test_trace_in_two_suites_needs_suite(tmp_path: Path) -> None:
    config = _config(tmp_path)
    second = json.loads(json.dumps(config["suites"][0]))
    second["name"] = "s2"
    config["suites"].append(second)
    loaded = _loaded(config, tmp_path)
    with pytest.raises(ConfigError, match="several suites"):
        api.trace(loaded, table="t", keys=["1"])
    assert api.trace(loaded, table="t", keys=["1"], suite="s2").suite == "s2"


def test_trace_on_sqlite_source(tmp_path: Path) -> None:
    load_sqlite(
        {"s": pa.table({"id": [1, 2], "v": ["a", "b"]})}, tmp_path / "db.sqlite", {"s": "id"}
    )
    data = tmp_path / "t"
    write_parquet(pa.table({"id": [1, 2], "v": ["a", "c"]}), data / "t.parquet")
    config = {
        "version": 1,
        "project": {"name": "lite"},
        "connections": {
            "lite": {"type": "sqlite", "database": (tmp_path / "db.sqlite").as_posix()},
            "f": {"type": "files", "path": data.as_posix()},
        },
        "suites": [
            {
                "name": "s",
                "source": "lite",
                "target": "f",
                "tables": [
                    {
                        "name": "t",
                        "source": {"table": "s"},
                        "target": {"path": "t.parquet"},
                        "keys": ["id"],
                    }
                ],
            }
        ],
    }
    report = api.trace(_loaded(config, tmp_path), table="t", keys=["2"])
    assert {c.column: c.equal for c in report.columns}["v"] is False
    assert report.source_hashes[0] != report.target_hashes[0]


def test_load_run_without_extracts_and_rerun_selectors(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["reports"] = {"formats": ["json"], "extracts": {"enabled": False}}
    loaded = _loaded(config, tmp_path)
    output = api.run(loaded, output_dir=tmp_path / "out")
    handle = api.load_run(tmp_path / "out" / "results.json")
    mismatch = handle.check("s.t.compare.column_mismatch")
    table = mismatch.mismatches()
    assert table.num_rows >= 1 and "column" in table.column_names
    pandas = mismatch.mismatches(format="pandas")
    assert len(pandas) == table.num_rows
    polars = mismatch.mismatches(format="polars")
    assert polars.height == table.num_rows
    keys = handle.check("compare.keys")
    assert keys.mismatches().num_rows >= 1  # from its samples
    assert handle.check("not_null.id").mismatches().num_rows == 0
    assert mismatch.queries() == []
    with pytest.raises(ConfigError, match="matches"):
        handle.check(".id")
    selectors = handle.rerun_selectors()
    assert "table:s.t" in selectors.raw
    by_id = api.load_run(output.result.run_id, state=tmp_path / ".dvf")
    assert by_id.output_dir == (tmp_path / "out").resolve()
    with pytest.raises(ConfigError, match="No saved results"):
        api.load_run("nope", state=tmp_path / ".dvf")
    passing = handle.result.model_copy(deep=True)
    for check in passing.iter_checks():
        check.status = Status.PASSED
    with pytest.raises(ConfigError, match="no failed"):
        type(handle)(passing).rerun_selectors()


def test_explain_and_debug_cli_text(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = tmp_path / "dvf.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    text = cli.invoke(app, ["explain", str(path)])
    assert text.exit_code == 0, text.output
    assert "hash_bucket level 0" in text.output and "check" in text.output
    assert "SELECT" not in cli.invoke(app, ["explain", str(path), "--no-sql"]).output
    bad = cli.invoke(app, ["explain", str(tmp_path / "missing.yaml")])
    assert bad.exit_code == 3
    run = cli.invoke(app, ["run", str(path), "--no-reports", "--no-history"])
    assert run.exit_code == 1
    run_id = next((tmp_path / ".dvf" / "runs").iterdir()).name
    detail = cli.invoke(app, ["debug", run_id, "compare.keys", "--config", str(path)])
    assert detail.exit_code == 0 and "SELECT" in detail.output
    as_json = cli.invoke(
        app, ["debug", run_id, "compare.keys", "--config", str(path), "--format", "json"]
    )
    assert json.loads(as_json.output)["type"] == "compare.keys"
    listing = cli.invoke(app, ["debug", run_id, "--config", str(path), "--format", "json"])
    assert "s.t.compare.keys" in json.loads(listing.output)
    empty = cli.invoke(app, ["history", str(path)])
    assert "no runs" in empty.output
    assert cli.invoke(app, ["history", str(path), "--format", "json"]).output.strip() == "[]"
    rerun = cli.invoke(app, ["rerun", "nope"])
    assert rerun.exit_code == 3
