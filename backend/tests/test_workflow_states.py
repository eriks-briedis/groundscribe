"""The workflow state vocabulary and its transition table (phase 05).

Spec (plan/05):

- the state enum holding the spec's ~24 states;
- a *transition table with guards*, illegal transitions rejected;
- an ``available_actions(state)`` resolver returning the valid next actions;
- a *human-pause mechanism* — the engine parks at review/approval states.

These tests are about the table as data: what states exist, what may follow
what, and who is allowed to trigger it. Applying transitions is
``test_workflow_machine``; recording them is ``test_workflow_engine``.

The vocabulary is pinned exhaustively for the same reason phase 03 pins its
own: these values are written into decision records, so renaming a member
rewrites the meaning of history already on disk.
"""

from __future__ import annotations

import pytest

from groundscribe.provenance.enums import ActorType
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import (
    TERMINAL_STATES,
    TRANSITIONS,
    UNIVERSAL_ACTIONS,
    available_actions,
    human_pause_states,
    targets_for,
    transition_for,
    transitions_from,
)

#: Every state the spec names, with the value stored in provenance records.
EXPECTED_STATES = {
    "SOURCE_INGESTED": "source_ingested",
    "SOURCE_MODEL_EXTRACTING": "source_model_extracting",
    "SOURCE_QUESTIONS_REQUIRED": "source_questions_required",
    "SOURCE_MODEL_READY": "source_model_ready",
    "ARCHITECTURE_PROPOSING": "architecture_proposing",
    "ARCHITECTURE_REVIEW_REQUIRED": "architecture_review_required",
    "ARCHITECTURE_APPROVED": "architecture_approved",
    "BRIEF_GENERATING": "brief_generating",
    "BRIEF_REVIEW_REQUIRED": "brief_review_required",
    "DRAFT_GENERATING": "draft_generating",
    "SUBSTANTIVE_REVIEWING": "substantive_reviewing",
    "REVISION_PLAN_REQUIRED": "revision_plan_required",
    "SUBSTANTIVE_REWRITING": "substantive_rewriting",
    "VOICE_ALIGNING": "voice_aligning",
    "SCORING": "scoring",
    "REVISION_REQUIRED": "revision_required",
    "PASSED": "passed",
    "FINAL_VALIDATING": "final_validating",
    "HUMAN_APPROVAL_REQUIRED": "human_approval_required",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "STALLED": "stalled",
}

#: The exact action set each state offers, universal actions included.
EXPECTED_ACTIONS: dict[WorkflowState, set[WorkflowAction]] = {
    WorkflowState.SOURCE_INGESTED: {WorkflowAction.EXTRACT_SOURCE_MODEL},
    WorkflowState.SOURCE_MODEL_EXTRACTING: {
        WorkflowAction.REQUEST_ANSWERS,
        WorkflowAction.COMPLETE_EXTRACTION,
    },
    WorkflowState.SOURCE_QUESTIONS_REQUIRED: {WorkflowAction.ANSWER_QUESTIONS},
    WorkflowState.SOURCE_MODEL_READY: {WorkflowAction.PROPOSE_ARCHITECTURE},
    WorkflowState.ARCHITECTURE_PROPOSING: {WorkflowAction.SUBMIT_ARCHITECTURE},
    WorkflowState.ARCHITECTURE_REVIEW_REQUIRED: {
        WorkflowAction.APPROVE_ARCHITECTURE,
        WorkflowAction.REJECT_ARCHITECTURE,
    },
    WorkflowState.ARCHITECTURE_APPROVED: {
        WorkflowAction.GENERATE_BRIEF,
        WorkflowAction.REOPEN_ARCHITECTURE,
    },
    WorkflowState.BRIEF_GENERATING: {WorkflowAction.SUBMIT_BRIEF},
    WorkflowState.BRIEF_REVIEW_REQUIRED: {
        WorkflowAction.APPROVE_BRIEF,
        WorkflowAction.REJECT_BRIEF,
    },
    WorkflowState.DRAFT_GENERATING: {WorkflowAction.SUBMIT_DRAFT},
    WorkflowState.SUBSTANTIVE_REVIEWING: {
        WorkflowAction.REQUIRE_REVISION_PLAN,
        WorkflowAction.ACCEPT_REVIEW,
    },
    WorkflowState.REVISION_PLAN_REQUIRED: {
        WorkflowAction.APPROVE_REVISION_PLAN,
        WorkflowAction.RETURN_TO_BRIEF,
    },
    WorkflowState.SUBSTANTIVE_REWRITING: {WorkflowAction.SUBMIT_REWRITE},
    WorkflowState.VOICE_ALIGNING: {WorkflowAction.SUBMIT_VOICE_PASS},
    WorkflowState.SCORING: {WorkflowAction.SCORE_PASSED, WorkflowAction.SCORE_FAILED},
    WorkflowState.REVISION_REQUIRED: {
        WorkflowAction.ROUTE_REVISION,
        WorkflowAction.STALL,
        WorkflowAction.OVERRIDE_AND_APPROVE,
    },
    WorkflowState.PASSED: {WorkflowAction.VALIDATE_FINAL},
    WorkflowState.FINAL_VALIDATING: {
        WorkflowAction.VALIDATION_PASSED,
        WorkflowAction.VALIDATION_FAILED,
    },
    WorkflowState.HUMAN_APPROVAL_REQUIRED: {
        WorkflowAction.APPROVE_FINAL,
        WorkflowAction.REJECT_FINAL,
    },
    WorkflowState.STALLED: {
        WorkflowAction.AUTHORISE_REWRITE,
        WorkflowAction.RETURN_TO_BRIEF,
        WorkflowAction.REOPEN_ARCHITECTURE,
        WorkflowAction.OVERRIDE_AND_APPROVE,
    },
    WorkflowState.COMPLETED: set(),
    WorkflowState.FAILED: set(),
    WorkflowState.CANCELLED: set(),
}


def test_state_vocabulary_is_exactly_the_spec_list() -> None:
    """Renaming or dropping a state rewrites history already on disk."""
    assert {member.name: member.value for member in WorkflowState} == EXPECTED_STATES


def test_terminal_states_are_the_three_endings() -> None:
    """A run ends by finishing, failing, or being stopped — nothing else."""
    endings = {WorkflowState.COMPLETED, WorkflowState.FAILED, WorkflowState.CANCELLED}
    assert set(TERMINAL_STATES) == endings


def test_terminal_states_offer_no_actions() -> None:
    for state in TERMINAL_STATES:
        assert available_actions(state) == ()
        assert transitions_from(state) == ()


def test_available_actions_is_correct_for_every_state() -> None:
    """plan/05 exit criterion: ``available_actions(state)`` correct for every state."""
    for state in WorkflowState:
        expected = EXPECTED_ACTIONS[state]
        if state not in TERMINAL_STATES:
            expected = expected | set(UNIVERSAL_ACTIONS)
        assert set(available_actions(state)) == expected, state


def test_available_actions_are_ordered_and_unique() -> None:
    """A resolver feeding a UI must not offer the same action twice."""
    for state in WorkflowState:
        actions = available_actions(state)
        assert len(actions) == len(set(actions))
        assert list(actions) == sorted(actions)


def test_every_non_terminal_state_can_be_cancelled_or_failed() -> None:
    """A run must always be stoppable; nothing may trap it."""
    for state in WorkflowState:
        if state in TERMINAL_STATES:
            continue
        assert targets_for(state, WorkflowAction.CANCEL) == (WorkflowState.CANCELLED,)
        assert targets_for(state, WorkflowAction.FAIL) == (WorkflowState.FAILED,)


def test_every_state_is_reachable_from_the_entry_state() -> None:
    """An unreachable state is a modelling error, not a feature."""
    seen = {WorkflowState.SOURCE_INGESTED}
    frontier = [WorkflowState.SOURCE_INGESTED]
    while frontier:
        for transition in transitions_from(frontier.pop()):
            if transition.target not in seen:
                seen.add(transition.target)
                frontier.append(transition.target)
    assert seen == set(WorkflowState)


def test_transitions_never_leave_a_terminal_state() -> None:
    for transition in TRANSITIONS:
        assert transition.source not in TERMINAL_STATES


def test_transitions_are_unique_per_source_action_target() -> None:
    """A duplicated row would make one edge win silently over its twin."""
    keys = [(t.source, t.action, t.target) for t in TRANSITIONS]
    assert len(keys) == len(set(keys))


def test_human_pause_states_are_the_review_and_approval_states() -> None:
    """plan/05: the engine parks where a person is required.

    Derived from the table rather than listed beside it: a pause is exactly a
    state whose only real way forward needs a user, and a hand-maintained list
    would drift the first time an action is added.
    """
    assert human_pause_states() == frozenset(
        {
            WorkflowState.SOURCE_QUESTIONS_REQUIRED,
            WorkflowState.ARCHITECTURE_REVIEW_REQUIRED,
            WorkflowState.BRIEF_REVIEW_REQUIRED,
            WorkflowState.REVISION_PLAN_REQUIRED,
            WorkflowState.HUMAN_APPROVAL_REQUIRED,
            WorkflowState.STALLED,
        }
    )


def test_human_pause_states_only_advance_on_a_user_action() -> None:
    for state in human_pause_states():
        for transition in transitions_from(state):
            if transition.action in UNIVERSAL_ACTIONS:
                continue
            assert transition.actor is ActorType.USER, transition


def test_completed_is_reachable_only_through_final_validation() -> None:
    """plan/05 invariant: no export before ``FINAL_VALIDATING`` passes."""
    into_completed = [t for t in TRANSITIONS if t.target is WorkflowState.COMPLETED]
    assert [t.source for t in into_completed] == [WorkflowState.HUMAN_APPROVAL_REQUIRED]

    into_approval = [t for t in TRANSITIONS if t.target is WorkflowState.HUMAN_APPROVAL_REQUIRED]
    assert [t.source for t in into_approval] == [WorkflowState.FINAL_VALIDATING]


def test_approving_the_final_output_requires_a_user() -> None:
    """The last gate before export is a person, never a policy."""
    approval = transition_for(
        WorkflowState.HUMAN_APPROVAL_REQUIRED,
        WorkflowAction.APPROVE_FINAL,
        WorkflowState.COMPLETED,
    )
    assert approval is not None
    assert approval.actor is ActorType.USER


def test_transition_for_returns_none_when_the_edge_does_not_exist() -> None:
    assert (
        transition_for(
            WorkflowState.SOURCE_INGESTED,
            WorkflowAction.APPROVE_FINAL,
            WorkflowState.COMPLETED,
        )
        is None
    )


def test_transition_for_disambiguates_a_multi_target_action() -> None:
    """``ROUTE_REVISION`` has several legal destinations; the target picks one."""
    targets = targets_for(WorkflowState.REVISION_REQUIRED, WorkflowAction.ROUTE_REVISION)
    assert len(targets) > 1
    for target in targets:
        found = transition_for(
            WorkflowState.REVISION_REQUIRED, WorkflowAction.ROUTE_REVISION, target
        )
        assert found is not None
        assert found.target is target


def test_targets_for_an_unavailable_action_is_empty() -> None:
    assert targets_for(WorkflowState.SOURCE_INGESTED, WorkflowAction.SCORE_PASSED) == ()


@pytest.mark.parametrize(
    ("source", "action", "target"),
    [
        (
            WorkflowState.SOURCE_QUESTIONS_REQUIRED,
            WorkflowAction.ANSWER_QUESTIONS,
            WorkflowState.SOURCE_MODEL_EXTRACTING,
        ),
        (
            WorkflowState.SUBSTANTIVE_REWRITING,
            WorkflowAction.SUBMIT_REWRITE,
            WorkflowState.SUBSTANTIVE_REVIEWING,
        ),
        (
            WorkflowState.FINAL_VALIDATING,
            WorkflowAction.VALIDATION_FAILED,
            WorkflowState.REVISION_REQUIRED,
        ),
    ],
)
def test_the_loops_the_spec_requires_exist(
    source: WorkflowState, action: WorkflowAction, target: WorkflowState
) -> None:
    """Answers re-enter extraction, rewrites re-enter review, validation can bounce back."""
    assert transition_for(source, action, target) is not None
