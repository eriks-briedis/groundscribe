"""Applying transitions: the pure state machine (phase 05).

Spec (plan/05):

- illegal transitions are *rejected*, not tolerated;
- the engine parks at review/approval states (human-pause mechanism);
- an article cannot reach ``COMPLETED``/export before ``FINAL_VALIDATING``
  passes.

The machine under test holds no database. Transition rules are the part of
phase 05 that has to be provable, and rules exercised only through a recorder
are rules whose failures arrive dressed as persistence problems.
"""

from __future__ import annotations

import pytest

from groundscribe.provenance.enums import ActorType
from groundscribe.workflow.errors import (
    AmbiguousTransition,
    HumanActionRequired,
    IllegalTransition,
)
from groundscribe.workflow.machine import WorkflowMachine
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import TERMINAL_STATES

A = WorkflowAction
S = WorkflowState


def test_a_new_machine_starts_at_the_entry_state() -> None:
    assert WorkflowMachine().state is S.SOURCE_INGESTED


def test_a_machine_can_be_resumed_at_a_given_state() -> None:
    """Phase 09 rehydrates a parked run; the machine must accept a start state."""
    assert WorkflowMachine(state=S.SCORING).state is S.SCORING


def test_applying_a_legal_action_moves_the_machine() -> None:
    machine = WorkflowMachine()
    outcome = machine.apply(A.EXTRACT_SOURCE_MODEL)
    assert outcome.previous_state is S.SOURCE_INGESTED
    assert outcome.state is S.SOURCE_MODEL_EXTRACTING
    assert machine.state is S.SOURCE_MODEL_EXTRACTING


def test_the_outcome_carries_the_edge_that_was_taken() -> None:
    """The engine records the edge's actor and rationale; it must come back out."""
    machine = WorkflowMachine(state=S.SUBSTANTIVE_REVIEWING)
    outcome = machine.apply(A.ACCEPT_REVIEW)
    assert outcome.transition.actor is ActorType.POLICY
    assert outcome.transition.rationale


def test_an_action_the_state_does_not_offer_is_rejected() -> None:
    machine = WorkflowMachine()
    with pytest.raises(IllegalTransition):
        machine.apply(A.APPROVE_FINAL)


def test_a_rejected_transition_leaves_the_state_untouched() -> None:
    """A guard that half-applied would be worse than no guard."""
    machine = WorkflowMachine()
    with pytest.raises(IllegalTransition):
        machine.apply(A.SCORE_PASSED)
    assert machine.state is S.SOURCE_INGESTED


def test_a_legal_action_with_an_illegal_target_is_rejected() -> None:
    """``ROUTE_REVISION`` is legal here, but never to ``COMPLETED``."""
    machine = WorkflowMachine(state=S.REVISION_REQUIRED)
    with pytest.raises(IllegalTransition):
        machine.apply(A.ROUTE_REVISION, target=S.COMPLETED)
    assert machine.state is S.REVISION_REQUIRED


def test_terminal_states_accept_nothing() -> None:
    for state in TERMINAL_STATES:
        machine = WorkflowMachine(state=state)
        with pytest.raises(IllegalTransition):
            machine.apply(A.CANCEL, actor=ActorType.USER)


def test_a_single_target_action_needs_no_target() -> None:
    machine = WorkflowMachine(state=S.PASSED)
    assert machine.apply(A.VALIDATE_FINAL).state is S.FINAL_VALIDATING


def test_a_multi_target_action_without_a_target_is_ambiguous() -> None:
    """Guessing one of seven destinations would make routing unenforceable."""
    machine = WorkflowMachine(state=S.REVISION_REQUIRED)
    with pytest.raises(AmbiguousTransition):
        machine.apply(A.ROUTE_REVISION)
    assert machine.state is S.REVISION_REQUIRED


def test_the_engine_cannot_step_past_a_human_pause() -> None:
    """plan/05 human-pause: a policy actor may not take a user's edge."""
    machine = WorkflowMachine(state=S.HUMAN_APPROVAL_REQUIRED)
    with pytest.raises(HumanActionRequired):
        machine.apply(A.APPROVE_FINAL, actor=ActorType.POLICY)
    assert machine.state is S.HUMAN_APPROVAL_REQUIRED


def test_a_user_may_take_a_user_edge() -> None:
    machine = WorkflowMachine(state=S.HUMAN_APPROVAL_REQUIRED)
    assert machine.apply(A.APPROVE_FINAL, actor=ActorType.USER).state is S.COMPLETED


def test_a_user_may_also_trigger_a_policy_edge() -> None:
    """Phase 09's commands are user-initiated; the guard runs one way only."""
    machine = WorkflowMachine(state=S.PASSED)
    assert machine.apply(A.VALIDATE_FINAL, actor=ActorType.USER).state is S.FINAL_VALIDATING


def test_the_machine_reports_when_it_is_parked() -> None:
    assert WorkflowMachine(state=S.BRIEF_REVIEW_REQUIRED).is_paused
    assert not WorkflowMachine(state=S.DRAFT_GENERATING).is_paused


def test_available_actions_follows_the_current_state() -> None:
    machine = WorkflowMachine(state=S.SCORING)
    assert A.SCORE_PASSED in machine.available_actions()
    machine.apply(A.SCORE_PASSED)
    assert A.SCORE_PASSED not in machine.available_actions()
    assert A.VALIDATE_FINAL in machine.available_actions()


def test_the_machine_keeps_the_transitions_it_took() -> None:
    """Rewrite limits and stagnation both read the run's own history."""
    machine = WorkflowMachine()
    machine.apply(A.EXTRACT_SOURCE_MODEL)
    machine.apply(A.COMPLETE_EXTRACTION)
    assert [outcome.state for outcome in machine.history] == [
        S.SOURCE_MODEL_EXTRACTING,
        S.SOURCE_MODEL_READY,
    ]


def test_a_run_can_always_be_cancelled_by_a_person() -> None:
    for state in WorkflowState:
        if state in TERMINAL_STATES:
            continue
        machine = WorkflowMachine(state=state)
        assert machine.apply(A.CANCEL, actor=ActorType.USER).state is S.CANCELLED


def test_completion_is_unreachable_without_passing_final_validation() -> None:
    """plan/05 invariant: no ``COMPLETED``/export before ``FINAL_VALIDATING``."""
    machine = WorkflowMachine(state=S.PASSED)
    with pytest.raises(IllegalTransition):
        machine.apply(A.APPROVE_FINAL, actor=ActorType.USER)

    machine.apply(A.VALIDATE_FINAL)
    with pytest.raises(IllegalTransition):
        machine.apply(A.APPROVE_FINAL, actor=ActorType.USER)

    machine.apply(A.VALIDATION_PASSED)
    assert machine.apply(A.APPROVE_FINAL, actor=ActorType.USER).state is S.COMPLETED
    assert [outcome.state for outcome in machine.history] == [
        S.FINAL_VALIDATING,
        S.HUMAN_APPROVAL_REQUIRED,
        S.COMPLETED,
    ]


def test_a_failed_validation_returns_to_routing_not_to_export() -> None:
    machine = WorkflowMachine(state=S.FINAL_VALIDATING)
    assert machine.apply(A.VALIDATION_FAILED).state is S.REVISION_REQUIRED


def test_the_happy_path_walks_from_ingest_to_completed() -> None:
    """One end-to-end walk, so the table is known to compose, not just to exist."""
    machine = WorkflowMachine()
    steps: list[tuple[WorkflowAction, ActorType]] = [
        (A.EXTRACT_SOURCE_MODEL, ActorType.POLICY),
        (A.COMPLETE_EXTRACTION, ActorType.POLICY),
        (A.PROPOSE_ARCHITECTURE, ActorType.POLICY),
        (A.SUBMIT_ARCHITECTURE, ActorType.POLICY),
        (A.APPROVE_ARCHITECTURE, ActorType.USER),
        (A.GENERATE_BRIEF, ActorType.POLICY),
        (A.SUBMIT_BRIEF, ActorType.POLICY),
        (A.APPROVE_BRIEF, ActorType.USER),
        (A.SUBMIT_DRAFT, ActorType.POLICY),
        (A.ACCEPT_REVIEW, ActorType.POLICY),
        (A.SUBMIT_VOICE_PASS, ActorType.POLICY),
        (A.SCORE_PASSED, ActorType.POLICY),
        (A.VALIDATE_FINAL, ActorType.POLICY),
        (A.VALIDATION_PASSED, ActorType.POLICY),
        (A.APPROVE_FINAL, ActorType.USER),
    ]
    for action, actor in steps:
        machine.apply(action, actor=actor)
    assert machine.state is S.COMPLETED
