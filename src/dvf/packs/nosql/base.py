"""The base class of the NoSQL connectors: read documents, shape them into rows, and stage them.

:class:`DocumentConnector` extends :class:`~dvf.connectors.staged.StagedConnector`. A subclass
reads documents in batches with its driver (:meth:`DocumentConnector.iter_documents`) and
classifies its driver's value types (:meth:`DocumentConnector.classify`); this class validates
the dataset ``options``, flattens and unwinds the documents, unifies their types, and stages the
rows as Parquet for DuckDB. Checks, comparisons, and engines then run unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar, Literal

from dvf.config.models import DatasetSpec
from dvf.connectors.staged import StagedConnector, StagedData, mentions_staged_view
from dvf.core.errors import ConnectorError
from dvf.packs.nosql.flatten import DocumentShaper
from dvf.packs.nosql.options import DocumentOptions, parse_options
from dvf.packs.nosql.staging import RowStager, TypeHints
from dvf.packs.nosql.values import Classified, classify_python
from dvf.plugins.base import Relation

log = logging.getLogger("dvf.nosql")

Sampling = Literal["first", "random"]


class DocumentConnector(StagedConnector):
    """Base class for document and wide-column connectors (MongoDB, Cassandra, DynamoDB, ...).

    Subclasses set :attr:`options_model` and implement :meth:`open_source`,
    :meth:`close_source`, and :meth:`iter_documents`; they may override :meth:`classify`,
    :meth:`default_primary_key`, :meth:`type_hints`, :meth:`dataset_exists`, and
    :meth:`list_datasets`.
    """

    options_model: ClassVar[type[DocumentOptions]] = DocumentOptions
    """Validates dataset ``options``."""
    system: ClassVar[str] = "document store"
    """The system's name in messages."""
    default_keep: ClassVar[tuple[str, ...]] = ()
    """Parent fields kept on unwound rows when ``unwind.keep`` is not set."""
    decimal_precision: ClassVar[int] = 38
    timestamp_precision: ClassVar[int | None] = None
    default_batch_size: ClassVar[int] = 1000
    random_sampling: ClassVar[bool] = False
    """Whether :meth:`iter_documents` supports ``sampling="random"``."""

    # ------------------------------------------------------------------ subclass API
    def classify(self, value: Any) -> Classified:
        """Classify one driver value (override for driver-specific types)."""
        return classify_python(value)

    def iter_documents(
        self,
        spec: DatasetSpec,
        options: DocumentOptions,
        *,
        limit: int | None = None,
        sampling: Sampling | None = None,
    ) -> Iterator[list[Mapping[str, Any]]]:  # pragma: no cover - interface
        """Read the dataset's documents in batches, applying its native filter.

        ``limit`` caps the number of documents; ``sampling="random"`` asks for a random sample
        of ``limit`` documents where the system supports it.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement iter_documents()")

    def default_primary_key(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        """The columns identifying a document, when rows are not unwound (for ``keys: auto``)."""
        return ()

    def keep_fields(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        """Parent fields kept on unwound rows when ``unwind.keep`` is not set."""
        return self.default_keep

    def type_hints(self, spec: DatasetSpec, options: DocumentOptions) -> TypeHints:
        """Connector knowledge used when choosing column types."""
        return TypeHints(
            decimal_precision=self.decimal_precision,
            timestamp_precision=self.timestamp_precision,
        )

    # ------------------------------------------------------------------ options
    def collection(self, spec: DatasetSpec) -> str:
        """The collection, table, or container a dataset names (``table:``)."""
        if spec.as_of is not None:
            raise ConnectorError(
                f"Connection '{self.name}' ({self.system}) cannot read datasets as of a point in "
                "time.",
                hint="Remove 'as_of', or compare a snapshot exported at that time.",
            )
        if spec.table is None:
            given = "query" if spec.query is not None else "path"
            raise ConnectorError(
                f"Datasets on connection '{self.name}' ({self.system}) are named with 'table', "
                f"not '{given}'.",
                hint="Set 'table' to the collection, table, or container name, and put native "
                "filters in 'options' (see docs/nosql.md).",
            )
        return spec.table

    def parse_options(self, spec: DatasetSpec) -> DocumentOptions:
        """The dataset's validated ``options``."""
        return parse_options(
            self.options_model,
            dict(spec.options),
            connection=self.name,
            dataset=spec.table or spec.query or spec.path or "dataset",
        )

    def resolve(self, spec: DatasetSpec) -> Relation:
        if not (spec.query is not None and mentions_staged_view(spec.query)):
            self.collection(spec)
            self.parse_options(spec)
        return super().resolve(spec)

    def batch_size(self, options: DocumentOptions) -> int:
        return options.batch_size or self.default_batch_size

    # ------------------------------------------------------------------ staging
    def fetch(self, spec: DatasetSpec) -> StagedData:
        self.collection(spec)
        options = self.parse_options(spec)
        shaper = DocumentShaper(
            options, self.classify, default_keep=self.keep_fields(spec, options)
        )
        stager = RowStager(
            forced=options.types,
            hints=self.type_hints(spec, options),
            temp_dir=self.temp_root,
        )
        try:
            return self._stage(spec, options, shaper, stager)
        except BaseException:
            stager.close()  # the batches will never be read
            raise

    def _stage(
        self,
        spec: DatasetSpec,
        options: DocumentOptions,
        shaper: DocumentShaper,
        stager: RowStager,
    ) -> StagedData:
        documents = 0
        for batch in self.iter_documents(spec, options, limit=options.limit):
            for document in batch:
                documents += 1
                stager.add_all(shaper.rows(document))
        fixed = list(options.fields) if options.fields else None
        if fixed is not None and options.unwind is not None:
            fixed.append(options.unwind.index_column)
        if fixed is None and stager.rows == 0:
            fixed = self._empty_columns(spec, options)
        staged = stager.finish(columns=fixed)
        for note in shaper.finish_notes() + staged.notes:
            log.info("%s.%s: %s", self.name, spec.table, note)
        columns = set(staged.schema.names)
        key = self._primary_key(spec, options, columns)
        comments = staged.comments
        for path, count in shaper.truncated.items():
            prefix = path.replace(".", options.separator) + options.separator
            for name in columns:
                if name.startswith(prefix):
                    comments.setdefault(
                        name, f"{count:,} '{path}' arrays were longer than index_limit"
                    )
        log.debug(
            "%s.%s: %s documents shaped into %s rows", self.name, spec.table, documents, staged.rows
        )
        return StagedData(
            schema=staged.schema,
            batches=staged.batches,
            native_types=staged.native_types,
            canonical_types=staged.canonical_types,
            primary_key=key,
            comments=comments,
        )

    def _empty_columns(self, spec: DatasetSpec, options: DocumentOptions) -> list[str]:
        """Columns of a dataset without documents (Parquet needs at least one column)."""
        if options.unwind is not None:
            keep = options.unwind.keep
            parents = list(keep) if keep is not None else list(self.keep_fields(spec, options))
            names = [p.replace(".", options.separator) for p in parents]
            return [*names, options.unwind.index_column]
        return list(self.default_primary_key(spec, options)) or ["_id"]

    def _primary_key(
        self, spec: DatasetSpec, options: DocumentOptions, columns: set[str]
    ) -> tuple[str, ...]:
        if options.primary_key is not None:
            missing = [c for c in options.primary_key if c not in columns]
            if missing:
                raise ConnectorError(
                    f"options.primary_key names column(s) {', '.join(missing)}, which dataset "
                    f"'{spec.table}' does not have.",
                    hint=f"Available columns: {', '.join(sorted(columns)[:30])}",
                )
            return tuple(options.primary_key)
        if options.unwind is not None:
            if options.fields is not None:
                return ()
            keep = options.unwind.keep
            parents = list(keep) if keep is not None else list(self.keep_fields(spec, options))
            names = [p.replace(".", options.separator) for p in parents]
            key = (*names, options.unwind.index_column)
            return key if parents and all(n in columns for n in key) else ()
        key = self.default_primary_key(spec, options)
        if options.fields is not None:
            by_path = {path: name for name, path in options.fields.items()}
            key = tuple(by_path.get(k, "") for k in key)
        return key if key and all(k in columns for k in key) else ()

    # ------------------------------------------------------------------ sampling
    def sample_documents(
        self, spec: DatasetSpec, size: int, sampling: Sampling = "first"
    ) -> list[Mapping[str, Any]]:
        """Up to ``size`` documents of the dataset (native filter applied, not shaped).

        The shaping options (``fields``, ``unwind``) are ignored, so connectors that read only
        the fields a shape needs (MongoDB) return whole documents here.
        """
        self.collection(spec)
        options = self.parse_options(spec).model_copy(update={"fields": None, "unwind": None})
        self.open()
        found: list[Mapping[str, Any]] = []
        for batch in self.iter_documents(spec, options, limit=size, sampling=sampling):
            found.extend(batch)
            if len(found) >= size:
                break
        return found[:size]


def chunked(items: Iterator[Any], size: int) -> Iterator[list[Any]]:
    """Group an iterator into lists of ``size`` items."""
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


__all__ = ["DocumentConnector", "Sampling", "chunked"]
