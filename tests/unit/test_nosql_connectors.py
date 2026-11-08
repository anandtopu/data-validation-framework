"""NoSQL pack: the document connector base, end-to-end reconciliation of embedded arrays against
child tables (``nested`` strategy), schema inference, and the ``document_schema`` check.

A fake document store stands in for MongoDB here; tests/unit/test_nosql_mongodb.py and
tests/e2e/test_mongodb_to_postgres.py run the same logic against a live MongoDB.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import pytest
from pydantic import Field

from dvf import api
from dvf.config.connections import ConnectionConfig
from dvf.config.models import DatasetSpec
from dvf.core.errors import ConnectorError
from dvf.core.types import LogicalType
from dvf.packs.nosql.base import DocumentConnector, Sampling
from dvf.packs.nosql.inference import infer_document_schema, infer_from_documents
from dvf.packs.nosql.options import DocumentOptions
from dvf.packs.nosql.schema_check import DocumentSchemaCheck, allowed, evaluate_report
from dvf.plugins.registry import PluginKind, Registry, new_registry

# ====================================================================== a fake document store


class FakeDocsConfig(ConnectionConfig):
    collections: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class FakeDocsOptions(DocumentOptions):
    match: dict[str, Any] = Field(default_factory=dict)
    """Equality filter applied "natively"."""


class FakeDocs(DocumentConnector):
    """An in-memory document store with a native equality filter."""

    Config: ClassVar[type[ConnectionConfig]] = FakeDocsConfig
    options_model = FakeDocsOptions
    system = "fake store"
    default_keep = ("_id",)
    default_batch_size = 2
    reads: ClassVar[list[tuple[str, int | None, str | None]]] = []

    def iter_documents(
        self,
        spec: DatasetSpec,
        options: DocumentOptions,
        *,
        limit: int | None = None,
        sampling: Sampling | None = None,
    ) -> Iterator[list[Mapping[str, Any]]]:
        assert isinstance(options, FakeDocsOptions) and isinstance(self.config, FakeDocsConfig)
        name = self.collection(spec)
        self.reads.append((name, limit, sampling))
        docs = [
            d
            for d in self.config.collections.get(name, [])
            if all(d.get(k) == v for k, v in options.match.items())
        ][: limit or None]
        size = self.batch_size(options)
        for start in range(0, len(docs), size):
            yield copy.deepcopy(docs[start : start + size])

    def dataset_exists(self, spec: DatasetSpec) -> bool | None:
        assert isinstance(self.config, FakeDocsConfig)
        return self.collection(spec) in self.config.collections

    def default_primary_key(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        return ("_id",)


@pytest.fixture
def fake_registry() -> Registry:
    registry = new_registry()
    registry.register(PluginKind.CONNECTOR, "fake_docs", FakeDocs, origin="local")
    return registry


# ====================================================================== synthetic orders

PG_LINES = {
    1001: [(1, "A", 2, "9.99"), (2, "B", 1, "5.00")],
    1002: [(1, "C", 1, "3.50"), (2, "D", 4, "1.25"), (3, "E", 2, "2.00")],
    1003: [(1, "F", 3, "7.00"), (2, "G", 5, "1.10")],
    1004: [(1, "H", 1, "20.00"), (2, "I", 2, "4.40")],
    1005: [(1, "K", 1, "9.00")],
    1006: [(1, "L", 2, "3.00"), (2, "M", 2, "4.00")],
}
STATUS = {
    1001: "shipped",
    1002: "shipped",
    1003: "open",
    1004: "open",
    1005: "shipped",
    1006: "open",
}


def _total(lines: list[tuple[int, str, int, str]]) -> Decimal:
    return sum((Decimal(p) * q for _, _, q, p in lines), Decimal("0.00"))


def _documents() -> list[dict[str, Any]]:
    """MongoDB-style orders with seeded defects against the relational rows."""
    docs = []
    for order_id, lines in PG_LINES.items():
        embedded = [
            {"line_no": n, "sku": sku, "qty": q, "price": Decimal(p)} for n, sku, q, p in lines
        ]
        docs.append(
            {
                "_id": f"oid-{order_id}",
                "order_id": order_id,
                "customer": {"name": f"Customer {order_id}"},
                "order_date": datetime(2024, 3, order_id - 1000, tzinfo=UTC),
                "status": STATUS[order_id],
                "total": _total(lines),
                "lines": embedded,
            }
        )
    by_id = {d["order_id"]: d for d in docs}
    del by_id[1002]["lines"][2]  # a missing line
    by_id[1003]["lines"][1]["qty"] = 6  # a changed quantity
    by_id[1004]["lines"].append({"line_no": 3, "sku": "J", "qty": 1, "price": Decimal("1.00")})
    by_id[1005]["status"] = "pending"  # a changed header field
    first, second = by_id[1006]["lines"]
    first["sku"], second["sku"] = second["sku"], first["sku"]  # same count and totals
    by_id[1001]["total"] = float(by_id[1001]["total"])  # a type quirk: double instead of decimal
    return docs


def _relational(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_name VARCHAR, "
        "order_date DATE, status VARCHAR, total DECIMAL(12, 2))"
    )
    con.execute(
        "CREATE TABLE order_lines (order_id INTEGER, line_no INTEGER, sku VARCHAR, "
        "qty INTEGER, price DECIMAL(10, 2), PRIMARY KEY (order_id, line_no))"
    )
    for order_id, lines in PG_LINES.items():
        con.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
            [
                order_id,
                f"Customer {order_id}",
                date(2024, 3, order_id - 1000),
                STATUS[order_id],
                _total(lines),
            ],
        )
        for n, sku, q, p in lines:
            con.execute(
                "INSERT INTO order_lines VALUES (?, ?, ?, ?, ?)", [order_id, n, sku, q, Decimal(p)]
            )
    con.close()


def _config(db: Path, **lines_table: Any) -> dict[str, Any]:
    order_lines: dict[str, Any] = {
        "name": "order_lines",
        "source": {"table": "order_lines"},
        "target": {
            "table": "orders",
            "options": {"unwind": {"path": "lines", "keep": ["order_id"], "index": "line_index"}},
        },
        "keys": ["order_id", "line_no"],
        "columns": {"exclude": ["line_index"]},
        "comparison": {"strategies": ["nested"], "group_by": ["order_id"]},
    }
    order_lines.update(lines_table)
    return {
        "version": 1,
        "project": {"name": "nosql"},
        "defaults": {"engine": "duckdb"},
        "connections": {
            "rel": {"type": "duckdb", "database": str(db)},
            "docs": {"type": "fake_docs", "collections": {"orders": _documents()}},
        },
        "suites": [
            {
                "name": "orders",
                "source": "rel",
                "target": "docs",
                "tables": [
                    {
                        "name": "orders",
                        "source": {"table": "orders"},
                        "target": {
                            "table": "orders",
                            "options": {
                                "fields": {
                                    "order_id": "order_id",
                                    "customer_name": "customer.name",
                                    "order_date": "order_date",
                                    "status": "status",
                                    "total": "total",
                                }
                            },
                        },
                        "keys": ["order_id"],
                        "comparison": {"strategies": ["row_count", "full_diff"]},
                        "checks": [
                            {
                                "type": "document_schema",
                                "side": "target",
                                "fields": {
                                    "order_id": "integer",
                                    "customer.name": "string",
                                    "total": {"type": "decimal"},
                                    "lines[].qty": "integer",
                                    "lines[].price": ["decimal"],
                                },
                            }
                        ],
                    },
                    order_lines,
                ],
            }
        ],
    }


def _checks(out: Any, table: str) -> dict[str, Any]:
    dataset = next(d for d in out.result.suites[0].datasets if d.name == table)
    return {c.type: c for c in dataset.checks}


def test_embedded_arrays_reconcile_against_child_tables(
    tmp_path: Path, fake_registry: Registry
) -> None:
    db = tmp_path / "rel.duckdb"
    _relational(db)
    loaded = api.load_config_data(_config(db), base_dir=tmp_path, registry=fake_registry)
    out = api.run(loaded, write_reports=False, history=False, registry=fake_registry)

    lines = _checks(out, "order_lines")
    nested = lines["compare.nested"]
    assert nested.status.value == "failed"
    assert nested.unexpected_count == 4 and nested.total_count == 6
    assert nested.details["missing_in_target"] == 0
    assert nested.details["child_rows_differ"] == 2
    assert nested.details["totals_differ"] == 1
    assert nested.details["content_differs"] == 1
    assert nested.details["summed_columns"] == ["qty", "price"]
    assert nested.samples is not None
    by_key = {r[0]: r for r in nested.samples.rows}
    assert sorted(by_key) == ["1002", "1003", "1004", "1006"]
    assert by_key["1002"][1:4] == ["child rows differ", 3, 2]
    assert "sum(qty) 7 vs 5; sum(price) 6.75 vs 4.75" in by_key["1002"][4]
    assert by_key["1003"][1] == "totals differ" and "sum(qty) 8 vs 9" in by_key["1003"][4]
    assert by_key["1004"][1:4] == ["child rows differ", 2, 3]
    assert by_key["1006"][1] == "content differs"
    assert "same count and totals" in by_key["1006"][4]
    assert "order_id=1002: child rows 3 vs 2" in (nested.message or "")
    metrics = {(m.metric, m.column, m.group["order_id"]) for m in nested.metrics}
    assert ("child_rows", None, "1002") in metrics and ("sum", "qty", "1003") in metrics
    assert ("child_content", None, "1006") in metrics
    assert len(nested.queries) == 2

    # on_mismatch: full_diff (the default) names the exact child rows and columns.
    keys = lines["compare.keys"]
    assert keys.observed["missing_in_target"] == 1 and keys.observed["extra_in_target"] == 1
    mismatch = lines["compare.column_mismatch"]
    assert {m.column: m.mismatch_count for m in mismatch.column_mismatches} == {"qty": 1, "sku": 2}

    orders = _checks(out, "orders")
    assert orders["compare.row_count"].status.value == "passed"
    header = orders["compare.column_mismatch"]
    assert [(m.column, m.mismatch_count) for m in header.column_mismatches] == [("status", 1)]
    schema = orders["document_schema"]
    assert schema.status.value == "failed"
    assert schema.samples is not None
    assert schema.samples.rows == [
        ["total", "unexpected type", "double in 1 of 6 values", "decimal"]
    ]


def test_nested_passes_and_explains_configuration_errors(
    tmp_path: Path, fake_registry: Registry
) -> None:
    db = tmp_path / "rel.duckdb"
    _relational(db)
    config = _config(db, source={"table": "order_lines", "filter": "order_id = 1001"})
    config["suites"][0]["tables"][1]["target"]["filter"] = "order_id = 1001"
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=fake_registry)
    out = api.run(
        loaded,
        only=["orders.order_lines"],
        write_reports=False,
        history=False,
        registry=fake_registry,
    )
    nested = _checks(out, "order_lines")["compare.nested"]
    assert nested.status.value == "passed"
    assert nested.message == (
        "all 1 parent keys match: 2 child rows on each side, compared by count, 2 total(s), "
        "and a hash of 4 column(s)"
    )

    config = _config(db, comparison={"strategies": ["nested"]})
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=fake_registry)
    out = api.run(
        loaded,
        only=["orders.order_lines"],
        write_reports=False,
        history=False,
        registry=fake_registry,
    )
    error = _checks(out, "order_lines")["compare.nested"]
    assert error.status.value == "error"
    assert "needs comparison.group_by" in (error.message or "")


def test_nested_float_tolerance_null_parents_and_strict_privacy(
    tmp_path: Path, fake_registry: Registry
) -> None:
    db = tmp_path / "rel.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE a (parent VARCHAR, w DOUBLE, label VARCHAR)")
    con.execute("CREATE TABLE b (parent VARCHAR, w DOUBLE, label VARCHAR)")
    con.execute(
        "INSERT INTO a VALUES ('p1', 0.1, 'x'), ('p1', 0.2, 'y'), (NULL, 1.0, 'z'), ('p2', 1, 'q')"
    )
    con.execute(
        "INSERT INTO b VALUES ('p1', 0.3000000001, 'x'), ('p1', 0.0, 'y'), (NULL, 1.0, 'z'), ('p2', 5, 'q')"
    )
    con.close()
    config = {
        "version": 1,
        "project": {"name": "n"},
        "defaults": {"privacy": {"mode": "strict"}},
        "connections": {"d": {"type": "duckdb", "database": str(db)}},
        "suites": [
            {
                "name": "s",
                "source": "d",
                "target": "d",
                "tables": [
                    {
                        "name": "t",
                        "source": {"table": "a"},
                        "target": {"table": "b"},
                        "comparison": {
                            "strategies": ["nested"],
                            "group_by": ["parent"],
                            "on_mismatch": None,
                            "tolerance": {"numeric_abs": 0.001},
                        },
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=fake_registry)
    out = api.run(loaded, write_reports=False, history=False, registry=fake_registry)
    nested = _checks(out, "t")["compare.nested"]
    # p1: w sums 0.3 vs 0.3000000001 (within tolerance) but labels pair differently -> content;
    # p2: w 1 vs 5 -> totals; the NULL parent matches.
    assert nested.unexpected_count == 1 and nested.total_count == 3
    assert nested.details["totals_differ"] == 1
    assert nested.details["tolerance_columns"] == ["w"]
    assert nested.samples is None  # strict privacy keeps parent keys out of results
    assert "p2" not in (nested.message or "")
    assert any("full_diff" in h for h in nested.hints)


# ====================================================================== the connector base


def _fake(tmp_path: Path, docs: list[dict[str, Any]] | None = None) -> FakeDocs:
    FakeDocs.reads = []
    config = FakeDocsConfig(type="fake_docs", collections={"orders": docs or _documents()})
    return FakeDocs("docs", config, tmp_path)


def test_document_connector_schema_keys_and_native_filters(tmp_path: Path) -> None:
    con = _fake(tmp_path)
    try:
        rel = con.resolve(DatasetSpec(table="orders", options={"match": {"status": "open"}}))
        schema = con.get_schema(rel)
        native = {c.name: c.native_type for c in schema.columns}
        assert native["order_id"] == "int" and native["total"] == "decimal"
        assert native["order_date"] == "timestamp"
        assert schema.primary_key == ("_id",)
        assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 3}]
        assert FakeDocs.reads == [("orders", None, None)]

        unwound = con.resolve(DatasetSpec(table="orders", options={"unwind": "lines"}))
        unwound_schema = con.get_schema(unwound)
        assert unwound_schema.primary_key == ("_id", "lines_index")
        assert [c.name for c in unwound_schema.columns][:3] == ["_id", "lines_index", "line_no"]
        assert con.staged_rows(unwound) == 12

        renamed = con.resolve(
            DatasetSpec(table="orders", options={"fields": {"id": "_id", "n": "order_id"}})
        )
        assert con.get_schema(renamed).primary_key == ("id",)
        explicit = con.resolve(
            DatasetSpec(
                table="orders", options={"unwind": "lines", "primary_key": ["_id", "line_no"]}
            )
        )
        assert con.get_schema(explicit).primary_key == ("_id", "line_no")
        bad = con.resolve(DatasetSpec(table="orders", options={"primary_key": ["nope"]}))
        with pytest.raises(ConnectorError, match=r"options\.primary_key names column"):
            con.get_schema(bad)
        assert not con.exists(con.resolve(DatasetSpec(table="missing")))
    finally:
        con.close()


def test_document_connector_rejects_unsupported_specs(tmp_path: Path) -> None:
    con = _fake(tmp_path)
    with pytest.raises(ConnectorError, match="named with 'table', not 'path'"):
        con.resolve(DatasetSpec(path="orders.json"))
    with pytest.raises(ConnectorError, match="named with 'table', not 'query'"):
        con.resolve(DatasetSpec(query="db.orders.find()"))
    with pytest.raises(ConnectorError, match="as of a point in time"):
        con.resolve(DatasetSpec(table="orders", as_of="2024-01-01"))
    with pytest.raises(ConnectorError, match=r"options\.bogus: unknown option"):
        con.resolve(DatasetSpec(table="orders", options={"bogus": 1}))
    con.close()


def test_internal_sql_over_staged_views_types_source_expressions(
    tmp_path: Path, fake_registry: Registry
) -> None:
    db = tmp_path / "rel.duckdb"
    _relational(db)
    config = {
        "version": 1,
        "project": {"name": "expr"},
        "connections": {
            "rel": {"type": "duckdb", "database": str(db)},
            "docs": {"type": "fake_docs", "collections": {"orders": _documents()}},
        },
        "suites": [
            {
                "name": "s",
                "source": "docs",
                "target": "rel",
                "tables": [
                    {
                        "name": "lines",
                        "source": {
                            "table": "orders",
                            "options": {"unwind": {"path": "lines", "keep": ["order_id"]}},
                        },
                        "target": {"table": "order_lines"},
                        "keys": ["order_id", "line_no"],
                        "columns": {
                            "include": ["qty"],
                            "mappings": [{"source_expr": "qty * 1", "target": "qty"}],
                        },
                        "comparison": {"strategies": ["full_diff"]},
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=fake_registry)
    out = api.run(loaded, write_reports=False, history=False, registry=fake_registry)
    mismatch = _checks(out, "lines")["compare.column_mismatch"]
    assert [(m.column, m.mismatch_count) for m in mismatch.column_mismatches] == [("qty", 1)]


def test_staging_query_relations_on_the_connector(tmp_path: Path) -> None:
    con = _fake(tmp_path)
    try:
        base = con.resolve(DatasetSpec(table="orders", options={"fields": {"id": "order_id"}}))
        query = con.resolve(
            DatasetSpec(query=f"SELECT id * 2 AS twice FROM {base.sql}", filter="twice > 2004")
        )
        assert con.exists(query)
        schema = con.get_schema(query)
        assert [(c.name, c.type.logical) for c in schema.columns] == [
            ("twice", LogicalType.INTEGER)
        ]
        assert con.query(f"SELECT COUNT(*) AS n FROM {query.sql}").to_pylist() == [{"n": 4}]
        broken = con.resolve(DatasetSpec(query=f"SELECT nope FROM {base.sql}"))
        assert not con.exists(broken)
    finally:
        con.close()


# ====================================================================== schema inference


def test_infer_document_schema_reports_presence_types_and_arrays(tmp_path: Path) -> None:
    con = _fake(tmp_path)
    try:
        report = infer_document_schema(con, DatasetSpec(table="orders"), 4)
        assert report.documents == 4 and FakeDocs.reads[-1] == ("orders", 4, "first")
        rel = con.resolve(DatasetSpec(table="orders"))
        random_report = infer_document_schema(con, rel, sampling="random")
        assert "no random sampling" in random_report.notes[0]
        assert random_report.documents == 6
    finally:
        con.close()
    lines = report.field("lines")
    assert lines is not None and lines.types == {"array": 4}
    assert lines.to_dict()["array_length"] == {"min": 2, "max": 3, "avg": 2.25}
    element = report.field("lines[]")
    assert element is not None and element.containers == 9 and element.presence_pct == 100
    qty = report.field("lines[].qty")
    assert qty is not None and qty.depth == 2 and qty.present == 9
    total = report.field("total")
    assert total is not None and total.heterogeneous and total.types == {"double": 1, "decimal": 3}
    assert report.to_dict()["fields"][0]["path"] == "_id"


def test_infer_from_rows_of_sql_connectors(tmp_path: Path) -> None:
    db = tmp_path / "nested.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE t AS SELECT 1 AS id, {'a': 1, 'b': [1, 2]} AS s UNION ALL SELECT 2, NULL"
    )
    con.close()
    from dvf.config.connections import DuckDBConnection
    from dvf.connectors.duckdb import DuckDBConnector

    connector = DuckDBConnector("d", DuckDBConnection(type="duckdb", database=str(db)), tmp_path)
    try:
        report = infer_document_schema(connector, DatasetSpec(table="t"), sampling="random")
    finally:
        connector.close()
    assert report.documents == 2
    field = report.field("s.b[]")
    assert field is not None and field.containers == 2 and field.types == {"int": 2}
    s = report.field("s")
    assert s is not None and s.null_pct == 50
    assert "MongoDB only" in report.notes[0]


def test_document_schema_rules() -> None:
    docs = [
        {"a": 1, "b": "x", "c": {"d": 1}, "tags": [1, "two"], "n": None},
        {"a": 2, "b": 3, "c": {"d": 2}, "extra": True},
        {"a": 3, "c": {"d": None}},
    ]
    report = infer_from_documents(docs, dataset="t")
    config = DocumentSchemaCheck(
        type="document_schema",
        fields={
            "a": "integer",
            "b": {"type": "string", "min_presence": 90},
            "c.d": {"type": "number", "nullable": False},
            "missing": None,
            "optional": {"min_presence": 0},
            "tags[]": {"max_types": 1},
        },
        max_types=1,
        allow_extra_fields=False,
        ignore=["n"],
    )
    problems = evaluate_report(report, config)
    assert problems == [
        ["b", "low presence", "66.67% (2 of 3)", "present in at least 90%"],
        ["b", "unexpected type", "int in 1 of 2 values", "string"],
        ["b", "mixed types", "string (1), int (1)", "at most 1 type(s)"],
        ["c.d", "null values", "1 NULL value(s)", "no NULL values"],
        ["missing", "missing", "absent from every sampled document", "present in at least 100%"],
        ["tags[]", "mixed types", "int (1), string (1)", "at most 1 type(s)"],
        [
            "extra",
            "unexpected field",
            "present in 33.33%",
            "listed in 'fields' or matched by 'ignore'",
        ],
    ]  # 'tags' is covered: it is the parent of the listed 'tags[]'; 'n' is ignored
    assert allowed("long", "int", ["integer"]) and not allowed("long", "int", ["int"])
    assert allowed("date", "timestamp", ["date"]) and allowed("N", "decimal", ["number"])
    assert allowed("objectId", "object_id", ["objectid"])


def test_document_schema_check_rejects_where(tmp_path: Path, fake_registry: Registry) -> None:
    config = {
        "version": 1,
        "project": {"name": "w"},
        "connections": {"docs": {"type": "fake_docs", "collections": {"orders": _documents()}}},
        "suites": [
            {
                "name": "s",
                "source": "docs",
                "tables": [
                    {
                        "name": "orders",
                        "source": {"table": "orders"},
                        "checks": [
                            {"type": "document_schema", "where": "status = 'open'"},
                            {"type": "document_schema", "name": "ok", "fields": {"_id": "string"}},
                        ],
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=fake_registry)
    out = api.run(loaded, write_reports=False, history=False, registry=fake_registry)
    checks = {c.check_id: c for c in out.result.suites[0].datasets[0].checks}
    rejected = checks["s.orders.document_schema"]
    assert rejected.status.value == "error" and "'where' does not apply" in (rejected.message or "")
    accepted = checks["s.orders.document_schema.ok"]
    assert accepted.status.value == "passed"
    assert accepted.details["documents_sampled"] == 6


def test_empty_datasets_keep_their_key_columns(tmp_path: Path) -> None:
    config = FakeDocsConfig(type="fake_docs", collections={"empty": [], "no_lines": [{"_id": 1}]})
    con = FakeDocs("docs", config, tmp_path)
    try:
        for options, columns in (
            ({}, ["_id"]),
            ({"fields": {"a": "a"}}, ["a"]),
            ({"unwind": {"path": "lines", "keep": ["order_id"]}}, ["order_id", "lines_index"]),
        ):
            rel = con.resolve(DatasetSpec(table="empty", options=options))
            assert [c.name for c in con.get_schema(rel).columns] == columns
            assert con.query(f"SELECT COUNT(*) AS n FROM {rel.sql}").to_pylist() == [{"n": 0}]
        rel = con.resolve(DatasetSpec(table="no_lines", options={"unwind": "lines"}))
        assert [c.name for c in con.get_schema(rel).columns] == ["_id", "lines_index"]
    finally:
        con.close()
