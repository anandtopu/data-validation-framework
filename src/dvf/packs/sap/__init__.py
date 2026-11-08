"""SAP pack: HANA and RFC connectors, DDIC schemas, SAP normalizers, and business object templates.

``register`` is called by :func:`dvf.packs.register_packs` with the built-ins. Nothing here imports
an SAP driver: ``hdbcli`` and ``pyrfc`` are imported when a connection opens, and users install
them themselves (dvf never bundles SAP software).

* Normalizers (``sap_alpha`` ... ``sap_quan``) are verified by parity tests against DuckDB,
  SQLite, PostgreSQL, Polars, and Spark, so they register as stable.
* The ``sap_hana`` and ``sap_rfc`` connectors are unit-tested against fake drivers only, so they
  register as beta until someone runs their live integration tests.
"""

from __future__ import annotations

from dvf.plugins.registry import PluginKind, Registry

_HANA_DESCRIPTION = "SAP HANA at database level (user-installed hdbcli)"
_RFC_DESCRIPTION = (
    "SAP ECC and S/4HANA through RFC or OData (backends: pyrfc, custom_fm, odata; "
    "nwrfcsdk declared, not implemented)"
)


def register(registry: Registry) -> None:
    """Attach this pack's implementations to the built-in declarations."""
    from dvf.connectors.sap_hana import SapHanaConnector
    from dvf.connectors.sap_rfc import SapRfcConnector
    from dvf.packs import attach
    from dvf.packs.sap.normalizers import SAP_NORMALIZERS

    for name, implementation in SAP_NORMALIZERS.items():
        attach(registry, PluginKind.NORMALIZER, name, implementation)
    attach(
        registry,
        PluginKind.CONNECTOR,
        "sap_hana",
        SapHanaConnector,
        maturity="beta",
        description=_HANA_DESCRIPTION,
    )
    attach(
        registry,
        PluginKind.CONNECTOR,
        "sap_rfc",
        SapRfcConnector,
        maturity="beta",
        description=_RFC_DESCRIPTION,
    )
