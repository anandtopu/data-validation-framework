"""Notebook helper for failed checks (BUILD_PROMPT.md section 9.6).

>>> import dvf.debug
>>> run = dvf.debug.load("20261107T101500Z-3f9a", config="dvf.yaml")
>>> run.check("finance.customers.compare.column_mismatch").mismatches(format="pandas")
"""

from __future__ import annotations

from dvf.api import CheckHandle, RunHandle
from dvf.api import load_run as load

__all__ = ["CheckHandle", "RunHandle", "load"]
