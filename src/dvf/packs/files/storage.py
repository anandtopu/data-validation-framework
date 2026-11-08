"""The files behind a files dataset: listing, opening, hashing, and reading control lines.

Local paths are handled with :mod:`pathlib`. URLs (``s3://``, ``abfs://``, ``memory://``, ...)
go through `fsspec <https://filesystem-spec.readthedocs.io>`_ when it is importable, with the
connection's ``storage_options``; each protocol also needs its fsspec implementation (``s3fs``,
``adlfs``, ``gcsfs``).
"""

from __future__ import annotations

import bz2
import glob as globmod
import gzip
import hashlib
import importlib
import io
import lzma
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from dvf.core.errors import ConnectorError

COMPRESSIONS = {".gz": "gzip", ".bz2": "bz2", ".zst": "zstd", ".zstd": "zstd", ".xz": "xz"}
"""Whole-file compression recognized by its suffix."""


@dataclass(frozen=True)
class FileInfo:
    """One file behind a dataset."""

    path: str
    """Absolute local path (with forward slashes) or URL."""
    name: str
    """Path relative to the connection's base folder (or the URL), with forward slashes."""
    size: int
    """Size in bytes."""
    modified: datetime | None
    """Last modification time in UTC, when the storage reports it."""

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "modified": self.modified.isoformat() if self.modified else None,
        }


def is_url(path: str) -> bool:
    return "://" in path


def url_protocol(url: str) -> str:
    return url.split("://", 1)[0].lower()


def compression_of(path: str) -> str | None:
    """The whole-file compression implied by ``path``'s suffix, if any."""
    lowered = path.lower()
    for suffix, name in COMPRESSIONS.items():
        if lowered.endswith(suffix):
            return name
    return None


def strip_compression(path: str) -> str:
    lowered = path.lower()
    for suffix in (*COMPRESSIONS, ".snappy"):
        if lowered.endswith(suffix):
            return path[: -len(suffix)]
    return path


def is_data_file(relative: str) -> bool:
    """Whether a file inside a dataset folder holds data (Spark and Hive conventions).

    Names starting with ``_`` or ``.`` (``_SUCCESS``, ``_delta_log/``, ``.part-0.crc``) and
    ``.crc`` files are bookkeeping.
    """
    parts = relative.replace("\\", "/").split("/")
    if any(p.startswith(("_", ".")) for p in parts if p):
        return False
    return not relative.lower().endswith(".crc")


# ---------------------------------------------------------------------------- fsspec


def fsspec_filesystem(url: str, storage_options: dict[str, Any]) -> Any:
    """The fsspec filesystem serving ``url`` (raises a clear error when it is unavailable)."""
    try:
        fsspec = importlib.import_module("fsspec")
    except ImportError as exc:
        raise ConnectorError(
            f"Reading '{url}' needs fsspec: pip install fsspec (plus s3fs, adlfs, or gcsfs for "
            "the storage), or list a DuckDB extension such as httpfs in the connection's "
            "options.extensions."
        ) from exc
    protocol = url_protocol(url)
    try:
        return fsspec.filesystem(protocol, **storage_options)
    except (ImportError, ValueError) as exc:
        raise ConnectorError(
            f"No fsspec implementation is available for '{protocol}://' ({exc}). Install it "
            "(s3fs for s3://, adlfs for abfs:// and az://, gcsfs for gs://)."
        ) from exc


def _with_protocol(protocol: str, path: str) -> str:
    return path if is_url(path) else f"{protocol}://{path.lstrip('/')}"


def _fsspec_modified(info: dict[str, Any]) -> datetime | None:
    for key in ("mtime", "LastModified", "last_modified", "updated", "created", "modified"):
        value = info.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, int | float):
            return datetime.fromtimestamp(float(value), UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


# ---------------------------------------------------------------------------- listing


def _relative(path: str, root: str) -> str:
    root = root.rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return path


def list_files(
    location: str, root: str, storage_options: dict[str, Any] | None = None
) -> list[FileInfo]:
    """The files that ``location`` names: one file, the data files of a folder, or a glob.

    ``root`` is the connection's base folder or URL; names are given relative to it.
    """
    if is_url(location):
        return _list_fsspec(location, root, storage_options or {})
    return _list_local(location, root)


def _local_info(path: Path, root: str) -> FileInfo:
    stat = path.stat()
    posix = path.as_posix()
    return FileInfo(
        path=posix,
        name=_relative(posix, root),
        size=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, UTC),
    )


def _list_local(location: str, root: str) -> list[FileInfo]:
    path = Path(location)
    if globmod.has_magic(location):
        matches = sorted(globmod.glob(location, recursive=True))  # noqa: PTH207 - absolute pattern
        return [_local_info(Path(m), root) for m in matches if Path(m).is_file()]
    if path.is_file():
        return [_local_info(path, root)]
    if path.is_dir():
        files = []
        for child in sorted(path.rglob("*")):
            relative = child.relative_to(path).as_posix()
            if child.is_file() and is_data_file(relative):
                files.append(_local_info(child, root))
        return files
    return []


def _list_fsspec(location: str, root: str, storage_options: dict[str, Any]) -> list[FileInfo]:
    fs = fsspec_filesystem(location, storage_options)
    protocol = url_protocol(location)
    if globmod.has_magic(location):
        found = fs.glob(location, detail=True)
        infos = [(p, i) for p, i in sorted(found.items()) if i.get("type") == "file"]
    elif not fs.exists(location):
        infos = []
    elif fs.isdir(location):
        base = location.rstrip("/")
        found = fs.find(base, detail=True)
        infos = []
        for path, info in sorted(found.items()):
            full = _with_protocol(protocol, path)
            if info.get("type") == "file" and is_data_file(_relative(full, base)):
                infos.append((path, info))
    else:
        infos = [(location, fs.info(location))]
    files = []
    for path, info in infos:
        full = _with_protocol(protocol, str(path))
        files.append(
            FileInfo(
                path=full,
                name=_relative(full, root),
                size=int(info.get("size") or 0),
                modified=_fsspec_modified(info),
            )
        )
    return files


# ---------------------------------------------------------------------------- reading


def open_binary(path: str, storage_options: dict[str, Any] | None = None) -> IO[bytes]:
    """Open a local file or URL for binary reading (not decompressed)."""
    if is_url(path):
        fs = fsspec_filesystem(path, storage_options or {})
        handle: IO[bytes] = fs.open(path, "rb")
        return handle
    return Path(path).open("rb")


def open_decompressed(path: str, storage_options: dict[str, Any] | None = None) -> IO[bytes]:
    """Open ``path`` for binary reading, decompressing gzip, bz2, zstd, or xz by suffix."""
    compression = compression_of(path)
    if is_url(path):
        fs = fsspec_filesystem(path, storage_options or {})
        handle: IO[bytes] = fs.open(path, "rb", compression=compression)
        return handle
    if compression is None:
        return Path(path).open("rb")
    if compression == "gzip":
        return gzip.open(path, "rb")  # type: ignore[return-value]
    if compression == "bz2":
        return bz2.open(path, "rb")
    if compression == "xz":
        return lzma.open(path, "rb")
    try:
        zstandard = importlib.import_module("zstandard")
    except ImportError as exc:
        raise ConnectorError(f"Reading '{path}' needs zstandard: pip install zstandard") from exc
    raw = Path(path).open("rb")  # noqa: SIM115 - the stream reader owns and closes it
    reader: IO[bytes] = zstandard.ZstdDecompressor().stream_reader(raw, closefd=True)
    return reader


def open_text(
    path: str,
    encoding: str = "utf-8",
    storage_options: dict[str, Any] | None = None,
    *,
    errors: str = "strict",
) -> IO[str]:
    """Open ``path`` as text (decompressed), keeping line endings as they are."""
    return io.TextIOWrapper(
        open_decompressed(path, storage_options), encoding=encoding, errors=errors, newline=""
    )


def file_digest(path: str, algorithm: str, storage_options: dict[str, Any] | None = None) -> str:
    """The hex digest of a file's bytes as stored (compressed files are hashed compressed)."""
    digest = hashlib.new(algorithm)
    with open_binary(path, storage_options) as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_lines(
    path: str, encoding: str = "utf-8", storage_options: dict[str, Any] | None = None
) -> Iterator[str]:
    """The lines of a text file without their line endings (streamed, decompressed)."""
    with open_text(path, encoding, storage_options) as handle:
        for line in handle:
            yield line.rstrip("\r\n")


def head_and_tail(
    path: str,
    head: int,
    tail: int,
    encoding: str = "utf-8",
    storage_options: dict[str, Any] | None = None,
) -> tuple[list[str], list[str], int]:
    """The first ``head`` and last ``tail`` non-empty lines of a file, and its line count.

    The file is streamed once; memory holds at most ``head + tail`` lines. Trailing empty lines
    (a final newline, padding) are ignored.
    """
    first: list[str] = []
    last: deque[str] = deque(maxlen=max(tail, 0) or None)
    count = 0
    pending_blank = 0
    for line in iter_lines(path, encoding, storage_options):
        if not line.strip():
            pending_blank += 1
            continue
        count += pending_blank + 1
        pending_blank = 0
        if len(first) < head:
            first.append(line)
        if tail > 0:
            last.append(line)
    return first, list(last) if tail > 0 else [], count


def read_text(
    path: str, encoding: str = "utf-8", storage_options: dict[str, Any] | None = None
) -> str:
    """A whole (small) text file, such as a checksum sidecar or manifest."""
    with open_text(path, encoding, storage_options) as handle:
        return handle.read()


def exists(path: str, storage_options: dict[str, Any] | None = None) -> bool:
    if is_url(path):
        return bool(fsspec_filesystem(path, storage_options or {}).exists(path))
    return Path(path).exists()


__all__ = [
    "COMPRESSIONS",
    "FileInfo",
    "compression_of",
    "exists",
    "file_digest",
    "fsspec_filesystem",
    "head_and_tail",
    "is_data_file",
    "is_url",
    "iter_lines",
    "list_files",
    "open_binary",
    "open_decompressed",
    "open_text",
    "read_text",
    "strip_compression",
    "url_protocol",
]
