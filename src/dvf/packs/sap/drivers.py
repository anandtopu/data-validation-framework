"""Lazy imports of SAP drivers that users install themselves.

dvf never depends on or bundles SAP software: ``hdbcli`` is distributed by SAP under the SAP
Developer License, and PyRFC needs the proprietary SAP NW RFC SDK. The connectors import them
when a connection opens and explain how to install them when they are missing.
"""

from __future__ import annotations

import importlib
from typing import Any

from dvf.core.errors import DependencyError, ExecutionError

HDBCLI_HINT = (
    "Install SAP's HANA client for Python yourself: 'pip install hdbcli' (SAP Developer License) "
    "or the SAP HANA Client package from SAP. Check that your SAP license allows its use."
)
PYRFC_HINT = (
    "Install the SAP NW RFC SDK from the SAP Support Portal (an S-user is needed), set "
    "SAPNWRFC_HOME, then build and install PyRFC from https://github.com/SAP/PyRFC. PyRFC is no "
    "longer maintained by SAP; consider the 'odata' backend or HANA database access instead."
)


class SapDriverMissingError(DependencyError):
    """An SAP driver is needed but not installed (dvf cannot install it for you)."""

    def __init__(self, package: str, purpose: str, hint: str) -> None:
        ExecutionError.__init__(
            self,
            f"{purpose} needs SAP's '{package}' package, which is not installed. dvf does not "
            "install or bundle SAP drivers.",
            hint=hint,
        )
        self.package = package
        self.extra = ""


def import_driver(module: str, package: str, purpose: str, hint: str) -> Any:
    """Import ``module``, or raise :class:`SapDriverMissingError` explaining how to install it."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise SapDriverMissingError(package, purpose, hint) from exc


__all__ = ["HDBCLI_HINT", "PYRFC_HINT", "SapDriverMissingError", "import_driver"]
