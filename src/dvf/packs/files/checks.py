"""File-level checks: existence, counts, names, sizes, checksums, freshness, control records,
schema on read, and bad records (BUILD_PROMPT.md 7.7 and REC-09).

They run on datasets of a ``files`` connection (any connector with a ``list_files`` method), in
single-dataset and comparison suites, on the side the check's ``side`` selects.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar

from dvf.checks.context import CheckContext, CheckOutcome, mostly_passes, pct
from dvf.config.base import parse_duration
from dvf.core.enums import Dimension
from dvf.core.errors import PlanError
from dvf.core.types import CanonicalType, parse_native_type
from dvf.models.results import MetricComparison, SampleSet
from dvf.packs.files import config as cfg
from dvf.packs.files import storage
from dvf.packs.files.storage import FileInfo
from dvf.plugins.base import Check, Connector

_LISTED = 50
"""Files listed in a result's details."""


# ============================================================================ helpers


def _lister(connector: Connector, check: str) -> Any:
    lister = getattr(connector, "list_files", None)
    if lister is None:
        raise PlanError(
            f"Check '{check}' reads files, but connection '{connector.name}' "
            f"({connector.plugin_name or type(connector).__name__}) is not a files connection.",
            hint="Run file checks on datasets of a connection with type: files.",
        )
    return lister


def _size(n: int) -> str:
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{n:,} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n:,} bytes"  # pragma: no cover - loop always returns


def _names(files: list[FileInfo], limit: int = 5) -> str:
    shown = ", ".join(f.name for f in files[:limit])
    return shown + (f" and {len(files) - limit:,} more" if len(files) > limit else "")


def _file_samples(ctx: CheckContext, columns: list[str], rows: list[list[Any]]) -> SampleSet | None:
    if not rows or ctx.sample_size <= 0 or ctx.privacy.strict:
        return None
    return SampleSet(
        columns=columns,
        rows=rows[: ctx.sample_size],
        masked=False,
        truncated=len(rows) > ctx.sample_size,
    )


def _file_rows(files: list[FileInfo]) -> list[list[Any]]:
    return [[f.name, f.size, f.modified.isoformat() if f.modified else None] for f in files]


_FILE_COLUMNS = ["file", "size_bytes", "modified"]


class FileCheck(Check):
    """Base class of checks about the files behind a dataset."""

    requires_dataset: ClassVar[bool] = False
    """The check is meaningful even when the dataset matches no file (for example
    ``file_exists``), so the runner may evaluate it without a readable dataset."""

    def target(self, ctx: CheckContext) -> str:
        path = getattr(self.config, "path", None)
        return ctx.render(path) if path else ctx.relation.ref.name

    def files(self, ctx: CheckContext) -> list[FileInfo]:
        lister = _lister(ctx.connector, self.config.type)
        path = getattr(self.config, "path", None)
        if path:
            files: list[FileInfo] = lister(None, ctx.render(path))
        else:
            files = lister(ctx.relation, None)
        return files

    def base_details(self, ctx: CheckContext, files: list[FileInfo]) -> dict[str, Any]:
        details: dict[str, Any] = {"location": self.target(ctx), "file_count": len(files)}
        if files:
            details["files"] = [f.as_dict() for f in files[:_LISTED]]
            if len(files) > _LISTED:
                details["files_truncated"] = True
        return details


# ============================================================================ existence and counts


class FileExists(FileCheck):
    """At least one file must exist, or exactly ``count`` files."""

    dimension = Dimension.COMPLETENESS
    Config = cfg.FileExistsCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.FileExistsCheck)
        files = self.files(ctx)
        where = self.target(ctx)
        found = len(files)
        if config.count is None:
            passed = found > 0
            message = (
                f"{found:,} file(s) match '{where}'" if passed else f"no file matches '{where}'"
            )
            expected: Any = ">= 1"
        else:
            passed = found == config.count
            message = f"{found:,} file(s) match '{where}'" + (
                "" if passed else f"; expected exactly {config.count:,}"
            )
            expected = config.count
        hints = []
        if not passed and found == 0:
            hints.append(
                "Check the path or glob (relative to the connection's folder) and whether the "
                "files have arrived; '**' matches folders recursively."
            )
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=found,
            expected=expected,
            total_count=found,
            details=self.base_details(ctx, files),
            hints=hints,
            samples=_file_samples(ctx, _FILE_COLUMNS, _file_rows(files)) if not passed else None,
        )


class FileCount(FileCheck):
    """The number of files must fall within a range."""

    dimension = Dimension.VOLUME
    Config = cfg.FileCountCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.FileCountCheck)
        files = self.files(ctx)
        found = len(files)
        low_ok = config.min is None or found >= config.min
        high_ok = config.max is None or found <= config.max
        bounds = " and ".join(
            part
            for part in (
                f"at least {config.min:,}" if config.min is not None else "",
                f"at most {config.max:,}" if config.max is not None else "",
            )
            if part
        )
        passed = low_ok and high_ok
        message = f"{found:,} file(s) match '{self.target(ctx)}'" + (
            "" if passed else f"; expected {bounds}"
        )
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=found,
            expected={"min": config.min, "max": config.max},
            total_count=found,
            details=self.base_details(ctx, files),
        )


# ============================================================================ names and sizes


class FileNamePattern(FileCheck):
    """File names must match a regular expression."""

    dimension = Dimension.VALIDITY
    Config = cfg.FileNamePatternCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.FileNamePatternCheck)
        pattern = re.compile(config.pattern)
        matcher = pattern.fullmatch if config.full_match else pattern.search
        files = self.files(ctx)
        bad = [f for f in files if not matcher(f.basename if config.match_on == "name" else f.name)]
        total = len(files)
        passed = mostly_passes(len(bad), total, config.mostly) if total else False
        if not total:
            message = f"no file matches '{self.target(ctx)}', so no name can be checked"
        elif bad:
            message = (
                f"{len(bad):,} of {total:,} file name(s) do not match {config.pattern}: "
                f"{_names(bad)}"
            )
        else:
            message = f"all {total:,} file name(s) match {config.pattern}"
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=total - len(bad),
            expected=total,
            unexpected_count=len(bad),
            total_count=total,
            details=self.base_details(ctx, files),
            samples=_file_samples(ctx, _FILE_COLUMNS, _file_rows(bad)),
        )


class FileSize(FileCheck):
    """File sizes must fall within a range; empty files fail unless allowed."""

    dimension = Dimension.VOLUME
    Config = cfg.FileSizeCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.FileSizeCheck)
        files = self.files(ctx)
        problems: list[tuple[FileInfo, str]] = []
        for file in files:
            if file.size == 0 and not config.allow_empty:
                problems.append((file, "empty"))
            elif config.min_bytes is not None and file.size < config.min_bytes:
                problems.append((file, f"smaller than {_size(config.min_bytes)}"))
            elif config.max_bytes is not None and file.size > config.max_bytes:
                problems.append((file, f"larger than {_size(config.max_bytes)}"))
        total = len(files)
        passed = mostly_passes(len(problems), total, config.mostly) if total else False
        if not total:
            message = f"no file matches '{self.target(ctx)}', so no size can be checked"
        elif problems:
            shown = "; ".join(f"{f.name} ({_size(f.size)}, {why})" for f, why in problems[:5])
            message = f"{len(problems):,} of {total:,} file(s) fail the size rule: {shown}"
        else:
            sizes = [f.size for f in files]
            message = (
                f"all {total:,} file(s) pass the size rule "
                f"({_size(min(sizes))} to {_size(max(sizes))})"
            )
        rows = [[f.name, f.size, why] for f, why in problems]
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=total - len(problems),
            expected=total,
            unexpected_count=len(problems),
            total_count=total,
            details={
                **self.base_details(ctx, files),
                "empty_files": sum(1 for f in files if f.size == 0),
                "total_bytes": sum(f.size for f in files),
            },
            samples=_file_samples(ctx, ["file", "size_bytes", "problem"], rows),
        )


# ============================================================================ checksums


def _parse_manifest(text: str) -> dict[str, str]:
    """Digests by file name from ``<digest>  <file>`` lines or ``file,digest`` CSV rows."""
    digests: dict[str, str] = {}
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    hex_first = re.compile(r"^\s*([0-9a-fA-F]{32,128})\s+\*?(.+?)\s*$")
    if lines and all(hex_first.match(line) for line in lines):
        for line in lines:
            match = hex_first.match(line)
            assert match is not None
            digests[match.group(2).replace("\\", "/")] = match.group(1).lower()
        return digests
    for row in csv.reader(io.StringIO("\n".join(lines))):
        if len(row) < 2:
            continue
        name, digest = row[0].strip(), row[1].strip()
        if re.fullmatch(r"[0-9a-fA-F]{32,128}", digest):
            digests[name.replace("\\", "/")] = digest.lower()
        elif re.fullmatch(r"[0-9a-fA-F]{32,128}", name):
            digests[digest.replace("\\", "/")] = name.lower()
    return digests


def _lookup(digests: dict[str, str], file: FileInfo) -> str | None:
    for key in (file.name, file.basename):
        if key in digests:
            return digests[key]
    for key, digest in digests.items():
        if fnmatch.fnmatchcase(file.name, key) or fnmatch.fnmatchcase(file.basename, key):
            return digest
    return None


def _glob_base(location: str) -> str:
    head = re.split(r"[*?\[]", location, maxsplit=1)[0]
    if head != location:
        return head.rsplit("/", 1)[0] if "/" in head else ""
    return location.rstrip("/")


def _relative_key(file: FileInfo, base: str) -> str:
    base = base.rstrip("/")
    if base and file.path.startswith(base + "/"):
        return file.path[len(base) + 1 :]
    return file.basename


class FileChecksum(FileCheck):
    """File checksums must match expected values, sidecars, a manifest, or other files."""

    dimension = Dimension.RECONCILIATION
    Config = cfg.FileChecksumCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.FileChecksumCheck)
        if config.compare_to is not None:
            return self._compare(ctx, config)
        files = self.files(ctx)
        storage_options = dict(getattr(ctx.connector, "storage_options", {}) or {})
        if config.sidecar:
            files = [f for f in files if not self._is_sidecar(f, config)]
        expected = dict(config.expected)
        if config.manifest:
            manifest = self._resolve(ctx, ctx.render(config.manifest))
            if not storage.exists(manifest, storage_options):
                raise PlanError(
                    f"Checksum manifest '{config.manifest}' not found ({manifest}).",
                    hint="Give 'manifest' relative to the connection's folder.",
                )
            expected = _parse_manifest(storage.read_text(manifest, "utf-8", storage_options))
            files = [f for f in files if f.path != manifest]
        algorithm = config.algorithm
        rows: list[list[Any]] = []
        mismatched: list[FileInfo] = []
        missing: list[FileInfo] = []
        for file in files:
            want = (
                self._sidecar_digest(file, config, storage_options)
                if config.sidecar
                else _lookup(expected, file)
            )
            if want is None:
                missing.append(file)
                rows.append([file.name, None, None, "no expected checksum"])
                continue
            got = storage.file_digest(file.path, algorithm, storage_options)
            if got.lower() != want.lower():
                mismatched.append(file)
                rows.append([file.name, want.lower(), got, "checksum differs"])
        total = len(files)
        failed = len(mismatched) + len(missing)
        label = "MD5" if algorithm == "md5" else "SHA-256"
        if not total:
            message = f"no file matches '{self.target(ctx)}', so no checksum can be verified"
        elif failed:
            parts = []
            if mismatched:
                parts.append(f"{len(mismatched):,} differ ({_names(mismatched)})")
            if missing:
                parts.append(f"{len(missing):,} have no expected checksum ({_names(missing)})")
            message = (
                f"{total - failed:,} of {total:,} file(s) match their {label} checksum; "
                + "; ".join(parts)
            )
        else:
            message = f"all {total:,} file(s) match their {label} checksum"
        return CheckOutcome(
            passed=bool(total) and failed == 0,
            message=message,
            observed=total - failed,
            expected=total,
            unexpected_count=failed,
            total_count=total,
            details={**self.base_details(ctx, files), "algorithm": algorithm},
            samples=_file_samples(ctx, ["file", "expected", "actual", "problem"], rows),
            hints=["A file differs when its bytes differ; compressed files are hashed as stored."]
            if mismatched
            else [],
        )

    @staticmethod
    def _suffix(config: cfg.FileChecksumCheck) -> str:
        if isinstance(config.sidecar, str):
            return config.sidecar if config.sidecar.startswith(".") else "." + config.sidecar
        return "." + config.algorithm

    def _is_sidecar(self, file: FileInfo, config: cfg.FileChecksumCheck) -> bool:
        return file.name.lower().endswith(self._suffix(config).lower())

    def _sidecar_digest(
        self, file: FileInfo, config: cfg.FileChecksumCheck, storage_options: dict[str, Any]
    ) -> str | None:
        sidecar = file.path + self._suffix(config)
        if not storage.exists(sidecar, storage_options):
            return None
        text = storage.read_text(sidecar, "utf-8", storage_options).strip()
        match = re.match(r"([0-9a-fA-F]{32,128})", text)
        return match.group(1) if match else None

    @staticmethod
    def _resolve(ctx: CheckContext, path: str) -> str:
        if storage.is_url(path) or Path(path).is_absolute():
            return path
        root = getattr(ctx.connector, "root", None)
        base = root() if callable(root) else ctx.connector.base_dir.as_posix()
        return f"{str(base).rstrip('/')}/{path}"

    def _compare(self, ctx: CheckContext, config: cfg.FileChecksumCheck) -> CheckOutcome:
        assert config.compare_to is not None
        here = self.files(ctx)
        other_connector = (
            ctx.connectors.get(config.compare_to.connection)
            if config.compare_to.connection
            else ctx.connector
        )
        other_path = ctx.render(config.compare_to.path)
        there: list[FileInfo] = _lister(other_connector, config.type)(None, other_path)
        algorithm = config.algorithm
        here_options = dict(getattr(ctx.connector, "storage_options", {}) or {})
        there_options = dict(getattr(other_connector, "storage_options", {}) or {})
        here_base = _glob_base(self._resolve(ctx, self.target(ctx)))
        root = getattr(other_connector, "root", None)
        other_root = root() if callable(root) else other_connector.base_dir.as_posix()
        there_base = _glob_base(
            other_path
            if storage.is_url(other_path) or Path(other_path).is_absolute()
            else f"{str(other_root).rstrip('/')}/{other_path}"
        )

        def key(file: FileInfo, base: str) -> str:
            return file.basename if config.match_by == "name" else _relative_key(file, base)

        left = {key(f, here_base): f for f in here}
        right = {key(f, there_base): f for f in there}
        rows: list[list[Any]] = []
        missing = sorted(set(left) - set(right))
        extra = sorted(set(right) - set(left))
        differing: list[str] = []
        for name in sorted(set(left) & set(right)):
            a = storage.file_digest(left[name].path, algorithm, here_options)
            b = storage.file_digest(right[name].path, algorithm, there_options)
            if a != b:
                differing.append(name)
                rows.append([name, a, b, "checksum differs"])
        rows.extend([name, None, None, "missing from the reference"] for name in missing)
        rows.extend([name, None, None, "only in the reference"] for name in extra)
        failed = len(missing) + len(extra) + len(differing)
        total = len(set(left) | set(right))
        where = f"'{other_path}'" + (
            f" on '{config.compare_to.connection}'" if config.compare_to.connection else ""
        )
        if not total:
            message = f"no file matches '{self.target(ctx)}' or {where}"
        elif failed:
            parts = []
            if differing:
                parts.append(f"{len(differing):,} differ ({', '.join(differing[:5])})")
            if missing:
                parts.append(f"{len(missing):,} missing from {where} ({', '.join(missing[:5])})")
            if extra:
                parts.append(f"{len(extra):,} only in {where} ({', '.join(extra[:5])})")
            message = f"{total - failed:,} of {total:,} file(s) are identical; " + "; ".join(parts)
        else:
            message = f"all {total:,} file(s) are identical to those in {where}"
        return CheckOutcome(
            passed=bool(total) and failed == 0,
            message=message,
            observed=total - failed,
            expected=total,
            unexpected_count=failed,
            total_count=total,
            details={
                **self.base_details(ctx, here),
                "algorithm": algorithm,
                "reference": other_path,
                "reference_file_count": len(there),
            },
            samples=_file_samples(ctx, ["file", "checksum", "reference_checksum", "problem"], rows),
        )


# ============================================================================ freshness


class FileFreshness(FileCheck):
    """The newest (or oldest) file must have been modified within ``max_age``."""

    dimension = Dimension.TIMELINESS
    Config = cfg.FileFreshnessCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.FileFreshnessCheck)
        files = [f for f in self.files(ctx) if f.modified is not None]
        limit = parse_duration(config.max_age)
        if not files:
            return CheckOutcome(
                passed=False,
                message=f"no file with a modification time matches '{self.target(ctx)}'",
                expected=config.max_age,
                details=self.base_details(ctx, files),
            )
        now = datetime.now(UTC)
        pick = max if config.which == "newest" else min
        chosen = pick(files, key=lambda f: f.modified or now)
        assert chosen.modified is not None
        age = now - chosen.modified
        passed = age <= limit
        hours = age.total_seconds() / 3600
        message = (
            f"the {config.which} file, {chosen.name}, was modified {hours:,.1f} h ago "
            f"({chosen.modified.isoformat(timespec='seconds')}); maximum age {config.max_age}"
        )
        stale = [f for f in files if f.modified is not None and now - f.modified > limit]
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=chosen.modified.isoformat(),
            expected=config.max_age,
            unexpected_count=len(stale),
            total_count=len(files),
            details={
                **self.base_details(ctx, files),
                "age_seconds": round(age.total_seconds(), 3),
                "stale_files": len(stale),
            },
            samples=_file_samples(ctx, _FILE_COLUMNS, _file_rows(stale)) if not passed else None,
        )


# ============================================================================ control records


def _control_number(text: str, scale: int = 0) -> Decimal:
    cleaned = text.strip().replace(",", "")
    negative = cleaned.endswith("-")
    if negative or cleaned.endswith("+"):
        cleaned = cleaned[:-1]
    try:
        value = Decimal(cleaned) if cleaned else Decimal(0)
    except InvalidOperation as exc:
        raise ValueError(f"'{text}' is not a number") from exc
    if scale:
        value = value.scaleb(-scale)
    return -value if negative else value


class ControlTotals(FileCheck):
    """Header or trailer control records (record counts, column sums) must match the data."""

    dimension = Dimension.RECONCILIATION
    Config = cfg.ControlTotalsCheck
    requires_dataset = True

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.ControlTotalsCheck)
        files = self.files(ctx)
        if not files:
            return CheckOutcome(
                passed=False,
                message=f"no file matches '{self.target(ctx)}', so no control record can be read",
                details=self.base_details(ctx, files),
            )
        storage_options = dict(getattr(ctx.connector, "storage_options", {}) or {})
        records = [
            (kind, record, record.line or default)
            for kind, record, default in (
                ("header", config.header, 1),
                ("trailer", config.trailer, -1),
            )
            if record is not None
        ]
        head = max((line for _, _, line in records if line > 0), default=0)
        tail = max((-line for _, _, line in records if line < 0), default=0)
        controls: dict[str, Decimal] = {}
        per_file: list[dict[str, Any]] = []
        for file in files:
            first, last, _ = storage.head_and_tail(
                file.path, head, tail, config.encoding, storage_options
            )
            values: dict[str, Decimal] = {}
            for kind, record, line in records:
                values.update(self._read(file, kind, record, line, first, last))
            per_file.append({"file": file.name, **{k: str(v) for k, v in values.items()}})
            for name, value in values.items():
                controls[name] = controls.get(name, Decimal(0)) + value
        metrics: list[MetricComparison] = []
        selects = ["COUNT(*) AS dvf_records"]
        for i, column in enumerate(config.sums):
            selects.append(f"SUM({ctx.col(column)}) AS dvf_sum_{i}")
        row = ctx.row(f"SELECT {', '.join(selects)}\n{ctx.from_where()}")
        failures = []
        if config.record_count is not None:
            expected_count = controls[config.record_count]
            actual = int(row.get("dvf_records") or 0)
            ok = Decimal(actual) == expected_count
            metrics.append(
                MetricComparison(
                    metric="record_count",
                    source=_plain(expected_count),
                    target=actual,
                    difference=float(Decimal(actual) - expected_count),
                    passed=ok,
                    note=f"control value '{config.record_count}' vs data rows",
                )
            )
            if not ok:
                failures.append(
                    f"control records say {_plain(expected_count):,} records, the data has {actual:,}"
                )
        for i, (column, name) in enumerate(config.sums.items()):
            expected_sum = controls[name]
            raw = row.get(f"dvf_sum_{i}")
            actual_sum = Decimal(str(raw)) if raw is not None else Decimal(0)
            difference = actual_sum - expected_sum
            ok = abs(difference) <= Decimal(str(config.tolerance))
            metrics.append(
                MetricComparison(
                    metric="sum",
                    column=column,
                    source=str(expected_sum),
                    target=str(actual_sum),
                    difference=float(difference),
                    tolerance=config.tolerance,
                    passed=ok,
                    note=f"control value '{name}' vs SUM({column})",
                )
            )
            if not ok:
                failures.append(
                    f"control records say SUM({column}) = {expected_sum}, the data has {actual_sum}"
                )
        passed = not failures
        summary = ", ".join(
            f"{m.metric if m.column is None else f'SUM({m.column})'} {m.target}" for m in metrics
        )
        message = "; ".join(failures) if failures else f"control records match the data ({summary})"
        return CheckOutcome(
            passed=passed,
            message=message,
            observed={m.note or m.metric: m.target for m in metrics},
            expected={m.note or m.metric: m.source for m in metrics},
            metrics=metrics,
            details={**self.base_details(ctx, files), "control_values": per_file[:_LISTED]},
            hints=[
                "Exclude the header and trailer lines from the dataset (skip, skip_footer, "
                "or a filter) so they are not counted as data."
            ]
            if failures
            else [],
        )

    @staticmethod
    def _read(
        file: FileInfo,
        kind: str,
        record: cfg.ControlRecord,
        line: int,
        first: list[str],
        last: list[str],
    ) -> dict[str, Decimal]:
        try:
            text = first[line - 1] if line > 0 else last[line]
        except IndexError:
            raise PlanError(
                f"File '{file.name}' has no {kind} record at line {line}: it has too few lines."
            ) from None
        values: dict[str, Decimal] = {}
        if record.pattern is not None:
            match = re.search(record.pattern, text)
            if match is None:
                raise PlanError(
                    f"The {kind} record of '{file.name}' does not match the pattern: {text[:200]!r}",
                    hint=f"Pattern: {record.pattern}",
                )
            for name, raw in match.groupdict().items():
                try:
                    values[name] = _control_number(raw or "")
                except ValueError as exc:
                    raise PlanError(
                        f"The {kind} record of '{file.name}': control value '{name}' {exc}."
                    ) from exc
            return values
        for name, field in record.fields.items():
            raw = text[field.start - 1 : field.start - 1 + field.width]
            try:
                values[name] = _control_number(raw, field.scale)
            except ValueError as exc:
                raise PlanError(
                    f"The {kind} record of '{file.name}': control value '{name}' at positions "
                    f"{field.start}-{field.start + field.width - 1} {exc}."
                ) from exc
        return values


def _plain(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


# ============================================================================ schema on read


def _expected_columns(ctx: CheckContext, config: cfg.SchemaOnReadCheck) -> list[cfg.SchemaColumn]:
    if config.columns:
        return list(config.columns)
    assert config.schema_file is not None
    path = Path(ctx.render(config.schema_file))
    if not path.is_absolute():
        path = ctx.connector.base_dir / path
    if not path.is_file():
        raise PlanError(f"Schema file '{config.schema_file}' not found ({path}).")
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except Exception as exc:
        raise PlanError(f"Schema file '{config.schema_file}' cannot be parsed: {exc}") from exc
    if isinstance(data, dict) and "columns" in data:
        data = data["columns"]
    try:
        model = cfg.SchemaOnReadCheck.model_validate({"type": "schema_on_read", "columns": data})
    except Exception as exc:
        raise PlanError(f"Schema file '{config.schema_file}' does not list columns: {exc}") from exc
    return list(model.columns)


def _same_type(expected: CanonicalType, actual: CanonicalType, exact: bool) -> bool:
    if exact:
        return expected == actual
    return expected.logical is actual.logical


class SchemaOnRead(Check):
    """The columns and types read from the dataset must match an expected schema."""

    dimension = Dimension.SCHEMA
    Config = cfg.SchemaOnReadCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        config = self.config
        assert isinstance(config, cfg.SchemaOnReadCheck)
        if ctx.schema is None:
            raise PlanError(f"The schema of {ctx.relation.ref.display()} could not be read.")
        expected = _expected_columns(ctx, config)
        actual = list(ctx.schema.columns)

        def norm(name: str) -> str:
            return name if config.case_sensitive else name.lower()

        by_name = {norm(c.name): c for c in actual}
        rows: list[list[Any]] = []
        for column in expected:
            found = by_name.get(norm(column.name))
            if found is None:
                rows.append([column.name, column.type, None, "missing"])
                continue
            if column.type:
                want = parse_native_type(column.type, "duckdb")
                if not _same_type(want, found.type, config.type_match == "exact"):
                    rows.append([column.name, column.type, found.native_type, "type differs"])
        expected_names = {norm(c.name) for c in expected}
        if not config.allow_extra_columns:
            rows.extend(
                [c.name, None, c.native_type, "not expected"]
                for c in actual
                if norm(c.name) not in expected_names
            )
        if config.ordered:
            present = [norm(c.name) for c in actual if norm(c.name) in expected_names]
            wanted = [norm(c.name) for c in expected if norm(c.name) in by_name]
            if present != wanted:
                rows.append(["(order)", ", ".join(wanted), ", ".join(present), "order differs"])
        passed = not rows
        if passed:
            message = f"all {len(expected):,} expected column(s) are present with matching types"
        else:
            shown = "; ".join(
                f"'{r[0]}' {r[3]}"
                + (f" ({r[2]}, expected {r[1]})" if r[3] == "type differs" else "")
                for r in rows[:6]
            )
            message = f"{len(rows):,} difference(s) from the expected schema: {shown}"
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=[f"{c.name} {c.native_type}" for c in actual],
            expected=[f"{c.name} {c.type or 'any'}" for c in expected],
            unexpected_count=len(rows),
            total_count=len(expected),
            samples=SampleSet(
                columns=["column", "expected_type", "actual_type", "problem"], rows=rows
            )
            if rows
            else None,
        )


# ============================================================================ bad records


class BadRecords(Check):
    """Unreadable and malformed records must not exceed a limit."""

    dimension = Dimension.VALIDITY
    Config = cfg.BadRecordsCheck

    def evaluate(self, ctx: CheckContext) -> CheckOutcome:
        from dvf.connectors.duckdb import BAD_RECORD_COLUMNS

        config = self.config
        assert isinstance(config, cfg.BadRecordsCheck)
        scanner = getattr(ctx.connector, "bad_records", None)
        if scanner is None:
            raise PlanError(
                f"Check 'bad_records' reads files, but connection '{ctx.connector.name}' is not a "
                "files connection.",
                hint="Run bad_records on datasets of a connection with type: files.",
            )
        limit = max(ctx.sample_size, ctx.extract_rows)
        found = scanner(ctx.relation, limit if not ctx.privacy.strict else 0)
        bad, records = int(found.bad), found.records
        passed = True
        if config.max_count is not None:
            passed = bad <= config.max_count
        if records is not None and config.mostly < 1:
            passed = passed and mostly_passes(bad, records, config.mostly)
        elif config.max_count is None:
            passed = bad == 0
        columns = list(BAD_RECORD_COLUMNS)
        rows = [[row.get(c) for c in columns] for row in found.rows]
        samples = None
        extract = None
        if rows and not ctx.privacy.strict:
            masked = ctx.privacy.mask_rows(columns, rows)
            if ctx.sample_size > 0:
                samples = SampleSet(
                    columns=columns,
                    rows=masked[: ctx.sample_size],
                    masked=ctx.privacy.mask_pii,
                    truncated=bad > min(len(masked), ctx.sample_size),
                )
            if ctx.extract_rows > 0 and len(masked) > ctx.sample_size:
                from dvf.execution.artifacts import text_table

                extract = text_table(columns, masked)
        share = pct(bad, records) if records else None
        if bad:
            scope = f" of {records:,} records" if records is not None else ""
            ratio = f" ({share:.4g}%)" if share is not None else ""
            message = f"{bad:,} bad record(s){scope}{ratio}: {found.method}"
            if config.max_count is not None:
                message += f"; at most {config.max_count:,} allowed"
        else:
            scope = (
                f"all {records:,} records" if records is not None else f"{found.files:,} file(s)"
            )
            message = f"no bad records ({scope} read cleanly)"
        hints = []
        dataset_format = getattr(ctx.connector, "dataset_format", None)
        if bad and callable(dataset_format) and dataset_format(ctx.relation) in ("csv", "tsv"):
            hints.append(
                "Set 'ignore_errors: true' in the dataset's options so other checks read the "
                "good rows; this check still counts and extracts the bad ones."
            )
        return CheckOutcome(
            passed=passed,
            message=message,
            observed=bad,
            expected=config.max_count if config.max_count is not None else 0,
            unexpected_count=bad,
            total_count=records,
            samples=samples,
            extract=extract,
            details={"method": found.method, "files": found.files},
            hints=hints,
        )


FILE_CHECKS: dict[str, type[Check]] = {
    "file_exists": FileExists,
    "file_count": FileCount,
    "file_name_pattern": FileNamePattern,
    "file_size": FileSize,
    "file_checksum": FileChecksum,
    "file_freshness": FileFreshness,
    "control_totals": ControlTotals,
    "schema_on_read": SchemaOnRead,
    "bad_records": BadRecords,
}
"""Check type name to implementation."""

__all__ = ["FILE_CHECKS", "FileCheck"]
