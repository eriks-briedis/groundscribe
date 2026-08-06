"""The run starts the work nobody has to be asked about (phase 16).

Every stage the pipeline owns used to need a person to press start. The gates a
person owns were doing their job; the ones nobody owns were being pressed for no
reason — a run that had just finished extracting sat in ``source_model_ready``
until somebody clicked "propose architecture", which is a question with one
answer.

Four properties, and the second is the one that matters:

**It moves through pipeline-owned states.** Ingest, and the run extracts.

**It stops dead at every gate a person owns.** Asserted state by state, because
the enforcement is an *absence* from :data:`~groundscribe.app.advance.NEXT` and an
absence is exactly what a test has to pin — nothing in the code says "stop here",
so nothing in the code would break if a gate were added to the map by mistake.

**It drives the article the architecture chose**, out of the several that
approval opens, and leaves the rest alone.

**It does nothing at all when the project has it switched off.**
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json, golden_text
from groundscribe.app.advance import HUMAN_GATES, NEXT, next_step
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import SourceFormat
from groundscribe.jobs.enums import JobType
from groundscribe.provenance.enums import ActorType
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import UNIVERSAL_ACTIONS, available_actions
from service_helpers import AUTHOR, Harness, build_harness
from stage_helpers import DEFAULT_CONSTRAINTS
from test_services import new_project, script_extraction, with_source

S = WorkflowState

#: Extraction that found nothing to ask about, so the run does not park.
NO_GAPS: dict[str, Any] = {"schema_version": 1, "gaps": []}


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


def manual_project(harness: Harness) -> str:
    """A project that has asked to be driven by hand."""
    created = harness.service.create_project(
        title="Read-through caching",
        author_id=AUTHOR,
        constraints=DEFAULT_CONSTRAINTS.model_copy(update={"auto_advance": False}),
    )
    return created.project_id


async def ingest(harness: Harness, project_id: str) -> Any:
    return await harness.service.import_source(
        project_id,
        title="Read-through caching for the render pipeline",
        text=golden_text("source.md"),
        source_format=SourceFormat.MARKDOWN,
    )


# ----------------------------------------------------------------------
# The map is the enforcement
# ----------------------------------------------------------------------


def test_no_state_a_person_owns_is_in_the_map() -> None:
    """The gates are enforced by omission, so the omission is what to assert.

    Nothing in ``advance`` reads :data:`HUMAN_GATES`; a list the code consulted
    would have to be kept in step with the state machine and would fail *open*
    when it drifted. This is the test that would have caught the drift instead.
    """
    for gate in HUMAN_GATES:
        assert next_step(gate) is None, f"{gate.value} is a person's to take"


def test_every_ending_is_a_place_the_run_stops() -> None:
    """A finished run does not start anything, however it finished."""
    for ending in (S.COMPLETED, S.FAILED, S.CANCELLED):
        assert next_step(ending) is None


def test_the_map_only_names_states_that_exist() -> None:
    """A typo in a state name would be a stage that silently never auto-starts."""
    for state in NEXT:
        assert isinstance(state, WorkflowState)


# ----------------------------------------------------------------------
# Moving
# ----------------------------------------------------------------------


async def test_ingesting_a_source_starts_the_extraction(harness: Harness) -> None:
    """``source_ingested`` is waiting for nothing but a click, so there is none."""
    project_id = new_project(harness)

    result = await ingest(harness, project_id)

    assert result.job is not None
    assert result.job.job_type == JobType.EXTRACT_SOURCE_MODEL
    assert result.state is S.SOURCE_MODEL_EXTRACTING


async def test_a_finished_job_starts_the_one_the_next_state_wants(harness: Harness) -> None:
    """The worker is what walks the run forward: a completion queues the follower.

    One step per completion, not a drain. Advancing the whole pipeline inside one
    call would hold a transaction open across every model call in a run.
    """
    project_id = await with_source(harness)
    script_extraction(harness, gaps=NO_GAPS)

    await harness.drain()
    state = harness.service.project_state(project_id)

    # Extraction found nothing to ask about, so the run passed straight through
    # `source_model_ready` into proposing an architecture without being asked.
    assert state.state is S.ARCHITECTURE_PROPOSING


# ----------------------------------------------------------------------
# Stopping
# ----------------------------------------------------------------------


async def test_an_unanswered_question_stops_the_run_dead(harness: Harness) -> None:
    """The gate that matters most: a question the author has not seen.

    Auto-advance moving past this would mean building an article on a source
    model the author was never given the chance to correct.
    """
    project_id = await with_source(harness)
    script_extraction(harness)

    await harness.drain()
    state = harness.service.project_state(project_id)

    assert state.state is S.SOURCE_QUESTIONS_REQUIRED
    assert harness.runtime.queue.pending_count() == 0, "nothing was queued past the author"


async def test_a_proposed_architecture_waits_to_be_approved(harness: Harness) -> None:
    """Approval is a person's, so the run parks with the proposal unread.

    Both responses are scripted before the first drain, and that is the point:
    one drain now carries the run through extraction *and* the architecture it
    queued for itself. A test that scripted them a drain apart would be written
    for the stop-and-go rhythm this phase removed.
    """
    project_id = await with_source(harness)
    script_extraction(harness, gaps=NO_GAPS)
    harness.client.script_response("propose_content_architecture", golden_json("architecture.json"))

    await harness.drain()
    state = harness.service.project_state(project_id)

    assert state.state is S.ARCHITECTURE_REVIEW_REQUIRED
    assert harness.runtime.queue.pending_count() == 0


# ----------------------------------------------------------------------
# Which article
# ----------------------------------------------------------------------


async def test_approval_drives_the_article_the_decision_selected(harness: Harness) -> None:
    """Approval opens an article per concept, and the run has one state for all of them.

    So something has to choose, and it is the proposal's own decision record —
    a choice already made, argued for and stored — rather than a second one
    invented at the point of enqueueing. The golden architecture proposes ``a1``
    and ``a2`` and selects ``a1``.

    The other concepts keep their articles and stay addressable by hand; what
    they do not get is a run driving them without anyone asking.
    """
    project_id = await with_source(harness)
    script_extraction(harness, gaps=NO_GAPS)
    harness.client.script_response("propose_content_architecture", golden_json("architecture.json"))
    await harness.drain()

    result = harness.service.approve_architecture(project_id, approved_by=AUTHOR)

    concepts = {
        concept.ref: concept.id
        for concept in harness.runtime.session.scalars(select(domain_models.ArticleConcept))
    }
    assert len(concepts) == 2, "both concepts became articles"
    assert result.job is not None, "approving is not also a request to press draft"
    assert result.job.job_type == JobType.GENERATE_BRIEF
    assert result.job.payload["article_id"] == concepts["a1"]


# ----------------------------------------------------------------------
# The setting
# ----------------------------------------------------------------------


async def test_a_project_that_asked_to_be_driven_by_hand_is(harness: Harness) -> None:
    """Off means off: the run parks exactly where it used to."""
    project_id = manual_project(harness)

    result = await ingest(harness, project_id)

    assert result.job is None
    assert result.state is S.SOURCE_INGESTED
    assert harness.runtime.queue.pending_count() == 0


def test_the_setting_is_versioned_with_the_rest_of_the_constraints(harness: Harness) -> None:
    """It branches rather than being edited, so a run can say what was in force.

    "Did anyone ask for this draft?" is asked of an artefact afterwards, and an
    overwritten flag could not answer it.
    """
    project_id = manual_project(harness)

    row = harness.runtime.session.scalars(
        select(domain_models.ProjectConstraints).where(
            domain_models.ProjectConstraints.project_id == project_id
        )
    ).one()

    assert row.auto_advance is False


# ----------------------------------------------------------------------
# Writing another of the approved articles
# ----------------------------------------------------------------------


def test_approving_can_go_back_for_another_approved_article() -> None:
    """Approval opens an article per concept; the run carried one to publication.

    The rest were rows nothing could act on — the finished state is terminal, and
    artefacts are scoped to the run that produced them, so a second run would
    have found no source model and no architecture to work from.

    Asserted on the table rather than through a run, because what had to change
    is the table: `human_approval_required` gained a second way out.
    """
    from groundscribe.workflow.transitions import targets_for

    assert targets_for(S.HUMAN_APPROVAL_REQUIRED, WorkflowAction.APPROVE_AND_CONTINUE) == (
        S.ARCHITECTURE_APPROVED,
    )
    # And the finished state stays finished: this is an edge *into* the loop
    # again, taken before a run ends, never out of one that has.
    assert next_step(S.COMPLETED) is None
    assert WorkflowAction.APPROVE_AND_CONTINUE.value not in available_actions(S.COMPLETED)


def test_it_is_a_persons_edge_and_leads_exactly_one_place() -> None:
    """The machine takes the sole target when none is named, and only routing is
    allowed to be ambiguous — which is why this is its own action rather than a
    second destination for ``approve_final``."""
    from groundscribe.workflow.transitions import transition_for

    edge = transition_for(
        S.HUMAN_APPROVAL_REQUIRED,
        WorkflowAction.APPROVE_AND_CONTINUE,
        S.ARCHITECTURE_APPROVED,
    )

    assert edge is not None
    assert edge.actor is ActorType.USER, "only the author knows if another is worth writing"


def test_every_state_routing_can_reach_knows_what_to_run_there() -> None:
    """A run routed into a state with no job sits in it forever.

    ``route_revision`` moves a run into one of seven correcting states without
    queueing anything — unlike the ordinary path, where the entry edge and the
    enqueue happen in the same command. Three of the seven end in ``-ing``, which
    means "work is in flight", and were absent from the map for exactly the
    reason that made them dangerous: normally there is already a job.

    Observed on a real run, which routed to ``source_model_extracting`` and
    parked there with an empty queue and an idle worker.
    """
    from groundscribe.workflow.transitions import targets_for

    reachable = targets_for(S.REVISION_REQUIRED, WorkflowAction.ROUTE_REVISION)
    assert reachable, "routing has destinations, or this asserts nothing"

    for destination in reachable:
        if destination in HUMAN_GATES:
            continue  # a person is what happens next there, not a job
        assert next_step(destination) is not None, (
            f"routing can reach {destination.value} and nothing knows what to run there"
        )


def test_it_will_not_start_a_job_that_cannot_succeed() -> None:
    """Proposing an architecture over an approved one is refused by design.

    ``_guard_architecture`` wants two things from a proposal that supersedes an
    approved architecture: lineage showing what changed, and an override naming
    who decided it should. Auto-advance has neither — nobody asked for the
    proposal, so there is nobody to name — which makes this the one step in the
    map that is not merely likely to fail but unable to pass.

    Observed on a real run. A factual failure routed back to the source, the map
    carried it through re-extraction into ``architecture_proposing``, and the job
    failed with ``SilentMutationError``. The run was then stuck: the only ways out
    of that state are ``submit_architecture`` — which needs the proposal that just
    proved impossible — and cancelling.
    """
    from groundscribe.app.advance import Have, startable

    propose = NEXT[S.ARCHITECTURE_PROPOSING]
    assert startable(propose, Have()), "the first proposal is ordinary work"
    assert not startable(propose, Have(architecture_approved=True))


def test_approving_an_architecture_still_starts_the_brief() -> None:
    """The guard is about proposals, and must not stop the stages after one.

    ``architecture_approved`` is by definition a state with an approved
    architecture, so a check written as "an approved architecture stops
    auto-advance" would park every run at the moment it was approved.
    """
    from groundscribe.app.advance import Have, startable

    for state, step in NEXT.items():
        if step.job_type is JobType.PROPOSE_ARCHITECTURE:
            continue
        assert startable(step, Have(architecture_approved=True)), (
            f"{state.value} stopped moving once an architecture was approved"
        )


def test_a_plan_that_exists_is_not_planned_again() -> None:
    """``revision_plan_required`` is the one state whose job does not leave it.

    Writing the plan is the pipeline's; approving it is a person's. So a finished
    plan returns the run to the state that asked for one, and the completion
    queues another — a loop that spends a model call per turn and replaces a plan
    nobody has read yet.

    It ran eleven times on a real run before anyone noticed. It had never turned
    before, because every route into ``revision_plan_required`` used to fail at
    the missing-review guard, so the plan stage was never reached to complete.
    """
    from groundscribe.app.advance import Have, startable

    plan = NEXT[S.REVISION_PLAN_REQUIRED]
    assert startable(plan, Have()), "the first plan is the work the state is waiting for"
    assert not startable(plan, Have(revision_plan=True))


def test_no_step_in_the_map_leaves_the_state_it_starts_from() -> None:
    """The property the loop broke, asserted over the map rather than one entry.

    Every other step queues work whose completion moves the run somewhere else,
    which is why auto-advance can be "one step per completion" and still
    terminate. ``revision_plan_required`` is the exception and is handled by
    :func:`startable`; a second exception added without one would loop the same
    way, so this fails when one appears.
    """
    from groundscribe.app.advance import Have, startable
    from groundscribe.workflow.transitions import transitions_from

    for state, step in NEXT.items():
        moves = [
            transition
            for transition in transitions_from(state)
            if transition.action not in UNIVERSAL_ACTIONS and transition.actor is not ActorType.USER
        ]
        if moves:
            continue  # the job's own exit carries the run out of this state
        assert not startable(step, Have(architecture_approved=True, revision_plan=True)), (
            f"{state.value} has no pipeline edge out, so its job returns the run here "
            "and the completion starts it again"
        )
