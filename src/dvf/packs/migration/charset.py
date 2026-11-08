"""MIG-10: character set conversion (EBCDIC, Latin-1, or code pages to UTF-8).

SQL cannot decide whether a value is mojibake or fits a legacy code page, so the database groups
each column's values (``GROUP BY``, which also counts them) and dvf streams the distinct values to
Python in batches, keeping only counters and a few samples: memory stays bounded however large
the table is.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Iterable
from typing import Any

import pyarrow as pa

from dvf.checks.context import CheckContext, CheckOutcome, mostly_passes, pct
from dvf.checks.pair import masked_evidence
from dvf.core.enums import Dimension
from dvf.core.types import LogicalType
from dvf.models.results import MetricComparison
from dvf.packs.migration import config as cfg
from dvf.plugins.base import Check

BATCH_SIZE = 20_000

LABELS = {
    "replacement_char": "U+FFFD replacement characters",
    "mojibake": "mojibake (UTF-8 bytes read as a single-byte code page)",
    "control_chars": "control characters",
    "round_trip": "characters the legacy code page cannot represent",
}


def _continuation_chars() -> str:
    """How UTF-8 continuation bytes (0x80 to 0xBF) look when decoded as cp1252 or Latin-1."""
    chars: list[str] = []
    for byte in range(0x80, 0xC0):
        raw = bytes([byte])
        try:
            chars.append(raw.decode("cp1252"))
        except UnicodeDecodeError:
            chars.append(raw.decode("latin-1"))
    return "".join(dict.fromkeys(chars))


_CONT = "[" + re.escape(_continuation_chars()) + "]"
_MOJIBAKE = re.compile("(?:[Â-ß]" + _CONT + "|[à-ï]" + _CONT + "{2}|[ð-ô]" + _CONT + "{3})")


def _to_bytes(text: str) -> bytes | None:
    """``text`` encoded as the single-byte code page it was misread as (cp1252, else Latin-1)."""
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            try:
                out += ch.encode("latin-1")
            except UnicodeEncodeError:
                return None
    return bytes(out)


def is_mojibake(value: str, extra_patterns: Iterable[str] = ()) -> bool:
    """Whether ``value`` contains UTF-8 text that was decoded as cp1252 or Latin-1 (for example
    ``'CafÃ©'`` for ``'Café'``), or one of ``extra_patterns``."""
    if any(p and p in value for p in extra_patterns):
        return True
    for match in _MOJIBAKE.finditer(value):
        raw = _to_bytes(match.group(0))
        if raw is None:
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return True
    return False


def control_chars(value: str, allowed: frozenset[str]) -> bool:
    return any(unicodedata.category(ch) == "Cc" and ch not in allowed for ch in value)


def round_trips(value: str, code_page: str, form: str) -> bool:
    """Whether ``value`` survives encoding to ``code_page`` and back unchanged."""
    text = value
    if form == "NFC":
        text = unicodedata.normalize("NFC", value)
    elif form == "NFKC":
        text = unicodedata.normalize("NFKC", value)
    try:
        return text.encode(code_page).decode(code_page) == text
    except UnicodeError:
        return False


def violations(value: Any, config: cfg.CharsetConversionCheck, rules: list[str]) -> list[str]:
    """The rules ``value`` violates."""
    text = value if isinstance(value, str) else str(value)
    found: list[str] = []
    if "replacement_char" in rules and "�" in text:
        found.append("replacement_char")
    if "mojibake" in rules and is_mojibake(text, config.mojibake_patterns):
        found.append("mojibake")
    if "control_chars" in rules and control_chars(text, frozenset(config.allowed_control)):
        found.append("control_chars")
    if (
        "round_trip" in rules
        and config.code_page is not None
        and not round_trips(text, config.code_page, config.unicode_form)
    ):
        found.append("round_trip")
    return found


def _escape(value: str, limit: int = 200) -> str:
    """A sample value with invisible characters made visible."""
    shown = "".join(
        f"\\x{ord(ch):02x}" if unicodedata.category(ch) == "Cc" else ch for ch in value[:limit]
    )
    return shown + ("…" if len(value) > limit else "")


class CharsetConversion(Check):
    """Text survived the character set conversion: no U+FFFD replacement characters, mojibake,
    or control characters, and every value fits the legacy code page (MIG-10)."""

    dimension = Dimension.VALIDITY
    Config = cfg.CharsetConversionCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.CharsetConversionCheck)
        rules = config.effective_rules
        d = ctx.dialect
        counts = dict.fromkeys(rules, 0)
        distinct_bad = dict.fromkeys(rules, 0)
        total_rows = failing_rows = 0
        unchecked: dict[str, int] = {}
        samples: list[list[Any]] = []
        metrics: list[MetricComparison] = []
        limit = max(ctx.sample_size, ctx.extract_rows)
        for name in config.columns:
            col = ctx.col(name)
            found_col = ctx.schema.column(name) if ctx.schema is not None else None
            is_text = found_col is not None and found_col.type.logical is LogicalType.STRING
            value = col if is_text else d.to_text(col)
            sql = (
                f"SELECT {value} AS v, COUNT(*) AS n\n{ctx.from_where(f'{col} IS NOT NULL')}\n"
                f"GROUP BY {value}"
            )
            started = time.perf_counter()
            seen = column_rows = column_bad = 0
            for batch in ctx.connector.iter_batches(sql, BATCH_SIZE):
                values, ns = batch.column(0).to_pylist(), batch.column(1).to_pylist()
                for value, n in zip(values, ns, strict=True):
                    if value is None:
                        continue
                    rows = int(n or 0)
                    seen += 1
                    if seen > config.max_distinct:
                        unchecked[name] = unchecked.get(name, 0) + rows
                        continue
                    column_rows += rows
                    found = violations(value, config, rules)
                    if not found:
                        continue
                    column_bad += rows
                    for rule in found:
                        counts[rule] += rows
                        distinct_bad[rule] += 1
                    if len(samples) < limit:
                        samples.append([name, _escape(str(value)), ", ".join(found), rows])
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            ctx.recorder.add(ctx.connector, sql, ctx.side, elapsed, seen)
            total_rows += column_rows
            failing_rows += column_bad
            metrics.append(
                MetricComparison(
                    metric="charset_violations",
                    column=name,
                    source=column_rows,
                    target=column_bad,
                    passed=column_bad == 0,
                    note="rows checked, rows with violations",
                )
            )
        passed = mostly_passes(failing_rows, total_rows, config.mostly)
        parts = [f"{counts[r]:,} rows with {LABELS[r]}" for r in rules if counts[r]]
        if parts:
            share = pct(failing_rows, total_rows)
            message = "; ".join(parts) + (
                f" ({failing_rows:,} of {total_rows:,} values, {share:.4g}%)" if share else ""
            )
        else:
            message = f"all {total_rows:,} non-NULL values pass ({', '.join(rules)})"
        hints: list[str] = []
        if counts.get("mojibake"):
            hints.append(
                "Mojibake usually means UTF-8 data was read as cp1252 or Latin-1 during "
                "extraction; fix the extract's encoding rather than the target."
            )
        if counts.get("round_trip"):
            hints.append(
                f"Values contain characters {config.code_page} cannot hold, so they did not come "
                "from the legacy system as-is: check for substitutions or double conversion."
            )
        if unchecked:
            hints.append(
                "Some values were not checked because a column has more than "
                f"{config.max_distinct:,} distinct values: {unchecked}"
            )
        evidence_samples = extract = None
        if samples:
            table = pa.table(
                {
                    "column": pa.array([s[0] for s in samples], pa.string()),
                    "value": pa.array([s[1] for s in samples], pa.string()),
                    "rules": pa.array([s[2] for s in samples], pa.string()),
                    "rows": pa.array([s[3] for s in samples], pa.int64()),
                }
            )
            evidence_samples, extract = masked_evidence(
                table, ctx.sample_size, ctx.extract_rows, ctx.privacy
            )
            if evidence_samples is not None and ctx.privacy.mask_pii:
                pii = [c for c in config.columns if ctx.privacy.is_pii(c)]
                if pii:
                    evidence_samples = evidence_samples.model_copy(
                        update={
                            "rows": [
                                [r[0], ctx.privacy.mask(str(r[0]), r[1]), *r[2:]]
                                if r[0] in pii
                                else r
                                for r in evidence_samples.rows
                            ]
                        }
                    )
        return CheckOutcome(
            passed=passed,
            message=message,
            observed={r: counts[r] for r in rules},
            expected=0 if config.mostly >= 1 else f"at least {config.mostly:.4%} valid",
            unexpected_count=failing_rows,
            total_count=total_rows,
            samples=evidence_samples,
            extract=extract,
            metrics=metrics,
            hints=hints,
            details={
                "rules": rules,
                "violations": counts,
                "distinct_values_with_violations": distinct_bad,
                "code_page": config.code_page,
                "rows_not_checked": unchecked,
            },
        )


__all__ = ["CharsetConversion", "is_mojibake", "round_trips", "violations"]
