"""Schema checks SCH-07 to SCH-11 and SCH-14, and the connector catalog metadata behind them."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf import api
from dvf.checks.schema import snapshot_path
from dvf.config.models import NamingRule
from dvf.core.enums import Status
from dvf.models.results import CheckResult, RunResult
from dvf.packs.catalog.introspect import _semantics_from_name, split_table
from dvf.plugins.base import CollationInfo, ColumnCollation

ALL_CHECKS = [
    "object_exists",
    "physical_design",
    "column_order",
    "collation",
    "object_inventory",
    "schema_drift",
    "naming_standards",
]


def _make_pair(base: Path) -> None:
    con = duckdb.connect(str(base / "src.duckdb"))
    con.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, code VARCHAR COLLATE NOCASE, name VARCHAR, "
        "amount DECIMAL(10, 2))"
    )
    con.execute("CREATE INDEX ix_code ON orders (code)")
    con.execute("CREATE VIEW v_orders AS SELECT * FROM orders")
    con.execute("CREATE SEQUENCE seq_orders")
    con.execute("INSERT INTO orders VALUES (1, 'a', 'x', 1.5), (2, 'b', 'y', 2.5)")
    con.close()
    lite = sqlite3.connect(base / "tgt.sqlite")
    lite.execute(
        "CREATE TABLE Orders (Name TEXT, id INTEGER PRIMARY KEY, code TEXT, amount NUMERIC(10, 2))"
    )
    lite.execute("CREATE UNIQUE INDEX ux_name ON Orders (Name) WHERE Name IS NOT NULL")
    lite.execute("INSERT INTO Orders VALUES ('x', 1, 'a', 1.5), ('y', 2, 'b', 2.5)")
    lite.execute("CREATE TRIGGER trg_orders AFTER INSERT ON Orders BEGIN SELECT 1; END")
    lite.commit()
    lite.close()


def _config(checks: list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "schema"},
        "connections": {
            "src": {"type": "duckdb", "database": "src.duckdb"},
            "tgt": {"type": "sqlite", "database": "tgt.sqlite"},
        },
        "defaults": {"schema_checks": checks, "schema_options": options or {}},
        "suites": [
            {
                "name": "s",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "orders",
                        "source": {"table": "orders"},
                        "target": {"table": "Orders"},
                        "keys": ["id"],
                        "comparison": {"enabled": False},
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }


def _run(config: dict[str, Any], base: Path, only: tuple[str, ...] = ()) -> dict[str, CheckResult]:
    loaded = api.load_config_data(config, base_dir=base, strict=False)
    result: RunResult = api.run(loaded, write_reports=False, only=only).result
    return {c.type.removeprefix("schema."): c for c in result.iter_checks()}


@pytest.fixture
def pair(tmp_path: Path) -> Path:
    _make_pair(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------- helpers


def test_split_table_follows_catalog_case() -> None:
    assert split_table("Sales.Orders", "postgres").schema == "sales"
    assert split_table('"Sales"."Orders"', "postgres").name == "Orders"
    assert split_table("sales.orders", "oracle").name == "ORDERS"
    assert split_table("[dbo].[Orders]", "mssql").schema == "dbo"
    assert split_table("orders", "duckdb").schema is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SQL_Latin1_General_CP1_CI_AS", (False, True)),
        ("Latin1_General_100_CS_AS_SC_UTF8", (True, True)),
        ("Latin1_General_BIN2", (True, True)),
        ("utf8mb4_0900_ai_ci", (False, False)),
        ("utf8mb4_bin", (True, True)),
        ("Latin1_General_CI_AI", (False, False)),
        (None, (None, None)),
    ],
)
def test_collation_semantics_from_names(name: str | None, expected: tuple[Any, Any]) -> None:
    assert _semantics_from_name(name) == expected


def test_collation_info_falls_back_to_default() -> None:
    info = CollationInfo(
        default_collation="C",
        charset="UTF8",
        columns={"Code": ColumnCollation("ci", case_sensitive=False)},
        case_sensitive_default=True,
    )
    assert info.for_column("code").case_sensitive is False
    assert info.for_column("name") == ColumnCollation("C", "UTF8", True, None, None)


def test_naming_rule_validation() -> None:
    assert NamingRule(preset="snake_case").regex is not None
    assert NamingRule(max_length=30).label == "max 30 characters"
    with pytest.raises(ValueError, match="invalid regular expression"):
        NamingRule(pattern="[")
    with pytest.raises(ValueError, match="set one of"):
        NamingRule()


# ---------------------------------------------------------------------- DuckDB vs SQLite


def test_catalog_checks_duckdb_vs_sqlite(pair: Path) -> None:
    options = {
        "naming": {
            "rules": [
                {"preset": "snake_case", "exclude": ["Name"]},
                {"max_length": 4, "applies_to": "columns"},
            ]
        }
    }
    checks = _run(_config(ALL_CHECKS, options), pair)

    physical = checks["physical_design"]
    assert physical.status is Status.WARNING  # informational: capped at warning
    rows = {tuple(r[:1] + r[3:]) for r in physical.samples.rows}  # type: ignore[union-attr]
    assert ("index", "missing_in_target") in rows  # ix_code is not unique in the target...
    assert ("index", "matched") in rows  # ...but the primary key is
    assert ("index", "extra_in_target") in rows  # the target's partial unique index
    assert physical.details["target"]["indexes"]

    order = checks["column_order"]
    assert order.status is Status.FAILED
    assert order.samples is not None and order.samples.rows[0] == [1, "id", "Name"]

    collation = checks["collation"]
    assert collation.status is Status.FAILED
    assert collation.columns == ["code"]
    assert "case-insensitive in the source" in str(collation.samples.rows[0][-1])  # type: ignore[union-attr]

    inventory = checks["object_inventory"]
    assert inventory.status is Status.WARNING
    by_type = {r[0]: r for r in inventory.samples.rows}  # type: ignore[union-attr]
    assert by_type["view"][3] == "v_orders"
    assert "the target system has no sequences" in by_type["sequence"][3]
    assert by_type["trigger"][4] == "trg_orders"

    naming = checks["naming_standards"]
    assert naming.status is Status.FAILED
    violations = {(r[1], r[2], r[3]) for r in naming.samples.rows}  # type: ignore[union-attr]
    assert ("table", "Orders", "snake_case") in violations
    assert ("column", "amount", "max 4 characters") in violations
    assert not any(name == "Name" and rule == "snake_case" for _, name, rule in violations)

    drift = checks["schema_drift"]
    assert drift.status is Status.SKIPPED
    assert "saved as the baseline" in (drift.message or "")


def test_schema_drift_against_previous_run(pair: Path) -> None:
    config = _config(["schema_drift"])
    assert _run(config, pair)["schema_drift"].status is Status.SKIPPED
    path = snapshot_path(pair / ".dvf", "s", "orders")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["format"] == "dvf-schema-snapshot"
    assert [c["name"] for c in snapshot["target"]["columns"]] == ["Name", "id", "code", "amount"]
    assert _run(config, pair)["schema_drift"].status is Status.PASSED

    lite = sqlite3.connect(pair / "tgt.sqlite")
    lite.execute("ALTER TABLE Orders ADD COLUMN note TEXT")
    lite.commit()
    lite.close()
    con = duckdb.connect(str(pair / "src.duckdb"))
    con.execute("DROP VIEW v_orders")
    con.execute("DROP INDEX ix_code")
    con.execute("ALTER TABLE orders ALTER COLUMN amount SET DATA TYPE DECIMAL(12, 3)")
    con.close()

    # A run that does not select the drift check leaves the baseline alone.
    _run(config, pair, only=("check:schema.object_exists",))
    drift = _run(config, pair)["schema_drift"]
    assert drift.status is Status.FAILED
    changes = {(r[0], r[1], r[2]) for r in drift.samples.rows}  # type: ignore[union-attr]
    assert ("target", "column_added", "note") in changes
    assert ("source", "type_changed", "amount") in changes
    # The changed schema is the baseline of the next run.
    assert _run(config, pair)["schema_drift"].status is Status.PASSED


def test_schema_drift_against_committed_file(pair: Path) -> None:
    baseline = pair / "baseline.yaml"
    baseline.write_text(
        "tables:\n"
        "  orders:\n"
        "    target:\n"
        "      columns:\n"
        "        - {name: id, native_type: INTEGER}\n"
        "        - {name: code, native_type: TEXT}\n"
        "        - {name: legacy_flag, native_type: TEXT}\n"
        "      primary_key: [id]\n",
        encoding="utf-8",
    )
    options = {
        "drift": {"baseline": "baseline.yaml", "side": "target", "ignore_columns": ["amount"]}
    }
    drift = _run(_config(["schema_drift"], options), pair)["schema_drift"]
    assert drift.status is Status.FAILED
    changes = {(r[1], r[2]) for r in drift.samples.rows}  # type: ignore[union-attr]
    assert changes == {("column_added", "Name"), ("column_removed", "legacy_flag")}
    assert not (pair / ".dvf" / "schemas").exists()  # a committed baseline is never written

    missing = _run(_config(["schema_drift"], {"drift": {"baseline": "nope.json"}}), pair)
    assert missing["schema_drift"].status is Status.ERROR


def test_checks_without_metadata_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("id,name\n1,x\n", encoding="utf-8")
    config = {
        "version": 1,
        "project": {"name": "files"},
        "connections": {"f": {"type": "files", "path": tmp_path.as_posix()}},
        "defaults": {
            "schema_checks": [
                "physical_design",
                "collation",
                "object_inventory",
                "naming_standards",
                "contract_conformance",
                "nested_schema",
            ]
        },
        "suites": [
            {
                "name": "s",
                "source": "f",
                "target": "f",
                "tables": [
                    {
                        "name": "a",
                        "source": {"path": "a.csv"},
                        "target": {"path": "a.csv"},
                        "keys": ["id"],
                        "comparison": {"enabled": False},
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }
    checks = _run(config, tmp_path)
    for name in ("physical_design", "collation", "object_inventory", "naming_standards"):
        assert checks[name].status is Status.SKIPPED, name
    assert "not available" in (checks["physical_design"].message or "")
    assert "add {type: contract}" in (checks["contract_conformance"].message or "")
    assert "document_schema" in (checks["nested_schema"].message or "")


def test_column_order_passes_when_only_extras_differ(pair: Path) -> None:
    lite = sqlite3.connect(pair / "tgt2.sqlite")
    lite.execute(
        "CREATE TABLE orders (id INTEGER, extra TEXT, code TEXT, name TEXT, amount NUMERIC)"
    )
    lite.commit()
    lite.close()
    config = _config(["column_order"])
    config["connections"]["tgt"]["database"] = "tgt2.sqlite"
    assert _run(config, pair)["column_order"].status is Status.PASSED


# ---------------------------------------------------------------------- PostgreSQL (live)

_PG_SETUP = """
DROP SCHEMA IF EXISTS p4_cat_src CASCADE;
DROP SCHEMA IF EXISTS p4_cat_tgt CASCADE;
CREATE SCHEMA p4_cat_src;
CREATE SCHEMA p4_cat_tgt;
CREATE TABLE p4_cat_src.orders (id INTEGER PRIMARY KEY, code VARCHAR(10), region TEXT,
  posted DATE NOT NULL);
CREATE INDEX ix_orders_code ON p4_cat_src.orders (code);
CREATE UNIQUE INDEX ux_orders_region ON p4_cat_src.orders (region, posted) WHERE region IS NOT NULL;
CLUSTER p4_cat_src.orders USING ix_orders_code;
CREATE VIEW p4_cat_src.v_orders AS SELECT * FROM p4_cat_src.orders;
CREATE FUNCTION p4_cat_src.f_total() RETURNS INTEGER LANGUAGE sql AS 'SELECT 1';
CREATE PROCEDURE p4_cat_src.p_load() LANGUAGE sql AS 'SELECT 1';
CREATE SEQUENCE p4_cat_src.seq_batch;
CREATE FUNCTION p4_cat_src.trg_fn() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';
CREATE TRIGGER trg_orders BEFORE INSERT ON p4_cat_src.orders FOR EACH ROW
  EXECUTE FUNCTION p4_cat_src.trg_fn();
CREATE TABLE p4_cat_tgt.orders (id INTEGER, code VARCHAR(10) COLLATE "C", region TEXT,
  posted DATE NOT NULL, PRIMARY KEY (id, posted)) PARTITION BY RANGE (posted);
CREATE TABLE p4_cat_tgt.orders_2024 PARTITION OF p4_cat_tgt.orders
  FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
CREATE VIEW p4_cat_tgt.v_orders AS SELECT * FROM p4_cat_tgt.orders;
CREATE SEQUENCE p4_cat_tgt.orders_id_seq;
"""


@pytest.fixture
def pg_schemas(postgres_url: str) -> Iterator[str]:
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as con:
        con.execute(_PG_SETUP)
        icu = True
        try:
            con.execute(
                "CREATE COLLATION p4_cat_tgt.ci (provider = icu, locale = 'und-u-ks-level2', "
                "deterministic = false)"
            )
            con.execute(
                "ALTER TABLE p4_cat_tgt.orders ALTER COLUMN region TYPE TEXT COLLATE p4_cat_tgt.ci"
            )
        except psycopg.Error:
            icu = False
    try:
        yield "icu" if icu else "no-icu"
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as con:
            con.execute("DROP SCHEMA IF EXISTS p4_cat_src CASCADE")
            con.execute("DROP SCHEMA IF EXISTS p4_cat_tgt CASCADE")


def _pg_config(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "pg-schema"},
        "connections": {"src": source, "tgt": target},
        "defaults": {
            "schema_checks": ["physical_design", "collation", "object_inventory", "column_order"]
        },
        "suites": [
            {
                "name": "s",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "orders",
                        "source": {"table": "p4_cat_src.orders"},
                        "target": {"table": "p4_cat_tgt.orders"},
                        "keys": ["id"],
                        "comparison": {"enabled": False},
                    }
                ],
            }
        ],
        "history": {"enabled": False},
    }


def _assert_pg_catalog(checks: dict[str, CheckResult], icu: bool) -> None:
    physical = checks["physical_design"]
    assert physical.status is Status.WARNING, physical.message
    source = physical.details["source"]
    target = physical.details["target"]
    assert any(i.startswith("ix_orders_code (code)") for i in source["indexes"])
    assert any("where (region IS NOT NULL)" in i for i in source["indexes"])
    assert source["clustering"] == ["code"]
    assert source["partitioning"] is None
    assert target["partitioning"] == "RANGE (posted) (1 partitions)"
    assert "partitioning differs" in (physical.message or "")
    assert "3 source index(es) have no target index" in (physical.message or "")

    inventory = checks["object_inventory"]
    assert inventory.status is Status.WARNING
    by_type = {r[0]: r for r in inventory.samples.rows}  # type: ignore[union-attr]
    assert by_type["view"][1:3] == [1, 1]
    assert by_type["procedure"][3] == "p_load"
    assert set(by_type["function"][3].split(", ")) == {"f_total", "trg_fn"}
    assert by_type["trigger"][3] == "trg_orders"
    assert by_type["sequence"][3] == "seq_batch"
    assert by_type["sequence"][4] == "orders_id_seq"
    assert inventory.details["source"]["schema"] == "p4_cat_src"

    collation = checks["collation"]
    if icu:
        assert collation.status is Status.FAILED
        assert collation.columns == ["region"]
    else:
        assert collation.status is Status.PASSED
    renamed = [r for r in (collation.samples.rows if collation.samples else []) if r[0] == "code"]
    assert renamed and renamed[0][2] == "C"
    assert checks["column_order"].status is Status.PASSED


@pytest.mark.integration
def test_catalog_checks_postgres(pg_schemas: str, postgres_url: str, tmp_path: Path) -> None:
    config = _pg_config(
        {"type": "postgres", "url": postgres_url}, {"type": "postgres", "url": postgres_url}
    )
    _assert_pg_catalog(_run(config, tmp_path), pg_schemas == "icu")


@pytest.mark.integration
def test_catalog_checks_postgres_through_generic_connectors(
    pg_schemas: str, postgres_url: str, tmp_path: Path
) -> None:
    from tests.unit.test_catalog_generic import odbc_connection_string, sqlalchemy_postgres_url

    source = {"type": "sqlalchemy", "url": sqlalchemy_postgres_url(postgres_url)}
    target = {"type": "odbc", "connection_string": odbc_connection_string(postgres_url)}
    _assert_pg_catalog(_run(_pg_config(source, target), tmp_path), pg_schemas == "icu")


# ---------------------------------------------------------------------- MySQL, SQL Server, Oracle

_SERVER_DDL: dict[str, list[str]] = {
    "mysql": [
        "CREATE TABLE p4_cat_orders (id INT PRIMARY KEY, code VARCHAR(10) COLLATE utf8mb4_bin, "
        "name VARCHAR(20) CHARACTER SET latin1 COLLATE latin1_general_ci, region VARCHAR(5)) "
        "PARTITION BY HASH (id) PARTITIONS 2",
        "CREATE INDEX ix_p4_cat_code ON p4_cat_orders (code, region)",
        "CREATE VIEW p4_cat_v AS SELECT * FROM p4_cat_orders",
        "CREATE PROCEDURE p4_cat_p() SELECT 1",
        "CREATE TRIGGER p4_cat_trg BEFORE INSERT ON p4_cat_orders FOR EACH ROW SET NEW.region = 'x'",
    ],
    "mssql": [
        "CREATE TABLE p4_cat_orders (id INT NOT NULL, code VARCHAR(10) COLLATE Latin1_General_CS_AS, "
        "name NVARCHAR(20), region VARCHAR(5) COLLATE Latin1_General_100_CI_AI_SC_UTF8, "
        "CONSTRAINT pk_p4_cat PRIMARY KEY NONCLUSTERED (id))",
        "CREATE CLUSTERED INDEX cx_p4_cat_code ON p4_cat_orders (code)",
        "CREATE UNIQUE INDEX ux_p4_cat_region ON p4_cat_orders (region) WHERE region IS NOT NULL",
        "CREATE VIEW p4_cat_v AS SELECT * FROM p4_cat_orders",
        "CREATE SEQUENCE p4_cat_seq",
        "CREATE SYNONYM p4_cat_syn FOR p4_cat_orders",
    ],
    "oracle": [
        "CREATE TABLE p4_cat_orders (id NUMBER(10) PRIMARY KEY, code VARCHAR2(10), "
        "name NVARCHAR2(20), region VARCHAR2(5)) "
        "PARTITION BY RANGE (id) (PARTITION p1 VALUES LESS THAN (100), "
        "PARTITION p2 VALUES LESS THAN (MAXVALUE))",
        "CREATE INDEX ix_p4_cat_code ON p4_cat_orders (code, region)",
        "CREATE VIEW p4_cat_v AS SELECT * FROM p4_cat_orders",
        "CREATE SEQUENCE p4_cat_seq",
        "CREATE SYNONYM p4_cat_syn FOR p4_cat_orders",
        "CREATE OR REPLACE PROCEDURE p4_cat_p AS BEGIN NULL; END;",
    ],
}
_SERVER_DROP: dict[str, list[str]] = {
    "mysql": [
        "DROP VIEW IF EXISTS p4_cat_v",
        "DROP PROCEDURE IF EXISTS p4_cat_p",
        "DROP TABLE IF EXISTS p4_cat_orders",
    ],
    "mssql": [
        "DROP SYNONYM IF EXISTS p4_cat_syn",
        "DROP SEQUENCE IF EXISTS p4_cat_seq",
        "DROP VIEW IF EXISTS p4_cat_v",
        "DROP TABLE IF EXISTS p4_cat_orders",
    ],
    "oracle": [
        "DROP SYNONYM p4_cat_syn",
        "DROP SEQUENCE p4_cat_seq",
        "DROP VIEW p4_cat_v",
        "DROP PROCEDURE p4_cat_p",
        "DROP TABLE p4_cat_orders PURGE",
    ],
}


@pytest.mark.integration
@pytest.mark.parametrize("server", ["mysql", "mssql", "oracle"])
def test_catalog_metadata_on_servers(server: str) -> None:
    """Catalog metadata on live MySQL, SQL Server, and Oracle (DVF_TEST_*_URL variables)."""
    import contextlib

    from dvf.config.models import DatasetSpec
    from tests.server_backends import make_backend

    backend = make_backend(server)
    try:
        for statement in _SERVER_DROP[server]:
            with contextlib.suppress(Exception):
                backend.execute(statement)
        for statement in _SERVER_DDL[server]:
            backend.execute(statement)
        conn = backend.connector
        relation = conn.resolve(DatasetSpec(table="p4_cat_orders"))

        design = conn.get_physical_design(relation)
        assert design is not None
        by_name = {i.name.lower(): i for i in design.indexes}
        primary = next(i for i in design.indexes if i.primary)
        assert [c.lower() for c in primary.columns] == ["id"]
        if server == "mssql":
            assert [c.lower() for c in by_name["cx_p4_cat_code"].columns] == ["code"]
            assert [c.lower() for c in design.clustering] == ["code"]
            assert by_name["ux_p4_cat_region"].unique
            assert "region" in str(by_name["ux_p4_cat_region"].predicate)
        else:
            assert [c.lower() for c in by_name["ix_p4_cat_code"].columns] == ["code", "region"]
            assert design.partitioning is not None
            assert ("HASH" if server == "mysql" else "RANGE") in design.partitioning
            assert "2 partitions" in design.partitioning

        info = conn.get_collations(relation)
        assert info is not None
        code, name, region = (info.for_column(c) for c in ("code", "name", "region"))
        if server == "mysql":
            assert (code.case_sensitive, code.collation) == (True, "utf8mb4_bin")
            assert (name.case_sensitive, name.unicode) == (False, False)
            assert region.unicode is True
        elif server == "mssql":
            assert (code.case_sensitive, code.accent_sensitive, code.unicode) == (True, True, False)
            assert (name.charset, name.unicode) == ("UTF-16", True)
            assert (region.case_sensitive, region.accent_sensitive, region.unicode) == (
                False,
                False,
                True,
            )
        else:
            # Column collations need MAX_STRING_SIZE=EXTENDED (Oracle Free has STANDARD).
            assert code.case_sensitive is True and region.case_sensitive is True
            assert code.unicode is True and code.charset == "AL32UTF8"
            assert name.unicode is True and name.charset == "AL16UTF16"

        inventory = conn.get_object_inventory(relation)
        assert inventory is not None
        lowered = {k: {n.lower() for n in v} for k, v in inventory.objects.items()}
        assert "p4_cat_v" in lowered["view"]
        if server == "mysql":
            assert "p4_cat_p" in lowered["procedure"]
            assert "p4_cat_trg" in lowered["trigger"]
            assert "sequence" in inventory.unsupported
        else:
            assert "p4_cat_seq" in lowered["sequence"]
            assert "p4_cat_syn" in lowered["synonym"]
    finally:
        for statement in _SERVER_DROP[server]:
            with contextlib.suppress(Exception):
                backend.execute(statement)
        backend.close()
