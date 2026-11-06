from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from dvf.config.checks import NotNullCheck, RegexCheck
from dvf.config.connections import DatabaseConnection, DuckDBConnection
from dvf.config.diagnostics import Code
from dvf.config.loader import LoadOptions, load_config, load_config_data
from dvf.core.enums import SchemaCheck, Severity, Side
from dvf.core.errors import ConfigError, PluginError
from dvf.plugins.registry import Registry
from tests.conftest import MINIMAL


def load(path: Path, registry: Registry, **options: Any) -> Any:
    options.setdefault("environ", {})
    return load_config(path, LoadOptions(**options), registry=registry)


def errors_of(path: Path, registry: Registry, **options: Any) -> list[Any]:
    with pytest.raises(ConfigError) as info:
        load(path, registry, **options)
    return [d for d in info.value.diagnostics if d.is_error]


def test_minimal_config(write: Any, registry: Registry) -> None:
    loaded = load(write("dvf.yaml", MINIMAL), registry)
    config = loaded.config
    assert config.project.name == "demo"
    assert isinstance(config.connections["src"], DuckDBConnection)
    table = config.suites[0].tables[0]
    assert table.keys[0].source == "id" and table.keys[0].target_name == "id"
    assert table.severity is Severity.ERROR
    assert SchemaCheck.TYPE_COMPATIBILITY in table.schema_checks
    assert [f.name for f in loaded.files] == ["dvf.yaml"]
    assert loaded.diagnostics == []


def test_inheritance_project_suite_table(write: Any, registry: Registry) -> None:
    path = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: demo}
        defaults:
          severity: warning
          normalization: {trim: rtrim, timezone: Europe/Berlin}
          comparison: {strategies: [row_count], buckets: 64}
        connections: {src: {type: duckdb}, tgt: {type: duckdb}}
        suites:
          - name: s1
            source: src
            target: tgt
            defaults:
              normalization: {case_sensitive: false}
              comparison: {buckets: 128}
            tables:
              - name: t1
                source: {table: a}
                target: {table: b}
                normalization: {trim: both}
                checks:
                  - {type: not_null, columns: [id]}
                  - {type: regex, column: c, pattern: "^x", severity: critical, side: source}
              - name: t2
                source: {table: a}
                target: {table: b}
                severity: error
                comparison: {strategies: [row_count, aggregate]}
                checks: [{type: not_null, columns: [id]}]
        """,
    )
    config = load(path, registry).config
    suite = config.suites[0]
    assert suite.defaults.normalization.trim == "rtrim"
    assert suite.defaults.normalization.case_sensitive is False
    assert suite.defaults.comparison.buckets == 128
    t1, t2 = suite.tables
    assert t1.normalization.trim == "both"
    assert t1.normalization.case_sensitive is False
    assert t1.normalization.timezone == "Europe/Berlin"
    assert t1.severity is Severity.WARNING
    assert t1.comparison.strategies == ["row_count"] and t1.comparison.buckets == 128
    not_null, regex = t1.checks
    assert isinstance(not_null, NotNullCheck) and isinstance(regex, RegexCheck)
    assert not_null.severity is Severity.WARNING and not_null.side is Side.TARGET
    assert regex.severity is Severity.CRITICAL and regex.side is Side.SOURCE
    assert t2.severity is Severity.ERROR and t2.checks[0].severity is Severity.ERROR
    assert t2.comparison.strategies == ["row_count", "aggregate"]
    assert t2.comparison.buckets == 128


def test_single_dataset_suite_defaults_side_to_source(write: Any, registry: Registry) -> None:
    path = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: dq}
        connections: {db: {type: sqlite, database: x.db}}
        suites:
          - name: dq
            source: db
            tables:
              - name: t
                source: {table: t}
                keys: auto
                checks: [{type: unique, columns: [id]}]
        """,
    )
    table = load(path, registry).config.suites[0].tables[0]
    assert table.checks[0].side is Side.SOURCE
    assert table.discover_keys is True and table.keys == []


def test_extends_profiles_local_env_and_set_layering(write: Any, registry: Registry) -> None:
    write(
        "base.yaml",
        """
        project: {name: base, description: from-base}
        defaults: {sample_size: 1, severity: info}
        """,
    )
    main = write(
        "dvf.yaml",
        MINIMAL
        + """
        extends: base.yaml
        defaults: {sample_size: 2}
        profiles:
          uat:
            defaults: {sample_size: 3}
            project: {description: from-profile}
        """.replace("        ", ""),
    )
    write("dvf.uat.yaml", "defaults: {sample_size: 4}\n")
    write("dvf.local.yaml", "defaults: {sample_size: 5}\n")

    def sample_size(**options: Any) -> int:
        return int(load(main, registry, **options).config.defaults.sample_size)

    assert sample_size(local_override=False) == 2
    assert sample_size() == 5
    config = load(main, registry, profile="uat", local_override=False).config
    assert config.defaults.sample_size == 4
    assert config.project.name == "demo"
    assert config.project.description == "from-profile"
    assert config.defaults.severity is Severity.INFO
    assert sample_size(environ={"DVF__DEFAULTS__SAMPLE_SIZE": "6"}) == 6
    assert sample_size(environ={"DVF__DEFAULTS__SAMPLE_SIZE": "6"}, env_overrides=False) == 5
    assert (
        sample_size(
            environ={"DVF__DEFAULTS__SAMPLE_SIZE": "6"}, overrides=["defaults.sample_size=7"]
        )
        == 7
    )
    loaded = load(main, registry, profile="uat")
    assert [p.name for p in loaded.files] == [
        "dvf.yaml",
        "base.yaml",
        "dvf.uat.yaml",
        "dvf.local.yaml",
    ]


def test_override_errors(write: Any, registry: Registry) -> None:
    path = write("dvf.yaml", MINIMAL)
    errors = errors_of(path, registry, overrides=["no-equals", "a..b=1", "suites[5].name=x"])
    assert [e.code for e in errors] == [Code.INVALID_OVERRIDE] * 3
    with pytest.raises(ConfigError):
        load(path, registry, environ={"DVF__": "x", "DVF__A____B": "1"}, overrides=["version=2"])


def test_profile_and_extends_errors(write: Any, registry: Registry) -> None:
    path = write("dvf.yaml", MINIMAL)
    assert errors_of(path, registry, profile="prod")[0].code is Code.PROFILE_NOT_FOUND
    write("a.yaml", "extends: b.yaml\n")
    write("b.yaml", "extends: a.yaml\n")
    cyclic = write("c.yaml", MINIMAL + "extends: a.yaml\n")
    assert "Circular" in errors_of(cyclic, registry)[0].message
    missing = write("d.yaml", MINIMAL + "extends: [nope.yaml]\n")
    assert "not found" in errors_of(missing, registry)[0].message
    bad = write("e.yaml", MINIMAL + "extends: 5\n")
    assert errors_of(bad, registry)[0].code is Code.EXTENDS_ERROR


def test_errors_have_locations_and_suggestions(write: Any, registry: Registry) -> None:
    path = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: demo}
        connections:
          src: {type: postgress, host: h}
          tgt: {type: postgres}
        suites:
          - name: s1
            source: src
            target: tgt
            tables:
              - name: t1
                source: {table: a}
                target: {table: b}
                colums: {}
                checks:
                  - {type: not_nul, columns: [id]}
                  - {type: unique, colums: [id]}
                  - {columns: [id]}
        """,
    )
    errors = {(e.code, e.path): e for e in errors_of(path, registry)}
    unknown_conn = errors[(Code.UNKNOWN_COMPONENT, "connections.src.type")]
    assert unknown_conn.hint == "Did you mean 'postgres'?"
    assert (unknown_conn.location.line, unknown_conn.location.column) == (4, 9)  # type: ignore[union-attr]
    assert errors[(Code.VALIDATION, "connections.tgt")].message == "set either 'url' or 'host'"
    assert errors[(Code.UNKNOWN_COMPONENT, "suites[0].tables[0].checks[0].type")].hint == (
        "Did you mean 'not_null'?"
    )
    field_error = errors[(Code.UNKNOWN_FIELD, "suites[0].tables[0].checks[1].colums")]
    assert field_error.hint == "Did you mean 'columns'?"
    assert field_error.location.line == 17  # type: ignore[union-attr]
    assert (Code.MISSING_FIELD, "suites[0].tables[0].checks[1].columns") in errors
    assert (Code.MISSING_FIELD, "suites[0].tables[0].checks[2].type") in errors
    table_field = errors[(Code.UNKNOWN_FIELD, "suites[0].tables[0].colums")]
    assert table_field.hint == "Did you mean 'columns'?"
    # Failed components are not re-reported against the generic base model.
    assert not any(
        path.startswith("connections.src.") and code is Code.UNKNOWN_FIELD for code, path in errors
    )


def test_version_and_missing_project(write: Any, registry: Registry) -> None:
    errors = errors_of(write("a.yaml", "project: {name: x}\n"), registry)
    assert errors[0].code is Code.MISSING_FIELD and "version: 1" in (errors[0].hint or "")
    errors = errors_of(write("b.yaml", "version: 2\nproject: {name: x}\n"), registry)
    assert errors[0].code is Code.UNSUPPORTED_VERSION
    errors = errors_of(write("c.yaml", "version: 1\n"), registry)
    assert errors[0].path == "project"


def test_cross_reference_checks(write: Any, registry: Registry) -> None:
    path = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: demo}
        defaults: {engine: duckdbb}
        connections: {src: {type: duckdb}, tgt: {type: duckdb}, solo: {type: duckdb}}
        reports: {formats: [htm]}
        suites:
          - name: s1
            source: src
            target: tgtt
            tables:
              - name: t1
                source: {table: a, connection: nope}
                target: {table: b}
                keys: [{source: id, normalize: [sap_alphaa]}]
                columns: {mappings: [{source: x, target: y, normalize: [rtrim]}]}
                comparison: {strategies: [row_cout], on_mismatch: ful_diff}
                checks:
                  - {type: not_null, columns: [id]}
                  - {type: not_null, columns: [id]}
                  - type: referential_integrity
                    column: id
                    parent: {connection: missing, table: p, column: id}
              - name: t1
                source: {table: a}
          - name: s1
            source: solo
            tables:
              - name: t
                source: {table: a}
                target: {table: b}
                checks: [{type: not_null, columns: [id], side: target}]
        """,
    )
    errors = errors_of(path, registry)
    found = {(e.code, e.path) for e in errors}
    expected = {
        (Code.UNKNOWN_COMPONENT, "defaults.engine"),
        (Code.UNKNOWN_COMPONENT, "reports.formats[0]"),
        (Code.UNKNOWN_CONNECTION, "suites[0].target"),
        (Code.UNKNOWN_CONNECTION, "suites[0].tables[0].source.connection"),
        (Code.UNKNOWN_COMPONENT, "suites[0].tables[0].keys[0].normalize[0]"),
        (Code.UNKNOWN_COMPONENT, "suites[0].tables[0].comparison.strategies[0]"),
        (Code.UNKNOWN_COMPONENT, "suites[0].tables[0].comparison.on_mismatch"),
        (Code.DUPLICATE_CHECK_ID, "suites[0].tables[0].checks[1]"),
        (Code.UNKNOWN_CONNECTION, "suites[0].tables[0].checks[2].parent.connection"),
        (Code.DUPLICATE_NAME, "suites[0].tables[1].name"),
        (Code.SUITE_TARGET_MISMATCH, "suites[0].tables[1]"),
        (Code.DUPLICATE_NAME, "suites[1].name"),
        (Code.SUITE_TARGET_MISMATCH, "suites[1].tables[0].target"),
        (Code.SUITE_TARGET_MISMATCH, "suites[1].tables[0].checks[0].side"),
    }
    assert expected <= found, expected - found
    assert len(found) == len(expected)


def test_waiver_warnings(write: Any, registry: Registry) -> None:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path = write(
        "dvf.yaml",
        MINIMAL
        + f"""
waivers:
  - {{check: s1.t1.compare.column_mismatch, reason: known, expires: {yesterday}}}
  - {{check: "s1.*", reason: glob ok}}
  - {{check: other.table.x, reason: typo}}
""",
    )
    loaded = load(path, registry)
    codes = [(w.code, w.path) for w in loaded.warnings]
    assert codes == [
        (Code.WAIVER_EXPIRED, "waivers[0].expires"),
        (Code.WAIVER_UNMATCHED, "waivers[2].check"),
    ]


def test_interpolation_and_dotenv(write: Any, registry: Registry, tmp_path: Path) -> None:
    write(".env", "PG_PASSWORD=from-dotenv\nPG_HOST=dotenv-host\n")
    path = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: "${var:name}"}
        vars: {name: interpolated, port: 5433}
        connections:
          src: {type: postgres, host: "${env:PG_HOST}", port: "${var:port}", password: "${secret:env:PG_PASSWORD}"}
          tgt: {type: duckdb}
        reports: {output_dir: "./out/${run.id}"}
        """,
    )
    loaded = load(path, registry, variables={"name": "from-cli"})
    connection = loaded.config.connections["src"]
    assert isinstance(connection, DatabaseConnection)
    assert loaded.config.project.name == "from-cli"
    assert connection.host == "dotenv-host"
    assert connection.port == 5433
    assert (
        connection.password is not None and connection.password.get_secret_value() == "from-dotenv"
    )
    assert loaded.config.reports.output_dir == "./out/${run.id}"
    explicit = write("other.env", "PG_PASSWORD=x\nPG_HOST=y\n")
    assert load(path, registry, env_file=explicit).config.connections["src"].host == "y"  # type: ignore[attr-defined]
    assert errors_of(path, registry, env_file=tmp_path / "nope.env")[0].code is Code.FILE_NOT_FOUND


def test_lenient_mode_warns_and_skips_secrets(write: Any, registry: Registry) -> None:
    path = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: demo}
        connections:
          src: {type: postgres, host: "${env:NOT_SET}", password: "${secret:env:NOT_SET_EITHER}"}
        """,
    )
    assert errors_of(path, registry)[0].code is Code.ENV_MISSING
    loaded = load(path, registry, strict=False, resolve_secrets=False)
    assert [w.code for w in loaded.warnings] == [Code.ENV_MISSING]


def test_local_plugin_check_and_hook(write: Any, registry: Registry) -> None:
    write(
        "plugins/acme.py",
        """
        from dvf.plugins import Check, CheckConfig, hookimpl, register_check

        SEEN = []

        @register_check("acme.vat")
        class Vat(Check):
            class Config(CheckConfig):
                column: str

        @hookimpl
        def on_config_loaded(config):
            SEEN.append(config.project.name)
        """,
    )
    path = write(
        "dvf.yaml",
        MINIMAL.replace("keys: [id]", "keys: [id]\n        checks: [{type: acme.vat, column: vat}]")
        + "plugins: {paths: [plugins]}\n",
    )
    loaded = load(path, registry)
    check = loaded.config.suites[0].tables[0].checks[0]
    assert type(check).__name__ == "Config" and check.column == "vat"  # type: ignore[attr-defined]
    module = next(m for k, m in registry.loaded_local.items() if k.endswith("acme.py"))
    assert module.SEEN == ["demo"]

    bad = write(
        "bad.yaml",
        MINIMAL.replace("keys: [id]", "keys: [id]\n        checks: [{type: acme.vat}]")
        + "plugins: {paths: [plugins]}\n",
    )
    assert errors_of(bad, registry)[0].path == "suites[0].tables[0].checks[0].column"


def test_plugin_load_errors_and_hook_failures(write: Any, registry: Registry) -> None:
    path = write("dvf.yaml", MINIMAL + "plugins: {paths: [missing_dir/]}\n")
    assert errors_of(path, registry)[0].code is Code.PLUGIN_LOAD
    write(
        "hooks/boom.py",
        "from dvf.plugins import hookimpl\n@hookimpl\ndef on_config_loaded(config):\n    raise RuntimeError('boom')\n",
    )
    path = write("dvf2.yaml", MINIMAL + "plugins: {paths: [hooks]}\n")
    with pytest.raises(PluginError, match="on_config_loaded hook failed"):
        load(path, registry)


def test_inherited_errors_are_deduplicated(write: Any, registry: Registry) -> None:
    path = write(
        "dvf.yaml",
        MINIMAL.replace(
            "    tables:", "    defaults: {normalization: {timezone: Mars/Base}}\n    tables:"
        ).replace(
            "keys: [id]", "keys: [id]\n      - {name: t2, source: {table: a}, target: {table: b}}"
        ),
    )
    errors = errors_of(path, registry)
    assert len(errors) == 1
    assert "Mars/Base" in errors[0].message


def test_load_config_data_in_memory(registry: Registry) -> None:
    data = {
        "version": 1,
        "project": {"name": "mem"},
        "profiles": {"dev": {"project": {"name": "mem-dev"}}},
        "extends": ["ignored.yaml"],
    }
    loaded = load_config_data(data, LoadOptions(profile="dev", environ={}), registry=registry)
    assert loaded.config.project.name == "mem-dev"
    with pytest.raises(ConfigError):
        load_config_data(data, LoadOptions(profile="prod", environ={}), registry=registry)


@pytest.mark.parametrize("suffix", [".json", ".toml", ".properties"])
def test_other_formats_end_to_end(write: Any, registry: Registry, suffix: str) -> None:
    content = {
        ".json": '{"version": 1, "project": {"name": "j"}, "connections": {"d": {"type": "duckdb"}}}',
        ".toml": 'version = 1\n[project]\nname = "j"\n[connections.d]\ntype = "duckdb"\n',
        ".properties": "version=1\nproject.name=j\nconnections.d.type=duckdb\n",
    }[suffix]
    config = load(write(f"dvf{suffix}", content), registry).config
    assert config.project.name == "j" and "d" in config.connections
