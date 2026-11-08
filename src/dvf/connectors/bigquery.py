"""Google BigQuery connector (beta; install with ``pip install 'dvf[bigquery]'``).

Implemented from the google-cloud-bigquery documentation and tested against a fake client; it
has not been verified against a live project.

Credentials come from ``credentials_path`` (a service account key file, relative to the
configuration's folder) or, when it is omitted, from application default credentials
(``gcloud auth application-default login``, workload identity, or ``GOOGLE_APPLICATION_CREDENTIALS``).

Queries run as standard SQL jobs labelled ``tool=dvf``, with ``dataset`` as the default dataset
and ``job_timeout_ms`` from ``query_timeout_s``. Other ``options`` are passed to
``QueryJobConfig`` (for example ``maximum_bytes_billed`` or ``priority``). BigQuery has no
read-only session mode: grant the service account ``roles/bigquery.dataViewer`` on the datasets
and ``roles/bigquery.jobUser`` on the project, nothing more.

Table schemas and declared (unenforced) primary keys come from the ``tables.get`` API, which is
free; query schemas come from a dry run, which is also free.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pyarrow as pa

from dvf.config.connections import BigQueryConnection
from dvf.config.models import DatasetSpec
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import Connector, Relation

_TYPE_NAMES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}
_LABELS = {"tool": "dvf"}


def _import_bigquery() -> Any:
    try:
        return importlib.import_module("google.cloud.bigquery")
    except ImportError as exc:
        raise DependencyError(
            "google-cloud-bigquery", "bigquery", "The BigQuery connector"
        ) from exc


def _error_types() -> tuple[type[BaseException], ...]:
    """Google API and authentication errors, plus job timeouts."""
    found: list[type[BaseException]] = [TimeoutError]
    for module_name, attr in (
        ("google.api_core.exceptions", "GoogleAPIError"),
        ("google.auth.exceptions", "GoogleAuthError"),
    ):
        try:
            candidate = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError):
            continue
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found.append(candidate)
    return tuple(found)


def _is_not_found(exc: BaseException) -> bool:
    return type(exc).__name__ == "NotFound" or getattr(exc, "code", None) == 404


def _hint(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "defaultcredentials" in text or "refresherror" in text or "credentials" in text:
        return (
            "No usable credentials: set 'credentials_path' to a service account key file, or "
            "run 'gcloud auth application-default login'."
        )
    if "forbidden" in text or "403" in text or "permission" in text or "access denied" in text:
        return (
            "Permission denied: grant roles/bigquery.dataViewer on the dataset and "
            "roles/bigquery.jobUser on the project to the account dvf uses."
        )
    if _is_not_found(exc) or "not found" in text:
        return "Check the project, dataset, and table names, and the connection's 'location'."
    if isinstance(exc, TimeoutError) or "timeout" in text:
        return "The job exceeded query_timeout_s; raise it or narrow the comparison."
    return "Check project, location, and credentials; run 'dvf connections test'."


def native_type(field: Any) -> str:
    """A GoogleSQL type string for a ``SchemaField`` (``ARRAY<...>`` for repeated fields)."""
    base = str(field.field_type).upper()
    base = _TYPE_NAMES.get(base, base)
    if base == "STRUCT":
        inner = ", ".join(f"{sub.name} {native_type(sub)}" for sub in field.fields or ())
        base = f"STRUCT<{inner}>"
    elif base in ("NUMERIC", "BIGNUMERIC") and getattr(field, "precision", None) is not None:
        base = f"{base}({field.precision}, {getattr(field, 'scale', None) or 0})"
    elif base in ("STRING", "BYTES") and getattr(field, "max_length", None):
        base = f"{base}({field.max_length})"
    if str(getattr(field, "mode", "") or "").upper() == "REPEATED":
        base = f"ARRAY<{base}>"
    return base


def table_sql(name: str) -> str:
    """A table path for SQL: quoted with backticks unless the configuration already quotes it."""
    return name if "`" in name else f"`{name}`"


class BigQueryConnector(Connector):
    """Google BigQuery, through google-cloud-bigquery."""

    dialect = "bigquery"
    Config = BigQueryConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._client: Any = None

    @property
    def _config(self) -> BigQueryConnection:
        config = self.config
        assert isinstance(config, BigQueryConnection)
        return config

    def _credentials_file(self) -> Path | None:
        path_text = self._config.credentials_path
        if not path_text:
            return None
        path = Path(path_text).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self._client is not None:
            return
        bq = _import_bigquery()
        c = self._config
        key_file = self._credentials_file()
        errors: tuple[type[BaseException], ...] = (*_error_types(), OSError, ValueError)
        try:
            if key_file is not None:
                self._client = bq.Client.from_service_account_json(
                    str(key_file), project=c.project, location=c.location
                )
            else:
                self._client = bq.Client(project=c.project, location=c.location)
        except errors as exc:
            raise ConnectorError(
                f"Could not create a BigQuery client for connection '{self.name}': {exc}",
                hint=_hint(exc),
            ) from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    @property
    def client(self) -> Any:
        self.open()
        return self._client

    def server_version(self) -> str | None:
        version = getattr(_import_bigquery(), "__version__", None)
        return "Google BigQuery" + (f" (google-cloud-bigquery {version})" if version else "")

    # ------------------------------------------------------------------ queries
    def _job_config(self, bq: Any, **extra: Any) -> Any:
        c = self._config
        settings: dict[str, Any] = {"use_legacy_sql": False, "labels": dict(_LABELS)}
        if c.dataset:
            settings["default_dataset"] = (
                c.dataset if "." in c.dataset else f"{c.project}.{c.dataset}"
            )
        if c.query_timeout_s:
            settings["job_timeout_ms"] = int(c.query_timeout_s) * 1000
        settings.update(c.options)
        settings.update(extra)
        try:
            return bq.QueryJobConfig(**settings)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ConnectorError(
                f"Invalid BigQuery job option on connection '{self.name}': {exc}",
                hint="Connection 'options' must be QueryJobConfig properties.",
            ) from exc

    def _fail(self, exc: BaseException) -> ConnectorError:
        return ConnectorError(f"Query failed on connection '{self.name}': {exc}", hint=_hint(exc))

    def _job(self, sql: str, **extra: Any) -> Any:
        bq = _import_bigquery()
        try:
            return self.client.query(sql, job_config=self._job_config(bq, **extra))
        except _error_types() as exc:
            raise self._fail(exc) from exc

    def _result(self, sql: str) -> Any:
        job = self._job(sql)
        try:
            return job.result(timeout=self._config.query_timeout_s)
        except _error_types() as exc:
            raise self._fail(exc) from exc

    def _storage_kwargs(self) -> dict[str, Any]:
        try:
            available = importlib.util.find_spec("google.cloud.bigquery_storage") is not None
        except (ImportError, ValueError):
            available = False
        return {"create_bqstorage_client": available}

    def query(self, sql: str) -> pa.Table:
        rows = self._result(sql)
        try:
            table: pa.Table = rows.to_arrow(**self._storage_kwargs())
        except _error_types() as exc:
            raise self._fail(exc) from exc
        return table

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        rows = self._result(sql)
        try:
            for batch in rows.to_arrow_iterable():
                yield from pa.Table.from_batches([batch]).to_batches(max_chunksize=batch_size)
        except _error_types() as exc:
            raise self._fail(exc) from exc

    # ------------------------------------------------------------------ datasets
    def _table_id(self, name: str) -> str:
        """``project.dataset.table`` (or ``dataset.table``) for the tables API."""
        plain = name.replace("`", "")
        if "." in plain:
            return plain
        dataset = self._config.dataset
        if not dataset:
            raise ConnectorError(
                f"Table '{name}' on connection '{self.name}' needs a dataset.",
                hint="Write 'dataset.table' or set 'dataset' on the connection.",
            )
        return (
            f"{dataset}.{plain}" if "." in dataset else f"{self._config.project}.{dataset}.{plain}"
        )

    def resolve(self, spec: DatasetSpec) -> Relation:
        if spec.path is not None:
            raise ConnectorError(
                f"Connection '{self.name}' is BigQuery; use 'table' or 'query', not 'path'."
            )
        table: str | None = None
        if spec.table is not None:
            kind, label = DatasetKind.TABLE, spec.table
            if spec.as_of is not None:
                literal = self.sql_dialect.literal(spec.as_of)
                base = f"{table_sql(spec.table)} FOR SYSTEM_TIME AS OF TIMESTAMP({literal})"
                label = f"{spec.table} AS OF {spec.as_of}"
            else:
                base, table = table_sql(spec.table), spec.table
        else:
            assert spec.query is not None
            if spec.as_of is not None:
                raise ConnectorError(
                    f"'as_of' applies to tables on connection '{self.name}'; "
                    "write FOR SYSTEM_TIME AS OF inside the query instead."
                )
            base, kind, label = f"(\n{spec.query}\n) AS dvf_query", DatasetKind.QUERY, "query"
        ref = DatasetRef(
            connection=self.name,
            kind=kind,
            name=label,
            query=spec.query if kind is DatasetKind.QUERY else None,
        )
        sql = (
            base
            if not spec.filter
            else f"(SELECT * FROM {base} WHERE {spec.filter}) AS dvf_filtered"
        )
        return Relation(ref, sql, base, table, spec.filter, self.name)

    def _get_table(self, name: str) -> Any:
        table_id = self._table_id(name)
        try:
            return self.client.get_table(table_id)
        except _error_types() as exc:
            if _is_not_found(exc):
                raise ConnectorError(
                    f"Table '{name}' not found on connection '{self.name}'.", hint=_hint(exc)
                ) from exc
            raise self._fail(exc) from exc

    def exists(self, relation: Relation) -> bool:
        if relation.table is None:
            return super().exists(relation)
        try:
            self.client.get_table(self._table_id(relation.table))
        except _error_types() as exc:
            if _is_not_found(exc):
                return False
            raise self._fail(exc) from exc
        return True

    def _columns(self, fields: Any) -> tuple[ColumnSchema, ...]:
        columns = []
        for i, field in enumerate(fields or ()):
            native = native_type(field)
            mode = str(getattr(field, "mode", "") or "NULLABLE").upper()
            columns.append(
                ColumnSchema(
                    name=str(field.name),
                    native_type=native,
                    type=parse_native_type(native, "bigquery"),
                    nullable=mode != "REQUIRED",
                    default=getattr(field, "default_value_expression", None),
                    ordinal=i + 1,
                    comment=getattr(field, "description", None) or None,
                )
            )
        return tuple(columns)

    def get_schema(self, relation: Relation) -> DatasetSchema:
        if relation.table is not None:
            table = self._get_table(relation.table)
            return DatasetSchema(
                ref=relation.ref,
                columns=self._columns(table.schema),
                primary_key=_primary_key(table),
                row_count_estimate=getattr(table, "num_rows", None),
            )
        sql = f"SELECT * FROM {relation.base_sql}"
        job = self._job(sql, dry_run=True, use_query_cache=False)
        fields = getattr(job, "schema", None)
        if fields is None:  # older clients expose the schema only on results
            fields = self._result(f"{sql} LIMIT 0").schema
        return DatasetSchema(ref=relation.ref, columns=self._columns(fields))

    def get_primary_key(self, relation: Relation) -> tuple[str, ...]:
        if relation.table is None:
            return ()
        return _primary_key(self._get_table(relation.table))

    def list_datasets(self, pattern: str) -> list[str]:
        c = self._config
        parts = pattern.replace("`", "").split(".")
        project = c.project
        try:
            if len(parts) == 1:
                if not c.dataset:
                    raise ConnectorError(
                        f"Pattern '{pattern}' on connection '{self.name}' needs a dataset.",
                        hint="Write 'dataset.table*' or set 'dataset' on the connection.",
                    )
                datasets, table_pattern, prefix = [c.dataset], parts[0], ""
            else:
                if len(parts) >= 3:
                    project = parts[0]
                dataset_pattern, table_pattern = parts[-2], parts[-1]
                datasets = sorted(
                    str(d.dataset_id)
                    for d in self.client.list_datasets(project=project)
                    if fnmatchcase(str(d.dataset_id).lower(), dataset_pattern.lower())
                )
                prefix = f"{project}." if len(parts) >= 3 else ""
            names: list[str] = []
            for dataset in datasets:
                dataset_id = dataset if "." in dataset else f"{project}.{dataset}"
                for item in self.client.list_tables(dataset_id):
                    table_id = str(item.table_id)
                    if fnmatchcase(table_id.lower(), table_pattern.lower()):
                        names.append(
                            table_id if len(parts) == 1 else f"{prefix}{dataset}.{table_id}"
                        )
        except _error_types() as exc:
            raise self._fail(exc) from exc
        return sorted(names)


def _primary_key(table: Any) -> tuple[str, ...]:
    constraints = getattr(table, "table_constraints", None)
    primary = getattr(constraints, "primary_key", None)
    columns = getattr(primary, "columns", None)
    return tuple(str(c) for c in columns) if columns else ()
