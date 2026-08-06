"""The transition table: every legal edge of the editorial workflow (phase 05).

plan/05 → *Transition table with guards; illegal transitions rejected*. The
table is data, deliberately: a state machine written as ``if`` statements
scattered through stage code cannot be enumerated, drawn, or checked for
unreachable states, and phase 09 has to hand its edges to an API as
``available_actions``.

Each row carries the actor allowed to trigger it. That single field is the
human-pause mechanism (plan/05): a state whose every real edge needs a
``USER`` actor is a state the engine cannot leave on its own, so parking is a
consequence of the table rather than a second mechanism bolted beside it.

``CANCEL`` and ``FAIL`` are expanded onto every non-terminal state instead of
being written out 20 times. A run must always be stoppable, and a table where
that is restated per row is a table where it will eventually be forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass

from groundscribe.provenance.enums import ActorType
from groundscribe.workflow.states import WorkflowAction, WorkflowState

S = WorkflowState
A = WorkflowAction

#: The endings. Nothing leaves them.
TERMINAL_STATES: frozenset[WorkflowState] = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED})

#: Available from every non-terminal state, so no run can be trapped.
UNIVERSAL_ACTIONS: frozenset[WorkflowAction] = frozenset({A.CANCEL, A.FAIL})


@dataclass(frozen=True)
class Transition:
    """One legal edge, and who may take it.

    ``rationale`` is not decoration: it is copied into the decision record the
    engine writes for the transition, so a reader of the provenance sees why the
    edge exists without going back to this file.
    """

    source: WorkflowState
    action: WorkflowAction
    target: WorkflowState
    actor: ActorType
    rationale: str = ""


def _policy(
    source: WorkflowState, action: WorkflowAction, target: WorkflowState, rationale: str = ""
) -> Transition:
    """An edge the engine may take on its own, under its versioned policy."""
    return Transition(source, action, target, ActorType.POLICY, rationale)


def _user(
    source: WorkflowState, action: WorkflowAction, target: WorkflowState, rationale: str = ""
) -> Transition:
    """An edge only a person may take. These are what make a state a pause."""
    return Transition(source, action, target, ActorType.USER, rationale)


#: The editorial edges, in pipeline order. Endings are appended below.
_EDITORIAL_TRANSITIONS: tuple[Transition, ...] = (
    # Source model. Extraction either has everything it needs or it does not;
    # the gap path parks for the author rather than guessing an answer
    # (plan/00 → source truth is separate from prose).
    _policy(S.SOURCE_INGESTED, A.EXTRACT_SOURCE_MODEL, S.SOURCE_MODEL_EXTRACTING),
    _policy(
        S.SOURCE_MODEL_EXTRACTING,
        A.REQUEST_ANSWERS,
        S.SOURCE_QUESTIONS_REQUIRED,
        "extraction found gaps only the author can close",
    ),
    _policy(S.SOURCE_MODEL_EXTRACTING, A.COMPLETE_EXTRACTION, S.SOURCE_MODEL_READY),
    _user(
        S.SOURCE_QUESTIONS_REQUIRED,
        A.ANSWER_QUESTIONS,
        S.SOURCE_MODEL_EXTRACTING,
        "answers re-enter extraction so the source model is rebuilt, not patched",
    ),
    # Architecture. Approval is a human gate, and rejection returns to proposal
    # rather than skipping ahead with an unapproved structure.
    _policy(S.SOURCE_MODEL_READY, A.PROPOSE_ARCHITECTURE, S.ARCHITECTURE_PROPOSING),
    _policy(S.ARCHITECTURE_PROPOSING, A.SUBMIT_ARCHITECTURE, S.ARCHITECTURE_REVIEW_REQUIRED),
    _user(S.ARCHITECTURE_REVIEW_REQUIRED, A.APPROVE_ARCHITECTURE, S.ARCHITECTURE_APPROVED),
    _user(S.ARCHITECTURE_REVIEW_REQUIRED, A.REJECT_ARCHITECTURE, S.ARCHITECTURE_PROPOSING),
    # The way back out of a proposal that cannot land. Every other `-ing` state
    # recovers by running its job again (`retry_failed_job`), and so does this one
    # while nothing is approved yet. Once an architecture *is* approved, a
    # proposal over it needs lineage and an override — which is a person's to
    # give — so a failed one is not work to retry but work to give up on, and
    # without this edge the run's only remaining moves are cancel and fail.
    #
    # Does not make `architecture_proposing` a state the engine parks in:
    # `is_human_pause` asks whether *every* non-universal edge is a person's, and
    # `submit_architecture` is still the pipeline's.
    _user(
        S.ARCHITECTURE_PROPOSING,
        A.ABANDON_PROPOSAL,
        S.ARCHITECTURE_APPROVED,
        "the proposal is given up; the approved architecture stands",
    ),
    _policy(S.ARCHITECTURE_APPROVED, A.GENERATE_BRIEF, S.BRIEF_GENERATING),
    _user(
        S.ARCHITECTURE_APPROVED,
        A.REOPEN_ARCHITECTURE,
        S.ARCHITECTURE_PROPOSING,
        "an approved architecture reopens only on a person's say-so",
    ),
    # Brief.
    _policy(S.BRIEF_GENERATING, A.SUBMIT_BRIEF, S.BRIEF_REVIEW_REQUIRED),
    _user(S.BRIEF_REVIEW_REQUIRED, A.APPROVE_BRIEF, S.DRAFT_GENERATING),
    _user(S.BRIEF_REVIEW_REQUIRED, A.REJECT_BRIEF, S.BRIEF_GENERATING),
    # Draft, substantive review, rewrite.
    _policy(S.DRAFT_GENERATING, A.SUBMIT_DRAFT, S.SUBSTANTIVE_REVIEWING),
    _policy(S.SUBSTANTIVE_REVIEWING, A.REQUIRE_REVISION_PLAN, S.REVISION_PLAN_REQUIRED),
    _policy(
        S.SUBSTANTIVE_REVIEWING,
        A.ACCEPT_REVIEW,
        S.VOICE_ALIGNING,
        "substance is settled, so what is left is how it reads",
    ),
    _user(S.REVISION_PLAN_REQUIRED, A.APPROVE_REVISION_PLAN, S.SUBSTANTIVE_REWRITING),
    # A review the author dismissed entirely. Triage can change a verdict: the
    # review asked for a plan because it found something blocking, and if every
    # finding is then rejected it has found nothing to act on — which is what
    # `accept_review` already means from `substantive_reviewing`. The same action,
    # taken by a person rather than by the stage, because deciding is theirs.
    #
    # Without it a dismissed review still had to be planned and rewritten around:
    # an empty plan passes `check_plan`, a rewrite that applies nothing passes
    # `check_rewrite`, and three model calls produce the draft that already
    # existed.
    _user(
        S.REVISION_PLAN_REQUIRED,
        A.ACCEPT_REVIEW,
        S.VOICE_ALIGNING,
        "every finding was decided and none of them needs a rewrite",
    ),
    _user(
        S.REVISION_PLAN_REQUIRED,
        A.RETURN_TO_BRIEF,
        S.BRIEF_GENERATING,
        "the plan showed the problem is the brief, not the prose",
    ),
    _policy(
        S.SUBSTANTIVE_REWRITING,
        A.SUBMIT_REWRITE,
        S.SUBSTANTIVE_REVIEWING,
        "a rewrite is re-reviewed; nothing skips straight to scoring",
    ),
    # Voice and scoring.
    _policy(S.VOICE_ALIGNING, A.SUBMIT_VOICE_PASS, S.SCORING),
    # Where a blocked voice pass goes. `revision_required` rather than a gate of
    # its own, because it is already the state that means "something is wrong and
    # somebody has to say where it goes" — and the problems a voice pass refuses
    # to fix carry a `suggested_route` in the same vocabulary the routing policy
    # speaks. A second pause with its own vocabulary would be a second way of
    # answering a question this one already answers.
    _policy(S.VOICE_ALIGNING, A.VOICE_BLOCKED, S.REVISION_REQUIRED),
    _policy(S.SCORING, A.SCORE_PASSED, S.PASSED),
    _policy(S.SCORING, A.SCORE_FAILED, S.REVISION_REQUIRED),
    # Routing. One action with several destinations: which correcting stage a
    # failure goes to is the versioned routing policy's call, not the table's,
    # so the table lists what is *permitted* and the policy picks
    # (see groundscribe.workflow.policy).
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.SOURCE_MODEL_EXTRACTING),
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.SOURCE_QUESTIONS_REQUIRED),
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.ARCHITECTURE_PROPOSING),
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.BRIEF_GENERATING),
    # Reviewing is a routing destination because a failing score is usually
    # judging a version nothing has reviewed. A voice pass produces a version and
    # takes the run straight to scoring, so by the time a score fails, the last
    # review describes the text as it stood before the voice pass reworded it —
    # and both of the substantive destinations read the *current* version's
    # review. They found none, and the run stopped with a plan it could not
    # write.
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.SUBSTANTIVE_REVIEWING),
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.REVISION_PLAN_REQUIRED),
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.SUBSTANTIVE_REWRITING),
    _policy(S.REVISION_REQUIRED, A.ROUTE_REVISION, S.VOICE_ALIGNING),
    # The narrow exit from `revision_required` that is not a revision.
    #
    # Its own action rather than a ninth `route_revision` destination, and the
    # distinction is the entire point: routing charges a round against the
    # rewrite ledger, and this is not a round. The score has already localised
    # the defect to a span, the article clears every floor, and nothing is
    # blocking — what is left is a deletion, not a revision, and giving it a
    # `route_revision` edge would put it on the same budget as the rewrites it
    # exists to avoid.
    #
    # The measured case: an article at 90.55 against a bar of 85, every floor
    # clear, eight deductions of which none blocking, failed by six words in its
    # opening paragraph that the source does not support. Three substantive
    # rounds were spent on that shape of failure and the score fell each time.
    _policy(
        S.REVISION_REQUIRED,
        A.CORRECT_CLAIMS,
        S.CLAIMS_CORRECTING,
        "the only failure is claims the source does not support, and they can be cut",
    ),
    # Straight back to scoring, with no voice pass in between. Prose that only
    # lost a clause has not been re-voiced, so there is nothing for the voice
    # stage to realign — and the guard on what the correction may touch is what
    # makes that true rather than hopeful. The re-score is the check: an article
    # that comes back below a floor it was above had a load-bearing claim cut,
    # and the round was owed after all.
    _policy(S.CLAIMS_CORRECTING, A.SUBMIT_CLAIM_CORRECTION, S.SCORING),
    _policy(
        S.REVISION_REQUIRED,
        A.STALL,
        S.STALLED,
        "the loop stopped improving, or a rewrite limit was reached",
    ),
    _user(
        S.REVISION_REQUIRED,
        A.OVERRIDE_AND_APPROVE,
        S.PASSED,
        "a person accepts the article despite its score",
    ),
    # Final validation and export.
    _policy(S.PASSED, A.VALIDATE_FINAL, S.FINAL_VALIDATING),
    _policy(S.FINAL_VALIDATING, A.VALIDATION_PASSED, S.HUMAN_APPROVAL_REQUIRED),
    _policy(
        S.FINAL_VALIDATING,
        A.VALIDATION_FAILED,
        S.REVISION_REQUIRED,
        "validation is deterministic; a failure re-enters routing, never export",
    ),
    _user(S.HUMAN_APPROVAL_REQUIRED, A.APPROVE_FINAL, S.COMPLETED),
    # Approving an architecture opens an article per approved concept, and the
    # run then carries exactly one of them here. Without this edge the rest were
    # rows nothing could act on: `completed` is terminal, and artefacts are
    # scoped to the run that produced them, so a second run would have found no
    # source model and no architecture to work from.
    #
    # Back to `architecture_approved` rather than to a brief, because that is
    # where the run already knows how to pick up an approved concept — the same
    # state approval itself lands in.
    _user(
        S.HUMAN_APPROVAL_REQUIRED,
        A.APPROVE_AND_CONTINUE,
        S.ARCHITECTURE_APPROVED,
        "the author accepted this article and chose to write another approved one",
    ),
    _user(S.HUMAN_APPROVAL_REQUIRED, A.REJECT_FINAL, S.REVISION_REQUIRED),
    # Stalled: the escalation options, every one of them a person's decision.
    _user(S.STALLED, A.AUTHORISE_REWRITE, S.SUBSTANTIVE_REWRITING),
    _user(S.STALLED, A.RETURN_TO_BRIEF, S.BRIEF_GENERATING),
    _user(S.STALLED, A.REOPEN_ARCHITECTURE, S.ARCHITECTURE_PROPOSING),
    _user(S.STALLED, A.OVERRIDE_AND_APPROVE, S.PASSED),
)


def _endings() -> tuple[Transition, ...]:
    """Cancel/fail edges for every non-terminal state."""
    return tuple(
        transition
        for state in WorkflowState
        if state not in TERMINAL_STATES
        for transition in (
            Transition(state, A.CANCEL, S.CANCELLED, ActorType.USER, "a person stopped the run"),
            Transition(state, A.FAIL, S.FAILED, ActorType.SYSTEM, "the run could not continue"),
        )
    )


#: Every legal edge in the machine.
TRANSITIONS: tuple[Transition, ...] = _EDITORIAL_TRANSITIONS + _endings()

_BY_SOURCE: dict[WorkflowState, tuple[Transition, ...]] = {
    state: tuple(t for t in TRANSITIONS if t.source is state) for state in WorkflowState
}


def transitions_from(state: WorkflowState) -> tuple[Transition, ...]:
    """Every edge leaving ``state``; empty for terminal states."""
    return _BY_SOURCE[state]


def available_actions(state: WorkflowState) -> tuple[WorkflowAction, ...]:
    """The actions offered in ``state``, deduplicated and ordered.

    Ordered because phase 09 returns this straight to an API client and an
    order that shifted between calls would look like the machine changing.
    """
    return tuple(sorted({transition.action for transition in transitions_from(state)}))


def targets_for(state: WorkflowState, action: WorkflowAction) -> tuple[WorkflowState, ...]:
    """The states ``action`` may lead to from ``state``; empty if it is illegal."""
    return tuple(t.target for t in transitions_from(state) if t.action is action)


def transition_for(
    state: WorkflowState, action: WorkflowAction, target: WorkflowState
) -> Transition | None:
    """The edge for one exact ``(state, action, target)``, or ``None``.

    The target is required rather than inferred: ``ROUTE_REVISION`` legally
    leads to seven different states, and a lookup that guessed one of them
    would make the routing policy's choice unenforceable.
    """
    for transition in transitions_from(state):
        if transition.action is action and transition.target is target:
            return transition
    return None


def is_taken_by_user(state: WorkflowState, action: WorkflowAction) -> bool:
    """Whether a *person* is the one who may take ``action`` from ``state``.

    Asked per edge rather than per action because the same name is a person's
    decision in one place and the engine's in another — ``REOPEN_ARCHITECTURE``
    is the machine's routing nowhere and an author's escalation from ``STALLED``.

    An interface needs this to stop describing a human edge as something the
    pipeline will get to on its own: ``answer_questions`` has no endpoint that a
    dashboard can offer, and "waiting for the pipeline" is the one thing it is
    not.
    """
    return any(
        transition.actor is ActorType.USER
        for transition in transitions_from(state)
        if transition.action is action
    )


def is_human_pause(state: WorkflowState) -> bool:
    """Whether the engine must park in ``state`` and wait for a person."""
    real = [t for t in transitions_from(state) if t.action not in UNIVERSAL_ACTIONS]
    return bool(real) and all(t.actor is ActorType.USER for t in real)


def human_pause_states() -> frozenset[WorkflowState]:
    """Every state the engine parks in (plan/05 → human-pause mechanism)."""
    return frozenset(state for state in WorkflowState if is_human_pause(state))


__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "UNIVERSAL_ACTIONS",
    "Transition",
    "available_actions",
    "human_pause_states",
    "is_human_pause",
    "is_taken_by_user",
    "targets_for",
    "transition_for",
    "transitions_from",
]
