"""Shared building blocks for configuration models."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

IDENTIFIER_PATTERN = r"^[A-Za-z0-9_-]+$"
COMPONENT_NAME_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
DURATION_PATTERN = r"^\s*\d+(\.\d+)?\s*(ms|s|m|h|d)\s*$"

Identifier = Annotated[
    str, StringConstraints(pattern=IDENTIFIER_PATTERN, min_length=1, max_length=128)
]
"""A name used inside check IDs: letters, digits, underscores, and hyphens."""

ComponentName = Annotated[str, StringConstraints(pattern=COMPONENT_NAME_PATTERN)]
"""A registered component name, optionally namespaced: ``row_count``, ``acme.vat_valid``."""

Duration = Annotated[str, StringConstraints(pattern=DURATION_PATTERN)]
"""A duration such as ``500ms``, ``30s``, ``15m``, ``24h``, or ``7d``."""

_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """Convert a :data:`Duration` string to a :class:`~datetime.timedelta`."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*", text)
    if not match:
        raise ValueError(f"Invalid duration '{text}'; use a number and a unit: ms, s, m, h, or d.")
    return timedelta(seconds=float(match.group(1)) * _DURATION_UNITS[match.group(2)])


class StrictModel(BaseModel):
    """Base for configuration models: unknown fields are errors; field docstrings become docs."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        use_attribute_docstrings=True,
        validate_assignment=False,
    )


class NormalizerRef(StrictModel):
    """A normalizer applied to a column before comparison."""

    name: ComponentName
    """Registered normalizer name, for example ``rtrim`` or ``sap_alpha``."""
    params: dict[str, Any] = Field(default_factory=dict)
    """Normalizer parameters, for example ``{digits: 2}`` for ``round``."""

    def __str__(self) -> str:
        if not self.params:
            return self.name
        args = ", ".join(f"{k}={v!r}" for k, v in sorted(self.params.items()))
        return f"{self.name}({args})"


def _coerce_normalizer(value: Any) -> Any:
    if isinstance(value, str):
        return {"name": value}
    if isinstance(value, dict) and len(value) == 1 and "name" not in value:
        ((name, params),) = value.items()
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            params = {"value": params}
        return {"name": name, "params": params}
    return value


NormalizerInput = Annotated[
    NormalizerRef,
    BeforeValidator(
        _coerce_normalizer, json_schema_input_type=str | dict[str, Any] | NormalizerRef
    ),
]
"""Accepts ``"rtrim"``, ``{round: {digits: 2}}``, ``{round: 2}``, or ``{name: round, params: {...}}``."""


class ExactlyOneOf:
    """Helper for "exactly one of these fields" validation messages."""

    @staticmethod
    def check(model: BaseModel, fields: tuple[str, ...], *, required: bool = True) -> None:
        present = [f for f in fields if getattr(model, f) is not None]
        names = ", ".join(f"'{f}'" for f in fields)
        if len(present) > 1:
            raise ValueError(f"set only one of {names} (got {', '.join(present)})")
        if required and not present:
            raise ValueError(f"set one of {names}")
