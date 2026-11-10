"""Prefect flow: load data, then validate it with dvf and publish the results as artifacts.

    pip install dvf-framework prefect
    python dvf_flow.py                 # one run against examples/ci/dvf.yaml
    python dvf_flow.py --serve         # serve the flow with a nightly schedule

``dvf_validate`` creates a Markdown artifact (the run summary) and a table artifact (failed
checks), returns the summary, and raises when the quality gates fail, so the flow run fails.
Set ``DVF_CONFIG`` to use your own configuration; the example data has seeded defects, so
the flow run fails its gates on purpose.

Verified only by dvf's unit tests with a stand-in Prefect module, not on a Prefect server.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from prefect import flow, task

from dvf.integrations.prefect import dvf_validate

CONFIG = os.environ.get("DVF_CONFIG", str(Path(__file__).resolve().parents[2] / "ci" / "dvf.yaml"))


@task
def load_orders() -> None:
    """Your migration or load step goes here."""


@flow(name="orders-migration")
def orders_migration(mode: str = "pr", min_score: float | None = None) -> dict[str, Any]:
    load_orders()
    summary: dict[str, Any] = dvf_validate(CONFIG, only=f"tag:{mode}", min_score=min_score)
    return summary


if __name__ == "__main__":
    if "--serve" in sys.argv:
        orders_migration.serve(
            name="nightly", cron="0 2 * * *", parameters={"mode": "nightly", "min_score": 95}
        )
    else:
        orders_migration()
