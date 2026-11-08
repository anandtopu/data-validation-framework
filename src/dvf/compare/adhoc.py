"""Ad hoc comparisons (``dvf compare``): build a one-table configuration from two locations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dvf.core.errors import ConfigError

_SCHEME = re.compile(r"^(?P<scheme>[a-z][a-z0-9+]*)://", re.IGNORECASE)


@dataclass
class AdHocSpec:
    source: str
    target: str
    keys: list[str] = field(default_factory=list)
    strategies: list[str] = field(default_factory=lambda: ["row_count", "aggregate", "full_diff"])
    engine: str = "auto"
    sample_size: int = 20
    formats: list[str] = field(default_factory=lambda: ["json"])
    output_dir: str = "./dvf-reports/${run.id}"


def parse_location(location: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Return (connection config, dataset spec, display name) for a location string."""
    match = _SCHEME.match(location)
    if match is None:
        path = Path(location).expanduser().resolve()
        return (
            {"type": "files", "path": path.parent.as_posix()},
            {"path": path.name},
            path.stem,
        )
    scheme = match.group("scheme").lower()
    base, sep, table = location.partition("::")
    if not sep or not table:
        raise ConfigError(
            f"Location '{location}' needs a table after '::', for example '{base}::schema.table'."
        )
    name = table.split(".")[-1]
    if scheme.startswith("postgres"):
        return {"type": "postgres", "url": base}, {"table": table}, name
    if scheme in ("duckdb", "sqlite"):
        database = base[len(match.group(0)) :] or ":memory:"
        return {"type": scheme, "database": database}, {"table": table}, name
    raise ConfigError(
        f"Unsupported location scheme '{scheme}://' in '{location}'; use a file path, "
        "duckdb:///, sqlite:///, or postgresql://."
    )


def adhoc_config(spec: AdHocSpec) -> dict[str, Any]:
    """A configuration mapping comparing ``spec.source`` with ``spec.target``."""
    source_conn, source_ds, _ = parse_location(spec.source)
    target_conn, target_ds, target_name = parse_location(spec.target)
    table_name = re.sub(r"[^A-Za-z0-9_-]+", "_", target_name) or "dataset"
    return {
        "version": 1,
        "project": {"name": "adhoc-compare"},
        # An ad hoc comparison leaves nothing behind but its reports.
        "defaults": {"execution": {"checkpoints": False}},
        "connections": {"source": source_conn, "target": target_conn},
        "suites": [
            {
                "name": "compare",
                "source": "source",
                "target": "target",
                "tables": [
                    {
                        "name": table_name,
                        "source": source_ds,
                        "target": target_ds,
                        "keys": list(spec.keys),
                        "engine": spec.engine,
                        "sample_size": spec.sample_size,
                        "comparison": {"strategies": list(spec.strategies), "on_mismatch": None},
                    }
                ],
            }
        ],
        "reports": {"formats": list(spec.formats), "output_dir": spec.output_dir},
        "history": {"enabled": False},
    }
