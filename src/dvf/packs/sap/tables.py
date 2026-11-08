"""Reference tables the SAP normalizers use when the system's own tables are not given.

They are defaults, not a copy of any customer system: SAP lets every installation maintain its
own TCURX (currency decimals) and T002 (languages). Read your system's tables and pass the
differences as ``decimals`` or ``overrides`` parameters (see docs/sap.md).
"""

from __future__ import annotations

from types import MappingProxyType

DEFAULT_CURRENCY_DECIMALS = 2
"""Decimals of a currency that has no TCURX entry (SAP's rule)."""

_ZERO_DECIMALS = (
    # ISO 4217 currencies without minor units.
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "ISK",
    "JPY",
    "KMF",
    "KRW",
    "PYG",
    "RWF",
    "UGX",
    "UYI",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
    # Legacy European currencies that SAP systems still hold in old documents.
    "ADP",
    "BEF",
    "ESP",
    "ITL",
    "LUF",
    "PTE",
    "TRL",
)
_THREE_DECIMALS = ("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND")

TCURX_DEFAULTS: MappingProxyType[str, int] = MappingProxyType(
    {**dict.fromkeys(_ZERO_DECIMALS, 0), **dict.fromkeys(_THREE_DECIMALS, 3)}
)
"""Currency keys whose number of decimals is not 2 (TCURX.CURRDEC), following ISO 4217 minor
units. Currencies with four decimals (CLF, UYW) are not included: SAP systems rarely carry them,
and ``decimals: {CLF: 4}`` adds them."""

T002_DEFAULTS: MappingProxyType[str, str] = MappingProxyType(
    {
        "0": "SR",
        "1": "ZH",
        "2": "TH",
        "3": "KO",
        "4": "RO",
        "5": "SL",
        "6": "HR",
        "7": "MS",
        "8": "UK",
        "9": "ET",
        "A": "AR",
        "B": "HE",
        "C": "CS",
        "D": "DE",
        "E": "EN",
        "F": "FR",
        "G": "EL",
        "H": "HU",
        "I": "IT",
        "J": "JA",
        "K": "DA",
        "L": "PL",
        "M": "ZF",
        "N": "NL",
        "O": "NO",
        "P": "PT",
        "Q": "SK",
        "R": "RU",
        "S": "ES",
        "T": "TR",
        "U": "FI",
        "V": "SV",
        "W": "BG",
        "X": "LT",
        "Y": "LV",
        "a": "AF",
        "b": "IS",
        "c": "CA",
        "d": "SH",
        "i": "ID",
    }
)
"""SAP's internal one-character language keys (T002.SPRAS) and their two-character codes
(T002.LAISO) for the languages SAP delivers. ``ZF`` (traditional Chinese) and ``SH`` (Serbian in
Latin script) are SAP's own codes, not ISO 639-1."""

__all__ = [
    "DEFAULT_CURRENCY_DECIMALS",
    "T002_DEFAULTS",
    "TCURX_DEFAULTS",
]
