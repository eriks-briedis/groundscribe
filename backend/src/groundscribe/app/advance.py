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
    # No entry action: `revise` takes `correct_claims` itself, because choosing
    # this over routing is the decision that makes it cost no round.
    WorkflowState.CLAIMS_CORRECTING: Step(JobType.CORRECT_CLAIMS, per_article=True),
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


@dataclass(frozen=True)
class Have:
    """What the run has produced already.

    The map answers "what is this state waiting for?" from the state alone, which
    is the whole reason it is readable. Two steps cannot be decided that way, and
    this is what they need to know. Resolved by the caller, because it is
    database work and this module is the decision.
    """

    #: An architecture is locked, so a proposal over it needs a person.
    architecture_approved: bool = False
    #: The current review has been planned from, so the plan is written.
    revision_plan: bool = False
    #: Somebody has accepted or rejected at least one of the review's findings.
    #:
    #: False on a review nobody has been through, which is where every review
    #: starts — findings arrive ``proposed`` and only a person moves them.
    triaged_review: bool = True
    #: This job already ran, succeeded, and left the run exactly where it is.
    #:
    #: The general form of the two loops that were found one at a time. A step is
    #: worth starting because its completion moves the run; one that has already
    #: completed without moving it will not move it this time either.
    ran_without_moving: bool = False


def startable(step: Step, have: Have) -> bool:
    """Whether auto-advance should start what the map named.

    Two questions the state cannot answer: whether the job *could* succeed, and
    whether it is *still wanted*. Both are asked here because both have the same
    answer shape — leave the run alone — and both were learned the same way.

    **Proposing an architecture over an approved one cannot succeed.**
    ``route_revision`` can send a failure to ``architecture_proposing`` long
    after an architecture was approved, and a proposal that lands over one is
    refused twice by :meth:`WorkflowEngine._guard_architecture`: it must fork
    from the approved snapshot, and it must carry an override naming who
    authorised superseding it. Neither is something a job nobody asked for can
    supply. Observed on a real run, which failed with ``SilentMutationError``
    into a state whose only remaining exits were cancel and fail.

    **Planning a revision that is already planned is not wanted.**
    ``revision_plan_required`` is the one state in the map whose job does not
    leave it — the plan is the pipeline's to write and the approval is a
    person's — so a finished plan returns the run to the state that asked for
    one, and the completion queues another. Six ran in ninety seconds before
    anyone noticed, each one a model call replacing a plan waiting to be read.

    That loop was always in the map and had never run, because every route to
    ``revision_plan_required`` failed at the missing-review guard before the plan
    stage was reached. Fixing that guard is what let it turn.
    """
    # Asked first, and of every step, because it is the general form of the two
    # cases below it and of the ones not found yet. A step earns its place in the
    # map by moving the run when it finishes; one that has already finished
    # without moving it is not going to, and starting it again spends a model
    # call to arrive at the same state.
    #
    # Two loops were found this way, a stage apart, and neither was visible from
    # the map. `revision_plan_required` writes a plan and waits for a person, so
    # the completion returns the run to the state that asked for the plan.
    # `voice_aligning` withholds its own exit when the pass reports a structural
    # fault it refused to fix — deliberate, and it leaves the run in a state
    # whose only edge it just declined to take. The first ran eleven times, the
    # second five.
    if have.ran_without_moving:
        return False
    if step.job_type is JobType.PROPOSE_ARCHITECTURE:
        return not have.architecture_approved
    if step.job_type is JobType.PLAN_REVISION:
        # Not yet planned, *and* somebody has been through the findings. A plan
        # built from a review nobody has decided anything about has nothing to
        # apply, and an empty plan passes every check downstream — see
        # `check_triaged`. The run parks instead, which is what it should have
        # been doing all along: accepting a finding is a person's call and there
        # is no workflow state that says so.
        return not have.revision_plan and have.triaged_review
    return True


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
