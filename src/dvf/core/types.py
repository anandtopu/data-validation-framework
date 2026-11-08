"""Canonical type system.

Every native column type (from any database, file format, or SAP dictionary) maps to a
:class:`CanonicalType`. Schema validation compares canonical types with :func:`classify`, which
returns one of five outcomes: ``exact``, ``compatible_widening``, ``lossy``, ``incompatible``, or
``unknown``.

Native SQL type strings are parsed with sqlglot (see ADR 0003), then corrected for dialect
behaviour sqlglot does not model, such as Oracle ``DATE`` carrying a time component.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError


class LogicalType(StrEnum):
    """The logical kind of a value, independent of any engine."""

    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    FLOAT = "FLOAT"
    STRING = "STRING"
    BINARY = "BINARY"
    DATE = "DATE"
    TIME = "TIME"
    TIMESTAMP = "TIMESTAMP"
    TIMESTAMP_TZ = "TIMESTAMP_TZ"
    INTERVAL = "INTERVAL"
    UUID = "UUID"
    JSON = "JSON"
    ARRAY = "ARRAY"
    MAP = "MAP"
    STRUCT = "STRUCT"
    SPATIAL = "SPATIAL"
    UNKNOWN = "UNKNOWN"


class StructField(BaseModel):
    """A named field of a ``STRUCT`` type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: CanonicalType


class CanonicalType(BaseModel):
    """An engine-independent column type.

    Attributes that do not apply to a logical type stay ``None`` (or their default).
    ``length=None`` on strings and binaries means unbounded; ``precision=None`` on decimals means
    the engine's maximum or a floating decimal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical: LogicalType
    bits: int | None = None
    """Width of INTEGER (8, 16, 24, 32, 64, 128, 256) or FLOAT (32, 64)."""
    unsigned: bool = False
    precision: int | None = None
    """Total digits for DECIMAL; fractional-second digits for TIME and TIMESTAMP types."""
    scale: int | None = None
    """Digits after the decimal point for DECIMAL. ``None`` with a precision means floating."""
    length: int | None = None
    """Maximum length of STRING (characters) or BINARY (bytes). ``None`` means unbounded."""
    fixed_length: bool = False
    """True for blank-padded CHAR or fixed BINARY types."""
    unicode: bool = True
    """False when a STRING can only hold a single-byte code page (for example SQL Server VARCHAR)."""
    element: CanonicalType | None = None
    key: CanonicalType | None = None
    value: CanonicalType | None = None
    fields: tuple[StructField, ...] = ()

    # ------------------------------------------------------------------ constructors
    @classmethod
    def boolean(cls) -> CanonicalType:
        return cls(logical=LogicalType.BOOLEAN)

    @classmethod
    def integer(cls, bits: int = 64, *, unsigned: bool = False) -> CanonicalType:
        return cls(logical=LogicalType.INTEGER, bits=bits, unsigned=unsigned)

    @classmethod
    def decimal(cls, precision: int | None = None, scale: int | None = None) -> CanonicalType:
        return cls(logical=LogicalType.DECIMAL, precision=precision, scale=scale)

    @classmethod
    def float(cls, bits: int = 64) -> CanonicalType:
        return cls(logical=LogicalType.FLOAT, bits=bits)

    @classmethod
    def string(
        cls, length: int | None = None, *, fixed: bool = False, unicode: bool = True
    ) -> CanonicalType:
        return cls(logical=LogicalType.STRING, length=length, fixed_length=fixed, unicode=unicode)

    @classmethod
    def binary(cls, length: int | None = None, *, fixed: bool = False) -> CanonicalType:
        return cls(logical=LogicalType.BINARY, length=length, fixed_length=fixed)

    @classmethod
    def date(cls) -> CanonicalType:
        return cls(logical=LogicalType.DATE)

    @classmethod
    def time(cls, precision: int | None = None) -> CanonicalType:
        return cls(logical=LogicalType.TIME, precision=precision)

    @classmethod
    def timestamp(cls, precision: int | None = None, *, tz: bool = False) -> CanonicalType:
        logical = LogicalType.TIMESTAMP_TZ if tz else LogicalType.TIMESTAMP
        return cls(logical=logical, precision=precision)

    @classmethod
    def array(cls, element: CanonicalType) -> CanonicalType:
        return cls(logical=LogicalType.ARRAY, element=element)

    @classmethod
    def map(cls, key: CanonicalType, value: CanonicalType) -> CanonicalType:
        return cls(logical=LogicalType.MAP, key=key, value=value)

    @classmethod
    def struct(cls, fields: Sequence[tuple[str, CanonicalType]]) -> CanonicalType:
        return cls(
            logical=LogicalType.STRUCT,
            fields=tuple(StructField(name=name, type=typ) for name, typ in fields),
        )

    @classmethod
    def simple(cls, logical: LogicalType) -> CanonicalType:
        return cls(logical=logical)

    @classmethod
    def unknown(cls) -> CanonicalType:
        return cls(logical=LogicalType.UNKNOWN)

    # ------------------------------------------------------------------ rendering
    def __str__(self) -> str:
        lt = self.logical
        if lt is LogicalType.INTEGER:
            return f"{'U' if self.unsigned else ''}INTEGER({self.bits})"
        if lt is LogicalType.FLOAT:
            return f"FLOAT({self.bits})"
        if lt is LogicalType.DECIMAL:
            if self.precision is None:
                return "DECIMAL"
            if self.scale is None:
                return f"DECIMAL({self.precision})"
            return f"DECIMAL({self.precision},{self.scale})"
        if lt in (LogicalType.STRING, LogicalType.BINARY):
            parts = [] if self.length is None else [str(self.length)]
            if self.fixed_length:
                parts.append("fixed")
            if lt is LogicalType.STRING and not self.unicode:
                parts.append("ascii")
            return f"{lt.value}({','.join(parts)})" if parts else lt.value
        if lt in (LogicalType.TIME, LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ):
            return lt.value if self.precision is None else f"{lt.value}({self.precision})"
        if lt is LogicalType.ARRAY:
            return f"ARRAY<{self.element}>"
        if lt is LogicalType.MAP:
            return f"MAP<{self.key},{self.value}>"
        if lt is LogicalType.STRUCT:
            inner = ",".join(f"{f.name}:{f.type}" for f in self.fields)
            return f"STRUCT<{inner}>"
        return lt.value


StructField.model_rebuild()


# ====================================================================== dialects

_DIALECT_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "pg": "postgres",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "sql_server": "tsql",
    "azure_sql": "tsql",
    "synapse": "tsql",
    "fabric": "tsql",
    "mariadb": "mysql",
    "sap_hana": "hana",
    "saphana": "hana",
    "spark_sql": "spark",
    "pyspark": "spark",
}

_CUSTOM_DIALECTS = frozenset({"hana", "db2"})
"""Dialects sqlglot does not support; they use the generic parser plus overrides."""


def normalize_dialect(dialect: str | None) -> str | None:
    """Map a dialect name or alias to the key used by dvf (``None`` means generic SQL)."""
    if dialect is None:
        return None
    key = dialect.strip().lower()
    if key in ("", "generic", "ansi", "sql"):
        return None
    return _DIALECT_ALIASES.get(key, key)


def _sqlglot_dialect(dialect: str | None) -> str | None:
    if dialect is None or dialect in _CUSTOM_DIALECTS:
        return None
    from sqlglot.dialects.dialect import Dialect

    try:
        Dialect.get_or_raise(dialect)
    except ValueError:
        return None
    return dialect


_TS_DEFAULT_PRECISION: dict[str | None, int] = {
    "postgres": 6,
    "redshift": 6,
    "oracle": 6,
    "snowflake": 9,
    "tsql": 7,
    "bigquery": 6,
    "databricks": 6,
    "spark": 6,
    "duckdb": 6,
    "mysql": 0,
    "hana": 7,
    "db2": 6,
    "teradata": 6,
    None: 6,
}

_TIME_DEFAULT_PRECISION: dict[str | None, int] = {"tsql": 7, "mysql": 0, "snowflake": 9}

_DECIMAL_DEFAULTS: dict[str | None, tuple[int, int]] = {
    "tsql": (18, 0),
    "mysql": (10, 0),
    "bigquery": (38, 9),
    "redshift": (18, 0),
    "db2": (5, 0),
}


# ====================================================================== overrides

_Override = Callable[[list[str]], CanonicalType]


def _int_param(params: list[str], index: int = 0) -> int | None:
    if len(params) <= index:
        return None
    raw = params[index].strip().split()[0] if params[index].strip() else ""
    return int(raw) if raw.isdigit() else None


def _hana_tinyint(_: list[str]) -> CanonicalType:
    return CanonicalType.integer(8, unsigned=True)


def _oracle_number(params: list[str]) -> CanonicalType:
    """``NUMBER(p, 0)`` with up to 18 digits holds integers; other forms are decimals."""
    precision = _int_param(params, 0)
    scale = _int_param(params, 1) if len(params) > 1 else 0
    if precision is not None and scale == 0 and precision <= 18:
        bits = 8 if precision <= 2 else 16 if precision <= 4 else 32 if precision <= 9 else 64
        return CanonicalType.integer(bits)
    return CanonicalType.decimal(precision, scale if precision is not None else None)


def _tsql_float(params: list[str]) -> CanonicalType:
    """SQL Server ``FLOAT(n)``: 4 bytes up to 24 mantissa bits, otherwise 8 (the default 53)."""
    bits = _int_param(params, 0)
    return CanonicalType.float(32 if bits is not None and bits <= 24 else 64)


_OVERRIDES: dict[str | None, dict[str, _Override]] = {
    "oracle": {
        # Oracle DATE stores a time component to the second.
        "DATE": lambda _: CanonicalType.timestamp(0),
        "RAW": lambda p: CanonicalType.binary(_int_param(p)),
        "LONG RAW": lambda _: CanonicalType.binary(),
        "LONG": lambda _: CanonicalType.string(),
        # Oracle FLOAT is a NUMBER subtype with binary precision, not IEEE floating point.
        "FLOAT": lambda _: CanonicalType.decimal(),
        "ROWID": lambda _: CanonicalType.string(18),
        "UROWID": lambda p: CanonicalType.string(_int_param(p) or 4000),
        "XMLTYPE": lambda _: CanonicalType.string(),
        "NUMBER": _oracle_number,
        "NCLOB": lambda _: CanonicalType.string(unicode=True),
        "INTERVAL DAY TO SECOND": lambda _: CanonicalType.simple(LogicalType.INTERVAL),
        "INTERVAL YEAR TO MONTH": lambda _: CanonicalType.simple(LogicalType.INTERVAL),
    },
    "hana": {
        "SECONDDATE": lambda _: CanonicalType.timestamp(0),
        "TIMESTAMP": lambda _: CanonicalType.timestamp(7),
        "SMALLDECIMAL": lambda _: CanonicalType.decimal(16),
        "ALPHANUM": lambda p: CanonicalType.string(_int_param(p)),
        "SHORTTEXT": lambda p: CanonicalType.string(_int_param(p)),
        "TEXT": lambda _: CanonicalType.string(),
        "BINTEXT": lambda _: CanonicalType.string(),
        "NCLOB": lambda _: CanonicalType.string(),
        "CLOB": lambda _: CanonicalType.string(),
        "BLOB": lambda _: CanonicalType.binary(),
        "TINYINT": _hana_tinyint,
        "REAL": lambda _: CanonicalType.float(32),
        "ST_GEOMETRY": lambda _: CanonicalType.simple(LogicalType.SPATIAL),
        "ST_POINT": lambda _: CanonicalType.simple(LogicalType.SPATIAL),
    },
    "db2": {
        "GRAPHIC": lambda p: CanonicalType.string(_int_param(p) or 1, fixed=True),
        "VARGRAPHIC": lambda p: CanonicalType.string(_int_param(p)),
        "DBCLOB": lambda _: CanonicalType.string(),
        "CLOB": lambda _: CanonicalType.string(),
        "DECFLOAT": lambda p: CanonicalType.decimal(_int_param(p) or 34),
    },
    "tsql": {
        # In SQL Server, TIMESTAMP is a synonym for ROWVERSION, not a date-time type.
        "TIMESTAMP": lambda _: CanonicalType.binary(8, fixed=True),
        "SQL_VARIANT": lambda _: CanonicalType.unknown(),
        "FLOAT": _tsql_float,
        "NTEXT": lambda _: CanonicalType.string(unicode=True),
    },
    "mysql": {
        "YEAR": lambda _: CanonicalType.integer(16),
    },
}

_SIMPLE_TYPE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*(?:\(([^()]*)\))?\s*$")


def _apply_override(native: str, dialect: str | None) -> CanonicalType | None:
    overrides = _OVERRIDES.get(dialect)
    if not overrides:
        return None
    match = _SIMPLE_TYPE_RE.match(native)
    if not match:
        # Parameters inside the name, such as INTERVAL DAY(2) TO SECOND(6).
        bare = " ".join(re.sub(r"\([^()]*\)", " ", native).upper().split())
        handler = overrides.get(bare)
        return handler([]) if handler is not None else None
    base = " ".join(match.group(1).upper().split())
    handler = overrides.get(base)
    if handler is None:
        return None
    params = [p.strip() for p in match.group(2).split(",")] if match.group(2) else []
    return handler(params)


# ====================================================================== sqlglot mapping

_INT_BITS: dict[str, tuple[int, bool]] = {
    "TINYINT": (8, False),
    "SMALLINT": (16, False),
    "SMALLSERIAL": (16, False),
    "MEDIUMINT": (24, False),
    "INT": (32, False),
    "SERIAL": (32, False),
    "BIGINT": (64, False),
    "BIGSERIAL": (64, False),
    "INT128": (128, False),
    "INT256": (256, False),
    "UTINYINT": (8, True),
    "USMALLINT": (16, True),
    "UMEDIUMINT": (24, True),
    "UINT": (32, True),
    "UBIGINT": (64, True),
    "UINT128": (128, True),
    "UINT256": (256, True),
}

_DECIMAL_NAMES = frozenset(
    {"DECIMAL", "DECIMAL32", "DECIMAL64", "DECIMAL128", "DECIMAL256", "UDECIMAL", "DECFLOAT"}
)
_FIXED_CHAR_NAMES = frozenset({"CHAR", "NCHAR", "BPCHAR", "FIXEDSTRING"})
_VAR_CHAR_NAMES = frozenset({"VARCHAR", "NVARCHAR"})
_TEXT_NAMES = frozenset(
    {"TEXT", "LONGTEXT", "MEDIUMTEXT", "ENUM", "ENUM8", "ENUM16", "SET", "XML", "INET"}
)
_NETWORK_NAMES = frozenset({"IPADDRESS", "IPPREFIX", "IPV4", "IPV6"})
_JSON_NAMES = frozenset({"JSON", "JSONB", "VARIANT", "OBJECT", "SUPER", "DYNAMIC"})
_BLOB_NAMES = frozenset({"BLOB", "LONGBLOB", "MEDIUMBLOB", "TINYBLOB", "IMAGE"})
_SPATIAL_NAMES = frozenset(
    {
        "GEOGRAPHY",
        "GEOGRAPHYPOINT",
        "GEOMETRY",
        "POINT",
        "RING",
        "LINESTRING",
        "MULTILINESTRING",
        "POLYGON",
        "MULTIPOLYGON",
    }
)
_NON_UNICODE_IN_TSQL = frozenset({"CHAR", "VARCHAR", "TEXT"})


def _params(dt: exp.DataType) -> list[exp.Expression]:
    return [e for e in dt.expressions if isinstance(e, exp.DataTypeParam)]


def _param_int(dt: exp.DataType, index: int) -> int | None:
    params = _params(dt)
    if len(params) <= index:
        return None
    text = params[index].name.strip()
    return int(text) if text.isdigit() else None


def _param_is_max(dt: exp.DataType) -> bool:
    params = _params(dt)
    return bool(params) and params[0].name.strip().upper() == "MAX"


def _from_sqlglot(dt: exp.DataType, dialect: str | None) -> CanonicalType:
    name = dt.this.name
    nested = [e for e in dt.expressions if isinstance(e, exp.DataType)]

    if name in ("BOOLEAN",):
        return CanonicalType.boolean()
    if name == "BIT":
        width = _param_int(dt, 0)
        if width is not None and width > 1:
            return CanonicalType.binary(fixed=True)
        return CanonicalType.boolean()
    if name in _INT_BITS:
        bits, unsigned = _INT_BITS[name]
        return CanonicalType.integer(bits, unsigned=unsigned)
    if name in _DECIMAL_NAMES:
        precision, scale = _param_int(dt, 0), _param_int(dt, 1)
        if precision is None and dialect in _DECIMAL_DEFAULTS:
            precision, scale = _DECIMAL_DEFAULTS[dialect]
        elif precision is not None and scale is None:
            scale = 0
        return CanonicalType.decimal(precision, scale)
    if name in ("BIGDECIMAL", "BIGNUM"):
        precision, scale = _param_int(dt, 0), _param_int(dt, 1)
        return CanonicalType.decimal(precision or 76, 38 if scale is None else scale)
    if name == "MONEY":
        return CanonicalType.decimal(19, 4)
    if name == "SMALLMONEY":
        return CanonicalType.decimal(10, 4)
    if name == "FLOAT":
        width = _param_int(dt, 0)
        return CanonicalType.float(64 if width is not None and width > 24 else 32)
    if name in ("DOUBLE", "UDOUBLE"):
        return CanonicalType.float(64)
    if name in _FIXED_CHAR_NAMES:
        unicode = not (dialect == "tsql" and name in _NON_UNICODE_IN_TSQL)
        return CanonicalType.string(_param_int(dt, 0) or 1, fixed=True, unicode=unicode)
    if name in _VAR_CHAR_NAMES:
        unicode = not (dialect == "tsql" and name in _NON_UNICODE_IN_TSQL)
        length = None if _param_is_max(dt) else _param_int(dt, 0)
        return CanonicalType.string(length, unicode=unicode)
    if name in _TEXT_NAMES or name in _NETWORK_NAMES:
        unicode = not (dialect == "tsql" and name in _NON_UNICODE_IN_TSQL)
        return CanonicalType.string(unicode=unicode)
    if name == "TINYTEXT":
        return CanonicalType.string(255)
    if name == "NAME":
        return CanonicalType.string(63)
    if name == "LOWCARDINALITY" and nested:
        return _from_sqlglot(nested[0], dialect)
    if name == "BINARY":
        length = _param_int(dt, 0)
        return CanonicalType.binary(length, fixed=length is not None)
    if name == "VARBINARY":
        return CanonicalType.binary(None if _param_is_max(dt) else _param_int(dt, 0))
    if name in _BLOB_NAMES:
        return CanonicalType.binary()
    if name == "ROWVERSION":
        return CanonicalType.binary(8, fixed=True)
    if name in ("DATE", "DATE32"):
        return CanonicalType.date()
    if name in ("TIME", "TIMETZ"):
        precision = _param_int(dt, 0)
        if precision is None:
            precision = _TIME_DEFAULT_PRECISION.get(dialect, 6)
        return CanonicalType.time(precision)
    if name == "TIME_NS":
        return CanonicalType.time(9)
    if name == "DATETIME":
        precision = _param_int(dt, 0)
        if precision is None:
            precision = {"tsql": 3, "mysql": 0}.get(dialect or "", 6)
        return CanonicalType.timestamp(precision)
    if name == "DATETIME2":
        precision = _param_int(dt, 0)
        return CanonicalType.timestamp(7 if precision is None else precision)
    if name == "SMALLDATETIME":
        return CanonicalType.timestamp(0)
    if name == "DATETIME64":
        precision = _param_int(dt, 0)
        return CanonicalType.timestamp(3 if precision is None else precision)
    if name in ("TIMESTAMP", "TIMESTAMPNTZ", "TIMESTAMPTZ", "TIMESTAMPLTZ"):
        precision = _param_int(dt, 0)
        if precision is None:
            precision = _TS_DEFAULT_PRECISION.get(dialect, 6)
        return CanonicalType.timestamp(precision, tz=name in ("TIMESTAMPTZ", "TIMESTAMPLTZ"))
    if name == "TIMESTAMP_S":
        return CanonicalType.timestamp(0)
    if name == "TIMESTAMP_MS":
        return CanonicalType.timestamp(3)
    if name == "TIMESTAMP_NS":
        return CanonicalType.timestamp(9)
    if name == "INTERVAL":
        return CanonicalType.simple(LogicalType.INTERVAL)
    if name == "UUID":
        return CanonicalType.simple(LogicalType.UUID)
    if name in _JSON_NAMES:
        return CanonicalType.simple(LogicalType.JSON)
    if name in ("ARRAY", "LIST"):
        if nested:
            return CanonicalType.array(_from_sqlglot(nested[0], dialect))
        # Untyped arrays (for example Snowflake ARRAY) hold semi-structured values.
        return CanonicalType.array(CanonicalType.simple(LogicalType.JSON))
    if name == "MAP":
        if len(nested) == 2:
            return CanonicalType.map(
                _from_sqlglot(nested[0], dialect), _from_sqlglot(nested[1], dialect)
            )
        return CanonicalType.simple(LogicalType.JSON)
    if name == "HSTORE":
        return CanonicalType.map(CanonicalType.string(), CanonicalType.string())
    if name in ("STRUCT", "NESTED"):
        fields: list[tuple[str, CanonicalType]] = []
        for item in dt.expressions:
            if isinstance(item, exp.ColumnDef):
                kind = item.args.get("kind")
                field_type = (
                    _from_sqlglot(kind, dialect)
                    if isinstance(kind, exp.DataType)
                    else CanonicalType.unknown()
                )
                fields.append((item.name, field_type))
        return CanonicalType.struct(fields)
    if name in _SPATIAL_NAMES:
        return CanonicalType.simple(LogicalType.SPATIAL)
    return CanonicalType.unknown()


def parse_native_type(native: str, dialect: str | None = None) -> CanonicalType:
    """Parse a native type string such as ``"NUMBER(10,2)"`` for a dialect.

    Unrecognized types map to ``UNKNOWN`` instead of raising, so schema validation can report
    them rather than fail.
    """
    key = normalize_dialect(dialect)
    overridden = _apply_override(native, key)
    if overridden is not None:
        return overridden
    try:
        dt = exp.DataType.build(native, dialect=_sqlglot_dialect(key))
    except (ParseError, TokenError, ValueError):
        return CanonicalType.unknown()
    return _from_sqlglot(dt, key)


# ====================================================================== SAP dictionary

_SAP_FIXED_STRINGS: dict[str, int | None] = {
    "CHAR": None,
    "NUMC": None,
    "CLNT": 3,
    "LANG": 1,
    "CUKY": 5,
    "UNIT": 3,
    "ACCP": 6,
    "LCHR": None,
}


def from_sap_ddic(
    datatype: str, length: int | None = None, decimals: int | None = None
) -> CanonicalType:
    """Map an SAP ABAP dictionary type (as stored in DD03L.DATATYPE) to a canonical type.

    Character-like key types (CHAR, NUMC, CLNT, ...) map to fixed-length strings, because the
    database stores them blank-padded. DATS and TIMS map to their logical date and time meaning;
    the ``sap_dats`` and ``sap_tims`` normalizers convert the stored ``YYYYMMDD`` and ``HHMMSS``
    strings.
    """
    code = datatype.strip().upper()
    if code in _SAP_FIXED_STRINGS:
        fixed = _SAP_FIXED_STRINGS[code]
        if code == "LCHR":
            return CanonicalType.string(length)
        return CanonicalType.string(fixed if fixed is not None else length, fixed=True)
    if code in ("DEC", "CURR", "QUAN"):
        return CanonicalType.decimal(length, decimals if decimals is not None else 0)
    if code == "INT1":
        return CanonicalType.integer(8, unsigned=True)
    if code in ("INT2", "PREC"):
        return CanonicalType.integer(16)
    if code == "INT4":
        return CanonicalType.integer(32)
    if code == "INT8":
        return CanonicalType.integer(64)
    if code == "FLTP":
        return CanonicalType.float(64)
    if code in ("D16N", "D16R", "D16S", "D16D", "DF16_DEC", "DF16_RAW"):
        return CanonicalType.decimal(16)
    if code in ("D34N", "D34R", "D34S", "D34D", "DF34_DEC", "DF34_RAW"):
        return CanonicalType.decimal(34)
    if code in ("DATS", "DATN"):
        return CanonicalType.date()
    if code in ("TIMS", "TIMN"):
        return CanonicalType.time(0)
    if code == "UTCL":
        return CanonicalType.timestamp(7, tz=True)
    if code == "STRG":
        return CanonicalType.string()
    if code == "SSTR":
        return CanonicalType.string(length)
    if code in ("STRING", "SSTRING"):
        return CanonicalType.string(length if code == "SSTRING" else None)
    if code == "RAW":
        return CanonicalType.binary(length, fixed=True)
    if code in ("RSTR", "RAWSTRING", "LRAW"):
        return CanonicalType.binary()
    return CanonicalType.unknown()


# ====================================================================== compatibility


class Compatibility(StrEnum):
    """How safely values of one type fit into another."""

    EXACT = "exact"
    WIDENING = "compatible_widening"
    LOSSY = "lossy"
    UNKNOWN = "unknown"
    INCOMPATIBLE = "incompatible"

    @property
    def rank(self) -> int:
        return _COMPAT_RANK[self]


_COMPAT_RANK = {
    Compatibility.EXACT: 0,
    Compatibility.WIDENING: 1,
    Compatibility.LOSSY: 2,
    Compatibility.UNKNOWN: 3,
    Compatibility.INCOMPATIBLE: 4,
}


@dataclass(frozen=True)
class CompatibilityResult:
    """Outcome of :func:`classify`, with human-readable reasons."""

    level: Compatibility
    reasons: tuple[str, ...] = ()

    def worst(self, other: CompatibilityResult) -> CompatibilityResult:
        level = self.level if self.level.rank >= other.level.rank else other.level
        return CompatibilityResult(level, self.reasons + other.reasons)


def _result(level: Compatibility, *reasons: str) -> CompatibilityResult:
    return CompatibilityResult(level, tuple(reasons))


_EXACT = CompatibilityResult(Compatibility.EXACT)
_TEMPORAL = (LogicalType.TIME, LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ)


def classify(source: CanonicalType, target: CanonicalType) -> CompatibilityResult:
    """Classify whether every value of ``source`` can be stored in ``target`` without change."""
    if LogicalType.UNKNOWN in (source.logical, target.logical):
        return _result(Compatibility.UNKNOWN, f"cannot classify {source} -> {target}")
    if source == target:
        return _EXACT
    if source.logical is target.logical:
        return _classify_same(source, target)
    return _classify_cross(source, target)


def _int_range(t: CanonicalType) -> tuple[int, int]:
    bits = t.bits or 64
    if t.unsigned:
        return 0, 2**bits - 1
    return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1


def _int_digits(t: CanonicalType) -> int:
    low, high = _int_range(t)
    return max(len(str(abs(low))), len(str(high)))


def _classify_same(s: CanonicalType, t: CanonicalType) -> CompatibilityResult:
    lt = s.logical
    if lt is LogicalType.INTEGER:
        (s_lo, s_hi), (t_lo, t_hi) = _int_range(s), _int_range(t)
        if t_lo <= s_lo and s_hi <= t_hi:
            return _result(Compatibility.WIDENING, f"integer range widened: {s} -> {t}")
        return _result(Compatibility.LOSSY, f"integer range narrowed: {s} -> {t}")
    if lt is LogicalType.FLOAT:
        if (t.bits or 64) >= (s.bits or 64):
            return _result(Compatibility.WIDENING, f"float widened: {s} -> {t}")
        return _result(Compatibility.LOSSY, f"float precision reduced: {s} -> {t}")
    if lt is LogicalType.DECIMAL:
        return _classify_decimal(s, t)
    if lt in (LogicalType.STRING, LogicalType.BINARY):
        return _classify_lengths(s, t)
    if lt in _TEMPORAL:
        s_p, t_p = s.precision if s.precision is not None else 6, t.precision
        t_p = t_p if t_p is not None else 6
        if t_p < s_p:
            return _result(
                Compatibility.LOSSY, f"fractional seconds precision reduced from {s_p} to {t_p}"
            )
        return _result(Compatibility.WIDENING, f"fractional seconds precision {s_p} -> {t_p}")
    if lt is LogicalType.ARRAY and s.element is not None and t.element is not None:
        inner = classify(s.element, t.element)
        return CompatibilityResult(inner.level, tuple(f"array element: {r}" for r in inner.reasons))
    if (
        lt is LogicalType.MAP
        and s.key is not None
        and s.value is not None
        and t.key is not None
        and t.value is not None
    ):
        key = classify(s.key, t.key)
        value = classify(s.value, t.value)
        return CompatibilityResult(key.level, tuple(f"map key: {r}" for r in key.reasons)).worst(
            CompatibilityResult(value.level, tuple(f"map value: {r}" for r in value.reasons))
        )
    if lt is LogicalType.STRUCT:
        return _classify_struct(s, t)
    return _EXACT


def _classify_decimal(s: CanonicalType, t: CanonicalType) -> CompatibilityResult:
    if t.precision is None:
        return _result(Compatibility.WIDENING, f"target decimal is unbounded: {s} -> {t}")
    if s.precision is None:
        return _result(Compatibility.LOSSY, f"source decimal is unbounded: {s} -> {t}")
    if s.scale is None or t.scale is None:
        if s.scale is None and t.scale is not None:
            return _result(Compatibility.LOSSY, f"floating decimal to fixed scale: {s} -> {t}")
        if t.precision >= s.precision:
            return _result(Compatibility.WIDENING, f"decimal precision widened: {s} -> {t}")
        return _result(Compatibility.LOSSY, f"decimal precision reduced: {s} -> {t}")
    reasons: list[str] = []
    s_int, t_int = s.precision - s.scale, t.precision - t.scale
    if t_int < s_int:
        reasons.append(f"integer digits reduced from {s_int} to {t_int}")
    if t.scale < s.scale:
        reasons.append(f"scale reduced from {s.scale} to {t.scale}")
    if reasons:
        return _result(Compatibility.LOSSY, *reasons)
    return _result(Compatibility.WIDENING, f"decimal widened: {s} -> {t}")


def _classify_lengths(s: CanonicalType, t: CanonicalType) -> CompatibilityResult:
    lossy: list[str] = []
    notes: list[str] = []
    if t.length is not None and (s.length is None or t.length < s.length):
        lossy.append(f"maximum length reduced from {s.length or 'unbounded'} to {t.length}")
    if s.logical is LogicalType.STRING and s.unicode and not t.unicode:
        lossy.append("Unicode to non-Unicode string")
    if s.fixed_length and not t.fixed_length:
        notes.append("fixed-length (blank-padded) to variable-length: trailing spaces may differ")
    elif t.fixed_length and not s.fixed_length:
        notes.append("variable-length to fixed-length: values will be blank-padded")
    if lossy:
        return _result(Compatibility.LOSSY, *lossy, *notes)
    return _result(Compatibility.WIDENING, f"{s} -> {t}", *notes)


def _classify_struct(s: CanonicalType, t: CanonicalType) -> CompatibilityResult:
    target_fields = {f.name.lower(): f for f in t.fields}
    source_names = {f.name.lower() for f in s.fields}
    result = _EXACT
    for field in s.fields:
        match = target_fields.get(field.name.lower())
        if match is None:
            result = result.worst(
                _result(Compatibility.LOSSY, f"struct field '{field.name}' missing in target")
            )
            continue
        inner = classify(field.type, match.type)
        result = result.worst(
            CompatibilityResult(
                inner.level, tuple(f"struct field '{field.name}': {r}" for r in inner.reasons)
            )
        )
    for name, field in target_fields.items():
        if name not in source_names:
            result = result.worst(
                _result(Compatibility.WIDENING, f"struct field '{field.name}' added in target")
            )
    return result


def _text_width(t: CanonicalType) -> int | None:
    """Maximum characters needed to render a value of ``t`` as text (None if unbounded)."""
    lt = t.logical
    if lt is LogicalType.BOOLEAN:
        return 5
    if lt is LogicalType.INTEGER:
        return _int_digits(t) + (0 if t.unsigned else 1)
    if lt is LogicalType.DECIMAL:
        return None if t.precision is None else t.precision + 2
    if lt is LogicalType.FLOAT:
        return 24
    if lt is LogicalType.DATE:
        return 10
    if lt is LogicalType.TIME:
        return 8 + (1 + t.precision if t.precision else 0)
    if lt is LogicalType.TIMESTAMP:
        return 19 + (1 + t.precision if t.precision else 0)
    if lt is LogicalType.TIMESTAMP_TZ:
        return 25 + (1 + t.precision if t.precision else 0)
    if lt is LogicalType.UUID:
        return 36
    if lt is LogicalType.BINARY:
        return None if t.length is None else 2 * t.length
    if lt is LogicalType.INTERVAL:
        return 32
    return None


def _classify_cross(s: CanonicalType, t: CanonicalType) -> CompatibilityResult:
    sl, tl = s.logical, t.logical
    w, lossy = Compatibility.WIDENING, Compatibility.LOSSY

    if tl is LogicalType.STRING:
        width = _text_width(s)
        if t.length is None or (width is not None and t.length >= width):
            return _result(w, f"{s} stored as text")
        needed = "unbounded" if width is None else str(width)
        return _result(
            lossy, f"{s} stored as text; needs {needed} characters, target has {t.length}"
        )
    if sl is LogicalType.STRING:
        return _result(lossy, f"text must be parsed into {t}; unparseable values are lost")

    if sl is LogicalType.BOOLEAN and tl in (LogicalType.INTEGER, LogicalType.DECIMAL):
        return _result(w, f"boolean stored as {t}")
    if tl is LogicalType.BOOLEAN:
        return _result(lossy, f"{s} narrowed to boolean")

    if sl is LogicalType.INTEGER and tl is LogicalType.DECIMAL:
        if t.precision is None:
            return _result(w, f"integer stored in unbounded decimal {t}")
        available = t.precision - (t.scale or 0)
        if available >= _int_digits(s):
            return _result(w, f"{s} fits in {t}")
        return _result(lossy, f"{s} needs {_int_digits(s)} integer digits, {t} has {available}")
    if sl is LogicalType.INTEGER and tl is LogicalType.FLOAT:
        mantissa = 24 if (t.bits or 64) == 32 else 53
        magnitude_bits = (s.bits or 64) - (0 if s.unsigned else 1)
        if magnitude_bits <= mantissa:
            return _result(w, f"{s} is exactly representable in {t}")
        return _result(lossy, f"large values of {s} lose precision in {t}")
    if sl is LogicalType.DECIMAL and tl is LogicalType.INTEGER:
        if s.scale is None or s.scale > 0:
            return _result(lossy, f"fractional digits of {s} are dropped in {t}")
        if s.precision is not None and 10**s.precision - 1 <= _int_range(t)[1]:
            return _result(w, f"{s} fits in {t}")
        return _result(lossy, f"{s} can exceed the range of {t}")
    if sl in (LogicalType.DECIMAL, LogicalType.FLOAT) and tl in (
        LogicalType.DECIMAL,
        LogicalType.FLOAT,
        LogicalType.INTEGER,
    ):
        return _result(lossy, f"{s} -> {t} rounds values")

    if sl is LogicalType.DATE and tl in (LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ):
        reasons = ["date stored as timestamp at midnight"]
        if tl is LogicalType.TIMESTAMP_TZ:
            reasons.append("a time zone must be assumed")
        return _result(w, *reasons)
    if sl in (LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ) and tl is LogicalType.DATE:
        return _result(lossy, "time component dropped")
    if sl is LogicalType.TIMESTAMP and tl is LogicalType.TIMESTAMP_TZ:
        base = _classify_same(s, s.model_copy(update={"precision": t.precision}))
        return _result(
            Compatibility.LOSSY if base.level is Compatibility.LOSSY else w,
            *base.reasons,
            "a time zone must be assumed when adding an offset",
        )
    if sl is LogicalType.TIMESTAMP_TZ and tl is LogicalType.TIMESTAMP:
        return _result(lossy, "time zone offset dropped")

    if sl is LogicalType.UUID and tl is LogicalType.BINARY:
        if t.length is None or t.length >= 16:
            return _result(w, "UUID stored as binary")
        return _result(lossy, f"UUID needs 16 bytes, target has {t.length}")
    if sl is LogicalType.BINARY and tl is LogicalType.UUID:
        return _result(lossy, "binary must be parsed into UUID")

    if sl in (LogicalType.ARRAY, LogicalType.MAP, LogicalType.STRUCT) and tl is LogicalType.JSON:
        return _result(w, f"{sl.value.lower()} stored as JSON")
    if sl is LogicalType.JSON and tl in (LogicalType.ARRAY, LogicalType.MAP, LogicalType.STRUCT):
        return _result(lossy, f"JSON must match the {tl.value.lower()} structure")

    return _result(Compatibility.INCOMPATIBLE, f"{s} cannot be stored as {t}")
