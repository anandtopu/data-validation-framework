"""dvf plan, schema-diff, waivers list, config migrate, and plugin new (dvf.cli.tools)."""

from __future__ import annotations

import json
import re
import textwrap
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from dvf import api
from dvf.cli.app import app
from dvf.cli.tools import diff_level, waiver_rows
from dvf.config.migrate import (
    MigrationNotes,
    Migrator,
    MigratorRegistry,
    backup_path,
    migrate_data,
    migrate_file,
    render,
    use_migrators,
)
from dvf.config.models import CONFIG_VERSION, WaiverConfig
from dvf.core.errors import ConfigError
from dvf.plugins.scaffold import (
    KINDS,
    ScaffoldError,
    create_package,
    dvf_requirement,
    package_files,
    plugin_api_range,
    plugin_spec,
)
from dvf.plugins.scaffold import render as render_template

runner = CliRunner()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


# ---------------------------------------------------------------------- plan


def test_plan_is_an_alias_of_explain(tmp_path: Path) -> None:
    config = _diff_project(tmp_path)
    plan = runner.invoke(app, ["plan", str(config), "--table", "customers", "--format", "json"])
    explain = runner.invoke(
        app, ["explain", str(config), "--table", "customers", "--format", "json"]
    )
    assert plan.exit_code == 0, plan.output
    assert json.loads(plan.output) == json.loads(explain.output)
    assert "Alias of 'dvf explain'" in runner.invoke(app, ["plan", "--help"]).output


# ---------------------------------------------------------------------- schema-diff


def _diff_project(tmp_path: Path) -> Path:
    for name, ddl in (
        (
            "source.duckdb",
            [
                "CREATE TABLE customers (id INTEGER NOT NULL, name VARCHAR(40), balance DOUBLE,"
                " legacy_flag VARCHAR)",
                "CREATE TABLE orders (id INTEGER, amount DECIMAL(10,2))",
            ],
        ),
        (
            "target.duckdb",
            [
                "CREATE TABLE customers (id BIGINT, name VARCHAR(40), balance DECIMAL(12,2),"
                " created_at TIMESTAMP)",
                "CREATE TABLE orders (id INTEGER, amount DECIMAL(10,2))",
            ],
        ),
    ):
        with duckdb.connect(str(tmp_path / name)) as con:
            for statement in ddl:
                con.execute(statement)
    return _write(
        tmp_path / "dvf.yaml",
        """
        version: 1
        project: {name: diff}
        defaults: {execution: {checkpoints: false}}
        connections:
          src: {type: duckdb, database: source.duckdb}
          tgt: {type: duckdb, database: target.duckdb}
        suites:
          - name: legacy
            source: src
            target: tgt
            tables:
              - {name: customers, source: {table: customers}, target: {table: customers}, keys: [id]}
              - {name: orders, source: {table: orders}, target: {table: orders}, keys: [id]}
              - {name: gone, source: {table: orders}, target: {table: missing_table}, keys: [id]}
          - name: quality
            source: src
            tables:
              - {name: orders, source: {table: orders}}
        """,
    )


def test_schema_diff_of_a_configuration(tmp_path: Path) -> None:
    config = _diff_project(tmp_path)
    result = runner.invoke(app, ["schema-diff", str(config), "--suite", "legacy"])
    assert result.exit_code == 1, result.output
    assert "missing in" in result.output and "extra in" in result.output
    assert "legacy.gone" in result.output and "problem" in result.output
    assert "quality.orders" not in result.output
    assert "3 table(s), 2 with differences" in result.output

    data = runner.invoke(
        app, ["schema-diff", str(config), "--format", "json", "--fail-on", "never"]
    )
    assert data.exit_code == 0, data.output
    payload = json.loads(data.output)
    tables = {f"{t['suite']}.{t['table']}": t for t in payload["tables"]}
    assert tables["legacy.orders"]["level"] == "none"
    assert tables["legacy.gone"]["level"] == "incompatible"
    assert tables["quality.orders"]["problems"] == [
        "single-dataset suite: there is no target to compare"
    ]
    rows = {
        (r["source_column"], r["target_column"]): r for r in tables["legacy.customers"]["columns"]
    }
    assert rows[("id", "id")]["nullable_change"] == "not null -> nullable"
    assert rows[("legacy_flag", None)]["level"] == "incompatible"
    assert rows[(None, "created_at")]["level"] == "any"
    assert rows[("balance", "balance")]["level"] == "lossy"
    assert payload["worst"] == "incompatible" and payload["failed"] is False

    only_orders = runner.invoke(app, ["schema-diff", str(config), "--table", "orders", "--all"])
    assert only_orders.exit_code == 0, only_orders.output
    assert "amount" in only_orders.output


def test_schema_diff_fail_on_levels(tmp_path: Path) -> None:
    config = _diff_project(tmp_path)
    args = ["schema-diff", str(config), "--only", "table:customers", "--format", "json"]
    assert runner.invoke(app, [*args, "--fail-on", "any"]).exit_code == 1
    assert runner.invoke(app, [*args, "--fail-on", "never"]).exit_code == 0


def test_schema_diff_of_two_datasets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "old.csv", "id,name\n1,Ada\n")
    _write(tmp_path / "new.csv", "id,name\n1,Ada\n")
    _write(tmp_path / "wider.csv", "id,name,email\n1,Ada,a@example.com\n")
    same = runner.invoke(app, ["schema-diff", "old.csv", "new.csv", "--key", "id"])
    assert same.exit_code == 0, same.output
    assert "0 difference(s), 2 column(s) identical" in same.output
    wider = runner.invoke(app, ["schema-diff", "old.csv", "wider.csv"])
    assert wider.exit_code == 0 and "extra in" in wider.output
    assert (
        runner.invoke(app, ["schema-diff", "old.csv", "wider.csv", "--fail-on", "any"]).exit_code
        == 1
    )


def test_schema_diff_errors(tmp_path: Path) -> None:
    missing = runner.invoke(app, ["schema-diff", str(tmp_path / "nope.yaml")])
    assert missing.exit_code == 3
    bad = runner.invoke(app, ["schema-diff", "a.csv", "ftp://host/x::t"])
    assert bad.exit_code == 3 and "Unsupported location scheme" in bad.output
    config = _write(
        tmp_path / "broken.yaml",
        """
        version: 1
        project: {name: broken}
        connections:
          src: {type: postgres, host: 127.0.0.1, port: 1, database: x, user: u}
          tgt: {type: duckdb}
        suites:
          - {name: s, source: src, target: tgt, tables: [{name: t, source: {table: a}, target: {table: b}}]}
        """,
    )
    broken = runner.invoke(app, ["schema-diff", str(config)])
    assert broken.exit_code in (1, 2), broken.output


def test_diff_level() -> None:
    assert diff_level({"status": "derived"}) == 0
    assert diff_level({"status": "matched", "compatibility": "exact"}) == 0
    assert diff_level({"status": "matched", "compatibility": "unknown"}) == 2
    assert diff_level({"status": "matched", "compatibility": "compatible_widening"}) == 1
    assert diff_level({"status": "matched", "compatibility": "exact", "nullable_change": "x"}) == 1


# ---------------------------------------------------------------------- waivers list


def test_waiver_rows_statuses() -> None:
    day = date(2026, 11, 8)
    waivers = [
        WaiverConfig(check="a", reason="r", expires=date(2026, 11, 7)),
        WaiverConfig(check="b", reason="r", expires=date(2026, 11, 8)),
        WaiverConfig(check="c", reason="r", expires=date(2026, 11, 22)),
        WaiverConfig(check="d", reason="r", expires=date(2026, 11, 23)),
        WaiverConfig(check="e", reason="r"),
    ]
    statuses = [r["status"] for r in waiver_rows(waivers, day, 14)]
    assert statuses == ["expired", "expiring soon", "expiring soon", "active", "active"]


def test_waivers_list(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "dvf.yaml",
        f"""
        version: 1
        project: {{name: waivers}}
        connections:
          db: {{type: duckdb, database: "${{env:DVF_TEST_UNSET_DB_PATH}}"}}
        waivers:
          - {{check: "s.t.*", reason: Accepted, ticket: CR-1, approved_by: qa-lead, expires: "{(date.today() + timedelta(days=90)).isoformat()}"}}
          - {{check: s.t.compare.column_mismatch, column: name, reason: Truncation, expires: "{(date.today() + timedelta(days=3)).isoformat()}"}}
          - {{check: "s.u.*", reason: Old, ticket: CR-0, expires: "{(date.today() - timedelta(days=1)).isoformat()}"}}
          - {{check: "s.v.*", reason: Permanent exception}}
        """,
    )
    text = runner.invoke(app, ["waivers", "list", str(config)])
    assert text.exit_code == 0, text.output
    assert "CR-1" in text.output and "qa-lead" in text.output and "never" in text.output
    assert "4 waiver(s): 2 active, 1 expiring within 14 days, 1 expired" in text.output
    assert (
        runner.invoke(app, ["waivers", "list", str(config), "--fail-on", "expired"]).exit_code == 1
    )
    data = runner.invoke(
        app, ["waivers", "list", str(config), "--format", "json", "--soon-days", "0"]
    )
    payload = json.loads(data.output)
    assert payload["counts"] == {"active": 3, "expiring soon": 0, "expired": 1}
    later = (date.today() + timedelta(days=400)).isoformat()
    future = runner.invoke(
        app,
        [
            "waivers",
            "list",
            str(config),
            "--as-of",
            later,
            "--fail-on",
            "expiring",
            "--format",
            "json",
        ],
    )
    assert future.exit_code == 1
    assert json.loads(future.output)["counts"]["expired"] == 3
    bad_date = runner.invoke(app, ["waivers", "list", str(config), "--as-of", "soon"])
    assert bad_date.exit_code == 3


def test_waivers_list_without_waivers_and_errors(tmp_path: Path) -> None:
    config = _write(tmp_path / "dvf.yaml", "version: 1\nproject: {name: none}\n")
    empty = runner.invoke(app, ["waivers", "list", str(config)])
    assert empty.exit_code == 0 and "no waivers" in empty.output
    invalid = _write(
        tmp_path / "bad.yaml", "version: 1\nproject: {name: x}\nwaivers: [{check: a}]\n"
    )
    assert runner.invoke(app, ["waivers", "list", str(invalid)]).exit_code == 3


# ---------------------------------------------------------------------- config migrate


def _v0_registry() -> MigratorRegistry:
    """A synthetic migrator for tests only: version 0 called the sample size 'samples'."""
    registry = MigratorRegistry()

    @registry.register(0, "Rename defaults.samples to defaults.sample_size")
    def _v0_to_v1(data: dict[str, Any], notes: MigrationNotes) -> dict[str, Any]:
        defaults = data.get("defaults") or {}
        if "samples" in defaults:
            defaults["sample_size"] = defaults.pop("samples")
            notes.add("defaults.samples is now defaults.sample_size")
        data["defaults"] = defaults
        return data

    return registry


V0_CONFIG = """
# A version 0 configuration
version: 0
project: {name: old}
defaults: {samples: 7}
connections:
  db: {type: duckdb}
"""


def test_migrate_file_upgrades_with_a_backup(tmp_path: Path) -> None:
    path = _write(tmp_path / "dvf.yaml", V0_CONFIG)
    original = path.read_text(encoding="utf-8")
    with use_migrators(_v0_registry()):
        checked = runner.invoke(app, ["config", "migrate", str(path), "--check"])
        assert checked.exit_code == 1, checked.output
        assert "is version 0" in checked.output
        assert path.read_text(encoding="utf-8") == original
        upgraded = runner.invoke(app, ["config", "migrate", str(path)])
    assert upgraded.exit_code == 0, upgraded.output
    assert "Upgraded" in upgraded.output and "defaults.sample_size" in upgraded.output
    backup = tmp_path / "dvf.yaml.v0.bak"
    assert backup.read_text(encoding="utf-8") == original
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert next(iter(data)) == "version" and data["version"] == CONFIG_VERSION
    assert data["defaults"] == {"sample_size": 7}
    report = api.validate_config(path)
    assert report.ok, [d.message for d in report.diagnostics]
    current = runner.invoke(app, ["config", "migrate", str(path), "--check"])
    assert current.exit_code == 0 and "nothing to migrate" in current.output


def test_migrate_to_another_file_and_json(tmp_path: Path) -> None:
    path = _write(tmp_path / "dvf.yaml", V0_CONFIG)
    with use_migrators(_v0_registry()):
        result = runner.invoke(
            app,
            ["config", "migrate", str(path), "-o", str(tmp_path / "new.json"), "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["from_version"] == 0 and payload["written"].endswith("new.json")
        assert payload["backup"] is None
        assert json.loads((tmp_path / "new.json").read_text(encoding="utf-8"))["version"] == 1
        no_backup = runner.invoke(app, ["config", "migrate", str(path), "--no-backup"])
        assert no_backup.exit_code == 0
    assert not list(tmp_path.glob("*.bak"))


def test_migrate_fragment_and_errors(tmp_path: Path) -> None:
    fragment = _write(tmp_path / "base.yaml", "defaults: {samples: 3}\n")
    with use_migrators(_v0_registry()):
        missing_version = runner.invoke(app, ["config", "migrate", str(fragment)])
        assert missing_version.exit_code == 3 and "--from-version" in missing_version.output
        ok = runner.invoke(app, ["config", "migrate", str(fragment), "--from-version", "0"])
        assert ok.exit_code == 0, ok.output
        assert yaml.safe_load(fragment.read_text(encoding="utf-8")) == {
            "defaults": {"sample_size": 3}
        }
    newer = _write(tmp_path / "newer.yaml", "version: 99\nproject: {name: x}\n")
    result = runner.invoke(app, ["config", "migrate", str(newer)])
    assert result.exit_code == 3 and "newer than version" in result.output
    toml = _write(tmp_path / "old.toml", 'version = 0\n[project]\nname = "x"\n')
    with use_migrators(_v0_registry()):
        result = runner.invoke(app, ["config", "migrate", str(toml)])
        assert result.exit_code == 3 and "--output" in result.output
        result = runner.invoke(
            app, ["config", "migrate", str(toml), "-o", str(tmp_path / "x.yaml")]
        )
        assert result.exit_code == 0, result.output
    unknown = runner.invoke(
        app, ["config", "migrate", str(_write(tmp_path / "v0.yaml", V0_CONFIG))]
    )
    assert unknown.exit_code == 3 and "No migration from configuration version 0" in unknown.output


def test_migrate_data_rules() -> None:
    registry = _v0_registry()
    result = migrate_data({"version": 1, "project": {"name": "x"}}, registry=registry)
    assert not result.changed and result.steps == []
    with pytest.raises(ConfigError, match="invalid 'version"):
        migrate_data({"version": "one"}, registry=registry)
    with pytest.raises(ConfigError, match="--from-version 0 was given"):
        migrate_data({"version": 1}, from_version=0, registry=registry)
    bad = MigratorRegistry()
    bad.add(Migrator(0, "broken", lambda data, notes: None))  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="not a mapping"):
        migrate_data({"version": 0}, registry=bad)
    with pytest.raises(ValueError, match="already exists"):
        registry.add(Migrator(0, "again", lambda data, notes: data))
    two_steps = MigratorRegistry()
    two_steps.add(Migrator(0, "a", lambda data, notes: {**data, "a": 1}))
    two_steps.add(Migrator(1, "b", lambda data, notes: {**data, "b": 2}))
    upgraded = migrate_data({"version": 0}, to_version=2, registry=two_steps)
    assert upgraded.data == {"version": 2, "a": 1, "b": 2}
    assert len(upgraded.steps) == 2 and len(two_steps) == 2
    assert two_steps.supported(2) == "0, 1"
    assert MigratorRegistry().supported(1).startswith("none")


def test_migrate_file_helpers(tmp_path: Path) -> None:
    path = _write(tmp_path / "dvf.yaml", "version: 1\nproject: {name: x}\n")
    assert backup_path(path, 1).name == "dvf.yaml.v1.bak"
    (tmp_path / "dvf.yaml.v1.bak").write_text("", encoding="utf-8")
    assert backup_path(path, 1).name == "dvf.yaml.v1.1.bak"
    assert render({"a": 1}, ".json") == '{\n  "a": 1\n}\n'
    with pytest.raises(ConfigError):
        render({"a": 1}, ".ini")
    result = migrate_file(path, output=tmp_path / "copy.yml")
    assert result.output == tmp_path / "copy.yml" and not result.changed


# ---------------------------------------------------------------------- plugin new


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_plugin_new_generates_a_package(tmp_path: Path, kind: str) -> None:
    name = "acme_thing" if kind == "secret_provider" else "acme.thing"
    folder = tmp_path / "pkg"
    result = runner.invoke(app, ["plugin", "new", kind, name, "--output", str(folder)])
    assert result.exit_code == 0, result.output
    spec = KINDS[kind]
    pyproject = (folder / "pyproject.toml").read_text(encoding="utf-8")
    assert f'[project.entry-points."{spec.kind.entry_point_group}"]' in pyproject
    assert f'"{name}" = "dvf_acme_thing.{spec.module}:' in pyproject
    assert "dvf-framework>=" in pyproject
    module = folder / "src" / "dvf_acme_thing" / f"{spec.module}.py"
    test = folder / "tests" / f"test_{spec.module}.py"
    for file in (module, test, folder / "src" / "dvf_acme_thing" / "__init__.py"):
        source = file.read_text(encoding="utf-8")
        assert not re.search(r"[{][{] *[a-z_]+ *[}][}]", source)
        compile(source, str(file), "exec")
    assert f'dvf_plugin_api: ClassVar[str | None] = "{plugin_api_range()}"' in module.read_text(
        encoding="utf-8"
    )
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert name in readme and "pip install -e" in readme


def test_plugin_new_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["plugin", "new", "check", "Bad-Name"]).exit_code == 3
    dotted = runner.invoke(app, ["plugin", "new", "secret_provider", "acme.vault"])
    assert dotted.exit_code == 3 and "without dots" in dotted.output
    assert runner.invoke(app, ["plugin", "new", "engine", "acme.x"]).exit_code == 2
    first = runner.invoke(
        app, ["plugin", "new", "check", "acme.vat", "--package", "dvf-acme-checks"]
    )
    assert first.exit_code == 0, first.output
    assert (tmp_path / "dvf-acme-checks" / "src" / "dvf_acme_checks" / "checks.py").exists()
    again = runner.invoke(
        app, ["plugin", "new", "check", "acme.vat", "--package", "dvf-acme-checks"]
    )
    assert again.exit_code == 3 and "already exists" in again.output
    forced = runner.invoke(
        app, ["plugin", "new", "check", "acme.vat", "--package", "dvf-acme-checks", "--force"]
    )
    assert forced.exit_code == 0
    alias = runner.invoke(app, ["plugins", "new", "sink", "acme.store"])
    assert alias.exit_code == 0, alias.output
    assert (tmp_path / "dvf-acme-store" / "src" / "dvf_acme_store" / "sink.py").exists()
    assert "new" not in runner.invoke(app, ["plugins", "--help"]).output.split("Commands")[-1]


def test_scaffold_helpers() -> None:
    assert plugin_api_range("0.1.0") == ">=0.1,<0.2"
    assert plugin_api_range("1.2.0") == ">=1.2,<2"
    assert dvf_requirement("1.0.0") == ">=1.0,<2"
    assert dvf_requirement("0.4.0.dev3+g1") == ">=0.4.dev0,<0.5"
    spec = plugin_spec("secret-provider", "acme_vault")
    assert spec.class_name == "AcmeVaultSecretProvider" and spec.env_var == "ACME_VAULT_DIR"
    assert plugin_spec("connector", "acme.rest_connector").class_name == "RestConnector"
    assert plugin_spec("check", "acme.x2", distribution="Acme.Checks").module == "acme_checks"
    with pytest.raises(ScaffoldError, match="Unknown plugin kind"):
        plugin_spec("hook", "acme.x")
    with pytest.raises(ScaffoldError, match="Invalid package name"):
        plugin_spec("check", "acme.x", distribution="-bad-")
    with pytest.raises(KeyError, match="unknown template placeholder"):
        render_template("{{ missing }}", {})
    assert set(package_files(spec)) >= {"pyproject.toml", "README.md", ".gitignore"}


def test_create_package_default_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec, folder, files = create_package("reporter", "acme.csv")
    assert folder == Path("dvf-acme-csv") and spec.class_name == "CsvReporter"
    assert all(f.exists() for f in files)
