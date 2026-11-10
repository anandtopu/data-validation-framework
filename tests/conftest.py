from __future__ import annotations

import gc
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from dvf.plugins import registry as registry_module
from dvf.plugins.registry import Registry, new_registry
from dvf.security.redaction import global_redactor

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"


@pytest.fixture
def registry() -> Registry:
    """A fresh registry with built-ins declared and no entry points."""
    return new_registry()


@pytest.fixture
def default_registry(registry: Registry) -> Iterator[Registry]:
    """Install a fresh registry as the process default (for decorators and the CLI)."""
    previous = registry_module._default_registry
    registry_module._default_registry = registry
    try:
        yield registry
    finally:
        registry_module._default_registry = previous


@pytest.fixture(autouse=True)
def _clean_redactor() -> Iterator[None]:
    yield
    global_redactor().clear()


WriteFile = Callable[..., Path]


@pytest.fixture
def write(tmp_path: Path) -> WriteFile:
    """Write a dedented text file under tmp_path and return its path."""

    def _write(name: str, content: str) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    return _write


MINIMAL = """
version: 1
project: {name: demo}
connections:
  src: {type: duckdb}
  tgt: {type: duckdb, database: target.duckdb}
suites:
  - name: s1
    source: src
    target: tgt
    tables:
      - name: t1
        source: {table: a}
        target: {table: b}
        keys: [id]
"""


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A PostgreSQL connection URL for integration tests.

    Uses ``DVF_TEST_POSTGRES_URL`` when set (CI services, or a local server), otherwise starts a
    container with testcontainers. Skips when neither is available.
    """
    import os

    url = os.environ.get("DVF_TEST_POSTGRES_URL")
    if url:
        yield url
        return
    import warnings

    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # testcontainers emits deprecation warnings on import
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError:  # pragma: no cover - dev dependency
            pytest.skip("testcontainers is not installed and DVF_TEST_POSTGRES_URL is not set")
        failure: str | None = None
        try:
            container = PostgresContainer("postgres:16-alpine", driver=None)
            container.start()
        except Exception as exc:  # no Docker, or a broken container runtime
            failure = type(exc).__name__
        if failure is not None:
            # A Docker client that could not connect leaves its socket open (macOS runners have
            # no Docker). Collect it here, where warnings are ignored, instead of at interpreter
            # exit, where its ResourceWarning would fail the session.
            gc.collect()
            pytest.skip(
                f"PostgreSQL container could not start ({failure}); set DVF_TEST_POSTGRES_URL"
            )
    try:
        yield container.get_connection_url()
    finally:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            container.stop()
