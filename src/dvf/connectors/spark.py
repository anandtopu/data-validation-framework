"""Apache Spark connector (``pip install 'dvf-framework[spark]'``; Spark needs Java 17 or later).

A ``spark`` connection works in a Spark session:

* With ``master`` unset, dvf uses the session that is already running (a notebook, a Spark job,
  or an in-pipeline call) and starts a ``local[*]`` session when there is none.
* With ``master`` set (``local[4]``, ``spark://host:7077``, ``yarn``, ...), dvf starts a session
  with that master, or uses the running one (Spark allows one per process).
* With ``databricks_connect: true``, dvf opens a Databricks Connect session. It needs the
  ``databricks-connect`` package (installed instead of pyspark) and is not verified by dvf's tests.

dvf works in a child session (``SparkSession.newSession()``) that shares the application's
cluster, tables, and cache but has its own settings and temporary views, so nothing dvf sets
leaks into your session. The child session uses ``spark.sql.session.timeZone=UTC`` (timestamp
canonicalization relies on it), adaptive query execution with skew-join handling, the
connection's ``conf``, and the connection's ``catalog`` and ``schema`` as current namespace.

Datasets are catalog tables (``table``), Spark SQL queries (``query``), or files read by Spark
(``path`` plus a format: Parquet, ORC, CSV, TSV, JSON, Avro, Delta, Iceberg). Files become
temporary views of dvf's session when first used. ``as_of`` reads Delta and Iceberg data as of
a version (snapshot ID) or a timestamp. Spark knows no primary keys, so ``keys: auto`` cannot
be used. :meth:`SparkConnector.from_dataframe` wraps a DataFrame you already have.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PureWindowsPath
from typing import Any

import pyarrow as pa

from dvf.config.connections import SparkConnection
from dvf.config.models import DatasetSpec
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import ConnectorError, DependencyError
from dvf.core.types import parse_native_type
from dvf.plugins.base import Connector, Relation
from dvf.sql.spark import DIALECT as _SQL

log = logging.getLogger(__name__)

TIME_ZONE_KEY = "spark.sql.session.timeZone"
SESSION_DEFAULTS: dict[str, str] = {
    "spark.sql.adaptive.enabled": "true",
    "spark.sql.adaptive.coalescePartitions.enabled": "true",
    "spark.sql.adaptive.skewJoin.enabled": "true",
}
"""Settings of every dvf session; the connection's ``conf`` overrides them."""
ENFORCED: dict[str, str] = {
    TIME_ZONE_KEY: "UTC",
    "spark.sql.parser.escapedStringLiterals": "false",
}
"""Settings dvf's SQL relies on; they cannot be overridden."""
LOCAL_DEFAULTS: dict[str, str] = {
    "spark.ui.enabled": "false",
    "spark.ui.showConsoleProgress": "false",
}
"""Extra settings of local sessions dvf starts (no web UI, no progress bars on the console)."""

FORMATS: dict[str, str] = {
    "parquet": "parquet",
    "orc": "orc",
    "csv": "csv",
    "tsv": "csv",
    "json": "json",
    "ndjson": "json",
    "avro": "avro",
    "delta": "delta",
    "iceberg": "iceberg",
}
"""dvf format names and the Spark data source that reads each."""
_EXTENSIONS = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".orc": "orc",
    ".csv": "csv",
    ".tsv": "tsv",
    ".txt": "csv",
    ".json": "json",
    ".jsonl": "ndjson",
    ".ndjson": "ndjson",
    ".avro": "avro",
}
_COMPRESSION_SUFFIXES = (".gz", ".bz2", ".zst", ".snappy", ".lz4", ".deflate")
_PACKAGES = {
    "avro": "org.apache.spark:spark-avro_2.13:<spark version>",
    "delta": "io.delta:delta-spark_2.13:<delta version>",
    "iceberg": "org.apache.iceberg:iceberg-spark-runtime-<spark minor>_2.13:<iceberg version>",
}
_VIEW_RE = re.compile(r"dvf_path_[0-9a-f]{12}")
_JOB_GROUP_KEYS = ("spark.jobGroup.id", "spark.job.description", "spark.job.interruptOnCancel")
_VERSION_RE = re.compile(r"\d+")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")


# ====================================================================== sessions


def _pyspark_sql() -> Any:
    try:
        return importlib.import_module("pyspark.sql")
    except ImportError as exc:
        raise DependencyError("pyspark", "spark", "The Spark connector") from exc


def spark_errors() -> tuple[type[BaseException], ...]:
    """Exception types PySpark raises for failed queries and sessions."""
    types: list[type[BaseException]] = []
    for module_name, names in (
        ("pyspark.errors", ("PySparkException",)),
        ("py4j.protocol", ("Py4JError",)),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in names:
            found = getattr(module, name, None)
            if isinstance(found, type) and issubclass(found, BaseException):
                types.append(found)
    return tuple(types) or (Exception,)


def is_connect(session: Any) -> bool:
    """Whether ``session`` is a Spark Connect session (Databricks Connect, ``spark.remote``)."""
    return type(session).__module__.startswith("pyspark.sql.connect")


def _alive(session: Any) -> bool:
    if is_connect(session):
        return True
    context = getattr(session, "_sc", None)
    return context is not None and getattr(context, "_jsc", None) is not None


def running_session() -> Any | None:
    """The active or default Spark session of this process, if one is running."""
    spark_sql = _pyspark_sql()
    session = spark_sql.SparkSession.getActiveSession()
    if session is None:
        try:
            session = spark_sql.SparkSession.active()
        except Exception:  # no active or default session
            return None
    return session if session is not None and _alive(session) else None


def start_session(
    master: str = "local[*]", app_name: str = "dvf", conf: dict[str, str] | None = None
) -> Any:
    """Start a Spark session with dvf's settings (or return the running one: Spark allows one
    per process, and then only runtime settings of ``conf`` apply)."""
    spark_sql = _pyspark_sql()
    settings = dict(SESSION_DEFAULTS)
    if master.startswith("local"):
        settings.update(LOCAL_DEFAULTS)
    settings.update(conf or {})
    settings.update(ENFORCED)
    builder = spark_sql.SparkSession.builder.master(master).appName(app_name)
    for key, value in settings.items():
        builder = builder.config(key, value)
    failures: tuple[type[BaseException], ...] = (*spark_errors(), OSError, RuntimeError)
    try:
        return builder.getOrCreate()
    except failures as exc:
        raise ConnectorError(
            f"Could not start a Spark session with master '{master}': {exc}",
            hint="Spark needs Java 17 or later on PATH or in JAVA_HOME; check the master URL.",
        ) from exc


def configure_session(session: Any, conf: dict[str, str] | None = None) -> None:
    """Apply dvf's runtime settings and ``conf`` to ``session`` (static settings are skipped)."""
    settings = {**SESSION_DEFAULTS, **(conf or {})}
    if settings.get(TIME_ZONE_KEY, "UTC") != "UTC":
        log.warning("dvf sessions use the UTC time zone; ignoring %s", TIME_ZONE_KEY)
    settings.update(ENFORCED)
    runtime = session.conf
    for key, value in settings.items():
        current = _setting(runtime, key)
        if current == value:
            continue  # setting a deprecated key at all logs a warning
        try:
            modifiable = runtime.isModifiable(key)
        except Exception:  # Spark Connect may not answer for unknown keys
            modifiable = True
        if modifiable:
            try:
                runtime.set(key, value)
                continue
            except spark_errors():
                pass
        log.warning(
            "Spark setting %s=%s cannot change on a running session (it is %s); set it "
            "where the session is started",
            key,
            value,
            current,
        )


def _setting(runtime: Any, key: str) -> str | None:
    try:
        value = runtime.get(key)
    except Exception:  # unset keys without a default raise
        return None
    return None if value is None else str(value)


def child_session(root: Any, conf: dict[str, str] | None = None) -> Any:
    """A configured session for dvf on ``root``'s application: a new session for classic Spark,
    ``root`` itself for Spark Connect (which cannot fork sessions)."""
    session = root if is_connect(root) else root.newSession()
    configure_session(session, conf)
    return session


@contextlib.contextmanager
def keep_active_session(session: Any) -> Iterator[None]:
    """Keep the thread's active Spark session: PySpark makes a session active when it builds a
    DataFrame from local data, and dvf's sessions must never replace yours."""
    if is_connect(session):
        yield
        return
    session_class = type(session)
    jvm_class = session._jvm.SparkSession
    saved_python = getattr(session_class, "_activeSession", None)
    saved = jvm_class.getActiveSession()
    try:
        yield
    finally:
        with contextlib.suppress(*spark_errors()):
            if saved.isDefined():
                jvm_class.setActiveSession(saved.get())
            else:
                jvm_class.clearActiveSession()
            session_class._activeSession = saved_python


_ENGINE_LOCK = threading.Lock()
_ENGINE_SESSIONS: dict[int, Any] = {}


def engine_session() -> Any:
    """dvf's child session on the running Spark session, or on a new ``local[*]`` session.

    Used by the Spark engine when neither side of a comparison is a Spark connection. A session
    dvf starts here stays running for later comparisons; stop it with
    ``SparkSession.active().stop()``.
    """
    with _ENGINE_LOCK:
        root = running_session() or start_session()
        key = id(root)
        session = _ENGINE_SESSIONS.get(key)
        if session is None:
            _ENGINE_SESSIONS.clear()
            session = child_session(root)
            _ENGINE_SESSIONS[key] = session
        return session


def global_temp_database(session: Any) -> str:
    """The database that holds global temporary views (``global_temp`` by default)."""
    return str(session.conf.get("spark.sql.globalTempDatabase", "global_temp"))


def same_application(a: Any, b: Any) -> bool:
    """Whether two sessions share one Spark application (and so tables, cache, and global
    temporary views)."""
    if a is b:
        return True
    if is_connect(a) or is_connect(b):
        return False
    context_a, context_b = getattr(a, "_sc", None), getattr(b, "_sc", None)
    return context_a is not None and context_a is context_b


def _databricks_session(config: SparkConnection) -> Any:
    try:
        module = importlib.import_module("databricks.connect")
    except ImportError as exc:
        error = DependencyError(
            "databricks-connect", "spark", "A Databricks Connect session (databricks_connect)"
        )
        error.hint = (
            "Install Databricks Connect in an environment without pyspark: "
            "pip install 'databricks-connect==<your runtime version>.*'"
        )
        raise error from exc
    builder = module.DatabricksSession.builder
    options = config.options
    if options.get("profile"):
        builder = builder.profile(str(options["profile"]))
    if options.get("serverless"):
        builder = builder.serverless(True)
    if options.get("cluster_id"):
        builder = builder.clusterId(str(options["cluster_id"]))
    try:
        return builder.getOrCreate()
    except Exception as exc:  # the SDK raises several error types for bad configuration
        raise ConnectorError(
            f"Could not open a Databricks Connect session: {exc}",
            hint="Check the Databricks configuration profile, host, token, and cluster ID.",
        ) from exc


# ====================================================================== helpers


def _hint(exc: BaseException) -> str | None:
    text = str(exc)
    if "TABLE_OR_VIEW_NOT_FOUND" in text:
        return "Check the table name and the connection's catalog and schema."
    if "PATH_NOT_FOUND" in text:
        return "Check the path; relative paths are relative to the configuration's folder."
    if "DATA_SOURCE_NOT_FOUND" in text:
        return (
            "This file format needs a Spark package: add it to the connection's conf "
            "(spark.jars.packages) or to the cluster."
        )
    if "UNRESOLVED_COLUMN" in text:
        return "A column named in the configuration does not exist in the dataset."
    if "JAVA_GATEWAY_EXITED" in text or "Java gateway" in text:
        return "Spark needs Java 17 or later on PATH or in JAVA_HOME."
    return None


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _location(path: str, base_dir: Path) -> str:
    """A path Spark can read: URLs unchanged, local paths as absolute ``file:`` URIs."""
    if _SCHEME_RE.match(path) and not PureWindowsPath(path).drive:
        return path
    local = Path(path)
    if not local.is_absolute():
        local = base_dir / local
    posix = local.absolute().as_posix()
    return f"file:///{posix}" if not posix.startswith("/") else f"file://{posix}"


def _infer_format(path: str) -> str | None:
    name = path.lower().rstrip("/")
    for suffix in _COMPRESSION_SUFFIXES:
        name = name.removesuffix(suffix)
    return _EXTENSIONS.get(Path(name).suffix)


def _epoch_millis(text: str) -> int:
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def as_of_clause(as_of: str) -> str:
    """``VERSION AS OF n`` for an integer, ``TIMESTAMP AS OF '...'`` otherwise."""
    value = as_of.strip()
    if _VERSION_RE.fullmatch(value):
        return f"VERSION AS OF {int(value)}"
    return f"TIMESTAMP AS OF {_SQL.literal(value)}"


def _has_timestamp(data_type: Any, types: Any) -> bool:
    if isinstance(data_type, types.TimestampType | types.TimestampNTZType):
        return True
    if isinstance(data_type, types.ArrayType):
        return _has_timestamp(data_type.elementType, types)
    if isinstance(data_type, types.MapType):
        return _has_timestamp(data_type.keyType, types) or _has_timestamp(
            data_type.valueType, types
        )
    if isinstance(data_type, types.StructType):
        return any(_has_timestamp(f.dataType, types) for f in data_type.fields)
    return False


def _text_timestamps(data_type: Any, types: Any) -> Any:
    """``data_type`` with every nested timestamp replaced by STRING."""
    if isinstance(data_type, types.TimestampType | types.TimestampNTZType):
        return types.StringType()
    if isinstance(data_type, types.ArrayType):
        return types.ArrayType(
            _text_timestamps(data_type.elementType, types), data_type.containsNull
        )
    if isinstance(data_type, types.MapType):
        return types.MapType(
            _text_timestamps(data_type.keyType, types),
            _text_timestamps(data_type.valueType, types),
            data_type.valueContainsNull,
        )
    if isinstance(data_type, types.StructType):
        return types.StructType(
            [
                types.StructField(
                    f.name, _text_timestamps(f.dataType, types), f.nullable, f.metadata
                )
                for f in data_type.fields
            ]
        )
    return data_type


@dataclass(frozen=True)
class _Collected:
    """How one column is collected through Python rows: a SQL expression and its Arrow type."""

    expr: str
    arrow_type: pa.DataType
    micros: bool = False
    """The expression yields microseconds since the epoch, cast to ``arrow_type`` afterwards."""


def _collection_plan(schema: Any) -> list[_Collected]:
    """Collect timestamps as epoch microseconds: PySpark turns collected timestamps into
    ``datetime`` objects in the local time zone, and fails before 1970 on Windows."""
    types = importlib.import_module("pyspark.sql.types")
    arrow = importlib.import_module("pyspark.sql.pandas.types")
    plan: list[_Collected] = []
    for i, f in enumerate(schema.fields):
        name = f"dvf_c{i}"
        data_type = f.dataType
        if isinstance(data_type, types.TimestampType):
            plan.append(_Collected(f"unix_micros({name})", pa.timestamp("us", tz="UTC"), True))
            continue
        if isinstance(data_type, types.TimestampNTZType):
            # The session time zone is UTC, so the wall-clock time converts unchanged.
            plan.append(
                _Collected(f"unix_micros(CAST({name} AS TIMESTAMP))", pa.timestamp("us"), True)
            )
            continue
        expr = name
        if _has_timestamp(data_type, types):
            data_type = _text_timestamps(data_type, types)
            expr = f"CAST({name} AS {data_type.simpleString()})"
        try:
            arrow_type = arrow.to_arrow_type(data_type, timezone="UTC")
        except Exception:  # types Arrow cannot hold (VARIANT, intervals): collect as text
            expr, arrow_type = f"CAST({name} AS STRING)", pa.string()
        plan.append(_Collected(expr, arrow_type))
    return plan


def _plain(value: Any) -> Any:
    """A collected value in the form pyarrow converts (structs as tuples, binaries as bytes)."""
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return [(_plain(k), _plain(v)) for k, v in value.items()]
    if isinstance(value, tuple):
        names = getattr(value, "__fields__", None)
        if names is None:
            return [_plain(v) for v in value]
        return {name: _plain(v) for name, v in zip(names, value, strict=True)}
    return value


# ====================================================================== connector


@dataclass
class _Shared:
    """Session state shared by a connector and the sessions forked from it."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    session: Any = None
    pending: dict[str, DatasetSpec] = field(default_factory=dict)
    """File datasets resolved but not yet registered, by view name."""
    views: set[str] = field(default_factory=set)
    """Temporary views registered in the session."""
    global_views: set[str] = field(default_factory=set)
    """Global temporary views created for this connector (dropped on close)."""
    restore: dict[str, str | None] = field(default_factory=dict)
    """Settings to restore on close, when dvf had to change a session it does not own."""


class SparkConnector(Connector):
    """Spark sessions: local, cluster, the running session, or Databricks Connect."""

    dialect = "spark"
    Config = SparkConnection

    def __init__(self, name: str, config: Any, base_dir: Path) -> None:
        super().__init__(name, config, base_dir)
        self._shared = _Shared()
        self._owner = True

    @property
    def _config(self) -> SparkConnection:
        config = self.config
        assert isinstance(config, SparkConnection)
        return config

    def is_transient(self, exc: BaseException) -> bool:
        """Spark retries failed tasks itself, so only Spark Connect's lost connections are worth
        a retry (reconnecting a classic session would drop its temporary views)."""
        session = self._shared.session
        return session is not None and is_connect(session) and super().is_transient(exc)

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_dataframe(
        cls, df: Any, name: str = "dataframe", *, base_dir: Path | None = None
    ) -> tuple[SparkConnector, Relation]:
        """Wrap a Spark DataFrame as a dataset of a new connector on the DataFrame's session.

        The DataFrame becomes a global temporary view (dropped when the connector closes), so
        dvf's child session and the Spark engine can read it. Returns the connector, already
        open, and the DataFrame's relation (``ref.kind`` is ``dataframe``).
        """
        connector = cls(name, SparkConnection(type="spark"), base_dir or Path.cwd())
        session = connector._attach(df.sparkSession)
        view = f"dvf_frame_{secrets.token_hex(6)}"
        try:
            df.createOrReplaceGlobalTempView(view)
        except spark_errors() as exc:
            raise ConnectorError(f"Could not register the DataFrame '{name}': {exc}") from exc
        connector._shared.global_views.add(view)
        sql = f"{_SQL.quote(global_temp_database(session))}.{_SQL.quote(view)}"
        ref = DatasetRef(connection=name, kind=DatasetKind.DATAFRAME, name=name)
        return connector, Relation(ref=ref, sql=sql, base_sql=sql, connection=name)

    @classmethod
    def for_session(cls, session: Any, name: str = "spark") -> SparkConnector:
        """A connector that uses ``session`` as it is (dvf's own sessions, such as the engine's)."""
        connector = cls(name, SparkConnection(type="spark"), Path.cwd())
        connector._shared.session = session
        return connector

    def _attach(self, root: Any) -> Any:
        session = child_session(root, self._config.conf)
        with self._shared.lock:
            self._shared.session = session
        return session

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        with self._shared.lock:
            if self._shared.session is not None:
                return
            config = self._config
            if config.databricks_connect:
                root = _databricks_session(config)
            else:
                # Spark allows one application per process: a running one is used as it is.
                root = running_session()
                if root is None:
                    root = start_session(config.master or "local[*]", config.app_name, config.conf)
                elif (
                    config.master
                    and not is_connect(root)
                    and root.sparkContext.master != config.master
                ):
                    log.warning(
                        "connection %s asks for master %s, but a Spark session with master %s "
                        "is already running in this process; using it",
                        self.name,
                        config.master,
                        root.sparkContext.master,
                    )
            if is_connect(root):
                self._remember(root)
            session = child_session(root, config.conf)
            self._set_namespace(session)
            level = config.options.get("log_level")
            if level and not is_connect(session):
                session.sparkContext.setLogLevel(str(level).upper())
            self._shared.session = session

    def _remember(self, session: Any) -> None:
        """Note settings dvf changes on a session it shares with its owner, to restore them."""
        keys = [*SESSION_DEFAULTS, *self._config.conf, *ENFORCED]
        for key in keys:
            try:
                self._shared.restore[key] = session.conf.get(key, None)
            except spark_errors():
                continue

    def _set_namespace(self, session: Any) -> None:
        config = self._config
        try:
            if config.catalog:
                session.catalog.setCurrentCatalog(config.catalog)
            if config.schema_:
                session.catalog.setCurrentDatabase(config.schema_)
        except spark_errors() as exc:
            raise ConnectorError(
                f"Connection '{self.name}' cannot use catalog '{config.catalog}' and schema "
                f"'{config.schema_}': {_first_line(exc)}",
                hint="Check that the catalog and schema exist and that you may use them.",
            ) from exc

    def close(self) -> None:
        if not self._owner:
            return
        shared = self._shared
        with shared.lock:
            session = shared.session
            if session is None:
                return
            for view in sorted(shared.views):
                with contextlib.suppress(*spark_errors()):
                    session.catalog.dropTempView(view)
            for view in sorted(shared.global_views):
                with contextlib.suppress(*spark_errors()):
                    session.catalog.dropGlobalTempView(view)
            for key, value in shared.restore.items():
                with contextlib.suppress(*spark_errors()):
                    if value is None:
                        session.conf.unset(key)
                    else:
                        session.conf.set(key, value)
            shared.views.clear()
            shared.global_views.clear()
            shared.restore.clear()
            shared.pending.clear()
            shared.session = None

    def fork(self) -> Connector:
        """A connector on the same Spark session (Spark sessions are thread-safe)."""
        twin = type(self)(self.name, self.config, self.base_dir)
        twin._shared = self._shared
        twin._owner = False
        twin.retry_policy = self.retry_policy
        return twin

    @property
    def session(self) -> Any:
        """dvf's Spark session for this connection (opened on first use)."""
        self.open()
        session = self._shared.session
        assert session is not None
        return session

    def server_version(self) -> str | None:
        session = self.session
        kind = " (Spark Connect)" if is_connect(session) else ""
        return f"Apache Spark {session.version}{kind}"

    def colocated(self, session: Any) -> bool:
        """Whether this connector's data is readable from ``session`` without moving it."""
        return same_application(self.session, session)

    # ------------------------------------------------------------------ queries
    def frame(self, sql: str) -> Any:
        """A DataFrame of ``sql`` in this connection's session (file views registered first)."""
        self._ensure_views(sql)
        try:
            return self.session.sql(sql)
        except spark_errors() as exc:
            raise self._fail(exc) from exc

    def _fail(self, exc: BaseException) -> ConnectorError:
        return ConnectorError(
            f"Query failed on connection '{self.name}': {_first_line(exc)}", hint=_hint(exc)
        )

    @contextlib.contextmanager
    def _deadline(self) -> Iterator[Callable[[], bool]]:
        """Cancel the Spark jobs started inside the block after ``query_timeout_s``.

        Classic sessions run the block's jobs in a job group (inherited by the threads Spark
        starts to serve results, unlike session tags); Spark Connect tags its operations.
        """
        timeout = self._config.query_timeout_s
        session = self.session
        fired = threading.Event()
        if not timeout:
            yield fired.is_set
            return
        name = f"dvf-{secrets.token_hex(6)}"
        restore: Callable[[], None]
        if is_connect(session):
            session.addTag(name)

            def cancel() -> None:
                session.interruptTag(name)

            def restore() -> None:
                session.removeTag(name)

        else:
            context = session.sparkContext
            saved = {key: context.getLocalProperty(key) for key in _JOB_GROUP_KEYS}
            context.setJobGroup(name, f"dvf: connection {self.name}", interruptOnCancel=True)

            def cancel() -> None:
                context.cancelJobGroup(name)

            def restore() -> None:
                for key, value in saved.items():
                    context.setLocalProperty(key, value)

        done = threading.Event()

        def expire() -> None:
            if done.wait(float(timeout)):
                return
            fired.set()
            # Jobs may start after the deadline (planning takes time): keep cancelling.
            while not done.is_set():
                with contextlib.suppress(*spark_errors()):
                    cancel()
                done.wait(0.5)

        watchdog = threading.Thread(target=expire, name=f"{name}-timeout", daemon=True)
        watchdog.start()
        try:
            yield fired.is_set
        finally:
            done.set()
            with contextlib.suppress(*spark_errors()):
                restore()

    def _timed_out(self, exc: BaseException) -> ConnectorError:
        return ConnectorError(
            f"Query on connection '{self.name}' was cancelled after "
            f"{self._config.query_timeout_s} seconds (query_timeout_s): {_first_line(exc)}",
            hint="Raise query_timeout_s, or filter the dataset.",
        )

    def query(self, sql: str) -> pa.Table:
        with self._deadline() as timed_out:
            try:
                table: pa.Table = self.frame(sql).toArrow()
            except spark_errors() as exc:
                raise (self._timed_out(exc) if timed_out() else self._fail(exc)) from exc
            except ConnectorError as exc:
                if timed_out():
                    raise self._timed_out(exc) from exc
                raise
        return table

    def iter_batches(self, sql: str, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
        """Stream the rows one partition at a time (one Spark job per partition), so memory on
        the driver and here stays bounded by a partition and a batch."""
        df = self.frame(sql)
        names = list(df.columns)
        plan = _collection_plan(df.schema)
        positional = df.toDF(*[f"dvf_c{i}" for i in range(len(names))])
        selected = positional.selectExpr(*[f"{c.expr} AS dvf_c{i}" for i, c in enumerate(plan)])
        schema = pa.schema([pa.field(n, c.arrow_type) for n, c in zip(names, plan, strict=True)])
        with self._deadline() as timed_out:
            try:
                buffer: list[Any] = []
                for row in selected.toLocalIterator():
                    buffer.append(row)
                    if len(buffer) >= batch_size:
                        yield _batch(buffer, plan, schema)
                        buffer = []
                if buffer:
                    yield _batch(buffer, plan, schema)
            except spark_errors() as exc:
                raise (self._timed_out(exc) if timed_out() else self._fail(exc)) from exc

    def register_arrow(self, name: str, table: pa.Table) -> bool:
        try:
            with keep_active_session(self.session):
                self.session.createDataFrame(table).createOrReplaceTempView(name)
        except spark_errors() as exc:
            raise self._fail(exc) from exc
        with self._shared.lock:
            self._shared.views.add(name)
        return True

    def unregister_arrow(self, name: str) -> None:
        with self._shared.lock:
            self._shared.views.discard(name)
        with contextlib.suppress(*spark_errors()):
            self.session.catalog.dropTempView(name)

    # ------------------------------------------------------------------ datasets
    def resolve(self, spec: DatasetSpec) -> Relation:
        table: str | None = None
        query: str | None = None
        if spec.table is not None:
            kind, label = DatasetKind.TABLE, spec.table
            if spec.as_of is not None:
                base = f"{spec.table} {as_of_clause(spec.as_of)}"
                label = f"{spec.table} AS OF {spec.as_of}"
            else:
                base = table = spec.table
        elif spec.query is not None:
            if spec.as_of is not None:
                raise ConnectorError(
                    f"'as_of' applies to tables and files on connection '{self.name}'; write "
                    "VERSION AS OF or TIMESTAMP AS OF inside the query instead."
                )
            kind, label, query = DatasetKind.QUERY, "query", spec.query
            base = f"(\n{spec.query}\n) AS dvf_query"
        else:
            assert spec.path is not None
            kind, label = DatasetKind.PATH, spec.path
            base = _SQL.quote(self._file_view(spec))
        ref = DatasetRef(connection=self.name, kind=kind, name=label, query=query)
        sql = base
        if spec.filter:
            sql = f"(SELECT * FROM {base} WHERE {spec.filter}) AS dvf_filtered"
        return Relation(ref, sql, base, table, spec.filter, self.name)

    def _file_view(self, spec: DatasetSpec) -> str:
        """Name the temporary view of a file dataset; it is registered on first use."""
        payload = json.dumps(
            {"path": spec.path, "options": spec.options, "as_of": spec.as_of},
            sort_keys=True,
            default=str,
        )
        view = f"dvf_path_{hashlib.sha256(payload.encode()).hexdigest()[:12]}"
        with self._shared.lock:
            if view not in self._shared.views:
                self._shared.pending[view] = spec
        return view

    def _ensure_views(self, sql: str) -> None:
        names = set(_VIEW_RE.findall(sql))
        if not names:
            return
        shared = self._shared
        with shared.lock:
            for view in sorted(names):
                spec = shared.pending.get(view)
                if spec is None:
                    continue
                df = self._read(spec)
                try:
                    df.createOrReplaceTempView(view)
                except spark_errors() as exc:
                    raise self._fail(exc) from exc
                shared.views.add(view)
                del shared.pending[view]

    def _read(self, spec: DatasetSpec) -> Any:
        """A DataFrame reading the file dataset ``spec`` with Spark."""
        assert spec.path is not None
        options = dict(spec.options)
        fmt = options.pop("format", None) or _infer_format(spec.path)
        if fmt is None:
            raise ConnectorError(
                f"Cannot infer the file format of '{spec.path}' on connection '{self.name}'; "
                "set 'format' in the dataset's options.",
            )
        fmt = str(fmt).lower()
        source = FORMATS.get(fmt, fmt)
        ddl = options.pop("schema", None)
        if fmt in ("csv", "tsv"):
            options.setdefault("header", True)
            if ddl is None:
                options.setdefault("inferSchema", True)
        if fmt == "tsv":
            options.setdefault("sep", "\t")
        if spec.as_of is not None:
            options.update(self._time_travel(source, spec.as_of))
        reader = self.session.read.format(source)
        if ddl is not None:
            reader = reader.schema(str(ddl))
        for key, value in options.items():
            reader = reader.option(key, value)
        location = _location(spec.path, self.base_dir)
        try:
            return reader.load(location)
        except spark_errors() as exc:
            error = self._fail(exc)
            if "DATA_SOURCE_NOT_FOUND" in str(exc) or "Failed to find" in str(exc):
                package = _PACKAGES.get(source, "the data source's package")
                error = ConnectorError(
                    f"Spark cannot read '{fmt}' files on connection '{self.name}': its data "
                    "source is not installed.",
                    hint=f"Add {package} to the connection's conf as spark.jars.packages "
                    "(or install it on the cluster)"
                    + (
                        ", and configure the Delta or Iceberg extensions and catalog"
                        if source in ("delta", "iceberg")
                        else ""
                    )
                    + ".",
                )
            raise error from exc

    def _time_travel(self, source: str, as_of: str) -> dict[str, Any]:
        value = as_of.strip()
        version = _VERSION_RE.fullmatch(value) is not None
        if source == "delta":
            return {"versionAsOf": int(value)} if version else {"timestampAsOf": value}
        if source == "iceberg":
            if version:
                return {"snapshot-id": int(value)}
            return {"as-of-timestamp": _epoch_millis(value)}
        raise ConnectorError(
            f"'as_of' needs a Delta or Iceberg dataset on connection '{self.name}', "
            f"not '{source}' files."
        )

    def exists(self, relation: Relation) -> bool:
        failures: tuple[type[BaseException], ...] = (ConnectorError, *spark_errors())
        try:
            _ = self.frame(f"SELECT * FROM {relation.base_sql}").schema
        except failures:
            return False
        return True

    def get_schema(self, relation: Relation) -> DatasetSchema:
        try:
            schema = self.frame(f"SELECT * FROM {relation.base_sql}").schema
        except spark_errors() as exc:
            raise self._fail(exc) from exc
        catalog_table = relation.table is not None
        columns = []
        for i, f in enumerate(schema.fields):
            native = f.dataType.simpleString()
            comment = f.metadata.get("comment") if f.metadata else None
            columns.append(
                ColumnSchema(
                    name=f.name,
                    native_type=native,
                    type=parse_native_type(native, "spark"),
                    nullable=bool(f.nullable) if catalog_table else None,
                    ordinal=i + 1,
                    comment=comment or None,
                )
            )
        return DatasetSchema(ref=relation.ref, columns=tuple(columns))

    def list_datasets(self, pattern: str) -> list[str]:
        """Tables and views of the current schema (or of the schema the pattern names)."""
        qualified = "." in pattern
        schema = pattern.rsplit(".", 1)[0] if qualified else None
        try:
            tables = self.session.catalog.listTables(schema)
        except spark_errors() as exc:
            raise self._fail(exc) from exc
        names: list[str] = []
        for table in tables:
            if table.isTemporary and str(table.name).startswith("dvf_"):
                continue  # dvf's own views
            name = f"{schema}.{table.name}" if qualified else str(table.name)
            if fnmatchcase(name.lower(), pattern.lower()):
                names.append(name)
        return sorted(set(names))


def _batch(rows: list[Any], plan: list[_Collected], schema: pa.Schema) -> pa.RecordBatch:
    arrays = []
    for i, column in enumerate(plan):
        if column.micros:
            micros = pa.array([row[i] for row in rows], type=pa.int64())
            arrays.append(micros.cast(column.arrow_type))
        else:
            arrays.append(pa.array([_plain(row[i]) for row in rows], type=column.arrow_type))
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


__all__ = [
    "FORMATS",
    "SparkConnector",
    "child_session",
    "configure_session",
    "engine_session",
    "running_session",
    "start_session",
]
