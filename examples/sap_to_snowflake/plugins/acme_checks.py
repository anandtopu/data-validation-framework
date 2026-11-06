"""An example project-local plugin check.

dvf imports every ``*.py`` file in the folders listed under ``plugins.paths``. The decorator
registers the check; its ``Config`` model is validated and added to the JSON Schema.

A :class:`~dvf.plugins.RowCheck` only builds a SQL condition that is true for valid rows. dvf
counts the rows where it is not true, applies ``where`` and ``mostly``, and captures masked
sample rows. The context helpers (``ctx.col``, ``ctx.regex_match``) produce SQL for the dataset's
dialect, so the same check runs on DuckDB, SQLite, PostgreSQL, and files.
"""

from __future__ import annotations

from dvf.plugins import CheckConfig, CheckContext, Dimension, RowCheck, register_check

# Simplified VAT number formats (country prefix plus digits or letters), for the example only.
_VAT_PATTERN = r"(DE[0-9]{9}|FR[0-9A-Z]{2}[0-9]{9}|GB([0-9]{9}|[0-9]{12})|[A-Z]{2}[0-9A-Z]{8,12})"


@register_check("acme.vat_number_valid")
class VatNumberValid(RowCheck):
    """VAT numbers must be valid for their country."""

    dimension = Dimension.VALIDITY
    dvf_plugin_api = ">=0.1,<0.2"

    class Config(CheckConfig):
        column: str
        """Column holding the VAT number."""
        country_column: str | None = None
        """Column holding the ISO country code; when set, the VAT prefix must match it."""

    def predicate(self, ctx: CheckContext) -> str:
        config = self.config
        if not isinstance(config, VatNumberValid.Config):  # narrows the type for type checkers
            raise TypeError("unexpected configuration model")
        vat = ctx.text(ctx.col(config.column))
        condition = ctx.regex_match(vat, _VAT_PATTERN)
        if config.country_column:
            prefix = f"SUBSTR({vat}, 1, 2)"
            condition = f"({condition} AND {prefix} = {ctx.text(ctx.col(config.country_column))})"
        return condition
