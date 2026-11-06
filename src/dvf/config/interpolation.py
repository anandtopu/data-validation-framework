"""``${...}`` interpolation of configuration values.

Supported forms:

* ``${env:NAME}`` and ``${env:NAME:-default}``: environment variable (falls back to ``.env``)
* ``${var:name}``: a value from ``vars:`` or ``--var``
* ``${secret:provider:path#key}``: a secret from a registered provider
* ``${run.id}``, ``${run.date}``, ``${run.timestamp}``, ``${run.profile}``: kept as is and
  resolved when a run starts
* ``$${`` produces a literal ``${``
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dvf.config.diagnostics import (
    Code,
    Diagnostic,
    LocationMap,
    ValuePath,
    format_path,
)
from dvf.core.enums import DiagnosticLevel
from dvf.core.errors import SecretError
from dvf.security.secrets import SecretRef, SecretResolver

RUN_PLACEHOLDERS = frozenset({"run.id", "run.date", "run.timestamp", "run.profile"})

_TOKEN_RE = re.compile(r"\$\$\{|\$\{([^{}]*)\}")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Interpolator:
    """Resolves placeholders across a configuration tree and collects diagnostics."""

    environ: Mapping[str, str]
    dotenv: Mapping[str, str]
    secrets: SecretResolver
    locations: LocationMap
    strict: bool = True
    resolve_secrets: bool = True
    variables: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    _in_vars: bool = False

    def interpolate_vars(
        self, raw_vars: Mapping[str, Any], prefix: ValuePath = ("vars",)
    ) -> dict[str, Any]:
        """Resolve the ``vars`` section. Variables may use env and secrets, but not other vars."""
        self._in_vars = True
        try:
            resolved = {k: self.interpolate(v, (*prefix, k)) for k, v in raw_vars.items()}
        finally:
            self._in_vars = False
        return resolved

    def interpolate(self, value: Any, path: ValuePath = ()) -> Any:
        """Return ``value`` with placeholders resolved, recursing into mappings and lists."""
        if isinstance(value, str):
            return self._string(value, path)
        if isinstance(value, Mapping):
            return {k: self.interpolate(v, (*path, k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.interpolate(v, (*path, i)) for i, v in enumerate(value)]
        return value

    # ------------------------------------------------------------------ internals
    def _string(self, text: str, path: ValuePath) -> Any:
        if "${" not in text:
            return text
        matches = list(_TOKEN_RE.finditer(text))
        whole = (
            len(matches) == 1
            and matches[0].group(1) is not None
            and matches[0].start() == 0
            and matches[0].end() == len(text)
        )
        if whole:
            return self._resolve(matches[0].group(1).strip(), matches[0].group(0), path)
        parts: list[str] = []
        position = 0
        for match in matches:
            parts.append(text[position : match.start()])
            if match.group(1) is None:
                parts.append("${")
            else:
                resolved = self._resolve(match.group(1).strip(), match.group(0), path)
                parts.append("" if resolved is None else str(resolved))
            position = match.end()
        parts.append(text[position:])
        return "".join(parts)

    def _resolve(self, expression: str, original: str, path: ValuePath) -> Any:
        if expression in RUN_PLACEHOLDERS:
            return original
        if expression.startswith("run."):
            known = ", ".join(f"${{{p}}}" for p in sorted(RUN_PLACEHOLDERS))
            self._report(
                DiagnosticLevel.ERROR,
                Code.RUN_PLACEHOLDER_UNKNOWN,
                f"Unknown run placeholder '{original}'. Known: {known}.",
                path,
            )
            return original
        kind, sep, rest = expression.partition(":")
        if sep:
            handler = {"env": self._env, "var": self._var, "secret": self._secret}.get(kind.strip())
            if handler is not None:
                return handler(rest, original, path)
        self._report(
            DiagnosticLevel.ERROR,
            Code.INTERPOLATION_SYNTAX,
            f"Cannot interpolate '{original}'. Use ${{env:NAME}}, ${{var:name}}, "
            "${secret:provider:path}, or ${run.id}.",
            path,
        )
        return original

    def _env(self, rest: str, original: str, path: ValuePath) -> Any:
        name, has_default, default = rest.partition(":-")
        name = name.strip()
        if not _ENV_NAME_RE.match(name):
            self._report(
                DiagnosticLevel.ERROR,
                Code.INTERPOLATION_SYNTAX,
                f"Invalid environment variable name in '{original}'.",
                path,
            )
            return original
        value = self.environ.get(name)
        if value is None:
            value = self.dotenv.get(name)
        if value is not None:
            return value
        if has_default:
            return default
        self._report(
            DiagnosticLevel.ERROR if self.strict else DiagnosticLevel.WARNING,
            Code.ENV_MISSING,
            f"Environment variable '{name}' is not set.",
            path,
            hint=f"Set it, add it to .env, or give a default: ${{env:{name}:-value}}.",
        )
        return original

    def _var(self, rest: str, original: str, path: ValuePath) -> Any:
        name = rest.strip()
        if self._in_vars:
            self._report(
                DiagnosticLevel.ERROR,
                Code.INTERPOLATION_SYNTAX,
                f"Variables cannot reference other variables ('{original}').",
                path,
            )
            return original
        if name in self.variables:
            return self.variables[name]
        known = ", ".join(sorted(self.variables)) or "none defined"
        self._report(
            DiagnosticLevel.ERROR,
            Code.VAR_MISSING,
            f"Variable '{name}' is not defined (known: {known}).",
            path,
            hint="Define it under 'vars:' or pass --var name=value.",
        )
        return original

    def _secret(self, rest: str, original: str, path: ValuePath) -> Any:
        try:
            ref = SecretRef.parse(rest)
        except SecretError as exc:
            self._report(DiagnosticLevel.ERROR, Code.INTERPOLATION_SYNTAX, str(exc), path)
            return original
        try:
            self.secrets.check_provider(ref.provider)
        except SecretError as exc:
            self._report(DiagnosticLevel.ERROR, Code.SECRET_PROVIDER_UNKNOWN, str(exc), path)
            return original
        if not self.resolve_secrets:
            return original
        try:
            return self.secrets.resolve(ref)
        except SecretError as exc:
            self._report(
                DiagnosticLevel.ERROR if self.strict else DiagnosticLevel.WARNING,
                Code.SECRET_UNRESOLVED,
                f"Cannot resolve secret '{ref}': {exc}",
                path,
            )
            return original

    def _report(
        self,
        level: DiagnosticLevel,
        code: Code,
        message: str,
        path: ValuePath,
        hint: str | None = None,
    ) -> None:
        self.diagnostics.append(
            Diagnostic(
                level,
                code,
                message,
                path=format_path(path),
                location=self.locations.get(path),
                hint=hint,
            )
        )
