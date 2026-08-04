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

from collections.abc import Mapping
from dataclasses import dataclass

from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import available_actions as transition_actions

#: Offered everywhere, because neither changes where the run is. Their endpoints
#: (``POST /executions/{id}/replay``, ``.../fork``) act on an execution, not on
#: the machine, so a terminal run still offers them.
EXECUTION_ACTIONS: tuple[str, ...] = ("fork_execution", "replay_execution")


@dataclass(frozen=True)
class Endpoint:
    """One API affordance: how a thing is done, and to what.

    ``scope`` says which id the template needs, so an action offered where that
    id is unknown — an article action seen from the project dashboard — reports
    itself as unavailable instead of producing a URL with a hole in it.
    """

    method: str
    template: str
    scope: str
    requires_actor: bool = False


PROJECT = "project"
ARTICLE = "article"

#: Which endpoint performs which action (phase 11).
#:
#: The table exists because the alternative is worse. ``available_actions`` gives
#: a client the *names* of what may happen next; turning a name into a request
#: needs the API's own shape, and a frontend that kept that mapping would hold a
#: second copy of the API which nothing tests. Here it sits beside the list it
#: annotates, in the module that already answers "what may be done next".
#:
#: Absent deliberately: every action no endpoint performs. ``fail``, ``stall``,
#: ``route_revision`` and the ``submit_*`` edges are the machine's own — a person
#: does not take them, and inventing a URL for them would say they could.
ACTION_ENDPOINTS: Mapping[WorkflowAction, Endpoint] = {
    WorkflowAction.EXTRACT_SOURCE_MODEL: Endpoint(
        "POST", "/projects/{project_id}/source-model/extract", PROJECT
    ),
    WorkflowAction.PROPOSE_ARCHITECTURE: Endpoint(
        "POST", "/projects/{project_id}/architecture/propose", PROJECT
    ),
    WorkflowAction.APPROVE_ARCHITECTURE: Endpoint(
        "POST",
        "/projects/{project_id}/architecture/current/approve",
        PROJECT,
        requires_actor=True,
    ),
    WorkflowAction.CANCEL: Endpoint(
        "POST", "/projects/{project_id}/cancel", PROJECT, requires_actor=True
    ),
    WorkflowAction.GENERATE_BRIEF: Endpoint(
        "POST", "/articles/{article_id}/brief/generate", ARTICLE
    ),
    WorkflowAction.APPROVE_BRIEF: Endpoint(
        "POST", "/articles/{article_id}/brief/approve", ARTICLE, requires_actor=True
    ),
    WorkflowAction.REQUIRE_REVISION_PLAN: Endpoint(
        "POST", "/articles/{article_id}/revision-plan", ARTICLE
    ),
    WorkflowAction.APPROVE_REVISION_PLAN: Endpoint(
        "POST", "/articles/{article_id}/revision-plan/approve", ARTICLE, requires_actor=True
    ),
    WorkflowAction.VALIDATE_FINAL: Endpoint("POST", "/articles/{article_id}/validate", ARTICLE),
    WorkflowAction.APPROVE_FINAL: Endpoint(
        "POST", "/articles/{article_id}/approve", ARTICLE, requires_actor=True
    ),
    WorkflowAction.OVERRIDE_AND_APPROVE: Endpoint(
        "POST", "/articles/{article_id}/override-approve", ARTICLE, requires_actor=True
    ),
}

#: The command that hands an answered round of questions back to the pipeline.
#:
#: Deliberately not in :data:`ACTION_ENDPOINTS`. A dashboard renders that table
#: as buttons, and a button labelled "answer questions" that submitted the round
#: would submit whatever happened to be answered so far — including nothing at
#: all. The command belongs to the queue screen, which is the one place that
#: shows what is about to be handed over.
SUBMIT_ANSWERS = Endpoint(
    "POST", "/projects/{project_id}/source-questions/submit", PROJECT, requires_actor=True
)

#: The command that runs failed work again.
#:
#: Not in :data:`ACTION_ENDPOINTS` either, and for a different reason: it is not
#: an action the transition table offers, because it takes no edge. A run whose
#: job failed is already in the state that job was carrying it out of, so the
#: recovery is to re-queue the work — and a table of *transitions* is the wrong
#: place to look for it.
RETRY_FAILED = Endpoint("POST", "/projects/{project_id}/retry", PROJECT, requires_actor=True)

#: The command that starts the work a state is waiting for.
#:
#: A state ending in ``-ing`` was entered by the approval before it, so no
#: *action* remains to describe what happens next — but a job still has to be
#: queued. Without this, every client would need its own map from state to
#: endpoint, which is the same duplication ``ACTION_ENDPOINTS`` exists to avoid.
STATE_COMMANDS: Mapping[WorkflowState, Endpoint] = {
    WorkflowState.DRAFT_GENERATING: Endpoint("POST", "/articles/{article_id}/draft", ARTICLE),
    WorkflowState.SUBSTANTIVE_REVIEWING: Endpoint("POST", "/articles/{article_id}/review", ARTICLE),
    WorkflowState.REVISION_PLAN_REQUIRED: Endpoint(
        "POST", "/articles/{article_id}/revision-plan", ARTICLE
    ),
    WorkflowState.SUBSTANTIVE_REWRITING: Endpoint(
        "POST", "/articles/{article_id}/rewrite", ARTICLE
    ),
    WorkflowState.VOICE_ALIGNING: Endpoint("POST", "/articles/{article_id}/voice-align", ARTICLE),
    WorkflowState.SCORING: Endpoint("POST", "/articles/{article_id}/score", ARTICLE),
    WorkflowState.PASSED: Endpoint("POST", "/articles/{article_id}/validate", ARTICLE),
}


def resolve(
    endpoint: Endpoint | None, *, project_id: str | None, article_id: str | None
) -> str | None:
    """The URL that performs an action here, or ``None`` where it cannot be built."""
    if endpoint is None:
        return None
    if endpoint.scope == PROJECT and project_id:
        return endpoint.template.format(project_id=project_id)
    if endpoint.scope == ARTICLE and article_id:
        return endpoint.template.format(article_id=article_id)
    return None


def available_actions(state: WorkflowState) -> tuple[str, ...]:
    """Every action offered in ``state``, sorted and deduplicated.

    Sorted because a client compares successive responses to decide what
    changed; an order that varied between calls would look like the machine
    changing when only the iteration did.
    """
    names = {action.value for action in transition_actions(state)}
    return tuple(sorted(names | set(EXECUTION_ACTIONS)))


__all__ = [
    "ACTION_ENDPOINTS",
    "ARTICLE",
    "EXECUTION_ACTIONS",
    "PROJECT",
    "RETRY_FAILED",
    "STATE_COMMANDS",
    "SUBMIT_ANSWERS",
    "Endpoint",
    "available_actions",
    "resolve",
]
