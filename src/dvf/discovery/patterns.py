"""Value formats recognized by profiling and turned into ``regex`` checks.

Patterns are written without anchors: ``regex`` checks match the whole value by default
(``full_match: true``), and every dialect adds the anchors itself. Character classes are spelled
out (``[0-9]`` rather than ``\\d``) so the same pattern works in DuckDB (RE2), PostgreSQL, and
SQLite (Python ``re``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValueFormat:
    name: str
    pattern: str
    description: str


KNOWN_FORMATS: tuple[ValueFormat, ...] = (
    ValueFormat(
        "uuid",
        "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "UUID",
    ),
    ValueFormat("email", r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", "email address"),
    ValueFormat(
        "iso_datetime",
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?"
        r"(Z|[+-][0-9]{2}:?[0-9]{2})?",
        "ISO 8601 date and time",
    ),
    ValueFormat("iso_date", "[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])", "ISO 8601 date"),
    ValueFormat("e164_phone", r"\+[1-9][0-9]{7,14}", "E.164 phone number"),
    ValueFormat("us_zip", "[0-9]{5}(-[0-9]{4})?", "US ZIP code (also 5-digit postal codes)"),
    ValueFormat("uk_postcode", "[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}", "UK postcode"),
    ValueFormat("ca_postal_code", "[A-Z][0-9][A-Z] ?[0-9][A-Z][0-9]", "Canadian postal code"),
)
"""Formats in order of specificity: the first one every value matches wins."""

ODCS_FORMATS: dict[str, str] = {
    "email": KNOWN_FORMATS[1].pattern,
    "uuid": KNOWN_FORMATS[0].pattern,
    "date": KNOWN_FORMATS[3].pattern,
    "date-time": KNOWN_FORMATS[2].pattern,
    "ipv4": r"((25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])",
}
"""ODCS ``logicalTypeOptions.format`` values of strings that dvf checks with a regex."""


def find_format(name: str) -> ValueFormat | None:
    return next((f for f in KNOWN_FORMATS if f.name == name), None)


__all__ = ["KNOWN_FORMATS", "ODCS_FORMATS", "ValueFormat", "find_format"]
