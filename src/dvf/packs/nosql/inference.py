"""Document schema inference by sampling (BUILD_PROMPT.md SCH-13).

:func:`infer_document_schema` samples a dataset's documents and reports, per path:

* how often the field is present (``presence_pct``) and how often it is NULL;
* the distribution of the source system's type names (``int``, ``string``, ``objectId``, ...);
* array lengths, for arrays.

Paths use dots for nested documents and ``[]`` for array elements: ``customer.name``,
``lines``, ``lines[]`` (the elements themselves), ``lines[].sku``. Presence is relative to the
sampled documents for paths outside arrays, and to the array's object elements for paths inside
one (``lines[].sku`` present in 100% means every line has a ``sku``).

It works with every connector: NoSQL connectors are sampled natively (their dataset filter
applies, flattening does not), and other connectors through a ``SELECT ... LIMIT`` on the
dataset, so nested Parquet, JSON, and DuckDB structs are profiled the same way.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from dvf.config.models import DatasetSpec
from dvf.plugins.base import Connector, Relation

from .values import Classifier, ValueKind, classify_python

_ROUND = 4


@dataclass
class FieldReport:
    """What sampling found about one path."""

    path: str
    present: int = 0
    """Times the field was present (NULL included)."""
    containers: int = 0
    """Documents (or array elements) in which it could have been present."""
    nulls: int = 0
    types: Counter[str] = field(default_factory=Counter)
    """Non-NULL values per native type name."""
    kinds: Counter[str] = field(default_factory=Counter)
    """Non-NULL values per value kind (``int``, ``float``, ``object``, ...)."""
    pairs: Counter[tuple[str, str]] = field(default_factory=Counter)
    """Non-NULL values per (native type, kind)."""
    array_min: int | None = None
    array_max: int | None = None
    array_total: int = 0
    arrays: int = 0

    @property
    def depth(self) -> int:
        return self.path.count(".") + self.path.count("[]")

    @property
    def presence_pct(self) -> float:
        if self.containers == 0:
            return 0.0
        return round(100.0 * self.present / self.containers, _ROUND)

    @property
    def null_pct(self) -> float:
        if self.present == 0:
            return 0.0
        return round(100.0 * self.nulls / self.present, _ROUND)

    @property
    def heterogeneous(self) -> bool:
        """More than one non-NULL type was seen."""
        return len(self.types) > 1

    @property
    def in_array(self) -> bool:
        return "[]" in self.path

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "depth": self.depth,
            "present": self.present,
            "presence_pct": self.presence_pct,
            "nulls": self.nulls,
            "null_pct": self.null_pct,
            "types": dict(self.types.most_common()),
            "heterogeneous": self.heterogeneous,
        }
        if self.arrays:
            data["array_length"] = {
                "min": self.array_min,
                "max": self.array_max,
                "avg": round(self.array_total / self.arrays, _ROUND),
            }
        return data

    def _observe_array(self, length: int) -> None:
        self.arrays += 1
        self.array_total += length
        self.array_min = length if self.array_min is None else min(self.array_min, length)
        self.array_max = length if self.array_max is None else max(self.array_max, length)


@dataclass
class DocumentSchemaReport:
    """The inferred schema of a sampled dataset."""

    dataset: str
    documents: int
    sample_size: int
    sampling: str
    fields: list[FieldReport]
    notes: list[str] = field(default_factory=list)

    def field(self, path: str) -> FieldReport | None:
        return next((f for f in self.fields if f.path == path), None)

    @property
    def heterogeneous_fields(self) -> list[FieldReport]:
        return [f for f in self.fields if f.heterogeneous]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "documents": self.documents,
            "sample_size": self.sample_size,
            "sampling": self.sampling,
            "fields": [f.to_dict() for f in self.fields],
            "notes": list(self.notes),
        }


class _Walker:
    def __init__(self, classify: Classifier) -> None:
        self.classify = classify
        self.fields: dict[str, FieldReport] = {}
        self.documents = 0

    def report(self, path: str) -> FieldReport:
        found = self.fields.get(path)
        if found is None:
            found = self.fields[path] = FieldReport(path=path)
        return found

    def document(self, document: Mapping[str, Any]) -> None:
        self.documents += 1
        self.walk_object(document, "")

    def walk_object(self, obj: Mapping[str, Any], prefix: str) -> None:
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            report = self.report(path)
            report.present += 1
            self.value(report, value)

    def value(self, report: FieldReport, value: Any) -> None:
        cell = self.classify(value)
        if cell.kind is ValueKind.NULL:
            report.nulls += 1
            return
        report.types[cell.native] += 1
        report.kinds[cell.kind.value] += 1
        report.pairs[(cell.native, cell.kind.value)] += 1
        if cell.kind is ValueKind.OBJECT:
            self.walk_object(cell.value, report.path)
        elif cell.kind is ValueKind.ARRAY:
            elements = list(cell.value)
            report._observe_array(len(elements))
            element_report = self.report(report.path + "[]")
            for element in elements:
                element_report.present += 1
                self.value(element_report, element)

    def finish(self) -> list[FieldReport]:
        reports = list(self.fields.values())
        objects_at = {
            f.path: f.kinds.get(ValueKind.OBJECT.value, 0) for f in reports if f.path.endswith("[]")
        }
        for report in reports:
            anchor = _array_anchor(report.path)
            if anchor is None:
                report.containers = self.documents
            elif report.path == anchor:
                parent = self.fields[anchor[:-2]]
                report.containers = parent.array_total
            else:
                report.containers = objects_at.get(anchor, 0)
        reports.sort(key=lambda f: f.path)
        return reports


def _array_anchor(path: str) -> str | None:
    """The nearest enclosing ``[]`` element path of ``path`` (``path`` itself for elements)."""
    if path.endswith("[]"):
        return path
    index = path.rfind("[]")
    if index < 0:
        return None
    return path[: index + 2]


def infer_from_documents(
    documents: Iterable[Mapping[str, Any]],
    classify: Classifier = classify_python,
    *,
    dataset: str = "",
    sample_size: int | None = None,
    sampling: str = "first",
) -> DocumentSchemaReport:
    """Infer the schema of ``documents`` (already sampled)."""
    walker = _Walker(classify)
    for document in documents:
        walker.document(document)
    fields = walker.finish()
    notes = []
    if walker.documents == 0:
        notes.append("the dataset returned no documents")
    return DocumentSchemaReport(
        dataset=dataset,
        documents=walker.documents,
        sample_size=sample_size if sample_size is not None else walker.documents,
        sampling=sampling,
        fields=fields,
        notes=notes,
    )


def infer_document_schema(
    connector: Connector,
    dataset: DatasetSpec | Relation,
    sample_size: int | None = None,
    *,
    sampling: str = "first",
) -> DocumentSchemaReport:
    """Sample a dataset and infer its document schema.

    ``dataset`` is a dataset specification or a relation the connector resolved.
    ``sample_size`` defaults to the dataset's ``options.sample_size`` (1,000) for NoSQL
    connectors, and to 1,000 otherwise. ``sampling`` is ``first`` (deterministic: the first
    documents in the source's natural order) or ``random`` (MongoDB ``$sample``; other systems
    fall back to ``first`` and say so in ``notes``).
    """
    from dvf.packs.nosql.base import DocumentConnector

    if isinstance(connector, DocumentConnector):
        spec = connector.spec_for(dataset) if isinstance(dataset, Relation) else dataset
        options = connector.parse_options(spec)
        size = sample_size or options.sample_size
        notes = []
        mode = sampling
        if sampling == "random" and not connector.random_sampling:
            notes.append(f"{connector.system} has no random sampling; read the first documents")
            mode = "first"
        documents = connector.sample_documents(
            spec, size, "random" if mode == "random" else "first"
        )
        report = infer_from_documents(
            documents,
            connector.classify,
            dataset=f"{connector.name}:{spec.table}",
            sample_size=size,
            sampling=mode,
        )
        report.notes.extend(notes)
        return report
    relation = dataset if isinstance(dataset, Relation) else connector.resolve(dataset)
    size = sample_size or 1000
    sql = connector.sql_dialect.limit(f"SELECT * FROM {relation.sql}", size)
    rows = connector.query(sql).to_pylist()
    report = infer_from_documents(
        rows, classify_python, dataset=relation.ref.display(), sample_size=size, sampling="first"
    )
    if sampling == "random":
        report.notes.append("random sampling is available for MongoDB only; read the first rows")
    return report


__all__ = [
    "DocumentSchemaReport",
    "FieldReport",
    "infer_document_schema",
    "infer_from_documents",
]
