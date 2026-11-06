"""Report options and the function that renders every configured format."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dvf.core.errors import PluginError
from dvf.models.results import ArtifactRef, RunResult
from dvf.plugins.base import Reporter
from dvf.plugins.registry import PluginKind, Registry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportSection:
    """An extra report section contributed by a ``report_sections`` hook.

    ``content`` is plain text (Markdown is shown as text in HTML). Reporters escape it.
    """

    title: str
    content: str


@dataclass(frozen=True)
class ReportOptions:
    """Presentation settings shared by every reporter."""

    title: str | None = None
    """Report title; defaults to the project name."""
    logo: Path | None = None
    """Logo image file. HTML embeds it as a data URI so the report stays self-contained."""
    primary_color: str | None = None
    """Accent color as ``#RRGGBB``."""
    include_passed: bool = True
    """Include passed checks in the detailed check listings."""
    max_samples_per_check: int = 50
    """Upper bound on sample rows shown per check or column."""
    templates_dir: Path | None = None
    """Folder whose Jinja2 templates override the built-in ones (same file names)."""
    extra_sections: tuple[ReportSection, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    """Extra key-value pairs shown in report headers (for example release or approver)."""


def render_reports(
    run: RunResult,
    formats: list[str],
    out_dir: Path,
    options: ReportOptions,
    registry: Registry,
) -> list[ArtifactRef]:
    """Render every format in ``formats`` into ``out_dir``.

    Raises :class:`PluginError` for unknown or not-yet-implemented formats, before writing anything.
    """
    reporters: list[Reporter] = []
    for name in formats:
        entry = registry.get(PluginKind.REPORTER, name)
        if not entry.ready:
            extra = f" (install dvf[{entry.extra}])" if entry.extra else ""
            raise PluginError(
                f"Report format '{name}' is declared but not implemented in this version{extra}. "
                "Run 'dvf plugins list --kind reporter' to see the available formats."
            )
        implementation = entry.implementation
        reporter = implementation() if isinstance(implementation, type) else implementation
        if not isinstance(reporter, Reporter):
            raise PluginError(f"Reporter '{name}' does not subclass dvf.plugins.Reporter.")
        reporters.append(reporter)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[ArtifactRef] = []
    for reporter in reporters:
        written = reporter.render(run, out_dir, options)
        log.debug("rendered %s report: %s", reporter.format, [a.path for a in written])
        artifacts.extend(written)
    return artifacts
