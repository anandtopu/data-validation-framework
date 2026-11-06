"""An example project-local plugin check.

dvf imports every ``*.py`` file in the folders listed under ``plugins.paths``. The decorator
registers the check; its ``Config`` model is validated and added to the JSON Schema. Execution
methods arrive with the Phase 1 check API.
"""

from __future__ import annotations

from dvf.plugins import Check, CheckConfig, Dimension, register_check


@register_check("acme.vat_number_valid")
class VatNumberValid(Check):
    """VAT numbers must be valid for their country."""

    dimension = Dimension.VALIDITY
    dvf_plugin_api = ">=0.1,<0.2"

    class Config(CheckConfig):
        column: str
        """Column holding the VAT number."""
        country_column: str | None = None
        """Column holding the ISO country code used to pick the VAT format."""
