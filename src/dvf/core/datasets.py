"""Dataset references and schemas, shared by connectors, checks, and results."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dvf.core.types import CanonicalType


class DatasetKind(StrEnum):
    TABLE = "table"
    QUERY = "query"
    PATH = "path"
    DATAFRAME = "dataframe"


class DatasetRef(BaseModel):
    """Where a dataset lives: a table, a query, a file path, or an in-process DataFrame."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection: str
    kind: DatasetKind
    name: str = Field(
        description="Qualified table name, file path, query label, or DataFrame name."
    )
    query: str | None = None

    @model_validator(mode="after")
    def _query_only_for_queries(self) -> DatasetRef:
        if (self.kind is DatasetKind.QUERY) != (self.query is not None):
            raise ValueError("'query' must be set if and only if kind is 'query'")
        return self

    def display(self) -> str:
        return f"{self.connection}:{self.name}"


class ColumnSchema(BaseModel):
    """One column as reported by a connector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    native_type: str
    type: CanonicalType
    nullable: bool | None = None
    default: str | None = None
    ordinal: int | None = None
    comment: str | None = None


class DatasetSchema(BaseModel):
    """The schema of a dataset, with optional key metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: DatasetRef
    columns: tuple[ColumnSchema, ...]
    primary_key: tuple[str, ...] = ()
    row_count_estimate: int | None = None

    def column(self, name: str, *, case_sensitive: bool = False) -> ColumnSchema | None:
        """Find a column by name."""
        if case_sensitive:
            return next((c for c in self.columns if c.name == name), None)
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)
