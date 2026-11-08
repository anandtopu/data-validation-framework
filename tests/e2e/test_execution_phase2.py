"""Phase 2 execution: parallel tables with per-connection caps, checkpoints and resume (including
after a killed process), waivers, history and baselines, extracts and repro files, and sinks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.core.enums import Status
from dvf.execution.runner import Runner, RunOptions
from dvf.execution.selectors import Selectors
from dvf.plugins.registry import new_registry
from dvf.scaffold.seeded import write_parquet

HERE = Path(__file__).parent
cli = CliRunner()


def _plugin_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "plugins"
    folder.mkdir(exist_ok=True)
    shutil.copy(HERE / "probe_plugin.py", folder / "probe_plugin.py")
    return folder


def _files(tmp_path: Path, names: list[str], rows: int = 50) -> Path:
    data = tmp_path / "data"
    for name in names:
        write_parquet(
            pa.table({"id": list(range(rows)), "v": [f"x{i}" for i in range(rows)]}),
            data / f"{name}.parquet",
        )
    return data


def _probe_config(
    tmp_path: Path,
    tables: list[str],
    *,
    sleep: float = 0.0,
    interrupt: str | None = None,
    parallel: int = 1,
    cap: int | None = None,
) -> dict[str, Any]:
    data = _files(tmp_path, tables)
    log = (tmp_path / "probe.jsonl").as_posix()
    conn: dict[str, Any] = {"type": "files", "path": data.as_posix()}
    if cap is not None:
        conn["max_parallel_queries"] = cap
    return {
        "version": 1,
        "project": {"name": "probe"},
        "plugins": {"paths": [_plugin_dir(tmp_path).as_posix()]},
        "defaults": {"execution": {"max_parallel_tables": parallel}},
        "connections": {"files": conn},
        "suites": [
            {
                "name": "s",
                "source": "files",
                "tables": [
                    {
                        "name": t,
                        "source": {"path": f"{t}.parquet"},
                        "checks": [
                            {
                                "type": "probe.timed",
                                "log": log,
                                "sleep": sleep,
                                "interrupt": t == interrupt,
                            },
                            {"type": "not_null", "columns": ["id"]},
                        ],
                    }
                    for t in tables
                ],
            }
        ],
        "history": {"enabled": False},
        "reports": {"formats": ["json"], "output_dir": "./reports/${run.id}"},
    }


def _probes(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "probe.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _max_overlap(records: list[dict[str, Any]]) -> int:
    events = sorted([(r["start"], 1) for r in records] + [(r["end"], -1) for r in records])
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _run(config: dict[str, Any], base: Path, **options: Any) -> api.RunOutput:
    loaded = api.load_config_data(config, base_dir=base, strict=False, registry=new_registry())
    return Runner(loaded, RunOptions(**options)).run()


# ====================================================================== parallel execution


def test_parallel_matches_sequential_and_keeps_order(tmp_path: Path) -> None:
    tables = [f"t{i}" for i in range(6)]
    sequential = _run(_probe_config(tmp_path / "a", tables), tmp_path / "a", write_reports=False)
    parallel = _run(
        _probe_config(tmp_path / "b", tables, parallel=4, cap=4, sleep=0.2),
        tmp_path / "b",
        write_reports=False,
    )
    names = [d.name for d in parallel.result.suites[0].datasets]
    assert names == tables
    assert [c.check_id for c in parallel.result.iter_checks()] == [
        c.check_id for c in sequential.result.iter_checks()
    ]
    assert parallel.result.status is Status.PASSED
    assert _max_overlap(_probes(tmp_path / "b")) >= 2


def test_connection_cap_limits_concurrent_tables(tmp_path: Path) -> None:
    tables = [f"t{i}" for i in range(6)]
    config = _probe_config(tmp_path, tables, parallel=6, cap=2, sleep=0.25)
    output = _run(config, tmp_path, write_reports=False)
    assert output.result.status is Status.PASSED
    records = _probes(tmp_path)
    assert len(records) == 6
    assert _max_overlap(records) <= 2


def test_parallel_option_overrides_config(tmp_path: Path) -> None:
    config = _probe_config(tmp_path, ["a", "b", "c"], parallel=3, cap=3, sleep=0.2)
    _run(config, tmp_path, write_reports=False, parallel=1)
    assert _max_overlap(_probes(tmp_path)) == 1


# ====================================================================== checkpoints and resume


def test_interrupt_writes_partial_results_and_resume_finishes(tmp_path: Path) -> None:
    tables = ["a", "b", "c", "d"]
    config = _probe_config(tmp_path, tables, interrupt="c")
    first = _run(config, tmp_path)
    result = first.result
    assert result.interrupted
    assert api.exit_code(result) == 2
    statuses = {d.name: d.status for d in result.suites[0].datasets}
    assert statuses["a"] is Status.PASSED and statuses["b"] is Status.PASSED
    assert statuses["c"] is Status.SKIPPED and statuses["d"] is Status.SKIPPED
    assert (first.output_dir or tmp_path) / "results.json"
    assert len(_probes(tmp_path)) == 2
    # The cause is fixed; resume runs only the unfinished tables, under the same run ID.
    config["suites"][0]["tables"][2]["checks"][0]["interrupt"] = False
    second = _run(config, tmp_path, resume=result.run_id, force_resume=True)
    assert second.result.run_id == result.run_id
    assert second.result.resumed_from_checkpoint
    assert not second.result.interrupted
    assert all(d.status is Status.PASSED for d in second.result.suites[0].datasets)
    assert [r["table"] for r in _probes(tmp_path)] == [
        "a.parquet",
        "b.parquet",
        "c.parquet",
        "d.parquet",
    ]


def test_resume_refuses_a_changed_configuration(tmp_path: Path) -> None:
    config = _probe_config(tmp_path, ["a", "b"], interrupt="b")
    first = _run(config, tmp_path, write_reports=False)
    config["suites"][0]["tables"][1]["checks"][0]["interrupt"] = False
    with pytest.raises(api.ConfigError, match="configuration changed"):
        _run(config, tmp_path, resume=first.result.run_id)
    with pytest.raises(api.ConfigError, match="No checkpoints"):
        _run(config, tmp_path, resume="20990101T000000Z-dead")


def test_resume_reuses_the_original_selectors(tmp_path: Path) -> None:
    config = _probe_config(tmp_path, ["a", "b", "c"], interrupt="b")
    first = _run(
        config, tmp_path, write_reports=False, selectors=Selectors.parse(["table:a", "table:b"])
    )
    assert [d.name for d in first.result.suites[0].datasets] == ["a", "b"]
    config["suites"][0]["tables"][1]["checks"][0]["interrupt"] = False
    second = _run(
        config, tmp_path, write_reports=False, resume=first.result.run_id, force_resume=True
    )
    assert [d.name for d in second.result.suites[0].datasets] == ["a", "b"]


def test_partition_checkpoints_are_reused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_parquet(
        pa.table({"id": list(range(100)), "v": [str(i) for i in range(100)]}), data / "s.parquet"
    )
    write_parquet(
        pa.table({"id": list(range(100)), "v": [str(i) if i != 42 else "x" for i in range(100)]}),
        data / "t.parquet",
    )
    config = {
        "version": 1,
        "project": {"name": "parts"},
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
                        "comparison": {
                            "strategies": ["row_hash"],
                            "on_mismatch": "full_diff",
                            "partitions": {"hash_buckets": 4},
                        },
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }
    first = _run(config, tmp_path, write_reports=False)
    run_id = first.result.run_id
    table_file = tmp_path / ".dvf" / "runs" / run_id / "tables" / "s.t.json"
    assert table_file.exists()
    table_file.unlink()  # as if the run died after the partitions but before the table finished
    second = _run(config, tmp_path, write_reports=False, resume=run_id)
    keys = next(c for c in second.result.iter_checks() if c.type == "compare.keys")
    parts = keys.details["partitions"]
    assert len(parts) == 5 and all(p["from_checkpoint"] for p in parts)
    mismatch = next(c for c in second.result.iter_checks() if c.type == "compare.column_mismatch")
    assert mismatch.column_mismatches[0].mismatch_count == 1


def _wait_for_checkpoints(proc: subprocess.Popen[bytes], runs: Path, count: int) -> Path | None:
    deadline = time.time() + 120
    while time.time() < deadline:
        tables_dir = next(iter(runs.glob("*/tables")), None) if runs.exists() else None
        if tables_dir is not None and len(list(tables_dir.glob("*.json"))) >= count:
            return tables_dir.parent
        if proc.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def test_resume_after_killed_process(tmp_path: Path) -> None:
    """Phase 2 acceptance: kill a running `dvf run`, then `dvf run --resume` finishes it."""
    tables = ["a", "b", "c", "d", "e"]
    config = _probe_config(tmp_path, tables, sleep=1.5)
    path = tmp_path / "dvf.yaml"
    path.write_text(json.dumps(config), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-m", "dvf", "run", str(path), "--no-reports"]
    log_path = tmp_path / "first-run.log"
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(  # noqa: S603 - runs dvf itself
            command, cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=log_file
        )
        run_dir = _wait_for_checkpoints(proc, tmp_path / ".dvf" / "runs", 2)
        proc.kill()  # TerminateProcess on Windows, SIGKILL elsewhere: no cleanup runs
        proc.wait(timeout=30)
    assert run_dir is not None, log_path.read_text(encoding="utf-8", errors="replace")
    done_before = {r["table"] for r in _probes(tmp_path)}
    assert 2 <= len(done_before) < len(tables)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    resumed = subprocess.run(  # noqa: S603 - runs dvf itself
        [*command[:-1], "--resume", run_dir.name, "-o", str(tmp_path / "out")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr.decode()
    result = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert result["run_id"] == run_dir.name and result["resumed_from_checkpoint"]
    assert [d["name"] for d in result["suites"][0]["datasets"]] == tables
    assert all(d["status"] == "passed" for d in result["suites"][0]["datasets"])
    runs_per_table: dict[str, int] = {}
    for record in _probes(tmp_path):
        runs_per_table[record["table"]] = runs_per_table.get(record["table"], 0) + 1
    # Tables finished before the kill were not run again.
    for table in done_before:
        assert runs_per_table[table] == 1
    assert (
        json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == "completed"
    )


# ====================================================================== waivers


def _pair(tmp_path: Path, **table: Any) -> dict[str, Any]:
    data = tmp_path / "data"
    write_parquet(
        pa.table({"id": [1, 2, 3, 4], "name": ["a", "b", "c", "d"], "city": ["x", "y", "z", "w"]}),
        data / "s.parquet",
    )
    write_parquet(
        pa.table({"id": [1, 2, 3, 4], "name": ["a", "B", "c", "d"], "city": ["x", "y", "Z", "w"]}),
        data / "t.parquet",
    )
    return {
        "version": 1,
        "project": {"name": "waivers"},
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
                        "comparison": {"strategies": ["row_hash"], "on_mismatch": "full_diff"},
                        **table,
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }


def _status(result: Any, check_type: str) -> Any:
    return next(c for c in result.iter_checks() if c.type == check_type)


def test_column_waivers(tmp_path: Path) -> None:
    config = _pair(tmp_path)
    config["waivers"] = [
        {
            "check": "s.t.compare.column_mismatch",
            "column": "name",
            "reason": "case fix pending",
            "ticket": "CR-1",
        },
    ]
    result = _run(config, tmp_path, write_reports=False).result
    mismatch = _status(result, "compare.column_mismatch")
    assert mismatch.status is Status.FAILED  # city still differs
    assert {m.column: m.waived for m in mismatch.column_mismatches} == {"name": True, "city": False}
    assert any("Waived columns: name" in h for h in mismatch.hints)
    config["waivers"].append(
        {"check": "s.t.compare.*", "column": "city", "reason": "accepted", "expires": "2099-01-01"}
    )
    config["waivers"].append({"check": "s.t.compare.row_hash", "reason": "known"})
    config["waivers"].append({"check": "s.t.compare.keys", "reason": "unused one"})
    result = _run(config, tmp_path, write_reports=False).result
    mismatch = _status(result, "compare.column_mismatch")
    assert mismatch.status is Status.WAIVED and mismatch.waiver is not None
    assert _status(result, "compare.row_hash").status is Status.WAIVED
    assert api.exit_code(result) == 0
    assert [w.status for w in result.waivers] == ["applied", "applied", "applied", "unused"]


def test_expired_waiver_keeps_the_failure(tmp_path: Path) -> None:
    config = _pair(tmp_path)
    config["waivers"] = [
        {
            "check": "s.t.compare.row_hash",
            "reason": "temporary",
            "ticket": "CR-9",
            "expires": "2026-01-31",
        }
    ]
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False, registry=new_registry())
    result = Runner(loaded, RunOptions(write_reports=False, today=date(2026, 11, 7))).run().result
    row_hash = _status(result, "compare.row_hash")
    assert row_hash.status is Status.FAILED
    assert row_hash.waiver is not None and row_hash.waiver.expired
    assert any("expired on 2026-01-31" in h for h in row_hash.hints)
    assert result.waivers[0].status == "expired" and result.waivers[0].checks == [
        "s.t.compare.row_hash"
    ]
    assert api.exit_code(result) == 1


# ====================================================================== history and baselines


@pytest.mark.parametrize(
    "store", ["duckdb:///./.dvf/history.duckdb", "sqlite:///hist/history.sqlite"]
)
def test_history_baseline_and_trend(store: str, tmp_path: Path) -> None:
    config = _pair(tmp_path)
    config["history"] = {"enabled": True, "store": store, "baseline": "last_run"}
    first = _run(config, tmp_path, write_reports=False).result
    assert first.baseline is None and first.trend == []
    second = _run(config, tmp_path, write_reports=False).result
    assert second.baseline is not None
    assert second.baseline.baseline_run_id == first.run_id
    assert "s.t.compare.column_mismatch" in second.baseline.persisting
    assert second.baseline.new_failures == []
    assert [p.run_id for p in second.trend] == [first.run_id]
    # Fix the target: failures resolve against the baseline.
    data = tmp_path / "data"
    shutil.copy(data / "s.parquet", data / "t.parquet")
    third = _run(config, tmp_path, write_reports=False).result
    assert third.status is Status.PASSED
    assert third.baseline is not None and "s.t.compare.row_hash" in third.baseline.resolved
    assert [p.run_id for p in third.trend] == [first.run_id, second.run_id]
    passed_policy = _run(config, tmp_path, write_reports=False, baseline="last_passed").result
    assert (
        passed_policy.baseline is not None
        and passed_policy.baseline.baseline_run_id == third.run_id
    )
    runs = api.run_history(api.load_config_data(config, base_dir=tmp_path, strict=False))
    assert [r["run_id"] for r in runs][:2] == [passed_policy.run_id, third.run_id]


def test_history_retention_prunes_runs(tmp_path: Path) -> None:
    config = _pair(tmp_path)
    config["history"] = {"enabled": True, "retention_runs": 2}
    for _ in range(3):
        _run(config, tmp_path, write_reports=False)
    runs = api.run_history(api.load_config_data(config, base_dir=tmp_path, strict=False))
    assert len(runs) == 2
    assert len(list((tmp_path / ".dvf" / "runs").iterdir())) == 2


# ====================================================================== extracts, repro, sinks


def test_extracts_and_repro_files(tmp_path: Path) -> None:
    config = _pair(
        tmp_path,
        checks=[{"type": "regex", "column": "name", "pattern": "^[a-c]$"}],
        sample_size=1,
    )
    config["reports"] = {"formats": ["json"], "extracts": {"format": "csv"}}
    path = tmp_path / "dvf.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    loaded = api.load_config(path, strict=False, registry=new_registry())
    output = Runner(loaded, RunOptions(output_dir=tmp_path / "out")).run()
    regex = _status(output.result, "regex")
    kinds = {a.kind for a in regex.artifacts}
    assert kinds == {"extract", "repro_sql", "repro_config"}
    extract = next(a for a in regex.artifacts if a.kind == "extract")
    assert extract.rows == 2 and (tmp_path / "out" / extract.path).exists()
    mismatch = _status(output.result, "compare.column_mismatch")
    assert any(a.kind == "extract" for a in mismatch.artifacts)
    repro = next(a for a in regex.artifacts if a.kind == "repro_config")
    rerun = api.run(tmp_path / "out" / repro.path, write_reports=False, history=False)
    checks = list(rerun.result.iter_checks())
    assert [c.type for c in checks] == ["schema.object_exists", "regex"] or [
        c.type for c in checks
    ][-1] == "regex"
    assert checks[-1].status is Status.FAILED and checks[-1].unexpected_count == 2


def test_strict_privacy_writes_no_extracts(tmp_path: Path) -> None:
    config = _pair(
        tmp_path, checks=[{"type": "regex", "column": "name", "pattern": "^[a-c]$"}], sample_size=1
    )
    config["defaults"] = {"privacy": {"mode": "strict"}}
    output = _run(config, tmp_path, output_dir=tmp_path / "out")
    assert not any(a.kind == "extract" for c in output.result.iter_checks() for a in c.artifacts)


def test_result_sinks_receive_runs(tmp_path: Path) -> None:
    config = _pair(tmp_path)
    config["sinks"] = [
        {"type": "duckdb", "path": "sink/results.duckdb"},
        {"type": "snowflake", "url": "snowflake://x"},
    ]
    result = _run(config, tmp_path, write_reports=False).result
    con = duckdb.connect(str(tmp_path / "sink" / "results.duckdb"), read_only=True)
    try:
        runs = con.execute("SELECT run_id FROM dvf_runs").fetchall()
        checks = con.execute("SELECT COUNT(*) FROM dvf_checks").fetchone()
    finally:
        con.close()
    assert runs == [(result.run_id,)]
    assert checks is not None and checks[0] == len(list(result.iter_checks()))
    assert any("snowflake" in e for e in result.metadata["sink_errors"])


# ====================================================================== CLI


def test_debugging_commands(tmp_path: Path) -> None:
    config = _pair(tmp_path)
    config["history"] = {"enabled": True}
    path = tmp_path / "dvf.yaml"
    path.write_text(json.dumps(config), encoding="utf-8")
    run = cli.invoke(app, ["run", str(path), "-o", str(tmp_path / "out")])
    assert run.exit_code == 1, run.output
    run_id = next((tmp_path / ".dvf" / "runs").iterdir()).name

    explained = cli.invoke(app, ["explain", str(path), "--format", "json"])
    assert explained.exit_code == 0, explained.output
    report = json.loads(explained.output)
    assert report["tables"][0]["row_engine"] == "duckdb"
    assert any("projection" in q["purpose"] for q in report["tables"][0]["queries"])
    assert cli.invoke(app, ["explain", str(path), "--table", "t"]).exit_code == 0

    traced = cli.invoke(app, ["trace", str(path), "--table", "t", "--key", "2", "--format", "json"])
    assert traced.exit_code == 0, traced.output
    columns = {c["column"]: c for c in json.loads(traced.output)["columns"]}
    assert columns["name"]["equal"] is False and columns["city"]["equal"] is True
    text = cli.invoke(app, ["trace", str(path), "--table", "t", "--key", "2"])
    assert "differs" in text.output
    assert cli.invoke(app, ["trace", str(path), "--table", "nope", "--key", "2"]).exit_code == 3
    assert (
        cli.invoke(app, ["trace", str(path), "--table", "t", "--key", "1", "--key", "2"]).exit_code
        == 3
    )

    listed = cli.invoke(app, ["debug", run_id, "--config", str(path)])
    assert listed.exit_code == 0 and "compare.column_mismatch" in listed.output
    detail = cli.invoke(
        app, ["debug", run_id, "s.t.compare.column_mismatch", "--config", str(path)]
    )
    assert detail.exit_code == 0 and "extract" in detail.output
    by_file = cli.invoke(app, ["debug", str(tmp_path / "out" / "results.json"), "row_hash"])
    assert by_file.exit_code == 0, by_file.output
    assert (
        cli.invoke(app, ["debug", run_id, "nothing_like_it", "--config", str(path)]).exit_code == 3
    )

    history = cli.invoke(app, ["history", str(path)])
    assert history.exit_code == 0 and run_id[:12] in history.output

    rerun = cli.invoke(app, ["rerun", run_id, "--config", str(path), "--no-reports"])
    assert rerun.exit_code == 1, rerun.output
    failed_from = cli.invoke(
        app, ["run", str(path), "--failed-from", run_id, "--no-reports", "--no-history"]
    )
    assert failed_from.exit_code == 1
    assert "compare.row_count" not in failed_from.output

    handle = api.load_run(run_id, config=path)
    frame = handle.check("s.t.compare.column_mismatch").mismatches()
    assert frame.num_rows == 2 and "classification" in frame.column_names
    assert handle.check("row_hash").result.status is Status.FAILED
