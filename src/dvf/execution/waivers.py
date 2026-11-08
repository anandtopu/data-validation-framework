"""Waivers: accepted, documented differences that do not fail a run until they expire.

A waiver matches checks by ID (glob patterns allowed) and, optionally, one column. An active
waiver turns a failed or warning result into ``waived``; for a column waiver on a column
comparison, the result is waived only when every differing column is waived. Expired waivers
never apply: the failure stands and the result says the waiver expired, so it gets reviewed.
"""

from __future__ import annotations

import threading
from datetime import date
from fnmatch import fnmatchcase

from dvf.config.models import WaiverConfig
from dvf.core.enums import Status
from dvf.models.results import CheckResult, WaiverApplied, WaiverStatus

_WAIVABLE = (Status.FAILED, Status.WARNING)


def _matches(pattern: str, value: str) -> bool:
    return fnmatchcase(value.lower(), pattern.lower())


def _applied(waiver: WaiverConfig, *, expired: bool) -> WaiverApplied:
    return WaiverApplied(
        selector=waiver.check,
        column=waiver.column,
        reason=waiver.reason,
        ticket=waiver.ticket,
        expires=waiver.expires.isoformat() if waiver.expires else None,
        approved_by=waiver.approved_by,
        expired=expired,
    )


def _label(waiver: WaiverConfig) -> str:
    ticket = f" ({waiver.ticket})" if waiver.ticket else ""
    return f"'{waiver.check}'{ticket}"


class WaiverBook:
    """Applies a configuration's waivers to results and remembers how each one was used."""

    def __init__(self, waivers: list[WaiverConfig], today: date | None = None) -> None:
        self.waivers = list(waivers)
        self.today = today or date.today()
        self._applied: dict[int, list[str]] = {i: [] for i in range(len(self.waivers))}
        self._expired_hits: dict[int, list[str]] = {i: [] for i in range(len(self.waivers))}
        self._lock = threading.Lock()

    def _column_hit(self, waiver: WaiverConfig, result: CheckResult) -> bool:
        assert waiver.column is not None
        if result.column_mismatches:
            return any(_matches(waiver.column, m.column) for m in result.column_mismatches)
        return any(_matches(waiver.column, c) for c in result.columns)

    def apply(self, result: CheckResult) -> CheckResult:
        """Return ``result`` with matching waivers applied (a copy when anything changed)."""
        if not self.waivers or result.status not in _WAIVABLE:
            return result
        matching = [
            (i, w)
            for i, w in enumerate(self.waivers)
            if _matches(w.check, result.check_id)
            and (w.column is None or self._column_hit(w, result))
        ]
        if not matching:
            return result
        active = [(i, w) for i, w in matching if not w.is_expired(self.today)]
        expired = [(i, w) for i, w in matching if w.is_expired(self.today)]
        update: dict[str, object] = {}
        hints = list(result.hints)
        with self._lock:
            for i, _ in expired:
                self._expired_hits[i].append(result.check_id)
        for _, w in expired:
            hints.append(
                f"Waiver {_label(w)} expired on {w.expires}; the failure stands until it is "
                "renewed or the difference is fixed."
            )
        whole = [(i, w) for i, w in active if w.column is None]
        if whole:
            i, w = whole[0]
            update.update(status=Status.WAIVED, waiver=_applied(w, expired=False))
            with self._lock:
                self._applied[i].append(result.check_id)
        elif active and result.column_mismatches:
            columns = [(i, w) for i, w in active if w.column is not None]
            mismatches = []
            used: set[int] = set()
            for m in result.column_mismatches:
                hit = next(((i, w) for i, w in columns if _matches(w.column or "", m.column)), None)
                if hit is not None:
                    used.add(hit[0])
                mismatches.append(m.model_copy(update={"waived": hit is not None}))
            update["column_mismatches"] = mismatches
            with self._lock:
                for i in used:
                    self._applied[i].append(result.check_id)
            first = next(w for i, w in columns if i in used)
            if all(m.waived for m in mismatches):
                update.update(status=Status.WAIVED, waiver=_applied(first, expired=False))
            else:
                waived = [m.column for m in mismatches if m.waived]
                hints.append(f"Waived columns: {', '.join(waived)}; other columns still differ.")
        elif active:
            i, w = active[0]
            update.update(status=Status.WAIVED, waiver=_applied(w, expired=False))
            with self._lock:
                self._applied[i].append(result.check_id)
        if expired and "waiver" not in update:
            update["waiver"] = _applied(expired[0][1], expired=True)
        update["hints"] = hints
        return result.model_copy(update=update)

    def statuses(self) -> list[WaiverStatus]:
        """How each waiver was used in this run."""
        out = []
        for i, w in enumerate(self.waivers):
            if w.is_expired(self.today):
                status, checks = "expired", self._expired_hits[i]
            elif self._applied[i]:
                status, checks = "applied", self._applied[i]
            else:
                status, checks = "unused", []
            out.append(
                WaiverStatus(
                    selector=w.check,
                    column=w.column,
                    reason=w.reason,
                    ticket=w.ticket,
                    approved_by=w.approved_by,
                    expires=w.expires.isoformat() if w.expires else None,
                    status=status,
                    checks=sorted(set(checks)),
                )
            )
        return out
