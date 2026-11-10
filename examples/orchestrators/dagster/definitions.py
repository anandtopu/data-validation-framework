"""Dagster definitions: dvf asset checks on the assets a migration loads.

    pip install dvf-framework dagster dagster-webserver
    dagster dev -f definitions.py

The checks come from ``examples/ci/dvf.yaml`` (PR and nightly suites over synthetic files); set
``DVF_CONFIG`` to use your own configuration. Each dvf table becomes one asset check on the
asset of the same name (``granularity="table"``); use ``granularity="check"`` for one asset
check per dvf check. ``blocking=True`` stops downstream assets when an ERROR check fails.

Verified only by dvf's unit tests with a stand-in Dagster module, not on a Dagster instance.
"""

from __future__ import annotations

import os
from pathlib import Path

import dagster as dg

from dvf.integrations.dagster import build_dvf_asset_checks

CONFIG = os.environ.get("DVF_CONFIG", str(Path(__file__).resolve().parents[2] / "ci" / "dvf.yaml"))


@dg.asset
def customers() -> None:
    """Loads customers into the curated zone (your migration job goes here)."""


@dg.asset
def orders() -> None:
    """Loads orders into the curated zone."""


dvf_checks = build_dvf_asset_checks(
    CONFIG,
    only="tag:pr",
    asset_key={"customers": ["customers"], "orders": ["orders"]},
    fail_on="error",
    blocking=True,
)

defs = dg.Definitions(assets=[customers, orders], asset_checks=[dvf_checks])
