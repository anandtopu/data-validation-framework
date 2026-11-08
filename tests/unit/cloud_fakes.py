"""Fake DB-API drivers for the cloud warehouse connector tests (no network, no real drivers).

A :class:`Responder` maps SQL (by regular expression) to a :class:`Result` or an exception; fake
connections record every statement they run and every keyword argument they were opened with.
"""

from __future__ import annotations

import re
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa


class FakeError(Exception):
    """The driver's PEP 249 ``Error``."""


class FakeNotSupportedError(FakeError):
    """Snowflake's ``NotSupportedError`` (results not in Arrow format)."""


@dataclass
class Result:
    names: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    description: list[tuple[Any, ...]] | None = None
    arrow: bool = True
    """Whether Arrow fetch methods work for this result (Snowflake SHOW/DESCRIBE do not)."""

    def table(self) -> pa.Table:
        if self.rows:
            columns = list(zip(*self.rows, strict=True))
            return pa.table({n: list(c) for n, c in zip(self.names, columns, strict=True)})
        return pa.table({n: pa.array([], type=pa.string()) for n in self.names})


Rule = tuple[str, "Result | BaseException | Callable[[str], Result]"]


class Responder:
    """First matching rule wins; unmatched statements return no rows."""

    def __init__(self, *rules: Rule) -> None:
        self.rules = list(rules)

    def add(self, pattern: str, result: Result | BaseException | Callable[[str], Result]) -> None:
        self.rules.insert(0, (pattern, result))

    def __call__(self, sql: str) -> Result:
        for pattern, result in self.rules:
            if re.search(pattern, sql, re.IGNORECASE | re.DOTALL):
                if isinstance(result, BaseException):
                    raise result
                return result(sql) if callable(result) else result
        return Result()


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description: list[tuple[Any, ...]] | None = None
        self.result = Result()
        self._pending: list[tuple[Any, ...]] = []
        self.closed = False
        self.arrow_calls: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.connection.executed.append((sql, params))
        result = self.connection.responder(sql)
        self.result = result
        if result.description is not None:
            self.description = result.description
        elif result.names:
            self.description = [(n, None, None, None, None, None, None) for n in result.names]
        else:
            self.description = None
        self._pending = list(result.rows)
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows, self._pending = self._pending, []
        return rows

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        rows, self._pending = self._pending[:size], self._pending[size:]
        return rows

    def close(self) -> None:
        self.closed = True

    # Snowflake Arrow API
    def fetch_arrow_all(
        self, force_return_table: bool = False, force_microsecond_precision: bool | None = None
    ) -> pa.Table | None:
        self.arrow_calls.append(
            {
                "force_return_table": force_return_table,
                "force_microsecond_precision": force_microsecond_precision,
            }
        )
        if not self.result.arrow:
            raise FakeNotSupportedError("not arrow")
        if not self.result.rows and not force_return_table:
            return None
        return self.result.table()

    def fetch_arrow_batches(
        self, force_microsecond_precision: bool | None = None
    ) -> Iterator[pa.Table]:
        self.arrow_calls.append({"force_microsecond_precision": force_microsecond_precision})
        if not self.result.arrow:
            raise FakeNotSupportedError("not arrow")
        table = self.result.table()
        # Two "result chunks", as the service returns them.
        half = table.num_rows // 2
        for chunk in (table.slice(0, half), table.slice(half)):
            if chunk.num_rows:
                yield chunk

    # Databricks Arrow API
    def fetchall_arrow(self) -> pa.Table:
        table = self.result.table()
        self._pending = []
        return table

    def fetchmany_arrow(self, size: int) -> pa.Table:
        rows, self._pending = self._pending[:size], self._pending[size:]
        names = self.result.names
        if not rows:
            return pa.table({n: pa.array([], type=pa.string()) for n in names})
        columns = list(zip(*rows, strict=True))
        return pa.table({n: list(c) for n, c in zip(names, columns, strict=True)})


class FakeConnection:
    def __init__(self, responder: Responder, **kwargs: Any) -> None:
        self.responder = responder
        self.kwargs = kwargs
        self.executed: list[tuple[str, Any]] = []
        self.closed = False
        self.autocommit = False
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed]


class FakeDriver:
    """A fake driver module: ``connect(**kwargs)`` returns a :class:`FakeConnection`."""

    def __init__(self, module_name: str, version: str = "1.0.0") -> None:
        self.responder = Responder()
        self.connections: list[FakeConnection] = []
        self.connect_error: BaseException | None = None
        self.module = types.ModuleType(module_name)
        self.module.connect = self.connect  # type: ignore[attr-defined]
        self.module.Error = FakeError  # type: ignore[attr-defined]
        self.module.NotSupportedError = FakeNotSupportedError  # type: ignore[attr-defined]
        self.module.__version__ = version  # type: ignore[attr-defined]

    def connect(self, *args: Any, **kwargs: Any) -> FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeConnection(self.responder, **kwargs)
        self.connections.append(connection)
        return connection

    @property
    def connection(self) -> FakeConnection:
        return self.connections[-1]


# ====================================================================== BigQuery


class GoogleAPIError(Exception):
    code: int | None = None


class NotFound(GoogleAPIError):  # noqa: N818 - mirrors google.api_core
    code = 404


class Forbidden(GoogleAPIError):  # noqa: N818 - mirrors google.api_core
    code = 403


class GoogleAuthError(Exception):
    pass


class DefaultCredentialsError(GoogleAuthError):
    pass


@dataclass
class SchemaField:
    name: str
    field_type: str
    mode: str = "NULLABLE"
    fields: tuple[SchemaField, ...] = ()
    precision: int | None = None
    scale: int | None = None
    max_length: int | None = None
    description: str | None = None
    default_value_expression: str | None = None


@dataclass
class FakeRowIterator:
    table: pa.Table
    schema: list[SchemaField] = field(default_factory=list)
    to_arrow_kwargs: dict[str, Any] = field(default_factory=dict)

    def to_arrow(self, **kwargs: Any) -> pa.Table:
        self.to_arrow_kwargs = kwargs
        return self.table

    def to_arrow_iterable(self, **kwargs: Any) -> Iterator[pa.RecordBatch]:
        yield from self.table.to_batches(max_chunksize=max(1, self.table.num_rows))


@dataclass
class FakeJob:
    sql: str
    job_config: Any
    rows: FakeRowIterator
    schema: list[SchemaField] | None = None
    timeout: Any = None

    def result(self, timeout: Any = None) -> FakeRowIterator:
        self.timeout = timeout
        return self.rows


class QueryJobConfig:
    def __init__(self, **kwargs: Any) -> None:
        unknown = set(kwargs) - {
            "use_legacy_sql",
            "labels",
            "default_dataset",
            "job_timeout_ms",
            "dry_run",
            "use_query_cache",
            "maximum_bytes_billed",
            "priority",
        }
        if unknown:
            raise AttributeError(f"Property {sorted(unknown)[0]} is unknown for QueryJobConfig.")
        self.settings = kwargs


@dataclass
class FakeTable:
    schema: list[SchemaField]
    table_constraints: Any = None
    num_rows: int | None = None


@dataclass
class _Named:
    dataset_id: str = ""
    table_id: str = ""


class FakeBigQuery:
    """A fake ``google.cloud.bigquery`` module plus its exception modules."""

    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        self.query_results: dict[str, tuple[pa.Table, list[SchemaField]]] = {}
        self.query_error: BaseException | None = None
        self.tables: dict[str, FakeTable] = {}
        self.datasets: dict[str, dict[str, list[str]]] = {}
        """project -> dataset -> table names."""
        fake = self

        class Client:
            def __init__(self, project: str | None = None, location: str | None = None, **kw: Any):
                self.project = project
                self.location = location
                self.credentials_file: str | None = None
                self.jobs: list[FakeJob] = []
                self.closed = False
                fake.clients.append(self)  # type: ignore[arg-type]

            @classmethod
            def from_service_account_json(cls, path: str, **kwargs: Any) -> Any:
                client = cls(**kwargs)
                client.credentials_file = path
                return client

            def query(self, sql: str, job_config: Any = None) -> FakeJob:
                if fake.query_error is not None:
                    raise fake.query_error
                table, schema = pa.table({"ok": [1]}), []
                for pattern, value in fake.query_results.items():
                    if re.search(pattern, sql, re.IGNORECASE | re.DOTALL):
                        table, schema = value
                        break
                dry = bool(job_config and job_config.settings.get("dry_run"))
                job = FakeJob(
                    sql, job_config, FakeRowIterator(table, schema), schema if dry else None
                )
                self.jobs.append(job)
                return job

            def get_table(self, table_id: str) -> FakeTable:
                if table_id not in fake.tables:
                    raise NotFound(f"Not found: Table {table_id}")
                return fake.tables[table_id]

            def list_datasets(self, project: str | None = None) -> list[_Named]:
                return [_Named(dataset_id=d) for d in fake.datasets.get(project or "", {})]

            def list_tables(self, dataset: str) -> list[_Named]:
                project, dataset_id = dataset.split(".", 1)
                names = fake.datasets.get(project, {}).get(dataset_id, [])
                return [_Named(table_id=t) for t in names]

            def close(self) -> None:
                self.closed = True

        self.module = types.ModuleType("google.cloud.bigquery")
        self.module.Client = Client  # type: ignore[attr-defined]
        self.module.QueryJobConfig = QueryJobConfig  # type: ignore[attr-defined]
        self.module.__version__ = "3.99.0"  # type: ignore[attr-defined]
        self.api_exceptions = types.ModuleType("google.api_core.exceptions")
        self.api_exceptions.GoogleAPIError = GoogleAPIError  # type: ignore[attr-defined]
        self.api_exceptions.NotFound = NotFound  # type: ignore[attr-defined]
        self.api_exceptions.Forbidden = Forbidden  # type: ignore[attr-defined]
        self.auth_exceptions = types.ModuleType("google.auth.exceptions")
        self.auth_exceptions.GoogleAuthError = GoogleAuthError  # type: ignore[attr-defined]
        self.auth_exceptions.DefaultCredentialsError = DefaultCredentialsError  # type: ignore[attr-defined]

    @property
    def client(self) -> FakeClient:
        return self.clients[-1]


FakeClient = Any
