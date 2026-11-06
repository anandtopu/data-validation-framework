"""The Phase 1 commands: init, run, compare, report, and connections test."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from dvf.cli.app import app
from dvf.compare.adhoc import AdHocSpec, adhoc_config, parse_location
from dvf.core.errors import ConfigError
from dvf.scaffold.example import example_config
from dvf.scaffold.seeded import write_csv, write_parquet
from tests.conftest import EXAMPLES

runner = CliRunner()


def test_committed_example_matches_the_scaffold() -> None:
    committed = (EXAMPLES / "local_files" / "dvf.yaml").read_text(encoding="utf-8")
    assert committed == example_config("../../schemas/config.schema.json")
    manifest = json.loads(
        (EXAMPLES / "local_files" / "data" / "seeded_defects.json").read_text(encoding="utf-8")
    )
    assert manifest["customers"]["mismatched_rows"] == 24


def test_init_example_and_run(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    result = runner.invoke(app, ["init", str(project), "--example"])
    assert result.exit_code == 0, result.output
    assert (project / "dvf.schema.json").exists()
    again = runner.invoke(app, ["init", str(project), "--example"])
    assert again.exit_code == 3 and "already exists" in again.output
    run = runner.invoke(
        app, ["run", str(project / "dvf.yaml"), "-o", str(tmp_path / "out"), "--verbose"]
    )
    assert run.exit_code == 1, run.output
    assert "timezone_shift" in run.output and "encoding" in run.output
    for name in ("results.json", "report.html", "junit.xml", "summary.md"):
        assert (tmp_path / "out" / name).exists()
    lenient = runner.invoke(
        app, ["run", str(project / "dvf.yaml"), "--no-reports", "--only", "check:row_count_range"]
    )
    assert lenient.exit_code == 0, lenient.output
    report = runner.invoke(
        app,
        [
            "report",
            "--from",
            str(tmp_path / "out" / "results.json"),
            "-f",
            "junit",
            "-f",
            "markdown",
            "-o",
            str(tmp_path / "again"),
            "--title",
            "Again",
        ],
    )
    assert report.exit_code == 0, report.output
    assert (tmp_path / "again" / "junit.xml").exists()
    bad = runner.invoke(
        app, ["report", "--from", str(tmp_path / "missing.json"), "-f", "html", "-o", str(tmp_path)]
    )
    assert bad.exit_code == 3


def test_init_starter(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path / "starter")])
    assert result.exit_code == 0, result.output
    validated = runner.invoke(app, ["validate-config", str(tmp_path / "starter" / "dvf.yaml")])
    assert validated.exit_code == 0, validated.output


def test_run_errors(tmp_path: Path, write: object) -> None:
    missing = runner.invoke(app, ["run", str(tmp_path / "nope.yaml")])
    assert missing.exit_code == 3
    config = tmp_path / "bad.yaml"
    config.write_text(
        "version: 1\nproject: {name: x}\nconnections: {db: {type: duckdb}}\n"
        "suites: [{name: s, source: db, tables: [{name: t, source: {table: t}}]}]\n",
        encoding="utf-8",
    )
    bad_selector = runner.invoke(app, ["run", str(config), "--only", "colour:red", "--no-reports"])
    assert bad_selector.exit_code == 3
    not_found = runner.invoke(app, ["run", str(config), "--no-reports"])
    assert not_found.exit_code == 1, not_found.output
    assert "not found" in not_found.output
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "{type: duckdb}", "{type: sqlite, database: x.sqlite}"
        ),
        encoding="utf-8",
    )
    errored = runner.invoke(app, ["run", str(config), "--no-reports"])
    assert errored.exit_code == 2, errored.output


def _files(tmp_path: Path) -> tuple[Path, Path]:
    source = pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    target = pa.table({"id": [1, 2, 4], "name": ["a", "B", "d"]})
    write_csv(source, tmp_path / "a.csv")
    write_parquet(target, tmp_path / "b.parquet")
    return tmp_path / "a.csv", tmp_path / "b.parquet"


def test_compare_command(tmp_path: Path) -> None:
    a, b = _files(tmp_path)
    result = runner.invoke(
        app, ["compare", str(a), str(b), "-k", "id", "-o", str(tmp_path / "cmp")]
    )
    assert result.exit_code == 1, result.output
    assert "case_only" in result.output
    assert (tmp_path / "cmp" / "report.html").exists()
    same = runner.invoke(app, ["compare", str(a), str(a), "--no-reports", "--engine", "polars"])
    assert same.exit_code == 0, same.output
    bad = runner.invoke(app, ["compare", "ftp://x::t", str(a)])
    assert bad.exit_code == 3


def test_connections_test_command(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    config = tmp_path / "dvf.yaml"
    config.write_text(
        "version: 1\nproject: {name: x}\nconnections:\n"
        "  mem: {type: duckdb}\n  files: {type: files, path: ./data}\n"
        "  gone: {type: files, path: ./missing}\n  lite: {type: sqlite, database: none.sqlite}\n"
        "  sf: {type: snowflake, account: acme}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["connections", "test", str(config)])
    assert result.exit_code == 2
    assert "ok" in result.output and "failed" in result.output and "not available" in result.output


def test_adhoc_locations(tmp_path: Path) -> None:
    conn, spec, name = parse_location(str(tmp_path / "x.parquet"))
    assert conn["type"] == "files" and spec == {"path": "x.parquet"} and name == "x"
    assert parse_location("postgresql+psycopg://u@h/db::s.t")[0] == {
        "type": "postgres",
        "url": "postgresql+psycopg://u@h/db",
    }
    assert parse_location("duckdb:///tmp/a.duckdb::main.t")[0] == {
        "type": "duckdb",
        "database": "/tmp/a.duckdb",
    }
    assert parse_location("sqlite:///a.sqlite::t")[1] == {"table": "t"}
    with pytest.raises(ConfigError, match="needs a table"):
        parse_location("duckdb:///a.duckdb")
    config = adhoc_config(
        AdHocSpec(source=str(tmp_path / "a.csv"), target="sqlite:///b.sqlite::my table")
    )
    assert config["suites"][0]["tables"][0]["name"] == "my_table"
