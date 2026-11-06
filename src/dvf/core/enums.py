"""Enumerations shared across configuration, execution, and results."""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    """How serious a failed check is. Ordered from least to most severe."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def at_least(self, other: Severity) -> bool:
        """Return True if this severity is ``other`` or more severe."""
        return self.rank >= other.rank


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}


class Status(StrEnum):
    """Outcome of a check, dataset, suite, or run."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    WAIVED = "waived"


class Dimension(StrEnum):
    """Data quality dimension a check belongs to."""

    COMPLETENESS = "completeness"
    UNIQUENESS = "uniqueness"
    VALIDITY = "validity"
    CONSISTENCY = "consistency"
    REFERENTIAL_INTEGRITY = "referential_integrity"
    ACCURACY = "accuracy"
    TIMELINESS = "timeliness"
    VOLUME = "volume"
    DISTRIBUTION = "distribution"
    SCHEMA = "schema"
    RECONCILIATION = "reconciliation"
    CUSTOM = "custom"


class Side(StrEnum):
    """Which dataset of a pair a check runs against."""

    SOURCE = "source"
    TARGET = "target"
    BOTH = "both"


class SchemaCheck(StrEnum):
    """Built-in schema validations (catalog IDs SCH-01 to SCH-14)."""

    OBJECT_EXISTS = "object_exists"  # SCH-01
    COLUMNS_PRESENT = "columns_present"  # SCH-02
    TYPE_COMPATIBILITY = "type_compatibility"  # SCH-03
    LENGTH_PRECISION = "length_precision"  # SCH-04
    NULLABILITY = "nullability"  # SCH-05
    CONSTRAINTS = "constraints"  # SCH-06
    PHYSICAL_DESIGN = "physical_design"  # SCH-07
    COLUMN_ORDER = "column_order"  # SCH-08
    COLLATION = "collation"  # SCH-09
    OBJECT_INVENTORY = "object_inventory"  # SCH-10
    SCHEMA_DRIFT = "schema_drift"  # SCH-11
    CONTRACT_CONFORMANCE = "contract_conformance"  # SCH-12
    NESTED_SCHEMA = "nested_schema"  # SCH-13
    NAMING_STANDARDS = "naming_standards"  # SCH-14


class DiagnosticLevel(StrEnum):
    """Severity of a configuration diagnostic."""

    ERROR = "error"
    WARNING = "warning"
