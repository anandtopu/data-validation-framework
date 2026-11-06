"""Exception hierarchy. Every error dvf raises on purpose derives from :class:`DvfError`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dvf.config.diagnostics import Diagnostic


class DvfError(Exception):
    """Base class for all dvf errors."""


class ConfigError(DvfError):
    """The configuration could not be loaded or is invalid.

    ``diagnostics`` holds every problem found, so users can fix them in one pass.
    """

    def __init__(self, message: str, diagnostics: Sequence[Diagnostic] = ()) -> None:
        super().__init__(message)
        self.diagnostics: list[Diagnostic] = list(diagnostics)

    def __str__(self) -> str:
        base = super().__str__()
        if not self.diagnostics:
            return base
        lines = [base, *(f"  - {d.format()}" for d in self.diagnostics)]
        return "\n".join(lines)


class PluginError(DvfError):
    """A plugin could not be registered or loaded."""


class UnknownPluginError(PluginError, LookupError):
    """No component of the requested kind and name is registered."""

    def __init__(self, kind: str, name: str, suggestions: Sequence[str] = ()) -> None:
        self.kind = kind
        self.name = name
        self.suggestions = list(suggestions)
        hint = f" Did you mean {_quote_list(self.suggestions)}?" if self.suggestions else ""
        super().__init__(f"Unknown {kind} '{name}'.{hint}")


class SecretError(DvfError):
    """A secret reference could not be parsed or resolved."""


def _quote_list(items: Sequence[str]) -> str:
    quoted = [f"'{item}'" for item in items]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + f" or {quoted[-1]}"
