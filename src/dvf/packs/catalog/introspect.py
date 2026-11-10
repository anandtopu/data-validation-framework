"""Catalog metadata for schema checks SCH-07 (physical design), SCH-09 (collations), and SCH-10
(object inventory), for DuckDB, SQLite, PostgreSQL, MySQL, SQL Server, and Oracle.

Every function runs read-only catalog queries through :meth:`Connector.query`, so it works with
the native connectors and with the generic ``sqlalchemy`` and ``odbc`` connectors of the same
dialect. Names are passed as SQL literals (quoted by the dialect), never interpolated raw. A
function returns ``None`` when the information is not available (a query or file dataset, or a
dialect without catalog support here).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from dvf.plugins.base import (
    CollationInfo,
    ColumnCollation,
    Connector,
    IndexInfo,
    ObjectInventory,
    PhysicalDesign,
    Relation,
)

log = logging.getLogger(__name__)

SUPPORTED_DIALECTS = ("duckdb", "sqlite", "postgres", "mysql", "mssql", "oracle")
"""Dialects whose catalogs this module reads."""

_SQLGLOT = {"mssql": "tsql"}

Rows = list[dict[str, Any]]


# ====================================================================== helpers


@dataclass(frozen=True)
class TableName:
    """A table name split into catalog parts: ``schema`` is None when not qualified."""

    schema: str | None
    name: str
    qualified: str
    """The name as written in the configuration."""


def split_table(table: str, dialect: str) -> TableName:
    """Split ``schema.table`` the way the dialect stores names in its catalog: unquoted names are
    lower case in PostgreSQL and upper case in Oracle, quoted names are kept."""
    parsed = exp.to_table(table, dialect=_SQLGLOT.get(dialect, dialect))

    def stored(identifier: exp.Expression | None) -> str | None:
        if not isinstance(identifier, exp.Identifier) or not identifier.name:
            return None
        if identifier.quoted:
            return identifier.name
        if dialect == "postgres":
            return identifier.name.lower()
        if dialect == "oracle":
            return identifier.name.upper()
        return identifier.name

    name = stored(parsed.this) or parsed.name
    return TableName(stored(parsed.args.get("db")), name, table)


def _run(conn: Connector, sql: str) -> Rows:
    """Rows as dictionaries with lower-case keys (Oracle upper-cases unquoted aliases)."""
    table = conn.query(sql)
    return [{str(k).lower(): v for k, v in row.items()} for row in table.to_pylist()]


def _lit(conn: Connector, value: Any) -> str:
    return conn.sql_dialect.literal(value)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "t", "true", "y", "yes", "unique")
    return bool(value)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _group_indexes(
    rows: Rows, *, clustered: Callable[[dict[str, Any]], bool] | None = None
) -> tuple[tuple[IndexInfo, ...], tuple[str, ...]]:
    """Rows with ``index_name``, ``position``, ``column_name``, ``is_unique``, ``is_primary``,
    ``method`` and ``predicate`` grouped into indexes (and the clustered index's columns)."""
    order: list[str] = []
    found: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (str(r["index_name"]), int(r.get("position") or 0))):
        name = str(row["index_name"])
        entry = found.get(name)
        if entry is None:
            entry = found[name] = {"columns": [], "row": row}
            order.append(name)
        column = row.get("column_name")
        if column is not None:
            entry["columns"].append(str(column))
    indexes: list[IndexInfo] = []
    clustering: tuple[str, ...] = ()
    for name in order:
        entry = found[name]
        row = entry["row"]
        info = IndexInfo(
            name=name,
            columns=tuple(entry["columns"]),
            unique=_truthy(row.get("is_unique")),
            primary=_truthy(row.get("is_primary")),
            kind=_text(row.get("method")),
            predicate=_text(row.get("predicate")),
        )
        indexes.append(info)
        if clustered is not None and clustered(row) and not clustering:
            clustering = info.columns
    return tuple(indexes), clustering


def _inventory(schema: str | None, rows: Rows, unsupported: tuple[str, ...]) -> ObjectInventory:
    objects: dict[str, set[str]] = {}
    for row in rows:
        kind, name = row.get("object_type"), row.get("object_name")
        if kind is None or name is None:
            continue
        objects.setdefault(str(kind), set()).add(str(name))
    return ObjectInventory(
        schema=schema,
        objects={k: tuple(sorted(v)) for k, v in sorted(objects.items())},
        unsupported=unsupported,
    )


def _semantics_from_name(name: str | None) -> tuple[bool | None, bool | None]:
    """(case_sensitive, accent_sensitive) from collation name conventions shared by SQL Server,
    MySQL, and Oracle (``_CI``/``_CS``, ``_AI``/``_AS``, ``_BIN``)."""
    if not name:
        return None, None
    upper = name.upper()
    tokens = set(re.split(r"[_\-\s.]+", upper))
    if tokens & {"BIN", "BIN2", "BINARY"} and not tokens & {"CI", "AI"}:
        return True, True
    case: bool | None = None
    accent: bool | None = None
    if "CI" in tokens:
        case = False
    elif "CS" in tokens:
        case = True
    if "AI" in tokens:
        accent, case = False, False if case is None else case
    elif "AS" in tokens:
        accent = True
    return case, accent


# ====================================================================== dispatch


def physical_design(conn: Connector, relation: Relation) -> PhysicalDesign | None:
    """SCH-07 metadata of a catalog table, or None when not available."""
    if relation.table is None or conn.dialect not in SUPPORTED_DIALECTS:
        return None
    name = split_table(relation.table, conn.dialect)
    return _PHYSICAL[conn.dialect](conn, name)


def collations(conn: Connector, relation: Relation) -> CollationInfo | None:
    """SCH-09 metadata of a catalog table, or None when not available."""
    if relation.table is None or conn.dialect not in SUPPORTED_DIALECTS:
        return None
    name = split_table(relation.table, conn.dialect)
    return _COLLATIONS[conn.dialect](conn, name)


def object_inventory(conn: Connector, relation: Relation) -> ObjectInventory | None:
    """SCH-10 metadata: objects in the schema of the table (or the session's default schema)."""
    if conn.dialect not in SUPPORTED_DIALECTS:
        return None
    name = split_table(relation.table, conn.dialect) if relation.table is not None else None
    return _INVENTORY[conn.dialect](conn, name.schema if name else None)


# ====================================================================== DuckDB


def _duck_schema(conn: Connector, schema: str | None) -> str:
    return _lit(conn, schema) if schema else "current_schema()"


def _duck_table(conn: Connector, name: TableName) -> str:
    return (
        f"lower(schema_name) = lower({_duck_schema(conn, name.schema)}) "
        f"AND lower(table_name) = lower({_lit(conn, name.name)}) "
        "AND database_name = current_database()"
    )


def _duckdb_physical(conn: Connector, name: TableName) -> PhysicalDesign:
    where = _duck_table(conn, name)
    indexes: list[IndexInfo] = []
    for row in _run(
        conn,
        "SELECT constraint_type, constraint_name, constraint_column_names "
        f"FROM duckdb_constraints() WHERE {where} "
        "AND constraint_type IN ('PRIMARY KEY', 'UNIQUE') ORDER BY constraint_index",
    ):
        primary = row["constraint_type"] == "PRIMARY KEY"
        indexes.append(
            IndexInfo(
                name=str(row["constraint_name"]),
                columns=tuple(str(c) for c in row["constraint_column_names"] or ()),
                unique=True,
                primary=primary,
                kind="ART",
            )
        )
    for row in _run(
        conn,
        f"SELECT index_name, is_unique, is_primary, expressions FROM duckdb_indexes() WHERE {where} "
        "ORDER BY index_name",
    ):
        text = str(row.get("expressions") or "").strip()
        columns = tuple(c.strip().strip('"') for c in text.strip("[]").split(",") if c.strip())
        indexes.append(
            IndexInfo(
                name=str(row["index_name"]),
                columns=columns,
                unique=_truthy(row.get("is_unique")),
                primary=_truthy(row.get("is_primary")),
                kind="ART",
            )
        )
    return PhysicalDesign(
        indexes=tuple(indexes),
        notes=("DuckDB has no partitioning or clustering metadata for tables",),
    )


def _sql_collations(sql: str | None, dialect: str) -> dict[str, str]:
    """Column collations declared in a CREATE TABLE statement (DuckDB, SQLite)."""
    if not sql:
        return {}
    import sqlglot

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        log.debug("could not parse table DDL for collations", exc_info=True)
        return {}
    found: dict[str, str] = {}
    for column in tree.find_all(exp.ColumnDef):
        for constraint in column.constraints:
            if isinstance(constraint.kind, exp.CollateColumnConstraint):
                value = constraint.kind.this
                found[column.name] = value.name if isinstance(value, exp.Expression) else str(value)
    return found


_LITE_SEMANTICS = {
    "BINARY": (True, True),
    "NOCASE": (False, True),
    "RTRIM": (True, True),
}


def _duckdb_collations(conn: Connector, name: TableName) -> CollationInfo:
    default = _run(conn, "SELECT current_setting('default_collation') AS c")
    default_name = str(default[0]["c"] or "") if default else ""
    rows = _run(conn, f"SELECT sql FROM duckdb_tables() WHERE {_duck_table(conn, name)}")
    declared = _sql_collations(rows[0]["sql"] if rows else None, "duckdb")
    columns: dict[str, ColumnCollation] = {}
    for column, collation in declared.items():
        parts = {p.strip().upper() for p in collation.split(".")}
        columns[column] = ColumnCollation(
            collation=collation,
            charset="UTF-8",
            case_sensitive=not parts & {"NOCASE"},
            accent_sensitive=not parts & {"NOACCENT"},
            unicode=True,
        )
    default_parts = {p.strip().upper() for p in default_name.split(".") if p.strip()}
    return CollationInfo(
        default_collation=default_name or "binary",
        charset="UTF-8",
        columns=columns,
        case_sensitive_default=not default_parts & {"NOCASE"},
        accent_sensitive_default=not default_parts & {"NOACCENT"},
    )


def _duckdb_inventory(conn: Connector, schema: str | None) -> ObjectInventory:
    s = _duck_schema(conn, schema)
    where = f"lower(schema_name) = lower({s}) AND database_name = current_database()"
    rows = _run(
        conn,
        f"SELECT 'view' AS object_type, view_name AS object_name FROM duckdb_views() "
        f"WHERE NOT internal AND {where} "
        "UNION ALL SELECT 'function', function_name FROM duckdb_functions() "
        f"WHERE NOT internal AND function_type IN ('macro', 'table_macro') AND {where} "
        f"UNION ALL SELECT 'sequence', sequence_name FROM duckdb_sequences() WHERE {where}",
    )
    resolved = schema or _scalar(conn, "SELECT current_schema() AS s")
    return _inventory(resolved, rows, ("procedure", "trigger", "synonym"))


def _scalar(conn: Connector, sql: str) -> str | None:
    rows = _run(conn, sql)
    if not rows:
        return None
    value = next(iter(rows[0].values()))
    return None if value is None else str(value)


# ====================================================================== SQLite


def _lite_prefix(conn: Connector, schema: str | None) -> str:
    return f"{conn.sql_dialect.quote(schema)}." if schema else ""


def _sqlite_physical(conn: Connector, name: TableName) -> PhysicalDesign:
    table = _lit(conn, name.name)
    schema_arg = f", {_lit(conn, name.schema)}" if name.schema else ""
    master = f"{_lite_prefix(conn, name.schema)}sqlite_master"
    indexes: list[IndexInfo] = []
    listed = _run(conn, f"SELECT * FROM pragma_index_list({table}{schema_arg}) ORDER BY name")
    ddl = {
        str(r["name"]): str(r["sql"] or "")
        for r in _run(conn, f"SELECT name, sql FROM {master} WHERE type = 'index'")
    }
    for row in listed:
        index = str(row["name"])
        columns = _run(
            conn,
            f"SELECT name FROM pragma_index_info({_lit(conn, index)}{schema_arg}) ORDER BY seqno",
        )
        predicate = None
        if _truthy(row.get("partial")):
            match = re.search(r"\bWHERE\b(.*)$", ddl.get(index, ""), re.IGNORECASE | re.DOTALL)
            predicate = match.group(1).strip() if match else None
        indexes.append(
            IndexInfo(
                name=index,
                columns=tuple(str(c["name"]) for c in columns if c["name"] is not None),
                unique=_truthy(row.get("unique")),
                primary=str(row.get("origin")) == "pk",
                kind="btree",
                predicate=predicate,
            )
        )
    info = _run(conn, f"SELECT name, pk FROM pragma_table_info({table}{schema_arg}) ORDER BY pk")
    pk = tuple(str(r["name"]) for r in info if int(r.get("pk") or 0) > 0)
    notes = ["SQLite has no partitioning; tables are clustered by rowid"]
    if pk and not any(i.primary for i in indexes):
        indexes.insert(
            0, IndexInfo(name="rowid", columns=pk, unique=True, primary=True, kind="rowid")
        )
    without_rowid = _run(
        conn,
        f"SELECT sql FROM {master} WHERE type = 'table' AND lower(name) = lower({table})",
    )
    clustering: tuple[str, ...] = ()
    if without_rowid and "WITHOUT ROWID" in str(without_rowid[0]["sql"] or "").upper():
        clustering = pk
        notes = [
            "SQLite has no partitioning; WITHOUT ROWID tables are clustered by the primary key"
        ]
    return PhysicalDesign(indexes=tuple(indexes), clustering=clustering, notes=tuple(notes))


def _sqlite_collations(conn: Connector, name: TableName) -> CollationInfo:
    master = f"{_lite_prefix(conn, name.schema)}sqlite_master"
    rows = _run(
        conn,
        f"SELECT sql FROM {master} WHERE type = 'table' AND lower(name) = lower({_lit(conn, name.name)})",
    )
    declared = _sql_collations(rows[0]["sql"] if rows else None, "sqlite")
    encoding = _scalar(conn, "SELECT encoding FROM pragma_encoding") or "UTF-8"
    columns = {}
    for column, collation in declared.items():
        case, accent = _LITE_SEMANTICS.get(collation.upper(), (None, None))
        columns[column] = ColumnCollation(
            collation=collation.upper(),
            charset=encoding,
            case_sensitive=case,
            accent_sensitive=accent,
            unicode=True,
        )
    return CollationInfo(
        default_collation="BINARY",
        charset=encoding,
        columns=columns,
        case_sensitive_default=True,
        accent_sensitive_default=True,
    )


def _sqlite_inventory(conn: Connector, schema: str | None) -> ObjectInventory:
    master = f"{_lite_prefix(conn, schema)}sqlite_master"
    rows = _run(
        conn,
        f"SELECT type AS object_type, name AS object_name FROM {master} "
        "WHERE type IN ('view', 'trigger')",
    )
    return _inventory(schema or "main", rows, ("procedure", "function", "sequence", "synonym"))


# ====================================================================== PostgreSQL


def _pg_oid(conn: Connector, name: TableName) -> str:
    return f"to_regclass({_lit(conn, name.qualified)})"


def _postgres_physical(conn: Connector, name: TableName) -> PhysicalDesign:
    oid = _pg_oid(conn, name)
    rows = _run(
        conn,
        "SELECT ic.relname AS index_name, k.n AS position, "
        "pg_get_indexdef(i.indexrelid, k.n, true) AS column_name, "
        "i.indisunique AS is_unique, i.indisprimary AS is_primary, "
        "i.indisclustered AS is_clustered, am.amname AS method, "
        "pg_get_expr(i.indpred, i.indrelid) AS predicate "
        "FROM pg_index i "
        "JOIN pg_class ic ON ic.oid = i.indexrelid "
        "JOIN pg_am am ON am.oid = ic.relam "
        "CROSS JOIN LATERAL generate_series(1, i.indnkeyatts) AS k(n) "
        f"WHERE i.indrelid = {oid} ORDER BY ic.relname, k.n",
    )
    indexes, clustering = _group_indexes(rows, clustered=lambda r: _truthy(r.get("is_clustered")))
    part = _run(
        conn,
        f"SELECT pg_get_partkeydef({oid}) AS partitioning, "
        f"(SELECT COUNT(*) FROM pg_inherits WHERE inhparent = {oid}) AS partitions, "
        f"(SELECT relispartition FROM pg_class WHERE oid = {oid}) AS is_partition",
    )
    partitioning = None
    notes: list[str] = []
    if part:
        row = part[0]
        if row.get("partitioning"):
            partitioning = f"{row['partitioning']} ({int(row.get('partitions') or 0)} partitions)"
        if _truthy(row.get("is_partition")):
            notes.append("the table is itself a partition of another table")
    if clustering:
        notes.append("CLUSTER ordering is applied once, not maintained by PostgreSQL")
    return PhysicalDesign(
        indexes=indexes, partitioning=partitioning, clustering=clustering, notes=tuple(notes)
    )


def _pg_icu_semantics(locale: str | None, deterministic: bool) -> tuple[bool | None, bool | None]:
    if deterministic:
        return True, True
    text = (locale or "").lower()
    if "ks-level1" in text:
        return False, False
    if "ks-level2" in text:
        return False, True
    if "kc-true" in text:
        return None, True
    return None, None


def _postgres_collations(conn: Connector, name: TableName) -> CollationInfo:
    oid = _pg_oid(conn, name)
    database = _run(
        conn,
        "SELECT datcollate AS collation, pg_encoding_to_char(encoding) AS encoding "
        "FROM pg_database WHERE datname = current_database()",
    )
    default_name = str(database[0]["collation"]) if database else None
    charset = str(database[0]["encoding"]) if database else None
    unicode = charset is not None and charset.upper() in ("UTF8", "UTF-8")
    rows = _run(
        conn,
        "SELECT a.attname AS column_name, co.collname AS collation, "
        "co.collisdeterministic AS deterministic, co.collprovider AS provider, "
        "co.collcollate AS locale "
        "FROM pg_attribute a JOIN pg_collation co ON co.oid = a.attcollation "
        f"WHERE a.attrelid = {oid} AND a.attnum > 0 AND NOT a.attisdropped",
    )
    columns: dict[str, ColumnCollation] = {}
    for row in rows:
        collation = str(row["collation"])
        if collation == "default":
            continue
        deterministic = _truthy(row.get("deterministic"))
        case, accent = _pg_icu_semantics(_text(row.get("locale")) or collation, deterministic)
        label = collation if deterministic else f"{collation} (nondeterministic)"
        columns[str(row["column_name"])] = ColumnCollation(
            collation=label,
            charset=charset,
            case_sensitive=case,
            accent_sensitive=accent,
            unicode=unicode,
        )
    return CollationInfo(
        default_collation=default_name,
        charset=charset,
        columns=columns,
        case_sensitive_default=True,
        accent_sensitive_default=True,
        unicode_default=unicode,
    )


def _postgres_inventory(conn: Connector, schema: str | None) -> ObjectInventory:
    s = _lit(conn, schema) if schema else "current_schema()"
    owned = (
        "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass "
        "AND d.objid = c.oid AND d.deptype IN ('a', 'i', 'e'))"
    )
    rows = _run(
        conn,
        "SELECT 'view' AS object_type, c.relname AS object_name FROM pg_class c "
        f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = {s} "
        "AND c.relkind IN ('v', 'm') "
        "UNION ALL SELECT CASE WHEN p.prokind = 'p' THEN 'procedure' ELSE 'function' END, "
        "p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        f"WHERE n.nspname = {s} AND NOT EXISTS (SELECT 1 FROM pg_depend d "
        "WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e') "
        "UNION ALL SELECT 'trigger', t.tgname FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {s} AND NOT t.tgisinternal "
        "UNION ALL SELECT 'sequence', c.relname FROM pg_class c "
        f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = {s} "
        f"AND c.relkind = 'S' AND {owned}",
    )
    resolved = schema or _scalar(conn, "SELECT current_schema() AS s")
    return _inventory(resolved, rows, ("synonym",))


# ====================================================================== MySQL


def _my_where(conn: Connector, name: TableName, schema_col: str = "TABLE_SCHEMA") -> str:
    schema = _lit(conn, name.schema) if name.schema else "DATABASE()"
    return f"{schema_col} = {schema} AND LOWER(TABLE_NAME) = LOWER({_lit(conn, name.name)})"


def _mysql_physical(conn: Connector, name: TableName) -> PhysicalDesign:
    where = _my_where(conn, name)
    rows = _run(
        conn,
        "SELECT INDEX_NAME AS index_name, SEQ_IN_INDEX AS position, "
        "COALESCE(COLUMN_NAME, EXPRESSION) AS column_name, "
        "CASE WHEN NON_UNIQUE = 0 THEN 1 ELSE 0 END AS is_unique, "
        "CASE WHEN INDEX_NAME = 'PRIMARY' THEN 1 ELSE 0 END AS is_primary, "
        "INDEX_TYPE AS method FROM information_schema.STATISTICS "
        f"WHERE {where} ORDER BY INDEX_NAME, SEQ_IN_INDEX",
    )
    indexes, _ = _group_indexes(rows)
    parts = _run(
        conn,
        "SELECT PARTITION_METHOD AS method, PARTITION_EXPRESSION AS expression, "
        "SUBPARTITION_METHOD AS sub_method, SUBPARTITION_EXPRESSION AS sub_expression, "
        "COUNT(*) AS partitions FROM information_schema.PARTITIONS "
        f"WHERE {where} AND PARTITION_NAME IS NOT NULL "
        "GROUP BY PARTITION_METHOD, PARTITION_EXPRESSION, SUBPARTITION_METHOD, "
        "SUBPARTITION_EXPRESSION",
    )
    partitioning = None
    if parts:
        row = parts[0]
        partitioning = f"{row['method']} ({row['expression']})"
        if row.get("sub_method"):
            partitioning += f" SUBPARTITION BY {row['sub_method']} ({row['sub_expression']})"
        partitioning += f" ({int(row.get('partitions') or 0)} partitions)"
    engine = _run(conn, f"SELECT ENGINE AS engine FROM information_schema.TABLES WHERE {where}")
    engine_name = str(engine[0]["engine"]) if engine and engine[0].get("engine") else None
    clustering: tuple[str, ...] = ()
    notes: list[str] = []
    if engine_name:
        notes.append(f"storage engine {engine_name}")
    if engine_name and engine_name.upper() == "INNODB":
        primary = next((i for i in indexes if i.primary), None)
        if primary is not None:
            clustering = primary.columns
            notes.append("InnoDB clusters rows by the primary key")
    return PhysicalDesign(
        indexes=indexes, partitioning=partitioning, clustering=clustering, notes=tuple(notes)
    )


_UNICODE_CHARSETS = frozenset(
    {"UTF8", "UTF8MB3", "UTF8MB4", "UCS2", "UTF16", "UTF16LE", "UTF32", "AL32UTF8", "AL16UTF16"}
)


def _mysql_semantics(collation: str | None) -> tuple[bool | None, bool | None]:
    case, accent = _semantics_from_name(collation)
    if case is False and accent is None:
        accent = False  # MySQL _ci collations without _as are accent-insensitive
    return case, accent


def _mysql_collations(conn: Connector, name: TableName) -> CollationInfo:
    where = _my_where(conn, name)
    table = _run(
        conn,
        "SELECT t.TABLE_COLLATION AS collation, c.CHARACTER_SET_NAME AS charset "
        "FROM information_schema.TABLES t LEFT JOIN information_schema.COLLATIONS c "
        f"ON c.COLLATION_NAME = t.TABLE_COLLATION WHERE {_my_where(conn, name, 't.TABLE_SCHEMA')}",
    )
    default_name = _text(table[0]["collation"]) if table else None
    default_charset = _text(table[0]["charset"]) if table else None
    rows = _run(
        conn,
        "SELECT COLUMN_NAME AS column_name, COLLATION_NAME AS collation, "
        "CHARACTER_SET_NAME AS charset FROM information_schema.COLUMNS "
        f"WHERE {where} AND COLLATION_NAME IS NOT NULL",
    )
    columns: dict[str, ColumnCollation] = {}
    for row in rows:
        collation = _text(row.get("collation"))
        charset = _text(row.get("charset"))
        case, accent = _mysql_semantics(collation)
        columns[str(row["column_name"])] = ColumnCollation(
            collation=collation,
            charset=charset,
            case_sensitive=case,
            accent_sensitive=accent,
            unicode=None if charset is None else charset.upper() in _UNICODE_CHARSETS,
        )
    case, accent = _mysql_semantics(default_name)
    return CollationInfo(
        default_collation=default_name,
        charset=default_charset,
        columns=columns,
        case_sensitive_default=case,
        accent_sensitive_default=accent,
        unicode_default=None
        if default_charset is None
        else default_charset.upper() in _UNICODE_CHARSETS,
    )


def _mysql_inventory(conn: Connector, schema: str | None) -> ObjectInventory:
    s = _lit(conn, schema) if schema else "DATABASE()"
    rows = _run(
        conn,
        "SELECT 'view' AS object_type, TABLE_NAME AS object_name FROM information_schema.VIEWS "
        f"WHERE TABLE_SCHEMA = {s} "
        "UNION ALL SELECT LOWER(ROUTINE_TYPE), ROUTINE_NAME FROM information_schema.ROUTINES "
        f"WHERE ROUTINE_SCHEMA = {s} "
        "UNION ALL SELECT 'trigger', TRIGGER_NAME FROM information_schema.TRIGGERS "
        f"WHERE TRIGGER_SCHEMA = {s}",
    )
    resolved = schema or _scalar(conn, "SELECT DATABASE() AS s")
    return _inventory(resolved, rows, ("sequence", "synonym"))


# ====================================================================== SQL Server


def _ms_object(conn: Connector, name: TableName) -> str:
    return f"OBJECT_ID({_lit(conn, name.qualified)})"


def _mssql_physical(conn: Connector, name: TableName) -> PhysicalDesign:
    oid = _ms_object(conn, name)
    rows = _run(
        conn,
        "SELECT i.name AS index_name, "
        "CASE WHEN ic.key_ordinal > 0 THEN ic.key_ordinal ELSE 1000 + ic.index_column_id END "
        "AS position, c.name AS column_name, i.is_unique AS is_unique, "
        "i.is_primary_key AS is_primary, i.type_desc AS method, "
        "i.filter_definition AS predicate, CASE WHEN i.type IN (1, 5) THEN 1 ELSE 0 END "
        "AS is_clustered "
        "FROM sys.indexes i "
        "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        f"WHERE i.object_id = {oid} AND i.type > 0 "
        "AND (ic.key_ordinal > 0 OR i.type IN (5, 6)) "
        "ORDER BY i.name, position",
    )
    indexes, clustering = _group_indexes(rows, clustered=lambda r: _truthy(r.get("is_clustered")))
    parts = _run(
        conn,
        "SELECT ps.name AS scheme, pf.name AS function_name, pf.type_desc AS kind, "
        "c.name AS column_name, (SELECT COUNT(*) FROM sys.partitions p "
        "WHERE p.object_id = i.object_id AND p.index_id = i.index_id) AS partitions "
        "FROM sys.indexes i "
        "JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id "
        "JOIN sys.partition_functions pf ON pf.function_id = ps.function_id "
        "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "AND ic.partition_ordinal > 0 "
        "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        f"WHERE i.object_id = {oid} AND i.index_id IN (0, 1)",
    )
    partitioning = None
    if parts:
        row = parts[0]
        partitioning = (
            f"{row['kind']} ({row['column_name']}) on scheme {row['scheme']} "
            f"({int(row.get('partitions') or 0)} partitions)"
        )
    notes = () if clustering else ("the table is a heap (no clustered index)",)
    return PhysicalDesign(
        indexes=indexes, partitioning=partitioning, clustering=clustering, notes=notes
    )


def _mssql_semantics(collation: str | None) -> tuple[bool | None, bool | None]:
    return _semantics_from_name(collation)


def _mssql_collations(conn: Connector, name: TableName) -> CollationInfo:
    oid = _ms_object(conn, name)
    default_name = _scalar(
        conn, "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS NVARCHAR(128)) AS c"
    )
    rows = _run(
        conn,
        "SELECT c.name AS column_name, c.collation_name AS collation, t.name AS type_name, "
        "CAST(COLLATIONPROPERTY(c.collation_name, 'CodePage') AS INT) AS code_page "
        "FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id "
        f"WHERE c.object_id = {oid} AND c.collation_name IS NOT NULL",
    )
    columns: dict[str, ColumnCollation] = {}
    for row in rows:
        collation = _text(row.get("collation"))
        type_name = str(row.get("type_name") or "").lower()
        utf8 = collation is not None and "UTF8" in collation.upper()
        if type_name in ("nchar", "nvarchar", "ntext", "sysname"):
            charset, unicode = "UTF-16", True
        elif utf8:
            charset, unicode = "UTF-8", True
        else:
            charset, unicode = f"code page {row.get('code_page')}", False
        case, accent = _mssql_semantics(collation)
        columns[str(row["column_name"])] = ColumnCollation(
            collation=collation,
            charset=charset,
            case_sensitive=case,
            accent_sensitive=accent,
            unicode=unicode,
        )
    case, accent = _mssql_semantics(default_name)
    return CollationInfo(
        default_collation=default_name,
        charset=None,
        columns=columns,
        case_sensitive_default=case,
        accent_sensitive_default=accent,
    )


_MSSQL_TYPES = {
    "V": "view",
    "P": "procedure",
    "PC": "procedure",
    "FN": "function",
    "IF": "function",
    "TF": "function",
    "FS": "function",
    "FT": "function",
    "TR": "trigger",
    "SO": "sequence",
    "SN": "synonym",
}


def _mssql_inventory(conn: Connector, schema: str | None) -> ObjectInventory:
    s = f"SCHEMA_ID({_lit(conn, schema)})" if schema else "SCHEMA_ID()"
    codes = ", ".join(_lit(conn, code) for code in _MSSQL_TYPES)
    rows = _run(
        conn,
        "SELECT RTRIM(o.type) AS object_type, o.name AS object_name FROM sys.objects o "
        f"WHERE o.schema_id = {s} AND o.type IN ({codes}) AND o.is_ms_shipped = 0",
    )
    mapped = [
        {
            "object_type": _MSSQL_TYPES.get(str(r["object_type"]).strip()),
            "object_name": r["object_name"],
        }
        for r in rows
    ]
    resolved = schema or _scalar(conn, "SELECT SCHEMA_NAME() AS s")
    return _inventory(resolved, mapped, ())


# ====================================================================== Oracle


def _ora_owner(conn: Connector, schema: str | None) -> str:
    return _lit(conn, schema) if schema else "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"


def _oracle_physical(conn: Connector, name: TableName) -> PhysicalDesign:
    owner, table = _ora_owner(conn, name.schema), _lit(conn, name.name)
    rows = _run(
        conn,
        "SELECT i.index_name AS index_name, c.column_position AS position, "
        "c.column_name AS column_name, "
        "CASE WHEN i.uniqueness = 'UNIQUE' THEN 1 ELSE 0 END AS is_unique, "
        "CASE WHEN k.constraint_name IS NULL THEN 0 ELSE 1 END AS is_primary, "
        "i.index_type AS method "
        "FROM all_indexes i "
        "JOIN all_ind_columns c ON c.index_owner = i.owner AND c.index_name = i.index_name "
        "LEFT JOIN all_constraints k ON k.owner = i.table_owner AND k.table_name = i.table_name "
        "AND k.constraint_type = 'P' AND k.index_name = i.index_name "
        f"WHERE i.table_owner = {owner} AND i.table_name = {table} "
        "ORDER BY i.index_name, c.column_position",
    )
    indexes, _ = _group_indexes(rows)
    notes: list[str] = []
    if any(c.startswith("SYS_NC") for i in indexes for c in i.columns):
        notes.append("function-based index expressions are shown as their hidden column names")
    part = _run(
        conn,
        "SELECT partitioning_type AS kind, subpartitioning_type AS sub_kind, "
        "partition_count AS partitions FROM all_part_tables "
        f"WHERE owner = {owner} AND table_name = {table}",
    )
    partitioning = None
    if part:
        keys = _run(
            conn,
            "SELECT column_name FROM all_part_key_columns "
            f"WHERE owner = {owner} AND name = {table} AND object_type = 'TABLE' "
            "ORDER BY column_position",
        )
        row = part[0]
        partitioning = f"{row['kind']} ({', '.join(str(k['column_name']) for k in keys)})"
        if row.get("sub_kind") and str(row["sub_kind"]) != "NONE":
            partitioning += f" SUBPARTITION BY {row['sub_kind']}"
        partitioning += f" ({int(row.get('partitions') or 0)} partitions)"
    tables = _run(
        conn,
        "SELECT cluster_name, iot_type FROM all_tables "
        f"WHERE owner = {owner} AND table_name = {table}",
    )
    clustering: tuple[str, ...] = ()
    if tables:
        if tables[0].get("iot_type"):
            primary = next((i for i in indexes if i.primary), None)
            clustering = primary.columns if primary else ()
            notes.append("index-organized table (rows stored in primary key order)")
        if tables[0].get("cluster_name"):
            notes.append(f"stored in cluster {tables[0]['cluster_name']}")
    return PhysicalDesign(
        indexes=indexes, partitioning=partitioning, clustering=clustering, notes=tuple(notes)
    )


def _oracle_semantics(collation: str | None) -> tuple[bool | None, bool | None]:
    if collation is None or collation.upper() in ("USING_NLS_COMP", "BINARY", "USING_NLS_SORT"):
        return True, True
    upper = collation.upper()
    if upper.endswith("_AI"):
        return False, False
    if upper.endswith("_CI"):
        return False, True
    return True, True


def _oracle_collations(conn: Connector, name: TableName) -> CollationInfo:
    owner, table = _ora_owner(conn, name.schema), _lit(conn, name.name)
    params = {
        str(r["parameter"]): _text(r["value"])
        for r in _run(
            conn,
            "SELECT parameter, value FROM nls_database_parameters "
            "WHERE parameter IN ('NLS_CHARACTERSET', 'NLS_NCHAR_CHARACTERSET', 'NLS_COMP', "
            "'NLS_SORT')",
        )
    }
    charset = params.get("NLS_CHARACTERSET")
    ncharset = params.get("NLS_NCHAR_CHARACTERSET")
    base = (
        "SELECT column_name, data_type, {collation} AS collation FROM all_tab_columns "
        f"WHERE owner = {owner} AND table_name = {table} "
        "AND data_type IN ('CHAR', 'VARCHAR2', 'NCHAR', 'NVARCHAR2', 'CLOB', 'NCLOB', 'LONG')"
    )
    try:
        rows = _run(conn, base.format(collation="collation"))
    except Exception:  # Oracle before 12.2 has no COLLATION column
        log.debug("Oracle has no column collations", exc_info=True)
        rows = _run(conn, base.format(collation="NULL"))
    comp = params.get("NLS_COMP")
    columns: dict[str, ColumnCollation] = {}
    for row in rows:
        data_type = str(row.get("data_type") or "").upper()
        column_charset = ncharset if data_type.startswith("N") else charset
        collation = _text(row.get("collation"))
        case, accent = _oracle_semantics(collation)
        columns[str(row["column_name"])] = ColumnCollation(
            collation=collation or comp,
            charset=column_charset,
            case_sensitive=case,
            accent_sensitive=accent,
            unicode=None if column_charset is None else column_charset.upper() in _UNICODE_CHARSETS,
        )
    linguistic = comp is not None and comp.upper() == "LINGUISTIC"
    sort = params.get("NLS_SORT")
    case, accent = _oracle_semantics(sort if linguistic else None)
    return CollationInfo(
        default_collation=f"NLS_COMP={comp}, NLS_SORT={sort}" if comp else None,
        charset=charset,
        columns=columns,
        case_sensitive_default=case,
        accent_sensitive_default=accent,
        unicode_default=None if charset is None else charset.upper() in _UNICODE_CHARSETS,
    )


_ORACLE_TYPES = {
    "VIEW": "view",
    "MATERIALIZED VIEW": "view",
    "PROCEDURE": "procedure",
    "FUNCTION": "function",
    "PACKAGE": "package",
    "TRIGGER": "trigger",
    "SEQUENCE": "sequence",
    "SYNONYM": "synonym",
}


def _oracle_inventory(conn: Connector, schema: str | None) -> ObjectInventory:
    owner = _ora_owner(conn, schema)
    kinds = ", ".join(_lit(conn, kind) for kind in _ORACLE_TYPES)
    rows = _run(
        conn,
        "SELECT object_type, object_name FROM all_objects "
        f"WHERE owner = {owner} AND object_type IN ({kinds}) "
        "AND object_name NOT LIKE 'BIN$%' AND object_name NOT LIKE 'ISEQ$$%' "
        "AND generated = 'N'",
    )
    mapped = [
        {"object_type": _ORACLE_TYPES.get(str(r["object_type"])), "object_name": r["object_name"]}
        for r in rows
    ]
    resolved = schema or _scalar(
        conn, "SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AS s FROM DUAL"
    )
    return _inventory(resolved, mapped, ())


_PHYSICAL: dict[str, Callable[[Connector, TableName], PhysicalDesign]] = {
    "duckdb": _duckdb_physical,
    "sqlite": _sqlite_physical,
    "postgres": _postgres_physical,
    "mysql": _mysql_physical,
    "mssql": _mssql_physical,
    "oracle": _oracle_physical,
}
_COLLATIONS: dict[str, Callable[[Connector, TableName], CollationInfo]] = {
    "duckdb": _duckdb_collations,
    "sqlite": _sqlite_collations,
    "postgres": _postgres_collations,
    "mysql": _mysql_collations,
    "mssql": _mssql_collations,
    "oracle": _oracle_collations,
}
_INVENTORY: dict[str, Callable[[Connector, str | None], ObjectInventory]] = {
    "duckdb": _duckdb_inventory,
    "sqlite": _sqlite_inventory,
    "postgres": _postgres_inventory,
    "mysql": _mysql_inventory,
    "mssql": _mssql_inventory,
    "oracle": _oracle_inventory,
}


__all__ = [
    "SUPPORTED_DIALECTS",
    "TableName",
    "collations",
    "object_inventory",
    "physical_design",
    "split_table",
]
