"""ODCS v3 contracts: parsing, conversion to checks, and SCH-12 conformance logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dvf import api
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConfigError
from dvf.core.types import parse_native_type
from dvf.discovery.conformance import ContractCheck, check_conformance, odcs_logical_type
from dvf.discovery.contract_import import import_contract
from dvf.discovery.odcs import load_contract, parse_contract, parse_odcs
from dvf.plugins.registry import Registry
from tests.conftest import EXAMPLES, WriteFile

CONTRACT = EXAMPLES / "discovery" / "orders.odcs.yaml"


def _checks(text: str) -> dict[str, list[dict[str, Any]]]:
    data = yaml.safe_load(text)
    return {t["name"]: t.get("checks", []) for t in data["suites"][0]["tables"]}


def _contract(write: WriteFile, body: dict[str, Any], name: str = "c.yaml") -> Path:
    document = {"apiVersion": "v3.1.0", "kind": "DataContract", "id": "c1", **body}
    return write(name, yaml.safe_dump(document, sort_keys=False))


def _object(
    properties: list[dict[str, Any]], quality: list[dict[str, Any]] | None = None, **extra: Any
) -> dict[str, Any]:
    obj: dict[str, Any] = {"name": "t", "properties": properties, **extra}
    if quality:
        obj["quality"] = quality
    return {"schema": [obj]}


# ---------------------------------------------------------------------- parsing


def test_parse_the_example_contract() -> None:
    contract = load_contract(CONTRACT)
    assert contract.format == "odcs" and contract.api_version == "v3.1.0"
    assert [o.name for o in contract.objects] == ["customers", "orders"]
    customers = contract.object("customers")
    assert customers.primary_key() == ["customer_id"]
    email = customers.find("email")
    assert (
        email is not None and email.required and email.unique and email.options["maxLength"] == 254
    )
    assert [s["server"] for s in contract.servers] == ["analytics", "local"]
    assert contract.object(table="shop.orders").name == "orders"
    with pytest.raises(ConfigError, match="no schema object 'nope'"):
        contract.object("nope")
    with pytest.raises(ConfigError, match="say which one"):
        contract.object()


def test_parse_errors_and_tolerated_shapes(write: WriteFile) -> None:
    with pytest.raises(ConfigError, match="ODCS v2"):
        parse_odcs({"dataset": [{"table": "x"}]})
    with pytest.raises(ConfigError, match=r"apiVersion 'v2.2.0'"):
        parse_odcs({"apiVersion": "v2.2.0", "schema": []})
    with pytest.raises(ConfigError, match="renamed 'properties'"):
        parse_odcs({"apiVersion": "v3.0.2", "schema": [{"name": "t", "columns": [{"name": "a"}]}]})
    with pytest.raises(ConfigError, match="needs a 'name'"):
        parse_odcs({"schema": [{"properties": []}]})
    with pytest.raises(ConfigError, match="every property needs a 'name'"):
        parse_odcs({"schema": [{"name": "t", "properties": [{"logicalType": "string"}]}]})
    with pytest.raises(ConfigError, match="logicalTypeOptions"):
        parse_odcs(
            {"schema": [{"name": "t", "properties": [{"name": "a", "logicalTypeOptions": 3}]}]}
        )
    with pytest.raises(ConfigError, match="list of mappings"):
        parse_odcs({"schema": "nope"})
    with pytest.raises(ConfigError, match="top level"):
        parse_contract(["x"])
    loose = parse_odcs({"kind": "Other", "servers": {"prod": {"type": "duckdb"}}})
    assert (
        loose.notes
        and loose.servers == [{"server": "prod", "type": "duckdb"}]
        and not loose.objects
    )
    with pytest.raises(ConfigError, match="not found"):
        load_contract(write("x.txt", "").with_name("missing.yaml"))
    with pytest.raises(ConfigError, match="Cannot read"):
        load_contract(write("bad.yaml", "a: [unclosed"))
    assert (
        load_contract(write("c.json", '{"apiVersion": "v3.0.0", "schema": []}')).api_version
        == "v3.0.0"
    )


def test_expected_schema_files(write: WriteFile) -> None:
    contract = load_contract(
        write(
            "orders.schema.yaml",
            """
            dialect: postgres
            columns:
              - {name: order_id, type: BIGINT, nullable: false}
              - {name: status, logical_type: string}
            primary_key: order_id
            """,
        )
    )
    assert contract.format == "expected_schema" and contract.dialect == "postgres"
    obj = contract.object()
    assert obj.name == "orders.schema" and obj.primary_key() == ["order_id"]
    assert obj.properties[0].required and not obj.properties[1].required
    with pytest.raises(ConfigError, match="non-empty 'columns'"):
        parse_contract({"columns": []})
    with pytest.raises(ConfigError, match=r"columns\[0\] needs a 'name'"):
        parse_contract({"columns": [{"type": "INT"}]})


# ---------------------------------------------------------------------- conversion


def test_import_the_example_contract(tmp_path: Path, default_registry: Registry) -> None:
    out = tmp_path / "contract.yaml"
    result = import_contract(CONTRACT, server="local", output=out)
    assert result.objects == 2 and result.output == out
    assert api.validate_config(out).ok
    reasons = {u.item: u.reason for u in result.unsupported}
    assert "text rules" in reasons["id customer_names_reviewed"]
    assert "engine 'soda'" in reasons["id soda_freshness"]
    checks = _checks(out.read_text(encoding="utf-8"))
    customers = checks["customers"]
    assert customers[0] == {
        "type": "contract",
        "contract": "orders.odcs.yaml",
        "object": "customers",
    } or (customers[0]["type"] == "contract" and customers[0]["object"] == "customers")
    assert {"type": "not_null", "columns": ["customer_id", "email", "created_at"]} in customers
    known = next(c for c in customers if c.get("name") == "country_code_known")
    assert known["type"] == "allowed_values" and known["mostly"] == 0.99
    assert {"type": "range", "column": "created_at", "min": "2015-01-01T00:00:00"} in customers
    orders = checks["orders"]
    volume = next(c for c in orders if c.get("name") == "orders_daily_volume")
    assert (volume["min"], volume["max"]) == (1, 999999)
    fk = next(c for c in orders if c["type"] == "referential_integrity")
    assert fk["parent"] == {"table": "customers", "columns": ["customer_id"]}
    sql = next(c for c in orders if c.get("name") == "no_orders_before_customers")
    assert "{table}" in sql["sql"] and sql["severity"] == "warning"
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["connections"]["local"]["type"] == "duckdb"
    assert data["project"]["metadata"]["contract_version"] == "1.2.0"
    inclusive = import_contract(CONTRACT, server="local", between="inclusive")
    volume2 = next(
        c for c in _checks(inclusive.text)["orders"] if c.get("name") == "orders_daily_volume"
    )
    assert (volume2["min"], volume2["max"]) == (0, 1000000)


def test_server_connections(write: WriteFile, default_registry: Registry) -> None:
    analytics = yaml.safe_load(import_contract(CONTRACT, server="analytics").text)
    conn = analytics["connections"]["analytics"]
    assert conn["type"] == "postgres" and conn["host"] == "analytics.example.internal"
    assert conn["password"] == "${secret:env:ANALYTICS_PASSWORD}" and conn["schema"] == "shop"
    with pytest.raises(ConfigError, match="Server 'nope'"):
        import_contract(CONTRACT, server="nope")
    servers = {
        "snow": ({"type": "snowflake", "account": "acme", "database": "DB"}, "snowflake"),
        "bq": ({"type": "bigquery", "project": "p", "dataset": "d"}, "bigquery"),
        "dbx": ({"type": "databricks", "host": "h", "catalog": "c", "schema": "s"}, "databricks"),
        "files": (
            {"type": "s3", "location": "s3://bucket/{model}.parquet", "format": "parquet"},
            "files",
        ),
        "ora": ({"type": "oracle", "host": "h", "port": 1521, "serviceName": "XE"}, "oracle"),
    }
    for name, (server, expected) in servers.items():
        path = _contract(
            write,
            {"servers": [{"server": name, **server}], **_object([{"name": "a"}])},
            f"{name}.yaml",
        )
        data = yaml.safe_load(import_contract(path).text)
        assert data["connections"][name]["type"] == expected
    s3 = _contract(
        write,
        {
            "servers": [{"server": "lake", "type": "s3", "location": "s3://b/{model}.parquet"}],
            **_object([{"name": "a"}]),
        },
        "s3.yaml",
    )
    lake = yaml.safe_load(import_contract(s3).text)
    assert lake["suites"][0]["tables"][0]["source"] == {"path": "t.parquet"}
    assert lake["connections"]["lake"]["options"] == {"extensions": ["httpfs"]}
    kafka = _contract(
        write, {"servers": [{"server": "k", "type": "kafka"}], **_object([{"name": "a"}])}, "k.yaml"
    )
    result = import_contract(kafka)
    assert any("no connector" in u.reason for u in result.unsupported)
    assert "REVIEW: placeholder" in result.text
    none = import_contract(_contract(write, _object([{"name": "a"}]), "none.yaml"))
    assert "the contract lists no servers" in none.notes


def _convert(
    write: WriteFile,
    properties: list[dict[str, Any]],
    quality: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    path = _contract(
        write,
        {
            "servers": [{"server": "db", "type": "duckdb", "database": "x.duckdb"}],
            **_object(properties, quality),
        },
    )
    result = import_contract(path, conformance=False, **kwargs)
    return _checks(result.text)["t"], [f"{u.item}: {u.reason}" for u in result.unsupported]


def test_library_metrics(write: WriteFile, default_registry: Registry) -> None:
    checks, skipped = _convert(
        write,
        [
            {
                "name": "a",
                "quality": [
                    {"metric": "nullValues", "mustBeLessOrEqualTo": 5, "unit": "percent"},
                    {"metric": "nullValues", "mustBeLessThan": 3},
                    {
                        "metric": "missingValues",
                        "arguments": {"missingValues": [None, "", "N/A"]},
                        "mustBe": 0,
                    },
                    {
                        "metric": "invalidValues",
                        "arguments": {"pattern": "^[a-z]+$"},
                        "mustBeLessThan": 10,
                    },
                    {"metric": "invalidValues", "arguments": {"pattern": "^[a-z]+$"}},
                    {"metric": "duplicateValues", "mustBeLessThan": 1, "unit": "percent"},
                    {"rule": "validValues", "validValues": ["x", "y"]},
                    {"rule": "duplicateCount", "mustBe": 0},
                    {"metric": "invalidValues", "unit": "rows", "arguments": {}},
                ],
            },
        ],
        [
            {"metric": "rowCount", "mustBe": 10},
            {"metric": "rowCount", "mustNotBe": 0},
            {"metric": "rowCount", "mustNotBeBetween": [5, 9]},
            {"metric": "rowCount", "mustBeLessThan": 0},
            {"metric": "rowCount"},
            {"metric": "rowCount", "mustBe": 1, "unit": "percent"},
            {"metric": "sizeInBytes", "mustBe": 1},
            {"metric": "nullValues", "mustBe": 0},
            {"metric": "duplicateValues", "mustBe": 0},
            {"metric": "rowCount", "mustBe": 1, "unit": "bytes"},
            {"metric": "rowCount", "mustBeBetween": [1]},
            {"metric": "rowCount", "mustBe": "many"},
            {"type": "magic"},
            {"description": "no metric"},
        ],
    )
    by_type: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        by_type.setdefault(check["type"], []).append(check)
    assert by_type["null_ratio"][0]["max_ratio"] == 0.05
    scalars = by_type["custom_sql"]
    assert any(
        c["expect"] == {"operator": "<", "value": 3} and "IS NULL" in c["sql"] for c in scalars
    )
    assert any("'N/A'" in c["sql"] for c in scalars)
    assert any("REGEXP_FULL_MATCH" in c["sql"] and c["expect"]["value"] == 10 for c in scalars)
    assert any("dvf_dups" in c["sql"] and "100.0" in c["sql"] for c in scalars)
    assert any(c["expect"] == {"operator": "!=", "value": 0} for c in scalars)
    assert any(
        "CASE WHEN" in c["sql"] and c["expect"] == {"operator": "==", "value": 1} for c in scalars
    )
    assert {
        "type": "regex",
        "column": "a",
        "pattern": "^[a-z]+$",
        "name": "invalidValues_5",
    } in by_type["regex"]
    assert any(c["values"] == ["x", "y"] for c in by_type["allowed_values"])
    assert any(c["columns"] == ["a"] for c in by_type["unique"])
    assert any(c.get("min") == 10 and c.get("max") == 10 for c in by_type["row_count_range"])
    text = "\n".join(skipped)
    for expected in (
        "cannot be met",
        "needs a threshold",
        "cannot be a percentage",
        "unknown library metric 'sizeInBytes'",
        "applies to a property",
        "needs arguments.properties",
        "needs arguments.validValues or arguments.pattern",
        "unit 'bytes'",
        "list of two numbers",
        "needs a number",
        "unknown rule type 'magic'",
        "has no 'metric'",
    ):
        assert expected in text, expected


def test_sql_rules_and_property_options(write: WriteFile, default_registry: Registry) -> None:
    checks, skipped = _convert(
        write,
        [
            {
                "name": "code",
                "logicalType": "string",
                "enum": ["A", "B"],
                "logicalTypeOptions": {
                    "minLength": 1,
                    "maxLength": 3,
                    "format": "uuid",
                    "pattern": "^[A-Z]+$",
                    "foo": 1,
                },
            },
            {
                "name": "qty",
                "logicalType": "integer",
                "logicalTypeOptions": {"minimum": 0, "exclusiveMaximum": 100, "multipleOf": 5},
            },
            {
                "name": "price",
                "logicalType": "number",
                "logicalTypeOptions": {
                    "minimum": 1,
                    "maximum": 9,
                    "exclusiveMinimum": True,
                    "exclusiveMaximum": True,
                    "multipleOf": 0.5,
                },
            },
            {
                "name": "day",
                "logicalType": "date",
                "logicalTypeOptions": {"format": "yyyy-MM-dd", "minimum": "2020-01-01"},
            },
            {
                "name": "blob",
                "logicalType": "string",
                "logicalTypeOptions": {"format": "base64", "maxLength": 0},
            },
            {"name": "tags", "logicalType": "array", "items": {"logicalType": "string"}},
            {
                "name": "parent_id",
                "relationships": [{"type": "derivedFrom", "to": "p.id"}, {"to": "bad"}],
            },
            {
                "name": "x",
                "quality": [
                    {
                        "type": "sql",
                        "query": "SELECT MAX({property}) FROM {object}",
                        "mustBeBetween": [0, 10],
                    },
                    {"type": "sql", "query": "SELECT 1"},
                    {"type": "sql", "mustBe": 1},
                ],
            },
        ],
        [
            {
                "type": "sql",
                "query": "SELECT COUNT(*) FROM {model} WHERE {column} > 1",
                "mustBe": 0,
            },
            {
                "type": "sql",
                "query": "SELECT COUNT(*) FROM ${table};",
                "mustBeLessOrEqualTo": 5,
                "id": "Count rows!",
            },
        ],
    )
    names = {c.get("name"): c for c in checks}
    assert names["enum"]["values"] == ["A", "B"]
    assert names["format_uuid"]["type"] == "regex" and names["pattern"]["pattern"] == "^[A-Z]+$"
    assert {"type": "length", "column": "code", "min": 1, "max": 3} in checks
    assert names["range_min"]["inclusive"] is True and names["range_max"]["inclusive"] is False
    assert any(
        c["type"] == "range" and c.get("inclusive") is False and c.get("min") == 1 for c in checks
    )
    assert names["qty_multiple_of_5"]["expression"].endswith("% 5) = 0")
    assert {"type": "range", "column": "day", "min": "2020-01-01"} in checks
    between = [c for c in checks if str(c.get("name", "")).startswith("sql_1")]
    assert [c["expect"]["operator"] for c in between] == [">", "<"]
    assert '"x"' in between[0]["sql"] and "{table}" in between[0]["sql"]
    assert names["Count_rows"]["sql"] == "SELECT COUNT(*) FROM {table}"
    text = "\n".join(skipped)
    for expected in (
        "logicalTypeOptions.foo",
        "multipleOf 0.5",
        "format yyyy-MM-dd",
        "no regular expression is known for format 'base64'",
        "maxLength 0",
        "nested object and array properties",
        "relationship type 'derivedFrom'",
        "write 'to' as object.property",
        "needs an operator",
        "needs a 'query'",
        "{property} but the rule is not under a property",
    ):
        assert expected in text, expected


def test_relationships_and_duplicates(write: WriteFile, default_registry: Registry) -> None:
    path = _contract(
        write,
        {
            "schema": [
                {
                    "name": "parents",
                    "physicalName": "core.parents",
                    "properties": [{"name": "id", "primaryKey": True, "unique": True}],
                },
                {
                    "name": "kids",
                    "properties": [
                        {"name": "pid", "relationships": [{"to": "parents.id"}]},
                        {"name": "a", "unique": True, "quality": [{"metric": "duplicateValues"}]},
                    ],
                    "relationships": [
                        {"from": ["kids.pid", "kids.a"], "to": ["parents.id", "parents.x"]},
                        {"from": ["kids.pid"], "to": ["parents.id", "other.y"]},
                    ],
                },
            ]
        },
    )
    result = import_contract(path)
    checks = _checks(result.text)
    assert {"type": "unique", "columns": ["id"]} in checks["parents"]
    assert sum(1 for c in checks["parents"] if c["type"] == "unique") == 1
    fks = [c for c in checks["kids"] if c["type"] == "referential_integrity"]
    assert fks[0]["parent"]["table"] == "core.parents"
    assert any(c["columns"] == ["pid", "a"] for c in fks)
    assert any(
        "one object" in u.reason or "matching 'from'" in u.reason for u in result.unsupported
    )
    assert api.load_config_data(yaml.safe_load(result.text), strict=False, resolve_secrets=False)


# ---------------------------------------------------------------------- conformance logic


def _schema(columns: list[tuple[str, str, bool | None]], pk: tuple[str, ...] = ()) -> DatasetSchema:
    return DatasetSchema(
        ref=DatasetRef(connection="c", kind=DatasetKind.TABLE, name="t"),
        columns=tuple(
            ColumnSchema(
                name=n, native_type=t, type=parse_native_type(t, "duckdb"), nullable=nullable
            )
            for n, t, nullable in columns
        ),
        primary_key=pk,
    )


def test_conformance_problems(write: WriteFile) -> None:
    contract = parse_contract(
        {
            "apiVersion": "v3.1.0",
            "schema": [
                {
                    "name": "t",
                    "properties": [
                        {
                            "name": "id",
                            "physicalType": "BIGINT",
                            "primaryKey": True,
                            "required": True,
                        },
                        {"name": "name", "physicalType": "SMALLINT", "required": True},
                        {"name": "amount", "physicalType": "DECIMAL(10,2)"},
                        {"name": "flag", "logicalType": "boolean"},
                        {"name": "seen", "logicalType": "date"},
                        {"name": "gone", "logicalType": "string"},
                        {"name": "odd", "physicalType": "GEOGRAPHY_X"},
                    ],
                }
            ],
        }
    )
    obj = contract.object()
    schema = _schema(
        [
            ("ID", "INTEGER", False),
            ("name", "INTEGER", True),
            ("amount", "DECIMAL(10,1)", None),
            ("flag", "VARCHAR", None),
            ("seen", "TIMESTAMP", None),
            ("odd", "VARCHAR", None),
            ("extra", "INTEGER", None),
        ]
    )
    result = check_conformance(
        obj, schema, ContractCheck(type="contract", contract="c.yaml"), dataset_dialect="duckdb"
    )
    found = {(p.column, p.aspect): p for p in result.problems}
    assert found[("id", "type")].level == "error" and "lossy" in found[("id", "type")].note
    assert found[("name", "type")].level == "warning"
    assert found[("amount", "type")].level == "error"
    assert found[("flag", "type")].level == "error"
    assert ("seen", "type") not in found
    assert found[("gone", "missing")].level == "error"
    assert found[("extra", "extra")].level == "warning"
    assert found[("name", "nullability")].level == "warning"
    assert found[("id", "primary_key")].actual == "none declared"
    assert ("odd", "type") not in found
    relaxed = ContractCheck(
        type="contract",
        contract="c.yaml",
        extra_columns="ignore",
        types="ignore",
        nullability="ignore",
        primary_key="ignore",
    )
    quiet = check_conformance(obj, schema, relaxed, dataset_dialect="duckdb")
    assert {p.aspect for p in quiet.problems} == {"missing"}
    other_pk = check_conformance(
        obj,
        _schema([("id", "BIGINT", False)], ("name",)),
        relaxed.model_copy(update={"primary_key": "error"}),
        dataset_dialect="duckdb",
    )
    assert [p.aspect for p in other_pk.problems if p.level == "error"].count("primary_key") == 1
    files = check_conformance(
        obj,
        _schema([("id", "BIGINT", None), ("name", "SMALLINT", None)], ("id",)),
        relaxed.model_copy(update={"nullability": "error"}),
        dataset_dialect="duckdb",
    )
    assert files.unknown == ["nullability of name"]  # primary key columns are never NULL


def test_odcs_logical_types() -> None:
    expected = {
        "INTEGER": "integer",
        "DECIMAL(10,0)": "integer",
        "DECIMAL(30,0)": "number",
        "DECIMAL(10,2)": "number",
        "DOUBLE": "number",
        "VARCHAR": "string",
        "BOOLEAN": "boolean",
        "DATE": "date",
        "TIMESTAMP": "timestamp",
        "TIME": "time",
        "INTEGER[]": "array",
        "UUID": "string",
    }
    for native, logical in expected.items():
        assert odcs_logical_type(parse_native_type(native, "duckdb")) == logical, native
    assert odcs_logical_type(parse_native_type("NOT_A_TYPE", "duckdb")) is None


# ---------------------------------------------------------------------- export


def test_export_translates_checks_and_lists_the_rest(
    tmp_path: Path, write: WriteFile, default_registry: Registry
) -> None:
    import duckdb

    con = duckdb.connect(str(tmp_path / "src.duckdb"))
    con.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a VARCHAR(10) NOT NULL, b INTEGER, c INTEGER)"
    )
    con.close()
    con = duckdb.connect(str(tmp_path / "tgt.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER, a VARCHAR, b INTEGER, c INTEGER)")
    con.close()
    (tmp_path / "files").mkdir()
    config = write(
        "dvf.yaml",
        """
        version: 1
        project: {name: exp}
        connections:
          src: {type: duckdb, database: src.duckdb}
          tgt: {type: duckdb, database: tgt.duckdb}
          lake: {type: files, path: files, format: parquet}
          lite: {type: sqlite, database: x.sqlite}
          pg: {type: postgres, url: "postgresql://me:pw@db.example:5433/sales", schema: s}
        suites:
          - name: s
            source: src
            target: tgt
            tables:
              - name: t
                source: {table: t}
                target: {table: t}
                keys: [id]
                checks:
                  - {type: not_null, columns: [a], mostly: 0.9, severity: warning, description: mostly set}
                  - {type: unique, columns: [a, b], mostly: 0.9}
                  - {type: unique, columns: [b]}
                  - {type: regex, column: a, pattern: "x", full_match: false}
                  - {type: range, column: b, min: 1, inclusive: false}
                  - {type: range, column: c, max: 5, mostly: 0.5}
                  - {type: length, column: a, min: 2, max: 20}
                  - {type: custom_sql, name: rows, sql: "SELECT * FROM {table}", mostly: 0.5}
                  - {type: referential_integrity, columns: [b, c], parent: {table: p, columns: [x, y]}}
                  - {type: not_null, name: ghost, columns: [nope]}
                  - {type: not_null, name: src_only, columns: [a], side: source}
                  - {type: contract, contract: c.yaml}
        """,
    )
    result = api.export_contract(config)
    data = yaml.safe_load(result.text)
    obj = data["schema"][0]
    props = {p["name"]: p for p in obj["properties"]}
    assert {
        "metric": "nullValues",
        "mustBeLessOrEqualTo": 10.0,
        "unit": "percent",
        "severity": "warning",
        "description": "mostly set",
    } in props["a"]["quality"]
    assert props["b"]["unique"] is True
    assert props["b"]["logicalTypeOptions"] == {"minimum": 1, "exclusiveMinimum": True}
    assert props["a"]["logicalTypeOptions"] == {"minLength": 2, "maxLength": 20}
    assert obj["relationships"] == [{"from": ["t.b", "t.c"], "to": ["p.x", "p.y"]}]
    assert "primaryKey" not in props["id"]  # the target declares no key
    reasons = "\n".join(str(u) for u in result.unsupported)
    for expected in (
        "'mostly' on uniqueness",
        "matches substrings",
        "'mostly' on ranges",
        "'mostly' on failing-row",
        "column 'nope'",
        "contract checks refer",
    ):
        assert expected in reasons, expected
    assert "src_only" not in reasons
    source = yaml.safe_load(
        api.export_contract(config, side="source", tables=["t"], suite="s").text
    )
    src_props = {p["name"]: p for p in source["schema"][0]["properties"]}
    assert src_props["id"]["primaryKey"] is True and src_props["a"]["required"] is True
    from dvf.discovery.contract_export import _servers

    loaded = api.load_config(config)
    servers = {s["server"]: s for s in _servers(loaded, ["lake", "lite", "pg", "src"])}
    assert servers["lake"] == {
        "server": "lake",
        "type": "local",
        "path": "files",
        "format": "parquet",
    }
    assert "lite" not in servers
    assert servers["pg"] == {
        "server": "pg",
        "type": "postgresql",
        "host": "db.example",
        "port": 5433,
        "database": "sales",
        "schema": "s",
    }
    with pytest.raises(ConfigError, match="No tables matched"):
        api.export_contract(config, tables=["nope"])
