"""What a client may do next, per state (phase 09).

Spec (plan/09 → Test-first specification): *each state returns exactly the
spec's valid actions*, returned in every command response so a UI never has to
re-derive the workflow's rules.

The plan gives one illustrative list — ``revision_required`` offering
``approve_revision_plan``, ``edit_revision_plan``, ``return_to_brief``,
``fork_execution``, ``override_and_approve`` — and reading it carefully is what
shapes this module, because those five are not all the same kind of thing:

- ``approve_revision_plan`` and ``return_to_brief`` are *transitions* leaving
  ``REVISION_PLAN_REQUIRED`` in the phase-05 table; ``override_and_approve`` is
  a transition from the ``REVISION_REQUIRED`` state the run passes through on
  its way there. The spec is describing the revision pause as a person
  experiences it, spanning both machine states.
- ``fork_execution`` moves nothing. It is a provenance affordance, available
  wherever there is an execution to fork — including on a finished run, which is
  when comparing alternatives is most useful.
- ``edit_revision_plan`` is an artefact edit. Editing an artefact is offered by
  the artefact, not by the run's position, so it is not a state action; the same
  goes for ``PUT /projects/{id}/architecture/{ver}``.

So the answer has two sources: the transition table, which stays the sole
authority on what may legally happen next — an API keeping its own opinion would
be a second state machine, which plan/09 explicitly forbids — and the fixed pair
of execution affordances.
"""

from __future__ import annotations

import pytest

from groundscribe.app.actions import EXECUTION_ACTIONS, available_actions
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import TERMINAL_STATES
from groundscribe.workflow.transitions import available_actions as table_actions

A = WorkflowAction
S = WorkflowState


@pytest.mark.parametrize("state", list(WorkflowState))
def test_every_state_offers_exactly_its_transitions(state: WorkflowState) -> None:
    """The workflow half of the answer is the table's, verbatim.

    Not "a superset" and not "close enough": an action offered that the machine
    would refuse is a button that fails, and one withheld that the machine would
    accept is a run nobody can move.
    """
    offered = set(available_actions(state))
    workflow_names = {action.value for action in WorkflowAction}

    assert offered & workflow_names == {action.value for action in table_actions(state)}


@pytest.mark.parametrize("state", list(WorkflowState))
def test_forking_and_replaying_are_offered_in_every_state(state: WorkflowState) -> None:
    """Provenance affordances do not depend on where the run got to."""
    assert set(EXECUTION_ACTIONS) <= set(available_actions(state))


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_a_finished_run_offers_nothing_that_would_move_it(state: WorkflowState) -> None:
    """Terminal means terminal: only inspection remains."""
    assert available_actions(state) == EXECUTION_ACTIONS


def test_the_revision_pause_offers_what_the_spec_illustrates() -> None:
    """The plan's example list, reconciled against the phase-05 table.

    Written as one test rather than spread over the two states because the
    reconciliation *is* the claim: the spec's ``revision_required`` is the
    revision pause a person sees, and the actions it names are distributed
    across the two machine states that pause consists of.
    """
    failing = set(available_actions(S.REVISION_REQUIRED))
    planning = set(available_actions(S.REVISION_PLAN_REQUIRED))

    assert A.OVERRIDE_AND_APPROVE.value in failing
    assert {A.APPROVE_REVISION_PLAN.value, A.RETURN_TO_BRIEF.value} <= planning
    assert "fork_execution" in failing & planning


def test_a_human_pause_offers_no_action_the_engine_could_take_alone() -> None:
    """A pause is a pause: nothing on offer there advances the run by policy.

    ``fail`` is the exception and belongs to nobody — a run must always be
    stoppable (plan/05 → universal actions), including one waiting for a person
    who never comes back.
    """
    offered = set(available_actions(S.HUMAN_APPROVAL_REQUIRED))

    assert offered & {action.value for action in WorkflowAction} == {
        A.APPROVE_FINAL.value,
        A.REJECT_FINAL.value,
        A.CANCEL.value,
        A.FAIL.value,
    }


def test_actions_are_sorted_and_free_of_duplicates() -> None:
    """A set with an order, so two responses can be compared.

    plan/05 already orders the table's actions for this reason; the assembled
    list keeps the property, or a UI diffing responses sees changes that are
    only reordering.
    """
    for state in WorkflowState:
        offered = available_actions(state)
        assert list(offered) == sorted(set(offered))


def test_every_offered_action_is_one_the_api_implements() -> None:
    """Nothing is advertised that a client cannot then call."""
    known = {action.value for action in WorkflowAction} | set(EXECUTION_ACTIONS)

    for state in WorkflowState:
        assert set(available_actions(state)) <= known
