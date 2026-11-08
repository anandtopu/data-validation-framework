"""Turn documents into rows: dot paths, unwinding, and array strategies.

A :class:`DocumentShaper` maps one document to zero or more rows of classified cells, following a
dataset's :class:`~dvf.packs.nosql.options.DocumentOptions`:

* with ``fields``, each output column takes the value at its dot path;
* without ``fields``, nested documents are flattened into one column per leaf, named by the dot
  path (joined with ``separator``);
* ``unwind`` produces one row per array element, with the kept parent fields and the element's
  position;
* arrays that stay in a row become canonical JSON, a hash, a count, or one column per position.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any

from dvf.core.errors import ConnectorError
from dvf.packs.nosql.options import DocumentOptions
from dvf.packs.nosql.values import (
    Classified,
    Classifier,
    ValueKind,
    canonical_json,
    json_hash,
    unordered_json,
)


class _Missing:
    """A path that does not exist in a document (different from an explicit NULL)."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()

_NULL = Classified(ValueKind.NULL, "null", None)


def split_path(path: str) -> list[str]:
    return [part for part in path.split(".") if part]


def resolve_path(value: Any, segments: list[str]) -> Any:
    """The value at ``segments`` inside ``value``, with MongoDB's dot-path semantics.

    A numeric segment indexes an array; any other segment applied to an array collects that field
    from every element (so ``lines.sku`` is the list of every line's ``sku``). Returns
    :data:`MISSING` when the path does not exist.
    """
    if not segments:
        return value
    head, rest = segments[0], segments[1:]
    if isinstance(value, Mapping):
        if head not in value:
            return MISSING
        return resolve_path(value[head], rest)
    if isinstance(value, list | tuple):
        if head.isdigit():
            index = int(head)
            return resolve_path(value[index], rest) if index < len(value) else MISSING
        found = []
        for element in value:
            if isinstance(element, Mapping | list | tuple):
                item = resolve_path(element, segments)
                if item is not MISSING:
                    found.append(item)
        return found if found else MISSING
    return MISSING


class DocumentShaper:
    """Maps documents to rows of classified cells for one dataset."""

    def __init__(
        self,
        options: DocumentOptions,
        classify: Classifier,
        *,
        default_keep: tuple[str, ...] = (),
    ) -> None:
        self.options = options
        self.classify = classify
        self.sep = options.separator
        self.unwind = options.unwind
        self.unwind_segments = split_path(options.unwind.path) if options.unwind else []
        self.keep = (
            list(options.unwind.keep)
            if options.unwind and options.unwind.keep is not None
            else list(default_keep)
        )
        self.fields = (
            [(name, split_path(path), path) for name, path in options.fields.items()]
            if options.fields
            else None
        )
        self.truncated: Counter[str] = Counter()
        """Arrays cut to ``index_limit`` columns, by path."""
        self.notes: list[str] = []

    # ------------------------------------------------------------------ rows
    def rows(self, document: Mapping[str, Any]) -> Iterator[dict[str, Classified]]:
        """The rows of one document (one per array element when unwinding)."""
        if self.unwind is None:
            yield self._row(document, MISSING, None, unwound=False)
            return
        array = resolve_path(document, self.unwind_segments)
        if array is MISSING or array is None or (isinstance(array, list | tuple) and not array):
            if self.unwind.preserve_empty:
                yield self._row(document, MISSING, None, unwound=True)
            return
        if not isinstance(array, list | tuple):
            # MongoDB treats a non-array value as a one-element array without a position.
            yield self._row(document, array, None, unwound=True)
            return
        for position, element in enumerate(array):
            yield self._row(document, element, position, unwound=True)

    def _row(
        self, document: Mapping[str, Any], element: Any, position: int | None, *, unwound: bool
    ) -> dict[str, Classified]:
        out: dict[str, Classified] = {}
        if self.fields is not None:
            for name, segments, path in self.fields:
                value = self._field_value(document, element, segments, unwound=unwound)
                self._emit(out, name, path, value, depth=0, flatten_objects=False)
            if unwound:
                self._put(out, self.unwind_index_column, _int_cell(position))
            return out
        if not unwound:
            self._flatten(out, document, prefix="", path="", depth=0)
            return out
        for path in self.keep:
            value = resolve_path(document, split_path(path))
            self._emit(out, path.replace(".", self.sep), path, value, depth=0)
        self._put(out, self.unwind_index_column, _int_cell(position))
        assert self.unwind is not None
        prefix = self.unwind.prefix
        array_path = self.unwind.path
        if element is MISSING:
            return out
        cell = self.classify(element)
        if cell.kind is ValueKind.OBJECT and self.options.flatten:
            self._flatten(out, cell.value, prefix=prefix, path=array_path, depth=1, joiner="")
        else:
            name = prefix or self.unwind_segments[-1]
            self._emit(out, name, array_path, element, depth=1)
        return out

    @property
    def unwind_index_column(self) -> str:
        assert self.unwind is not None
        return self.unwind.index_column

    def _field_value(
        self, document: Mapping[str, Any], element: Any, segments: list[str], *, unwound: bool
    ) -> Any:
        if unwound:
            n = len(self.unwind_segments)
            if segments[:n] == self.unwind_segments:
                return element if len(segments) == n else resolve_path(element, segments[n:])
        return resolve_path(document, segments)

    # ------------------------------------------------------------------ flattening
    def _flatten(
        self,
        out: dict[str, Classified],
        obj: Mapping[str, Any],
        *,
        prefix: str,
        path: str,
        depth: int,
        joiner: str | None = None,
    ) -> None:
        join = self.sep if joiner is None else joiner
        for key, value in obj.items():
            key_text = str(key)
            name = f"{prefix}{join}{key_text}" if prefix else key_text
            child_path = f"{path}.{key_text}" if path else key_text
            self._emit(out, name, child_path, value, depth=depth + 1)

    def _emit(
        self,
        out: dict[str, Classified],
        name: str,
        path: str,
        value: Any,
        *,
        depth: int,
        flatten_objects: bool = True,
    ) -> None:
        if value is MISSING:
            self._put(out, name, _NULL)
            return
        cell = self.classify(value)
        if cell.kind is ValueKind.OBJECT:
            if flatten_objects and self.options.flatten and depth < self.options.max_depth:
                self._flatten(out, cell.value, prefix=name, path=path, depth=depth)
                return
            text = canonical_json(cell, self.classify)
            self._put(out, name, Classified(ValueKind.OBJECT, cell.native, text))
            return
        if cell.kind is ValueKind.ARRAY:
            self._array(out, name, path, cell, depth=depth)
            return
        self._put(out, name, cell)

    def _array(
        self, out: dict[str, Classified], name: str, path: str, cell: Classified, *, depth: int
    ) -> None:
        strategy = self.options.arrays.get(path, self.options.default_array)
        elements: list[Any] = cell.value
        if strategy == "count":
            self._put(out, name, Classified(ValueKind.INT, f"{cell.native} (count)", len(elements)))
        elif strategy == "hash":
            digest = json_hash(canonical_json(cell, self.classify))
            self._put(out, name, Classified(ValueKind.STRING, f"{cell.native} (hash)", digest))
        elif strategy == "unordered_hash":
            digest = json_hash(unordered_json(elements, self.classify))
            native = f"{cell.native} (unordered hash)"
            self._put(out, name, Classified(ValueKind.STRING, native, digest))
        elif strategy == "index":
            limit = self.options.index_limit
            if len(elements) > limit:
                self.truncated[path] += 1
            for position, element in enumerate(elements[:limit]):
                self._emit(
                    out, f"{name}{self.sep}{position}", f"{path}.{position}", element, depth=depth
                )
        elif strategy == "explode":
            # Only reachable for a nested array named in 'arrays' while another is unwound.
            raise ConnectorError(
                f"Array '{path}' is set to 'explode', but only the unwound array can be exploded.",
                hint="Use 'json', 'hash', 'count', or 'index' for other arrays.",
            )
        else:
            text = canonical_json(cell, self.classify)
            self._put(out, name, Classified(ValueKind.ARRAY, cell.native, text))

    @staticmethod
    def _put(out: dict[str, Classified], name: str, cell: Classified) -> None:
        if name in out:
            raise ConnectorError(
                f"Two values map to column '{name}' in one row.",
                hint=(
                    "A parent field and an element field may share a name (set 'unwind.prefix'), "
                    "a field name may contain the separator (set 'separator'), or 'fields' may "
                    "repeat a column."
                ),
            )
        out[name] = cell

    def finish_notes(self) -> list[str]:
        """Notes about the shaping, for example truncated arrays."""
        notes = list(self.notes)
        for path, count in sorted(self.truncated.items()):
            notes.append(
                f"{count:,} '{path}' array(s) had more than {self.options.index_limit} elements; "
                "the rest were not turned into columns (raise 'index_limit')"
            )
        return notes


def _int_cell(position: int | None) -> Classified:
    if position is None:
        return _NULL
    return Classified(ValueKind.INT, "int", position)


__all__ = ["MISSING", "DocumentShaper", "resolve_path", "split_path"]
