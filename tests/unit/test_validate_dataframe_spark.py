"""In-pipeline validation of Spark DataFrames: checks run as Spark SQL, outputs stay lazy."""

from __future__ import annotations

from typing import Any

import pytest

import dvf
from dvf.core.enums import Status
from dvf.core.errors import DataValidationError
from dvf.dataframes import REASON_COLUMN
from tests.unit.test_spark_support import PANDAS_WARNING, local_spark

pytestmark = [pytest.mark.spark, pytest.mark.filterwarnings(PANDAS_WARNING)]

ROWS = [
    (1, "a@x.org", "DE", 10.0, "A"),
    (2, None, "FR", 20.0, "B"),
    (3, "bad", "XX1", -5.0, "C"),
    (4, "d@x.org", "US", 40.0, "C"),
    (5, "e@x.org", "DE", 50.0, "D"),
    (6, "f@x.org", "us", 60.0, "E"),
]
COLUMNS = ["id", "email", "country", "amount", "code"]


@pytest.fixture(scope="module")
def frame() -> Any:
    spark = local_spark()
    return spark.createDataFrame(ROWS, COLUMNS)


def _ids(df: Any) -> list[int]:
    return sorted(r["id"] for r in df.select("id").collect())


def test_spark_quarantine_drop_and_warn(frame: Any) -> None:
    checks = [
        {"type": "not_null", "columns": ["email"]},
        {"type": "regex", "column": "country", "pattern": "^[A-Z]{2}$"},
        {"type": "range", "column": "amount", "min": 0, "action": "drop"},
        {"type": "unique", "columns": ["code"], "action": "warn"},
        {"type": "row_count_range", "min": 1},
    ]
    valid, quarantine, result = dvf.validate_dataframe(frame, checks, keys=["id"], name="orders")
    assert type(valid).__name__ == "DataFrame" and valid.columns == COLUMNS
    assert _ids(valid) == [1, 4, 5]
    assert _ids(quarantine) == [2, 3, 6]
    assert quarantine.columns == [*COLUMNS, REASON_COLUMN]
    reasons = {r["id"]: r[REASON_COLUMN] for r in quarantine.collect()}
    assert reasons[2] == "not_null.email"
    assert "regex.country" in reasons[3] and "range.amount" in reasons[3]
    status = {c.check_id.split(".", 2)[2]: c.status for c in result.checks}
    assert status["not_null.email"] is Status.FAILED
    assert status["unique.code"] is Status.WARNING
    assert status["row_count_range"] is Status.PASSED
    assert all(q.dialect == "spark" for c in result.checks for q in c.queries)


def test_spark_unique_quarantines_every_duplicate(frame: Any) -> None:
    valid, quarantine, _ = dvf.validate_dataframe(frame, [{"type": "unique", "columns": ["code"]}])
    assert _ids(quarantine) == [3, 4]
    assert _ids(valid) == [1, 2, 5, 6]


def test_spark_fail_raises_and_dataset_checks_only(frame: Any) -> None:
    with pytest.raises(DataValidationError, match="action 'fail' failed"):
        dvf.validate_dataframe(frame, [{"type": "not_null", "columns": ["email"]}], on_fail="fail")
    valid, quarantine, result = dvf.validate_dataframe(
        frame, [{"type": "row_count_range", "max": 3}]
    )
    assert valid is frame
    assert quarantine.count() == 0 and REASON_COLUMN in quarantine.columns
    assert result.status is Status.FAILED


def test_spark_outputs_survive_after_validation(frame: Any) -> None:
    valid, quarantine, _ = dvf.validate_dataframe(
        frame, [{"type": "not_null", "columns": ["email"]}]
    )
    # The outputs derive from the input frame, not from dvf's temporary views, so they can be
    # used after dvf has closed its session.
    assert valid.count() == 5
    assert quarantine.count() == 1


def test_spark_foreach_batch(frame: Any) -> None:
    seen: dict[str, Any] = {}
    process = dvf.foreach_batch_validator(
        [{"type": "not_null", "columns": ["email"]}],
        on_valid=lambda df, batch_id: seen.setdefault("valid", (df.count(), batch_id)),
        on_quarantine=lambda df, batch_id: seen.setdefault("quarantine", df.count()),
    )
    process(frame, 3)
    assert seen == {"valid": (5, 3), "quarantine": 1}
