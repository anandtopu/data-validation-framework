from __future__ import annotations

import base64
import json
import logging
import re
import struct
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import UTC, date, datetime
from pathlib import Path

import jsonschema
import pytest

from dvf.config.schema import results_json_schema
from dvf.core.enums import Dimension, Severity, Status
from dvf.core.errors import PluginError, UnknownPluginError
from dvf.models.results import (
    CheckResult,
    ColumnMismatch,
    DatasetPairResult,
    MetricComparison,
    RunEnvironment,
    RunResult,
    SuiteResult,
    TimingBreakdown,
)
from dvf.plugins.registry import Registry
from dvf.reporting import _common
from dvf.reporting.base import ReportOptions, ReportSection, render_reports
from dvf.reporting.builtin import BUILTIN_REPORTERS
from dvf.reporting.html import (
    DEFAULT_ACCENT,
    HtmlReporter,
    accent_color,
    artifact_href,
    logo_data_uri,
    render_html,
    text_color_on,
)
from dvf.reporting.json_report import JsonReporter, results_document
from dvf.reporting.junit import JUnitReporter, render_junit, xml_text
from dvf.reporting.markdown import (
    MAX_CHARS,
    MarkdownReporter,
    md_code,
    md_escape,
    render_markdown,
)
from tests.unit.reporting_fixtures import PIPE_MESSAGE, XSS, build_run, many_checks_run

ESCAPED_XSS = "&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.fixture
def run() -> RunResult:
    return build_run()


@pytest.fixture
def options() -> ReportOptions:
    return ReportOptions(metadata={"environment": "staging"})


def _all_ids(run: RunResult) -> list[str]:
    return [check.check_id for check in run.iter_checks()]


def _tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def _replace_check(run: RunResult, check_id: str, **updates: object) -> RunResult:
    """A deep copy of ``run`` with one check's fields replaced."""
    copy = run.model_copy(deep=True)
    for suite in copy.suites:
        for dataset in suite.datasets:
            for position, check in enumerate(dataset.checks):
                if check.check_id == check_id:
                    dataset.checks[position] = check.model_copy(update=updates)
                    return copy
    raise AssertionError(f"no check {check_id}")


def _empty_run() -> RunResult:
    return RunResult(
        run_id="empty",
        project="empty-project",
        status=Status.SKIPPED,
        started_at=datetime(2026, 11, 6, tzinfo=UTC),
        environment=RunEnvironment(dvf_version="0", python_version="3", platform="x"),
    )


# ---------------------------------------------------------------------- JSON


def test_json_round_trips_and_matches_the_schema(
    run: RunResult, options: ReportOptions, tmp_path: Path
) -> None:
    run = run.model_copy(update={"metadata": {**run.metadata, "owner": "Zoë Ünal"}})
    artifacts = JsonReporter().render(run, tmp_path / "out", options)

    assert [(a.kind, a.format) for a in artifacts] == [("report", "json")]
    path = Path(artifacts[0].path)
    assert path.name == "results.json"
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"}\n")
    text = raw.decode("utf-8")
    assert "Zoë Ünal" in text  # not ASCII-escaped
    assert text.startswith('{\n  "schema_version"')
    assert RunResult.model_validate_json(text) == run
    jsonschema.Draft202012Validator(results_json_schema()).validate(json.loads(text))


def test_json_replaces_non_finite_numbers(run: RunResult) -> None:
    broken = _replace_check(
        run, "sales_migration.customers.unique.customer_id", unexpected_pct=float("nan")
    )
    data = json.loads(results_document(broken))
    jsonschema.Draft202012Validator(results_json_schema()).validate(data)
    checks = [c for s in data["suites"] for d in s["datasets"] for c in d["checks"]]
    unique = next(c for c in checks if c["check_id"].endswith("unique.customer_id"))
    assert unique["unexpected_pct"] is None


# ---------------------------------------------------------------------- JUnit


def _parse_junit(text: str) -> ET.Element:
    return ET.fromstring(text.encode("utf-8"))  # noqa: S314 - our own output


def test_junit_totals_and_elements(run: RunResult, options: ReportOptions, tmp_path: Path) -> None:
    artifacts = JUnitReporter().render(run, tmp_path, options)
    path = Path(artifacts[0].path)
    assert path.name == "junit.xml" and artifacts[0].format == "junit"
    text = path.read_text(encoding="utf-8")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    root = _parse_junit(text)

    counts = _common.status_counts(run.iter_checks())
    assert root.tag == "testsuites"
    assert root.get("name") == "synthetic-migration"
    assert int(root.get("tests", "")) == sum(counts.values()) == 11
    assert int(root.get("failures", "")) == counts[Status.FAILED] == 3
    assert int(root.get("errors", "")) == counts[Status.ERROR] == 1
    assert int(root.get("skipped", "")) == counts[Status.SKIPPED] == 2
    assert root.get("time") == "3.000"

    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["sales_migration", "reference_data"]
    sales = suites[0]
    assert (sales.get("tests"), sales.get("failures"), sales.get("errors")) == ("8", "3", "1")
    assert sales.get("hostname") == "ci-runner-7"
    assert sales.get("timestamp") == "2026-11-06T09:30:00+00:00"
    props = {p.get("name"): p.get("value") for p in sales.iter("property")}
    assert props["run_id"] == run.run_id
    assert props["project"] == "synthetic-migration"
    assert props["quality_score"] == "50"

    cases = {case.get("name"): case for case in root.iter("testcase")}
    assert set(cases) == set(_all_ids(run))
    compare = cases["sales_migration.customers.column_compare"]
    assert compare.get("classname") == "sales_migration.customers"
    assert compare.get("time") == "2.000"
    failure = compare.find("failure")
    assert failure is not None
    assert failure.get("type") == "column_compare"
    assert failure.get("message") == PIPE_MESSAGE
    body = failure.text or ""
    for expected in ("Observed:", "Unexpected: 38 of 9,988", "whitespace_only: 12", "hint:"):
        assert expected in body
    assert "WHERE 1 < 2" in body  # repro SQL survives escaping

    row_count = cases["sales_migration.customers.row_count"].find("failure")
    assert row_count is not None and "row_count [region=EU]" in (row_count.text or "")

    error = cases["sales_migration.orders.aggregate_compare"].find("error")
    assert error is not None
    assert error.get("type") == "OperationalError"
    assert "Traceback" in (error.text or "") and "Hint:" in (error.text or "")

    assert len(list(root.iter("failure"))) == 3
    assert len(list(root.iter("error"))) == 1
    assert len(list(root.iter("skipped"))) == 2
    skipped = cases["reference_data.countries.freshness"].find("skipped")
    assert skipped is not None and skipped.get("message") == "No timestamp column configured."

    warning = cases["sales_migration.customers.unique.customer_id"].find("system-out")
    assert warning is not None and (warning.text or "").startswith("WARNING: 1 duplicate key")
    waived = cases["sales_migration.orders.accepted_values.status"]
    assert waived.find("failure") is None
    waived_out = waived.find("system-out")
    assert waived_out is not None
    assert (waived_out.text or "").startswith(
        "WAIVED: Legacy codes retired after cut-over, ticket MIG-101, expires 2026-12-31"
    )
    assert list(cases["sales_migration.orders.not_null.order_id"]) == []


def test_junit_strips_invalid_xml_characters(run: RunResult, options: ReportOptions) -> None:
    dirty = _replace_check(
        run,
        "sales_migration.customers.type_compatibility",
        message="bad\x00 value\x07 with\x1b escape\ufffe",
    )
    root = _parse_junit(render_junit(dirty, options))
    failure = next(
        case.find("failure")
        for case in root.iter("testcase")
        if case.get("name") == "sales_migration.customers.type_compatibility"
    )
    assert failure is not None
    assert failure.get("message") == "bad value with escape"
    assert "\x00" not in (failure.text or "") and "\x07" not in (failure.text or "")
    assert xml_text("a\ud800b\tc\nd") == "ab\tc\nd"
    assert xml_text(42) == "42"


def test_junit_fallbacks_without_error_or_waiver_details(
    run: RunResult, options: ReportOptions
) -> None:
    changed = _replace_check(run, "sales_migration.orders.aggregate_compare", error=None)
    changed = _replace_check(changed, "sales_migration.orders.accepted_values.status", waiver=None)
    many_columns = [
        ColumnMismatch(column=f"c{n}", mismatch_count=n, mismatch_pct=0.1) for n in range(12)
    ]
    many_metrics = [
        MetricComparison(metric="row_count", group={"g": n}, passed=False) for n in range(22)
    ]
    changed = _replace_check(
        changed,
        "sales_migration.customers.type_compatibility",
        column_mismatches=many_columns,
        metrics=many_metrics,
    )
    root = _parse_junit(render_junit(changed, options))
    cases = {case.get("name"): case for case in root.iter("testcase")}
    error = cases["sales_migration.orders.aggregate_compare"].find("error")
    assert error is not None
    assert error.get("type") == "Error" and error.get("message") == "Target query failed."
    waived = cases["sales_migration.orders.accepted_values.status"].find("system-out")
    assert waived is not None and waived.text == "WAIVED: 2 unexpected status codes."
    failure = cases["sales_migration.customers.type_compatibility"].find("failure")
    assert failure is not None
    assert "2 more columns" in (failure.text or "") and "2 more metrics" in (failure.text or "")


def test_junit_for_an_empty_run(options: ReportOptions) -> None:
    root = _parse_junit(render_junit(_empty_run(), options))
    assert root.get("tests") == "0" and root.findall("testsuite") == []
    assert root.get("time") == "0.000"


# ---------------------------------------------------------------------- HTML


def test_html_report_contents(run: RunResult, options: ReportOptions, tmp_path: Path) -> None:
    artifacts = HtmlReporter().render(run, tmp_path / "nested" / "dir", options)
    path = Path(artifacts[0].path)
    assert path.name == "report.html" and artifacts[0].format == "html"
    html = path.read_text(encoding="utf-8")
    assert html == render_html(run, options)  # streamed output equals the rendered text
    assert html.startswith("<!DOCTYPE html>")

    for check_id in _all_ids(run):
        assert check_id in html
    for name in ("whitespace_only", "case_only", "numeric_rounding", "timezone_shift"):
        assert name in html
    for heading in (
        "Executive summary",
        "Top issues",
        "Schema diff",
        "Column mismatch matrix",
        "Column mismatches",
        "Profile comparison",
        "Metric comparisons",
        "Reproducibility",
        "Configuration snapshot",
    ):
        assert heading in html
    assert 'class="compat compat-lossy"' in html
    assert 'class="compat compat-incompatible"' in html
    assert "scale 4 -&gt; 2 loses digits" in html
    assert "null_count 4 -&gt; 0" in html  # profile differences
    assert 'class="num diff"' in html  # differing profile cells are highlighted
    assert "region=EU" in html and "missing recent rows" in html  # grouped metrics
    assert "<svg" in html and 'class="bar-failed"' in html
    # header, metadata, and reproducibility details
    for text in (
        "20261106T093000Z-abc123",
        "staging",
        "R-2026.11",
        "0123456789abcdef0123456789abcdef01234567",
        "sha256:feedface",
        "dvf-acme",
        "sqlglot",
        "dvf.ci.yaml",
        "run --profile ci --set reports.include_passed=true",
        "ci-runner-7",
        "Linux-6.8-x86_64",
    ):
        assert text in html
    # every check field family
    for text in (
        "OperationalError",
        "Check the target table name",
        "Traceback (most recent call last)",
        "Legacy codes retired after cut-over",
        "MIG-101",
        "qa.lead",
        "trim: rtrim",
        "dw.dim_customer",
        "comparison_mode",
        "row_hash",
        "DQ-42",
        "nested",
        "data-qa@example.test",
        "Source query",
        "region=EU",
    ):
        assert text in html
    assert 'href="extracts/customers.mismatches.parquet"' in html
    assert html.count('<details class="check ') == len(_all_ids(run))


def test_html_is_self_contained(run: RunResult, options: ReportOptions) -> None:
    html = render_html(run, options)
    lowered = html.lower()
    assert "<script src" not in lowered
    assert "<link" not in lowered
    assert "http://" not in lowered and "https://" not in lowered
    assert "@import" not in lowered and "url(" not in lowered
    assert lowered.count("<script") == 1


def test_html_escapes_user_data(run: RunResult, options: ReportOptions) -> None:
    html = render_html(run, options)
    assert XSS not in html
    assert ESCAPED_XSS in html
    assert "javascript:alert(2)" in html
    assert 'href="javascript:' not in html
    assert "a|b" in html
    assert "38 rows differ | see side-by-side samples" in html


def test_html_include_passed_false(run: RunResult) -> None:
    html = render_html(run, ReportOptions(include_passed=False))
    passed = [c.check_id for c in run.iter_checks() if c.status is Status.PASSED]
    assert len(passed) == 3
    for check_id in passed:
        assert f'data-id="{check_id}"' not in html
    assert html.count('<details class="check ') == len(_all_ids(run)) - 3
    assert "3 passed checks omitted" in html
    assert '<div class="l">passed</div><div class="v">3</div>' in html  # still counted


def test_html_template_override(run: RunResult, tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "_summary.html.j2").write_text(
        '<section id="summary">CUSTOM SUMMARY for {{ run.run_id }} {{ title }}</section>\n',
        encoding="utf-8",
    )
    html = render_html(run, ReportOptions(templates_dir=templates, title="Brand <b>X</b>"))
    assert "CUSTOM SUMMARY for 20261106T093000Z-abc123 Brand &lt;b&gt;X&lt;/b&gt;" in html
    assert "Executive summary" not in html
    assert "Schema diff" in html  # other sections still come from the package


def test_html_logo_and_branding(run: RunResult, tmp_path: Path) -> None:
    logo = tmp_path / "logo.png"
    png = _tiny_png()
    logo.write_bytes(png)
    html = render_html(run, ReportOptions(logo=logo, primary_color="#FFCC00", title="Acme QA"))
    assert f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}" in html
    assert "--accent: #FFCC00;" in html
    assert "--accent-text: #111827;" in html  # dark text on a light accent
    assert "<h1>Acme QA</h1>" in html

    missing = render_html(run, ReportOptions(logo=tmp_path / "missing.png"))
    assert "data:image" not in missing and 'class="logo"' not in missing


def test_logo_data_uri_variants(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert logo_data_uri(None) is None
    sniffed = tmp_path / "logo"
    sniffed.write_bytes(_tiny_png())
    assert (logo_data_uri(sniffed) or "").startswith("data:image/png;base64,")
    svg = tmp_path / "logo.bin"
    svg.write_bytes(b'<?xml version="1.0"?>\n<svg xmlns="x"></svg>')
    assert (logo_data_uri(svg) or "").startswith("data:image/svg+xml;base64,")
    webp = tmp_path / "logo.img"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert (logo_data_uri(webp) or "").startswith("data:image/webp;base64,")
    with caplog.at_level(logging.WARNING, logger="dvf.reporting.html"):
        text = tmp_path / "notes.txt"
        text.write_text("not an image", encoding="utf-8")
        assert logo_data_uri(text) is None
        big = tmp_path / "big.png"
        big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024))
        assert logo_data_uri(big) is None
    assert "not a recognized image" in caplog.text and "larger than" in caplog.text
    assert logo_data_uri(tmp_path) is None  # a folder cannot be read


def test_accent_color_validation(caplog: pytest.LogCaptureFixture) -> None:
    assert accent_color("#123abc") == "#123abc"
    assert accent_color("#abc") == "#abc"
    assert accent_color(None) == DEFAULT_ACCENT
    with caplog.at_level(logging.WARNING, logger="dvf.reporting.html"):
        assert accent_color("red;} body {display:none") == DEFAULT_ACCENT
    assert "Ignoring invalid primary_color" in caplog.text
    assert text_color_on("#000000") == "#ffffff"
    assert text_color_on("#fff") == "#111827"


def test_html_rejects_css_injection_in_color(run: RunResult) -> None:
    html = render_html(run, ReportOptions(primary_color="#fff;}body{display:none"))
    assert "display:none" not in html
    assert f"--accent: {DEFAULT_ACCENT};" in html


def test_html_extra_sections_are_escaped(run: RunResult) -> None:
    section = ReportSection(title="Sign-off <notes>", content="Approved by **QA**\n<b>not bold</b>")
    html = render_html(run, ReportOptions(extra_sections=(section,)))
    assert "Sign-off &lt;notes&gt;" in html
    assert "Approved by **QA**\n&lt;b&gt;not bold&lt;/b&gt;" in html
    assert 'href="#extra-1"' in html


def test_html_caps_sample_rows(run: RunResult) -> None:
    html = render_html(run, ReportOptions(max_samples_per_check=1))
    assert "Showing 1 of 4 sample rows (capped at 1 per check)" in html
    assert "ann@example.test" in html and "BOB@EXAMPLE.TEST" not in html
    assert ESCAPED_XSS not in html  # the payload is in the third sample row
    none_shown = render_html(run, ReportOptions(max_samples_per_check=0))
    assert "Showing 0 of 4 sample rows (capped at 0 per check)" in none_shown
    assert 'class="data samples"' not in none_shown


def test_html_for_an_empty_run() -> None:
    html = render_html(_empty_run(), ReportOptions())
    assert "No suites in this run." in html
    assert "No checks to show." in html
    assert "No failed, errored, or warning checks." in html


def test_html_scales_to_thousands_of_checks() -> None:
    big = many_checks_run(2000)
    started = time.perf_counter()
    html = render_html(big, ReportOptions())
    elapsed = time.perf_counter() - started
    assert html.count('<details class="check ') == 2000
    assert elapsed < 30  # generous bound for slow CI machines
    assert len(html) < 20_000_000


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("extracts/a b#1.csv", "extracts/a%20b%231.csv"),
        ("extracts\\win.csv", "extracts/win.csv"),
        ("C:\\reports\\x.csv", "file:///C:/reports/x.csv"),
        ("\\\\server\\share\\x y.csv", "file://server/share/x%20y.csv"),
        ("/var/reports/x.csv", "file:///var/reports/x.csv"),
        ("javascript:alert(1)", None),
        ("https://example.test/x", None),
        ("", None),
    ],
)
def test_artifact_href(path: str, expected: str | None) -> None:
    assert artifact_href(path) == expected


# ---------------------------------------------------------------------- Markdown


def test_markdown_summary(run: RunResult, options: ReportOptions, tmp_path: Path) -> None:
    section = ReportSection(title="Release notes", content="- item **one**")
    opts = ReportOptions(metadata={"environment": "staging"}, extra_sections=(section,))
    artifacts = MarkdownReporter().render(run, tmp_path, opts)
    path = Path(artifacts[0].path)
    assert path.name == "summary.md" and artifacts[0].format == "markdown"
    text = path.read_text(encoding="utf-8")

    assert text.startswith("# synthetic-migration: ERROR\n")
    assert "**Quality score:** 50%" in text
    assert "3 passed, 1 warning, 3 failed, 1 error, 2 skipped, 1 waived" in text
    assert "**Tables:** 4" in text and "**Rows compared:** 60,238" in text
    assert "**environment:** staging · **release:** R-2026.11" in text
    assert "| Status | Checks |" in text and "| failed | 3 |" in text
    assert "| Suite | Table | Status | Source rows | Target rows | Missing |" in text
    assert (
        "| sales\\_migration | customers | **FAILED** | 10,000 | 9,988 | 12 | 0 | 38 | 99.5% |"
        in (text)
    )
    assert "## Top failures" in text
    top = text.split("## Top failures", 1)[1].split("<details>", 1)[0]
    assert "1. **FAILED** · critical · `sales_migration.customers.column_compare`" in top
    assert "whitespace\\_only 12, case\\_only 3" in top
    assert "timezone\\_shift 10" in top and "numeric\\_rounding 8" in top
    assert "Hint: 80% of mismatches are trailing spaces" in top
    assert 'Observed {"mismatched\\_rows": 38}' in top
    assert "Error: OperationalError" in top and "· Hint: Check the target table" in top
    assert "unique.customer_id" not in top  # warnings are not failures
    assert "38 rows differ \\| see side-by-side samples" in text
    assert PIPE_MESSAGE not in text  # the raw pipe would split a table cell
    assert "<details>" in text and "<summary>All non-passed checks (8)</summary>" in text
    assert "Waived: Legacy codes retired after cut-over (MIG-101)" in text
    assert "## Release notes\n\n- item **one**" in text
    assert XSS not in text
    assert len(text) < MAX_CHARS


def test_markdown_truncates_many_checks() -> None:
    text = render_markdown(many_checks_run(3000), ReportOptions())
    assert len(text) <= MAX_CHARS
    match = re.search(r"… (\d+) more checks omitted; see the HTML or JSON report\.", text)
    assert match is not None and int(match.group(1)) > 0
    assert text.rstrip().endswith("</sub>")


def test_markdown_caps_tables_and_hard_limit() -> None:
    datasets = [DatasetPairResult(name=f"table_{n:03d}", status=Status.PASSED) for n in range(150)]
    datasets[120] = DatasetPairResult(name="broken_table", status=Status.FAILED)
    run = RunResult(
        run_id="wide",
        project="wide",
        status=Status.FAILED,
        started_at=datetime(2026, 11, 6, tzinfo=UTC),
        environment=RunEnvironment(dvf_version="0", python_version="3", platform="x"),
        suites=[SuiteResult(name="s", status=Status.FAILED, datasets=datasets)],
    )
    text = render_markdown(run, ReportOptions())
    assert "… 50 more tables omitted" in text
    assert "broken\\_table" in text.split("## Top failures")[0]  # worst tables are kept
    assert "No failed or errored checks." in text
    assert "<details>" not in text

    sections = tuple(ReportSection(f"S{n}", "x" * 9000) for n in range(10))
    huge = render_markdown(run, ReportOptions(extra_sections=sections))
    assert len(huge) <= MAX_CHARS
    assert "section truncated" in huge
    assert huge.rstrip().endswith("summary truncated; see the HTML or JSON report.")


def test_markdown_details_budget_exhausted() -> None:
    run = many_checks_run(5)
    plain = render_markdown(run, ReportOptions())
    head_len = len(plain.split("<details>")[0])
    footer_len = len(plain) - plain.rfind("<sub>")
    # Eight sections that leave about 150 characters: too few for the details table.
    size = (MAX_CHARS - head_len - footer_len - 150) // 8 - len("## S0") - 4
    sections = tuple(ReportSection(f"S{n}", "y" * size) for n in range(8))
    text = render_markdown(run, ReportOptions(extra_sections=sections))
    assert "… 5 more checks omitted; see the HTML or JSON report." in text
    assert "<details>" not in text and "summary truncated" not in text
    assert len(text) <= MAX_CHARS


def test_markdown_top_failure_limits(run: RunResult) -> None:
    columns = [
        ColumnMismatch(
            column=f"col{n}",
            mismatch_count=10 - n,
            mismatch_pct=1.0,
            hints=["h1", "h2", "h3", "h4"],
        )
        for n in range(7)
    ]
    metrics = [
        MetricComparison(metric="sum", column="amount", group={"g": n}, passed=False)
        for n in range(7)
    ]
    changed = _replace_check(
        run,
        "sales_migration.customers.type_compatibility",
        column_mismatches=columns,
        metrics=metrics,
        severity=Severity.CRITICAL,
        unexpected_count=10**6,
    )
    text = render_markdown(changed, ReportOptions())
    assert "- … 2 more columns" in text
    assert "- … 2 more failing metrics" in text
    assert "Metric sum(amount) \\[g=0\\]" in text
    assert text.count("Hint: h4") == 0  # hints are capped per column


def test_markdown_escaping_helpers() -> None:
    assert md_escape("a|b\nc*d_e<f>&[g]") == "a\\|b c\\*d\\_e\\<f\\>\\&\\[g\\]"
    assert md_escape("x" * 300).endswith("…") and len(md_escape("x" * 300)) == 200
    assert md_escape(None) == "—"
    assert md_code("a`b") == "``a`b``"
    assert md_code("`edge`") == "`` `edge` ``"
    assert md_code("a|b", table=True) == "`a\\|b`"
    assert md_code("") == ""


def test_markdown_for_an_empty_run() -> None:
    text = render_markdown(_empty_run(), ReportOptions())
    assert "**Checks:** none" in text
    assert "## Tables" not in text
    assert "No failed or errored checks." in text


# ---------------------------------------------------------------------- render_reports and registry


def _registry() -> Registry:
    registry = Registry()
    for name, cls in BUILTIN_REPORTERS.items():
        registry.register("reporter", name, cls, origin="builtin")
    return registry


def test_builtin_reporters_map() -> None:
    assert set(BUILTIN_REPORTERS) == {"json", "html", "junit", "markdown"}
    for name, cls in BUILTIN_REPORTERS.items():
        assert cls.format == name
        assert cls.__doc__


def test_render_reports_writes_every_format(
    run: RunResult, options: ReportOptions, tmp_path: Path
) -> None:
    out = tmp_path / "reports" / "run-1"
    artifacts = render_reports(
        run, ["json", "html", "junit", "markdown"], out, options, _registry()
    )
    assert [Path(a.path).name for a in artifacts] == [
        "results.json",
        "report.html",
        "junit.xml",
        "summary.md",
    ]
    assert all(Path(a.path).is_file() and a.kind == "report" for a in artifacts)


def test_render_reports_rejects_declared_and_unknown_formats(
    run: RunResult, options: ReportOptions, tmp_path: Path
) -> None:
    registry = _registry()
    registry.declare("reporter", "pdf", "PDF")
    out = tmp_path / "never"
    with pytest.raises(PluginError, match="declared but not implemented"):
        render_reports(run, ["json", "pdf"], out, options, registry)
    assert not out.exists()  # nothing is written before every format is resolved
    with pytest.raises(UnknownPluginError):
        render_reports(run, ["htm"], out, options, registry)


# ---------------------------------------------------------------------- shared helpers


def _plain_check(check_id: str, status: Status, **fields: object) -> CheckResult:
    data: dict[str, object] = {
        "check_id": check_id,
        "name": check_id,
        "type": "custom",
        "dimension": Dimension.VALIDITY,
        "severity": Severity.ERROR,
        "status": status,
        **fields,
    }
    return CheckResult.model_validate(data)


def test_formatting_helpers() -> None:
    assert _common.format_duration(None) == "—"
    assert _common.format_duration(12.4) == "12 ms"
    assert _common.format_duration(1500) == "1.50 s"
    assert _common.format_duration(125_000) == "2 min 5 s"
    assert _common.format_duration(3_725_000) == "1 h 2 min"
    assert _common.format_pct(None) == "—"
    assert _common.format_pct(float("nan")) == "—"
    assert _common.format_pct(0.001) == "<0.01%"
    assert _common.format_pct(99.999) == ">99.99%"
    assert _common.format_pct(100.0) == "100%"
    assert _common.format_pct(12.5) == "12.5%"
    assert _common.format_count(1234567) == "1,234,567"
    assert _common.format_count(2.5) == "2.50"
    assert _common.format_count(None) == "—"
    assert _common.format_signed(5) == "+5" and _common.format_signed(-5) == "-5"
    assert _common.format_signed(None) == "—"
    assert _common.format_value(True) == "true"
    assert _common.format_value(Status.FAILED) == "failed"
    assert _common.format_value(3.0) == "3" and _common.format_value(0.1) == "0.1"
    assert _common.format_value(date(2026, 1, 2)) == "2026-01-02"
    assert _common.format_value({"a": [1, None]}) == '{"a": [1, null]}'
    assert _common.format_value(b"x") == "b'x'"
    assert _common.format_value("abcdef", limit=4) == "abc…"
    assert _common.format_value(None, none="NULL") == "NULL"
    assert _common.format_datetime(None) == "—"
    assert _common.format_group(None) == "—"
    assert _common.format_group({"region": "EU", "code": None}) == "region=EU, code=NULL"
    assert _common.truncate("abc", None) == "abc"
    assert _common.sorted_classifications({"b": 2, "a": 2, "c": 5}) == [
        ("c", 5),
        ("a", 2),
        ("b", 2),
    ]


def test_scores_timings_and_issue_order() -> None:
    checks = [
        _plain_check("a", Status.PASSED, dimension=Dimension.COMPLETENESS),
        _plain_check("b", Status.FAILED, dimension=Dimension.COMPLETENESS),
        _plain_check("c", Status.WAIVED, dimension=Dimension.COMPLETENESS),
        _plain_check("d", Status.SKIPPED, dimension=Dimension.UNIQUENESS),
        _plain_check(
            "e",
            Status.WARNING,
            severity=Severity.CRITICAL,
            timing=TimingBreakdown(source_query_ms=5, total_ms=10),
        ),
        _plain_check("f", Status.ERROR, severity=Severity.INFO),
        _plain_check("g", Status.FAILED, severity=Severity.CRITICAL, unexpected_count=5),
        _plain_check("h", Status.FAILED, severity=Severity.CRITICAL, unexpected_count=50),
    ]
    scores = {s.dimension: s for s in _common.dimension_scores(checks)}
    assert list(scores) == [Dimension.COMPLETENESS, Dimension.UNIQUENESS, Dimension.VALIDITY]
    assert scores[Dimension.COMPLETENESS].score == pytest.approx(66.67)
    assert scores[Dimension.UNIQUENESS].score is None
    assert scores[Dimension.UNIQUENESS].total == 1

    timing = _common.sum_timings(checks)
    assert timing is not None and timing.total_ms == 10 and timing.source_query_ms == 5
    assert _common.sum_timings(checks[:2]) is None

    dataset = DatasetPairResult(name="t", status=Status.FAILED, checks=checks)
    run = RunResult(
        run_id="r",
        project="p",
        status=Status.FAILED,
        started_at=datetime(2026, 11, 6, tzinfo=UTC),
        environment=RunEnvironment(dvf_version="0", python_version="3", platform="x"),
        suites=[SuiteResult(name="s", status=Status.FAILED, datasets=[dataset])],
    )
    top = _common.top_issues(_common.iter_entries(run), limit=4)
    assert [e.check.check_id for e in top] == ["h", "g", "b", "f"]
    assert _common.quality_score(run) == pytest.approx(33.33)  # (a, c) of (a, b, c, e, g, h)
    assert _common.rows_per_second(run) is None
    assert _common.headline(checks[0]) == "a"
    with_rows = run.model_copy(
        update={
            "duration_ms": 2000.0,
            "summary": run.summary.model_copy(update={"rows_compared": 10}),
        }
    )
    assert _common.rows_per_second(with_rows) == 5.0
