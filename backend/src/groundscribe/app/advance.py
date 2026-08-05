"""Starting the work nobody has to be asked about (phase 16).

A run parks in two very different kinds of state, and until now they were
pressed the same way. In ``brief_review_required`` it is waiting for a person to
read something and decide; in ``source_model_ready`` it is waiting for nothing at
all — the next step is the pipeline's own, the run knows what it is, and somebody
still had to click. This module presses the second kind.

**The map is the whole design.** :data:`NEXT` names, for each state, the job that
belongs there. A state absent from it is a state auto-advance will not move,
which is how every gate a person owns is enforced: not by a rule listing them,
but by their having no entry. A list of gates would have to be kept in step with
the state machine and would fail open when it drifted; an omission here fails
closed, which is the direction a mistake in this file should fail.

**A state's outgoing edge is not the test.** ``revision_plan_required`` is
reached before the plan exists and left by a person approving it, so its edge is
a person's and its work is the pipeline's. Asking "who owns the way out?" would
have stalled it forever. What the map asks instead is "is there work this state
is waiting to have done?", which is the question ``STATE_COMMANDS`` in
:mod:`groundscribe.app.actions` already answers for the interface — this is the
same fact, acted on rather than rendered.

**One article, the one the architecture chose.** The workflow state is
per-project and single-threaded, so after approval opens five articles something
has to say which one the run drives. It is the one the proposal's own decision
record selected — a choice already made, argued for and stored, rather than a
second one invented here. The other four stay addressable by hand.

Auto-advance never crosses a gate a person owns, never starts work already
queued, and never runs at all for a project whose constraints turn it off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select

from groundscribe.app.runtime import Runtime
from groundscribe.domain import models as domain_models
from groundscribe.jobs.enums import JobType
from groundscribe.jobs.models import Job
from groundscribe.workflow.states import WorkflowAction, WorkflowState


@dataclass(frozen=True)
class Step:
    """The job a state is waiting to have run, and how to enter it.

    ``entry`` is the edge that has to be taken before the job is queued, and is
    ``None`` for the states a person's own action already moved the run into —
    approving a brief lands the run in ``draft_generating``, and taking an edge
    out of it would leave the state the job is about to report from.
    """

    job_type: JobType
    entry: WorkflowAction | None = None
    #: Whether the job is addressed to one article rather than to the project.
    per_article: bool = False


#: Where the pipeline may start work without being asked.
#:
#: Sorted by where they fall in a run, because that is the order somebody
#: checking this against the state machine will read it in.
NEXT: dict[WorkflowState, Step] = {
    WorkflowState.SOURCE_INGESTED: Step(
        JobType.EXTRACT_SOURCE_MODEL, entry=WorkflowAction.EXTRACT_SOURCE_MODEL
    ),
    # The three "-ing" states routing can land a run in.
    #
    # They were absent, because a state ending in `-ing` is normally entered by
    # the same command that queues its job — the entry edge and the enqueue
    # happen together, so there is nothing left to start. Routing breaks that:
    # `route_revision` moves the run *into* one of these without queueing
    # anything, and a run then sits in a state that means "work is in flight"
    # with no work and no way to ask for any. Observed on a real run, which
    # parked in `source_model_extracting` with an empty queue and an idle worker.
    #
    # `_enqueue` dedupes on the active key, so a state entered the ordinary way —
    # with its job already queued — is handed that job rather than a second one.
    WorkflowState.SOURCE_MODEL_EXTRACTING: Step(JobType.EXTRACT_SOURCE_MODEL),
    WorkflowState.SOURCE_MODEL_READY: Step(
        JobType.PROPOSE_ARCHITECTURE, entry=WorkflowAction.PROPOSE_ARCHITECTURE
    ),
    WorkflowState.ARCHITECTURE_PROPOSING: Step(JobType.PROPOSE_ARCHITECTURE),
    WorkflowState.ARCHITECTURE_APPROVED: Step(
        JobType.GENERATE_BRIEF, entry=WorkflowAction.GENERATE_BRIEF, per_article=True
    ),
    WorkflowState.BRIEF_GENERATING: Step(JobType.GENERATE_BRIEF, per_article=True),
    WorkflowState.DRAFT_GENERATING: Step(JobType.GENERATE_DRAFT, per_article=True),
    WorkflowState.SUBSTANTIVE_REVIEWING: Step(JobType.REVIEW_ARTICLE, per_article=True),
    WorkflowState.REVISION_PLAN_REQUIRED: Step(JobType.PLAN_REVISION, per_article=True),
    WorkflowState.SUBSTANTIVE_REWRITING: Step(JobType.REWRITE_ARTICLE, per_article=True),
    WorkflowState.VOICE_ALIGNING: Step(JobType.ALIGN_VOICE, per_article=True),
    WorkflowState.SCORING: Step(JobType.SCORE_ARTICLE, per_article=True),
}

#: The states a person owns, listed for the reader rather than for the code.
#:
#: Nothing consults this: a gate is enforced by its absence from :data:`NEXT`.
#: It is here because the absence is invisible, and somebody adding a state will
#: want to see which side of the line the existing ones fell on.
HUMAN_GATES = (
    WorkflowState.SOURCE_QUESTIONS_REQUIRED,
    WorkflowState.ARCHITECTURE_REVIEW_REQUIRED,
    WorkflowState.BRIEF_REVIEW_REQUIRED,
    WorkflowState.REVISION_REQUIRED,
    WorkflowState.PASSED,
    WorkflowState.HUMAN_APPROVAL_REQUIRED,
    WorkflowState.STALLED,
)


def selected_article_id(runtime: Runtime, project_id: str) -> str | None:
    """The article the approved architecture chose, if there is one.

    Resolved through the concept's ``ref`` — the model's own label for the
    article, "a1" — because that is what the proposal's decision names. The
    article row shares the concept's id, so finding the concept finds the
    article.

    Falls back to the first concept by ordinal when the decision names nothing
    that exists. A proposal that selected an article it did not propose is
    refused at extraction time, so this is for the architecture a person edited
    by hand afterwards; parking a run because a label went stale would be a
    worse answer than starting on the article it lists first.
    """
    architecture = runtime.session.scalars(
        select(domain_models.ContentArchitecture)
        .where(
            domain_models.ContentArchitecture.project_id == project_id,
            domain_models.ContentArchitecture.locked.is_(True),
        )
        .order_by(domain_models.ContentArchitecture.id.desc())
    ).first()
    if architecture is None:
        return None

    concepts = runtime.session.scalars(
        select(domain_models.ArticleConcept)
        .where(domain_models.ArticleConcept.architecture_id == architecture.id)
        .order_by(domain_models.ArticleConcept.ordinal)
    ).all()
    if not concepts:
        return None

    chosen = runtime.session.get(domain_models.ArtifactSnapshot, architecture.snapshot_id or "")
    selected_ref = _selected_ref(runtime, chosen)
    for concept in concepts:
        if concept.ref and concept.ref == selected_ref:
            return concept.id
    return concepts[0].id


def _selected_ref(runtime: Runtime, snapshot: domain_models.ArtifactSnapshot | None) -> str:
    """The ``decision.selected`` label in a stored architecture proposal.

    Read defensively and returned as a string: this is one field of a document
    whose only job here is to pick between rows that all exist, and a proposal
    that cannot be read should leave the run on the first article rather than
    stop it.
    """
    if snapshot is None:
        return ""
    try:
        document = json.loads(runtime.snapshots.read(snapshot))
    except (OSError, ValueError):
        return ""
    decision = document.get("decision") if isinstance(document, dict) else None
    if not isinstance(decision, dict):
        return ""
    selected = decision.get("selected")
    return selected if isinstance(selected, str) else ""


def auto_advance_enabled(runtime: Runtime, project_id: str) -> bool:
    """Whether this project's constraints let the run start its own work.

    Read from the constraints in force rather than from a process-wide setting,
    because it is a project's answer and two projects run in one worker.
    Defaults to on where a project has no constraints at all, matching the
    column and the schema.
    """
    constraints = runtime.session.scalars(
        select(domain_models.ProjectConstraints)
        .where(domain_models.ProjectConstraints.project_id == project_id)
        .order_by(domain_models.ProjectConstraints.id.desc())
    ).first()
    return True if constraints is None else constraints.auto_advance


def startable(step: Step, *, architecture_approved: bool) -> bool:
    """Whether auto-advance can finish what it is about to start.

    The map answers "what is this state waiting for?" from the state alone,
    which is the whole reason it is readable. This is the one question it cannot
    answer that way: whether the job it names could succeed given what the run
    has already approved.

    Proposing an architecture is the case. ``route_revision`` can send a failure
    to ``architecture_proposing`` at any point, including long after an
    architecture was approved — and a proposal that lands over an approved one
    is refused twice over by :meth:`WorkflowEngine._guard_architecture`: it must
    fork from the approved snapshot, and it must carry an override naming who
    authorised superseding it. Both are deliberate, and neither is something a
    job started by nobody can supply. So the job is not merely likely to fail,
    it cannot succeed, and starting it costs a model call to arrive there.

    Observed on a real run, which routed a factual failure back to the source,
    followed the map through re-extraction into proposing a second architecture,
    and failed with ``SilentMutationError`` — leaving the run in
    ``architecture_proposing``, a state whose only remaining exits are cancel
    and fail.
    """
    if step.job_type is not JobType.PROPOSE_ARCHITECTURE:
        return True
    return not architecture_approved


def next_step(state: WorkflowState) -> Step | None:
    """The work ``state`` is waiting to have done, or nothing.

    ``None`` for every state a person owns and for every ending, because they
    are not in the map — see this module's docstring on why that is the whole
    enforcement.
    """
    return NEXT.get(state)


def pending_for(runtime: Runtime, key: str) -> Job | None:
    """The job already queued under ``key``, if one is."""
    return runtime.queue.active(key)
