"""dvf: configuration-driven data and schema validation for data migrations and data engineering."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("dvf")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__", "foreach_batch_validator", "run", "validate_dataframe"]

_LAZY = {"run", "validate_dataframe", "foreach_batch_validator"}


def __getattr__(name: str) -> object:
    """``dvf.run`` and ``dvf.validate_dataframe`` without importing everything on ``import dvf``."""
    if name in _LAZY:
        from dvf import api

        return getattr(api, name)
    raise AttributeError(f"module 'dvf' has no attribute {name!r}")
