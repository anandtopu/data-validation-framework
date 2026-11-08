"""SCH-12 contract conformance: the ``contract`` check.

The check compares the checked dataset's schema with one object of an ODCS v3 contract or a dvf
expected-schema file: missing and extra columns, types, required (NOT NULL) columns, and the
primary key. Each aspect has a level: ``error`` fails the check, ``warning`` makes it a warning,
and ``ignore`` skips the aspect. Missing columns are always errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from dvf.checks.context import CheckContext, CheckOutcome
from dvf.config.checks import CheckConfig
from dvf.core.datasets import ColumnSchema, DatasetSchema
from dvf.core.enums import Dimension, Status
from dvf.core.errors import PlanError
from dvf.core.types import CanonicalType, Compatibility, LogicalType, classify, parse_native_type
from dvf.discovery.odcs import Contract, ContractObject, ContractProperty, load_contract
from dvf.models.results import SampleSet
from dvf.plugins.base import Check

Level = Literal["error", "warning", "ignore"]

_ACCEPTED_LOGICAL: dict[LogicalType, frozenset[str]] = {
    LogicalType.INTEGER: frozenset({"integer", "number"}),
    LogicalType.DECIMAL: frozenset({"number"}),
    LogicalType.FLOAT: frozenset({"number"}),
    LogicalType.STRING: frozenset({"string"}),
    LogicalType.UUID: frozenset({"string"}),
    LogicalType.BOOLEAN: frozenset({"boolean"}),
    LogicalType.DATE: frozenset({"date"}),
    # ODCS v3.0 has no timestamp type; timestamps are described as 'date' there.
    LogicalType.TIMESTAMP: frozenset({"timestamp", "date"}),
    LogicalType.TIMESTAMP_TZ: frozenset({"timestamp", "date"}),
    LogicalType.TIME: frozenset({"time", "string"}),
    LogicalType.ARRAY: frozenset({"array", "vector"}),
    LogicalType.STRUCT: frozenset({"object"}),
    LogicalType.MAP: frozenset({"map", "object"}),
    LogicalType.JSON: frozenset({"object", "map", "array"}),
    LogicalType.BINARY: frozenset({"string"}),
    LogicalType.INTERVAL: frozenset({"string"}),
}


def odcs_logical_type(ctype: CanonicalType) -> str | None:
    """The ODCS logical type that best describes ``ctype`` (None when unknown)."""
    lt = ctype.logical
    if lt is LogicalType.DECIMAL and ctype.scale == 0:
        return "integer" if (ctype.precision or 0) <= 18 else "number"
    preferred = {
        LogicalType.INTEGER: "integer",
        LogicalType.DECIMAL: "number",
        LogicalType.FLOAT: "number",
        LogicalType.STRING: "string",
        LogicalType.UUID: "string",
        LogicalType.BOOLEAN: "boolean",
        LogicalType.DATE: "date",
        LogicalType.TIMESTAMP: "timestamp",
        LogicalType.TIMESTAMP_TZ: "timestamp",
        LogicalType.TIME: "time",
        LogicalType.ARRAY: "array",
        LogicalType.STRUCT: "object",
        LogicalType.MAP: "map",
        LogicalType.JSON: "object",
        LogicalType.BINARY: "string",
    }
    return preferred.get(lt)


class ContractCheck(CheckConfig):
    """The dataset must conform to an ODCS v3 contract or an expected-schema file (SCH-12)."""

    contract: str = Field(min_length=1)
    """Contract or expected-schema file (YAML or JSON), relative to the configuration file."""
    object: str | None = None
    """Schema object of the contract to check against; defaults to the only object, or the one
    named like the dataset's table."""
    extra_columns: Level = "warning"
    """Columns in the dataset that the contract does not list."""
    types: Level = "error"
    """Type mismatches: a different logical type, or a physical type the dataset cannot hold
    without loss. A dataset type wider than the contract's is always at most a warning."""
    nullability: Level = "warning"
    """Columns the contract marks ``required`` that the dataset declares nullable."""
    primary_key: Level = "warning"
    """A dataset primary key that differs from the contract's (or is not declared)."""
    case_sensitive: bool = False
    """Match column names case-sensitively."""
    type_dialect: str | None = None
    """Dialect of the contract's physical types (default: the dataset's dialect)."""


@dataclass
class Problem:
    """One deviation of the dataset from the contract."""

    column: str
    aspect: str
    expected: str
    actual: str
    level: str
    note: str = ""

    def row(self) -> list[object]:
        return [self.column, self.aspect, self.expected, self.actual, self.level, self.note]


@dataclass
class Conformance:
    """The outcome of comparing a dataset schema with a contract object."""

    problems: list[Problem]
    checked_columns: int
    unknown: list[str]
    """Aspects that could not be checked (for example nullability of files)."""

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "warning"]


def _expected_type(prop: ContractProperty, dialect: str | None) -> CanonicalType | None:
    if not prop.physical_type:
        return None
    parsed = parse_native_type(prop.physical_type, dialect)
    return None if parsed.logical is LogicalType.UNKNOWN else parsed


def _type_problem(
    prop: ContractProperty, column: ColumnSchema, level: Level, dialect: str | None
) -> Problem | None:
    if level == "ignore" or column.type.logical is LogicalType.UNKNOWN:
        return None
    expected = _expected_type(prop, dialect)
    if expected is not None:
        result = classify(expected, column.type)
        if result.level is Compatibility.EXACT or result.level is Compatibility.UNKNOWN:
            return None
        detail = "; ".join(result.reasons)
        if result.level is Compatibility.WIDENING:
            return Problem(
                prop.column,
                "type",
                prop.physical_type or "",
                column.native_type,
                "warning",
                f"the dataset type is wider than the contract's ({detail})",
            )
        return Problem(
            prop.column,
            "type",
            prop.physical_type or "",
            column.native_type,
            level,
            f"{result.level.value}: {detail}",
        )
    if prop.logical_type:
        accepted = _ACCEPTED_LOGICAL.get(column.type.logical, frozenset())
        if prop.logical_type.lower() not in accepted:
            return Problem(
                prop.column,
                "type",
                prop.logical_type,
                column.native_type,
                level,
                f"the dataset type is {odcs_logical_type(column.type) or column.type}",
            )
    return None


def check_conformance(
    obj: ContractObject,
    schema: DatasetSchema,
    config: ContractCheck,
    *,
    dataset_dialect: str | None,
    contract_dialect: str | None = None,
    dataset_primary_key: tuple[str, ...] = (),
) -> Conformance:
    """Compare ``schema`` with ``obj`` using the levels in ``config``."""
    problems: list[Problem] = []
    unknown: list[str] = []
    dialect = config.type_dialect or contract_dialect or dataset_dialect
    columns = [p for p in obj.properties if not p.nested or p.physical_type]

    def find(name: str) -> ColumnSchema | None:
        return schema.column(name, case_sensitive=config.case_sensitive)

    declared_pk = {c.lower() for c in (dataset_primary_key or schema.primary_key)}
    seen: set[str] = set()
    for prop in columns:
        column = find(prop.column)
        if column is None:
            problems.append(
                Problem(
                    prop.column,
                    "missing",
                    "present",
                    "missing",
                    "error",
                    "the contract lists this column",
                )
            )
            continue
        seen.add(column.name)
        found = _type_problem(prop, column, config.types, dialect)
        if found is not None:
            problems.append(found)
        if prop.required and config.nullability != "ignore":
            if column.name.lower() in declared_pk:
                pass  # primary key columns cannot hold NULL, whatever the catalog reports
            elif column.nullable is None:
                unknown.append(f"nullability of {column.name}")
            elif column.nullable:
                problems.append(
                    Problem(
                        prop.column,
                        "nullability",
                        "required (not null)",
                        "nullable",
                        config.nullability,
                    )
                )
    if config.extra_columns != "ignore":
        for column in schema.columns:
            if column.name not in seen:
                problems.append(
                    Problem(
                        column.name,
                        "extra",
                        "absent",
                        "present",
                        config.extra_columns,
                        "not in the contract",
                    )
                )
    expected_pk = obj.primary_key()
    if expected_pk and config.primary_key != "ignore":
        actual_pk = list(dataset_primary_key or schema.primary_key)
        if not actual_pk:
            problems.append(
                Problem(
                    ", ".join(expected_pk),
                    "primary_key",
                    ", ".join(expected_pk),
                    "none declared",
                    config.primary_key,
                    "add a 'unique' check if the dataset cannot declare keys",
                )
            )
        elif {c.lower() for c in actual_pk} != {c.lower() for c in expected_pk}:
            problems.append(
                Problem(
                    ", ".join(expected_pk),
                    "primary_key",
                    ", ".join(expected_pk),
                    ", ".join(actual_pk),
                    config.primary_key,
                )
            )
    return Conformance(problems=problems, checked_columns=len(columns), unknown=unknown)


def resolve_contract_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


class ContractConformance(Check):
    """The dataset conforms to an ODCS v3 contract or an expected-schema file (SCH-12)."""

    dimension = Dimension.SCHEMA
    Config = ContractCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, ContractCheck)
        if ctx.schema is None:
            raise PlanError(f"The schema of {ctx.relation.ref.display()} is not available.")
        path = resolve_contract_path(config.contract, ctx.connector.base_dir)
        contract: Contract = load_contract(path)
        obj = contract.object(config.object, table=ctx.relation.table or ctx.relation.ref.name)
        dataset_pk = ctx.schema.primary_key
        if not dataset_pk and ctx.relation.table is not None:
            dataset_pk = ctx.connector.get_primary_key(ctx.relation)
        result = check_conformance(
            obj,
            ctx.schema,
            config,
            dataset_dialect=ctx.connector.dialect or None,
            contract_dialect=contract.dialect,
            dataset_primary_key=dataset_pk,
        )
        label = f"contract {Path(config.contract).name} object '{obj.name}'"
        errors, warnings = result.errors, result.warnings
        samples = (
            SampleSet(
                columns=["column", "aspect", "expected", "actual", "level", "note"],
                rows=[p.row() for p in result.problems],
            )
            if result.problems
            else None
        )
        summary = _summary(result.problems)
        details: dict[str, Any] = {
            "contract": config.contract,
            "object": obj.name,
            "format": contract.format,
            "api_version": contract.api_version,
            "contract_version": contract.version,
        }
        if result.unknown:
            details["not_checked"] = result.unknown
        common: dict[str, Any] = {
            "observed": summary,
            "expected": "no deviations",
            "unexpected_count": len(errors) + len(warnings),
            "total_count": result.checked_columns,
            "samples": samples,
            "columns": sorted({p.column for p in result.problems if p.aspect != "primary_key"}),
            "details": details,
        }
        if errors:
            return CheckOutcome(
                passed=False,
                message=f"{len(errors)} deviation(s) from {label}: " + _describe(errors),
                hints=[
                    "Fix the dataset or update the contract; set a level to 'warning' or 'ignore' to relax an aspect."
                ],
                **common,
            )
        if warnings:
            return CheckOutcome(
                status=Status.WARNING,
                message=f"{len(warnings)} warning(s) against {label}: " + _describe(warnings),
                **common,
            )
        return CheckOutcome(
            passed=True,
            message=f"conforms to {label} ({result.checked_columns} columns)",
            **common,
        )


def _summary(problems: list[Problem]) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {}
    for problem in problems:
        summary.setdefault(problem.aspect, []).append(problem.column)
    return summary


def _describe(problems: list[Problem]) -> str:
    parts = [f"{p.aspect} {p.column}" for p in problems[:8]]
    return ", ".join(parts) + (" …" if len(problems) > 8 else "")


__all__ = [
    "Conformance",
    "ContractCheck",
    "ContractConformance",
    "Problem",
    "check_conformance",
    "odcs_logical_type",
]
