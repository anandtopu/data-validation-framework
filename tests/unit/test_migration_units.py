"""Unit tests for the migration validations: helpers, configuration models, run comparison,
named baselines, the sign-off report, and registration (synthetic data only)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from dvf.config.models import InFlightConfig, TableConfig, WindowConfig
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.enums import Dimension, Severity, Side, Status
from dvf.core.errors import ConfigError, PlanError
from dvf.core.types import CanonicalType
from dvf.execution.context import RunInfo
from dvf.execution.history import (
    compare_runs,
    list_named_baselines,
    load_named_baseline,
    save_named_baseline,
)
from dvf.models.results import (
    BaselineComparison,
    CheckResult,
    DatasetPairResult,
    ReconciliationSummary,
    RowCounts,
    RunEnvironment,
    RunResult,
    SuiteResult,
)
from dvf.packs.migration import CHECK_TYPES
from dvf.packs.migration import config as cfg
from dvf.packs.migration.charset import is_mojibake, round_trips, violations
from dvf.packs.migration.dst import local_intervals, transitions
from dvf.packs.migration.signoff import recommend, render_html, render_markdown
from dvf.packs.migration.windows import (
    bound_literal,
    in_flight_cutoff,
    parse_bound,
    window_predicate,
)
from dvf.plugins.registry import PluginKind, Registry
from dvf.reporting.base import ReportOptions
from dvf.sql.dialects import get_dialect

RUN = RunInfo(run_id="20261108T120000Z-abcd", started_at=datetime(2026, 11, 8, 12, 0, tzinfo=UTC))


# ---------------------------------------------------------------------- registration


def test_every_migration_component_is_registered_and_ready(registry: Registry) -> None:
    for name in CHECK_TYPES:
        entry = registry.get(PluginKind.CHECK, name)
        assert entry.status == "ready", name
        assert entry.config_model is not None
        assert "(MIG-" in entry.description, entry.description
    assert registry.get(PluginKind.REPORTER, "signoff").status == "ready"


# ---------------------------------------------------------------------- windows (MIG-02, MIG-09)


def test_parse_bound_types() -> None:
    assert parse_bound("2026-11-01", RUN) == date(2026, 11, 1)
    assert parse_bound("2026-11-01T10:00:00+01:00", RUN) == datetime(2026, 11, 1, 9, 0)
    assert parse_bound("${run.timestamp}", RUN) == datetime(2026, 11, 8, 12, 0)
    assert parse_bound("${run.date}", RUN) == date(2026, 11, 8)
    assert parse_bound(42, RUN) == 42
    assert parse_bound("12345", RUN) == "12345"  # compared with the column's own type
    assert parse_bound(datetime(2026, 1, 1, tzinfo=UTC), RUN) == datetime(2026, 1, 1)


def test_bound_literal_uses_sqlite_text_form() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5)
    assert bound_literal(moment, get_dialect("sqlite")) == "'2026-01-02 03:04:05'"
    assert bound_literal(moment, get_dialect("duckdb")) == "'2026-01-02T03:04:05'"


def _schema(*names: str) -> DatasetSchema:
    ref = DatasetRef(connection="c", kind=DatasetKind.TABLE, name="t")
    return DatasetSchema(
        ref=ref,
        columns=tuple(
            ColumnSchema(name=n, native_type="TIMESTAMP", type=CanonicalType.string())
            for n in names
        ),
    )


def test_window_predicate_per_side() -> None:
    window = WindowConfig(source_column="changed_at", target_column="LOAD_TS", start=5, end="9")
    d = get_dialect("duckdb")
    source = window_predicate(window, Side.SOURCE, _schema("changed_at"), d, RUN)
    assert source == '"changed_at" >= 5 AND "changed_at" < \'9\''
    target = window_predicate(window, Side.TARGET, _schema("load_ts"), d, RUN)
    assert target.startswith('"load_ts" >= 5')
    with pytest.raises(PlanError, match="not found"):
        window_predicate(window, Side.SOURCE, _schema("other"), d, RUN)


def test_window_config_validation() -> None:
    with pytest.raises(ValidationError, match="start"):
        WindowConfig(column="x")
    with pytest.raises(ValidationError, match="column"):
        WindowConfig(start=1)
    table = TableConfig.model_validate(
        {"name": "t", "source": {"table": "a"}, "window": {"column": "d", "end": "2026-01-01"}}
    )
    assert table.window is not None and table.window.end == "2026-01-01"


def test_in_flight_cutoff_defaults_to_run_start_in_the_normalization_zone() -> None:
    config = InFlightConfig(column="changed_at", window="15m")
    assert in_flight_cutoff(config, RUN, "UTC") == datetime(2026, 11, 8, 12, 0)
    assert in_flight_cutoff(config, RUN, "Europe/Berlin") == datetime(2026, 11, 8, 13, 0)
    explicit = InFlightConfig(column="c", window="1h", cutoff="2026-01-01T00:00:00Z")
    assert in_flight_cutoff(explicit, RUN, "Europe/Berlin") == datetime(2026, 1, 1, 1, 0)
    local = InFlightConfig(column="c", window="1h", cutoff="2026-01-01T00:00:00")
    assert in_flight_cutoff(local, RUN, "Europe/Berlin") == datetime(2026, 1, 1, 0, 0)
    with pytest.raises(PlanError, match="ISO"):
        in_flight_cutoff(InFlightConfig(column="c", window="1h", cutoff="soon"), RUN, "UTC")


# ---------------------------------------------------------------------- charset (MIG-10)


def test_mojibake_detection() -> None:
    assert is_mojibake("CafÃ©")  # "Café" encoded as UTF-8, read as cp1252
    assert is_mojibake("donâ€™t")  # "don't" with a typographic apostrophe
    assert is_mojibake("Ã…ngstrÃ¶m")  # "Ångström"
    assert not is_mojibake("Café")
    assert not is_mojibake("naïve Straße")
    assert not is_mojibake("plain ASCII")
    assert is_mojibake("abc??def", ["??"])


def test_round_trip_through_legacy_code_pages() -> None:
    assert round_trips("Straße", "cp037", "none")
    assert not round_trips("€uro", "cp037", "none")  # no euro sign in cp037
    assert round_trips("€uro", "cp1252", "none")
    assert not round_trips("é", "latin-1", "none")  # decomposed accent
    assert round_trips("é", "latin-1", "NFC")


def test_charset_violations_per_rule() -> None:
    config = cfg.CharsetConversionCheck(type="charset_conversion", columns=["x"], code_page="cp037")
    rules = config.effective_rules
    assert rules == ["replacement_char", "mojibake", "control_chars", "round_trip"]
    assert violations("ok\tfine", config, rules) == []
    assert violations("bad�", config, rules) == ["replacement_char", "round_trip"]
    assert violations("ctl\x01", config, rules) == ["control_chars"]
    assert "mojibake" in violations("CafÃ©", config, rules)


def test_charset_config_validation() -> None:
    with pytest.raises(ValidationError, match="unknown code page"):
        cfg.CharsetConversionCheck(type="charset_conversion", columns=["x"], code_page="nope")
    with pytest.raises(ValidationError, match="code_page"):
        cfg.CharsetConversionCheck(type="charset_conversion", columns=["x"], rules=["round_trip"])
    plain = cfg.CharsetConversionCheck(type="charset_conversion", columns=["x"])
    assert "round_trip" not in plain.effective_rules


# ---------------------------------------------------------------------- DST (MIG-11)


def test_dst_intervals_europe_berlin() -> None:
    gaps, overlaps = local_intervals(ZoneInfo("Europe/Berlin"), 2021, 2021)
    assert gaps == [(datetime(2021, 3, 28, 2, 0), datetime(2021, 3, 28, 3, 0))]
    assert overlaps == [(datetime(2021, 10, 31, 2, 0), datetime(2021, 10, 31, 3, 0))]


def test_dst_midnight_transition_sao_paulo() -> None:
    gaps, _ = local_intervals(ZoneInfo("America/Sao_Paulo"), 2018, 2018)
    assert (datetime(2018, 11, 4, 0, 0), datetime(2018, 11, 4, 1, 0)) in gaps


def test_no_transitions_in_zones_without_dst() -> None:
    assert transitions(ZoneInfo("Asia/Kolkata"), 2000, 2030) == []


def test_dst_config_rejects_unknown_zone() -> None:
    with pytest.raises(ValidationError, match="unknown time zone"):
        cfg.DstEdgeCasesCheck(type="dst_edge_cases", columns=["t"], timezone="Mars/Olympus")


# ---------------------------------------------------------------------- config models


def test_substitution_rule_shape() -> None:
    rule = cfg.SubstitutionRule(column="m", source_null=True, target_value="N/A")
    assert rule.describe() == "m: NULL -> 'N/A'"
    with pytest.raises(ValidationError, match="source_null"):
        cfg.SubstitutionRule(column="m", target_null=True)
    with pytest.raises(ValidationError, match="exactly one"):
        cfg.SubstitutionRule(column="m", source_null=True, target_null=True, target_value="x")


def test_delete_propagation_needs_something_to_check() -> None:
    with pytest.raises(ValidationError, match="source_flag"):
        cfg.DeletePropagationCheck(type="delete_propagation", hard_deletes=False)
    with pytest.raises(ValidationError, match="nothing to check"):
        cfg.DeletePropagationCheck(
            type="delete_propagation", hard_deletes=False, source_flag={"column": "d"}
        )
    with pytest.raises(ValidationError, match="target_flag"):
        cfg.DeletePropagationCheck(type="delete_propagation", soft_deleted_rows="flagged")


def test_flag_and_locator_shapes() -> None:
    with pytest.raises(ValidationError):
        cfg.FlagConfig(column="a", expression="b = 1")
    with pytest.raises(ValidationError):
        cfg.DatasetLocator()
    ref = cfg.ChangeTableRef(table="chg", key="id", operation="op", sequence="seq")
    assert ref.key == ["id"] and ref.sequence == ["seq"]


def test_key_map_arity_and_audit_requirements() -> None:
    with pytest.raises(ValidationError, match="same length"):
        cfg.KeyMapCheck(
            type="key_map",
            map={"table": "m", "legacy_columns": ["a"], "new_columns": ["b"]},
            source_key=["x", "y"],
        )
    with pytest.raises(ValidationError, match="at least one"):
        cfg.AuditColumnsCheck(type="audit_columns")
    with pytest.raises(ValidationError, match="batch_id"):
        cfg.AuditColumnsCheck(type="audit_columns", load_timestamp="t", max_batches=1)
    audit = cfg.AuditColumnsCheck(type="audit_columns", load_timestamp="t", batch_id="b")
    assert audit.target_columns() == ["t", "b"]


def test_late_arriving_needs_a_rule() -> None:
    with pytest.raises(ValidationError, match="max_lateness"):
        cfg.LateArrivingCheck(type="late_arriving", event_column="e", load_column="l")


# ---------------------------------------------------------------------- results helpers


def _check(
    check_id: str, status: Status, unexpected: int | None = None, **kw: object
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        name=check_id.rsplit(".", 1)[-1],
        type=check_id.rsplit(".", 1)[-1],
        dimension=Dimension.RECONCILIATION,
        severity=kw.pop("severity", Severity.ERROR),  # type: ignore[arg-type]
        status=status,
        unexpected_count=unexpected,
        message=f"{check_id} {status.value}",
    )


def _run(run_id: str, checks: list[CheckResult], **extra: object) -> RunResult:
    dataset = DatasetPairResult(
        name="customers",
        status=Status.FAILED if any(c.status is Status.FAILED for c in checks) else Status.PASSED,
        checks=checks,
        row_counts=RowCounts(source=100, target=extra.pop("target_rows", 100)),  # type: ignore[arg-type]
        reconciliation=ReconciliationSummary(matched=98, missing_in_target=2),
    )
    result = RunResult(
        run_id=run_id,
        project="mig",
        status=dataset.status,
        started_at=datetime(2026, 11, 8, tzinfo=UTC),
        duration_ms=1000.0,
        environment=RunEnvironment(dvf_version="0", python_version="3", platform="test"),
        suites=[SuiteResult(name="s", status=dataset.status, datasets=[dataset])],
        **extra,  # type: ignore[arg-type]
    )
    result.summary = result.summarize()
    return result


def test_compare_runs_reports_new_resolved_and_persisting_failures() -> None:
    a = _run(
        "A",
        [
            _check("s.customers.row_count", Status.FAILED, 5),
            _check("s.customers.not_null", Status.FAILED, 3),
            _check("s.customers.unique", Status.PASSED, 0),
            _check("s.customers.gone", Status.PASSED),
        ],
    )
    b = _run(
        "B",
        [
            _check("s.customers.row_count", Status.FAILED, 2),
            _check("s.customers.not_null", Status.PASSED, 0),
            _check("s.customers.unique", Status.FAILED, 7),
            _check("s.customers.added", Status.PASSED),
        ],
        target_rows=97,
    )
    diff = compare_runs(a, b)
    assert diff.new_failures == ["s.customers.unique"]
    assert diff.resolved == ["s.customers.not_null"]
    assert diff.persisting == ["s.customers.row_count"]
    assert diff.added_checks == ["s.customers.added"]
    assert diff.removed_checks == ["s.customers.gone"]
    changes = {c["check_id"]: (c["before"], c["after"]) for c in diff.check_deltas}
    assert changes["s.customers.row_count"] == (5, 2)
    target_rows = next(d for d in diff.dataset_deltas if d["metric"] == "target_rows")
    assert target_rows == {
        "table": "s.customers",
        "metric": "target_rows",
        "before": 100,
        "after": 97,
        "change": -3,
    }
    assert diff.to_dict()["run_b"] == "B"


def test_named_baselines_round_trip(tmp_path: Path) -> None:
    document = {"captured_at": "2026-11-08T10:00:00", "side": "source", "tables": {"s.t": {}}}
    path = save_named_baseline(tmp_path, "pre_cutover", document)
    assert path == tmp_path / "baselines" / "pre_cutover.json"
    assert load_named_baseline(tmp_path, "pre_cutover")["side"] == "source"
    with pytest.raises(ConfigError, match="already exists"):
        save_named_baseline(tmp_path, "pre_cutover", document)
    save_named_baseline(tmp_path, "pre_cutover", document, overwrite=True)
    assert [b["name"] for b in list_named_baselines(tmp_path)] == ["pre_cutover"]
    with pytest.raises(ConfigError, match="not found") as missing:
        load_named_baseline(tmp_path, "missing")
    assert missing.value.hint == "Available baselines: pre_cutover."
    with pytest.raises(ConfigError, match="Invalid baseline name"):
        save_named_baseline(tmp_path, "../escape", document)


# ---------------------------------------------------------------------- sign-off (MIG-08)


def test_recommendation_levels() -> None:
    assert recommend(_run("r", [_check("s.t.a", Status.PASSED)])).decision == "GO"
    warning = _run("r", [_check("s.t.a", Status.WARNING)])
    assert recommend(warning).decision == "CONDITIONAL"
    minor = _run("r", [_check("s.t.a", Status.FAILED, severity=Severity.WARNING)])
    assert recommend(minor).decision == "CONDITIONAL"
    blocking = _run("r", [_check("s.t.a", Status.FAILED)])
    decision = recommend(blocking)
    assert decision.decision == "NO-GO" and "error severity" in decision.reasons[0]
    errored = _run("r", [_check("s.t.a", Status.ERROR)])
    assert recommend(errored).decision == "NO-GO"


def test_signoff_documents_contain_the_sections() -> None:
    run = _run(
        "R1",
        [_check("s.customers.row_count", Status.FAILED, 2), _check("s.customers.u", Status.PASSED)],
        baseline=BaselineComparison(
            baseline_run_id="R0", policy="last_run", persisting=["s.customers.row_count"]
        ),
        metadata={"release": "rehearsal 3"},
    )
    markdown = render_markdown(run, ReportOptions(title="Cutover <3>"))
    for heading in (
        "# Cutover sign-off: Cutover <3>",
        "## Scope",
        "## Results by table",
        "## Open failures",
        "## Waivers (accepted differences)",
        "## Run-to-run comparison",
        "## Approvals",
    ):
        assert heading in markdown
    assert "NO-GO" in markdown and "persisting" in markdown and "rehearsal 3" in markdown
    page = render_html(run, ReportOptions(title="Cutover <3>"))
    assert "Cutover &lt;3&gt;" in page and 'class="no-go"' in page and "Business owner" in page
