"""dbt: generate dvf tables from a dbt ``manifest.json``, and run dvf after ``dbt build``.

:func:`import_manifest` (``dvf dbt import target/manifest.json -o dvf.yaml``) turns every dbt
model of the project into a dvf table in one data quality suite: the model's relation, its
description and tags, keys from primary-key constraints, ``unique_key``, or ``unique`` tests,
and checks converted from dbt generic tests:

======================================================== ==================================
dbt test                                                 dvf check
======================================================== ==================================
``not_null``, ``dbt_expectations...to_not_be_null``      ``not_null`` (columns merged)
``unique``, ``dbt_utils.unique_combination_of_columns``  ``unique``
``accepted_values``, ``dbt_expectations...in_set``       ``allowed_values``
``relationships``                                        ``referential_integrity``
``dbt_utils.accepted_range``, ``...to_be_between``       ``range``
``dbt_utils.expression_is_true``                         ``expression``
``dbt_expectations...to_match_regex``                    ``regex`` (``full_match: false``)
``dbt_expectations.expect_table_row_count_to_be_between`` ``row_count_range``
======================================================== ==================================

Test ``severity: warn`` becomes ``severity: warning`` and a test's ``where`` is kept. Singular
tests and other generic tests are listed at the end of the file as not converted (dbt still runs
them). The connection comes from a configuration file, a location, or the manifest's adapter
type (with ``${env:...}`` placeholders). The generated file never contains secrets and passes
``dvf validate-config``.

:func:`built_model_selectors` reads ``target/run_results.json`` and returns ``--only`` selectors
for the models ``dbt build`` built successfully, so dvf validates exactly those.

dbt itself is not imported: the functions read dbt's JSON artifacts. They are verified by unit
tests with synthetic artifacts, not against a dbt project.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from dvf.config.loader import LoadedConfig, LoadOptions, load_config_data
from dvf.core.errors import ConfigError
from dvf.discovery.common import UniqueNames, generated_header, identifier, write_output
from dvf.discovery.connections import ConnectionSpec, spec_from_config, spec_from_location
from dvf.discovery.yamlwriter import Document, Flow, Map, Seq, dump, plain
from dvf.plugins.registry import Registry, get_registry

CONNECTION_NAME = "warehouse"
ENV_PREFIX = "WAREHOUSE"

_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_REF = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""")
_RELATION_TYPES = ("model", "seed", "snapshot")

# dbt adapter type -> dvf connection type
ADAPTERS = {
    "postgres": "postgres",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "databricks": "databricks",
    "duckdb": "duckdb",
    "sqlserver": "mssql",
    "fabric": "mssql",
    "synapse": "mssql",
    "mysql": "mysql",
    "oracle": "oracle",
    "spark": "spark",
}


@dataclass
class DbtImportResult:
    """The generated configuration and what was (and was not) converted."""

    document: Document
    models: int
    checks: int
    unsupported: list[str]
    notes: list[str]
    output: Path | None = None

    @property
    def text(self) -> str:
        return dump(self.document)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output) if self.output else None,
            "models": self.models,
            "checks": self.checks,
            "unsupported": list(self.unsupported),
            "notes": list(self.notes),
        }


@dataclass
class _Check:
    data: dict[str, Any]
    comment: str | None = None


@dataclass
class _Model:
    node: dict[str, Any]
    checks: list[_Check] = field(default_factory=list)
    not_null: dict[tuple[str | None, str | None], list[tuple[str, str]]] = field(
        default_factory=dict
    )
    """(severity, where) -> [(column, dbt test name)], merged into one check each."""
    unique_tests: list[tuple[list[str], str]] = field(default_factory=list)
    has_row_count: bool = False

    @property
    def name(self) -> str:
        return str(self.node.get("name") or "")


# ---------------------------------------------------------------------- reading artifacts


def read_manifest(path: str | Path) -> dict[str, Any]:
    """A dbt ``manifest.json`` as a dictionary; :class:`ConfigError` when it is not one."""
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"{file} does not exist.", hint="Run 'dbt build' or 'dbt compile' to write it."
        ) from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{file} could not be read as JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
        raise ConfigError(
            f"{file} is not a dbt manifest (it has no 'nodes').",
            hint="Pass the manifest.json from your dbt project's target folder.",
        )
    return data


def built_model_selectors(
    run_results: str | Path, *, statuses: Iterable[str] = ("success",)
) -> list[str]:
    """``table:<model>`` selectors for the models with one of ``statuses`` in a dbt
    ``run_results.json`` (by default, the models ``dbt build`` or ``dbt run`` built)."""
    file = Path(run_results)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{file} could not be read as JSON: {exc}") from exc
    wanted = {s.lower() for s in statuses}
    selectors: list[str] = []
    for item in data.get("results", []) if isinstance(data, dict) else []:
        unique_id = str(item.get("unique_id", ""))
        if not unique_id.startswith("model.") or str(item.get("status", "")).lower() not in wanted:
            continue
        name = identifier(unique_id.split(".")[-1], "model")
        selectors.append(f"table:{name}")
    return list(dict.fromkeys(selectors))


# ---------------------------------------------------------------------- relations


def _relation(node: Mapping[str, Any], default_database: str | None) -> str:
    """``schema.name`` (or ``database.schema.name`` outside the connection's database)."""
    name = str(node.get("alias") or node.get("identifier") or node.get("name") or "")
    schema = node.get("schema")
    database = node.get("database")
    parts = [str(schema), name] if schema else [name]
    if database and schema and default_database and str(database) != default_database:
        parts.insert(0, str(database))
    return ".".join(parts)


def _main_database(nodes: Iterable[Mapping[str, Any]]) -> str | None:
    counts = Counter(str(n["database"]) for n in nodes if n.get("database"))
    return counts.most_common(1)[0][0] if counts else None


def _adapter_connection(adapter: str, database: str | None) -> tuple[ConnectionSpec, list[str]]:
    conn_type = ADAPTERS.get(adapter)
    env = ENV_PREFIX
    notes: list[str] = []
    config: dict[str, Any]
    if conn_type in ("postgres", "redshift", "mssql", "mysql", "oracle"):
        config = {"type": conn_type, "host": f"${{env:{env}_HOST}}"}
        if database:
            config["database"] = database
        config["user"] = f"${{env:{env}_USER}}"
        config["password"] = f"${{secret:env:{env}_PASSWORD}}"
        notes.append(f"Set {env}_HOST, {env}_USER, and {env}_PASSWORD before running.")
    elif conn_type == "snowflake":
        config = {"type": "snowflake", "account": f"${{env:{env}_ACCOUNT}}"}
        if database:
            config["database"] = database
        config["user"] = f"${{env:{env}_USER}}"
        config["password"] = f"${{secret:env:{env}_PASSWORD}}"
        notes.append(
            f"Set {env}_ACCOUNT, {env}_USER, and {env}_PASSWORD (add 'warehouse' and 'role' if "
            "the user has no defaults)."
        )
    elif conn_type == "bigquery":
        config = {"type": "bigquery", "project": database or f"${{env:{env}_PROJECT}}"}
        notes.append(
            "BigQuery uses Application Default Credentials unless credentials_path is set."
        )
    elif conn_type == "databricks":
        config = {
            "type": "databricks",
            "server_hostname": f"${{env:{env}_HOST}}",
            "http_path": f"${{env:{env}_HTTP_PATH}}",
            "access_token": f"${{secret:env:{env}_TOKEN}}",
        }
        if database:
            config["catalog"] = database
        notes.append(f"Set {env}_HOST, {env}_HTTP_PATH, and {env}_TOKEN before running.")
    elif conn_type == "duckdb":
        config = {"type": "duckdb", "database": f"./{database or 'warehouse'}.duckdb"}
        notes.append("REVIEW: point the DuckDB connection at the file dbt writes (profiles.yml).")
    elif conn_type == "spark":
        config = {"type": "spark"}
        notes.append("The Spark connection uses the running Spark session, or a local one.")
    else:
        config = {"type": "duckdb", "database": "./warehouse.duckdb"}
        notes.append(
            f"The connection is a placeholder: dvf has no connector for the dbt adapter "
            f"'{adapter or 'unknown'}'. Replace it with the warehouse dbt builds into."
        )
    return ConnectionSpec(CONNECTION_NAME, config, config, notes), notes


# ---------------------------------------------------------------------- tests


def _severity(config: Mapping[str, Any]) -> str | None:
    value = str(config.get("severity") or "").lower()
    return "warning" if value == "warn" else None


def _where(config: Mapping[str, Any]) -> str | None:
    value = config.get("where")
    return str(value) if value else None


def _attached(test: Mapping[str, Any], models_by_name: Mapping[str, str]) -> str | None:
    attached = test.get("attached_node")
    if attached:
        return str(attached)
    kwargs = (test.get("test_metadata") or {}).get("kwargs") or {}
    match = _REF.search(str(kwargs.get("model", "")))
    if match and match.group(1) in models_by_name:
        return models_by_name[match.group(1)]
    nodes = (test.get("depends_on") or {}).get("nodes") or []
    return str(nodes[0]) if nodes else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    try:
        number = float(str(value))
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


class _Converter:
    """Converts dbt test nodes into dvf checks on their models."""

    def __init__(
        self, manifest: Mapping[str, Any], models: dict[str, _Model], database: str | None
    ) -> None:
        self.manifest = manifest
        self.models = models
        self.database = database
        self.unsupported: list[str] = []
        self.names = UniqueNames()
        self.models_by_name = {m.name: uid for uid, m in models.items()}

    def parent_relation(self, test: Mapping[str, Any], model_id: str) -> str | None:
        nodes = (test.get("depends_on") or {}).get("nodes") or []
        for unique_id in nodes:
            if unique_id == model_id:
                continue
            node = self.manifest.get("nodes", {}).get(unique_id) or self.manifest.get(
                "sources", {}
            ).get(unique_id)
            if node is not None:
                return _relation(node, self.database)
        return None

    def convert(self, test: Mapping[str, Any]) -> None:
        name = str(test.get("name") or test.get("unique_id") or "test")
        config = test.get("config") or {}
        if config.get("enabled") is False:
            return
        metadata = test.get("test_metadata")
        model_id = _attached(test, self.models_by_name)
        if model_id is None or model_id not in self.models:
            return  # a test on a model that is not imported, or on a source
        model = self.models[model_id]
        if not metadata:
            self.unsupported.append(f"{model.name}: {name}: singular test (dbt runs it)")
            return
        test_name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "")
        full = f"{namespace}.{test_name}" if namespace else test_name
        kwargs: Mapping[str, Any] = metadata.get("kwargs") or {}
        column = kwargs.get("column_name") or test.get("column_name")
        if column is not None and not _COLUMN.match(str(column)):
            self.unsupported.append(
                f"{model.name}: {name}: column expression '{column}' (dvf checks take column names)"
            )
            return
        severity, where = _severity(config), _where(config)
        comment = f"dbt test {name}"
        error_if = str(config.get("error_if") or "!= 0").replace(" ", "")
        if error_if != "!=0":
            comment += f"; REVIEW: dbt fails when failures {config.get('error_if')}"
        check = self._check(full, kwargs, None if column is None else str(column), model, test)
        if check is None:
            self.unsupported.append(f"{model.name}: {name}: test '{full}' has no dvf equivalent")
            return
        if check == {}:  # merged into the model's not_null check
            model.not_null.setdefault((severity, where), []).append((str(column), name))
            return
        if where:
            check["where"] = where
        if severity:
            check["severity"] = severity
        model.checks.append(_Check(check, comment))

    def _check(
        self,
        full: str,
        kwargs: Mapping[str, Any],
        column: str | None,
        model: _Model,
        test: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """The dvf check for one generic test; ``{}`` for a ``not_null`` to merge."""
        short = full.rsplit(".", 1)[-1]
        if short in ("not_null", "expect_column_values_to_not_be_null") and column:
            return {}
        if short in ("unique", "expect_column_values_to_be_unique") and column:
            model.unique_tests.append(([column], str(test.get("name"))))
            return {"type": "unique", "columns": [column]}
        if short == "unique_combination_of_columns":
            columns = [str(c) for c in kwargs.get("combination_of_columns") or []]
            if not columns or not all(_COLUMN.match(c) for c in columns):
                return None
            model.unique_tests.append((columns, str(test.get("name"))))
            return {"type": "unique", "columns": columns}
        if short in ("accepted_values", "expect_column_values_to_be_in_set") and column:
            values = kwargs.get("values") if short == "accepted_values" else kwargs.get("value_set")
            if not isinstance(values, list) or not values:
                return None
            return {"type": "allowed_values", "column": column, "values": list(values)}
        if short == "relationships" and column:
            field_name = kwargs.get("field")
            parent = self.parent_relation(test, str(model.node.get("unique_id")))
            if not field_name or parent is None or not _COLUMN.match(str(field_name)):
                return None
            return {
                "type": "referential_integrity",
                "column": column,
                "parent": Flow({"table": parent, "column": str(field_name)}),
            }
        if short in ("accepted_range", "expect_column_values_to_be_between") and column:
            low, high = _number(kwargs.get("min_value")), _number(kwargs.get("max_value"))
            if low is None and high is None:
                return None
            check: dict[str, Any] = {"type": "range", "column": column}
            if low is not None:
                check["min"] = low
            if high is not None:
                check["max"] = high
            strict = kwargs.get("strictly") is True or kwargs.get("inclusive") is False
            if strict:
                check["inclusive"] = False
            return check
        if short == "expression_is_true":
            expression = str(kwargs.get("expression") or "").strip()
            if not expression:
                return None
            if column:
                expression = f"{column} {expression}"
            return {
                "type": "expression",
                "name": self.names.take(str(test.get("name") or ""), "expression"),
                "expression": expression,
            }
        if short == "expect_column_values_to_match_regex" and column and kwargs.get("regex"):
            return {
                "type": "regex",
                "column": column,
                "pattern": str(kwargs["regex"]),
                "full_match": False,
            }
        if short == "expect_table_row_count_to_be_between":
            low, high = _number(kwargs.get("min_value")), _number(kwargs.get("max_value"))
            if low is None and high is None:
                return None
            model.has_row_count = True
            check = {"type": "row_count_range"}
            if low is not None:
                check["min"] = int(low)
            if high is not None:
                check["max"] = int(high)
            return check
        return None


# ---------------------------------------------------------------------- selection and keys


def _selected(
    node: Mapping[str, Any],
    *,
    project: str | None,
    all_packages: bool,
    models: Sequence[str],
    tags: Sequence[str],
    exclude: Sequence[str],
) -> bool:
    if node.get("resource_type") != "model":
        return False
    config = node.get("config") or {}
    if config.get("enabled") is False or config.get("materialized") == "ephemeral":
        return False
    if not all_packages and project and node.get("package_name") not in (None, project):
        return False
    name = str(node.get("name") or "").lower()
    if models and not any(fnmatchcase(name, m.lower()) for m in models):
        return False
    if exclude and any(fnmatchcase(name, e.lower()) for e in exclude):
        return False
    node_tags = {str(t).lower() for t in [*(node.get("tags") or []), *(config.get("tags") or [])]}
    return not tags or any(t.lower() in node_tags for t in tags)


def _keys(model: _Model) -> tuple[list[str], str] | None:
    node = model.node
    for constraint in node.get("constraints") or []:
        if constraint.get("type") == "primary_key" and constraint.get("columns"):
            return [str(c) for c in constraint["columns"]], "primary key constraint in dbt"
    columns = node.get("columns") or {}
    pk = [
        str(name)
        for name, col in columns.items()
        if any(c.get("type") == "primary_key" for c in (col or {}).get("constraints") or [])
    ]
    if pk:
        return pk, "primary key constraint in dbt"
    unique_key = (node.get("config") or {}).get("unique_key")
    if isinstance(unique_key, str) and _COLUMN.match(unique_key):
        return [unique_key], "unique_key of the incremental model"
    if (
        isinstance(unique_key, list)
        and unique_key
        and all(_COLUMN.match(str(k)) for k in unique_key)
    ):
        return [str(k) for k in unique_key], "unique_key of the incremental model"
    if model.unique_tests:
        columns, test = min(model.unique_tests, key=lambda item: len(item[0]))
        return columns, f"from dbt test {test}"
    return None


# ---------------------------------------------------------------------- entry point


def _connection(
    manifest: Mapping[str, Any],
    database: str | None,
    *,
    loaded: LoadedConfig | None,
    config_path: Path | None,
    connection: str | None,
    location: str | None,
    output_dir: Path,
) -> tuple[ConnectionSpec, list[str]]:
    if loaded is not None:
        if config_path is None:
            raise ConfigError("A configuration's path is needed to copy its connection.")
        names = list(loaded.config.connections)
        if connection is None and len(names) != 1:
            raise ConfigError(
                f"{config_path} defines {len(names)} connections; choose one with --connection.",
                hint="Defined connections: " + (", ".join(names) or "none") + ".",
            )
        spec = spec_from_config(loaded, config_path, connection or names[0], output_dir=output_dir)
        return spec, list(spec.notes)
    if location is not None:
        spec = spec_from_location(
            location, name=CONNECTION_NAME, side="warehouse", output_dir=output_dir
        )
        return spec, list(spec.notes)
    adapter = str((manifest.get("metadata") or {}).get("adapter_type") or "").lower()
    return _adapter_connection(adapter, database)


def import_manifest(
    path: str | Path,
    *,
    loaded: LoadedConfig | None = None,
    config_path: str | Path | None = None,
    connection: str | None = None,
    location: str | None = None,
    models: Sequence[str] = (),
    tags: Sequence[str] = (),
    exclude: Sequence[str] = (),
    all_packages: bool = False,
    tests: bool = True,
    row_count: bool = True,
    suite_name: str | None = None,
    project_name: str | None = None,
    output: str | Path | None = None,
    overwrite: bool = False,
    registry: Registry | None = None,
) -> DbtImportResult:
    """Generate a dvf configuration with one table per dbt model of ``path`` (a manifest).

    ``models`` and ``exclude`` are globs over model names, ``tags`` keeps models with any of
    the tags, and ``all_packages`` includes models of installed packages. The connection is
    copied from ``loaded`` (``connection`` names it), built from ``location`` (a URL or database
    file), or derived from the manifest's adapter type with ``${env:WAREHOUSE_...}``
    placeholders. With ``output`` the file is written there (``overwrite`` replaces it).
    """
    manifest_path = Path(path)
    output_path = Path(output) if output is not None else None
    registry = registry or (loaded.registry if loaded is not None else get_registry())
    manifest = read_manifest(manifest_path)
    meta = manifest.get("metadata") or {}
    nodes: dict[str, Any] = manifest["nodes"]
    project = meta.get("project_name")
    selected = {
        uid: _Model(dict(node))
        for uid, node in sorted(nodes.items())
        if isinstance(node, dict)
        and _selected(
            node,
            project=str(project) if project else None,
            all_packages=all_packages,
            models=models,
            tags=tags,
            exclude=exclude,
        )
    }
    if not selected:
        raise ConfigError(
            f"No dbt models in {manifest_path} match the selection.",
            hint="Check --model, --tag, and --exclude, or pass --all-packages.",
        )
    database = _main_database(m.node for m in selected.values())
    output_dir = (output_path.parent if output_path is not None else Path.cwd()).resolve()
    spec, notes = _connection(
        manifest,
        database,
        loaded=loaded,
        config_path=Path(config_path) if config_path is not None else None,
        connection=connection,
        location=location,
        output_dir=output_dir,
    )
    converter = _Converter(manifest, selected, database)
    if tests:
        for node in nodes.values():
            if isinstance(node, dict) and node.get("resource_type") == "test":
                converter.convert(node)
    tables = Seq()
    table_names = UniqueNames()
    total_checks = 0
    for uid, model in selected.items():
        entry, count = _table_entry(uid, model, table_names, database, row_count)
        total_checks += count
        tables.add(entry)
    root = Map()
    root.add("version", 1)
    root.add("project", Map().add("name", project_name or str(project or "dbt")))
    root.add(
        "connections",
        Map().add(
            spec.name,
            Flow(spec.written_config),
            inline="REVIEW: placeholder" if any("placeholder" in n for n in notes) else None,
        ),
    )
    suite = Map().add("name", identifier(suite_name or "dbt_models", "dbt_models"))
    suite.add("source", spec.name)
    suite.add("tags", Flow(["dbt"]))
    suite.add("tables", tables)
    root.add("suites", Seq().add(suite))
    header = generated_header(
        "dvf dbt import",
        f"Manifest: {manifest_path.as_posix()} (dbt {meta.get('dbt_version', 'unknown')}, "
        f"adapter {meta.get('adapter_type', 'unknown')}).",
        "One table per dbt model; dbt generic tests are converted to checks.",
        "Review the connection and every REVIEW comment, then run 'dvf validate-config'.",
    )
    footer: list[str] = []
    if converter.unsupported:
        footer.append(f"{len(converter.unsupported)} dbt test(s) were not converted:")
        footer += [f"  - {u}" for u in converter.unsupported]
    footer += [f"Note: {n}" for n in notes]
    document = Document(root, header=header, footer=footer)
    _validate(document, registry, output_dir)
    result = DbtImportResult(document, len(selected), total_checks, converter.unsupported, notes)
    if output_path is not None:
        write_output(result.text, output_path, overwrite=overwrite)
        result.output = output_path
    return result


def _table_entry(
    unique_id: str,
    model: _Model,
    names: UniqueNames,
    database: str | None,
    row_count: bool,
) -> tuple[Map, int]:
    node = model.node
    materialized = (node.get("config") or {}).get("materialized") or "model"
    entry = Map()
    entry.add("name", names.take(model.name, "model"), inline=f"{unique_id} ({materialized})")
    description = str(node.get("description") or "").strip()
    if description:
        entry.add("description", description)
    model_tags = list(dict.fromkeys(str(t) for t in node.get("tags") or []))
    entry.add("tags", Flow(["dbt", *[t for t in model_tags if t != "dbt"]]))
    entry.add("source", Flow({"table": _relation(node, database)}))
    keys = _keys(model)
    if keys is not None:
        entry.add("keys", Flow(keys[0]), inline=keys[1])
    checks = Seq()
    for (severity, where), items in model.not_null.items():
        columns = list(dict.fromkeys(column for column, _ in items))
        data: dict[str, Any] = {"type": "not_null", "columns": Flow(columns)}
        if where:
            data["where"] = where
        if severity:
            data["severity"] = severity
        tests = ", ".join(name for _, name in items)
        checks.add(Flow(data), inline=f"dbt test{'s' if len(items) > 1 else ''} {tests}")
    for check in model.checks:
        checks.add(Flow(check.data), inline=check.comment)
    if row_count and not model.has_row_count:
        checks.add(
            Flow({"type": "row_count_range", "min": 1}),
            inline="REVIEW: remove if the model may be empty",
        )
    if len(checks):
        entry.add("checks", checks)
    return entry, len(checks)


def _validate(document: Document, registry: Registry, base_dir: Path) -> None:
    options = LoadOptions(strict=False, resolve_secrets=False, env_overrides=False)
    try:
        load_config_data(plain(document), options, registry=registry, base_dir=base_dir)
    except ConfigError as exc:
        raise ConfigError(
            "The dbt manifest produced an invalid configuration; please report this.",
            exc.diagnostics,
        ) from exc


__all__ = [
    "ADAPTERS",
    "DbtImportResult",
    "built_model_selectors",
    "import_manifest",
    "read_manifest",
]
