"""A test-only plugin check that records when it runs (to measure concurrency and resume).

Each evaluation appends ``{"table", "connection", "start", "end", "pid"}`` to the JSON-lines file
named by ``log``, sleeps ``sleep`` seconds, and raises ``KeyboardInterrupt`` when ``interrupt``
is true (a simulated Ctrl+C). Loaded through ``plugins.paths`` in test configurations.
"""

from __future__ import annotations

import json
import os
import threading
import time

from dvf.plugins import Check, CheckConfig, CheckContext, CheckOutcome, register_check

_LOCK = threading.Lock()


@register_check("probe.timed")
class TimedProbe(Check):
    """Records its start and end time, then passes."""

    class Config(CheckConfig):
        log: str
        sleep: float = 0.0
        interrupt: bool = False

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, TimedProbe.Config)
        start = time.time()
        if config.interrupt:
            raise KeyboardInterrupt
        time.sleep(config.sleep)
        ctx.scalar(f"SELECT COUNT(*) FROM {ctx.relation.sql}")
        record = {
            "table": ctx.relation.ref.name,
            "connection": ctx.connector.name,
            "start": start,
            "end": time.time(),
            "pid": os.getpid(),
        }
        with _LOCK, open(config.log, "a", encoding="utf-8") as handle:  # noqa: PTH123
            handle.write(json.dumps(record) + "\n")
        return CheckOutcome(passed=True, message="probe ran")
