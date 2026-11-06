from __future__ import annotations

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
