"""Dataset ``options`` for NoSQL connectors: native filters, flattening, arrays, and sampling.

Every NoSQL connector validates a dataset's ``options`` with a model derived from
:class:`DocumentOptions` (shared flattening settings) plus its own native read settings. Unknown
options are errors, so a misspelled option never silently reads the whole collection.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PositiveInt, ValidationError, model_validator

from dvf.config.base import StrictModel
from dvf.core.errors import ConnectorError

ArrayStrategy = Literal["json", "hash", "unordered_hash", "count", "index", "explode"]
"""How an array becomes column values:

* ``json``: canonical JSON text of the whole array (the default);
* ``hash``: SHA-256 of that canonical JSON;
* ``unordered_hash``: SHA-256 of the canonical JSON with the elements sorted, for arrays whose
  order does not matter;
* ``count``: the number of elements;
* ``index``: one column per position (``tags.0``, ``tags.1``, ...), up to ``index_limit``;
* ``explode``: one row per element (the same as ``unwind``).
"""

ColumnTypeName = Literal[
    "string", "integer", "decimal", "double", "boolean", "timestamp", "date", "json"
]
"""Types a column can be forced to with ``types``."""


class UnwindOptions(StrictModel):
    """One row per element of an array, like MongoDB's ``$unwind``."""

    path: str = Field(min_length=1)
    """Dot path of the array, for example ``lines``."""
    keep: list[str] | None = None
    """Parent fields copied to every element row (dot paths). Defaults to the document's identity
    (``_id`` for MongoDB, ``id`` for Cosmos DB, the key attributes for DynamoDB, and the primary
    key for Cassandra). Ignored when ``fields`` is set."""
    index: str | None = None
    """Name of the column holding the element's 0-based position; defaults to
    ``<path>_index`` (dots replaced by underscores)."""
    prefix: str = ""
    """Prepended to the names of the element's columns, for example ``line_``. Ignored when
    ``fields`` is set."""
    preserve_empty: bool = False
    """Emit one row with NULL element columns for documents whose array is missing, NULL, or
    empty (otherwise they produce no rows)."""

    @property
    def index_column(self) -> str:
        return self.index or self.path.replace(".", "_") + "_index"


def _unwind_input(value: Any) -> Any:
    return {"path": value} if isinstance(value, str) else value


class DocumentOptions(StrictModel):
    """Flattening and sampling options shared by every NoSQL connector."""

    fields: dict[str, str] | None = None
    """Output column name to dot path, for example ``{customer_name: customer.name}``. Paths may
    index arrays (``lines.0.sku``); a path through an array without an index collects the values
    of every element, as in MongoDB. When omitted, every field is flattened into columns named by
    their dot paths."""
    unwind: Annotated[
        UnwindOptions | None,
        BeforeValidator(_unwind_input, json_schema_input_type=str | UnwindOptions | None),
    ] = None
    """Produce one row per element of an array (a path, or an object with ``path``, ``keep``,
    ``index``, ``prefix``, and ``preserve_empty``). With ``fields``, the array's path refers to
    the element, as after MongoDB's ``$unwind``: ``lines.sku`` is the element's ``sku``."""
    arrays: dict[str, ArrayStrategy] = Field(default_factory=dict)
    """Array strategy per dot path (``json``, ``hash``, ``unordered_hash``, ``count``,
    ``index``, or ``explode``)."""
    default_array: Literal["json", "hash", "unordered_hash", "count"] = "json"
    """Strategy for arrays not listed in ``arrays``."""
    flatten: bool = True
    """Flatten nested documents into one column per leaf; when false, they become canonical
    JSON text."""
    max_depth: PositiveInt = Field(default=16, le=100)
    """Nesting depth beyond which nested documents become canonical JSON text."""
    separator: str = Field(default=".", min_length=1, max_length=4)
    """Joins the parts of flattened column names."""
    index_limit: PositiveInt = Field(default=20, le=1000)
    """Most positions turned into columns by the ``index`` strategy; longer arrays are truncated
    (and a note says how many)."""
    types: dict[str, ColumnTypeName] = Field(default_factory=dict)
    """Force output columns to a type, for example ISO date strings to ``timestamp``. Values
    that cannot be converted are errors."""
    primary_key: list[str] | None = None
    """Columns that identify a row, used by ``keys: auto``. Defaults to the source system's key
    when the rows are not unwound (``_id``, ``id``, key attributes, primary key)."""
    limit: PositiveInt | None = None
    """Read at most this many documents (rows before unwinding)."""
    batch_size: PositiveInt | None = Field(default=None, le=100_000)
    """Documents fetched per round trip (the connection's or driver's default when omitted)."""
    sample_size: PositiveInt = Field(default=1000, le=1_000_000)
    """Documents sampled by schema inference (``document_schema`` checks, profiling)."""

    @model_validator(mode="after")
    def _explode(self) -> DocumentOptions:
        exploded = [path for path, strategy in self.arrays.items() if strategy == "explode"]
        if len(exploded) > 1:
            raise ValueError(
                f"only one array can be exploded per dataset, got {', '.join(exploded)}; "
                "compare each array as its own table"
            )
        if exploded:
            if self.unwind is None:
                self.unwind = UnwindOptions(path=exploded[0])
            elif self.unwind.path != exploded[0]:
                raise ValueError(
                    f"'arrays.{exploded[0]}: explode' conflicts with 'unwind: {self.unwind.path}'"
                )
        if self.fields is not None and not self.fields:
            raise ValueError("'fields' is empty; omit it to flatten every field")
        return self


def parse_options(
    model: type[DocumentOptions], options: dict[str, Any], *, connection: str, dataset: str
) -> DocumentOptions:
    """Validate a dataset's ``options`` and turn validation errors into one clear message."""
    try:
        return model.model_validate(options)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            location = ".".join(str(p) for p in error["loc"]) or "options"
            message = error["msg"]
            if error["type"] == "extra_forbidden":
                message = "unknown option"
            problems.append(f"options.{location}: {message}")
        allowed = ", ".join(sorted(model.model_fields))
        raise ConnectorError(
            f"Invalid options for dataset '{dataset}' on connection '{connection}': "
            + "; ".join(problems),
            hint=f"Options this connector accepts: {allowed}.",
        ) from exc


__all__ = [
    "ArrayStrategy",
    "ColumnTypeName",
    "DocumentOptions",
    "UnwindOptions",
    "parse_options",
]
