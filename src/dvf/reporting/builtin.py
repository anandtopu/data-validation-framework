"""The built-in reporters, by format name, for registration in the plugin registry."""

from __future__ import annotations

from dvf.plugins.base import Reporter
from dvf.reporting.excel import ExcelReporter
from dvf.reporting.html import HtmlReporter
from dvf.reporting.json_report import JsonReporter
from dvf.reporting.junit import JUnitReporter
from dvf.reporting.markdown import MarkdownReporter

BUILTIN_REPORTERS: dict[str, type[Reporter]] = {
    JsonReporter.format: JsonReporter,
    HtmlReporter.format: HtmlReporter,
    JUnitReporter.format: JUnitReporter,
    MarkdownReporter.format: MarkdownReporter,
    ExcelReporter.format: ExcelReporter,
}
"""Format name (``json``, ``html``, ``junit``, ``markdown``, ``excel``) to reporter class.

``excel`` imports openpyxl only when instantiated, so the map loads without the extra.
"""

__all__ = [
    "BUILTIN_REPORTERS",
    "ExcelReporter",
    "HtmlReporter",
    "JUnitReporter",
    "JsonReporter",
    "MarkdownReporter",
]
