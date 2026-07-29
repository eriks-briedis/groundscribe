"""Access to the golden editorial data under ``evaluations/`` (phase 06).

plan/06 → *Golden tests*: a representative source, and the structured output a
good model would return for it. The data lives outside the test suite on purpose.
It is the same material phase 12's evaluation suite scores against, and a fixture
buried in a test module could not be reused, diffed, or replaced without touching
code.

Golden responses reference source segments by *label* (``S0``, ``S1``, …) rather
than by database id: ids are generated per run, and a golden file that hardcoded
them would be rewritten on every ingest. :func:`with_segment_ids` substitutes the
labels for the real ids of a freshly ingested document, which is exactly the
mapping a reader does by eye.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from groundscribe.paths import repo_root
from groundscribe.stages.ingestion import IngestedSource

#: Where the golden suites live, one directory per stretch of the pipeline.
GOLDEN_ROOT = repo_root() / "evaluations" / "golden"

#: The suite a caller gets when it does not say: phase 06's source-to-brief data,
#: which is what most tests start from even when they are testing a later stage.
DEFAULT_SUITE = "source_to_brief"

_LABEL = re.compile(r"^S(\d+)$")


def golden_text(name: str, *, suite: str = DEFAULT_SUITE) -> str:
    """Read one golden file verbatim."""
    return (GOLDEN_ROOT / suite / name).read_text(encoding="utf-8")


def golden_json(name: str, *, suite: str = DEFAULT_SUITE) -> dict[str, Any]:
    """Read one golden JSON document."""
    loaded = json.loads(golden_text(name, suite=suite))
    assert isinstance(loaded, dict)
    return loaded


def with_segment_ids(payload: dict[str, Any], source: IngestedSource) -> dict[str, Any]:
    """Replace ``S<n>`` segment labels with the ids of ``source``'s segments."""
    return relabel(payload, {f"S{segment.ordinal}": segment.id for segment in source.segments})


def relabel(payload: dict[str, Any], ids: Mapping[str, str]) -> dict[str, Any]:
    """Replace ``S<n>`` labels using an explicit map.

    The form a caller needs when it has the segment rows but not the
    :class:`~groundscribe.stages.ingestion.IngestedSource` they came in on —
    which is every caller that reloaded them from the database, as a worker
    does. Unknown labels are left untouched so a test can deliberately script a
    dangling reference.
    """
    substituted = _substitute(payload, dict(ids))
    assert isinstance(substituted, dict)
    return substituted


def _substitute(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, ids) for item in value]
    if isinstance(value, str) and _LABEL.match(value):
        return ids.get(value, value)
    return value


__all__ = [
    "DEFAULT_SUITE",
    "GOLDEN_ROOT",
    "golden_json",
    "golden_text",
    "relabel",
    "with_segment_ids",
]
