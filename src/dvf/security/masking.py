"""Masking of personal data in samples (``defaults.privacy``)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any

from dvf.config.models import PrivacyConfig
from dvf.security.redaction import MASK

_EMAIL = re.compile(r"^([^@\s])[^@\s]*(@[^@\s]+\.[^@\s]+)$")
_MAX_TEXT = 500


@dataclass(frozen=True)
class Privacy:
    """Applies privacy settings to sample values."""

    mode: str = "standard"
    mask_pii: bool = True
    pii_columns: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(cls, config: PrivacyConfig, extra_columns: Iterable[str] = ()) -> Privacy:
        return cls(
            mode=config.mode,
            mask_pii=config.mask_pii,
            pii_columns=(*config.pii_columns, *extra_columns),
        )

    @property
    def strict(self) -> bool:
        """In strict mode no raw value leaves the process: samples are dropped."""
        return self.mode == "strict"

    def is_pii(self, column: str) -> bool:
        lowered = column.lower()
        return any(fnmatchcase(lowered, p.lower()) for p in self.pii_columns)

    def mask(self, column: str, value: Any) -> Any:
        """Return a JSON-safe, masked-as-needed version of ``value`` for ``column``."""
        if value is None:
            return None
        if self.mask_pii and self.is_pii(column):
            return MASK
        plain = to_json_safe(value)
        if self.mask_pii and isinstance(plain, str):
            match = _EMAIL.match(plain)
            if match:
                return f"{match.group(1)}***{match.group(2)}"
        return plain

    def mask_rows(self, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
        return [[self.mask(c, v) for c, v in zip(columns, row, strict=True)] for row in rows]


def to_json_safe(value: Any) -> Any:
    """Convert a database value into something JSON can hold, shortening very long text."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value)[:64].hex()
    text = str(value)
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"
