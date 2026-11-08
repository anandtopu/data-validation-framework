"""In-pipeline validation: dvf.validate_dataframe and the foreachBatch helper."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

import dvf
from dvf.config.checks import NotNullCheck, RegexCheck
from dvf.core.enums import Status
from dvf.core.errors import ConfigError, DataValidationError
from dvf.dataframes import REASON_COLUMN

ROWS: dict[str, list[Any]] = {
    "id": [1, 2, 3, 4, 5, 6],
    "email": ["a@x.org", None, "bad", "d@x.org", "e@x.org", "f@x.org"],
    "country": ["DE", "FR", "XX1", "US", "DE", "us"],
    "amount": [10.0, 20.0, -5.0, 40.0, 50.0, 60.0],
    "code": ["A", "B", "C", "C", "D", "E"],
}

CHECKS: list[dict[str, Any]] = [
    {"type": "not_null", "columns": ["email"]},
    {"type": "regex", "column": "country", "pattern": "^[A-Z]{2}$"},
    {"type": "range", "column": "amount", "min": 0, "action": "drop"},
    {"type": "unique", "columns": ["code"], "action": "warn"},
    {"type": "row_count_range", "min": 1},
]


def _status(result: Any) -> dict[str, Status]:
    return {c.check_id.split(".", 2)[2]: c.status for c in result.checks}


def test_pandas_quarantine_keeps_index_and_dtypes() -> None:
    df = pd.DataFrame(ROWS, index=[f"r{i}" for i in range(6)])
    valid, quarantine, result = dvf.validate_dataframe(df, CHECKS, keys=["id"], name="orders")
    # Row 2 (null email), 3 (bad country, negative amount -> quarantine wins), 6 (lower-case).
    assert list(quarantine.index) == ["r1", "r2", "r5"]
    assert list(valid.index) == ["r0", "r3", "r4"]
    assert valid.dtypes.equals(df.dtypes)
    reasons = dict(zip(quarantine["id"], quarantine[REASON_COLUMN], strict=True))
    assert reasons[2] == "not_null.email"
    assert "regex.country" in reasons[3] and "range.amount" in reasons[3]
    assert reasons[6] == "regex.country"
    status = _status(result)
    assert status["not_null.email"] is Status.FAILED
    assert status["unique.code"] is Status.WARNING  # action warn caps the severity
    assert status["row_count_range"] is Status.PASSED
    assert result.status is Status.FAILED
    assert result.source is not None and result.source.name == "orders"


def test_drop_removes_rows_from_both_frames() -> None:
    df = pd.DataFrame(ROWS)
    checks = [{"type": "range", "column": "amount", "min": 0}]
    valid, quarantine, _ = dvf.validate_dataframe(df, checks, on_fail="drop")
    assert list(valid["id"]) == [1, 2, 4, 5, 6]
    assert quarantine.empty and REASON_COLUMN in quarantine.columns


def test_polars_and_arrow_inputs_return_their_own_types() -> None:
    checks = [
        NotNullCheck(type="not_null", columns=["email"]),
        RegexCheck(type="regex", column="country", pattern="^[A-Z]{2}$"),
    ]
    frame = pl.DataFrame(ROWS)
    valid, quarantine, result = dvf.validate_dataframe(frame, checks)
    assert isinstance(valid, pl.DataFrame) and isinstance(quarantine, pl.DataFrame)
    assert valid["id"].to_list() == [1, 4, 5]
    assert quarantine.schema[REASON_COLUMN] == pl.String
    assert valid.schema == frame.schema
    table = pa.table(ROWS)
    valid_t, quarantine_t, _ = dvf.validate_dataframe(table, checks)
    assert isinstance(valid_t, pa.Table)
    assert valid_t.column("id").to_pylist() == [1, 4, 5]
    assert quarantine_t.column(REASON_COLUMN).to_pylist()[0] == "not_null.email"
    batch = table.to_batches()[0]
    assert dvf.validate_dataframe(batch, checks).valid.num_rows == 3
    assert result.checks[0].samples is not None  # samples are captured like in dvf run


def test_warn_keeps_rows_and_fail_raises() -> None:
    df = pd.DataFrame(ROWS)
    valid, quarantine, result = dvf.validate_dataframe(
        df, [{"type": "not_null", "columns": ["email"]}], on_fail="warn"
    )
    assert len(valid) == 6 and quarantine.empty
    assert result.status is Status.WARNING
    with pytest.raises(DataValidationError, match="action 'fail' failed") as info:
        dvf.validate_dataframe(df, [{"type": "not_null", "columns": ["email"]}], on_fail="fail")
    assert info.value.result.status is Status.FAILED
    passing = dvf.validate_dataframe(df, [{"type": "not_null", "columns": ["id"]}], on_fail="fail")
    assert len(passing.valid) == 6


def test_where_and_mostly_and_expression() -> None:
    df = pd.DataFrame(ROWS)
    checks = [
        {"type": "expression", "name": "positive", "expression": "amount > 0", "where": "id > 3"},
        {"type": "not_null", "columns": ["email"], "mostly": 0.5},
    ]
    _valid, quarantine, result = dvf.validate_dataframe(df, checks)
    # The expression only applies to ids 4-6 (all positive); not_null still marks row 2 even
    # though the check passes within mostly.
    assert list(quarantine["id"]) == [2]
    status = _status(result)
    assert status["expression.positive"] is Status.PASSED
    assert status["not_null.email"] is Status.PASSED


def test_unique_with_quarantine_marks_every_duplicate() -> None:
    df = pd.DataFrame(ROWS)
    valid, quarantine, _ = dvf.validate_dataframe(df, [{"type": "unique", "columns": ["code"]}])
    assert list(quarantine["id"]) == [3, 4]
    assert len(valid) == 4


def test_errors_raise_and_bad_config_is_a_config_error() -> None:
    df = pd.DataFrame(ROWS)
    with pytest.raises(DataValidationError, match="could not be evaluated"):
        dvf.validate_dataframe(df, [{"type": "not_null", "columns": ["nope"]}])
    with pytest.raises(ConfigError):
        dvf.validate_dataframe(df, [{"type": "not_null"}])
    with pytest.raises(ValueError, match="on_fail"):
        dvf.validate_dataframe(df, [], on_fail="explode")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pandas, Polars"):
        dvf.validate_dataframe([1, 2], [])
    with pytest.raises(TypeError, match="LazyFrame"):
        dvf.validate_dataframe(pl.DataFrame(ROWS).lazy(), [])
    with pytest.raises(ValueError, match="reserved"):
        dvf.validate_dataframe(pd.DataFrame({REASON_COLUMN: [1]}), [])


def test_no_row_checks_and_empty_frames() -> None:
    df = pd.DataFrame(ROWS)
    valid, quarantine, result = dvf.validate_dataframe(df, [{"type": "row_count_range", "max": 3}])
    assert valid is df and quarantine.empty
    assert result.status is Status.FAILED
    empty = pd.DataFrame(
        {"id": pd.Series([], dtype="int64"), "email": pd.Series([], dtype="object")}
    )
    valid, quarantine, result = dvf.validate_dataframe(
        empty, [{"type": "not_null", "columns": ["email"]}]
    )
    assert valid.empty and quarantine.empty and result.status is Status.PASSED


def test_strict_privacy_has_no_samples_and_nothing_is_written(tmp_path: Path) -> None:
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        _, _, result = dvf.validate_dataframe(
            pd.DataFrame(ROWS),
            [{"type": "not_null", "columns": ["email"]}],
            privacy={"mode": "strict"},
        )
    finally:
        os.chdir(cwd)
    assert result.checks[0].samples is None
    assert list(tmp_path.iterdir()) == []


def test_foreach_batch_validator_routes_parts() -> None:
    seen: dict[str, Any] = {}

    def on_valid(df: Any, batch_id: int) -> None:
        seen["valid"] = (len(df), batch_id)

    def on_quarantine(df: Any, batch_id: int) -> None:
        seen["quarantine"] = len(df)

    def on_result(result: Any, batch_id: int) -> None:
        seen["status"] = result.status

    process = dvf.foreach_batch_validator(
        [{"type": "not_null", "columns": ["email"]}],
        on_valid=on_valid,
        on_quarantine=on_quarantine,
        on_result=on_result,
    )
    process(pd.DataFrame(ROWS), 7)
    assert seen == {"valid": (5, 7), "quarantine": 1, "status": Status.FAILED}


def test_dataframe_connection_cannot_be_used_from_a_file(tmp_path: Path) -> None:
    from dvf import api

    config = {
        "version": 1,
        "project": {"name": "x"},
        "connections": {"df": {"type": "dataframe", "handle": "missing"}},
        "suites": [
            {
                "name": "s",
                "source": "df",
                "tables": [
                    {
                        "name": "t",
                        "source": {"table": "t"},
                        "checks": [{"type": "not_null", "columns": ["a"]}],
                    }
                ],
            }
        ],
    }
    loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
    out = api.run(loaded, write_reports=False, history=False)
    checks = out.result.suites[0].datasets[0].checks
    assert checks[0].status is Status.ERROR
    assert "no DataFrame attached" in (checks[0].error.message if checks[0].error else "")
