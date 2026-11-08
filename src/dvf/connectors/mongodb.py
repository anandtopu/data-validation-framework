"""MongoDB connector: collections read with ``pymongo``, flattened, and staged for SQL.

A dataset names a collection with ``table``. Its ``options`` hold the native read (a query
document or an aggregation pipeline, a projection, a sort, a limit, read preference and read
concern) and the flattening settings shared by every NoSQL connector (``fields``, ``unwind``,
``arrays``, ...; see :mod:`dvf.packs.nosql.options`). Filters and pipelines may use MongoDB
Extended JSON (``{"$oid": ...}``, ``{"$date": ...}``, ``{"$numberDecimal": ...}``).

The connection is read-only: dvf only runs ``find``, ``aggregate``, ``listCollections``, and
``ping``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping
from fnmatch import fnmatchcase
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveInt, model_validator

from dvf.config.connections import MongoDBConnection
from dvf.config.models import DatasetSpec
from dvf.core.errors import ConnectorError, DependencyError
from dvf.packs.nosql.base import DocumentConnector, Sampling, chunked
from dvf.packs.nosql.bson_types import extended_json, make_bson_classifier
from dvf.packs.nosql.flatten import split_path
from dvf.packs.nosql.options import DocumentOptions
from dvf.packs.nosql.values import Classified, Classifier

ReadPreferenceName = Literal[
    "primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest"
]
ReadConcernName = Literal["local", "available", "majority", "linearizable", "snapshot"]


class MongoDBDatasetOptions(DocumentOptions):
    """Dataset options of the ``mongodb`` connector."""

    filter: dict[str, Any] | None = None
    """Query document, as given to ``find`` (Extended JSON allowed), for example
    ``{status: {$ne: cancelled}}``."""
    pipeline: list[dict[str, Any]] | None = None
    """Aggregation pipeline run instead of ``find`` (Extended JSON allowed)."""
    projection: dict[str, Any] | list[str] | None = None
    """Fields to read (a projection document or a list of fields). When omitted and ``fields``
    is set, dvf reads only the top-level fields that ``fields`` and ``unwind`` use."""
    sort: dict[str, Literal[1, -1]] | None = None
    """Sort order of ``find``, for example ``{_id: 1}`` (useful with ``limit``)."""
    hint: str | dict[str, Any] | None = None
    """Index to use for ``find``: its name or its key pattern."""
    read_preference: ReadPreferenceName | None = None
    """Overrides the connection's read preference for this dataset."""
    read_concern: ReadConcernName | None = None
    """Overrides the connection's read concern for this dataset."""
    max_time_ms: PositiveInt | None = None
    """Server-side time limit of the read; defaults to the connection's ``query_timeout_s``."""
    allow_disk_use: bool = False
    """Let aggregation stages spill to disk on the server."""
    collation: dict[str, Any] | None = None
    """Collation of the query, for example ``{locale: en, strength: 2}``."""
    batch_size: PositiveInt | None = Field(default=None, le=100_000)
    """Documents per round trip; defaults to the connection's ``batch_size``."""

    @model_validator(mode="after")
    def _filter_or_pipeline(self) -> MongoDBDatasetOptions:
        if self.pipeline is not None and (
            self.filter is not None
            or self.projection is not None
            or self.sort is not None
            or self.hint is not None
        ):
            raise ValueError(
                "'pipeline' replaces 'filter', 'projection', 'sort', and 'hint'; "
                "use $match, $project, and $sort stages instead"
            )
        return self


def _import_pymongo() -> Any:
    try:
        return importlib.import_module("pymongo")
    except ImportError as exc:
        raise DependencyError("pymongo", "mongodb", "The MongoDB connector") from exc


class MongoDBConnector(DocumentConnector):
    """MongoDB collections, flattened and staged for SQL comparison."""

    Config: ClassVar[type[MongoDBConnection]] = MongoDBConnection
    options_model = MongoDBDatasetOptions
    system = "MongoDB"
    default_keep = ("_id",)
    decimal_precision = 34
    timestamp_precision = 3
    random_sampling = True

    def __init__(self, name: str, config: Any, base_dir: Any) -> None:
        super().__init__(name, config, base_dir)
        self._client: Any = None
        self._db: Any = None
        self._classifier: Classifier | None = None

    @property
    def mongo_config(self) -> MongoDBConnection:
        assert isinstance(self.config, MongoDBConnection)
        return self.config

    # ------------------------------------------------------------------ lifecycle
    def open_source(self) -> None:
        pymongo = _import_pymongo()
        cfg = self.mongo_config
        kwargs: dict[str, Any] = {
            "tz_aware": True,
            "uuidRepresentation": cfg.uuid_representation,
            "datetime_conversion": "DATETIME_AUTO",
            "serverSelectionTimeoutMS": cfg.server_selection_timeout_ms,
            "appname": "dvf",
        }
        if cfg.read_preference is not None:
            kwargs["readPreference"] = cfg.read_preference
        if cfg.read_concern is not None:
            kwargs["readConcernLevel"] = cfg.read_concern
        kwargs.update(cfg.options)
        client = pymongo.MongoClient(cfg.uri.get_secret_value(), **kwargs)
        try:
            client.admin.command("ping")
        except Exception as exc:
            client.close()
            raise ConnectorError(
                f"Cannot reach MongoDB for connection '{self.name}': {exc}",
                hint="Check the connection string, network access, and credentials.",
            ) from exc
        self._client = client
        self._db = client[cfg.database]
        self._classifier = make_bson_classifier()

    def close_source(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._db = None

    def server_version(self) -> str | None:
        self.open()
        info = self._client.server_info()
        return str(info.get("version")) if info.get("version") else None

    def classify(self, value: Any) -> Classified:
        if self._classifier is None:
            self._classifier = make_bson_classifier()
        return self._classifier(value)

    # ------------------------------------------------------------------ datasets
    def dataset_exists(self, spec: DatasetSpec) -> bool | None:
        name = self.collection(spec)
        self.open()
        return bool(self._db.list_collection_names(filter={"name": name}))

    def list_datasets(self, pattern: str) -> list[str]:
        self.open()
        names = self._db.list_collection_names()
        return sorted(n for n in names if fnmatchcase(n, pattern) and not n.startswith("system."))

    def default_primary_key(self, spec: DatasetSpec, options: DocumentOptions) -> tuple[str, ...]:
        return ("_id",)

    # ------------------------------------------------------------------ reads
    def _collection(self, spec: DatasetSpec, options: MongoDBDatasetOptions) -> Any:
        self.open()
        collection = self._db[self.collection(spec)]
        changes: dict[str, Any] = {}
        if options.read_preference is not None:
            read_preferences = importlib.import_module("pymongo.read_preferences")
            mode = read_preferences.read_pref_mode_from_name(options.read_preference)
            changes["read_preference"] = read_preferences.make_read_preference(mode, None)
        if options.read_concern is not None:
            read_concern = importlib.import_module("pymongo.read_concern")
            changes["read_concern"] = read_concern.ReadConcern(options.read_concern)
        return collection.with_options(**changes) if changes else collection

    def native_query(
        self,
        spec: DatasetSpec,
        options: MongoDBDatasetOptions,
        *,
        limit: int | None = None,
        sampling: Sampling | None = None,
    ) -> tuple[str, Any]:
        """``("find", (filter, projection))`` or ``("aggregate", pipeline)`` for a dataset."""
        projection = self._projection(options)
        if options.pipeline is not None:
            pipeline = list(extended_json(options.pipeline))
            if sampling == "random" and limit:
                pipeline.append({"$sample": {"size": limit}})
            elif limit:
                pipeline.append({"$limit": limit})
            return "aggregate", pipeline
        query = extended_json(options.filter) if options.filter else {}
        if sampling == "random" and limit:
            pipeline = [{"$match": query}, {"$sample": {"size": limit}}]
            if projection is not None:
                pipeline.append({"$project": projection})
            return "aggregate", pipeline
        return "find", (query, projection)

    def _projection(self, options: MongoDBDatasetOptions) -> dict[str, Any] | None:
        if options.projection is not None:
            if isinstance(options.projection, list):
                return dict.fromkeys(options.projection, 1)
            return dict(options.projection)
        if options.pipeline is not None or options.fields is None:
            return None
        paths = list(options.fields.values())
        if options.unwind is not None:
            paths.append(options.unwind.path)
        tops = {split_path(p)[0] for p in paths if split_path(p)}
        projection: dict[str, Any] = dict.fromkeys(sorted(tops), 1)
        if "_id" not in tops:
            projection["_id"] = 0
        return projection

    def iter_documents(
        self,
        spec: DatasetSpec,
        options: DocumentOptions,
        *,
        limit: int | None = None,
        sampling: Sampling | None = None,
    ) -> Iterator[list[Mapping[str, Any]]]:
        assert isinstance(options, MongoDBDatasetOptions)
        collection = self._collection(spec, options)
        batch_size = options.batch_size or self.mongo_config.batch_size
        max_time_ms = options.max_time_ms or (
            self.mongo_config.query_timeout_s * 1000 if self.mongo_config.query_timeout_s else None
        )
        kind, query = self.native_query(spec, options, limit=limit, sampling=sampling)
        if kind == "aggregate":
            kwargs: dict[str, Any] = {
                "batchSize": batch_size,
                "allowDiskUse": options.allow_disk_use,
            }
            if max_time_ms:
                kwargs["maxTimeMS"] = max_time_ms
            if options.collation is not None:
                kwargs["collation"] = options.collation
            cursor = collection.aggregate(query, **kwargs)
        else:
            criteria, projection = query
            find_kwargs: dict[str, Any] = {"batch_size": batch_size}
            if projection is not None:
                find_kwargs["projection"] = projection
            if limit:
                find_kwargs["limit"] = limit
            if options.sort:
                find_kwargs["sort"] = list(options.sort.items())
            if options.hint is not None:
                find_kwargs["hint"] = (
                    list(options.hint.items()) if isinstance(options.hint, dict) else options.hint
                )
            if max_time_ms:
                find_kwargs["max_time_ms"] = max_time_ms
            if options.collation is not None:
                find_kwargs["collation"] = options.collation
            cursor = collection.find(criteria, **find_kwargs)
        try:
            yield from chunked(iter(cursor), batch_size)
        finally:
            cursor.close()


__all__ = ["MongoDBConnector", "MongoDBDatasetOptions"]
