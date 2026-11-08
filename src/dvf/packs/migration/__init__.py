"""Migration pack: migration validations MIG-01 to MIG-14.

``register`` is called by :func:`dvf.packs.register_packs`. It registers the migration checks
and the ``signoff`` report format. The watermark window (MIG-02) and the in-flight exclusion
(MIG-09) are table options applied by the runner (see :mod:`dvf.packs.migration.windows`);
baseline capture (MIG-14) and run-to-run comparison (MIG-08) are CLI commands in
:mod:`dvf.cli.migration`. See ``docs/migration.md``.
"""

from __future__ import annotations

import inspect
from typing import Any

from dvf.plugins.registry import PluginKind, Registry


def _checks() -> dict[str, Any]:
    from dvf.packs.migration.audit import AuditColumns
    from dvf.packs.migration.baseline import BaselineMetrics
    from dvf.packs.migration.charset import CharsetConversion
    from dvf.packs.migration.deletes import DeletePropagation
    from dvf.packs.migration.delta import CdcApplied, LateArriving
    from dvf.packs.migration.dst import DstEdgeCases
    from dvf.packs.migration.idempotency import Idempotency
    from dvf.packs.migration.keymap import KeyMap
    from dvf.packs.migration.scd2 import Scd2
    from dvf.packs.migration.scope import ScopeReconciliation
    from dvf.packs.migration.substitution import SubstitutionRules

    return {
        "scope_reconciliation": ScopeReconciliation,  # MIG-01
        "cdc_applied": CdcApplied,  # MIG-02
        "late_arriving": LateArriving,  # MIG-02
        "idempotency": Idempotency,  # MIG-03
        "scd2": Scd2,  # MIG-04
        "delete_propagation": DeletePropagation,  # MIG-05
        "key_map": KeyMap,  # MIG-06
        "audit_columns": AuditColumns,  # MIG-07
        "charset_conversion": CharsetConversion,  # MIG-10
        "dst_edge_cases": DstEdgeCases,  # MIG-11
        "substitution_rules": SubstitutionRules,  # MIG-12
        "baseline_metrics": BaselineMetrics,  # MIG-14
    }


CHECK_TYPES: tuple[str, ...] = (
    "scope_reconciliation",
    "cdc_applied",
    "late_arriving",
    "idempotency",
    "scd2",
    "delete_propagation",
    "key_map",
    "audit_columns",
    "charset_conversion",
    "dst_edge_cases",
    "substitution_rules",
    "baseline_metrics",
)
"""Check types this pack registers."""


def register(registry: Registry) -> None:
    """Register the migration checks and the ``signoff`` report format."""
    from dvf.packs.migration.signoff import SignoffReporter

    for name, implementation in _checks().items():
        registry.register(
            PluginKind.CHECK,
            name,
            implementation,
            description=_summary(implementation),
            origin="builtin",
        )
    registry.register(
        PluginKind.REPORTER,
        "signoff",
        SignoffReporter,
        description=_summary(SignoffReporter),
        origin="builtin",
    )


def _summary(implementation: Any) -> str:
    """The first paragraph of a docstring, on one line."""
    doc = inspect.cleandoc(implementation.__doc__ or "")
    return " ".join(doc.split("\n\n", 1)[0].split())


__all__ = ["CHECK_TYPES", "register"]
