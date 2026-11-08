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

    def __init__(
        self, message: str, diagnostics: Sequence[Diagnostic] = (), *, hint: str | None = None
    ) -> None:
        super().__init__(message)
        self.diagnostics: list[Diagnostic] = list(diagnostics)
        self.hint = hint

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


class ExecutionError(DvfError):
    """Running a check or comparison failed (a connection, query, or engine problem).

    ``hint`` says how to fix it, when dvf knows.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class ConnectorError(ExecutionError):
    """A connector could not connect, resolve a dataset, or run a query."""


class DependencyError(ExecutionError):
    """An optional dependency (a pip extra) is needed but not installed."""

    def __init__(self, package: str, extra: str, purpose: str) -> None:
        super().__init__(
            f"{purpose} needs the '{package}' package, which is not installed.",
            hint=f"Install it with: pip install 'dvf[{extra}]'",
        )
        self.package = package
        self.extra = extra


class PlanError(ExecutionError):
    """A table cannot be compared as configured (for example, a key column does not exist)."""


def _quote_list(items: Sequence[str]) -> str:
    quoted = [f"'{item}'" for item in items]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + f" or {quoted[-1]}"
