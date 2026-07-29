"""What a client may do next (phase 09).

plan/09 → *State-driven ``available_actions`` in responses*. Returned with every
command result so a UI never re-derives the workflow's rules — and, more to the
point, so it cannot hold a second, drifting opinion of them.

The list has exactly two sources:

1. **The transition table** (phase 05), which is the sole authority on what may
   legally happen next. Nothing here filters or extends it. An API that decided
   for itself which transitions were available would be a second state machine,
   and the plan's own risk note forbids exactly that.
2. **Execution affordances** — forking and replaying — which move nothing and
   are therefore available in every state, finished runs included. That is when
   comparing alternatives matters most.

Artefact edits (``PUT /projects/{id}/architecture/{ver}``, and the spec's
``edit_revision_plan``) are deliberately absent: editing an artefact is offered
by the artefact, not by the run's position, and a state that listed them would
be answering a different question from the one it was asked.
"""

from __future__ import annotations

from groundscribe.workflow.states import WorkflowState
from groundscribe.workflow.transitions import available_actions as transition_actions

#: Offered everywhere, because neither changes where the run is. Their endpoints
#: (``POST /executions/{id}/replay``, ``.../fork``) act on an execution, not on
#: the machine, so a terminal run still offers them.
EXECUTION_ACTIONS: tuple[str, ...] = ("fork_execution", "replay_execution")


def available_actions(state: WorkflowState) -> tuple[str, ...]:
    """Every action offered in ``state``, sorted and deduplicated.

    Sorted because a client compares successive responses to decide what
    changed; an order that varied between calls would look like the machine
    changing when only the iteration did.
    """
    names = {action.value for action in transition_actions(state)}
    return tuple(sorted(names | set(EXECUTION_ACTIONS)))


__all__ = ["EXECUTION_ACTIONS", "available_actions"]
