from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dvf.config.diagnostics import Code, Diagnostic, SourceLocation, format_path, resolve_path
from dvf.core.enums import DiagnosticLevel, Dimension, Severity, Status
from dvf.core.errors import ConfigError, SecretError
from dvf.models import (
    CheckResult,
    DatasetPairResult,
    ReconciliationSummary,
    RowCounts,
    RunEnvironment,
    RunResult,
    SuiteResult,
    aggregate_status,
    make_check_id,
)
from dvf.observability.logging import configure_logging, get_logger
from dvf.plugins.registry import Registry
from dvf.security.redaction import MASK, Redactor, global_redactor, is_sensitive_key
from dvf.security.secrets import (
    DotEnvSecretProvider,
    EnvSecretProvider,
    SecretContext,
    SecretRef,
    SecretResolver,
)

# ---------------------------------------------------------------------- results


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], Status.SKIPPED),
        ([Status.SKIPPED], Status.SKIPPED),
        ([Status.PASSED, Status.WAIVED], Status.PASSED),
        ([Status.WAIVED], Status.PASSED),
        ([Status.PASSED, Status.WARNING], Status.WARNING),
        ([Status.WARNING, Status.FAILED], Status.FAILED),
        ([Status.FAILED, Status.ERROR, Status.PASSED], Status.ERROR),
    ],
)
def test_aggregate_status(statuses: list[Status], expected: Status) -> None:
    assert aggregate_status(statuses) is expected


def test_make_check_id() -> None:
    assert make_check_id("s", "t", "not_null", columns=["a", "b"]) == "s.t.not_null.a+b"
    assert make_check_id("s", "t", "acme.vat", name="my rule!") == "s.t.acme.vat.my_rule_"
    assert make_check_id("s", "t", "row_count_range") == "s.t.row_count_range"


def _check(status: Status) -> CheckResult:
    return CheckResult(
        check_id=f"s.t.{status.value}",
        name=status.value,
        type="not_null",
        dimension=Dimension.COMPLETENESS,
        severity=Severity.ERROR,
        status=status,
    )


def test_run_result_round_trip_and_summary() -> None:
    dataset = DatasetPairResult(
        name="t",
        status=Status.FAILED,
        row_counts=RowCounts(source=10, target=8),
        reconciliation=ReconciliationSummary(matched=8, missing_in_target=2),
        checks=[_check(s) for s in (Status.PASSED, Status.FAILED, Status.WAIVED, Status.SKIPPED)],
    )
    run = RunResult(
        run_id="r1",
        project="p",
        status=Status.FAILED,
        started_at=datetime(2026, 11, 5, tzinfo=UTC),
        environment=RunEnvironment(dvf_version="0", python_version="3", platform="x"),
        suites=[SuiteResult(name="s", status=Status.FAILED, datasets=[dataset])],
    )
    assert dataset.row_counts.difference == -2
    assert dataset.reconciliation is not None and dataset.reconciliation.match_pct == 80.0
    assert ReconciliationSummary().match_pct is None
    assert RowCounts(source=1).difference is None
    summary = run.summarize()
    assert summary.quality_score == pytest.approx(66.67)
    assert summary.checks_by_status[Status.SKIPPED] == 1
    assert summary.datasets == 1
    assert RunResult.model_validate_json(run.model_dump_json()) == run


# ---------------------------------------------------------------------- diagnostics


def test_diagnostic_formatting() -> None:
    d = Diagnostic(
        DiagnosticLevel.ERROR,
        Code.UNKNOWN_FIELD,
        "Unknown field 'x'.",
        path="a[0].x",
        location=SourceLocation("f.yaml", 3, 5),
        hint="Did you mean 'y'?",
    )
    assert (
        d.format() == "f.yaml:3:5 (a[0].x): Unknown field 'x'. Hint: Did you mean 'y'? [DVF-C102]"
    )
    assert d.to_dict()["line"] == 3
    assert str(SourceLocation("f", 2)) == "f:2" and str(SourceLocation("f")) == "f"
    error = ConfigError("bad", [d])
    assert "Unknown field" in str(error)
    assert str(ConfigError("plain")) == "plain"
    assert format_path(["a", 0, "b", 1]) == "a[0].b[1]"


def test_resolve_path_strips_union_tags() -> None:
    data = {"suites": [{"tables": [{"keys": ["a"]}]}]}
    loc = ("suites", 0, "tables", 0, "keys", "list[KeyMapping]", 0, "source")
    assert resolve_path(data, loc) == ("suites", 0, "tables", 0, "keys", 0)
    assert resolve_path(data, ("suites", 0, "missing", "deeper")) == ("suites", 0, "missing")


# ---------------------------------------------------------------------- secrets and redaction


def test_secret_ref_parsing() -> None:
    ref = SecretRef.parse("azurekv:my-vault#db-password")
    assert (ref.provider, ref.path, ref.key, str(ref)) == (
        "azurekv",
        "my-vault",
        "db-password",
        "azurekv:my-vault#db-password",
    )
    assert str(SecretRef.parse("env:X")) == "env:X"
    for bad in ("nocolon", "Env:X", "env:", "env: #k"):
        with pytest.raises(SecretError):
            SecretRef.parse(bad)


def test_env_and_dotenv_providers(tmp_path: Path) -> None:
    context = SecretContext(environ={"A": "env-a"}, dotenv={"B": "dot-b"}, base_dir=tmp_path)
    env = EnvSecretProvider(context)
    assert env.resolve(SecretRef.parse("env:A")) == "env-a"
    assert env.resolve(SecretRef.parse("env:B")) == "dot-b"
    with pytest.raises(SecretError, match="not set"):
        env.resolve(SecretRef.parse("env:C"))
    with pytest.raises(SecretError, match="variable name only"):
        env.resolve(SecretRef.parse("env:A#x"))
    dotenv = DotEnvSecretProvider(context)
    assert dotenv.resolve(SecretRef.parse("dotenv:B")) == "dot-b"
    with pytest.raises(SecretError, match="not defined"):
        dotenv.resolve(SecretRef.parse("dotenv:A"))
    (tmp_path / "other.env").write_text("K=from-file\n", encoding="utf-8")
    assert dotenv.resolve(SecretRef.parse("dotenv:other.env#K")) == "from-file"
    with pytest.raises(SecretError, match="does not exist"):
        dotenv.resolve(SecretRef.parse("dotenv:missing.env#K"))


def test_resolver_records_values(registry: Registry) -> None:
    redactor = Redactor()
    resolver = SecretResolver(registry, SecretContext(environ={"TOKEN": "tok-12345"}), redactor)
    assert resolver.resolve(SecretRef.parse("env:TOKEN")) == "tok-12345"
    assert resolver.resolve(SecretRef.parse("env:TOKEN")) == "tok-12345"  # provider cached
    assert "tok-12345" in redactor
    registry.declare("secret_provider", "planned", "A provider declared for later", extra="future")
    with pytest.raises(SecretError, match="planned for the 'future' extra"):
        resolver.resolve(SecretRef.parse("planned:kv/db#pw"))
    with pytest.raises(SecretError, match="Unknown secret provider 'nope'"):
        resolver.resolve(SecretRef.parse("nope:x"))


def test_redactor() -> None:
    redactor = Redactor()
    redactor.add("hunter22")
    redactor.add("abc")  # too short to be redacted safely
    redactor.add(None)
    assert redactor.redact_text("pw=hunter22 abc") == f"pw={MASK} abc"
    assert (
        redactor.redact_text("postgresql://user:pa55@host/db")
        == f"postgresql://user:{MASK}@host/db"
    )
    data = {
        "password": "plain",
        "token": "${secret:env:T}",
        "webhook": "",
        "nested": [{"api_key": "k1"}, "hunter22"],
        "port": 5432,
        "name": "ok",
    }
    assert redactor.redact(data) == {
        "password": MASK,
        "token": "${secret:env:T}",
        "webhook": "",
        "nested": [{"api_key": MASK}, MASK],
        "port": 5432,
        "name": "ok",
    }
    redactor.clear()
    assert "hunter22" not in redactor
    assert is_sensitive_key("private_key_passphrase") and not is_sensitive_key("host")


# ---------------------------------------------------------------------- logging


def test_json_logging_with_redaction() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    global_redactor().add("sup3r-secret")
    get_logger("tests").info("connecting with %s", "sup3r-secret", extra={"run_id": "r1"})
    record = json.loads(stream.getvalue().strip())
    assert record["message"] == f"connecting with {MASK}"
    assert record["run_id"] == "r1"
    assert record["logger"] == "dvf.tests"
    assert get_logger("dvf.x").name == "dvf.x"


def test_text_logging_replaces_handlers() -> None:
    stream = io.StringIO()
    configure_logging("debug", "text", stream=stream)
    logger = configure_logging("warning", "text", stream=stream)
    assert len([h for h in logger.handlers if getattr(h, "_dvf_handler", False)]) == 1
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    assert "failed" in stream.getvalue()
    logger.setLevel(logging.WARNING)


def test_json_logging_includes_exceptions() -> None:
    stream = io.StringIO()
    logger = configure_logging("info", "json", stream=stream)
    try:
        raise RuntimeError("kaput")
    except RuntimeError:
        logger.exception("oops")
    assert "kaput" in json.loads(stream.getvalue().strip())["exception"]
