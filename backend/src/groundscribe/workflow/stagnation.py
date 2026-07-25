"""Detecting a revision loop that has stopped paying for itself (phase 05).

plan/05 → *Stagnation detection*, six conditions. They exist because the loop's
natural failure mode is not a crash but an expensive plateau: a run that keeps
scoring, keeps rewriting, and keeps arriving at the same article. Rewrite limits
cap the *number* of rounds; these conditions notice when the rounds stopped
helping before the cap is reached.

Each detector returns a :class:`StagnationFinding` carrying the evidence it
fired on, not just a flag. The outcome is a human decision — approve despite the
score, add source material, narrow the thesis, reopen the brief, abandon — and
none of those can be chosen from a bare "stalled".

Pure functions over a history the caller supplies. Phase 08 owns scoring and
will be the one to build these rounds; keeping the detector ignorant of where
the numbers came from is what lets phase 05 test all six conditions today.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Any

from groundscribe.workflow.policy import StagnationThresholds


class StagnationSignal(StrEnum):
    """Which of the spec's six conditions fired."""

    NO_IMPROVEMENT = "no_improvement"
    ENTRENCHED = "entrenched_issue"
    OSCILLATING = "oscillating_scores"
    DIVERGING = "diverging_dimensions"
    NOT_BETTER_THAN_PARENT = "not_better_than_parent"
    MANUAL_EDITING = "manual_editing"


@dataclass(frozen=True)
class ScoreRound:
    """One trip round the revision loop, as the detector needs to see it.

    Deliberately not the phase-08 evaluation record: this is the subset the six
    conditions read. A detector that took the full scoring output would have to
    change every time scoring did, and phase 05 could not test it at all.

    ``parent_overall`` is the score of the version this one branched from, which
    is a different comparison from the previous round — a rewrite can improve on
    the last attempt while still being no better than the version it forked.
    """

    ordinal: int
    overall: float
    dimensions: Mapping[str, float] = field(default_factory=dict)
    blocking_issues: tuple[str, ...] = ()
    manual_edit_distance: float | None = None
    voice_pass: bool = False
    parent_overall: float | None = None


@dataclass(frozen=True)
class StagnationFinding:
    """One condition that fired, with the evidence it fired on."""

    signal: StagnationSignal
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def detect_stagnation(
    history: Sequence[ScoreRound], thresholds: StagnationThresholds
) -> tuple[StagnationFinding, ...]:
    """Every stagnation condition the history satisfies, in a stable order.

    All six are evaluated rather than short-circuiting on the first hit: a run
    that is both oscillating *and* trading one dimension for another needs a
    different decision from one that is merely flat, and the person deciding
    cannot see that from the first signal alone.

    Sorted by signal so a stalled run's explanation reads the same on every
    read; two conditions firing in a different order between page loads would
    look like the run had changed.
    """
    findings = [
        finding
        for detector in (
            _no_improvement,
            _entrenched_issue,
            _oscillating,
            _diverging_dimensions,
            _not_better_than_parent,
            _manual_editing,
        )
        if (finding := detector(history, thresholds)) is not None
    ]
    return tuple(sorted(findings, key=lambda finding: finding.signal))


def _deltas(history: Sequence[ScoreRound]) -> list[float]:
    """Round-on-round change in the overall score."""
    return [later.overall - earlier.overall for earlier, later in pairwise(history)]


def _no_improvement(
    history: Sequence[ScoreRound], thresholds: StagnationThresholds
) -> StagnationFinding | None:
    """plan/05: improvement below the threshold for two consecutive rounds.

    A single flat round is not enough, and that is the point of the second: flat
    rounds are common and often followed by a good one, so stalling on the first
    would interrupt runs that were about to succeed.
    """
    needed = thresholds.improvement_rounds
    deltas = _deltas(history)
    if len(deltas) < needed:
        return None
    recent = deltas[-needed:]
    if any(delta >= thresholds.min_improvement for delta in recent):
        return None
    return StagnationFinding(
        signal=StagnationSignal.NO_IMPROVEMENT,
        detail=(
            f"the last {needed} rounds moved the overall score by "
            f"{', '.join(f'{delta:+g}' for delta in recent)}, all under "
            f"{thresholds.min_improvement:g} points"
        ),
        evidence={"deltas": recent, "min_improvement": thresholds.min_improvement},
    )


def _entrenched_issue(
    history: Sequence[ScoreRound], thresholds: StagnationThresholds
) -> StagnationFinding | None:
    """plan/05: the same blocking issue survives two rewrites.

    Surviving *n* rewrites means appearing in *n + 1* consecutive rounds — the
    round that raised it plus the ones that failed to fix it.
    """
    window = thresholds.blocking_issue_rounds + 1
    if len(history) < window:
        return None
    recent = history[-window:]
    survivors = set(recent[0].blocking_issues).intersection(
        *(set(item.blocking_issues) for item in recent[1:])
    )
    if not survivors:
        return None
    named = sorted(survivors)
    return StagnationFinding(
        signal=StagnationSignal.ENTRENCHED,
        detail=(
            f"blocking issue(s) {', '.join(named)} survived "
            f"{thresholds.blocking_issue_rounds} rewrites"
        ),
        evidence={"issues": named, "rounds": [item.ordinal for item in recent]},
    )


def _oscillating(
    history: Sequence[ScoreRound], thresholds: StagnationThresholds
) -> StagnationFinding | None:
    """plan/05: oscillating scores — a real swing up then down, or down then up.

    Both swings must clear the improvement threshold. Movement below it is noise
    rather than oscillation, and is already caught as no improvement; treating
    it as both would report two problems where there is one.
    """
    deltas = _deltas(history)
    if len(deltas) < 2:
        return None
    last, previous = deltas[-1], deltas[-2]
    swinging = (last > 0) != (previous > 0)
    significant = min(abs(last), abs(previous)) >= thresholds.min_improvement
    if not (swinging and significant):
        return None
    return StagnationFinding(
        signal=StagnationSignal.OSCILLATING,
        detail=f"the overall score swung {previous:+g} then {last:+g}",
        evidence={"deltas": [previous, last]},
    )


def _diverging_dimensions(
    history: Sequence[ScoreRound], thresholds: StagnationThresholds
) -> StagnationFinding | None:
    """plan/05: one dimension improves while another deteriorates.

    The loop trading factual fidelity for voice is not progress, and the overall
    score — a weighted blend — can rise while it happens, which is exactly why
    this is checked per dimension rather than on the total.
    """
    if len(history) < 2:
        return None
    earlier, later = history[-2], history[-1]
    shared = set(earlier.dimensions) & set(later.dimensions)
    moves = {name: later.dimensions[name] - earlier.dimensions[name] for name in shared}
    gained = {
        name: delta for name, delta in moves.items() if delta >= thresholds.dimension_divergence
    }
    lost = {
        name: delta for name, delta in moves.items() if delta <= -thresholds.dimension_divergence
    }
    if not (gained and lost):
        return None
    return StagnationFinding(
        signal=StagnationSignal.DIVERGING,
        detail=(
            f"{', '.join(f'{name} {delta:+g}' for name, delta in sorted(gained.items()))} "
            f"while {', '.join(f'{name} {delta:+g}' for name, delta in sorted(lost.items()))}"
        ),
        evidence={"gained": gained, "lost": lost},
    )


def _not_better_than_parent(
    history: Sequence[ScoreRound], thresholds: StagnationThresholds
) -> StagnationFinding | None:
    """plan/05: the latest version is not measurably better than its parent."""
    if not history:
        return None
    latest = history[-1]
    if latest.parent_overall is None:
        return None
    gain = latest.overall - latest.parent_overall
    if gain >= thresholds.min_improvement:
        return None
    return StagnationFinding(
        signal=StagnationSignal.NOT_BETTER_THAN_PARENT,
        detail=(
            f"round {latest.ordinal} scored {latest.overall:g} against a parent's "
            f"{latest.parent_overall:g} ({gain:+g})"
        ),
        evidence={
            "overall": latest.overall,
            "parent_overall": latest.parent_overall,
            "gain": gain,
        },
    )


def _manual_editing(
    history: Sequence[ScoreRound], thresholds: StagnationThresholds
) -> StagnationFinding | None:
    """plan/05: high manual edit distance after repeated voice passes.

    The first voice pass against a new profile is expected to need work; it is
    the *repeated* one still being rewritten by hand that says the voice profile
    is the problem, not the draft.
    """
    passes = [item for item in history if item.voice_pass]
    if len(passes) < thresholds.voice_pass_rounds:
        return None
    distance = passes[-1].manual_edit_distance
    if distance is None or distance <= thresholds.max_edit_distance:
        return None
    return StagnationFinding(
        signal=StagnationSignal.MANUAL_EDITING,
        detail=(
            f"{distance:g} of the text was still edited by hand after {len(passes)} voice passes"
        ),
        evidence={
            "manual_edit_distance": distance,
            "voice_passes": len(passes),
            "max_edit_distance": thresholds.max_edit_distance,
        },
    )


__all__ = [
    "ScoreRound",
    "StagnationFinding",
    "StagnationSignal",
    "detect_stagnation",
]
