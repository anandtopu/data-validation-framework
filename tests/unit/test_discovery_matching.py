"""Discovery building blocks: the commented YAML writer, name matching, and helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dvf.core.errors import ConfigError
from dvf.discovery.common import UniqueNames, bare_name, file_stem, identifier, write_output
from dvf.discovery.connections import from_location, rebase_paths, relative_path, spec_from_location
from dvf.discovery.matching import MatchMethod, match_items, normalize_name, similarity
from dvf.discovery.yamlwriter import Document, Flow, Map, Seq, dump, plain, scalar


def test_writer_round_trips_and_keeps_comments() -> None:
    table = Map().add("name", "t1").add("source", Flow({"table": "a,b", "filter": "x = 'y'"}))
    table.add("keys", ["id", "x y"], inline="primary key")
    table.add("sql", "SELECT *\nFROM t\n")
    table.add(
        "columns",
        Map().add("mappings", Seq().add(Flow({"source": "a", "target": "b"}), inline="fuzzy")),
    )
    tables = Seq().add(table, comment=["first table", "REVIEW: check"]).add("plain").add(["a", "b"])
    root = Map().add("version", 1, comment="the version").add("suites", tables)
    root.add("empty", []).add("nothing", {}).add("flag", "yes").add("code", "0100")
    root.add("colon", "a: b # c").add("none", None).add("number", 1.5).add("on", True)
    root.add("nested", [{"a": 1}, {"b": [1, 2]}])
    root.add("multiline_with_spaces", "a  \nb")
    document = Document(root, header=["header"], footer=["footer"])
    text = dump(document)
    assert yaml.safe_load(text) == plain(document)
    assert "# first table" in text and "# REVIEW: check" in text
    assert "keys: [id, x y]  # primary key" in text
    assert "sql: |\n      SELECT *\n      FROM t" in text
    assert "flag: 'yes'" in text and "code: '0100'" in text
    assert text.startswith("# header\n") and text.rstrip().endswith("# footer")


def test_writer_replaces_keys_and_rejects_non_finite_numbers() -> None:
    m = Map().add("a", 1).add("a", 2)
    assert plain(m) == {"a": 2} and "a" in m and m.get("a") == 2 and m.get("b") is None
    with pytest.raises(ValueError, match="non-finite"):
        scalar(float("inf"))
    assert scalar(None) == "null" and scalar(False) == "false"


def test_matching_order_and_methods() -> None:
    source = ["customers", "OrderLines", "Products", "legacy", "id"]
    target = ["CUSTOMERS", "order_lines", "product", "audit", "id"]
    result = match_items(source, target, lambda n: n, threshold=0.8)
    found = {(m.source, m.target): m for m in result.matches}
    assert found[("id", "id")].method is MatchMethod.EXACT
    assert found[("customers", "CUSTOMERS")].method is MatchMethod.CASE_INSENSITIVE
    assert found[("OrderLines", "order_lines")].method is MatchMethod.FUZZY
    assert found[("OrderLines", "order_lines")].score == 1.0
    assert 0.8 <= found[("Products", "product")].score < 1
    assert result.unmatched_source == ["legacy"] and result.unmatched_target == ["audit"]
    assert [m.source for m in result.matches] == ["customers", "OrderLines", "Products", "id"]
    strict = match_items(source, target, lambda n: n, fuzzy=False)
    assert "OrderLines" in strict.unmatched_source


def test_fuzzy_matching_is_one_to_one_and_best_first() -> None:
    result = match_items(
        ["customer_nm", "customer_name"], ["customer_name_x"], lambda n: n, threshold=0.5
    )
    assert len(result.matches) == 1 and result.matches[0].source == "customer_name"
    assert normalize_name("Order-Lines 2") == "orderlines2"
    assert similarity("__", "__") == 1.0 and similarity("a", "__") == 0.0


def test_helpers(tmp_path: Path) -> None:
    assert identifier("sales.order lines!") == "sales_order_lines"
    assert identifier("***", "x") == "x"
    names = UniqueNames(["orders"])
    assert names.take("Orders") == "Orders_2" and names.take("orders") == "orders_3"
    assert file_stem("data/orders.csv.gz") == "orders" and bare_name('sales."orders"') == "orders"
    out = tmp_path / "a" / "b.yaml"
    write_output("x\n", out, overwrite=False)
    with pytest.raises(ConfigError, match="already exists"):
        write_output("y\n", out, overwrite=False)
    write_output("y\n", out, overwrite=True)
    assert out.read_text(encoding="utf-8") == "y\n"


def test_locations_and_path_rebasing(tmp_path: Path) -> None:
    (tmp_path / "files").mkdir()
    (tmp_path / "db.duckdb").write_bytes(b"")
    assert from_location(str(tmp_path / "files"), side="source")["type"] == "files"
    assert from_location("db.duckdb", side="source", cwd=tmp_path)["type"] == "duckdb"
    assert from_location("x.sqlite", side="source", cwd=tmp_path)["type"] == "sqlite"
    assert from_location("sqlite:///:memory:", side="s")["database"] == ":memory:"
    assert from_location("duckdb:///rel.duckdb", side="s", cwd=tmp_path)["database"].endswith(
        "rel.duckdb"
    )
    assert from_location("mysql://u@h/db", side="s") == {"type": "mysql", "url": "mysql://u@h/db"}
    with pytest.raises(ConfigError, match="Unsupported"):
        from_location("oracle://x", side="source")
    with pytest.raises(ConfigError, match="not a folder"):
        from_location("missing.txt", side="target", cwd=tmp_path)
    spec = spec_from_location(
        "postgresql://dvf:pw@localhost/db", name="source", side="source", output_dir=tmp_path
    )
    assert spec.written_config["url"] == "${secret:env:SOURCE_DATABASE_URL}"
    assert spec.open_config["url"].startswith("postgresql://dvf:pw@") and spec.notes
    files = spec_from_location(
        str(tmp_path / "files"), name="t", side="target", output_dir=tmp_path / "out"
    )
    assert files.written_config["path"] == "../files"
    rebased = rebase_paths(
        {"type": "duckdb", "database": "a.duckdb"}, tmp_path / "x", tmp_path / "y"
    )
    assert rebased["database"] == "../x/a.duckdb"
    assert (
        rebase_paths({"type": "duckdb", "database": ":memory:"}, tmp_path, tmp_path / "y")[
            "database"
        ]
        == ":memory:"
    )
    assert rebase_paths({"type": "postgres", "url": "x"}, tmp_path, tmp_path) == {
        "type": "postgres",
        "url": "x",
    }
    assert relative_path(tmp_path / "a" / "b", tmp_path) == "a/b"
