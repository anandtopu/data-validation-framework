"""Run subsets of a configuration: ``dvf run --only <selector>``.

A selector is ``kind:pattern``; patterns are globs. Selectors of the same kind are alternatives
(OR); different kinds must all match (AND).

======== =====================================================================
suite    suite name
table    table name, or ``suite.table``
check    check type (``not_null``, ``compare.row_count``) or check ID
tag      a tag on the suite, table, or check
severity minimum severity of checks (``warning`` selects warning and above)
======== =====================================================================

A bare pattern without a kind matches ``suite.table`` or a check ID.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from dvf.core.enums import Severity
from dvf.core.errors import ConfigError

KINDS = ("suite", "table", "check", "tag", "severity")


def _match(value: str, patterns: list[str]) -> bool:
    lowered = value.lower()
    return any(fnmatchcase(lowered, p.lower()) for p in patterns)


@dataclass
class Selectors:
    """Parsed ``--only`` selectors."""

    by_kind: dict[str, list[str]] = field(default_factory=dict)
    bare: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    """The selectors as given (saved with checkpoints so a resumed run selects the same)."""

    @classmethod
    def parse(cls, items: list[str] | None) -> Selectors:
        by_kind: dict[str, list[str]] = defaultdict(list)
        bare: list[str] = []
        for item in items or []:
            kind, sep, pattern = item.partition(":")
            if not sep:
                bare.append(item)
                continue
            kind = kind.strip().lower()
            if kind not in KINDS:
                raise ConfigError(
                    f"Unknown selector kind '{kind}' in '{item}'; use one of: {', '.join(KINDS)}"
                )
            if kind == "severity":
                try:
                    Severity(pattern.strip().lower())
                except ValueError:
                    raise ConfigError(
                        f"Unknown severity '{pattern}' in selector '{item}'"
                    ) from None
            by_kind[kind].append(pattern.strip())
        return cls(dict(by_kind), bare, list(items or []))

    @property
    def empty(self) -> bool:
        return not self.by_kind and not self.bare

    def table_selected(self, suite: str, table: str, tags: list[str]) -> bool:
        if "suite" in self.by_kind and not _match(suite, self.by_kind["suite"]):
            return False
        return "table" not in self.by_kind or (
            _match(table, self.by_kind["table"])
            or _match(f"{suite}.{table}", self.by_kind["table"])
        )

    def check_selected(
        self,
        suite: str,
        table: str,
        check_type: str,
        check_id: str,
        tags: list[str],
        severity: Severity,
    ) -> bool:
        if not self.table_selected(suite, table, tags):
            return False
        if "check" in self.by_kind and not (
            _match(check_type, self.by_kind["check"]) or _match(check_id, self.by_kind["check"])
        ):
            return False
        if "tag" in self.by_kind and not any(_match(t, self.by_kind["tag"]) for t in tags):
            return False
        if "severity" in self.by_kind:
            minimum = min(Severity(s.lower()).rank for s in self.by_kind["severity"])
            if severity.rank < minimum:
                return False
        return not self.bare or _match(f"{suite}.{table}", self.bare) or _match(check_id, self.bare)
