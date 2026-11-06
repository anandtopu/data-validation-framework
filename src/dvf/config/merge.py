"""Layer merging: mappings merge key by key; lists and scalars replace."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from dvf.config.diagnostics import LocationMap, ValuePath


def deep_merge(base: Any, overlay: Any) -> Any:
    """Return ``overlay`` merged onto ``base`` without mutating either."""
    if isinstance(base, Mapping) and isinstance(overlay, Mapping):
        result = {k: copy.deepcopy(v) for k, v in base.items()}
        for key, value in overlay.items():
            result[key] = deep_merge(base[key], value) if key in base else copy.deepcopy(value)
        return result
    return copy.deepcopy(overlay)


def replaced_paths(base: Any, overlay: Any, prefix: ValuePath = ()) -> list[ValuePath]:
    """Paths where ``overlay`` replaces (rather than merges into) ``base``."""
    if isinstance(base, Mapping) and isinstance(overlay, Mapping):
        paths: list[ValuePath] = []
        for key, value in overlay.items():
            if key in base:
                paths.extend(replaced_paths(base[key], value, (*prefix, key)))
        return paths
    return [prefix]


def merge_layer(
    base: dict[str, Any],
    base_locations: LocationMap,
    overlay: Mapping[str, Any],
    overlay_locations: LocationMap,
    prefix: ValuePath = (),
) -> dict[str, Any]:
    """Merge ``overlay`` onto ``base`` and keep value locations accurate."""
    for path in replaced_paths(base, overlay):
        base_locations.drop_subtree(path)
    base_locations.overlay(overlay_locations, prefix)
    merged: dict[str, Any] = deep_merge(base, overlay)
    return merged
