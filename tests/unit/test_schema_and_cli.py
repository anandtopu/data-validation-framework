from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from dvf import __version__
from dvf.api import (
    ValidationReport,
    config_json_schema,
    load_config_data,
    results_json_schema,
    validate_config,
)
from dvf.cli.app import ExitCode, app
from dvf.config.formats import read_config_file
from dvf.plugins.registry import Registry
from tests.conftest import EXAMPLES, MINIMAL, REPO_ROOT

runner = CliRunner()

EXAMPLE_FILES = sorted(p for p in EXAMPLES.rglob("dvf.yaml"))


# ---------------------------------------------------------------------- JSON Schema


def test_committed_schemas_are_up_to_date(registry: Registry) -> None:
    """Regenerate with `uv run nox -s schemas` if this fails."""
    committed_config = json.loads(
        (REPO_ROOT / "schemas/config.schema.json").read_text(encoding="utf-8")
    )
    committed_results = json.loads(
        (REPO_ROOT / "schemas/results.schema.json").read_text(encoding="utf-8")
    )
    assert committed_config == config_json_schema(registry)
    assert committed_results == results_json_schema()


@pytest.mark.parametrize("example", EXAMPLE_FILES, ids=lambda p: p.parent.name)
def test_examples_match_json_schema(example: Path, registry: Registry) -> None:
    data, _ = read_config_file(example)
    # The schema describes the file as written, so dates and plugin types must still pass.
    schema = config_json_schema(registry)
    plugin_types = {"acme.vat_number_valid"}
    for suite in data.get("suites", []):
        for table in suite.get("tables", []):
            table["checks"] = [
                c for c in table.get("checks", []) if c.get("type") not in plugin_types
            ]
    for waiver in data.get("waivers", []):
        waiver["expires"] = str(waiver.get("expires"))
    jsonschema.Draft202012Validator(schema).validate(data)


def test_schema_accepts_shorthands_and_rejects_typos(registry: Registry) -> None:
    validator = jsonschema.Draft202012Validator(config_json_schema(registry))
    good = yaml.safe_load(MINIMAL)
    table = good["suites"][0]["tables"][0]
    table["keys"] = "auto"
    table["columns"] = {
        "include": "*",
        "mappings": [{"source": "a", "target": "b", "normalize": ["rtrim", {"round": 2}]}],
    }
    table["checks"] = [{"type": "not_null", "columns": ["id"]}]
    validator.validate(good)
    table["checks"] = [{"type": "not_null", "colums": ["id"]}]
    assert list(validator.iter_errors(good))
    table["checks"] = [{"type": "nope"}]
    assert list(validator.iter_errors(good))


def test_schema_includes_plugin_types(registry: Registry) -> None:
    from dvf.plugins import Check, CheckConfig, PluginKind

    class Custom(Check):
        class Config(CheckConfig):
            threshold: int

    registry.register(PluginKind.CHECK, "acme.custom", Custom)
    schema = json.dumps(config_json_schema(registry))
    assert '"const": "acme.custom"' in schema
    assert "AcmeCustomCheckConfig" in schema


# ---------------------------------------------------------------------- API


def test_validate_config_api(write: Any, registry: Registry) -> None:
    report = validate_config(write("ok.yaml", MINIMAL), registry=registry)
    assert report.ok and report.summary() == {
        "suites": 1,
        "tables": 1,
        "checks": 0,
        "connections": 2,
    }
    bad = validate_config(write("bad.yaml", "version: 1\n"), registry=registry)
    assert not bad.ok and bad.errors and bad.summary() == {}
    assert ValidationReport(path=Path("x")).warnings == []


def test_load_config_data_api(registry: Registry) -> None:
    loaded = load_config_data({"version": 1, "project": {"name": "api"}}, registry=registry)
    assert loaded.config.project.name == "api"


# ---------------------------------------------------------------------- CLI


@pytest.fixture(autouse=True)
def _cli_registry(default_registry: Registry) -> Registry:
    return default_registry


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.output.strip() == f"dvf {__version__}"
    result = runner.invoke(app, ["version", "--format", "json"])
    info = json.loads(result.output)
    assert info["dvf"] == __version__ and info["config_version"] == 1
    assert "plugin_api_version" in runner.invoke(app, ["version"]).output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "validate-config" in result.output


@pytest.mark.parametrize("example", EXAMPLE_FILES, ids=lambda p: p.parent.name)
def test_validate_examples(example: Path) -> None:
    result = runner.invoke(app, ["validate-config", str(example)])
    assert result.exit_code == ExitCode.OK, result.output
    assert "is valid" in result.output


def test_validate_with_profile_and_json_output() -> None:
    example = EXAMPLES / "sap_to_snowflake" / "dvf.yaml"
    result = runner.invoke(
        app, ["validate-config", str(example), "--profile", "uat", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert payload["summary"]["tables"] == 3
    assert any(f.endswith("dvf.uat.yaml") for f in payload["files"])


def test_validate_invalid_config(write: Any) -> None:
    path = write("bad.yaml", MINIMAL.replace("type: duckdb}", "type: duckdbb}", 1))
    result = runner.invoke(app, ["validate-config", str(path)])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "DVF-C110" in result.output and "Did you mean 'duckdb'" in result.output
    assert "INVALID" in result.output
    result = runner.invoke(app, ["validate-config", str(path), "--format", "json"])
    payload = json.loads(result.output)
    assert payload["valid"] is False and payload["diagnostics"][0]["line"] == 4


def test_validate_strict_and_vars(write: Any) -> None:
    path = write(
        "env.yaml",
        MINIMAL.replace("name: demo", "name: '${var:who}-${env:DVF_TEST_UNSET_VAR}'"),
    )
    lenient = runner.invoke(app, ["validate-config", str(path), "--var", "who=me"])
    assert lenient.exit_code == 0 and "1 warning" in lenient.output
    strict = runner.invoke(app, ["validate-config", str(path), "--var", "who=me", "--strict"])
    assert strict.exit_code == ExitCode.CONFIG_ERROR
    bad_var = runner.invoke(app, ["validate-config", str(path), "--var", "novalue"])
    assert bad_var.exit_code == ExitCode.CONFIG_ERROR and "NAME=VALUE" in bad_var.output


def test_validate_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate-config", str(tmp_path / "none.yaml")])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "not found" in result.output


def test_config_show_redacts_secrets(write: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DVF_TEST_PG_PASSWORD", "correct-horse-battery")
    path = write(
        "dvf.yaml",
        MINIMAL.replace(
            "src: {type: duckdb}",
            "src: {type: postgres, host: h, password: '${secret:env:DVF_TEST_PG_PASSWORD}', "
            "url: 'postgresql://u:correct-horse-battery@h/db'}",
        ),
    )
    shown = runner.invoke(
        app, ["config", "show", str(path), "--resolve-secrets", "--set", "defaults.sample_size=7"]
    )
    assert shown.exit_code == 0, shown.output
    assert "correct-horse-battery" not in shown.output
    data = yaml.safe_load(shown.output)
    assert data["defaults"]["sample_size"] == 7
    assert data["connections"]["src"]["password"] == "***"
    as_json = runner.invoke(app, ["config", "show", str(path), "--format", "json"])
    assert json.loads(as_json.output)["project"]["name"] == "demo"
    assert "profiles" not in json.loads(as_json.output)


def test_config_show_errors(write: Any) -> None:
    result = runner.invoke(app, ["config", "show", str(write("bad.yaml", "version: 1\n"))])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "Missing required field 'project'" in result.output


def test_schema_commands(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema", "results"])
    assert json.loads(result.output)["title"] == "dvf results"
    target = tmp_path / "out" / "config.schema.json"
    result = runner.invoke(app, ["schema", "config", "--output", str(target)])
    assert result.exit_code == 0
    assert json.loads(target.read_text(encoding="utf-8"))["title"] == "dvf configuration"


def test_plugins_list(write: Any, tmp_path: Path) -> None:
    result = runner.invoke(app, ["plugins", "list"])
    assert result.exit_code == 0
    assert "not_null" in result.output and "declared" in result.output
    write(
        "plugins/acme.py",
        "from dvf.plugins import Check, hookimpl, register_check\n"
        "@register_check('acme.cli_check')\nclass C(Check):\n    '''From the CLI.'''\n"
        "@hookimpl\ndef on_config_loaded(config):\n    pass\n",
    )
    result = runner.invoke(
        app,
        [
            "--plugins",
            str(tmp_path / "plugins"),
            "plugins",
            "list",
            "--kind",
            "check",
            "--format",
            "json",
        ],
    )
    payload = json.loads(result.output)
    names = {c["name"]: c for c in payload["components"]}
    assert names["acme.cli_check"]["status"] == "ready"
    assert {c["kind"] for c in payload["components"]} == {"check"}
    assert payload["hooks"]
    text = runner.invoke(
        app, ["--plugins", str(tmp_path / "plugins"), "plugins", "list", "-k", "check"]
    )
    assert "Hook plugins" in text.output


def test_cli_plugin_errors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--plugins", str(tmp_path / "missing/"), "plugins", "list"])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "Plugin path not found" in result.output


def test_log_options() -> None:
    result = runner.invoke(app, ["--log-level", "debug", "--log-format", "json", "version"])
    assert result.exit_code == 0
