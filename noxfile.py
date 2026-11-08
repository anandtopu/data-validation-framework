"""Development tasks. Run ``uv run nox`` for the default sessions (lint, typecheck, tests)."""

from __future__ import annotations

from pathlib import Path

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
def integration(session: nox.Session) -> None:
    """Integration tests (PostgreSQL). Set DVF_TEST_POSTGRES_URL, or have Docker available."""
    session.run("uv", "run", "pytest", "-m", "integration", "-v", *session.posargs)


@nox.session(venv_backend="none")
def example(session: nox.Session) -> None:
    """Scaffold the seeded example in a temporary folder and run it end to end."""
    import tempfile

    folder = tempfile.mkdtemp(prefix="dvf-example-")
    session.run("uv", "run", "dvf", "init", f"{folder}/demo", "--example")
    session.run("uv", "run", "dvf", "run", f"{folder}/demo/dvf.yaml", success_codes=[1])


@nox.session(venv_backend="none")
def benchmark(session: nox.Session) -> None:
    """Performance benchmarks (10 million rows by default; set DVF_BENCH_ROWS to change it).

    Fails if a benchmark misses its target or is more than 15% slower than the stored baseline
    (``.benchmarks/baseline.json``); pass ``-- --update`` to record a new baseline.
    """
    update = "--update" in session.posargs
    args = [a for a in session.posargs if a != "--update"]
    session.env["DVF_BENCHMARK"] = "1"
    results = Path(".benchmarks") / "results.json"
    results.unlink(missing_ok=True)
    session.run("uv", "run", "pytest", "tests/benchmarks", "-v", "--no-cov", *args)
    compare = [
        "uv",
        "run",
        "python",
        "tests/benchmarks/compare.py",
        str(results),
        ".benchmarks/baseline.json",
    ]
    session.run(*compare, *(["--update"] if update else []))


@nox.session(venv_backend="none")
def schemas(session: nox.Session) -> None:
    """Regenerate the committed JSON Schemas."""
    session.run("uv", "run", "dvf", "schema", "config", "--output", "schemas/config.schema.json")
    session.run("uv", "run", "dvf", "schema", "results", "--output", "schemas/results.schema.json")


@nox.session(venv_backend="none")
def docs(session: nox.Session) -> None:
    """Build the documentation site in strict mode."""
    session.run("uv", "run", "--group", "docs", "mkdocs", "build", "--strict")
