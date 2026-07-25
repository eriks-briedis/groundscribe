"""Stagnation detection over the revision loop's history (phase 05).

Spec (plan/05 → *Stagnation detection*), all six conditions:

- improvement < 2 points for two rounds;
- the same blocking issue survives two rewrites;
- oscillating scores;
- one dimension improves while another deteriorates;
- the latest version is not measurably better than its parent;
- high manual edit distance after repeated voice passes

→ route to ``STALLED`` / human decision.

Each condition gets a test that fires it and a counterexample that does not, so
a detector that simply returned every signal would fail here.
"""

from __future__ import annotations

import pytest

from groundscribe.workflow.errors import IllegalTransition
from groundscribe.workflow.policy import StagnationThresholds
from groundscribe.workflow.stagnation import (
    ScoreRound,
    StagnationSignal,
    detect_stagnation,
)
from groundscribe.workflow.states import WorkflowState
from workflow_helpers import machine_at, sample_policy

S = WorkflowState
G = StagnationSignal

THRESHOLDS = StagnationThresholds()


def rounds(*overalls: float) -> list[ScoreRound]:
    """A plain history of overall scores, one round each."""
    return [ScoreRound(ordinal=index, overall=value) for index, value in enumerate(overalls, 1)]


def signals(history: list[ScoreRound], thresholds: StagnationThresholds = THRESHOLDS) -> set[str]:
    return {finding.signal for finding in detect_stagnation(history, thresholds)}


def test_an_empty_history_is_not_stagnant() -> None:
    assert detect_stagnation([], THRESHOLDS) == ()


def test_a_single_round_is_not_stagnant() -> None:
    """One score is a starting point, not a trend."""
    assert detect_stagnation(rounds(70.0), THRESHOLDS) == ()


def test_steady_improvement_is_not_stagnant() -> None:
    assert signals(rounds(60.0, 70.0, 80.0)) == set()


def test_two_rounds_below_the_improvement_threshold_stagnate() -> None:
    """plan/05: improvement < 2 points for two rounds."""
    assert G.NO_IMPROVEMENT in signals(rounds(70.0, 71.0, 71.5))


def test_one_flat_round_is_not_yet_stagnation() -> None:
    """A single flat round is common and often followed by a good one."""
    assert G.NO_IMPROVEMENT not in signals(rounds(60.0, 70.0, 70.5))


def test_the_improvement_threshold_comes_from_the_policy() -> None:
    """plan/05 Risks: thresholds live in the versioned policy, never inline."""
    generous = StagnationThresholds(min_improvement=10.0)
    assert G.NO_IMPROVEMENT in signals(rounds(60.0, 65.0, 70.0), generous)
    assert G.NO_IMPROVEMENT not in signals(rounds(60.0, 65.0, 70.0))


def test_a_blocking_issue_that_survives_two_rewrites_is_entrenched() -> None:
    """plan/05: the same blocking issue survives two rewrites."""
    history = [
        ScoreRound(ordinal=1, overall=60.0, blocking_issues=("unsupported-claim-7",)),
        ScoreRound(ordinal=2, overall=70.0, blocking_issues=("unsupported-claim-7",)),
        ScoreRound(ordinal=3, overall=80.0, blocking_issues=("unsupported-claim-7", "thin-intro")),
    ]
    finding = next(f for f in detect_stagnation(history, THRESHOLDS) if f.signal is G.ENTRENCHED)
    assert "unsupported-claim-7" in finding.detail


def test_a_blocking_issue_that_gets_fixed_is_not_entrenched() -> None:
    history = [
        ScoreRound(ordinal=1, overall=60.0, blocking_issues=("unsupported-claim-7",)),
        ScoreRound(ordinal=2, overall=70.0, blocking_issues=("unsupported-claim-7",)),
        ScoreRound(ordinal=3, overall=80.0, blocking_issues=()),
    ]
    assert G.ENTRENCHED not in {f.signal for f in detect_stagnation(history, THRESHOLDS)}


def test_scores_that_swing_up_then_down_are_oscillating() -> None:
    """plan/05: oscillating scores."""
    assert G.OSCILLATING in signals(rounds(60.0, 75.0, 62.0))


def test_scores_that_swing_down_then_up_are_oscillating() -> None:
    assert G.OSCILLATING in signals(rounds(75.0, 60.0, 74.0))


def test_small_wobbles_are_not_oscillation() -> None:
    """Movement below the improvement threshold is noise, and already covered."""
    assert G.OSCILLATING not in signals(rounds(70.0, 71.0, 70.2))


def test_one_dimension_improving_while_another_falls_is_stagnation() -> None:
    """plan/05: one dimension improves while another deteriorates."""
    history = [
        ScoreRound(ordinal=1, overall=70.0, dimensions={"factual_fidelity": 90, "voice": 80}),
        ScoreRound(ordinal=2, overall=71.0, dimensions={"factual_fidelity": 70, "voice": 95}),
    ]
    finding = next(f for f in detect_stagnation(history, THRESHOLDS) if f.signal is G.DIVERGING)
    assert "voice" in finding.detail
    assert "factual_fidelity" in finding.detail


def test_dimensions_that_all_improve_are_not_diverging() -> None:
    history = [
        ScoreRound(ordinal=1, overall=70.0, dimensions={"factual_fidelity": 70, "voice": 70}),
        ScoreRound(ordinal=2, overall=85.0, dimensions={"factual_fidelity": 85, "voice": 90}),
    ]
    assert G.DIVERGING not in {f.signal for f in detect_stagnation(history, THRESHOLDS)}


def test_a_version_no_better_than_its_parent_is_stagnation() -> None:
    """plan/05: the latest is not measurably better than its parent."""
    history = [ScoreRound(ordinal=1, overall=80.5, parent_overall=80.0)]
    assert G.NOT_BETTER_THAN_PARENT in {f.signal for f in detect_stagnation(history, THRESHOLDS)}


def test_a_version_clearly_better_than_its_parent_is_not_stagnation() -> None:
    history = [ScoreRound(ordinal=1, overall=90.0, parent_overall=80.0)]
    assert detect_stagnation(history, THRESHOLDS) == ()


def test_heavy_manual_editing_after_repeated_voice_passes_is_stagnation() -> None:
    """plan/05: high manual edit distance after repeated voice passes."""
    history = [
        ScoreRound(ordinal=1, overall=80.0, voice_pass=True, manual_edit_distance=0.4),
        ScoreRound(ordinal=2, overall=82.0, voice_pass=True, manual_edit_distance=0.5),
    ]
    finding = next(
        f for f in detect_stagnation(history, THRESHOLDS) if f.signal is G.MANUAL_EDITING
    )
    assert "0.5" in finding.detail


def test_heavy_editing_after_a_single_voice_pass_is_not_yet_stagnation() -> None:
    """The first voice pass on a new profile is expected to need work."""
    history = [ScoreRound(ordinal=1, overall=80.0, voice_pass=True, manual_edit_distance=0.5)]
    assert G.MANUAL_EDITING not in {f.signal for f in detect_stagnation(history, THRESHOLDS)}


def test_light_editing_after_repeated_voice_passes_is_not_stagnation() -> None:
    history = [
        ScoreRound(ordinal=1, overall=80.0, voice_pass=True, manual_edit_distance=0.05),
        ScoreRound(ordinal=2, overall=90.0, voice_pass=True, manual_edit_distance=0.02),
    ]
    assert G.MANUAL_EDITING not in {f.signal for f in detect_stagnation(history, THRESHOLDS)}


def test_findings_are_reported_in_a_stable_order() -> None:
    """A stalled run's explanation must not reorder itself between reads."""
    history = [
        ScoreRound(ordinal=1, overall=70.0, dimensions={"a": 90, "b": 70}, parent_overall=70.0),
        ScoreRound(ordinal=2, overall=70.5, dimensions={"a": 70, "b": 90}, parent_overall=70.0),
        ScoreRound(ordinal=3, overall=71.0, dimensions={"a": 70, "b": 90}, parent_overall=70.0),
    ]
    found = detect_stagnation(history, THRESHOLDS)
    assert len(found) > 1
    assert [f.signal for f in found] == sorted(f.signal for f in found)


def test_every_finding_carries_the_evidence_for_it() -> None:
    """A stall a person cannot audit is a dead end, not a decision."""
    for finding in detect_stagnation(rounds(70.0, 71.0, 71.5), THRESHOLDS):
        assert finding.detail
        assert finding.evidence


def test_a_stagnant_check_parks_the_run() -> None:
    """plan/05: stagnation conditions route to ``STALLED`` / human decision."""
    machine = machine_at(S.REVISION_REQUIRED)
    check = machine.check_stagnation(rounds(70.0, 71.0, 71.5))
    assert check.stalled
    assert check.findings
    assert machine.state is S.STALLED
    assert machine.is_paused


def test_a_healthy_check_leaves_the_run_where_it_was() -> None:
    machine = machine_at(S.REVISION_REQUIRED)
    check = machine.check_stagnation(rounds(60.0, 70.0, 80.0))
    assert not check.stalled
    assert check.findings == ()
    assert machine.state is S.REVISION_REQUIRED


def test_stagnation_is_checked_at_the_routing_point_only() -> None:
    """Stalling from the middle of a stage would abandon work in progress."""
    machine = machine_at(S.DRAFT_GENERATING)
    with pytest.raises(IllegalTransition):
        machine.check_stagnation(rounds(70.0, 71.0, 71.5))


def test_the_machine_uses_its_own_policy_thresholds() -> None:
    policy = sample_policy(stagnation=StagnationThresholds(min_improvement=10.0))
    machine = machine_at(S.REVISION_REQUIRED, policy=policy)
    assert machine.check_stagnation(rounds(60.0, 65.0, 70.0)).stalled
