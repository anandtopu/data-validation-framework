"""``dvf import-contract``: turn an ODCS v3 data contract into schema expectations and checks.

Each schema object becomes a table of a single-dataset suite with:

* a ``contract`` check (SCH-12) that compares the dataset's schema with the contract object;
* checks derived from property constraints (``required``, ``unique``, ``primaryKey``,
  ``logicalTypeOptions``, ``enum``) and ``relationships``;
* checks derived from ``quality`` rules: library metrics (``nullValues``, ``missingValues``,
  ``invalidValues``, ``duplicateValues``, ``rowCount``, and the v3.0 ``rule`` names) and
  ``sql`` rules.

Rules dvf cannot run (``text`` and ``custom`` rules, unknown metrics, unsupported options) are
listed with the reason, in the summary and as comments in the generated file.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dvf.config.loader import LoadedConfig, LoadOptions, load_config_data
from dvf.core.errors import ConfigError
from dvf.discovery.common import UniqueNames, generated_header, identifier, write_output
from dvf.discovery.connections import (
    ConnectionSpec,
    relative_path,
    spec_from_config,
    spec_from_location,
)
from dvf.discovery.odcs import Contract, ContractObject, ContractProperty, load_contract
from dvf.discovery.patterns import ODCS_FORMATS
from dvf.discovery.yamlwriter import Document, Flow, Map, Seq, dump, plain
from dvf.plugins.registry import PluginKind, Registry, get_registry
from dvf.sql.dialects import Dialect, UnsupportedFeatureError, get_dialect

Between = Literal["exclusive", "inclusive"]

OPERATORS: dict[str, str] = {
    "mustBe": "==",
    "mustNotBe": "!=",
    "mustBeGreaterThan": ">",
    "mustBeGreaterOrEqualTo": ">=",
    "mustBeGreaterThanOrEqualTo": ">=",
    "mustBeLessThan": "<",
    "mustBeLessOrEqualTo": "<=",
    "mustBeLessThanOrEqualTo": "<=",
    "mustBeBetween": "between",
    "mustNotBeBetween": "not_between",
}
"""ODCS operators and their meaning (``...ThanOrEqualTo`` spellings are accepted too)."""

LIBRARY_METRICS = ("nullValues", "missingValues", "invalidValues", "duplicateValues", "rowCount")
_LEGACY_RULES = {
    "nullCheck": "nullValues",
    "nullValues": "nullValues",
    "duplicateCount": "duplicateValues",
    "duplicateValues": "duplicateValues",
    "validValues": "invalidValues",
    "invalidValues": "invalidValues",
    "missingValues": "missingValues",
    "rowCount": "rowCount",
}
_SEVERITIES = {
    "critical": "critical",
    "blocker": "critical",
    "error": "error",
    "high": "error",
    "major": "error",
    "warning": "warning",
    "warn": "warning",
    "medium": "warning",
    "minor": "info",
    "low": "info",
    "info": "info",
}
FORMAT_PATTERNS = ODCS_FORMATS
"""String ``logicalTypeOptions.format`` values dvf turns into regex checks."""

_SERVER_TYPES = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlserver": "mssql",
    "synapse": "mssql",
    "oracle": "oracle",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "databricks": "databricks",
    "duckdb": "duckdb",
    "local": "files",
    "s3": "files",
    "gcs": "files",
    "azure": "files",
}


@dataclass
class Unsupported:
    """A contract element that was not converted, and why."""

    where: str
    item: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"where": self.where, "item": self.item, "reason": self.reason}

    def __str__(self) -> str:
        return f"{self.where}: {self.item}: {self.reason}"


@dataclass
class ContractImportResult:
    """The generated configuration and what was (and was not) converted."""

    document: Document
    objects: int
    checks: int
    unsupported: list[Unsupported]
    notes: list[str]
    output: Path | None = None

    @property
    def text(self) -> str:
        return dump(self.document)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output) if self.output else None,
            "objects": self.objects,
            "checks": self.checks,
            "unsupported": [u.to_dict() for u in self.unsupported],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------- rule conversion


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


@dataclass
class _Constraint:
    op: str
    value: float | int


def _constraints(rule: Mapping[str, Any], between: Between) -> tuple[list[_Constraint], str | None]:
    """The rule's thresholds as simple comparisons, or an error message."""
    result: list[_Constraint] = []
    for key, op in OPERATORS.items():
        if key not in rule:
            continue
        raw = rule[key]
        if op in ("between", "not_between"):
            if not isinstance(raw, list) or len(raw) != 2:
                return [], f"'{key}' needs a list of two numbers"
            low, high = _number(raw[0]), _number(raw[1])
            if low is None or high is None:
                return [], f"'{key}' needs numbers"
            if op == "between":
                lo_op, hi_op = (">", "<") if between == "exclusive" else (">=", "<=")
                result += [_Constraint(lo_op, low), _Constraint(hi_op, high)]
            else:
                result.append(_Constraint("not_between", low))
                result.append(_Constraint("not_between_high", high))
            continue
        number = _number(raw)
        if number is None:
            return [], f"'{key}' needs a number, got {raw!r}"
        result.append(_Constraint(op, number))
    return result, None


def _is_zero(constraints: list[_Constraint], unit: str = "rows") -> bool:
    """No threshold, or exactly "must be 0" ("at most 0", or "below 1" when counting rows)."""
    if not constraints:
        return True
    if len(constraints) != 1:
        return False
    c = constraints[0]
    below_one = c.op == "<" and c.value == 1 and unit == "rows"
    return (c.op in ("==", "<=") and c.value == 0) or below_one


@dataclass
class _Rule:
    """What a library metric handler needs."""

    base: dict[str, Any]
    rule: Mapping[str, Any]
    arguments: Mapping[str, Any]
    constraints: list[_Constraint]
    unit: str
    obj: ContractObject
    prop: ContractProperty | None
    where: str
    label: str


class _Converter:
    def __init__(self, dialect: Dialect, between: Between) -> None:
        self.dialect = dialect
        self.between = between
        self.unsupported: list[Unsupported] = []

    def skip(self, where: str, item: str, reason: str) -> None:
        self.unsupported.append(Unsupported(where, item, reason))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def label(rule: Mapping[str, Any]) -> str:
        for key in ("id", "name", "metric", "rule", "type"):
            if rule.get(key):
                return f"{key} {rule[key]}"
        return "rule"

    @staticmethod
    def base(rule: Mapping[str, Any], fallback_name: str) -> dict[str, Any]:
        check: dict[str, Any] = {}
        name = rule.get("id") or rule.get("name")
        check["name"] = identifier(str(name), fallback_name) if name else fallback_name
        if rule.get("description"):
            check["description"] = str(rule["description"]).strip()
        severity = _SEVERITIES.get(str(rule.get("severity", "")).strip().lower())
        if severity:
            check["severity"] = severity
        details = {
            f"odcs_{key}": rule[key]
            for key in ("id", "dimension", "businessImpact", "method")
            if rule.get(key) is not None
        }
        if details:
            check["details"] = details
        return check

    def scalar_checks(
        self, base: dict[str, Any], sql: str, constraints: list[_Constraint]
    ) -> list[dict[str, Any]]:
        """custom_sql scalar checks, one per threshold (a not-between range is one check)."""
        checks: list[dict[str, Any]] = []
        lows = [c for c in constraints if c.op == "not_between"]
        highs = [c for c in constraints if c.op == "not_between_high"]
        if lows and highs:
            wrapped = (
                f"SELECT CASE WHEN ({sql}) < {lows[0].value} OR ({sql}) > {highs[0].value} "
                "THEN 1 ELSE 0 END"
            )
            check = dict(base, type="custom_sql", sql=wrapped, returns="scalar")
            check["expect"] = {"operator": "==", "value": 1}
            checks.append(check)
        plain_constraints = [c for c in constraints if not c.op.startswith("not_between")]
        for i, constraint in enumerate(plain_constraints):
            check = dict(base, type="custom_sql", sql=sql, returns="scalar")
            if len(plain_constraints) > 1:
                check["name"] = (
                    f"{base['name']}_{('min', 'max')[i] if len(plain_constraints) == 2 else i + 1}"
                )
            check["expect"] = {"operator": constraint.op, "value": constraint.value}
            checks.append(check)
        return checks

    def _text_list(self, values: list[Any]) -> str:
        return ", ".join(self.dialect.literal(str(v)) for v in values)

    def _rate(self, count_sql_body: str, unit: str) -> str:
        """A count of failing rows, or its percentage of all rows when ``unit`` is percent."""
        if unit == "percent":
            return (
                f"SELECT COALESCE(100.0 * SUM(CASE WHEN {count_sql_body} THEN 1 ELSE 0 END) "
                "/ NULLIF(COUNT(*), 0), 0) FROM {table}"
            )
        return f"SELECT COUNT(*) FROM {{table}} WHERE {count_sql_body}"

    # ------------------------------------------------------------------ rules
    def rule(
        self, rule: Mapping[str, Any], obj: ContractObject, prop: ContractProperty | None, n: int
    ) -> list[dict[str, Any]]:
        where = obj.name + (f".{prop.name}" if prop else "")
        kind = str(
            rule.get("type") or ("library" if (rule.get("metric") or rule.get("rule")) else "")
        ).lower()
        label = self.label(rule)
        if kind == "text":
            self.skip(
                where, label, "text rules describe an expectation in prose and are not executable"
            )
            return []
        if kind == "custom":
            engine = rule.get("engine") or "unknown"
            self.skip(
                where,
                label,
                f"custom rules for engine '{engine}' are tool-specific and not run by dvf",
            )
            return []
        if kind == "sql":
            return self.sql_rule(rule, prop, where, label, n)
        if kind not in ("library", ""):
            self.skip(where, label, f"unknown rule type '{kind}'")
            return []
        metric_name = rule.get("metric") or rule.get("rule")
        if not metric_name:
            self.skip(
                where, label, "the rule has no 'metric' (or v3.0 'rule'), 'type: sql', or 'query'"
            )
            return []
        metric = _LEGACY_RULES.get(str(metric_name))
        if metric is None:
            self.skip(
                where,
                label,
                f"unknown library metric '{metric_name}'; supported: {', '.join(LIBRARY_METRICS)}",
            )
            return []
        constraints, problem = _constraints(rule, self.between)
        if problem:
            self.skip(where, label, problem)
            return []
        unit = str(rule.get("unit") or "rows").lower()
        if unit not in ("rows", "percent"):
            self.skip(where, label, f"unit '{unit}' is not supported (use rows or percent)")
            return []
        base = self.base(rule, f"{metric}_{n}")
        arguments = rule.get("arguments") if isinstance(rule.get("arguments"), Mapping) else {}
        assert isinstance(arguments, Mapping)
        handlers = {
            "nullValues": self._null_values,
            "missingValues": self._missing_values,
            "invalidValues": self._invalid_values,
            "duplicateValues": self._duplicate_values,
            "rowCount": self._row_count,
        }
        context = _Rule(base, rule, arguments, constraints, unit, obj, prop, where, label)
        return handlers[metric](context)

    def _column(self, r: _Rule, metric: str) -> str | None:
        if r.prop is None:
            self.skip(
                r.where,
                r.label,
                f"'{metric}' applies to a property; put the rule under the property",
            )
            return None
        return r.prop.column

    def _null_values(self, r: _Rule) -> list[dict[str, Any]]:
        col = self._column(r, "nullValues")
        if col is None:
            return []
        if _is_zero(r.constraints, r.unit):
            return [dict(r.base, type="not_null", columns=[col])]
        if r.unit == "percent" and len(r.constraints) == 1 and r.constraints[0].op == "<=":
            ratio = r.constraints[0].value / 100
            return [dict(r.base, type="null_ratio", column=col, max_ratio=ratio)]
        body = f"{self.dialect.quote(col)} IS NULL"
        return self.scalar_checks(r.base, self._rate(body, r.unit), r.constraints)

    def _missing_values(self, r: _Rule) -> list[dict[str, Any]]:
        name = self._column(r, "missingValues")
        if name is None:
            return []
        values = r.arguments.get("missingValues", r.rule.get("missingValues"))
        values = list(values) if isinstance(values, list) else [None]
        col = self.dialect.quote(name)
        texts = [v for v in values if v is not None]
        parts = [f"{col} IS NULL"]
        if texts:
            parts.append(f"{self.dialect.to_text(col)} IN ({self._text_list(texts)})")
        body = "(" + " OR ".join(parts) + ")"
        constraints = r.constraints or [_Constraint("==", 0)]
        return self.scalar_checks(r.base, self._rate(body, r.unit), constraints)

    def _invalid_values(self, r: _Rule) -> list[dict[str, Any]]:
        col = self._column(r, "invalidValues")
        if col is None:
            return []
        valid = r.arguments.get("validValues", r.rule.get("validValues"))
        pattern = r.arguments.get("pattern", r.rule.get("pattern"))
        quoted = self.dialect.quote(col)
        invalid_body: str | None
        if isinstance(valid, list) and valid:
            check = dict(r.base, type="allowed_values", column=col, values=list(valid))
            invalid_body = (
                f"{quoted} IS NOT NULL AND "
                f"{self.dialect.to_text(quoted)} NOT IN ({self._text_list(valid)})"
            )
        elif isinstance(pattern, str) and pattern:
            check = dict(r.base, type="regex", column=col, pattern=pattern)
            try:
                matches = self.dialect.regex_match(quoted, pattern, full=True)
                invalid_body = f"{quoted} IS NOT NULL AND NOT ({matches})"
            except UnsupportedFeatureError:
                invalid_body = None
        else:
            self.skip(
                r.where, r.label, "'invalidValues' needs arguments.validValues or arguments.pattern"
            )
            return []
        if _is_zero(r.constraints, r.unit):
            return [check]
        if r.unit == "percent" and len(r.constraints) == 1 and r.constraints[0].op == "<=":
            mostly = round(1 - r.constraints[0].value / 100, 12)
            if mostly <= 0:
                self.skip(r.where, r.label, "the threshold allows every value to be invalid")
                return []
            check["mostly"] = mostly
            return [check]
        if invalid_body is None:
            self.skip(
                r.where,
                r.label,
                f"pattern rules with thresholds need regular expressions in {self.dialect.name}",
            )
            return []
        return self.scalar_checks(r.base, self._rate(invalid_body, r.unit), r.constraints)

    def _duplicate_values(self, r: _Rule) -> list[dict[str, Any]]:
        if r.prop is not None:
            columns = [r.prop.column]
        else:
            names = r.arguments.get("properties", r.rule.get("properties"))
            if not isinstance(names, list) or not names:
                self.skip(
                    r.where,
                    r.label,
                    "a schema-level 'duplicateValues' rule needs arguments.properties",
                )
                return []
            columns = []
            for name in names:
                found = r.obj.find(str(name))
                columns.append(found.column if found else str(name))
        if _is_zero(r.constraints, r.unit):
            return [dict(r.base, type="unique", columns=columns)]
        q = self.dialect.quote
        cols = ", ".join(q(c) for c in columns)
        not_null = " AND ".join(f"{q(c)} IS NOT NULL" for c in columns)
        duplicates = (
            "SELECT COALESCE(SUM(dvf_n - 1), 0) FROM (SELECT COUNT(*) AS dvf_n FROM {table} "
            f"WHERE {not_null} GROUP BY {cols} HAVING COUNT(*) > 1) AS dvf_dups"
        )
        if r.unit == "percent":
            duplicates = (
                f"SELECT COALESCE(100.0 * ({duplicates}) / "
                "NULLIF((SELECT COUNT(*) FROM {table}), 0), 0)"
            )
        return self.scalar_checks(r.base, duplicates, r.constraints)

    def _row_count(self, r: _Rule) -> list[dict[str, Any]]:
        if not r.constraints:
            self.skip(r.where, r.label, "'rowCount' needs a threshold such as mustBeGreaterThan")
            return []
        if r.unit == "percent":
            self.skip(r.where, r.label, "'rowCount' cannot be a percentage")
            return []
        low: int | None = None
        high: int | None = None
        for c in r.constraints:
            if c.op == "==":
                low = high = math.ceil(c.value)
            elif c.op == ">":
                low = math.floor(c.value) + 1
            elif c.op == ">=":
                low = math.ceil(c.value)
            elif c.op == "<":
                high = math.ceil(c.value) - 1
            elif c.op == "<=":
                high = math.floor(c.value)
            else:
                return self.scalar_checks(r.base, "SELECT COUNT(*) FROM {table}", r.constraints)
        check = dict(r.base, type="row_count_range")
        if low is not None:
            check["min"] = max(low, 0)
        if high is not None:
            if high < 0 or (low is not None and high < low):
                self.skip(r.where, r.label, "the row count bounds cannot be met")
                return []
            check["max"] = high
        return [check]

    def sql_rule(
        self, rule: Mapping[str, Any], prop: ContractProperty | None, where: str, label: str, n: int
    ) -> list[dict[str, Any]]:
        query = rule.get("query")
        if not isinstance(query, str) or not query.strip():
            self.skip(where, label, "a sql rule needs a 'query'")
            return []
        constraints, problem = _constraints(rule, self.between)
        if problem:
            self.skip(where, label, problem)
            return []
        if not constraints:
            self.skip(where, label, "a sql rule needs an operator such as mustBe or mustBeLessThan")
            return []
        sql = re.sub(r"\$?\{(object|model|table)\}", "{table}", query.strip().rstrip(";"))
        if re.search(r"\$?\{(property|field|column)\}", sql):
            if prop is None:
                self.skip(
                    where, label, "the query uses {property} but the rule is not under a property"
                )
                return []
            sql = re.sub(
                r"\$?\{(property|field|column)\}", lambda _: self.dialect.quote(prop.column), sql
            )
        return self.scalar_checks(self.base(rule, f"sql_{n}"), sql, constraints)

    # ------------------------------------------------------------------ property constraints
    def property_checks(self, obj: ContractObject, prop: ContractProperty) -> list[dict[str, Any]]:
        where = f"{obj.name}.{prop.name}"
        col = prop.column
        checks: list[dict[str, Any]] = []
        options = dict(prop.options)
        logical = (prop.logical_type or "").lower()
        if prop.nested:
            self.skip(
                where, "nested properties", "nested object and array properties are not checked yet"
            )
        if options.get("minLength") is not None or options.get("maxLength") is not None:
            length: dict[str, Any] = {"type": "length", "column": col}
            if options.get("minLength") is not None:
                length["min"] = int(options.pop("minLength"))
            if options.get("maxLength") is not None:
                length["max"] = int(options.pop("maxLength"))
            if length.get("max") == 0:
                self.skip(where, "maxLength 0", "a maximum length of 0 cannot be checked")
            else:
                checks.append(length)
        options.pop("minLength", None)
        options.pop("maxLength", None)
        pattern = options.pop("pattern", None)
        if isinstance(pattern, str) and pattern:
            checks.append({"type": "regex", "column": col, "pattern": pattern, "name": "pattern"})
        fmt = options.pop("format", None)
        if fmt is not None:
            regex = FORMAT_PATTERNS.get(str(fmt).lower()) if logical in ("string", "") else None
            if regex is not None:
                checks.append(
                    {
                        "type": "regex",
                        "column": col,
                        "pattern": regex,
                        "name": f"format_{identifier(str(fmt))}",
                    }
                )
            elif logical in ("date", "timestamp", "time"):
                self.skip(
                    where,
                    f"format {fmt}",
                    "the format describes how date values are written, not a rule on stored values",
                )
            else:
                self.skip(
                    where, f"format {fmt}", f"no regular expression is known for format '{fmt}'"
                )
        checks += self._range(obj, prop, options, where)
        multiple = options.pop("multipleOf", None)
        if multiple is not None:
            number = _number(multiple)
            if isinstance(number, int) and number > 0:
                quoted = self.dialect.quote(col)
                checks.append(
                    {
                        "type": "expression",
                        "name": f"{identifier(col)}_multiple_of_{number}",
                        "expression": f"{quoted} IS NULL OR ({quoted} % {number}) = 0",
                    }
                )
            else:
                self.skip(
                    where, f"multipleOf {multiple}", "only positive whole numbers are supported"
                )
        for key in sorted(options):
            self.skip(where, f"logicalTypeOptions.{key}", "this option has no dvf check")
        if prop.enum:
            checks.append(
                {"type": "allowed_values", "column": col, "values": list(prop.enum), "name": "enum"}
            )
        for k, relation in enumerate(prop.relationships):
            checks += self.relationship(obj, relation, prop, where, k)
        return checks

    def _range(
        self, obj: ContractObject, prop: ContractProperty, options: dict[str, Any], where: str
    ) -> list[dict[str, Any]]:
        low, high = options.pop("minimum", None), options.pop("maximum", None)
        excl_low, excl_high = (
            options.pop("exclusiveMinimum", None),
            options.pop("exclusiveMaximum", None),
        )
        # JSON Schema draft 6 style: exclusiveMinimum is itself the bound.
        if excl_low is not None and not isinstance(excl_low, bool):
            low, excl_low = excl_low, True
        if excl_high is not None and not isinstance(excl_high, bool):
            high, excl_high = excl_high, True
        if low is None and high is None:
            return []
        col = prop.column
        bounds = [(low, bool(excl_low), "min"), (high, bool(excl_high), "max")]
        present = [b for b in bounds if b[0] is not None]
        if len(present) == 2 and bounds[0][1] != bounds[1][1]:
            # One inclusive and one exclusive bound: one range check each.
            return [
                {
                    "type": "range",
                    "column": col,
                    key: value,
                    "inclusive": not exclusive,
                    "name": f"range_{key}",
                }
                for value, exclusive, key in present
            ]
        check: dict[str, Any] = {"type": "range", "column": col}
        for value, _, key in present:
            check[key] = (
                value
                if isinstance(value, int | float) and not isinstance(value, bool)
                else str(value)
            )
        if present[0][1]:
            check["inclusive"] = False
        return [check]

    def relationship(
        self,
        obj: ContractObject,
        relation: Mapping[str, Any],
        prop: ContractProperty | None,
        where: str,
        k: int,
    ) -> list[dict[str, Any]]:
        kind = str(relation.get("type") or "foreignKey")
        label = f"relationship {relation.get('id') or k + 1}"
        if kind != "foreignKey":
            self.skip(
                where, label, f"relationship type '{kind}' is not supported (only foreignKey)"
            )
            return []
        to = relation.get("to")
        source = relation.get("from") if prop is None else prop.column
        raw_targets = to if isinstance(to, list) else [to]
        sources = source if isinstance(source, list) else [source]
        targets = [t for t in raw_targets if isinstance(t, str) and "." in t]
        if not targets or len(targets) != len(raw_targets) or len(sources) != len(targets):
            self.skip(where, label, "write 'to' as object.property (with matching 'from' columns)")
            return []
        parents = {t.rsplit(".", 1)[0] for t in targets}
        if len(parents) != 1:
            self.skip(where, label, "all 'to' columns must belong to one object")
            return []
        parent_name = parents.pop()
        child_cols: list[str] = []
        for s in sources:
            name = str(s).rsplit(".", 1)[-1]
            found = obj.find(name)
            child_cols.append(found.column if found else name)
        return [
            {
                "type": "referential_integrity",
                "name": identifier(str(relation.get("id") or f"fk_{parent_name}"), "fk"),
                "columns": child_cols,
                "parent": {
                    "table": parent_name,
                    "columns": [t.rsplit(".", 1)[-1] for t in targets],
                },
                "_parent_object": parent_name,
            }
        ]


# ---------------------------------------------------------------------- connections


def _server_connection(
    contract: Contract, server_name: str | None
) -> tuple[ConnectionSpec | None, list[str], Unsupported | None]:
    servers = contract.servers
    if not servers:
        return None, ["the contract lists no servers"], None
    chosen = servers[0]
    if server_name is not None:
        found = next((s for s in servers if str(s.get("server")) == server_name), None)
        if found is None:
            raise ConfigError(
                f"Server '{server_name}' is not in the contract.",
                hint="Servers: " + ", ".join(str(s.get("server")) for s in servers),
            )
        chosen = found
    name = identifier(str(chosen.get("server") or "source"), "source")
    server_type = str(chosen.get("type") or "").lower()
    conn_type = _SERVER_TYPES.get(server_type)
    env = re.sub(r"[^A-Z0-9]+", "_", name.upper())
    notes: list[str] = []
    if conn_type is None:
        return (
            None,
            [],
            Unsupported(
                f"servers.{name}",
                f"type {server_type or 'missing'}",
                "dvf has no connector for this server type",
            ),
        )
    config: dict[str, Any] = {"type": conn_type}

    def copy(*pairs: tuple[str, str]) -> None:
        for src, dst in pairs:
            if chosen.get(src) is not None:
                config[dst] = chosen[src]

    if conn_type in ("postgres", "mysql", "mssql", "oracle", "redshift"):
        copy(("host", "host"), ("port", "port"), ("database", "database"), ("schema", "schema"))
        if conn_type == "oracle":
            copy(("serviceName", "database"))
        config.setdefault("host", f"${{env:{env}_HOST}}")
        config["user"] = f"${{env:{env}_USER}}"
        config["password"] = f"${{secret:env:{env}_PASSWORD}}"
        notes.append(
            f"Set {env}_USER and {env}_PASSWORD (and {env}_HOST if the contract has no host)."
        )
    elif conn_type == "snowflake":
        copy(
            ("account", "account"),
            ("database", "database"),
            ("schema", "schema"),
            ("warehouse", "warehouse"),
        )
        config.setdefault("account", f"${{env:{env}_ACCOUNT}}")
        config["user"] = f"${{env:{env}_USER}}"
        config["password"] = f"${{secret:env:{env}_PASSWORD}}"
        notes.append(f"Set {env}_USER and {env}_PASSWORD for Snowflake.")
    elif conn_type == "bigquery":
        copy(("project", "project"), ("dataset", "dataset"), ("location", "location"))
    elif conn_type == "databricks":
        copy(("host", "server_hostname"), ("catalog", "catalog"), ("schema", "schema"))
        config.setdefault("server_hostname", f"${{env:{env}_HOST}}")
        config["http_path"] = f"${{env:{env}_HTTP_PATH}}"
        config["access_token"] = f"${{secret:env:{env}_TOKEN}}"
        notes.append(f"Set {env}_HTTP_PATH and {env}_TOKEN for Databricks.")
    elif conn_type == "duckdb":
        copy(("database", "database"))
    else:  # files
        location = str(chosen.get("path") or chosen.get("location") or ".")
        config["path"] = location
        if chosen.get("format"):
            config["format"] = str(chosen["format"]).lower()
        if "://" in location:
            config["options"] = {"extensions": ["httpfs"]}
    return (
        ConnectionSpec(name=name, open_config=config, written_config=config, notes=notes),
        notes,
        None,
    )


def _dialect_for(connection_type: str, registry: Registry) -> Dialect:
    entry = registry.find(PluginKind.CONNECTOR, connection_type)
    name = getattr(entry.implementation, "dialect", "") if entry is not None and entry.ready else ""
    try:
        return get_dialect(name or {"files": "duckdb"}.get(connection_type, connection_type))
    except UnsupportedFeatureError:
        return Dialect()


def _file_locator(conn: ConnectionSpec, obj: ContractObject, objects: int) -> dict[str, Any]:
    root = str(conn.written_config.get("path", "."))
    fmt = conn.written_config.get("format")
    for placeholder in ("{model}", "{object}", "{table}"):
        if placeholder in root:
            return {"path": Path(root.replace(placeholder, obj.table)).name}
    if objects == 1 and Path(root).suffix:
        return {"path": Path(root).name}
    return {"path": f"{obj.table}.{fmt}" if fmt else obj.table}


# ---------------------------------------------------------------------- entry point


def import_contract(
    path: Path,
    *,
    server: str | None = None,
    loaded: LoadedConfig | None = None,
    config_path: Path | None = None,
    connection: str | None = None,
    location: str | None = None,
    suite_name: str | None = None,
    project_name: str | None = None,
    conformance: bool = True,
    between: Between = "exclusive",
    output: Path | None = None,
    overwrite: bool = False,
    registry: Registry | None = None,
) -> ContractImportResult:
    """Convert the contract at ``path`` into a configuration and, with ``output``, write it.

    The connection comes from ``loaded`` (``connection`` names it), from ``location`` (a URL,
    database file, or folder), from the contract's ``servers`` (``server`` picks one), or is a
    placeholder.
    """
    registry = registry or (loaded.registry if loaded is not None else get_registry())
    contract = load_contract(path)
    output_dir = (output.parent if output is not None else Path.cwd()).resolve()
    notes = list(contract.notes)
    unsupported: list[Unsupported] = []
    spec: ConnectionSpec | None = None
    if loaded is not None:
        assert config_path is not None
        names = list(loaded.config.connections)
        spec = spec_from_config(
            loaded,
            config_path,
            connection or (names[0] if names else "source"),
            output_dir=output_dir,
        )
    elif location is not None:
        spec = spec_from_location(location, name="source", side="source", output_dir=output_dir)
    else:
        spec, server_notes, problem = _server_connection(contract, server)
        notes += server_notes
        if problem is not None:
            unsupported.append(problem)
        if spec is not None and spec.type in ("duckdb", "files"):
            field_name = "database" if spec.type == "duckdb" else "path"
            value = spec.written_config.get(field_name)
            if isinstance(value, str) and "://" not in value and not Path(value).is_absolute():
                # Contract paths are relative to the contract file.
                rebased = relative_path(path.parent / value, output_dir)
                spec.written_config[field_name] = rebased
    if spec is None:
        placeholder = {"type": "duckdb", "database": "./data.duckdb"}
        spec = ConnectionSpec(
            "source",
            placeholder,
            placeholder,
            ["The connection is a placeholder: replace it with the system that holds the data."],
        )
    notes += [n for n in spec.notes if n not in notes]
    dialect = _dialect_for(spec.type, registry)
    converter = _Converter(dialect, between)
    contract_ref = relative_path(path, output_dir)
    is_files = spec.type == "files"
    tables = Seq()
    table_names = UniqueNames()
    total_checks = 0
    object_tables = {o.name.lower(): o.table for o in contract.objects}
    for obj in contract.objects:
        entry = Map()
        entry.add("name", table_names.take(obj.name, "table"))
        if obj.description:
            entry.add("description", obj.description.strip())
        locator = (
            _file_locator(spec, obj, len(contract.objects)) if is_files else {"table": obj.table}
        )
        entry.add("source", Flow(locator))
        pk = obj.primary_key()
        if pk:
            entry.add("keys", pk, inline="primary key from the contract")
        checks: list[tuple[dict[str, Any], str | None]] = []
        if conformance:
            checks.append(
                (
                    {"type": "contract", "contract": contract_ref, "object": obj.name},
                    "SCH-12 schema conformance",
                )
            )
        required = [
            p.column for p in obj.properties if (p.required or p.primary_key) and not p.nested
        ]
        if required:
            checks.append(
                ({"type": "not_null", "columns": required}, "required and primary key properties")
            )
        if pk:
            checks.append(({"type": "unique", "columns": pk}, "primary key"))
        for prop in obj.properties:
            if prop.unique and [prop.column] != pk:
                checks.append(
                    ({"type": "unique", "columns": [prop.column]}, f"{prop.name}: unique")
                )
            for check in converter.property_checks(obj, prop):
                checks.append(
                    (
                        check,
                        f"{prop.name}: logicalTypeOptions"
                        if check.get("type") != "referential_integrity"
                        else f"{prop.name}: relationship",
                    )
                )
            for n, rule in enumerate(prop.quality, start=1):
                for check in converter.rule(rule, obj, prop, n):
                    checks.append((check, f"{prop.name}: quality {converter.label(rule)}"))
        for n, rule in enumerate(obj.quality, start=1):
            for check in converter.rule(rule, obj, None, n):
                checks.append((check, f"quality {converter.label(rule)}"))
        for k, relation in enumerate(obj.relationships):
            for check in converter.relationship(obj, relation, None, obj.name, k):
                checks.append((check, "relationship"))
        seq = Seq()
        seen_ids: set[str] = set()
        for check, origin in checks:
            parent_object = check.pop("_parent_object", None)
            if parent_object is not None:
                check["parent"]["table"] = object_tables.get(
                    str(parent_object).lower(), parent_object
                )
            ident = (
                f"{check['type']}|{check.get('columns') or check.get('column')}|{check.get('name')}"
            )
            base_name = check.get("name")
            n = 2
            while ident in seen_ids:
                check["name"] = f"{base_name or check['type']}_{n}"
                ident = (
                    f"{check['type']}|{check.get('columns') or check.get('column')}|{check['name']}"
                )
                n += 1
            seen_ids.add(ident)
            check = _ordered(check)
            seq.add(Flow(check) if len(str(check)) < 140 else Map(check), inline=origin)
        total_checks += len(seq)
        if len(seq):
            entry.add("checks", seq)
        comments = [
            f"Contract object '{obj.name}'"
            + (f" ({obj.physical_type})" if obj.physical_type else "")
        ]
        skipped = [
            u
            for u in converter.unsupported
            if u.where == obj.name or u.where.startswith(f"{obj.name}.")
        ]
        if skipped:
            comments.append("Not converted (dvf cannot run these):")
            comments += [f"  - {u.where}: {u.item}: {u.reason}" for u in skipped]
        tables.add(entry, comment=comments)
    unsupported += converter.unsupported
    root = Map()
    root.add("version", 1)
    project = Map().add("name", project_name or identifier(contract.name or path.stem, "contract"))
    meta = {
        k: v
        for k, v in (
            ("contract_id", contract.id),
            ("contract_version", contract.version),
            ("odcs_api_version", contract.api_version),
        )
        if v
    }
    if meta:
        project.add("metadata", Flow(meta))
    root.add("project", project)
    root.add(
        "connections",
        Map().add(
            spec.name,
            Flow(spec.written_config),
            inline="REVIEW: placeholder" if spec.notes and "placeholder" in spec.notes[0] else None,
        ),
    )
    suite = Map().add("name", suite_name or identifier(contract.name or path.stem, "contract"))
    suite.add("source", spec.name)
    suite.add("tables", tables if len(tables) else [])
    root.add("suites", Seq().add(suite))
    header = generated_header(
        "dvf import-contract",
        f"Contract: {path.as_posix()}"
        + (f" (ODCS {contract.api_version})" if contract.api_version else "")
        + ".",
        f"mustBeBetween bounds are read as {between} (ODCS defines them like mustBeGreaterThan and mustBeLessThan).",
        "Review the connection and any rules listed as not converted.",
    )
    footer: list[str] = []
    if unsupported:
        footer.append(f"{len(unsupported)} contract element(s) were not converted:")
        footer += [f"  - {u}" for u in unsupported]
    footer += [f"Note: {n}" for n in notes]
    document = Document(root, header=header, footer=footer)
    _validate(document, registry, output_dir)
    result = ContractImportResult(document, len(contract.objects), total_checks, unsupported, notes)
    if output is not None:
        write_output(result.text, output, overwrite=overwrite)
        result.output = output
    return result


_FIRST = ("type", "name", "column", "columns")
_LAST = ("severity", "description", "details")


def _ordered(check: dict[str, Any]) -> dict[str, Any]:
    """``check`` with its identifying keys first and its annotations last."""
    first = {k: check[k] for k in _FIRST if k in check}
    last = {k: check[k] for k in _LAST if k in check}
    middle = {k: v for k, v in check.items() if k not in first and k not in last}
    return {**first, **middle, **last}


def _validate(document: Document, registry: Registry, base_dir: Path) -> None:
    options = LoadOptions(strict=False, resolve_secrets=False, env_overrides=False)
    try:
        load_config_data(plain(document), options, registry=registry, base_dir=base_dir)
    except ConfigError as exc:
        raise ConfigError(
            "The contract produced an invalid configuration; please report this.",
            exc.diagnostics,
        ) from exc


__all__ = [
    "FORMAT_PATTERNS",
    "LIBRARY_METRICS",
    "OPERATORS",
    "ContractImportResult",
    "Unsupported",
    "import_contract",
]
