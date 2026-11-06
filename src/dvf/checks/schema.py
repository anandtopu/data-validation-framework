"""Schema validations SCH-01 to SCH-06 (BUILD_PROMPT.md section 7.1).

The other catalog entries (SCH-07 to SCH-14) arrive in later phases and report ``skipped``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from dvf.checks.context import CheckOutcome
from dvf.compare.plan import ColumnPairing
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Dimension, SchemaCheck, Status
from dvf.core.types import Compatibility, classify
from dvf.models.results import ColumnSchemaDiff, SampleSet

IMPLEMENTED = (
    SchemaCheck.OBJECT_EXISTS,
    SchemaCheck.COLUMNS_PRESENT,
    SchemaCheck.TYPE_COMPATIBILITY,
    SchemaCheck.LENGTH_PRECISION,
    SchemaCheck.NULLABILITY,
    SchemaCheck.CONSTRAINTS,
)

UniquenessProbe = Callable[[list[str]], tuple[int, int, int]]
"""Given target key columns, return (duplicate rows beyond the first, rows with NULL keys, total rows)."""


@dataclass
class SchemaInputs:
    source_name: str
    target_name: str
    source_exists: bool
    target_exists: bool
    source: DatasetSchema | None = None
    target: DatasetSchema | None = None
    pairing: ColumnPairing | None = None
    source_pk: tuple[str, ...] = ()
    target_pk: tuple[str, ...] = ()
    key_columns: list[tuple[str, str]] = field(default_factory=list)
    """Configured keys as (source, target) names."""
    probe_target_uniqueness: UniquenessProbe | None = None
    target_enforces_constraints: bool = True


def _outcome(check: SchemaCheck, **kwargs: object) -> CheckOutcome:
    return CheckOutcome(type=f"schema.{check.value}", dimension=Dimension.SCHEMA, **kwargs)  # type: ignore[arg-type]


def schema_diff(pairing: ColumnPairing) -> list[ColumnSchemaDiff]:
    """Column-by-column comparison with compatibility classification."""
    rows: list[ColumnSchemaDiff] = []
    for pair in pairing.pairs:
        if pair.source is None:
            rows.append(
                ColumnSchemaDiff(
                    source_column=pair.source_label,
                    target_column=pair.target.name,
                    status="derived",
                    target_type=pair.target.type,
                    target_native_type=pair.target.native_type,
                    reasons=["derived from a source expression"],
                )
            )
            continue
        result = classify(pair.source.type, pair.target.type)
        nullable_change = None
        if pair.source.nullable is not None and pair.target.nullable is not None:
            if pair.source.nullable and not pair.target.nullable:
                nullable_change = "nullable -> not null"
            elif not pair.source.nullable and pair.target.nullable:
                nullable_change = "not null -> nullable"
        rows.append(
            ColumnSchemaDiff(
                source_column=pair.source.name,
                target_column=pair.target.name,
                status="matched",
                source_type=pair.source.type,
                target_type=pair.target.type,
                source_native_type=pair.source.native_type,
                target_native_type=pair.target.native_type,
                compatibility=result.level,
                reasons=list(result.reasons),
                nullable_change=nullable_change,
            )
        )
    for col in pairing.missing_in_target:
        rows.append(
            ColumnSchemaDiff(
                source_column=col.name,
                target_column=None,
                status="missing_in_target",
                source_type=col.type,
                source_native_type=col.native_type,
            )
        )
    for col in pairing.extra_in_target:
        rows.append(
            ColumnSchemaDiff(
                source_column=None,
                target_column=col.name,
                status="extra_in_target",
                target_type=col.type,
                target_native_type=col.native_type,
            )
        )
    return rows


def _table(columns: list[str], rows: list[list[object]]) -> SampleSet | None:
    if not rows:
        return None
    return SampleSet(columns=columns, rows=[list(r) for r in rows])


def run_schema_checks(selected: list[SchemaCheck], inputs: SchemaInputs) -> list[CheckOutcome]:
    """Run the selected schema checks. Missing datasets make the dependent checks ``skipped``."""
    outcomes: list[CheckOutcome] = []
    both_exist = inputs.source_exists and inputs.target_exists
    for check in selected:
        if check not in IMPLEMENTED:
            outcomes.append(
                _outcome(
                    check,
                    status=Status.SKIPPED,
                    message=f"schema check '{check.value}' is not available in this version",
                )
            )
            continue
        if check is SchemaCheck.OBJECT_EXISTS:
            outcomes.append(_object_exists(inputs))
            continue
        if not both_exist or inputs.pairing is None:
            outcomes.append(
                _outcome(check, status=Status.SKIPPED, message="skipped: a dataset does not exist")
            )
            continue
        handler = {
            SchemaCheck.COLUMNS_PRESENT: _columns_present,
            SchemaCheck.TYPE_COMPATIBILITY: _type_compatibility,
            SchemaCheck.LENGTH_PRECISION: _length_precision,
            SchemaCheck.NULLABILITY: _nullability,
            SchemaCheck.CONSTRAINTS: _constraints,
        }[check]
        outcomes.append(handler(inputs))
    return outcomes


def _object_exists(inputs: SchemaInputs) -> CheckOutcome:
    missing = [
        f"{side} '{name}'"
        for side, name, exists in (
            ("source", inputs.source_name, inputs.source_exists),
            ("target", inputs.target_name, inputs.target_exists),
        )
        if not exists
    ]
    if missing:
        return _outcome(
            SchemaCheck.OBJECT_EXISTS,
            passed=False,
            message="not found: " + ", ".join(missing),
            observed=missing,
            hints=["Check the table name, path, or connection, and that the load has run."],
        )
    message = "source and target exist" if inputs.target_name else "the dataset exists"
    return _outcome(SchemaCheck.OBJECT_EXISTS, passed=True, message=message)


def _columns_present(inputs: SchemaInputs) -> CheckOutcome:
    pairing = inputs.pairing
    assert pairing is not None
    missing = [c.name for c in pairing.missing_in_target]
    extra = [c.name for c in pairing.extra_in_target]
    problems = list(pairing.problems)
    rows: list[list[object]] = [[m, "missing_in_target"] for m in missing]
    rows.extend([e, "extra_in_target"] for e in extra)
    rows.extend([p, "mapping_error"] for p in problems)
    parts = []
    if missing:
        parts.append(f"{len(missing)} source column(s) missing in the target: {', '.join(missing)}")
    if problems:
        parts.append("; ".join(problems))
    if extra:
        parts.append(f"{len(extra)} extra target column(s): {', '.join(extra)}")
    failed = bool(missing or problems)
    return _outcome(
        SchemaCheck.COLUMNS_PRESENT,
        passed=not failed,
        message="; ".join(parts) or f"all {len(pairing.pairs)} paired columns are present",
        observed={"missing_in_target": missing, "extra_in_target": extra},
        expected={"missing_in_target": []},
        unexpected_count=len(missing) + len(problems),
        total_count=len(pairing.pairs) + len(missing),
        samples=_table(["column", "status"], rows),
        hints=["Map renamed columns under 'columns.mappings', or exclude out-of-scope ones."]
        if failed
        else [],
    )


def _type_compatibility(inputs: SchemaInputs) -> CheckOutcome:
    pairing = inputs.pairing
    assert pairing is not None
    issues: list[list[object]] = []
    changes: list[list[object]] = []
    for pair in pairing.pairs:
        if pair.source is None:
            continue
        result = classify(pair.source.type, pair.target.type)
        same_logical = pair.source.type.logical is pair.target.type.logical
        row: list[object] = [
            pair.target.name,
            pair.source.native_type,
            pair.target.native_type,
            result.level.value,
            "; ".join(result.reasons),
        ]
        if result.level is Compatibility.INCOMPATIBLE or (
            not same_logical and result.level is Compatibility.LOSSY
        ):
            issues.append(row)
        elif result.level is Compatibility.UNKNOWN or not same_logical:
            changes.append(row)
    columns = ["column", "source_type", "target_type", "compatibility", "reasons"]
    if issues:
        return _outcome(
            SchemaCheck.TYPE_COMPATIBILITY,
            passed=False,
            message=f"{len(issues)} column(s) have incompatible or lossy type changes: "
            + ", ".join(str(r[0]) for r in issues),
            unexpected_count=len(issues),
            total_count=len(pairing.pairs),
            samples=_table(columns, issues + changes),
            columns=[str(r[0]) for r in issues],
        )
    if changes:
        return _outcome(
            SchemaCheck.TYPE_COMPATIBILITY,
            status=Status.WARNING,
            message=f"{len(changes)} column(s) changed type without data loss or could not be classified: "
            + ", ".join(str(r[0]) for r in changes),
            unexpected_count=len(changes),
            total_count=len(pairing.pairs),
            samples=_table(columns, changes),
            columns=[str(r[0]) for r in changes],
        )
    return _outcome(
        SchemaCheck.TYPE_COMPATIBILITY,
        passed=True,
        message=f"all {len(pairing.pairs)} paired columns have compatible types",
        total_count=len(pairing.pairs),
    )


def _length_precision(inputs: SchemaInputs) -> CheckOutcome:
    pairing = inputs.pairing
    assert pairing is not None
    issues: list[list[object]] = []
    for pair in pairing.pairs:
        if pair.source is None or pair.source.type.logical is not pair.target.type.logical:
            continue
        result = classify(pair.source.type, pair.target.type)
        if result.level is Compatibility.LOSSY:
            issues.append(
                [
                    pair.target.name,
                    pair.source.native_type,
                    pair.target.native_type,
                    "; ".join(result.reasons),
                ]
            )
    if issues:
        return _outcome(
            SchemaCheck.LENGTH_PRECISION,
            passed=False,
            message=f"{len(issues)} column(s) risk truncation: "
            + ", ".join(str(r[0]) for r in issues),
            unexpected_count=len(issues),
            total_count=len(pairing.pairs),
            samples=_table(["column", "source_type", "target_type", "reasons"], issues),
            columns=[str(r[0]) for r in issues],
            hints=["Widen the target columns or confirm the values fit with a 'length' check."],
        )
    return _outcome(
        SchemaCheck.LENGTH_PRECISION,
        passed=True,
        message="no length, precision, or scale reductions",
        total_count=len(pairing.pairs),
    )


def _nullability(inputs: SchemaInputs) -> CheckOutcome:
    pairing = inputs.pairing
    assert pairing is not None
    risky: list[list[object]] = []
    relaxed: list[list[object]] = []
    known = 0
    for pair in pairing.pairs:
        if pair.source is None or pair.source.nullable is None or pair.target.nullable is None:
            continue
        known += 1
        if pair.source.nullable and not pair.target.nullable:
            risky.append([pair.target.name, "nullable", "not null"])
        elif not pair.source.nullable and pair.target.nullable:
            relaxed.append([pair.target.name, "not null", "nullable"])
    columns = ["column", "source", "target"]
    if known == 0:
        return _outcome(
            SchemaCheck.NULLABILITY,
            status=Status.SKIPPED,
            message="nullability is not declared by one or both datasets (files and queries)",
        )
    if risky:
        return _outcome(
            SchemaCheck.NULLABILITY,
            status=Status.WARNING,
            message=f"{len(risky)} column(s) are NOT NULL in the target but nullable in the source: "
            + ", ".join(str(r[0]) for r in risky),
            unexpected_count=len(risky),
            total_count=known,
            samples=_table(columns, risky + relaxed),
            columns=[str(r[0]) for r in risky],
            hints=["Add a 'not_null' check on the source columns to prove no NULLs need loading."],
        )
    message = f"nullability is consistent for {known} column(s)"
    if relaxed:
        message += f"; {len(relaxed)} relaxed to nullable in the target"
    return _outcome(
        SchemaCheck.NULLABILITY,
        passed=True,
        message=message,
        total_count=known,
        samples=_table(columns, relaxed),
    )


def _constraints(inputs: SchemaInputs) -> CheckOutcome:
    pairing = inputs.pairing
    assert pairing is not None
    expected_source = list(inputs.source_pk) or [s for s, _ in inputs.key_columns]
    if not expected_source:
        return _outcome(
            SchemaCheck.CONSTRAINTS,
            status=Status.SKIPPED,
            message="no primary key is declared in the source and no keys are configured",
        )
    expected_target: list[str] = []
    for name in expected_source:
        pair = pairing.target_for_source(name)
        target_name = pair.target.name if pair is not None else None
        if target_name is None:
            target_name = next(
                (t for s, t in inputs.key_columns if s.lower() == name.lower()), None
            )
        if target_name is None:
            return _outcome(
                SchemaCheck.CONSTRAINTS,
                passed=False,
                message=f"primary key column '{name}' has no counterpart in the target",
            )
        expected_target.append(target_name)
    if inputs.target_pk:
        if [c.lower() for c in inputs.target_pk] == [c.lower() for c in expected_target]:
            return _outcome(
                SchemaCheck.CONSTRAINTS,
                passed=True,
                message=f"target primary key ({', '.join(inputs.target_pk)}) matches the source",
            )
        return _outcome(
            SchemaCheck.CONSTRAINTS,
            passed=False,
            message=f"target primary key ({', '.join(inputs.target_pk)}) differs from the expected "
            f"({', '.join(expected_target)})",
            observed=list(inputs.target_pk),
            expected=expected_target,
        )
    if inputs.probe_target_uniqueness is None:
        return _outcome(
            SchemaCheck.CONSTRAINTS,
            status=Status.SKIPPED,
            message="the target declares no primary key and uniqueness could not be verified",
        )
    duplicates, null_keys, total = inputs.probe_target_uniqueness(expected_target)
    cols = ", ".join(expected_target)
    ok = duplicates == 0 and null_keys == 0
    return _outcome(
        SchemaCheck.CONSTRAINTS,
        passed=ok,
        message=(
            f"the target declares no primary key; a data check found ({cols}) unique and not NULL"
            if ok
            else f"the target declares no primary key and ({cols}) is not a valid key: "
            f"{duplicates:,} duplicate row(s), {null_keys:,} row(s) with NULL keys"
        ),
        observed={"duplicate_rows": duplicates, "null_key_rows": null_keys},
        expected={"duplicate_rows": 0, "null_key_rows": 0},
        unexpected_count=duplicates + null_keys,
        total_count=total,
        columns=expected_target,
        details={"converted_to_data_check": True},
    )


__all__ = ["IMPLEMENTED", "SchemaInputs", "run_schema_checks", "schema_diff"]
