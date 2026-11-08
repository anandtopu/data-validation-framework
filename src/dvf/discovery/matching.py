"""Name matching for tables and columns: exact, then case-insensitive, then fuzzy.

Fuzzy scores come from :class:`difflib.SequenceMatcher` on names lower-cased with separators
removed, so ``order_lines`` and ``OrderLines`` score 1.0 and ``customer`` and ``customers``
about 0.94. Every item is matched at most once; the best-scoring fuzzy pairs win.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")

_SEPARATORS = re.compile(r"[^0-9a-z]+")


class MatchMethod(StrEnum):
    """How two names were matched."""

    EXACT = "exact"
    CASE_INSENSITIVE = "case_insensitive"
    FUZZY = "fuzzy"


@dataclass(frozen=True)
class Match(Generic[T]):
    """A source item and the target item it was matched with."""

    source: T
    target: T
    method: MatchMethod
    score: float
    """1.0 for exact and case-insensitive matches; the similarity for fuzzy ones."""


@dataclass
class MatchResult(Generic[T]):
    """The outcome of :func:`match_items`."""

    matches: list[Match[T]] = field(default_factory=list)
    unmatched_source: list[T] = field(default_factory=list)
    unmatched_target: list[T] = field(default_factory=list)


def normalize_name(name: str) -> str:
    """``name`` lower-cased with everything but letters and digits removed."""
    return _SEPARATORS.sub("", name.lower())


def similarity(left: str, right: str) -> float:
    """Similarity of two names between 0 and 1, rounded to three digits."""
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 1.0 if left.lower() == right.lower() else 0.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def match_items(
    source: Sequence[T],
    target: Sequence[T],
    name: Callable[[T], str],
    *,
    threshold: float = 0.8,
    fuzzy: bool = True,
) -> MatchResult[T]:
    """Match ``source`` items with ``target`` items by ``name``.

    Exact matches come first, then case-insensitive ones, then fuzzy matches scoring at least
    ``threshold`` (best scores first). Matches are returned in source order.
    """
    left = list(range(len(source)))
    right = list(range(len(target)))
    found: dict[int, tuple[int, MatchMethod, float]] = {}

    def take(i: int, j: int, method: MatchMethod, score: float) -> None:
        found[i] = (j, method, score)
        left.remove(i)
        right.remove(j)

    for i in list(left):
        exact = next((j for j in right if name(target[j]) == name(source[i])), None)
        if exact is not None:
            take(i, exact, MatchMethod.EXACT, 1.0)
    for i in list(left):
        lowered = name(source[i]).lower()
        loose = next((j for j in right if name(target[j]).lower() == lowered), None)
        if loose is not None:
            take(i, loose, MatchMethod.CASE_INSENSITIVE, 1.0)
    if fuzzy:
        candidates = sorted(
            (
                (
                    -similarity(name(source[i]), name(target[j])),
                    name(source[i]),
                    name(target[j]),
                    i,
                    j,
                )
                for i in left
                for j in right
            ),
        )
        for negative, _, _, i, j in candidates:
            if -negative < threshold:
                break
            if i in left and j in right:
                take(i, j, MatchMethod.FUZZY, -negative)
    result: MatchResult[T] = MatchResult()
    for i in sorted(found):
        j, method, score = found[i]
        result.matches.append(Match(source[i], target[j], method, score))
    result.unmatched_source = [source[i] for i in left]
    result.unmatched_target = [target[j] for j in right]
    return result


__all__ = ["Match", "MatchMethod", "MatchResult", "match_items", "normalize_name", "similarity"]
