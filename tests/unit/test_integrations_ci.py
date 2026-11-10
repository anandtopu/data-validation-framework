"""The GitHub Action, the CI templates, the CI example, and the orchestrator example files.

These files cannot run here (they need GitHub, GitLab, Azure DevOps, Jenkins, or a cloud), so
the tests check what can be checked locally: every YAML and JSON file parses, the action's
inputs and outputs are wired, embedded Python compiles, the templates publish JUnit and
Markdown and have PR and nightly modes, and the CI example selects the right checks by tag.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from dvf import api
from dvf.integrations.common import run_validation
from tests.conftest import EXAMPLES, REPO_ROOT

ACTION = REPO_ROOT / "action.yml"
SELFTEST = REPO_ROOT / ".github" / "workflows" / "action-selftest.yml"
TEMPLATES = REPO_ROOT / "ci-templates"
ORCHESTRATORS = EXAMPLES / "orchestrators"
CI_EXAMPLE = EXAMPLES / "ci" / "dvf.yaml"

_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The ``on:`` mapping (YAML 1.1 reads the key ``on`` as True)."""
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


def heredocs(script: str, marker: str = "PY") -> list[str]:
    """Bodies of ``<<'PY'`` here-documents in a shell script."""
    bodies = re.findall(rf"<<'{marker}'\n(.*?)\n{marker}\n", script + "\n", flags=re.DOTALL)
    return [textwrap.dedent(body) for body in bodies]


# ---------------------------------------------------------------------- the GitHub Action


def test_action_is_a_composite_action_with_wired_inputs_and_outputs() -> None:
    action = load_yaml(ACTION)
    assert action["runs"]["using"] == "composite"
    inputs = set(action["inputs"])
    assert {"config", "profile", "only", "fail-on", "min-score", "formats", "output-dir"} <= inputs
    assert {"extras", "version", "python-version", "upload-artifact", "install-from"} <= inputs
    steps = action["runs"]["steps"]
    ids = {s["id"] for s in steps if "id" in s}
    text = ACTION.read_text(encoding="utf-8")
    for expression in _EXPR.findall(text):
        for name in re.findall(r"inputs\.([a-z0-9-]+)", expression):
            assert name in inputs, f"unknown input {name}"
        for step, output in re.findall(r"steps\.([a-z]+)\.outputs\.([a-z0-9-]+)", expression):
            assert step in ids, f"unknown step {step}"
            assert re.search(rf"\b{re.escape(output)}=", text), f"output {output} is never set"
    for name, spec in action["outputs"].items():
        assert spec["value"].startswith("${{ steps."), name
    assert {"exit-code", "quality-score", "results-path", "report-dir"} <= set(action["outputs"])
    for step in steps:
        if "run" in step:
            assert step["shell"] == "bash"
            # Inputs reach scripts through env, never by expansion inside the script.
            assert "${{" not in step["run"], step["name"]
        if "uses" in step:
            assert re.fullmatch(r"actions/[a-z-]+@v\d+", step["uses"]), step["uses"]


def test_action_runs_dvf_with_the_gates_and_publishes_the_summary() -> None:
    action = load_yaml(ACTION)
    run = next(s for s in action["runs"]["steps"] if s.get("id") == "run")["run"]
    for flag in ("--only", "--fail-on", "--min-score", "--profile", "--output-dir", "--strict"):
        assert flag in run
    assert "GITHUB_STEP_SUMMARY" in run and "summary.md" in run
    install = next(s for s in action["runs"]["steps"] if s.get("id") == "install")["run"]
    assert "dvf-framework" in install and "pip install --python" in install
    gate = action["runs"]["steps"][-1]
    assert gate["if"] == "inputs.fail-step == 'true'" and "exit 1" in gate["run"]
    upload = next(s for s in action["runs"]["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["if"].startswith("always()")


def test_action_embedded_python_compiles() -> None:
    action = load_yaml(ACTION)
    bodies = [b for s in action["runs"]["steps"] if "run" in s for b in heredocs(s["run"])]
    assert len(bodies) == 1
    compile(bodies[0], "action.yml", "exec")


def test_action_version_resolution_is_documented_in_the_input() -> None:
    version = load_yaml(ACTION)["inputs"]["version"]["description"]
    assert "v1.2.3 installs 1.2.3" in version and "latest" in version


def test_selftest_workflow_runs_the_local_action_on_the_ci_example() -> None:
    workflow = load_yaml(SELFTEST)
    assert {"pull_request", "push", "workflow_dispatch"} <= set(triggers(workflow))
    job = workflow["jobs"]["modes"]
    assert job["strategy"]["matrix"]["mode"] == ["pr", "nightly"]
    assert "windows-latest" in job["strategy"]["matrix"]["os"]
    uses = [s for s in job["steps"] if s.get("uses") == "./"]
    assert len(uses) == 3
    for step in uses:
        assert step["with"]["install-from"] == "."
        assert (REPO_ROOT / step["with"]["config"]).resolve() == CI_EXAMPLE.resolve()
    strict = next(s for s in uses if s["id"] == "strict")
    assert strict["continue-on-error"] is True
    for step in job["steps"]:
        for body in heredocs(step.get("run", "")):
            compile(body, "action-selftest.yml", "exec")


# ---------------------------------------------------------------------- CI templates


def test_gitlab_template() -> None:
    gitlab = load_yaml(TEMPLATES / "gitlab-ci.yml")
    base = gitlab[".dvf"]
    assert base["artifacts"]["reports"]["junit"] == "dvf-reports/junit.xml"
    assert base["artifacts"]["when"] == "always"
    assert gitlab["variables"]["DVF_REPORT_DIR"] == "dvf-reports"
    script = "\n".join(base["script"])
    assert "--format junit --format markdown" in script and 'exit "$code"' in script
    assert gitlab["dvf:pr"]["variables"]["DVF_ONLY"] == "$DVF_PR_ONLY"
    assert gitlab["dvf:nightly"]["variables"]["DVF_ONLY"] == "$DVF_NIGHTLY_ONLY"
    assert any("merge_request_event" in r.get("if", "") for r in gitlab["dvf:pr"]["rules"])
    assert any("schedule" in r.get("if", "") for r in gitlab["dvf:nightly"]["rules"])
    assert gitlab["variables"]["DVF_PR_ONLY"] == "tag:pr"
    comment = "\n".join(gitlab["dvf:mr-comment"]["script"])
    compile(heredocs(comment)[0], "gitlab-ci.yml", "exec")


def test_azure_devops_template() -> None:
    azure = load_yaml(TEMPLATES / "azure-pipelines.yml")
    assert azure["schedules"][0]["cron"] == "0 2 * * *"
    assert azure["parameters"][0]["values"] == ["auto", "pr", "nightly"]
    steps = azure["jobs"][0]["steps"]
    tasks = {s.get("task") for s in steps}
    assert {"PublishTestResults@2", "PublishPipelineArtifact@1", "UsePythonVersion@0"} <= tasks
    junit = next(s for s in steps if s.get("task") == "PublishTestResults@2")
    assert junit["inputs"]["testResultsFormat"] == "JUnit"
    assert junit["condition"] == "succeededOrFailed()"
    scripts = "\n".join(s.get("bash", "") for s in steps)
    assert "task.uploadsummary" in scripts and "DVF_NIGHTLY_ONLY" in scripts
    assert "Schedule" in scripts and "exit 1" in scripts


def test_github_workflow_template() -> None:
    workflow = load_yaml(TEMPLATES / "github-workflow.yml")
    assert {"pull_request", "schedule", "workflow_dispatch"} <= set(triggers(workflow))
    steps = workflow["jobs"]["dvf"]["steps"]
    action = next(s for s in steps if str(s.get("uses", "")).endswith("/dvf@v1"))
    assert action["with"]["only"] == "tag:${{ env.DVF_MODE }}"
    assert "nightly" in workflow["jobs"]["dvf"]["env"]["DVF_MODE"]
    assert any("junit" in str(s.get("with", {})) for s in steps)
    assert any("summary.md" in str(s.get("env", {})) for s in steps)


def test_jenkinsfile_template() -> None:
    text = (TEMPLATES / "Jenkinsfile").read_text(encoding="utf-8")
    code = re.sub(r"//.*", "", text)
    assert code.count("{") == code.count("}") and code.count("(") == code.count(")")
    for needle in (
        "junit testResults",
        "archiveArtifacts",
        "summary.md",
        "TimerTrigger",
        "DVF_PR_ONLY = 'tag:pr'",
        "DVF_NIGHTLY_ONLY = 'tag:nightly'",
        "--fail-on",
        "--min-score",
        "error('dvf quality gates failed')",
    ):
        assert needle in text, needle


# ---------------------------------------------------------------------- orchestrator examples


def test_every_example_json_parses() -> None:
    files = sorted(ORCHESTRATORS.rglob("*.json"))
    assert len(files) >= 8
    for file in files:
        json.loads(file.read_text(encoding="utf-8"))


def test_step_functions_state_machines_are_consistent() -> None:
    for file in ORCHESTRATORS.joinpath("aws").glob("*.asl.json"):
        machine = json.loads(file.read_text(encoding="utf-8"))
        states = machine["States"]
        assert machine["StartAt"] in states
        for name, state in states.items():
            targets = [state.get("Next"), state.get("Default")]
            targets += [c["Next"] for c in state.get("Choices", []) + state.get("Catch", [])]
            for target in filter(None, targets):
                assert target in states, f"{file.name}: {name} -> {target}"


def test_data_factory_pipelines_reference_their_activities() -> None:
    for file in ORCHESTRATORS.joinpath("azure").glob("adf_*.json"):
        pipeline = json.loads(file.read_text(encoding="utf-8"))
        activities = pipeline["properties"]["activities"]
        names = {a["name"] for a in activities}
        for activity in activities:
            for dependency in activity.get("dependsOn", []):
                assert dependency["activity"] in names, file.name
        assert any(a["type"] in ("WebActivity", "DatabricksNotebook") for a in activities), (
            file.name
        )


def test_databricks_job_tasks_are_consistent() -> None:
    job = json.loads((ORCHESTRATORS / "databricks" / "job.json").read_text(encoding="utf-8"))
    keys = {t["task_key"] for t in job["tasks"]}
    for task in job["tasks"]:
        for dependency in task.get("depends_on", []):
            assert dependency["task_key"] in keys
    wheel = next(t for t in job["tasks"] if "python_wheel_task" in t)
    assert wheel["python_wheel_task"]["entry_point"] == "dvf"
    assert wheel["python_wheel_task"]["parameters"][0] == "run"
    notebook = next(t for t in job["tasks"] if t["task_key"] == "dvf_full")
    assert notebook["notebook_task"]["base_parameters"]["raise_on_failure"] == "false"


def test_every_example_python_file_parses() -> None:
    files = sorted(ORCHESTRATORS.rglob("*.py"))
    assert len(files) >= 8
    for file in files:
        ast.parse(file.read_text(encoding="utf-8"), filename=str(file))


# ---------------------------------------------------------------------- PR and nightly modes


def test_ci_example_is_valid() -> None:
    report = api.validate_config(CI_EXAMPLE)
    assert report.ok, report.errors
    assert report.summary()["tables"] == 4


@pytest.mark.parametrize(
    ("mode", "present", "absent"),
    [
        ("pr", {"compare.row_count", "compare.aggregate", "not_null"}, {"compare.row_hash"}),
        ("nightly", {"compare.row_hash", "compare.hash_bucket", "referential_integrity"}, set()),
    ],
)
def test_ci_example_modes_select_checks_by_tag(
    mode: str, present: set[str], absent: set[str], tmp_path: Path
) -> None:
    state = CI_EXAMPLE.parent / ".dvf"
    existed = state.exists()
    output, summary = run_validation(
        CI_EXAMPLE,
        only=f"tag:{mode}",
        overrides=["defaults.execution.checkpoints=false"],
        output_dir=tmp_path / mode,
        history=False,
    )
    types = {c.type for c in output.result.iter_checks()}
    assert present <= types and not absent & types
    assert summary.exit_code == 1  # seeded defects
    lenient = run_validation(
        CI_EXAMPLE,
        only=f"tag:{mode}",
        overrides=["defaults.execution.checkpoints=false"],
        write_reports=False,
        history=False,
        fail_on="critical",
    )[1]
    assert lenient.exit_code == 0
    assert state.exists() == existed  # nothing written next to the example
