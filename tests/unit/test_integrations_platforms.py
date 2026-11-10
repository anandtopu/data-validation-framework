"""Databricks Workflows and AWS Glue entry points, with stand-ins for dbutils and S3."""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path
from typing import Any

import pytest

from dvf.core.errors import ConfigError
from dvf.integrations import aws, databricks
from dvf.integrations.common import QualityGateError
from tests.unit.test_integrations_common import (
    FakeRun,
    StubInstaller,
    example_project,
    fake_run,
    import_with_stubs,
    module,
)

__all__ = ["example_project", "fake_run", "import_with_stubs"]  # fixtures used here


class DbUtils:
    """The parts of Databricks dbutils that dvf uses."""

    def __init__(self, fail_task_values: bool = False) -> None:
        self.values: dict[str, Any] = {}
        self.exited: list[str] = []
        self._fail = fail_task_values
        self.jobs = types.SimpleNamespace(taskValues=types.SimpleNamespace(set=self._set))
        self.notebook = types.SimpleNamespace(exit=self.exited.append)

    def _set(self, key: str, value: Any) -> None:
        if self._fail:
            raise RuntimeError("task values are only available in job runs")
        self.values[key] = value


# ---------------------------------------------------------------------- Databricks


def test_databricks_main_runs_sets_task_values_and_exits_by_gate(
    fake_run: FakeRun,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    dbutils = DbUtils()
    monkeypatch.setattr(databricks, "_dbutils", lambda: dbutils)
    with pytest.raises(SystemExit) as info:
        databricks.main(
            [
                "run",
                "/Volumes/main/qa/dvf.yaml",
                "--only",
                "tag:pr",
                "--only",
                "table:orders",
                "--set",
                "defaults.sample_size=5",
                "--var",
                "env=qa",
                "--output-dir",
                str(tmp_path / "out"),
                "--format",
                "junit",
                "--min-score",
                "90",
                "--no-history",
            ]
        )
    assert info.value.code == 1
    call = fake_run.calls[0]
    assert call["config"] == "/Volumes/main/qa/dvf.yaml"
    assert call["only"] == ["tag:pr", "table:orders"]
    assert call["overrides"] == ["defaults.sample_size=5"] and call["variables"] == {"env": "qa"}
    assert call["formats"] == ["junit"] and call["history"] is False
    assert dbutils.values["dvf_exit_code"] == 1 and dbutils.values["dvf_passed"] is False
    assert set(dbutils.values) == {f"dvf_{k}" for k in databricks.TASK_VALUES}
    out = capsys.readouterr().out
    assert "quality gates failed" in out and "results:" in out


def test_databricks_main_returns_normally_when_gates_pass(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(databricks, "_dbutils", lambda: None)
    databricks.main(["dvf.yaml", "--fail-on", "critical"])  # no SystemExit
    assert fake_run.calls[0]["config"] == "dvf.yaml"


def test_databricks_main_maps_errors_to_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        databricks.main(["dvf.yaml", "--var", "novalue"])
    assert info.value.code == 3
    assert "NAME=VALUE" in capsys.readouterr().err

    def broken(config: Any, **kwargs: Any) -> Any:
        from dvf.core.errors import ConnectorError

        raise ConnectorError("could not connect")

    monkeypatch.setattr("dvf.api.run", broken)
    with pytest.raises(SystemExit) as info:
        databricks.main(["dvf.yaml"])
    assert info.value.code == 2
    assert "execution error: could not connect" in capsys.readouterr().err


def test_databricks_main_reads_sys_argv(fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["dvf-databricks", "cfg.yaml", "--fail-on", "critical"])
    monkeypatch.setattr(databricks, "_dbutils", lambda: None)
    databricks.main()
    assert fake_run.calls[0]["config"] == "cfg.yaml"


def test_databricks_run_notebook(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dbutils = DbUtils()
    with pytest.raises(QualityGateError):
        databricks.run_notebook("dvf.yaml", only="tag:nightly", dbutils=dbutils)
    assert dbutils.values["dvf_passed"] is False and dbutils.exited == []
    assert "# dvf run R1" in capsys.readouterr().out  # Markdown shown (plain text here)
    summary = databricks.run_notebook(
        "dvf.yaml", raise_on_failure=False, exit_notebook=True, dbutils=dbutils
    )
    assert summary.exit_code == 1
    assert json.loads(dbutils.exited[0])["run_id"] == "R1"


def test_databricks_task_values_are_optional(fake_run: FakeRun) -> None:
    from dvf.integrations.common import summarize

    summary = summarize(fake_run.result)
    assert databricks.set_task_values(summary, DbUtils(fail_task_values=True)) is False
    assert databricks.set_task_values(summary, None) is False  # not on Databricks


def test_databricks_finds_dbutils(import_with_stubs: StubInstaller) -> None:
    dbutils = DbUtils()
    runtime = module("databricks.sdk.runtime", dbutils=dbutils)
    stubs = {
        "databricks": module("databricks", __path__=[]),
        "databricks.sdk": module("databricks.sdk", __path__=[], runtime=runtime),
        "databricks.sdk.runtime": runtime,
    }
    loaded = import_with_stubs("dvf.integrations.databricks", stubs)
    assert loaded._dbutils() is dbutils


def test_databricks_finds_dbutils_in_a_notebook(import_with_stubs: StubInstaller) -> None:
    dbutils = DbUtils()
    shell = types.SimpleNamespace(user_ns={"dbutils": dbutils})
    display_calls: list[Any] = []
    display = module(
        "IPython.display", display=display_calls.append, Markdown=lambda text: ("md", text)
    )
    stubs = {
        "IPython": module("IPython", __path__=[], get_ipython=lambda: shell, display=display),
        "IPython.display": display,
    }
    loaded = import_with_stubs("dvf.integrations.databricks", stubs)
    assert loaded._dbutils() is dbutils
    loaded._display_markdown("# hi")
    assert display_calls == [("md", "# hi")]


# ---------------------------------------------------------------------- AWS Glue


class FakeS3:
    """list_objects_v2 pages, download_file, and upload_file over a local folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploaded: dict[str, bytes] = {}

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"
        return types.SimpleNamespace(paginate=self._paginate)

    def _paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
        base = self.root / Bucket
        keys = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
        matching = [{"Key": k} for k in keys if k.startswith(Prefix)]
        return [{"Contents": matching[:2]}, {"Contents": matching[2:]}, {}]

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        shutil.copyfile(self.root / bucket / key, filename)

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.uploaded[f"s3://{bucket}/{key}"] = Path(filename).read_bytes()


@pytest.fixture
def bucket(tmp_path: Path, example_project: Path) -> FakeS3:
    project = tmp_path / "s3" / "data-bucket" / "dvf" / "project"
    shutil.copytree(
        example_project.parent, project, ignore=shutil.ignore_patterns(".dvf", "reports")
    )
    (tmp_path / "s3" / "data-bucket" / "dvf" / "project" / "folder").mkdir()
    return FakeS3(tmp_path / "s3")


def test_glue_job_runs_a_project_from_s3_and_uploads_reports(
    bucket: FakeS3, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "--JOB_NAME",
        "dvf-validation",
        "--project_s3_uri",
        "s3://data-bucket/dvf/project",
        "--reports_s3_uri",
        "s3://data-bucket/dvf/reports/",
        "--only",
        "check:row_count_range",
        "--fail_on",
        "error",
    ]
    summary = aws.glue_main(args, client=bucket)
    assert summary.passed and summary.quality_score == 100.0
    prefix = f"s3://data-bucket/dvf/reports/{summary.run_id}/"
    assert summary.report_dir == prefix.rstrip("/")
    assert summary.results_path == prefix + "results.json"
    assert prefix + "results.json" in bucket.uploaded and prefix + "report.html" in bucket.uploaded
    assert set(summary.reports) == set(bucket.uploaded)
    assert "uploaded" in capsys.readouterr().out


def test_glue_job_fails_by_gate_after_uploading(bucket: FakeS3) -> None:
    with pytest.raises(QualityGateError) as info:
        aws.glue_main(
            [
                "--project_s3_uri",
                "s3://data-bucket/dvf/project/",
                "--reports_s3_uri",
                "s3://data-bucket/dvf/reports",
                "--only",
                "tag:pr",
                "--min_score",
                "50",
            ],
            client=bucket,
        )
    summary = info.value.summary
    assert summary.exit_code == 1 and summary.min_score == 50
    assert summary.results_path in bucket.uploaded


def test_glue_job_with_a_single_configuration_file(bucket: FakeS3) -> None:
    # Only the configuration is downloaded, not its data files, so the tables cannot be read.
    with pytest.raises(QualityGateError) as info:
        aws.glue_main(["--config_s3_uri", "s3://data-bucket/dvf/project/dvf.yaml"], client=bucket)
    assert info.value.exit_code in (1, 2)
    assert info.value.summary.report_dir is None and bucket.uploaded == {}


def test_glue_job_arguments_are_checked(bucket: FakeS3) -> None:
    with pytest.raises(ConfigError, match="project_s3_uri"):
        aws.glue_main([], client=bucket)
    with pytest.raises(ConfigError, match="was not found"):
        aws.glue_main(
            ["--project_s3_uri", "s3://data-bucket/dvf/project/", "--config", "missing.yaml"],
            client=bucket,
        )
    with pytest.raises(ConfigError, match="s3://bucket/key"):
        aws.split_s3_uri("https://example.com/x")


def test_glue_download_refuses_keys_outside_the_folder(tmp_path: Path) -> None:
    class Evil(FakeS3):
        def _paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
            return [{"Contents": [{"Key": Prefix + "../../escape.txt"}]}]

    with pytest.raises(ConfigError, match="outside"):
        aws.download_prefix("s3://b/p/", tmp_path / "target", client=Evil(tmp_path))


def test_glue_needs_boto3_without_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(aws.importlib, "import_module", missing)
    with pytest.raises(ConfigError, match="boto3"):
        aws.upload_folder(Path(), "s3://b/p")


def test_glue_creates_a_boto3_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeS3(tmp_path)
    boto3 = types.SimpleNamespace(client=lambda service: fake if service == "s3" else None)
    monkeypatch.setattr(aws.importlib, "import_module", lambda name: boto3)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_text("a", encoding="utf-8")
    assert aws.upload_folder(tmp_path / "out", "s3://b/p/") == ["s3://b/p/a.txt"]


def test_glue_main_reads_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["glue", "--JOB_NAME", "x"])
    with pytest.raises(ConfigError):
        aws.glue_main(client=object())
