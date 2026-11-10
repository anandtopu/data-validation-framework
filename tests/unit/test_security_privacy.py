"""Privacy: PII pattern detection, masking modes, strict mode, per-check sampling (synthetic data)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pydantic import SecretStr

from dvf import api
from dvf.config.models import PrivacyConfig
from dvf.core.enums import Dimension, Severity, Side, Status
from dvf.models.results import (
    CheckResult,
    ColumnMismatch,
    ColumnProfile,
    MetricComparison,
    QueryRecord,
    RunResult,
    SampleSet,
)
from dvf.scaffold.seeded import load_duckdb
from dvf.security.masking import (
    Privacy,
    detect_pii,
    hide_profile,
    hide_profile_differences,
    scrub_sql_literals,
    strict_check_result,
)
from dvf.security.redaction import MASK

# ====================================================================== detection


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("alice.smith@example.com", "email"),
        ("+44 20 7946 0958", "phone"),
        ("(555) 123-4567", "phone"),
        ("555-123-4567", "phone"),
        ("123-45-6789", "national_id"),
        ("AB 12 34 56 C", "national_id"),
        ("2345 6789 0123", "national_id"),
        ("4111 1111 1111 1111", "payment_card"),
        ("5500-0000-0000-0004", "payment_card"),
        ("GB82 WEST 1234 5698 7654 32", "iban"),
        ("DE89370400440532013000", "iban"),
        # not personal data
        ("bob@x", None),
        ("2026-11-08", None),
        ("08/11/2026", None),
        ("1,234,567.89", None),
        ("4111111111111112", None),  # fails the Luhn check
        ("5551234567", None),  # bare digits: an ID, not a phone number
        ("ORDER-2024-001", None),
        ("192.168.100.200", None),
        ("000-12-3456", None),
        ("short", None),
    ],
)
def test_detect_pii(value: str, kind: str | None) -> None:
    assert detect_pii(value) == kind


# ====================================================================== masking modes


def test_mask_mode_keeps_hints_only() -> None:
    privacy = Privacy(pii_columns=("*name*",))
    assert privacy.mask("FULL_NAME", "Zebulon Quixote") == MASK
    assert privacy.mask("contact", "alice.smith@example.com") == "a***@example.com"
    assert privacy.mask("contact", "+44 20 7946 0958") == f"{MASK}58"
    assert privacy.mask("pan", "4111 1111 1111 1111") == f"{MASK}1111"
    assert privacy.mask("ssn", "123-45-6789") == MASK
    assert privacy.mask("note", "plain text") == "plain text"
    assert privacy.mask("n", 42) == 42 and privacy.mask("n", None) is None
    off = Privacy(detect_patterns=False)
    assert off.mask("contact", "alice.smith@example.com") == "alice.smith@example.com"
    disabled = Privacy(mask_pii=False, pii_columns=("*name*",))
    assert disabled.mask("full_name", "Zebulon Quixote") == "Zebulon Quixote"


def test_hash_mode_is_keyed_and_consistent() -> None:
    keyed = Privacy(masking="hash", hash_key=b"synthetic-key", pii_columns=("email",))
    first = keyed.mask("email", "alice.smith@example.com")
    assert first.startswith("hash:") and len(first) == 21
    assert keyed.mask("email", "alice.smith@example.com") == first
    assert keyed.mask("email", "bob.jones@example.com") != first
    assert Privacy(masking="hash", hash_key=b"other").mask("x", "alice.smith@example.com") != first
    assert (
        Privacy(masking="hash", hash_key=b"synthetic-key").mask("x", "alice.smith@example.com")
        == first
    )
    unkeyed = Privacy(masking="hash")
    assert unkeyed.mask("x", "alice.smith@example.com") != first  # random per-process key


def test_tokenize_mode() -> None:
    privacy = Privacy(masking="tokenize", pii_columns=("name",))
    a, b = privacy.mask("name", "Zebulon Quixote"), privacy.mask("name", "Ada Lovelace")
    assert a.startswith("tok_") and b.startswith("tok_") and a != b
    assert privacy.mask("name", "Zebulon Quixote") == a
    rows = privacy.mask_rows(["name", "id"], [["Zebulon Quixote", 1], [None, 2]])
    assert rows == [[a, 1], [None, 2]]


def test_privacy_from_config() -> None:
    config = PrivacyConfig(
        mode="strict",
        pii_columns=["email"],
        masking="hash",
        detect_patterns=False,
        hash_key=SecretStr("synthetic-hash-key"),
    )
    privacy = Privacy.from_config(config, ["full_name"])
    assert privacy.strict and privacy.masking == "hash" and not privacy.detect_patterns
    assert privacy.hash_key == b"synthetic-hash-key"
    assert privacy.is_pii("FULL_NAME") and privacy.is_pii("email")
    assert "synthetic-hash-key" not in repr(privacy)
    with pytest.raises(ValueError, match="masking"):
        PrivacyConfig.model_validate({"masking": "encrypt"})


# ====================================================================== strict mode helpers


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT * FROM t WHERE \"k\" IN ('alice@example.com', 'bob''s')",
            "SELECT * FROM t WHERE \"k\" IN ('***', '***')",
        ),
        ('WHERE ("a" = 101 AND "b" >= -5.5)', 'WHERE ("a" = 0 AND "b" >= -0)'),
        ("WHERE id IN (1, 2, 3)", "WHERE id IN (0, 0, 0)"),
        ("CAST(x AS DECIMAL(38, 10)) LIMIT 50", "CAST(x AS DECIMAL(38, 10)) LIMIT 50"),
        ("SELECT \"it's\", `a'b`, [c'd] FROM t", "SELECT \"it's\", `a'b`, [c'd] FROM t"),
        ("WHERE n = N'Zoë' OR m = E'x'", "WHERE n = '***' OR m = '***'"),
    ],
)
def test_scrub_sql_literals(sql: str, expected: str) -> None:
    assert scrub_sql_literals(sql) == expected


def _result(**fields: Any) -> CheckResult:
    base: dict[str, Any] = {
        "check_id": "s.t.compare.column_mismatch",
        "name": "column_mismatch",
        "type": "compare.column_mismatch",
        "dimension": Dimension.ACCURACY,
        "severity": Severity.ERROR,
        "status": Status.FAILED,
    }
    return CheckResult(**{**base, **fields})


def test_strict_check_result_removes_raw_values() -> None:
    samples = SampleSet(columns=["email"], rows=[["alice.smith@example.com"]])
    result = _result(
        samples=samples,
        observed="2026-11-08T10:00:00",
        queries=[
            QueryRecord(
                side=Side.SOURCE,
                sql="SELECT * FROM t WHERE k IN ('alice.smith@example.com')",
                parameters={"attempts": 2, "keys": ["alice.smith@example.com"]},
            )
        ],
        metrics=[
            MetricComparison(
                metric="max", column="email", source="zed@example.com", target="z@x", passed=False
            ),
            MetricComparison(metric="sum", column="amount", source=10, target=11, passed=False),
        ],
        column_mismatches=[
            ColumnMismatch(column="email", mismatch_count=1, mismatch_pct=50.0, samples=samples)
        ],
    )
    strict = strict_check_result(result)
    text = strict.model_dump_json()
    assert "alice.smith@example.com" not in text and "zed@example.com" not in text
    assert strict.samples is None and strict.observed == MASK
    assert strict.queries[0].parameters == {"attempts": 2}
    assert [(m.source, m.target) for m in strict.metrics] == [(MASK, MASK), (10, 11)]
    assert strict.column_mismatches[0].samples is None
    numeric = _result(observed=3)
    assert strict_check_result(numeric) is numeric
    scalar = _result(
        observed="Zebulon", message="the query returned 'Zebulon', which is not a number"
    )
    assert (
        strict_check_result(scalar).message == f"the query returned {MASK}, which is not a number"
    )


def test_hide_profile() -> None:
    profile = ColumnProfile(count=2, null_count=0, min="alice@example.com", max="zed@example.com")
    hidden = hide_profile(profile)
    assert hidden is not None and hidden.min == MASK and hidden.max == MASK and hidden.count == 2
    assert hide_profile(None) is None
    assert hide_profile_differences(["min: a vs b", "null count: 1 vs 2", "max: y vs z"]) == [
        "min: differs (values hidden)",
        "null count: 1 vs 2",
        "max: differs (values hidden)",
    ]


# ====================================================================== end to end

SOURCE = pa.table(
    {
        "customer_key": [
            "alice.smith@example.com",
            "bob.jones@example.com",
            "carol.white@example.com",
        ],
        "full_name": ["Zebulon Quixote", "Ada Lovelace", "Grace Hopper"],
        "phone": ["+44 20 7946 0958", "+44 20 7946 0001", "+44 20 7946 0002"],
        "amount": [10, 20, 30],
    }
)
TARGET = pa.table(
    {
        "customer_key": [
            "alice.smith@example.com",
            "bob.jones@example.com",
            "dave.black@example.com",
        ],
        "full_name": ["Zebulon Quixotte", "Ada Lovelace", "Dave Black"],
        "phone": ["+44 20 7946 0999", "+44 20 7946 0001", "+44 20 7946 0003"],
        "amount": [10, 25, 40],
    }
)
RAW_VALUES = [
    "alice.smith@example.com",
    "bob.jones@example.com",
    "carol.white@example.com",
    "dave.black@example.com",
    "Zebulon",
    "Quixot",
    "Lovelace",
    "Grace Hopper",
    "Dave Black",
    # Every phone value, but not a bare "7946": four digits also occur by chance in timings
    # and hashes in the reports.
    "20 7946",
]


def _privacy_config(privacy: dict[str, Any], **table: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "privacy"},
        "defaults": {"privacy": privacy},
        "connections": {
            "src": {"type": "duckdb", "database": "src.duckdb"},
            "tgt": {"type": "duckdb", "database": "tgt.duckdb"},
        },
        "suites": [
            {
                "name": "crm",
                "source": "src",
                "target": "tgt",
                "tables": [
                    {
                        "name": "customers",
                        "source": {"table": "customers"},
                        "target": {"table": "customers"},
                        "keys": ["customer_key"],
                        "comparison": {
                            "strategies": ["row_count", "aggregate", "profile", "full_diff"],
                        },
                        **table,
                    }
                ],
            }
        ],
        "reports": {
            "formats": ["json", "html", "markdown", "junit"],
            "output_dir": "out/${run.id}",
            "extracts": {"enabled": True, "format": "csv"},
        },
        "history": {"enabled": False},
    }


def _seed(tmp_path: Path) -> None:
    load_duckdb({"customers": SOURCE}, tmp_path / "src.duckdb")
    load_duckdb({"customers": TARGET}, tmp_path / "tgt.duckdb")


def _files_text(folder: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.suffix in (".json", ".html", ".md", ".xml", ".sql", ".yaml", ".csv")
    )


@pytest.mark.parametrize("level", ["project", "suite"])
def test_strict_mode_writes_no_raw_values_anywhere(tmp_path: Path, level: str) -> None:
    _seed(tmp_path)
    config = _privacy_config({"mode": "strict"})
    if level == "suite":
        config["defaults"] = {}
        config["suites"][0]["defaults"] = {"privacy": {"mode": "strict"}}
    loaded = api.load_config_data(config, base_dir=tmp_path)
    output = api.run(loaded, run_id="strict-run")
    result: RunResult = output.result
    assert any(c.status is Status.FAILED for c in result.iter_checks())
    out = tmp_path / "out" / "strict-run"
    assert (out / "results.json").exists() and (out / "report.html").exists()
    assert not (out / "extracts").exists()
    assert list(out.rglob("*.sql"))  # repro SQL is written, without literals
    text = _files_text(out) + _files_text(tmp_path / ".dvf")
    leaked = [value for value in RAW_VALUES if value in text]
    assert leaked == []
    for check in result.iter_checks():
        assert check.samples is None
        assert all(c.samples is None for c in check.column_mismatches)


def test_standard_mode_hides_pii_column_extremes(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _privacy_config({"pii_columns": ["full_name", "amount"], "masking": "tokenize"})
    result = api.run(api.load_config_data(config, base_dir=tmp_path), write_reports=False).result
    dataset = result.suites[0].datasets[0]
    profiles = {p.column: p for p in dataset.profiles}
    amount = profiles["amount"]
    assert amount.source is not None and amount.source.min == MASK and amount.source.max == MASK
    assert all("30" not in d and "40" not in d for d in amount.differences)
    config = _privacy_config({"pii_columns": ["full_name"], "masking": "tokenize"})
    result = api.run(api.load_config_data(config, base_dir=tmp_path), write_reports=False).result
    profiles = {p.column: p for p in result.suites[0].datasets[0].profiles}
    assert profiles["amount"].source is not None and profiles["amount"].source.max == 30
    checks = {c.check_id.split(".", 2)[2]: c for c in result.iter_checks()}
    mismatch = {m.column: m for m in checks["compare.column_mismatch"].column_mismatches}
    rows = mismatch["full_name"].samples.rows if mismatch["full_name"].samples else []
    flat = [str(v) for row in rows for v in row]
    assert not any("Zebulon" in v or "@example.com" in v for v in flat)
    assert len(flat) == 4 and all(v.startswith("tok_") for v in flat[:3])  # key, source, target
    assert flat[1] != flat[2]  # different values get different tokens


def test_per_check_sample_size_disables_samples(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _privacy_config({}, comparison={"enabled": False}, schema_checks=[])
    config["suites"][0]["tables"][0]["checks"] = [
        {"type": "not_null", "columns": ["full_name"], "name": "default_samples", "side": "source"},
        {"type": "regex", "column": "full_name", "pattern": "^nomatch$", "side": "source"},
        {
            "type": "regex",
            "name": "no_samples",
            "column": "full_name",
            "pattern": "^nomatch$",
            "side": "source",
            "sample_size": 0,
        },
    ]
    result = api.run(api.load_config_data(config, base_dir=tmp_path), write_reports=False).result
    checks = {c.check_id.split(".", 2)[2]: c for c in result.iter_checks()}
    assert checks["regex.full_name"].samples is not None
    assert checks["regex.full_name.no_samples"].samples is None
    assert checks["regex.full_name.no_samples"].status is Status.FAILED
