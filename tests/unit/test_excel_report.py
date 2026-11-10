from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from dvf.core.enums import Status
from dvf.core.errors import DependencyError
from dvf.models.results import RunEnvironment, RunResult, SampleSet
from dvf.plugins.registry import Registry
from dvf.reporting import excel
from dvf.reporting.base import ReportOptions, render_reports
from dvf.reporting.builtin import BUILTIN_REPORTERS
from dvf.reporting.excel import EXCEL_FILE, ExcelReporter, excel_value, render_excel
from tests.unit.reporting_fixtures import BASELINE_RUN_ID, build_run, many_checks_run

SHEETS = [
    "Summary",
    "Tables",
    "Checks",
    "Schema diff",
    "Mismatches",
    "Samples",
    "Waivers",
    "Trend",
    "Sign-off",
]


@pytest.fixture
def run() -> RunResult:
    return build_run()


def _render(run: RunResult, tmp_path: Path, **options: Any) -> Any:
    path = tmp_path / EXCEL_FILE
    render_excel(run, path, ReportOptions(**options))
    return openpyxl.load_workbook(path)


def _rows(ws: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in ws.iter_rows(values_only=True)]


def _table(ws: Any) -> list[dict[str, Any]]:
    """Data rows under the header row, as dicts (stops at the first note or blank row)."""
    rows = _rows(ws)
    header = rows[0]
    out = []
    for row in rows[1:]:
        if row[1] is None and row[0] is not None and len(str(row[0])) > 40:
            break  # a note row
        if all(value is None for value in row):
            break
        out.append(dict(zip(header, row, strict=False)))
    return out


def _pairs(ws: Any) -> dict[str, Any]:
    return {row[0]: row[1] for row in _rows(ws) if row and isinstance(row[0], str)}


def _fill(cell: Any) -> str:
    return str(cell.fill.fgColor.rgb)[-6:]


def _texts(wb: Any) -> str:
    return "\n".join(
        str(value) for ws in wb for row in ws.iter_rows(values_only=True) for value in row if value
    )


def test_excel_reporter_writes_workbook(run: RunResult, tmp_path: Path) -> None:
    artifacts = ExcelReporter().render(run, tmp_path / "out", ReportOptions())
    assert [(a.kind, a.format) for a in artifacts] == [("report", "excel")]
    path = Path(artifacts[0].path)
    assert path.name == "report.xlsx" and path.is_file()
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == SHEETS
    assert wb.properties.creator == "dvf"


def test_excel_summary(run: RunResult, tmp_path: Path) -> None:
    flagged = run.model_copy(update={"interrupted": True, "resumed_from_checkpoint": True})
    wb = _render(flagged, tmp_path, metadata={"environment": "staging"})
    ws = wb["Summary"]
    assert ws.freeze_panes is None
    values = _pairs(ws)
    assert values["Overall status"] == "error"
    assert values["Quality score"] == pytest.approx(0.5)  # 50%, shown with a percent format
    assert values["Project"] == "synthetic-migration"
    assert values["Run ID"] == run.run_id
    assert values["Started (UTC)"] == datetime(2026, 11, 6, 9, 30)
    assert values["Tables"] == 4 and values["Suites"] == 2 and values["Checks"] == 11
    assert values["Rows compared"] == 60238
    for status, count in {
        "passed": 3,
        "warning": 1,
        "failed": 3,
        "error": 1,
        "skipped": 2,
        "waived": 1,
    }.items():
        assert values[status] == count
    assert values["Interrupted"] == "Yes" and values["Resumed from checkpoint"] == "Yes"
    assert values["Baseline run"] == BASELINE_RUN_ID
    assert values["New failures"] == 2 and values["Resolved"] == 1
    assert values["Persisting failures"] == 2
    assert values["applied"] == 2 and values["expired"] == 1 and values["unused"] == 1
    assert values["environment"] == "staging" and values["release"] == "R-2026.11"
    assert values["Config hash"] == "sha256:feedface"

    cells = {row[0].value: row for row in ws.iter_rows() if row and row[0].value}
    assert _fill(cells["Overall status"][1]) == "F9E3F1"  # error tone
    assert _fill(cells["expired"][1]) == "FFEBE9"  # expired waivers highlighted as failures
    assert cells["Quality score"][1].number_format.endswith("%")
    texts = _texts(wb)
    assert "resume it with 'dvf run --resume'" in texts
    assert "Top issues" in texts


def test_excel_tables_and_checks(run: RunResult, tmp_path: Path) -> None:
    wb = _render(run, tmp_path)
    tables = wb["Tables"]
    assert tables.freeze_panes == "C2"
    assert tables.auto_filter.ref == "A1:X5"
    customers = _table(tables)[0]
    assert customers["Table"] == "customers" and customers["Status"] == "failed"
    assert (customers["Source rows"], customers["Target rows"], customers["Difference"]) == (
        10000,
        9988,
        -12,
    )
    assert (customers["Matched"], customers["Missing in target"], customers["Mismatched"]) == (
        9950,
        12,
        38,
    )
    assert customers["Duplicate keys (source)"] == 1
    assert customers["Match %"] == pytest.approx(0.995)
    assert "reconciliation 0%" in customers["Quality dimensions"]

    checks_ws = wb["Checks"]
    assert checks_ws.freeze_panes == "B2"
    assert checks_ws.auto_filter.ref == "A1:Y12"
    checks = {row["Check ID"]: row for row in _table(checks_ws)}
    assert list(checks) == [c.check_id for c in run.iter_checks()]
    compare = checks["sales_migration.customers.column_compare"]
    assert compare["Status"] == "failed" and compare["Severity"] == "critical"
    assert compare["Baseline"] == "new"
    assert compare["Message"] == "38 rows differ | see side-by-side samples"
    assert compare["Observed"] == '{"mismatched_rows": 38}'
    assert compare["Unexpected count"] == 38 and compare["Total count"] == 9988
    assert compare["Unexpected %"] == pytest.approx(0.00380457)
    assert compare["Partition"] == "region=EU"
    assert (
        "Repro SQL: repro/sales_migration.customers.column_compare.source.sql"
        in compare["Artifacts"]
    )
    assert "Fix the CHAR to VARCHAR mapping" in compare["Hints"]
    row_count = checks["sales_migration.customers.row_count"]
    assert row_count["Waiver"].startswith("EXPIRED on 2026-10-31: Recent EU rows")
    assert row_count["Baseline"] == "persisting"
    waived = checks["sales_migration.orders.accepted_values.status"]
    assert waived["Status"] == "waived" and "ticket MIG-101" in waived["Waiver"]
    errored = checks["sales_migration.orders.aggregate_compare"]
    assert errored["Error"].startswith("OperationalError")
    assert "Check the target table name" in errored["Hints"]

    header = [c.value for c in checks_ws[1]]
    status_col = header.index("Status")
    waiver_col = header.index("Waiver")
    by_id = {row[0].value: row for row in checks_ws.iter_rows(min_row=2)}
    assert _fill(by_id["sales_migration.customers.column_compare"][status_col]) == "FFEBE9"
    assert _fill(by_id["sales_migration.orders.accepted_values.status"][status_col]) == "DDF4FF"
    assert _fill(by_id["sales_migration.customers.row_count"][waiver_col]) == "FFEBE9"
    assert _fill(by_id["sales_migration.orders.not_null.order_id"][status_col]) == "DAFBE1"


def test_excel_schema_mismatches_samples_waivers_trend(run: RunResult, tmp_path: Path) -> None:
    wb = _render(run, tmp_path)
    schema = _table(wb["Schema diff"])
    assert len(schema) == 5
    balance = next(r for r in schema if r["Source column"] == "BALANCE")
    assert balance["Compatibility"] == "lossy" and balance["Source type"] == "DECIMAL(18,4)"
    assert balance["Target canonical"] == "DECIMAL(18,2)"
    assert balance["Nullability change"] == "NOT NULL -> NULL"
    assert next(r for r in schema if r["Source column"] == "FAX")["Status"] == "missing_in_target"

    mismatches = {r["Column"]: r for r in _table(wb["Mismatches"])}
    assert set(mismatches) == {"email", "full_name", "balance", "created_at"}
    email = mismatches["email"]
    assert email["Mismatches"] == 15 and email["Mismatch %"] == pytest.approx(0.0015018)
    assert email["Top classification"] == "whitespace_only"
    assert email["Classification breakdown"] == "whitespace_only: 12, case_only: 3"
    assert email["Waived"] == "No" and mismatches["full_name"]["Waived"] == "Yes"

    samples = _rows(wb["Samples"])
    flat = [value for row in samples for value in row if value is not None]
    assert "sales_migration.customers.column_compare · column email" in flat
    assert "ann@example.test " in flat and "NULL" in flat
    assert "<script>alert(1)</script>" in flat  # text stays text in Excel

    waivers = _table(wb["Waivers"])
    assert [w["Status"] for w in waivers] == ["applied", "applied", "expired", "unused"]
    expired = waivers[2]
    assert expired["Selector"] == "sales_migration.customers.row_count"
    assert expired["Expires"] == "2026-10-31" and expired["Matched checks"] == 1
    ws = wb["Waivers"]
    expired_row = next(row for row in ws.iter_rows(min_row=2) if row[0].value == "expired")
    assert {_fill(cell) for cell in expired_row} == {"FFEBE9"}  # the whole row is flagged
    assert "Expired waivers are flagged as failures" in _texts(wb)

    trend = _table(wb["Trend"])
    assert [t["Run ID"] for t in trend][-1] == run.run_id
    assert [t["This run"] for t in trend] == ["No", "No", "No", "Yes"]
    assert trend[0]["Quality score"] == pytest.approx(0.8) and trend[1]["Quality score"] is None
    assert trend[-1]["Started (UTC)"] == datetime(2026, 11, 6, 9, 30)
    assert len(wb["Trend"]._charts) == 1


def test_excel_sign_off_sheet(run: RunResult, tmp_path: Path) -> None:
    wb = _render(run, tmp_path)
    ws = wb["Sign-off"]
    values = _pairs(ws)
    assert values["Project"] == "synthetic-migration" and values["Overall status"] == "error"
    assert values["approver"] == "QA Lead"  # run metadata
    rows = _rows(ws)
    header = next(
        i for i, row in enumerate(rows) if row[:4] == ("Role", "Approver", "Date", "Decision")
    )
    entries = rows[header + 1 : header + 6]
    assert [row[0] for row in entries] == ["Business owner", "Data owner", "QA lead", None, None]
    assert all(row[1:] == (None, None, None, None) for row in entries)  # left blank to fill in
    validations = ws.data_validations.dataValidation
    decision = next(v for v in validations if v.type == "list")
    assert decision.formula1 == '"Approved,Rejected,Conditional"'
    first, last = header + 2, header + 6
    assert str(decision.sqref) == f"D{first}:D{last}"
    date_rule = next(v for v in validations if v.type == "date")
    assert str(date_rule.sqref) == f"C{first}:C{last}"
    assert _fill(ws.cell(row=first, column=2)) == "FFFBE6"


def test_excel_without_trend_or_history(tmp_path: Path) -> None:
    plain = build_run().model_copy(update={"trend": [], "baseline": None, "waivers": []})
    wb = _render(plain, tmp_path)
    assert "Trend" not in wb.sheetnames
    values = _pairs(wb["Summary"])
    assert "Baseline run" not in values
    # Without run-level waiver records, the waivers are derived from the checks.
    waivers = _table(wb["Waivers"])
    assert [(w["Status"], w["Selector"]) for w in waivers] == [
        ("expired", "sales_migration.customers.row_count"),
        ("applied", "sales_migration.orders.accepted_values.*"),
    ]


def test_excel_empty_run(tmp_path: Path) -> None:
    empty = RunResult(
        run_id="empty",
        project="empty-project",
        status=Status.SKIPPED,
        started_at=datetime(2026, 11, 6, tzinfo=UTC),
        environment=RunEnvironment(dvf_version="0", python_version="3", platform="x"),
    )
    wb = _render(empty, tmp_path)
    texts = _texts(wb)
    for note in (
        "No tables in this run.",
        "No checks in this run.",
        "No schema comparisons in this run.",
        "No column mismatches in this run.",
        "No sample rows in this result.",
        "No waivers matched or were configured for this run.",
        "No failed, errored, or warning checks.",
    ):
        assert note in texts
    assert wb["Checks"].auto_filter.ref is None


def test_excel_never_writes_formulas_or_illegal_characters(run: RunResult, tmp_path: Path) -> None:
    hostile = run.model_copy(deep=True)
    check = hostile.suites[0].datasets[0].checks[0]
    hostile.suites[0].datasets[0].checks[0] = check.model_copy(
        update={
            "message": '=HYPERLINK("http://evil.test","click")',
            "owner": "#N/A",
            "tags": ["bell\x07", "nul\x00"],
            "samples": SampleSet(columns=["v"], rows=[["=1+1"], [float("nan")], [10**20]]),
        }
    )
    path = tmp_path / EXCEL_FILE
    render_excel(hostile, path, ReportOptions())
    wb = openpyxl.load_workbook(path)
    checks_ws = wb["Checks"]
    header = [c.value for c in checks_ws[1]]
    first = next(checks_ws.iter_rows(min_row=2, max_row=2))
    message = first[header.index("Message")]
    assert message.value == '=HYPERLINK("http://evil.test","click")'
    assert message.data_type == "s"  # text, not a formula
    owner = first[header.index("Owner")]
    assert owner.value == "#N/A" and owner.data_type == "s"
    assert first[header.index("Tags")].value == "bell, nul"
    samples = [c for row in wb["Samples"].iter_rows() for c in row if c.value is not None]
    formula_like = next(c for c in samples if c.value == "=1+1")
    assert formula_like.data_type == "s"
    values = [c.value for c in samples]
    assert "nan" in values and "100000000000000000000" in values
    for ws in wb:
        for row in ws.iter_rows():
            for cell in row:
                assert cell.data_type != "f"


def test_excel_value_conversions() -> None:
    assert excel_value(None) is None and excel_value(True) is True
    assert excel_value(Status.FAILED) == "failed"
    assert excel_value(12) == 12 and excel_value(10**16) == str(10**16)
    assert excel_value(float("inf")) == "inf"
    aware = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    assert excel_value(aware) == datetime(2026, 1, 1, 10)
    assert excel_value({"a": [1, None]}) == '{"a": [1, null]}'
    assert excel_value("x" * 5000).endswith("…") and len(excel_value("x" * 5000)) == 2000
    assert excel_value("tab\tok\x01") == "tab\tok"


def test_excel_caps_samples(run: RunResult, tmp_path: Path) -> None:
    wb = _render(run, tmp_path, max_samples_per_check=1)
    flat = [v for row in _rows(wb["Samples"]) for v in row if v is not None]
    assert "ann@example.test " in flat
    assert "BOB@EXAMPLE.TEST" not in flat and "<script>alert(1)</script>" not in flat
    assert "Showing 1 of 4 rows; more rows existed when the sample was captured." in flat
    none_shown = _render(run, tmp_path / "zero", max_samples_per_check=0)
    flat = [v for row in _rows(none_shown["Samples"]) for v in row if v is not None]
    assert "ann@example.test " not in flat and "customer_id" not in flat


def test_excel_include_passed_false(run: RunResult, tmp_path: Path) -> None:
    wb = _render(run, tmp_path, include_passed=False)
    ids = [row["Check ID"] for row in _table(wb["Checks"])]
    assert len(ids) == 8
    assert "sales_migration.orders.not_null.order_id" not in ids
    assert "3 passed checks are not listed" in _texts(wb)
    assert _pairs(wb["Summary"])["passed"] == 3  # still counted


def test_excel_caps_check_rows_keeping_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(excel, "MAX_CHECK_ROWS", 5)
    run = many_checks_run(12, status=Status.PASSED)
    checks = run.suites[0].datasets[0].checks
    checks[9] = checks[9].model_copy(update={"status": Status.FAILED})
    wb = _render(run, tmp_path)
    ids = [row["Check ID"] for row in _table(wb["Checks"])]
    assert len(ids) == 5
    assert "bulk.big_table.not_null.col_00010" in ids
    assert ids == sorted(ids)  # still in run order
    texts = _texts(wb)
    assert "Showing 5 of 12 listed checks (failures first)" in texts
    assert "The Checks sheet lists 5 checks; 7 more" in texts


def test_excel_scales(tmp_path: Path) -> None:
    started = time.perf_counter()
    wb = _render(many_checks_run(5000), tmp_path)
    assert time.perf_counter() - started < 60  # generous bound for slow CI machines
    assert wb["Checks"].max_row == 5001


def test_excel_requires_openpyxl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(DependencyError, match="openpyxl") as info:
        ExcelReporter()
    assert "dvf-framework[excel]" in (info.value.hint or "")
    with pytest.raises(DependencyError):
        render_excel(build_run(), tmp_path / EXCEL_FILE, ReportOptions())
    assert not (tmp_path / EXCEL_FILE).exists()


def test_excel_is_a_builtin_reporter(run: RunResult, tmp_path: Path) -> None:
    assert BUILTIN_REPORTERS["excel"] is ExcelReporter
    registry = Registry()
    for name, cls in BUILTIN_REPORTERS.items():
        registry.register("reporter", name, cls, origin="builtin")
    artifacts = render_reports(run, ["json", "excel"], tmp_path, ReportOptions(), registry)
    assert [Path(a.path).name for a in artifacts] == ["results.json", "report.xlsx"]


def test_excel_leaves_out_config_and_sql(run: RunResult, tmp_path: Path) -> None:
    texts = _texts(_render(run, tmp_path))
    assert "SELECT customer_id" not in texts  # query text
    assert "Traceback" not in texts
    assert "mssql" not in texts  # configuration snapshot
