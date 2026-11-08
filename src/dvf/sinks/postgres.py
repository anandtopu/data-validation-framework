"""PostgreSQL result sink: run and check results as tables in a PostgreSQL database.

Connects with psycopg 3 using ``url`` (a secret). ``schema`` places the tables in that schema,
created when missing. Extra ``options`` are passed to ``psycopg.connect`` (for example
``sslmode`` or ``application_name``). Every value is sent as a query parameter.

Needs the ``postgres`` extra (``psycopg``).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import unquote, urlsplit

from dvf.core.errors import ConfigError, DependencyError
from dvf.security.redaction import MASK
from dvf.sinks._schema import ColumnType, SinkTable
from dvf.sinks.sql import Session, SqlResultSink, quote

if TYPE_CHECKING:
    from dvf.config.models import SinkConfig

_DRIVER_SUFFIX = re.compile(r"^(postgres(?:ql)?)\+[a-z0-9_]+://", re.IGNORECASE)


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise DependencyError("psycopg", "postgres", "The PostgreSQL result sink") from exc
    return psycopg


class PostgresSession:
    """A :class:`~dvf.sinks.sql.Session` over a psycopg connection (not in autocommit mode)."""

    def __init__(self, con: Any) -> None:
        self.con = con

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.con.execute(sql, tuple(params) or None)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return list(self.con.execute(sql, tuple(params) or None).fetchall())

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self.con.cursor() as cursor:
            cursor.executemany(sql, [tuple(row) for row in rows])

    def begin(self) -> None:
        """psycopg opens a transaction with the first statement."""

    def commit(self) -> None:
        self.con.commit()

    def rollback(self) -> None:
        self.con.rollback()

    def close(self) -> None:
        self.con.close()


class PostgresSink(SqlResultSink):
    """Write run and check results to tables in PostgreSQL (``url``, optional ``schema``)."""

    label: ClassVar[str] = "PostgreSQL"
    placeholder: ClassVar[str] = "%s"
    type_names: ClassVar[dict[ColumnType, str]] = {
        ColumnType.TEXT: "TEXT",
        ColumnType.BIGINT: "BIGINT",
        ColumnType.DOUBLE: "DOUBLE PRECISION",
        ColumnType.BOOLEAN: "BOOLEAN",
        ColumnType.TIMESTAMP: "TIMESTAMPTZ",
    }
    driver_option_names: ClassVar[frozenset[str] | None] = None

    def __init__(self, config: SinkConfig, base_dir: Path) -> None:
        super().__init__(config, base_dir)
        if config.url is None or not config.url.get_secret_value().strip():
            raise ConfigError(
                "The PostgreSQL result sink needs 'url', for example "
                "'${secret:env:DVF_RESULTS_URL}' holding postgresql://user:password@host/db."
            )
        self._psycopg: Any = None

    def _module(self) -> Any:
        if self._psycopg is None:
            self._psycopg = _import_psycopg()
        return self._psycopg

    def driver_errors(self) -> tuple[type[BaseException], ...]:
        return (self._module().Error,)

    def connect(self) -> Session:
        assert self.config.url is not None
        conninfo = _DRIVER_SUFFIX.sub(r"\1://", self.config.url.get_secret_value().strip())
        options = {"connect_timeout": 15, **self.driver_options}
        con = self._module().connect(conninfo, autocommit=False, **options)
        return PostgresSession(con)

    def _connect_hint(self) -> str:
        return "Check the sink 'url' (host, port, database, credentials) and network access."

    def redact(self, text: str) -> str:
        url = self.config.url.get_secret_value().strip() if self.config.url else ""
        secrets = {url, _DRIVER_SUFFIX.sub(r"\1://", url)}
        with contextlib.suppress(ValueError):
            password = urlsplit(url).password
            if password:
                secrets.update({password, unquote(password)})
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            text = text.replace(secret, MASK)
        return super().redact(text)

    def ensure_schema(self, session: Session) -> None:
        """Create the schema only when it is missing (creating needs a privilege that writing
        into an existing schema does not)."""
        if not self.schema:
            return
        found = session.query("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self.schema,))
        if not found:
            session.execute(f"CREATE SCHEMA IF NOT EXISTS {quote(self.schema)}")

    def existing_columns(self, session: Session, table: SinkTable) -> set[str]:
        rows = session.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = COALESCE(CAST(%s AS TEXT), current_schema()) "
            "AND table_name = %s",
            (self.schema, self.prefix + table.name),
        )
        return {str(row[0]) for row in rows}
