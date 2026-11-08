"""A shared local Spark session for the Spark tests, and checks of dvf's session settings.

Spark tests import :func:`local_spark` from here. It skips cleanly when pyspark or Java is
missing, and starts one quiet ``local[2]`` session per test process (Spark allows one).
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import pytest

from dvf.core.errors import ConnectorError

pytestmark = [
    pytest.mark.spark,
    pytest.mark.filterwarnings("ignore:PySpark does not yet fully support pandas:FutureWarning"),
]

PANDAS_WARNING = "ignore:PySpark does not yet fully support pandas:FutureWarning"
"""PySpark warns about pandas 3 on import; dvf's Spark code does not use pandas."""

TEST_CONF = {
    "spark.sql.shuffle.partitions": "2",
    "spark.default.parallelism": "2",
    "spark.driver.memory": "2g",
}


def local_spark() -> Any:
    """The test process's local Spark session (skips when Spark cannot run here)."""
    pytest.importorskip("pyspark")
    if shutil.which("java") is None and not os.environ.get("JAVA_HOME"):
        pytest.skip("Spark tests need Java 17 or later (java on PATH or JAVA_HOME)")
    from dvf.connectors.spark import running_session, start_session

    session = running_session()
    if session is None:
        try:
            session = start_session("local[2]", "dvf-tests", TEST_CONF)
        except ConnectorError as exc:
            pytest.skip(f"Spark could not start here: {exc}")
        session.sparkContext.setLogLevel("ERROR")
    return session


@pytest.fixture(scope="module")
def spark() -> Any:
    return local_spark()


def test_child_sessions_use_utc_and_adaptive_execution(spark: Any) -> None:
    from dvf.connectors.spark import ENFORCED, SESSION_DEFAULTS, child_session

    spark.conf.set("spark.sql.session.timeZone", "America/New_York")
    try:
        child = child_session(spark, {"spark.sql.shuffle.partitions": "3"})
        assert child is not spark
        for key, value in {**SESSION_DEFAULTS, **ENFORCED}.items():
            assert child.conf.get(key) == value
        assert child.conf.get("spark.sql.shuffle.partitions") == "3"
        # The parent session is untouched.
        assert spark.conf.get("spark.sql.session.timeZone") == "America/New_York"
    finally:
        spark.conf.set("spark.sql.session.timeZone", "UTC")


def test_a_different_time_zone_in_conf_is_ignored(
    spark: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from dvf.connectors.spark import child_session

    child = child_session(spark, {"spark.sql.session.timeZone": "Asia/Kolkata"})
    assert child.conf.get("spark.sql.session.timeZone") == "UTC"
    assert "UTC time zone" in caplog.text


def test_static_settings_on_a_running_session_are_reported(
    spark: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from dvf.connectors.spark import configure_session

    configure_session(spark.newSession(), {"spark.sql.warehouse.dir": "elsewhere"})
    assert "cannot change on a running session" in caplog.text


def test_engine_session_is_reused(spark: Any) -> None:
    from dvf.connectors.spark import engine_session, same_application

    first, second = engine_session(), engine_session()
    assert first is second
    assert same_application(first, spark)
    assert first.conf.get("spark.sql.session.timeZone") == "UTC"
