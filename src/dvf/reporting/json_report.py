"""The JSON reporter: the canonical, versioned results document (``results.json``)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from dvf.models.results import ArtifactRef, RunResult
from dvf.plugins.base import Reporter
from dvf.reporting._common import report_artifact, write_text

if TYPE_CHECKING:
    from dvf.reporting.base import ReportOptions

RESULTS_FILE = "results.json"


def _finite(value: Any) -> Any:
    """Replace NaN and infinities, which JSON cannot represent, with ``null``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def results_document(run: RunResult) -> str:
    """The results document as JSON text (UTF-8 friendly, two-space indent, trailing newline)."""
    data = _finite(run.model_dump(mode="json"))
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


class JsonReporter(Reporter):
    """Canonical results document (versioned JSON); every other format derives from it."""

    format: ClassVar[str] = "json"

    def render(self, run: RunResult, out_dir: Path, options: ReportOptions) -> list[ArtifactRef]:
        """Write ``results.json`` into ``out_dir``."""
        path = out_dir / RESULTS_FILE
        write_text(path, results_document(run))
        return [report_artifact(path, self.format)]
