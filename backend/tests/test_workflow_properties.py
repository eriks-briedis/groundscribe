"""Property tests over random valid action sequences (phase 05).

Spec (plan/05, test-first specification):

    **Property (Hypothesis):** for random valid action sequences, the machine
    never enters an illegal state and never exceeds rewrite limits without an
    approval action.

Driven against the pure machine. The example-based tests fix behaviour at the
points a person thought to look; these check the same invariants everywhere
else, which is the only way to be confident a table with 60-odd edges has no
path around them.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from groundscribe.provenance.enums import ActorType
from groundscribe.workflow.errors import HumanActionRequired, IllegalTransition
from groundscribe.workflow.machine import RewriteApproval, WorkflowMachine
from groundscribe.workflow.policy import FailureCategory, LimitKind
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import (
    TERMINAL_STATES,
    UNIVERSAL_ACTIONS,
    transition_for,
)
from workflow_helpers import sample_policy

A = WorkflowAction
S = WorkflowState

#: Long enough to run the revision loop past every limit several times over.
STEPS = 40

CATEGORIES = st.sampled_from(list(FailureCategory))

#: Escalations a person takes out of a pause. Each *is* an approval action —
#: nobody else needs to authorise what a person has just chosen — so each grants
#: the round it spends. Restated here rather than imported so the property
#: states its own expectation instead of echoing the implementation's.
ESCALATIONS: dict[WorkflowAction, LimitKind] = {
    A.AUTHORISE_REWRITE: LimitKind.SUBSTANTIVE,
    A.REOPEN_ARCHITECTURE: LimitKind.ARCHITECTURE,
}


def progressing_actions(machine: WorkflowMachine) -> list[WorkflowAction]:
    """The actions that move a run on, excluding the two that end it.

    Cancelling is always legal and always available, which a random walk would
    take almost immediately and then explore nothing. It is covered by its own
    example-based test.
    """
    return [action for action in machine.available_actions() if action not in UNIVERSAL_ACTIONS]


def test_routing_cannot_bypass_the_rewrite_ledger() -> None:
    """``ROUTE_REVISION`` is reachable only through ``route()``.

    Applied directly it would move the run to a correcting stage without
    charging the limit that bounds it, which is a hole the property test below
    would otherwise walk straight through — and so would a caller.
    """
    machine = WorkflowMachine(state=S.REVISION_REQUIRED, policy=sample_policy())
    with pytest.raises(IllegalTransition, match="route"):
        machine.apply(A.ROUTE_REVISION, target=S.VOICE_ALIGNING)
    assert machine.state is S.REVISION_REQUIRED
    assert machine.ledger.spent(LimitKind.STYLE) == 0


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_random_valid_sequences_stay_legal_and_bounded(data: st.DataObject) -> None:
    """The spec's property, in full.

    A walk of legal moves is generated, and after every one of them: the run is
    in a real state reached by a real edge, no bounded loop has run more rounds
    than it was allowed, and nothing has been exported without validation.
    """
    machine = WorkflowMachine(policy=sample_policy())
    offered: dict[LimitKind, int] = dict.fromkeys(LimitKind, 0)

    for _ in range(STEPS):
        actions = progressing_actions(machine)
        if not actions:
            break
        action = data.draw(st.sampled_from(actions), label="action")

        if action is A.ROUTE_REVISION:
            category = data.draw(CATEGORIES, label="category")
            approving = data.draw(st.booleans(), label="approving")
            approval = RewriteApproval(approved_by="ada") if approving else None
            result = machine.route(category, approval=approval)
            limit = result.outcome.limit
            if approving and limit is not None:
                offered[limit] += 1
        else:
            target = data.draw(st.sampled_from(machine.targets(action)), label="target")
            # A person may take any edge; the policy-only walk is the next test.
            machine.apply(action, target=target, actor=ActorType.USER)
            escalated = ESCALATIONS.get(action)
            if escalated is not None:
                offered[escalated] += 1

        # The run is somewhere real, reached by an edge that exists.
        assert machine.state in set(WorkflowState)
        last = machine.history[-1]
        assert transition_for(last.previous_state, last.action, last.state) is not None

        # No bounded loop ran more rounds than it was allowed, and the
        # allowance only ever grew because someone approved a round.
        for kind in LimitKind:
            assert machine.ledger.spent(kind) <= machine.ledger.allowance(kind)
            assert machine.ledger.approved(kind) <= offered[kind]

        if machine.state in TERMINAL_STATES:
            break

    visited = [outcome.state for outcome in machine.history]
    if S.COMPLETED in visited:
        # plan/05: nothing reaches export before final validation passes.
        assert S.FINAL_VALIDATING in visited[: visited.index(S.COMPLETED)]


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_a_policy_actor_can_never_walk_past_a_pause(data: st.DataObject) -> None:
    """The human-pause guarantee, checked at every state a random walk reaches.

    The engine acts as a policy. If any reachable review or approval state let a
    policy actor through, a run could be completed without the person whose
    judgement the state exists to collect.
    """
    machine = WorkflowMachine(policy=sample_policy())

    for _ in range(STEPS):
        actions = progressing_actions(machine)
        if not actions:
            break

        if machine.is_paused:
            for action in actions:
                with pytest.raises(HumanActionRequired):
                    machine.apply(action, target=machine.targets(action)[0])
            assert machine.state in set(WorkflowState)

        action = data.draw(st.sampled_from(actions), label="action")
        if action is A.ROUTE_REVISION:
            machine.route(data.draw(CATEGORIES, label="category"))
        else:
            target = data.draw(st.sampled_from(machine.targets(action)), label="target")
            machine.apply(action, target=target, actor=ActorType.USER)

        if machine.state in TERMINAL_STATES:
            break
