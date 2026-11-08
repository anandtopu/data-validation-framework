"""The ``document_schema`` check (SCH-13): an inferred document schema against expectations.

It samples the checked dataset (see :mod:`dvf.packs.nosql.inference`) and reports:

* expected fields that are missing or present less often than ``min_presence``;
* values whose type is not one of the expected types;
* NULL values in fields that must not be NULL;
* fields with more types than allowed (``max_types``), listed or not;
* unexpected fields, when ``allow_extra_fields`` is false.

Types are the source system's names (``objectId``, ``int``, ``long``, ``double``, ``decimal``,
``string``, ``date``, ``bool``, ``binData``, ``uuid``, ``object``, ``array``; DynamoDB's ``S``,
``N``, ...) or these aliases: ``integer``, ``number``, ``float``, ``decimal``, ``string``,
``boolean``, ``timestamp``, ``date``, ``time``, ``binary``, ``uuid``, ``objectid``, ``object``,
``array``.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PositiveInt

from dvf.checks.context import CheckContext, CheckOutcome
from dvf.config.base import StrictModel
from dvf.config.checks import CheckConfig
from dvf.core.enums import Dimension
from dvf.core.errors import PlanError
from dvf.models.results import SampleSet
from dvf.packs.nosql.inference import DocumentSchemaReport, FieldReport, infer_document_schema
from dvf.plugins.base import Check

_NUMBER = {"int", "float", "decimal"}
ALIASES: dict[str, set[str]] = {
    "integer": {"int"},
    "number": _NUMBER,
    "numeric": _NUMBER,
    "float": {"float"},
    "double": {"float"},
    "decimal": {"decimal"},
    "string": {"string"},
    "text": {"string"},
    "boolean": {"bool"},
    "bool": {"bool"},
    "timestamp": {"timestamp"},
    "datetime": {"timestamp"},
    "date": {"timestamp", "date"},
    "time": {"time"},
    "binary": {"binary"},
    "uuid": {"uuid"},
    "objectid": {"object_id"},
    "object": {"object"},
    "document": {"object"},
    "map": {"object"},
    "array": {"array"},
    "list": {"array"},
}
"""Type aliases and the value kinds they accept."""


def _expectation_input(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, str | list):
        return {"type": value}
    return value


class FieldExpectation(StrictModel):
    """What a field must look like. A string or list is shorthand for ``type``."""

    type: str | list[str] | None = None
    """Allowed types: native names or aliases (see the check's description)."""
    min_presence: float = Field(default=100.0, ge=0, le=100)
    """Lowest accepted presence, in percent of sampled documents (or array elements)."""
    nullable: bool = True
    """Whether NULL values are accepted."""
    max_types: PositiveInt | None = None
    """Most distinct non-NULL types accepted (overrides the check's ``max_types``)."""

    @property
    def types(self) -> list[str]:
        if self.type is None:
            return []
        return [self.type] if isinstance(self.type, str) else list(self.type)


ExpectationInput = Annotated[
    FieldExpectation,
    BeforeValidator(
        _expectation_input,
        json_schema_input_type=str | list[str] | FieldExpectation | None,
    ),
]


class DocumentSchemaCheck(CheckConfig):
    """Inferred document schema: expected fields, types, presence, and heterogeneity (SCH-13)."""

    fields: dict[str, ExpectationInput] = Field(default_factory=dict)
    """Expected fields by path (``customer.name``, ``lines[].qty``), each with its allowed
    types, minimum presence, and nullability."""
    sample_size: PositiveInt | None = Field(default=None, le=1_000_000)
    """Documents sampled; defaults to the dataset's ``options.sample_size`` (1,000)."""
    sampling: Literal["first", "random"] = "first"
    """``first`` reads the first documents (repeatable); ``random`` samples at random
    (MongoDB only)."""
    max_types: PositiveInt | None = None
    """Most distinct non-NULL types accepted per field (unlisted fields too); unlimited when
    omitted."""
    allow_extra_fields: bool = True
    """Accept fields that are not listed (and are not parents of listed fields)."""
    ignore: list[str] = Field(default_factory=list)
    """Glob patterns of paths ignored by ``max_types`` and ``allow_extra_fields``."""


def allowed(native: str, kind: str, tokens: list[str]) -> bool:
    """Whether a value of ``native`` type and ``kind`` matches one of the type ``tokens``."""
    for token in tokens:
        lowered = token.lower()
        if lowered == native.lower() or kind in ALIASES.get(lowered, set()):
            return True
    return False


def _ancestors(path: str) -> set[str]:
    """``lines[].sku`` has the ancestors ``lines[]`` and ``lines``."""
    found: set[str] = set()
    current = path
    while current:
        if current.endswith("[]"):
            current = current[:-2]
        else:
            cut = current.rfind(".")
            if cut < 0:
                break
            current = current[:cut]
        if current:
            found.add(current)
    return found


def evaluate_report(report: DocumentSchemaReport, config: DocumentSchemaCheck) -> list[list[str]]:
    """Problems found in ``report``: rows of ``[field, problem, observed, expected]``."""
    problems: list[list[str]] = []
    listed = set(config.fields)
    covered = set(listed)
    for path in listed:
        covered |= _ancestors(path)

    def ignored(path: str) -> bool:
        return any(fnmatchcase(path, pattern) for pattern in config.ignore)

    for path, expectation in config.fields.items():
        found = report.field(path)
        if found is None or found.present == 0:
            if expectation.min_presence > 0:
                problems.append(
                    [path, "missing", "absent from every sampled document", _presence(expectation)]
                )
            continue
        if found.presence_pct < expectation.min_presence:
            problems.append(
                [
                    path,
                    "low presence",
                    f"{found.presence_pct:.4g}% ({found.present:,} of {found.containers:,})",
                    _presence(expectation),
                ]
            )
        tokens = expectation.types
        if tokens:
            wrong = {
                native: count
                for (native, kind), count in found.pairs.items()
                if not allowed(native, kind, tokens)
            }
            if wrong:
                total = sum(found.types.values())
                observed = ", ".join(f"{n} in {c:,} of {total:,} values" for n, c in wrong.items())
                problems.append([path, "unexpected type", observed, " or ".join(tokens)])
        if not expectation.nullable and found.nulls:
            problems.append(
                [path, "null values", f"{found.nulls:,} NULL value(s)", "no NULL values"]
            )
        limit = expectation.max_types or config.max_types
        if limit is not None and len(found.types) > limit:
            problems.append(_heterogeneous(found, limit))
    for found in report.fields:
        if found.path in listed or ignored(found.path):
            continue
        if config.max_types is not None and len(found.types) > config.max_types:
            problems.append(_heterogeneous(found, config.max_types))
        if not config.allow_extra_fields and found.path not in covered:
            problems.append(
                [
                    found.path,
                    "unexpected field",
                    f"present in {found.presence_pct:.4g}%",
                    "listed in 'fields' or matched by 'ignore'",
                ]
            )
    return problems


def _presence(expectation: FieldExpectation) -> str:
    return f"present in at least {expectation.min_presence:g}%"


def _heterogeneous(found: FieldReport, limit: int) -> list[str]:
    types = ", ".join(f"{n} ({c:,})" for n, c in found.types.most_common())
    return [found.path, "mixed types", types, f"at most {limit} type(s)"]


class DocumentSchema(Check):
    """Inferred document schema: expected fields, types, presence, and heterogeneity (SCH-13)."""

    dimension = Dimension.SCHEMA
    Config = DocumentSchemaCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, DocumentSchemaCheck)
        if config.where:
            raise PlanError(
                "The document_schema check samples documents with the dataset's native read, so "
                "'where' does not apply.",
                hint="Remove 'where', and filter with the dataset's 'options' instead.",
            )
        report = infer_document_schema(
            ctx.connector, ctx.relation, config.sample_size, sampling=config.sampling
        )
        problems = evaluate_report(report, config)
        columns = ["field", "problem", "observed", "expected"]
        samples = (
            SampleSet(columns=columns, rows=[list(p) for p in problems[:200]], masked=False)
            if problems
            else None
        )
        summary = ", ".join(f"{p[0]} ({p[1]})" for p in problems[:6]) + (
            " …" if len(problems) > 6 else ""
        )
        message = (
            f"{len(problems):,} schema problem(s) in {report.documents:,} sampled documents: "
            + summary
            if problems
            else f"{len(config.fields)} expected field(s) match in {report.documents:,} sampled "
            f"documents ({len(report.fields)} paths observed)"
        )
        details: dict[str, Any] = {
            "documents_sampled": report.documents,
            "sampling": report.sampling,
            "paths_observed": len(report.fields),
            "heterogeneous_paths": [f.path for f in report.heterogeneous_fields],
            "schema": [f.to_dict() for f in report.fields[:500]],
        }
        if report.notes:
            details["notes"] = report.notes
        return CheckOutcome(
            passed=not problems,
            message=message,
            observed=len(problems),
            expected=0,
            unexpected_count=len(problems),
            total_count=len(config.fields),
            samples=samples,
            hints=list(report.notes),
            details=details,
        )


__all__ = [
    "ALIASES",
    "DocumentSchema",
    "DocumentSchemaCheck",
    "FieldExpectation",
    "allowed",
    "evaluate_report",
]
