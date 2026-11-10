"""Schema validations SCH-01 to SCH-11 and SCH-14 (BUILD_PROMPT.md section 7.1).

SCH-12 (contract conformance) and SCH-13 (semi-structured schemas) are check types (``contract``
and ``document_schema``), so listing them under ``schema_checks`` reports ``skipped`` with a
pointer. SCH-07, SCH-09, and SCH-10 read catalog metadata through the optional connector methods
:meth:`~dvf.plugins.base.Connector.get_physical_design`,
:meth:`~dvf.plugins.base.Connector.get_collations`, and
:meth:`~dvf.plugins.base.Connector.get_object_inventory`; a connector that does not implement
them makes the check ``skipped`` with a message. SCH-11 compares schemas with a snapshot saved by
the previous run (under ``<state_dir>/schemas``) or with a committed snapshot file.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvf.checks.context import CheckOutcome
from dvf.compare.plan import ColumnPairing
from dvf.config.models import SchemaOptionsConfig
from dvf.core.datasets import DatasetSchema
from dvf.core.enums import Dimension, SchemaCheck, Status
from dvf.core.types import CanonicalType, Compatibility, LogicalType, classify
from dvf.models.results import ColumnSchemaDiff, SampleSet

if TYPE_CHECKING:
    from dvf.plugins.base import Connector, Relation

log = logging.getLogger(__name__)

IMPLEMENTED = (
    SchemaCheck.OBJECT_EXISTS,
    SchemaCheck.COLUMNS_PRESENT,
    SchemaCheck.TYPE_COMPATIBILITY,
    SchemaCheck.LENGTH_PRECISION,
    SchemaCheck.NULLABILITY,
    SchemaCheck.CONSTRAINTS,
    SchemaCheck.PHYSICAL_DESIGN,
    SchemaCheck.COLUMN_ORDER,
    SchemaCheck.COLLATION,
    SchemaCheck.OBJECT_INVENTORY,
    SchemaCheck.SCHEMA_DRIFT,
    SchemaCheck.NAMING_STANDARDS,
)

_CHECK_TYPES = {
    SchemaCheck.CONTRACT_CONFORMANCE: "contract",
    SchemaCheck.NESTED_SCHEMA: "document_schema",
}
"""Catalog entries implemented as check types rather than ``schema_checks`` entries."""

UniquenessProbe = Callable[[list[str]], tuple[int, int, int]]
"""Given target key columns, return (duplicate rows beyond the first, rows with NULL keys, total rows)."""

SNAPSHOT_FORMAT = "dvf-schema-snapshot"
SNAPSHOT_VERSION = 1
MAX_LISTED_OBJECTS = 500
"""Object names kept per type and side in the object inventory's details."""


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
    normalized_types: dict[str, tuple[CanonicalType, CanonicalType]] = field(default_factory=dict)
    """Source and target types after each column's normalizers, by target column name; type
    compatibility compares these instead of the declared types."""
    source_connector: Connector | None = None
    target_connector: Connector | None = None
    source_relation: Relation | None = None
    target_relation: Relation | None = None
    """Connectors and relations, for the catalog metadata of SCH-07, SCH-09, and SCH-10."""
    options: SchemaOptionsConfig = field(default_factory=SchemaOptionsConfig)
    """Naming rules (SCH-14) and the drift baseline (SCH-11)."""
    snapshot_path: Path | None = None
    """Where this table's ``previous_run`` schema snapshot is read and written (SCH-11)."""
    base_dir: Path | None = None
    """The configuration's folder, for a committed drift baseline file."""
    suite_name: str = ""
    table_name: str = ""
    run_id: str | None = None


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
            check_type = _CHECK_TYPES.get(check)
            outcomes.append(
                _outcome(
                    check,
                    status=Status.SKIPPED,
                    message=(
                        f"'{check.value}' is a check type, not a schema check: add "
                        f"{{type: {check_type}}} under 'checks'"
                        if check_type
                        else f"schema check '{check.value}' is not available in this version"
                    ),
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
            SchemaCheck.PHYSICAL_DESIGN: _physical_design,
            SchemaCheck.COLUMN_ORDER: _column_order,
            SchemaCheck.COLLATION: _collation,
            SchemaCheck.OBJECT_INVENTORY: _object_inventory,
            SchemaCheck.SCHEMA_DRIFT: _schema_drift,
            SchemaCheck.NAMING_STANDARDS: _naming_standards,
        }[check]
        try:
            outcomes.append(handler(inputs))
        except Exception as exc:  # one broken catalog query must not stop the other checks
            log.debug("schema check %s failed", check.value, exc_info=True)
            outcomes.append(
                _outcome(
                    check,
                    status=Status.ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                    hints=["Check that the dvf login can read the database catalog."],
                )
            )
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
        source_type, target_type = inputs.normalized_types.get(
            pair.target.name, (pair.source.type, pair.target.type)
        )
        result = classify(source_type, target_type)
        same_logical = source_type.logical is target_type.logical
        reasons = list(result.reasons)
        if pair.target.name in inputs.normalized_types:
            reasons.append(f"compared after normalizers as {source_type} -> {target_type}")
        row: list[object] = [
            pair.target.name,
            pair.source.native_type,
            pair.target.native_type,
            result.level.value,
            "; ".join(reasons),
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


# ====================================================================== catalog metadata helpers


def _metadata(inputs: SchemaInputs, side: str, method: str) -> tuple[Any | None, str | None]:
    """Call an optional connector metadata method; return (value, problem)."""
    conn = inputs.source_connector if side == "source" else inputs.target_connector
    rel = inputs.source_relation if side == "source" else inputs.target_relation
    if conn is None or rel is None:
        return None, f"no {side} connection"
    try:
        return getattr(conn, method)(rel), None
    except Exception as exc:
        log.debug("%s.%s failed", side, method, exc_info=True)
        return None, f"{side}: {type(exc).__name__}: {exc}"


def _target_name_of(inputs: SchemaInputs, source_column: str) -> str:
    """The target column paired with a source column (or the name itself, lower case)."""
    pairing = inputs.pairing
    pair = pairing.target_for_source(source_column) if pairing is not None else None
    return (pair.target.name if pair is not None else source_column).lower()


def _unavailable(check: SchemaCheck, what: str, problems: Sequence[str | None]) -> CheckOutcome:
    found = [p for p in problems if p]
    message = f"{what} is not available for these connections"
    if found:
        message += ": " + "; ".join(found)
    return _outcome(check, status=Status.SKIPPED, message=message)


# ====================================================================== SCH-07 physical design


def _index_text(index: Any) -> str:
    flags = [f for f, on in (("primary", index.primary), ("unique", index.unique)) if on]
    text = f"{index.name} ({', '.join(index.columns)})"
    if index.kind:
        flags.append(str(index.kind).lower())
    if index.predicate:
        flags.append(f"where {index.predicate}")
    return text + (f" [{', '.join(flags)}]" if flags else "")


def _physical_design(inputs: SchemaInputs) -> CheckOutcome:
    check = SchemaCheck.PHYSICAL_DESIGN
    source, s_problem = _metadata(inputs, "source", "get_physical_design")
    target, t_problem = _metadata(inputs, "target", "get_physical_design")
    if source is None and target is None:
        return _unavailable(check, "physical design metadata", [s_problem, t_problem])
    columns = ["item", "source", "target", "status"]
    rows: list[list[object]] = []
    details: dict[str, Any] = {}
    for side, design in (("source", source), ("target", target)):
        if design is not None:
            details[side] = {
                "indexes": [_index_text(i) for i in design.indexes],
                "partitioning": design.partitioning,
                "clustering": list(design.clustering),
                "distribution": design.distribution,
                "sort_keys": list(design.sort_keys),
                "notes": list(design.notes),
            }
    if source is None or target is None:
        known, side = (source, "source") if source is not None else (target, "target")
        assert known is not None
        return _outcome(
            check,
            passed=True,
            message=f"physical design is known for the {side} only: "
            f"{len(known.indexes)} index(es), partitioning {known.partitioning or 'none'}",
            details=details,
            severity_cap="warning",
        )
    differences = 0
    target_by_columns: dict[tuple[str, ...], Any] = {}
    for index in target.indexes:
        target_by_columns.setdefault(tuple(c.lower() for c in index.columns), index)
    matched_targets: set[str] = set()
    for index in source.indexes:
        mapped = tuple(_target_name_of(inputs, c) for c in index.columns)
        match = target_by_columns.get(mapped)
        if match is None:
            status = "missing_in_target"
            differences += 1
        elif index.unique and not match.unique:
            status = "not_unique_in_target"
            differences += 1
        else:
            status = "matched"
        if match is not None:
            matched_targets.add(match.name)
        rows.append(
            ["index", _index_text(index), _index_text(match) if match is not None else "", status]
        )
    for index in target.indexes:
        if index.name not in matched_targets:
            rows.append(["index", "", _index_text(index), "extra_in_target"])
    for item, s_value, t_value in (
        ("partitioning", source.partitioning, target.partitioning),
        ("clustering", ", ".join(source.clustering), ", ".join(target.clustering)),
        ("distribution", source.distribution, target.distribution),
        ("sort keys", ", ".join(source.sort_keys), ", ".join(target.sort_keys)),
    ):
        if not s_value and not t_value:
            continue
        differs = bool(s_value) != bool(t_value)
        if item == "partitioning" and differs:
            differences += 1
        rows.append([item, s_value or "none", t_value or "none", "differs" if differs else "both"])
    missing = sum(1 for r in rows if r[3] == "missing_in_target")
    weaker = sum(1 for r in rows if r[3] == "not_unique_in_target")
    parts = []
    if missing:
        parts.append(f"{missing} source index(es) have no target index on the same columns")
    if weaker:
        parts.append(f"{weaker} unique index(es) are not unique in the target")
    if any(r[0] == "partitioning" and r[3] == "differs" for r in rows):
        parts.append(
            f"partitioning differs (source {source.partitioning or 'none'}, "
            f"target {target.partitioning or 'none'})"
        )
    message = (
        "informational: " + "; ".join(parts)
        if parts
        else f"every source index has a target counterpart ({len(source.indexes)} source, "
        f"{len(target.indexes)} target index(es))"
    )
    return _outcome(
        check,
        passed=differences == 0,
        message=message,
        unexpected_count=differences,
        total_count=len(source.indexes),
        samples=_table(columns, rows),
        details=details,
        hints=["Physical design is informational: recreate indexes the target workload needs."]
        if differences
        else [],
        severity_cap="warning",
    )


# ====================================================================== SCH-08 column order


def _positions(schema: DatasetSchema) -> dict[str, int]:
    return {
        c.name.lower(): (c.ordinal if c.ordinal is not None else i + 1)
        for i, c in enumerate(schema.columns)
    }


def _column_order(inputs: SchemaInputs) -> CheckOutcome:
    pairing, source, target = inputs.pairing, inputs.source, inputs.target
    assert pairing is not None
    if source is None or target is None:
        return _outcome(SchemaCheck.COLUMN_ORDER, status=Status.SKIPPED, message="no schemas")
    s_pos, t_pos = _positions(source), _positions(target)
    pairs = [p for p in pairing.pairs if p.source is not None]
    expected = [
        p.target.name
        for p in sorted(pairs, key=lambda p: s_pos.get(p.source.name.lower(), 0))  # type: ignore[union-attr]
    ]
    actual = [
        p.target.name for p in sorted(pairs, key=lambda p: t_pos.get(p.target.name.lower(), 0))
    ]
    rows: list[list[object]] = [
        [i + 1, e, a] for i, (e, a) in enumerate(zip(expected, actual, strict=True)) if e != a
    ]
    if not rows:
        return _outcome(
            SchemaCheck.COLUMN_ORDER,
            passed=True,
            message=f"the {len(pairs)} paired columns are in the source's order",
            total_count=len(pairs),
        )
    first = rows[0]
    return _outcome(
        SchemaCheck.COLUMN_ORDER,
        passed=False,
        message=f"{len(rows)} of {len(pairs)} paired columns are in a different position; at "
        f"position {first[0]} the source order has '{first[1]}', the target '{first[2]}'",
        observed=actual,
        expected=expected,
        unexpected_count=len(rows),
        total_count=len(pairs),
        samples=_table(["position", "source_order", "target_order"], rows),
        columns=[str(r[2]) for r in rows],
        hints=["Column order matters to positional loads, SELECT * consumers, and file exports."],
    )


# ====================================================================== SCH-09 collation

_TEXT_TYPES = (LogicalType.STRING, LogicalType.JSON, LogicalType.UUID)


def _yes_no(value: bool | None, yes: str, no: str) -> str:
    return "unknown" if value is None else (yes if value else no)


def _collation(inputs: SchemaInputs) -> CheckOutcome:
    check = SchemaCheck.COLLATION
    pairing = inputs.pairing
    assert pairing is not None
    source, s_problem = _metadata(inputs, "source", "get_collations")
    target, t_problem = _metadata(inputs, "target", "get_collations")
    if source is None or target is None:
        what = "collation metadata"
        if source is not None or target is not None:
            what += f" of the {'target' if target is None else 'source'}"
        return _unavailable(check, what, [s_problem, t_problem])
    columns = [
        "column",
        "source_collation",
        "target_collation",
        "source_charset",
        "target_charset",
        "issue",
    ]
    issues: list[list[object]] = []
    renamed: list[list[object]] = []
    text_pairs = 0
    for pair in pairing.pairs:
        if pair.source is None:
            continue
        if (
            pair.source.type.logical not in _TEXT_TYPES
            and pair.target.type.logical not in _TEXT_TYPES
        ):
            continue
        text_pairs += 1
        s, t = source.for_column(pair.source.name), target.for_column(pair.target.name)
        problems = []
        if (
            None not in (s.case_sensitive, t.case_sensitive)
            and s.case_sensitive != t.case_sensitive
        ):
            problems.append(
                f"{_yes_no(s.case_sensitive, 'case-sensitive', 'case-insensitive')} in the "
                f"source, {_yes_no(t.case_sensitive, 'case-sensitive', 'case-insensitive')} "
                "in the target"
            )
        if (
            None not in (s.accent_sensitive, t.accent_sensitive)
            and s.accent_sensitive != t.accent_sensitive
        ):
            problems.append(
                f"{_yes_no(s.accent_sensitive, 'accent-sensitive', 'accent-insensitive')} in "
                f"the source, "
                f"{_yes_no(t.accent_sensitive, 'accent-sensitive', 'accent-insensitive')} in "
                "the target"
            )
        if s.unicode is True and t.unicode is False:
            problems.append("the target character set cannot store every Unicode character")
        row: list[object] = [
            pair.target.name,
            s.collation,
            t.collation,
            s.charset,
            t.charset,
            "; ".join(problems),
        ]
        if problems:
            issues.append(row)
        elif (s.collation or "").lower() != (t.collation or "").lower():
            renamed.append(row)
    details = {
        "source": {"default_collation": source.default_collation, "charset": source.charset},
        "target": {"default_collation": target.default_collation, "charset": target.charset},
    }
    if not text_pairs:
        return _outcome(
            check, passed=True, message="no paired text columns", details=details, total_count=0
        )
    if issues:
        return _outcome(
            check,
            passed=False,
            message=f"{len(issues)} text column(s) compare or store text differently: "
            + ", ".join(str(r[0]) for r in issues),
            unexpected_count=len(issues),
            total_count=text_pairs,
            samples=_table(columns, issues + renamed),
            columns=[str(r[0]) for r in issues],
            details=details,
            hints=[
                "Different case or accent sensitivity changes uniqueness, joins, GROUP BY, and "
                "filters; align the target collation or set normalization.case_sensitive: false "
                "where the difference is intended."
            ],
        )
    message = f"comparison semantics match for {text_pairs} text column(s)"
    if renamed:
        message += f"; {len(renamed)} differ in collation name only"
    return _outcome(
        check,
        passed=True,
        message=message,
        total_count=text_pairs,
        samples=_table(columns, renamed),
        details=details,
    )


# ====================================================================== SCH-10 object inventory


def _object_inventory(inputs: SchemaInputs) -> CheckOutcome:
    from dvf.plugins.base import OBJECT_TYPES

    check = SchemaCheck.OBJECT_INVENTORY
    source, s_problem = _metadata(inputs, "source", "get_object_inventory")
    target, t_problem = _metadata(inputs, "target", "get_object_inventory")
    if source is None and target is None:
        return _unavailable(check, "the object inventory", [s_problem, t_problem])
    kinds = list(OBJECT_TYPES)
    for inventory in (source, target):
        if inventory is not None:
            kinds += [k for k in inventory.objects if k not in kinds]
    details: dict[str, Any] = {}
    for side, inventory in (("source", source), ("target", target)):
        if inventory is not None:
            details[side] = {
                "schema": inventory.schema,
                "objects": {k: list(v[:MAX_LISTED_OBJECTS]) for k, v in inventory.objects.items()},
                "unsupported": list(inventory.unsupported),
            }
    rows: list[list[object]] = []
    missing_total = 0
    for kind in kinds:
        s_names = {n.lower(): n for n in (source.objects.get(kind, ()) if source else ())}
        t_names = {n.lower(): n for n in (target.objects.get(kind, ()) if target else ())}
        if not s_names and not t_names:
            continue
        if source is None or target is None:
            rows.append([kind, len(s_names), len(t_names), "", ""])
            continue
        missing = sorted(s_names[k] for k in set(s_names) - set(t_names))
        extra = sorted(t_names[k] for k in set(t_names) - set(s_names))
        missing_total += len(missing)
        note = ""
        if missing and kind in target.unsupported:
            note = f" (the target system has no {kind}s)"
        rows.append(
            [
                kind,
                len(s_names),
                len(t_names),
                ", ".join(missing[:20]) + note,
                ", ".join(extra[:20]),
            ]
        )
    columns = [
        "object_type",
        "source_count",
        "target_count",
        "missing_in_target",
        "extra_in_target",
    ]
    counts = ", ".join(f"{r[0]} {r[1]}/{r[2]}" for r in rows) or "no objects"
    if source is None or target is None:
        side = "source" if source is not None else "target"
        return _outcome(
            check,
            passed=True,
            message=f"object inventory is known for the {side} only ({counts})",
            samples=_table(columns, rows),
            details=details,
            severity_cap="warning",
        )
    return _outcome(
        check,
        passed=missing_total == 0,
        message=(
            f"informational: {missing_total} source object(s) have no target object of the same "
            f"type and name (source/target counts: {counts})"
            if missing_total
            else f"every source object has a target counterpart (source/target counts: {counts})"
        ),
        unexpected_count=missing_total,
        samples=_table(columns, rows),
        details=details,
        hints=["Migrate or retire the listed objects; names are compared case-insensitively."]
        if missing_total
        else [],
        severity_cap="warning",
    )


# ====================================================================== SCH-11 schema drift


def snapshot_side(schema: DatasetSchema, dataset: str) -> dict[str, Any]:
    """The JSON form of one dataset's schema in a drift snapshot."""
    return {
        "dataset": dataset,
        "columns": [
            {
                "name": c.name,
                "native_type": c.native_type,
                "type": str(c.type),
                "nullable": c.nullable,
                "ordinal": c.ordinal if c.ordinal is not None else i + 1,
            }
            for i, c in enumerate(schema.columns)
        ],
        "primary_key": list(schema.primary_key),
    }


def snapshot_path(state_dir: Path, suite: str, table: str) -> Path:
    """Where a table's ``previous_run`` snapshot lives: ``<state_dir>/schemas/<suite>/<table>.json``."""

    def safe(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.=-]+", "_", value)[:120] or "_"

    return state_dir / "schemas" / safe(suite) / f"{safe(table)}.json"


def _load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        return yaml.safe_load(text)
    return json.loads(text)


def _baseline_for_table(document: Any, inputs: SchemaInputs) -> Mapping[str, Any] | None:
    """The snapshot of this table in a baseline document (one table, or ``tables`` by name)."""
    if not isinstance(document, Mapping):
        return None
    tables = document.get("tables")
    if isinstance(tables, Mapping):
        for key in (f"{inputs.suite_name}.{inputs.table_name}", inputs.table_name):
            found = tables.get(key)
            if isinstance(found, Mapping):
                return found
        return None
    return document


def _write_snapshot(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(document, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def _norm_type(value: Any) -> str:
    return re.sub(r"\s+", "", str(value)).lower()


def _drift_rows(
    side: str, before: Mapping[str, Any], after: Mapping[str, Any], inputs: SchemaInputs
) -> list[list[object]]:
    config = inputs.options.drift
    ignored = [p.lower() for p in config.ignore_columns]

    def kept(name: str) -> bool:
        return not any(fnmatchcase(name.lower(), p) for p in ignored)

    def columns(doc: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        found: dict[str, Mapping[str, Any]] = {}
        for entry in doc.get("columns") or []:
            if isinstance(entry, Mapping) and entry.get("name") and kept(str(entry["name"])):
                found[str(entry["name"]).lower()] = entry
        return found

    old, new = columns(before), columns(after)
    rows: list[list[object]] = []
    compare = set(config.compare)
    if "columns" in compare:
        rows += [
            [side, "column_added", new[k]["name"], None, _describe(new[k])]
            for k in new
            if k not in old
        ]
        rows += [
            [side, "column_removed", old[k]["name"], _describe(old[k]), None]
            for k in old
            if k not in new
        ]
    for key in [k for k in new if k in old]:
        o, n = old[key], new[key]
        if "types" in compare:
            changed = False
            if o.get("native_type") is not None and n.get("native_type") is not None:
                changed = _norm_type(o["native_type"]) != _norm_type(n["native_type"])
            elif o.get("type") is not None and n.get("type") is not None:
                changed = _norm_type(o["type"]) != _norm_type(n["type"])
            if changed:
                rows.append([side, "type_changed", n["name"], _type_of(o), _type_of(n)])
        if (
            "nullability" in compare
            and o.get("nullable") is not None
            and n.get("nullable") is not None
            and bool(o["nullable"]) != bool(n["nullable"])
        ):
            rows.append(
                [
                    side,
                    "nullability_changed",
                    n["name"],
                    "nullable" if o["nullable"] else "not null",
                    "nullable" if n["nullable"] else "not null",
                ]
            )
    if "primary_key" in compare and "primary_key" in before:
        old_pk = [str(c).lower() for c in before.get("primary_key") or []]
        new_pk = [str(c).lower() for c in after.get("primary_key") or []]
        if old_pk != new_pk:
            rows.append(
                [
                    side,
                    "primary_key_changed",
                    None,
                    ", ".join(old_pk) or "none",
                    ", ".join(new_pk) or "none",
                ]
            )
    if "order" in compare:
        old_order = [
            k for k in sorted(old, key=lambda k: int(old[k].get("ordinal") or 0)) if k in new
        ]
        new_order = [
            k for k in sorted(new, key=lambda k: int(new[k].get("ordinal") or 0)) if k in old
        ]
        if old_order != new_order:
            rows.append([side, "order_changed", None, ", ".join(old_order), ", ".join(new_order)])
    return rows


def _type_of(entry: Mapping[str, Any]) -> str:
    return str(entry.get("native_type") or entry.get("type") or "")


def _describe(entry: Mapping[str, Any]) -> str:
    text = _type_of(entry)
    if entry.get("nullable") is False:
        text += " not null"
    return text.strip()


def _schema_drift(inputs: SchemaInputs) -> CheckOutcome:
    check = SchemaCheck.SCHEMA_DRIFT
    config = inputs.options.drift
    sides = ("source", "target") if config.side == "both" else (config.side,)
    current: dict[str, Any] = {}
    for side, schema, name in (
        ("source", inputs.source, inputs.source_name),
        ("target", inputs.target, inputs.target_name),
    ):
        if schema is not None:
            current[side] = snapshot_side(schema, name)
    previous_run = config.baseline == "previous_run"
    if previous_run:
        if inputs.snapshot_path is None:
            return _outcome(check, status=Status.SKIPPED, message="no state folder for snapshots")
        path = inputs.snapshot_path
        origin = "the previous run"
    else:
        path = Path(config.baseline).expanduser()
        if not path.is_absolute() and inputs.base_dir is not None:
            path = inputs.base_dir / path
        origin = f"'{config.baseline}'"
    baseline: Mapping[str, Any] | None = None
    if path.exists():
        try:
            baseline = _baseline_for_table(_load_document(path), inputs)
        except Exception as exc:
            return _outcome(
                check,
                status=Status.ERROR,
                message=f"could not read the schema baseline {path}: {exc}",
                hints=["The baseline must be a dvf schema snapshot (JSON or YAML)."],
            )
    elif not previous_run:
        return _outcome(
            check,
            status=Status.ERROR,
            message=f"the schema baseline file {path} does not exist",
            hints=[
                "Commit a snapshot written by an earlier run (under <state_dir>/schemas), or use "
                "baseline: previous_run."
            ],
        )
    if previous_run:
        document = {
            "format": SNAPSHOT_FORMAT,
            "version": SNAPSHOT_VERSION,
            "suite": inputs.suite_name,
            "table": inputs.table_name,
            "run_id": inputs.run_id,
            "captured_at": datetime.now(UTC).isoformat(),
            **current,
        }
        try:
            _write_snapshot(path, document)
        except OSError as exc:
            log.warning("could not save the schema snapshot %s: %s", path, exc)
    if baseline is None:
        if previous_run:
            return _outcome(
                check,
                status=Status.SKIPPED,
                message="no baseline yet: this run's schema was saved as the baseline for the "
                "next run",
                details={"snapshot": str(path)},
            )
        return _outcome(
            check,
            status=Status.ERROR,
            message=f"the baseline {path} has no snapshot for table '{inputs.table_name}'",
        )
    rows: list[list[object]] = []
    compared: list[str] = []
    for side in sides:
        before, after = baseline.get(side), current.get(side)
        if isinstance(before, Mapping) and after is not None:
            compared.append(side)
            rows += _drift_rows(side, before, after, inputs)
    details = {"baseline": str(path), "baseline_run_id": baseline.get("run_id"), "sides": compared}
    if not compared:
        return _outcome(
            check,
            status=Status.SKIPPED,
            message=f"the baseline from {origin} has no schema for the {' or '.join(sides)}",
            details=details,
        )
    columns = ["side", "change", "column", "before", "after"]
    if rows:
        kinds = sorted({str(r[1]).replace("_", " ") for r in rows})
        return _outcome(
            check,
            passed=False,
            message=f"{len(rows)} schema change(s) since {origin}: {', '.join(kinds)}",
            unexpected_count=len(rows),
            samples=_table(columns, rows),
            columns=sorted({str(r[2]) for r in rows if r[2]}),
            details=details,
            hints=[
                "Review the changes; once accepted, the next run compares with this schema"
                if previous_run
                else "Review the changes and update the committed baseline when they are accepted."
            ],
        )
    return _outcome(
        check,
        passed=True,
        message=f"no schema changes since {origin} ({' and '.join(compared)})",
        details=details,
    )


# ====================================================================== SCH-14 naming standards


def _unqualified(relation: Relation | None) -> str | None:
    """The table name without schema or catalog, unquoted."""
    if relation is None or relation.table is None:
        return None
    parts = re.findall(r'"((?:[^"]|"")*)"|\[([^\]]*)\]|`([^`]*)`|([^."\[\]`]+)', relation.table)
    names = [next(p for p in groups if p) for groups in parts if any(groups)]
    return names[-1].strip() if names else None


def _naming_standards(inputs: SchemaInputs) -> CheckOutcome:
    check = SchemaCheck.NAMING_STANDARDS
    naming = inputs.options.naming
    if not naming.rules:
        return _outcome(
            check,
            status=Status.SKIPPED,
            message="no naming rules are configured (schema_options.naming.rules)",
        )
    sides = ("source", "target") if naming.side == "both" else (naming.side,)
    rows: list[list[object]] = []
    checked = 0
    for side in sides:
        schema = inputs.source if side == "source" else inputs.target
        relation = inputs.source_relation if side == "source" else inputs.target_relation
        names: list[tuple[str, str]] = []
        table = _unqualified(relation)
        if table:
            names.append(("table", table))
        if schema is not None:
            names += [("column", c.name) for c in schema.columns]
        for kind, name in names:
            checked += 1
            for rule in naming.rules:
                if rule.applies_to != "all" and rule.applies_to != f"{kind}s":
                    continue
                if any(fnmatchcase(name.lower(), p.lower()) for p in rule.exclude):
                    continue
                problems = []
                regex = rule.regex
                if regex is not None and re.fullmatch(regex, name) is None:
                    problems.append(f"does not match {regex}")
                if rule.max_length is not None and len(name) > rule.max_length:
                    problems.append(f"{len(name)} characters, more than {rule.max_length}")
                if problems:
                    rows.append([side, kind, name, rule.label, "; ".join(problems)])
    columns = ["side", "object", "name", "rule", "problem"]
    if rows:
        return _outcome(
            check,
            passed=False,
            message=f"{len(rows)} naming violation(s): "
            + ", ".join(sorted({str(r[2]) for r in rows})[:10]),
            unexpected_count=len(rows),
            total_count=checked,
            samples=_table(columns, rows),
            columns=sorted({str(r[2]) for r in rows if r[1] == "column"}),
        )
    return _outcome(
        check,
        passed=True,
        message=f"{checked} name(s) follow {len(naming.rules)} naming rule(s)",
        total_count=checked,
    )


__all__ = [
    "IMPLEMENTED",
    "SchemaInputs",
    "run_schema_checks",
    "schema_diff",
    "snapshot_path",
    "snapshot_side",
]
