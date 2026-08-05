"""The editorial workflow's state and action vocabularies (phase 05).

plan/00 → *Explicit state machine over autonomous agents*: the pipeline's
progress is a value from a closed set, not an emergent property of whatever a
model decided to do next. These two enums are that closed set.

As in phases 02 and 03 they are :class:`~enum.StrEnum`s, because every
transition is written into a :class:`~groundscribe.provenance.models.DecisionRecord`
— renaming a member silently rewrites the meaning of decisions already stored.

States and actions are kept apart on purpose. A state says where a run *is*; an
action says what may be done to it. Collapsing them (a single "event" vocabulary)
would make ``available_actions`` — which the API returns verbatim in phase 09 —
impossible to answer without inventing a second vocabulary anyway.
"""

from __future__ import annotations

from enum import StrEnum


class WorkflowState(StrEnum):
    """Where a pipeline run currently sits.

    The order mirrors the editorial pipeline: source → architecture → brief →
    draft → review → voice → score → validate → approve, with the three endings
    last. Names ending in ``_REQUIRED`` are the human-pause states — the engine
    parks there until a person acts (see
    :func:`~groundscribe.workflow.transitions.human_pause_states`).
    """

    SOURCE_INGESTED = "source_ingested"
    SOURCE_MODEL_EXTRACTING = "source_model_extracting"
    SOURCE_QUESTIONS_REQUIRED = "source_questions_required"
    SOURCE_MODEL_READY = "source_model_ready"
    ARCHITECTURE_PROPOSING = "architecture_proposing"
    ARCHITECTURE_REVIEW_REQUIRED = "architecture_review_required"
    ARCHITECTURE_APPROVED = "architecture_approved"
    BRIEF_GENERATING = "brief_generating"
    BRIEF_REVIEW_REQUIRED = "brief_review_required"
    DRAFT_GENERATING = "draft_generating"
    SUBSTANTIVE_REVIEWING = "substantive_reviewing"
    REVISION_PLAN_REQUIRED = "revision_plan_required"
    SUBSTANTIVE_REWRITING = "substantive_rewriting"
    VOICE_ALIGNING = "voice_aligning"
    SCORING = "scoring"
    REVISION_REQUIRED = "revision_required"
    PASSED = "passed"
    FINAL_VALIDATING = "final_validating"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALLED = "stalled"


class WorkflowAction(StrEnum):
    """What may be done to a run in a given state.

    Every action is either a *policy* action the engine may take on its own or a
    *user* action a person must take; which one is a property of the transition
    (:class:`~groundscribe.workflow.transitions.Transition`), not of the action
    name, because the same intent can be either depending on where it is taken —
    ``REOPEN_ARCHITECTURE`` from ``STALLED`` is a human's escalation choice.
    """

    # Source model.
    EXTRACT_SOURCE_MODEL = "extract_source_model"
    REQUEST_ANSWERS = "request_answers"
    ANSWER_QUESTIONS = "answer_questions"
    COMPLETE_EXTRACTION = "complete_extraction"

    # Architecture.
    PROPOSE_ARCHITECTURE = "propose_architecture"
    SUBMIT_ARCHITECTURE = "submit_architecture"
    APPROVE_ARCHITECTURE = "approve_architecture"
    REJECT_ARCHITECTURE = "reject_architecture"
    REOPEN_ARCHITECTURE = "reopen_architecture"

    # Brief.
    GENERATE_BRIEF = "generate_brief"
    SUBMIT_BRIEF = "submit_brief"
    APPROVE_BRIEF = "approve_brief"
    REJECT_BRIEF = "reject_brief"
    RETURN_TO_BRIEF = "return_to_brief"

    # Draft, review and rewrite.
    SUBMIT_DRAFT = "submit_draft"
    REQUIRE_REVISION_PLAN = "require_revision_plan"
    ACCEPT_REVIEW = "accept_review"
    APPROVE_REVISION_PLAN = "approve_revision_plan"
    SUBMIT_REWRITE = "submit_rewrite"
    AUTHORISE_REWRITE = "authorise_rewrite"

    # Voice and scoring.
    SUBMIT_VOICE_PASS = "submit_voice_pass"
    SCORE_PASSED = "score_passed"
    SCORE_FAILED = "score_failed"
    ROUTE_REVISION = "route_revision"
    STALL = "stall"
    OVERRIDE_AND_APPROVE = "override_and_approve"

    # Final validation and export.
    VALIDATE_FINAL = "validate_final"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    APPROVE_FINAL = "approve_final"
    #: Accept this article and go back for another of the approved ones.
    #:
    #: Its own action rather than a second destination for ``APPROVE_FINAL``,
    #: because the machine takes the sole target when none is named and only
    #: ``ROUTE_REVISION`` — which asks the routing policy — is allowed to be
    #: ambiguous. Two destinations here would put that choice in the call site,
    #: where a caller that forgot to name one would silently finish a run the
    #: author meant to continue.
    APPROVE_AND_CONTINUE = "approve_and_continue"
    REJECT_FINAL = "reject_final"

    # Endings available everywhere.
    CANCEL = "cancel"
    FAIL = "fail"


__all__ = ["WorkflowAction", "WorkflowState"]
