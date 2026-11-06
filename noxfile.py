"""Development tasks. Run ``uv run nox`` for the default sessions (lint, typecheck, tests)."""

from __future__ import annotations

import nox

nox.options.sessions = ["lint", "typecheck", "tests"]

PYTHONS = ["3.11", "3.12", "3.13", "3.14"]


@nox.session(venv_backend="none")
def lint(session: nox.Session) -> None:
    """Ruff lint and format check."""
    session.run("uv", "run", "ruff", "check", ".")
    session.run("uv", "run", "ruff", "format", "--check", ".")


@nox.session(venv_backend="none")
def fmt(session: nox.Session) -> None:
    """Apply Ruff fixes and formatting."""
    session.run("uv", "run", "ruff", "check", "--fix", ".")
    session.run("uv", "run", "ruff", "format", ".")


@nox.session(venv_backend="none")
def typecheck(session: nox.Session) -> None:
    """mypy in strict mode."""
    session.run("uv", "run", "mypy")


@nox.session(venv_backend="none")
def tests(session: nox.Session) -> None:
    """pytest with coverage on the project environment's Python."""
    session.run("uv", "run", "pytest", "--cov", "--cov-report=term-missing", *session.posargs)


@nox.session(venv_backend="none")
def tests_all(session: nox.Session) -> None:
    """pytest on every supported Python, each in an isolated environment."""
    for python in PYTHONS:
        session.run("uv", "run", "--isolated", "--python", python, "pytest", *session.posargs)


@nox.session(venv_backend="none")
def schemas(session: nox.Session) -> None:
    """Regenerate the committed JSON Schemas."""
    session.run("uv", "run", "dvf", "schema", "config", "--output", "schemas/config.schema.json")
    session.run("uv", "run", "dvf", "schema", "results", "--output", "schemas/results.schema.json")


@nox.session(venv_backend="none")
def docs(session: nox.Session) -> None:
    """Build the documentation site in strict mode."""
    session.run("uv", "run", "--group", "docs", "mkdocs", "build", "--strict")
