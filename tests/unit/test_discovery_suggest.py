"""dvf suggest: candidate checks derived from synthetic column profiles."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from dvf.core.errors import ConfigError
from dvf.discovery.profiling import (
    ColumnProfileEntry,
    ProfileReport,
    TableProfile,
    is_masked,
    load_profile,
)
from dvf.discovery.suggest import SuggestOptions, suggest, suggest_from_profiles

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _col(name: str, logical: str, **kwargs: Any) -> ColumnProfileEntry:
    values: dict[str, Any] = {
        "native_type": logical,
        "type": logical,
        "nullable": None,
        "count": 100,
        "null_count": 0,
    }
    values.update(kwargs)
    return ColumnProfileEntry(name=name, logical=logical, **values)


def _report(
    columns: list[ColumnProfileEntry],
    *,
    side: str = "source",
    comparison: bool = False,
    rows: int = 100,
) -> ProfileReport:
    table = TableProfile(
        suite="s",
        table="t",
        side=side,
        dataset="c:t",
        connection="c",
        dialect="duckdb",
        row_count=rows,
        columns=columns,
        is_comparison=comparison,
    )
    return ProfileReport(project="demo", generated_at="2026-11-08T12:00:00+00:00", tables=[table])


def _checks(result: Any) -> list[dict[str, Any]]:
    return [s.check for s in result.suggestions]


def test_rules_from_profiles() -> None:
    columns = [
        _col("id", "INTEGER", distinct_count=100, min=1, max=100),
        _col(
            "code",
            "STRING",
            distinct_count=100,
            min_length=36,
            max_length=36,
            formats={"uuid": 100},
        ),
        _col(
            "status",
            "STRING",
            distinct_count=3,
            top_values=[["new", 50], ["paid", 30], ["shipped", 20]],
            min_length=3,
            max_length=7,
        ),
        _col("level", "INTEGER", distinct_count=2, top_values=[[1, 60], [2, 40]], min=1, max=2),
        _col("amount", "DECIMAL", null_count=10, distinct_count=80, min="0.50", max="99.50"),
        _col("ratio", "FLOAT", distinct_count=90, min=0.25, max=0.75),
        _col("name", "STRING", null_count=5, distinct_count=60, min_length=2, max_length=20),
        _col("day", "DATE", distinct_count=30, min="2026-01-01", max="2026-01-30"),
        _col(
            "loaded_at",
            "TIMESTAMP",
            distinct_count=90,
            min="2026-11-01T00:00:00.000000",
            max="2026-11-07T12:00:00.000000",
        ),
        _col(
            "updated_at",
            "TIMESTAMP",
            distinct_count=90,
            min="2026-01-01T00:00:00",
            max="2026-11-08T06:00:00",
        ),
        _col("future", "TIMESTAMP", distinct_count=5, min="2026-01-01", max="2099-01-01T00:00:00"),
        _col("empty", "STRING", null_count=100, distinct_count=0),
        _col("flag", "BOOLEAN", distinct_count=2, top_values=[[True, 50], [False, 50]]),
        _col(
            "email",
            "STRING",
            distinct_count=4,
            top_values=[
                ["a***@x.com", 30],
                ["b***@x.com", 30],
                ["c***@x.com", 20],
                ["d***@x.com", 20],
            ],
            min_length=7,
            max_length=9,
            masked=True,
        ),
    ]
    result = suggest_from_profiles(_report(columns), SuggestOptions(now=NOW))
    checks = _checks(result)
    not_null = next(c for c in checks if c["type"] == "not_null")
    assert "amount" not in not_null["columns"] and "id" in not_null["columns"]
    assert {"type": "unique", "columns": ["id"]} in checks
    assert {"type": "unique", "columns": ["code"]} in checks
    assert not any(c["type"] == "range" and c["column"] == "id" for c in checks)
    assert {
        "type": "allowed_values",
        "column": "status",
        "values": ["new", "paid", "shipped"],
    } in checks
    assert {"type": "allowed_values", "column": "level", "values": [1, 2]} in checks
    assert {"type": "range", "column": "amount", "min": 0.5, "max": 99.5} in checks
    assert {"type": "range", "column": "ratio", "min": 0.25, "max": 0.75} in checks
    assert {"type": "length", "column": "name", "min": 2, "max": 20} in checks
    assert {"type": "range", "column": "day", "min": "2026-01-01"} in checks
    assert {"type": "range", "column": "loaded_at", "min": "2026-11-01 00:00:00"} in checks
    regex = next(c for c in checks if c["type"] == "regex")
    assert regex["column"] == "code" and regex["pattern"].startswith("[0-9a-fA-F]{8}")
    fresh = next(c for c in checks if c["type"] == "freshness")
    assert fresh == {
        "type": "freshness",
        "column": "updated_at",
        "max_age": "6h",
        "severity": "warning",
    }
    assert not any(c.get("column") == "email" and c["type"] == "allowed_values" for c in checks)
    assert not any(c.get("column") == "flag" and c["type"] == "allowed_values" for c in checks)
    evidence = {
        s.check["type"] + ":" + str(s.check.get("column")): s.evidence for s in result.suggestions
    }
    assert evidence["allowed_values:status"].startswith(
        "3 distinct values in 100 non-NULL rows: new (50)"
    )
    assert evidence["freshness:updated_at"] == "newest updated_at 2026-11-08T06:00:00 is 6.0h old"
    text = result.text
    assert "# evidence (source): no NULLs in 100 rows" in text
    data = yaml.safe_load(text)
    assert data["suites"][0]["tables"][0]["name"] == "t"


def test_margin_sides_and_documents(tmp_path: Path) -> None:
    columns = [
        _col("n", "INTEGER", distinct_count=10, min=10, max=20),
        _col("d", "DECIMAL", distinct_count=1, min="5", max="5"),
        _col("s", "STRING", distinct_count=50, min_length=10, max_length=20),
        _col(
            "ts",
            "TIMESTAMP",
            distinct_count=50,
            min="2026-01-01 00:00:00",
            max="2026-01-11 00:00:00",
        ),
        _col("dt", "DATE", distinct_count=50, min="2026-01-01", max="2026-01-11"),
    ]
    options = SuggestOptions(margin=0.1, now=NOW, freshness=True)
    result = suggest_from_profiles(_report(columns, side="target", comparison=True), options)
    checks = _checks(result)
    assert {"type": "range", "column": "n", "min": 9, "max": 21} in checks
    assert {"type": "range", "column": "d", "min": 4.5, "max": 5.5} in checks
    assert {"type": "length", "column": "s", "min": 9, "max": 22} in checks
    assert {"type": "range", "column": "ts", "min": "2025-12-31 00:00:00"} in checks
    assert {"type": "range", "column": "dt", "min": "2025-12-31"} in checks
    assert all("side" not in c for c in checks)  # target is the default side of comparison suites
    fresh = next(c for c in checks if c["type"] == "freshness")
    assert fresh["max_age"].endswith("d")
    assert "widened by 10%" in result.text
    both = _report(columns)
    both.tables.append(
        TableProfile(**{**both.tables[0].__dict__, "side": "target", "is_comparison": True})
    )
    both.tables[0].is_comparison = True
    named = _checks(suggest_from_profiles(both, SuggestOptions(now=NOW)))
    assert {c["name"] for c in named} == {"source", "target"}
    assert all(c.get("side") == "source" for c in named if c["name"] == "source")
    out = tmp_path / "s.yaml"
    written = suggest(_report(columns), SuggestOptions(now=NOW), output=out)
    assert written.output == out and out.exists()
    assert written.to_dict()["suggestions"]
    with pytest.raises(ConfigError, match="margin"):
        suggest_from_profiles(_report(columns), SuggestOptions(margin=-1))


def test_skipped_and_empty_tables() -> None:
    report = _report([], rows=0)
    report.tables.append(
        TableProfile(
            suite="s",
            table="u",
            side="source",
            dataset="",
            connection="c",
            dialect="",
            error="boom",
        )
    )
    result = suggest_from_profiles(report)
    assert result.suggestions == [] and result.skipped == ["s.u (source): boom"]
    assert "No checks could be suggested" in result.text
    assert yaml.safe_load(result.text) == {"suites": []}


def test_profile_documents_round_trip(tmp_path: Path) -> None:
    report = _report(
        [_col("id", "INTEGER", distinct_count=100, min=1, max=100, top_values=[[1, 2]])]
    )
    path = tmp_path / "p.json"
    path.write_text(json.dumps(report.to_dict()), encoding="utf-8")
    loaded = load_profile(path)
    assert loaded.tables[0].columns[0].top_values == [[1, 2]]
    assert load_profile(report) is report
    assert loaded.tables[0].column("ID") is not None and loaded.tables[0].column("x") is None
    with pytest.raises(ConfigError, match="Cannot read"):
        load_profile(tmp_path / "missing.json")
    path.write_text('{"x": 1}', encoding="utf-8")
    with pytest.raises(ConfigError, match="not a dvf profile"):
        load_profile(path)
    path.write_text('{"version": 99, "tables": []}', encoding="utf-8")
    with pytest.raises(ConfigError, match="version 99"):
        load_profile(path)
    path.write_text('{"tables": [{"bogus": 1}]}', encoding="utf-8")
    with pytest.raises(ConfigError, match="not a dvf profile document"):
        load_profile(path)
    assert is_masked("***") and is_masked("a***@b.c") and not is_masked(3)
