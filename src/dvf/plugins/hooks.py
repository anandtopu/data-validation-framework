"""Lifecycle hook specifications (pluggy).

Implement any subset in a module or class decorated with :data:`hookimpl`, then register it
through the ``dvf.hooks`` entry point group or a local plugin path. Arguments are always passed by
keyword, so implementations may accept only the ones they need.

Phase 0 calls ``on_config_loaded``. The other hooks are part of the contract now and are called as
execution lands in later phases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pluggy

if TYPE_CHECKING:
    from dvf.config.models import DvfConfig, SuiteConfig
    from dvf.models.results import CheckResult, RunResult, SuiteResult

PROJECT_NAME = "dvf"

hookspec = pluggy.HookspecMarker(PROJECT_NAME)
hookimpl = pluggy.HookimplMarker(PROJECT_NAME)


class HookSpecs:
    """All lifecycle hooks a plugin may implement."""

    @hookspec
    def on_config_loaded(self, config: DvfConfig) -> None:
        """Called once the configuration is loaded and validated."""

    @hookspec
    def modify_plan(self, plan: Any) -> None:
        """Called with the execution plan before it runs; may reorder or filter tasks. (Phase 1)"""

    @hookspec
    def on_run_start(self, run_id: str, config: DvfConfig) -> None:
        """Called when a run starts. (Phase 1)"""

    @hookspec
    def on_suite_start(self, run_id: str, suite: SuiteConfig) -> None:
        """Called before a suite runs. (Phase 1)"""

    @hookspec
    def on_check_start(self, run_id: str, check_id: str) -> None:
        """Called before a check runs. (Phase 1)"""

    @hookspec
    def enrich_result(self, result: CheckResult) -> None:
        """Called after a check completes; may add entries to ``details`` or ``metadata``. (Phase 1)"""

    @hookspec
    def on_check_complete(self, run_id: str, result: CheckResult) -> None:
        """Called after a check completes and has been enriched. (Phase 1)"""

    @hookspec
    def on_failure(self, run_id: str, result: CheckResult) -> None:
        """Called for checks that failed or errored, for example to open a ticket. (Phase 1)"""

    @hookspec
    def on_suite_end(self, run_id: str, result: SuiteResult) -> None:
        """Called after a suite completes. (Phase 1)"""

    @hookspec
    def on_run_end(self, result: RunResult) -> None:
        """Called after the run completes and before reports render. (Phase 1)"""

    @hookspec
    def report_sections(self, result: RunResult, report_format: str) -> list[dict[str, Any]]:
        """Return extra report sections, each ``{"title": str, "content": str}``. (Phase 1)"""
        raise NotImplementedError
