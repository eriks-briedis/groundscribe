"""Structured diffs between two versions of a stage artefact (phase 06).

plan/06 asks for a *visible* diff twice: when answers rebuild the source model
(§3) and when the author overrides an architecture (§5). Both are cases where the
system replaces something the author already read, and "here is the new version"
is not an acceptable answer to "what changed?".

The diff is over the parsed structures, not over rendered text. A textual diff of
serialised JSON reports formatting churn as change and hides a reordering as a
rewrite; a structural one names the field — ``claims.0.classification`` — which is
also the language the rest of the system already uses for validation errors.

Lists are compared positionally. Matching by identity would be better for a list
of claims with stable ids and worse for everything else, and the fallback when
identity is unavailable is exactly this. A reordering therefore reads as several
changes, which is honest: to a reader checking what the model altered, a moved
claim *is* a change at both positions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ChangeKind(StrEnum):
    """What happened at one path."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class DiffEntry(BaseModel):
    """One difference, at one dotted path."""

    model_config = ConfigDict(frozen=True)

    path: str
    change: ChangeKind
    before: Any = None
    after: Any = None


class StructuredDiff(BaseModel):
    """A whole diff, ready to be snapshotted and shown to a person.

    Carries the entries plus the counts, so a reader (or a phase-11 view) can say
    "3 changed, 1 added" without walking the list.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    entries: tuple[DiffEntry, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def counts(self) -> dict[str, int]:
        """How many entries of each kind."""
        return {
            kind.value: sum(1 for entry in self.entries if entry.change is kind)
            for kind in ChangeKind
        }


def structured_diff(before: Any, after: Any) -> StructuredDiff:
    """Diff two parsed structures, returning the differences by path."""
    return StructuredDiff(entries=tuple(_walk("", before, after)))


def _walk(path: str, before: Any, after: Any) -> list[DiffEntry]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        return _walk_mapping(path, before, after)
    if _is_sequence(before) and _is_sequence(after):
        return _walk_sequence(path, before, after)
    if before != after:
        return [DiffEntry(path=path, change=ChangeKind.CHANGED, before=before, after=after)]
    return []


def _walk_mapping(
    path: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    for key in sorted({*before, *after}):
        child = f"{path}.{key}" if path else key
        if key not in after:
            entries.append(DiffEntry(path=child, change=ChangeKind.REMOVED, before=before[key]))
        elif key not in before:
            entries.append(DiffEntry(path=child, change=ChangeKind.ADDED, after=after[key]))
        else:
            entries.extend(_walk(child, before[key], after[key]))
    return entries


def _walk_sequence(path: str, before: Sequence[Any], after: Sequence[Any]) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    for index in range(max(len(before), len(after))):
        child = f"{path}.{index}" if path else str(index)
        if index >= len(after):
            entries.append(DiffEntry(path=child, change=ChangeKind.REMOVED, before=before[index]))
        elif index >= len(before):
            entries.append(DiffEntry(path=child, change=ChangeKind.ADDED, after=after[index]))
        else:
            entries.extend(_walk(child, before[index], after[index]))
    return entries


def _is_sequence(value: Any) -> bool:
    """A list-like value, excluding the strings and bytes that are also sequences."""
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


__all__ = ["ChangeKind", "DiffEntry", "StructuredDiff", "structured_diff"]
