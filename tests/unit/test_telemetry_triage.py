"""The AI-assisted triage plugin interface: off by default, opt-in only, masked and aggregated
summaries, suggestions in reports, and failure containment. No external service is involved."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from dvf import api
from dvf.config.models import TriageConfig
from dvf.packs.integrations.triage import TriageSummary, build_summary, report_section
from dvf.plugins import hookimpl
from dvf.plugins.registry import Registry
from dvf.scaffold.seeded import load_duckdb
from tests.unit.reporting_fixtures import XSS, build_run


class Recorder:
    def __init__(self, suggestions: Any = None, fail: bool = False) -> None:
        self.calls: list[TriageSummary] = []
        self.suggestions = suggestions
        self.fail = fail

    @hookimpl
    def triage_failures(self, summary: TriageSummary) -> Any:
        self.calls.append(summary)
        if self.fail:
            raise RuntimeError("model unavailable")
        return self.suggestions


PEOPLE = pa.table({"id": [1, 2, 3], "email": ["ann@example.test", None, "bob@example.test"]})


def _run(tmp_path: Path, registry: Registry, triage: dict[str, Any], **kwargs: Any) -> Any:
    load_duckdb({"people": PEOPLE}, tmp_path / "db.duckdb")
    config = {
        "version": 1,
        "project": {"name": "triage-demo"},
        "connections": {"db": {"type": "duckdb", "database": "db.duckdb"}},
        "suites": [
            {
                "name": "dq",
                "source": "db",
                "tables": [
                    {
                        "name": "people",
                        "source": {"table": "people"},
                        "checks": [
                            {"type": "not_null", "columns": ["email"]},
                            {"type": "regex", "column": "email", "pattern": "^ann@"},
                        ],
                    }
                ],
            }
        ],
        "history": {"enabled": False},
        "reports": {"formats": ["markdown"], "output_dir": "out"},
        "triage": triage,
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, registry=registry, strict=False)
    return api.run(loaded, **kwargs)


def test_triage_is_off_by_default(tmp_path: Path, registry: Registry) -> None:
    recorder = Recorder([{"summary": "never"}])
    registry.register_hooks(recorder)
    _run(tmp_path, registry, {})
    assert recorder.calls == []
    assert "Triage" not in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")


def test_opted_in_plugins_get_a_masked_summary(tmp_path: Path, registry: Registry) -> None:
    recorder = Recorder(
        [
            {
                "check_id": "dq.people.not_null.email",
                "summary": "Emails are missing.",
                "source": "acme",
            },
            "A plain suggestion.",
            {"no_summary": True},
        ]
    )
    registry.register_hooks(recorder)
    _run(tmp_path, registry, {"enabled": True})
    (summary,) = recorder.calls
    assert summary.project == "triage-demo" and summary.status == "failed"
    assert {c.check_id for c in summary.checks} == {
        "dq.people.not_null.email",
        "dq.people.regex.email",
    }
    text = json.dumps(summary.to_dict())
    assert "example.test" not in text  # no values, samples, or messages
    assert "SELECT" not in text  # no queries
    report = (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "Triage suggestions" in report
    assert "dq.people.not_null.email: Emails are missing. (acme)" in report
    assert "A plain suggestion." in report


def test_summary_holds_only_aggregates() -> None:
    run = build_run()
    run.summary = run.summarize()
    summary = build_summary(run, TriageConfig(enabled=True, max_checks=50))
    data = summary.to_dict()
    text = json.dumps(data)
    assert XSS not in text and "ann@example.test" not in text
    assert "38 rows differ" not in text  # check messages are excluded
    allowed = {
        "check_id",
        "type",
        "dimension",
        "severity",
        "status",
        "columns",
        "unexpected_count",
        "total_count",
        "unexpected_pct",
        "engine",
        "error_type",
        "column_mismatches",
        "reconciliation",
    }
    for check in data["checks"]:
        assert set(check) == allowed
    mismatches = [m for c in summary.checks for m in c.column_mismatches]
    assert mismatches and any(m.classifications for m in mismatches)
    errored = [c for c in summary.checks if c.status == "error"]
    assert errored and all(c.error_type for c in errored)
    limited = build_summary(run, TriageConfig(enabled=True, max_checks=1))
    assert len(limited.checks) == 1 and limited.omitted_checks == len(summary.checks) - 1
    assert limited.checks[0].status == "error"  # the worst first


def test_a_failing_triage_plugin_never_fails_the_run(
    tmp_path: Path, registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    registry.register_hooks(Recorder(fail=True))
    with caplog.at_level(logging.WARNING, logger="dvf"):
        output = _run(tmp_path, registry, {"enabled": True})
    assert output.result.status.value == "failed"
    assert "a triage plugin failed: RuntimeError: model unavailable" in caplog.text


def test_no_failures_no_call(tmp_path: Path, registry: Registry) -> None:
    recorder = Recorder([{"summary": "x"}])
    registry.register_hooks(recorder)
    _run(tmp_path, registry, {"enabled": True}, only=["check:no_such_check"])
    assert recorder.calls == []


def test_report_section_is_empty_without_suggestions() -> None:
    assert report_section([]) is None
