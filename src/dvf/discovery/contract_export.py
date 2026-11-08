"""``dvf export-contract``: write an ODCS v3.1 data contract from a configuration's datasets.

Each selected table becomes a schema object. Its properties come from the dataset's schema
(physical and logical types, NOT NULL, primary key), and its checks are translated into ODCS
constraints and quality rules where ODCS can express them. Checks it cannot express are listed
in the result and as comments in the contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dvf.config import checks as cfg
from dvf.config.connections import DatabaseConnection, DuckDBConnection, FileConnection
from dvf.config.loader import LoadedConfig
from dvf.config.models import SuiteConfig, TableConfig
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Side
from dvf.core.errors import ConfigError, DvfError
from dvf.core.types import LogicalType
from dvf.discovery.common import generated_header, identifier, write_output
from dvf.discovery.conformance import odcs_logical_type
from dvf.discovery.contract_import import Unsupported
from dvf.discovery.profiling import SideChoice, render_spec, selected_sides
from dvf.discovery.yamlwriter import Document, Map, dump
from dvf.execution.runner import Runner, RunOptions
from dvf.execution.selectors import Selectors

ODCS_VERSION = "v3.1.0"
_REVERSE_OPERATORS = {
    "==": "mustBe",
    "!=": "mustNotBe",
    ">": "mustBeGreaterThan",
    ">=": "mustBeGreaterOrEqualTo",
    "<": "mustBeLessThan",
    "<=": "mustBeLessOrEqualTo",
}


@dataclass
class ContractExportResult:
    """The contract and what could not be expressed in it."""

    document: Document
    objects: int
    unsupported: list[Unsupported] = field(default_factory=list)
    output: Path | None = None

    @property
    def text(self) -> str:
        return dump(self.document)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output) if self.output else None,
            "objects": self.objects,
            "unsupported": [u.to_dict() for u in self.unsupported],
        }


def _check_side(suite: SuiteConfig, check: cfg.CheckConfig) -> set[str]:
    side = check.side or (Side.TARGET if suite.is_comparison else Side.SOURCE)
    if side is Side.BOTH:
        return {"source", "target"}
    return {side.value}


def _percent(mostly: float) -> float:
    return round((1 - mostly) * 100, 10)


class _Exporter:
    def __init__(self) -> None:
        self.unsupported: list[Unsupported] = []

    def skip(self, where: str, check: cfg.CheckConfig, reason: str) -> None:
        label = f"check {check.type}" + (f" ({check.name})" if check.name else "")
        self.unsupported.append(Unsupported(where, label, reason))

    def object(
        self,
        name: str,
        physical: str,
        is_file: bool,
        schema: DatasetSchema,
        table: TableConfig,
        side: str,
        suite: SuiteConfig,
    ) -> dict[str, Any]:
        properties: dict[str, dict[str, Any]] = {}
        for column in schema.columns:
            prop: dict[str, Any] = {"name": column.name}
            logical = odcs_logical_type(column.type)
            if logical:
                prop["logicalType"] = logical
            prop["physicalType"] = column.native_type
            if column.nullable is False:
                prop["required"] = True
            if column.name in schema.primary_key:
                prop["primaryKey"] = True
                prop["primaryKeyPosition"] = schema.primary_key.index(column.name) + 1
            if column.type.logical is LogicalType.STRING and column.type.length:
                prop["logicalTypeOptions"] = {"maxLength": column.type.length}
            if column.comment:
                prop["description"] = column.comment
            properties[column.name.lower()] = prop
        obj: dict[str, Any] = {
            "name": name,
            "physicalName": physical,
            "physicalType": "file" if is_file else "table",
            "logicalType": "object",
        }
        quality: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        for check in table.checks:
            if not check.enabled or side not in _check_side(suite, check):
                continue
            self.check(name, check, properties, quality, relationships, table.severity.value)
        obj["properties"] = list(properties.values())
        if quality:
            obj["quality"] = quality
        if relationships:
            obj["relationships"] = relationships
        return obj

    def check(
        self,
        where: str,
        check: cfg.CheckConfig,
        properties: dict[str, dict[str, Any]],
        quality: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        default_severity: str = "error",
    ) -> None:
        if check.where:
            self.skip(where, check, "ODCS rules have no row filter ('where')")
            return

        def prop(name: str) -> dict[str, Any] | None:
            found = properties.get(name.lower())
            if found is None:
                self.skip(where, check, f"column '{name}' is not in the dataset")
            return found

        def rule(base: dict[str, Any]) -> dict[str, Any]:
            if check.name:
                base = {"id": check.name, **base}
            if check.description:
                base["description"] = check.description
            if check.severity is not None and check.severity.value != default_severity:
                base["severity"] = check.severity.value
            return base

        def add_property_rule(name: str, body: dict[str, Any]) -> None:
            target = prop(name)
            if target is not None:
                target.setdefault("quality", []).append(rule(body))

        mostly = check.mostly
        tolerance = (
            {"mustBe": 0}
            if mostly >= 1
            else {"mustBeLessOrEqualTo": _percent(mostly), "unit": "percent"}
        )
        if isinstance(check, cfg.NotNullCheck):
            # A data rule, not the declared schema: 'required' stays what the catalog says.
            for name in check.columns:
                add_property_rule(name, {"metric": "nullValues", **tolerance})
        elif isinstance(check, cfg.UniqueCheck):
            if len(check.columns) == 1 and mostly >= 1:
                target = prop(check.columns[0])
                if target is not None:
                    target["unique"] = True
            elif mostly >= 1:
                quality.append(
                    rule(
                        {
                            "metric": "duplicateValues",
                            "arguments": {"properties": list(check.columns)},
                            "mustBe": 0,
                        }
                    )
                )
            else:
                self.skip(where, check, "'mostly' on uniqueness has no ODCS equivalent")
        elif isinstance(check, cfg.AllowedValuesCheck):
            add_property_rule(
                check.column,
                {
                    "metric": "invalidValues",
                    "arguments": {"validValues": list(check.values)},
                    **tolerance,
                },
            )
        elif isinstance(check, cfg.RegexCheck):
            if not check.full_match:
                self.skip(
                    where, check, "ODCS patterns match whole values; this regex matches substrings"
                )
                return
            add_property_rule(
                check.column,
                {"metric": "invalidValues", "arguments": {"pattern": check.pattern}, **tolerance},
            )
        elif isinstance(check, cfg.NullRatioCheck):
            add_property_rule(
                check.column,
                {
                    "metric": "nullValues",
                    "mustBeLessOrEqualTo": round(check.max_ratio * 100, 10),
                    "unit": "percent",
                },
            )
        elif isinstance(check, cfg.RangeCheck | cfg.LengthCheck):
            self.bounds(where, check, prop)
        elif isinstance(check, cfg.RowCountRangeCheck):
            if check.min is not None:
                quality.append(rule({"metric": "rowCount", "mustBeGreaterOrEqualTo": check.min}))
            if check.max is not None:
                quality.append(rule({"metric": "rowCount", "mustBeLessOrEqualTo": check.max}))
        elif isinstance(check, cfg.CustomSqlCheck):
            sql = check.sql.replace("{table}", "{object}")
            if check.returns == "scalar":
                assert check.expect is not None
                operator = _REVERSE_OPERATORS[check.expect.operator]
                quality.append(rule({"type": "sql", "query": sql, operator: check.expect.value}))
            elif mostly >= 1:
                quality.append(
                    rule(
                        {
                            "type": "sql",
                            "query": f"SELECT COUNT(*) FROM ({sql}) AS failing_rows",
                            "mustBe": 0,
                        }
                    )
                )
            else:
                self.skip(where, check, "'mostly' on failing-row queries has no ODCS equivalent")
        elif isinstance(check, cfg.ReferentialIntegrityCheck):
            parent = check.parent
            if len(check.columns) == 1:
                target = prop(check.columns[0])
                if target is not None:
                    target.setdefault("relationships", []).append(
                        {"to": f"{parent.table}.{parent.columns[0]}"}
                    )
            else:
                relationships.append(
                    {
                        "from": [f"{where}.{c}" for c in check.columns],
                        "to": [f"{parent.table}.{c}" for c in parent.columns],
                    }
                )
        elif check.type == "contract":
            self.skip(where, check, "contract checks refer to a contract themselves")
        else:
            self.skip(where, check, f"ODCS has no equivalent of '{check.type}' checks")

    def bounds(self, where: str, check: cfg.RangeCheck | cfg.LengthCheck, prop: Any) -> None:
        if check.mostly < 1:
            self.skip(where, check, "'mostly' on ranges and lengths has no ODCS equivalent")
            return
        target = prop(check.column)
        if target is None:
            return
        options = target.setdefault("logicalTypeOptions", {})
        if isinstance(check, cfg.LengthCheck):
            if check.min is not None:
                options["minLength"] = check.min
            if check.max is not None:
                options["maxLength"] = min(check.max, options.get("maxLength", check.max))
            return
        if check.min is not None:
            options["minimum"] = check.min
            if not check.inclusive:
                options["exclusiveMinimum"] = True
        if check.max is not None:
            options["maximum"] = check.max
            if not check.inclusive:
                options["exclusiveMaximum"] = True


def _servers(loaded: LoadedConfig, names: Sequence[str]) -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    for name in dict.fromkeys(names):
        conn = loaded.config.connections.get(name)
        server: dict[str, Any] = {"server": name}
        if isinstance(conn, FileConnection):
            server.update(type="local", path=conn.path)
            if conn.format:
                server["format"] = conn.format
        elif isinstance(conn, DuckDBConnection):
            server.update(type="duckdb", database=conn.database)
        elif isinstance(conn, DatabaseConnection) and conn.type in (
            "postgres",
            "mysql",
            "mssql",
            "oracle",
            "redshift",
        ):
            server["type"] = {"postgres": "postgresql", "mssql": "sqlserver"}.get(
                conn.type, conn.type
            )
            for key in ("host", "port", "database"):
                if getattr(conn, key) is not None:
                    server[key] = getattr(conn, key)
            if conn.url is not None:  # host, port, and database only: never the user or password
                parts = urlsplit(conn.url.get_secret_value())
                if parts.hostname:
                    server.setdefault("host", parts.hostname)
                if parts.port:
                    server.setdefault("port", parts.port)
                if parts.path.strip("/"):
                    server.setdefault("database", parts.path.strip("/"))
            if conn.schema_:
                server["schema"] = conn.schema_
        else:
            continue  # other connection types: no server entry (credentials are never exported)
        servers.append(server)
    return servers


def export_contract(
    loaded: LoadedConfig,
    *,
    tables: Sequence[str] = (),
    suite: str | None = None,
    side: SideChoice = "auto",
    contract_id: str | None = None,
    version: str = "1.0.0",
    status: str = "draft",
    output: Path | None = None,
    overwrite: bool = False,
) -> ContractExportResult:
    """Build an ODCS v3.1 contract from the selected tables and, with ``output``, write it."""
    if side == "both":
        raise ConfigError("A contract describes one side; use --side source or --side target.")
    only = [f"table:{t}" for t in tables] + ([f"suite:{suite}"] if suite else [])
    runner = Runner(loaded, RunOptions(selectors=Selectors.parse(only), write_reports=False))
    exporter = _Exporter()
    objects: list[dict[str, Any]] = []
    connections: list[str] = []
    try:
        for task in runner._expand():
            for chosen in selected_sides(task.suite, side):
                table, suite_cfg = task.table, task.suite
                assert table.source is not None
                if chosen is Side.SOURCE:
                    spec, conn_name = table.source, table.source.connection or suite_cfg.source
                else:
                    spec = table.target or table.source.model_copy(
                        update={"filter": None, "connection": None}
                    )
                    conn_name = spec.connection or suite_cfg.target or suite_cfg.source
                try:
                    connector = runner.pool.get(conn_name)
                    relation = connector.resolve(render_spec(runner, spec))
                    schema = connector.get_schema(relation)
                    if not schema.primary_key and relation.table is not None:
                        schema = schema.model_copy(
                            update={"primary_key": connector.get_primary_key(relation)}
                        )
                except DvfError as exc:
                    raise ConfigError(
                        f"Table '{table.label}' ({chosen.value}) could not be read: {exc}"
                    ) from exc
                physical = relation.table or spec.path or relation.ref.name
                objects.append(
                    exporter.object(
                        table.label,
                        physical,
                        spec.path is not None,
                        schema,
                        table,
                        chosen.value,
                        suite_cfg,
                    )
                )
                connections.append(conn_name)
    finally:
        runner.pool.close_all()
    if not objects:
        raise ConfigError(
            "No tables matched the selection.", hint="Check --table, --suite, and --side."
        )
    project = loaded.config.project
    root = Map()
    root.add("apiVersion", ODCS_VERSION)
    root.add("kind", "DataContract")
    root.add("id", contract_id or identifier(project.name, "contract"))
    root.add("name", project.name)
    root.add("version", version)
    root.add("status", status)
    if project.description:
        root.add("description", {"purpose": project.description})
    servers = _servers(loaded, connections)
    if servers:
        root.add("servers", servers)
    root.add("schema", objects)
    header = generated_header(
        "dvf export-contract",
        f"ODCS {ODCS_VERSION} contract for project '{project.name}'. Review it before publishing:",
        "types, required columns, and keys come from the database catalog; rules come from the",
        "configuration's checks. Credentials are never exported.",
    )
    footer = [f"Not exported: {u}" for u in exporter.unsupported]
    result = ContractExportResult(
        Document(root, header=header, footer=footer), len(objects), exporter.unsupported
    )
    if output is not None:
        write_output(result.text, output, overwrite=overwrite)
        result.output = output
    return result


__all__ = ["ODCS_VERSION", "ContractExportResult", "export_contract"]
