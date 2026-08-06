"""The application service layer (phase 09).

Spec (plan/09):

- *Application service layer: the single entry point both API and CLI call;
  issues commands to the workflow engine, never re-implements transition rules.*
- *Async enqueue: long-running commands enqueue a job and return immediately
  (no model call inside the request).*
- Risk: *API embedding workflow rules — forbidden; all rules stay in the engine.*

The sharpest test here is the negative one: after a command that will call a
model, the fake client must have received **nothing**. That is the whole point of
the phase — the work moved off the request — and it is the kind of property that
quietly stops being true the first time someone "just awaits it here".

The service is exercised together with a real worker over the same rows, because
"enqueued" is only a useful outcome if something can then run it.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json, golden_text, relabel
from groundscribe.app.services import NothingToAbandon, NothingToRetry
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import SourceFormat
from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.jobs.models import Job
from groundscribe.llm import InjectableFailure
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.errors import AttributionRequired, IllegalTransition
from groundscribe.workflow.states import WorkflowState
from service_helpers import AUTHOR, Harness, build_harness
from stage_helpers import DEFAULT_CONSTRAINTS
from test_gap_questions import GAPS, SIX_GAPS

S = WorkflowState


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


def new_project(harness: Harness) -> str:
    created = harness.service.create_project(
        title="Read-through caching",
        author_id=AUTHOR,
        constraints=DEFAULT_CONSTRAINTS,
    )
    return created.project_id


async def with_source(harness: Harness) -> str:
    """A project with the golden source document ingested."""
    project_id = new_project(harness)
    await harness.service.import_source(
        project_id,
        title="Read-through caching for the render pipeline",
        text=golden_text("source.md"),
        source_format=SourceFormat.MARKDOWN,
    )
    return project_id


# ----------------------------------------------------------------------
# Projects and sources
# ----------------------------------------------------------------------


def test_creating_a_project_opens_a_run_and_a_position(harness: Harness) -> None:
    """A project is not just a row: it is a run with somewhere to resume from."""
    created = harness.service.create_project(
        title="Read-through caching", author_id=AUTHOR, constraints=DEFAULT_CONSTRAINTS
    )

    assert created.state is S.SOURCE_INGESTED
    assert "extract_source_model" in created.available_actions
    assert created.run_id is not None
    assert created.job is None


def test_creating_a_project_records_the_constraints_it_publishes_under(
    harness: Harness, db_session: Session
) -> None:
    """plan/06's constraints are set at creation, not discovered at drafting."""
    created = harness.service.create_project(
        title="Read-through caching", author_id=AUTHOR, constraints=DEFAULT_CONSTRAINTS
    )

    stored = db_session.scalars(select(domain_models.ProjectConstraints)).all()

    assert len(stored) == 1
    assert created.project_id is not None


async def test_importing_a_source_runs_in_the_request_because_no_model_is_called(
    harness: Harness,
) -> None:
    """Parsing and hashing are local work, and queueing them would only add latency.

    plan/09's rule is that *LLM* work leaves the request. Ingestion calls
    nothing, finishes in microseconds, and the next command needs its result, so
    a job would buy a round trip and a second failure mode for nothing.

    The job on the result is not ingestion's. Auto-advance (phase 16) queues the
    extraction that ``source_ingested`` was waiting for, which is the distinction
    this test now also pins: no model was called *in the request*, and the model
    work that follows is a job like every other.
    """
    project_id = new_project(harness)

    result = await harness.service.import_source(
        project_id,
        title="Read-through caching for the render pipeline",
        text=golden_text("source.md"),
        source_format=SourceFormat.MARKDOWN,
    )

    assert harness.client.received_requests == (), "ingestion calls no model"
    assert result.job is not None, "the run does not sit still waiting to be asked"
    assert result.job.job_type == JobType.EXTRACT_SOURCE_MODEL.value
    assert result.state is S.SOURCE_MODEL_EXTRACTING


# ----------------------------------------------------------------------
# Async enqueue
# ----------------------------------------------------------------------


async def test_extraction_enqueues_and_returns_without_calling_a_model(
    harness: Harness,
) -> None:
    """plan/09 → *no model call inside the request*, and the state says so.

    The run moves into ``source_model_extracting`` immediately. That state means
    "work is in flight", and leaving the run in ``source_ingested`` until a
    worker picked the job up would make it unreadable — a client could not
    distinguish "not started" from "started".
    """
    project_id = await with_source(harness)

    result = await harness.service.extract_source_model(project_id)

    assert result.job is not None
    assert result.job.status is JobStatus.PENDING
    assert result.job.job_type == JobType.EXTRACT_SOURCE_MODEL
    assert result.state is S.SOURCE_MODEL_EXTRACTING
    assert harness.client.received_requests == ()


async def test_a_repeated_command_joins_the_job_already_queued(harness: Harness) -> None:
    """Two clicks are one piece of work; the engine only moves once."""
    project_id = await with_source(harness)

    first = await harness.service.extract_source_model(project_id)
    second = await harness.service.extract_source_model(project_id)

    assert first.job is not None
    assert second.job is not None
    assert second.job.id == first.job.id
    assert harness.runtime.queue.pending_count() == 1


async def test_the_worker_is_what_calls_the_model(harness: Harness) -> None:
    """The other half of the same property: the work does happen, elsewhere."""
    project_id = await with_source(harness)
    script_extraction(harness)

    await harness.service.extract_source_model(project_id)
    (job,) = await harness.drain()

    assert job.status is JobStatus.SUCCEEDED
    assert harness.client.received_requests != ()
    assert job.stage_execution_id is not None
    assert job.result["snapshot_ids"]


async def test_the_run_advances_to_where_the_stage_left_it(harness: Harness) -> None:
    """The worker's transition is visible to the next request, not just to itself."""
    project_id = await with_source(harness)
    script_extraction(harness)
    await harness.service.extract_source_model(project_id)

    await harness.drain()
    state = harness.service.project_state(project_id)

    assert state.state is S.SOURCE_QUESTIONS_REQUIRED
    assert "answer_questions" in state.available_actions


# ----------------------------------------------------------------------
# The question queue is a queue
# ----------------------------------------------------------------------


async def parked_on_questions(harness: Harness) -> tuple[str, list[domain_models.SourceGap]]:
    """A run waiting on the author, with more than one question surfaced.

    More than one because that is the case the single-answer shape could not
    express: with one question, "answer" and "submit the round" are the same
    click and the difference between them is invisible.
    """
    project_id = await with_source(harness)
    script_extraction(harness, gaps=SIX_GAPS)
    await harness.service.extract_source_model(project_id)
    await harness.drain()
    gaps = list(
        harness.runtime.session.scalars(
            select(domain_models.SourceGap)
            .where(domain_models.SourceGap.surfaced.is_(True))
            .order_by(domain_models.SourceGap.ordinal)
        )
    )
    return project_id, gaps


async def test_recording_an_answer_leaves_the_run_waiting_for_the_others(
    harness: Harness,
) -> None:
    """plan/06 → *a queue*. An author answers what they can, then hands the round back.

    Recording an answer is not a transition. A run that moved on the first one
    would make every later answer illegal in the state the first one caused, so
    the author would be told their own second answer was out of order — which is
    not a queue, it is a form with one field.
    """
    project_id, gaps = await parked_on_questions(harness)

    first = harness.service.answer_gap(
        project_id, gap_id=gaps[0].id, text="640ms.", answered_by=AUTHOR
    )
    second = harness.service.answer_gap(
        project_id, gap_id=gaps[1].id, text="Seven locales.", answered_by=AUTHOR
    )

    assert first.state is S.SOURCE_QUESTIONS_REQUIRED
    assert second.state is S.SOURCE_QUESTIONS_REQUIRED
    assert first.job is None and second.job is None
    assert harness.runtime.queue.pending_count() == 0


async def test_submitting_the_round_is_what_rebuilds_the_source_model(harness: Harness) -> None:
    """The edge the author takes, once, for however many answers they gave.

    ``answer_questions`` re-enters extraction (plan/05's table), so this is the
    command that spends a model call — which is the other half of why recording
    an answer must not: five answers would have been five rebuilds, four of them
    already contradicted by the time they ran.
    """
    project_id, gaps = await parked_on_questions(harness)
    harness.service.answer_gap(project_id, gap_id=gaps[0].id, text="640ms.", answered_by=AUTHOR)
    harness.service.answer_gap(project_id, gap_id=gaps[1].id, text="Seven.", answered_by=AUTHOR)

    submitted = harness.service.submit_answers(project_id, submitted_by=AUTHOR)

    assert submitted.state is S.SOURCE_MODEL_EXTRACTING
    assert submitted.job is not None
    assert submitted.job.job_type == JobType.EXTRACT_SOURCE_MODEL


async def test_every_answer_reaches_the_rebuild_not_only_the_last(harness: Harness) -> None:
    """The point of collecting a round: the model is asked again knowing all of it."""
    project_id, gaps = await parked_on_questions(harness)
    harness.service.answer_gap(project_id, gap_id=gaps[0].id, text="640ms.", answered_by=AUTHOR)
    harness.service.answer_gap(project_id, gap_id=gaps[1].id, text="Seven.", answered_by=AUTHOR)
    script_extraction(harness)

    harness.service.submit_answers(project_id, submitted_by=AUTHOR)
    await harness.drain()

    sent = "\n".join(str(request) for request in harness.client.received_requests)
    assert "640ms." in sent
    assert "Seven." in sent


async def test_an_answer_is_refused_once_the_run_has_left_the_queue(harness: Harness) -> None:
    """Recording is not a transition, so it needs a gate of its own — the same one.

    Without it, dropping the transition from ``answer_gap`` would make answering
    legal everywhere, including in a finished run: an answer nothing will ever
    read, recorded as though it counted.
    """
    project_id, gaps = await parked_on_questions(harness)
    harness.service.cancel(project_id, cancelled_by=AUTHOR)

    with pytest.raises(IllegalTransition):
        harness.service.answer_gap(
            project_id, gap_id=gaps[0].id, text="Too late.", answered_by=AUTHOR
        )


async def test_an_unattributed_answer_is_refused(harness: Harness) -> None:
    """plan/03: an intervention nobody can be identified as is not reviewable."""
    project_id, gaps = await parked_on_questions(harness)

    with pytest.raises(ValueError):
        harness.service.answer_gap(project_id, gap_id=gaps[0].id, text="640ms.", answered_by="")


# ----------------------------------------------------------------------
# A run whose job failed
# ----------------------------------------------------------------------


async def failed_extraction(harness: Harness) -> str:
    """A run parked in an ``-ing`` state by a job that failed under it.

    The situation a real installation reaches and cannot leave: the entry
    transition was taken in the request, the worker took the job, the job failed,
    and the state it was carrying the run *out* of is the state the run is now
    stuck in. Nothing is queued, and every edge out of an ``-ing`` state belongs
    to the pipeline.
    """
    project_id = await with_source(harness)
    harness.client.script_failure("extract_source_truth", InjectableFailure.PROVIDER_ERROR)
    await harness.service.extract_source_model(project_id)
    (job,) = await harness.drain()
    assert job.status is JobStatus.FAILED
    return project_id


async def test_a_failed_job_can_be_run_again_without_moving_the_run(
    harness: Harness,
) -> None:
    """The recovery a stranded run needs, and the smallest one that is honest.

    No transition: the run is already in the state the job was meant to carry it
    out of, so retrying queues the same work again rather than negotiating with
    the transition table. A person asks for it — it spends a model call — and the
    failed attempt stays on the record beside the new one.
    """
    project_id = await failed_extraction(harness)
    script_extraction(harness)

    retried = harness.service.retry_failed_job(project_id, requested_by=AUTHOR)

    assert retried.state is S.SOURCE_MODEL_EXTRACTING
    assert retried.job is not None
    assert retried.job.status is JobStatus.PENDING
    assert retried.job.job_type == JobType.EXTRACT_SOURCE_MODEL


async def test_the_retried_job_actually_carries_the_run_on(harness: Harness) -> None:
    """Queued is only useful if running it finishes what the first attempt started."""
    project_id = await failed_extraction(harness)
    script_extraction(harness)

    harness.service.retry_failed_job(project_id, requested_by=AUTHOR)
    (job,) = await harness.drain()

    assert job.status is JobStatus.SUCCEEDED
    assert harness.service.project_state(project_id).state is S.SOURCE_QUESTIONS_REQUIRED


async def test_nothing_is_retried_while_the_queue_still_has_the_work(
    harness: Harness,
) -> None:
    """A second copy of a job that is still coming is not a recovery, it is a duplicate."""
    project_id = await with_source(harness)
    script_extraction(harness)
    await harness.service.extract_source_model(project_id)

    with pytest.raises(NothingToRetry):
        harness.service.retry_failed_job(project_id, requested_by=AUTHOR)


async def test_a_run_that_has_not_failed_has_nothing_to_retry(harness: Harness) -> None:
    """Said plainly rather than by queueing something nobody asked for."""
    project_id = await with_source(harness)

    with pytest.raises(NothingToRetry):
        harness.service.retry_failed_job(project_id, requested_by=AUTHOR)


async def test_a_retry_is_attributed(harness: Harness) -> None:
    """It spends a model call, so somebody has to be accountable for asking."""
    project_id = await failed_extraction(harness)

    with pytest.raises(AttributionRequired):
        harness.service.retry_failed_job(project_id, requested_by="")


# ----------------------------------------------------------------------
# The recovery a retry cannot perform
# ----------------------------------------------------------------------


async def reopened_architecture(harness: Harness) -> str:
    """A run with an architecture approved, and a second proposal in flight.

    Which is the shape the dead end has: three of the five ways into
    ``architecture_proposing`` are a person's, and once something is approved the
    proposal they start cannot land without lineage and an override.
    """
    from groundscribe.provenance.enums import ActorType
    from groundscribe.workflow.states import WorkflowAction

    # Driven by hand, so approval parks in `architecture_approved` instead of
    # being carried straight on into the brief.
    created = harness.service.create_project(
        title="Read-through caching",
        author_id=AUTHOR,
        constraints=DEFAULT_CONSTRAINTS.model_copy(update={"auto_advance": False}),
    )
    project_id = created.project_id
    await harness.service.import_source(
        project_id,
        title="Read-through caching for the render pipeline",
        text=golden_text("source.md"),
        source_format=SourceFormat.MARKDOWN,
    )
    script_extraction(harness, gaps={"schema_version": 1, "gaps": []})
    await harness.service.extract_source_model(project_id)
    await harness.drain()

    harness.client.script_response("propose_content_architecture", golden_json("architecture.json"))
    await harness.service.propose_architecture(project_id)
    await harness.drain()
    harness.service.approve_architecture(project_id, approved_by=AUTHOR)

    # Through the engine, because `reopen_architecture` is an edge the table
    # permits and no command performs — which is a gap of its own, and not one
    # this test should paper over by pretending the command exists.
    resumed = harness.service._resume(project_id)
    resumed.engine.apply(
        WorkflowAction.REOPEN_ARCHITECTURE, actor_id=AUTHOR, actor_type=ActorType.USER
    )
    harness.runtime.positions.capture(resumed.position, resumed.engine)
    assert harness.service.project_state(project_id).state is S.ARCHITECTURE_PROPOSING
    return project_id


async def test_a_proposal_that_cannot_land_can_be_given_up_on(harness: Harness) -> None:
    """The state had one forward edge and it needed the impossible thing.

    ``submit_architecture`` wants a proposal, and a proposal over an approved
    architecture is refused by the engine's guard unless it forks from the
    approved snapshot and carries an override. So a run here could be retried
    into the same refusal forever, or cancelled. Now it can put the proposal down
    and keep what was already approved.
    """
    project_id = await reopened_architecture(harness)

    result = harness.service.abandon_proposal(project_id, requested_by=AUTHOR)

    assert result.state is S.ARCHITECTURE_APPROVED


async def test_giving_up_does_not_also_rewrite_the_brief(harness: Harness) -> None:
    """Every other person's action advances afterwards; this one must not.

    ``architecture_approved`` is a state whose next step is the brief, and that is
    right for a run reaching it the first time. A run reaching it *here* has a
    brief already — somebody who said "give up on this proposal" did not ask for
    the next one to be written.
    """
    project_id = await reopened_architecture(harness)

    result = harness.service.abandon_proposal(project_id, requested_by=AUTHOR)

    assert result.job is None
    assert harness.runtime.queue.pending_count() == 0


async def test_a_run_with_nothing_approved_is_told_to_retry_instead(harness: Harness) -> None:
    """Wrong tool, and saying which is the right one beats moving the run.

    With no approved architecture there is nothing to fall back to, and the
    proposal is ordinary work that failed — which ``retry_failed_job`` fixes.
    """
    project_id = await with_source(harness)
    script_extraction(harness, gaps={"schema_version": 1, "gaps": []})
    await harness.drain()
    assert harness.service.project_state(project_id).state is S.ARCHITECTURE_PROPOSING

    with pytest.raises(NothingToAbandon):
        harness.service.abandon_proposal(project_id, requested_by=AUTHOR)


async def test_giving_up_on_a_proposal_is_attributed(harness: Harness) -> None:
    """Abandoning work is a decision, and the record should name who made it."""
    project_id = await reopened_architecture(harness)

    with pytest.raises(AttributionRequired):
        harness.service.abandon_proposal(project_id, requested_by="")


# ----------------------------------------------------------------------
# Rules stay in the engine
# ----------------------------------------------------------------------


async def test_a_command_the_workflow_forbids_is_refused_before_anything_is_queued(
    harness: Harness,
) -> None:
    """plan/09 risk: the API must not hold its own opinion of what is legal.

    The service asks the engine, so an out-of-order command fails the same way
    it would anywhere else — and, importantly, fails *before* a job exists, so
    no worker inherits an impossible instruction.
    """
    project_id = await with_source(harness)
    article_id = _an_article_addressed_before_its_time(harness, project_id)

    with pytest.raises(IllegalTransition):
        await harness.service.generate_brief(article_id)

    # Not "the queue is empty": auto-advance has already queued the extraction
    # this run is legitimately waiting for. What must not be there is the work
    # the refused command asked for.
    queued = set(
        harness.runtime.session.scalars(select(Job.job_type).where(Job.status == JobStatus.PENDING))
    )
    assert JobType.GENERATE_BRIEF not in queued


async def test_a_human_action_without_an_actor_is_refused(harness: Harness) -> None:
    """plan/05's attribution rule reaches the API unchanged.

    Cancelling is the action to test it with: it is available in every
    non-terminal state, so the refusal cannot be an accident of where the run
    happens to be.
    """
    project_id = await with_source(harness)

    with pytest.raises(AttributionRequired):
        harness.service.cancel(project_id, cancelled_by="")


# ----------------------------------------------------------------------
# Provenance commands
# ----------------------------------------------------------------------


async def test_replaying_an_execution_leaves_the_original_untouched(harness: Harness) -> None:
    """plan/05 → replays cannot overwrite original executions.

    Phase 12 gave the replay work to do, so the shape changed with it: the
    service queues a job, and the linked execution exists once the worker has
    opened it. The claim is the same one, checked further along — after the
    replay has actually run.
    """
    project_id = await with_source(harness)
    script_extraction(harness)
    await harness.service.extract_source_model(project_id)
    (job,) = await harness.drain()
    assert job.stage_execution_id is not None

    script_extraction(harness)
    queued = harness.service.replay_execution(job.stage_execution_id, requested_by=AUTHOR)
    (replayed_job,) = await harness.drain()
    replay = harness.service.get_execution(str(replayed_job.stage_execution_id))
    original = harness.service.get_execution(job.stage_execution_id)

    assert queued.source_execution_id == job.stage_execution_id
    assert replay.parent_execution_id == job.stage_execution_id
    assert replay.id != original.id
    assert original.status is ExecutionStatus.SUCCEEDED


async def test_an_execution_reports_its_events_and_its_model_calls(harness: Harness) -> None:
    """``GET /executions/{id}/events`` and ``.../invocations``, at the service seam."""
    project_id = await with_source(harness)
    script_extraction(harness)
    await harness.service.extract_source_model(project_id)
    (job,) = await harness.drain()
    assert job.stage_execution_id is not None

    events = harness.service.execution_events(job.stage_execution_id)
    invocations = harness.service.execution_invocations(job.stage_execution_id)

    assert events[0].event_type == "stage.started"
    assert [invocation.template_id for invocation in invocations] == ["extract_source_truth"]


def _an_article_addressed_before_its_time(harness: Harness, project_id: str) -> str:
    """An article row for a project whose architecture has not been approved.

    Written by hand because nothing legitimate produces one at this point —
    which is the situation under test: the command is addressable, and the
    workflow is what refuses it.
    """
    session = harness.runtime.session
    session.add(domain_models.Article(id="premature", project_id=project_id, title="Too early"))
    session.flush()
    return "premature"


def script_extraction(harness: Harness, *, gaps: dict[str, Any] = GAPS) -> None:
    """Queue what the extraction job will ask for: a source model and its gaps.

    Both, because one job runs both stages — the workflow has no state between
    them, and "extract the source model" that could not say what was missing
    would answer half a question.

    ``gaps`` is a parameter because how many questions surface is the variable
    some tests are about: one is the ordinary case, several is the case the
    queue exists for.

    The segment labels are mapped from the stored rows rather than from an
    ``IngestedSource``, because that is the position a worker is in: it has rows,
    not the object the ingesting request happened to build.
    """
    segments = harness.runtime.session.scalars(
        select(domain_models.SourceSegment).order_by(domain_models.SourceSegment.ordinal)
    ).all()
    harness.client.script_response(
        "extract_source_truth",
        relabel(
            golden_json("source_model.json"),
            {f"S{segment.ordinal}": segment.id for segment in segments},
        ),
    )
    harness.client.script_response("generate_gap_questions", gaps)


async def test_a_retry_runs_the_step_the_run_is_on_not_the_newest_failure(
    harness: Harness,
) -> None:
    """A run carries its old failures with it, and they are not what to re-run.

    Observed on a real run parked in ``substantive_rewriting`` whose newest failed
    job was a ``plan_revision`` from an hour before — already succeeded on a later
    attempt and long since approved. The interface offered "run that step again",
    which would have re-planned a revision that was already planned.

    Scoped to what the current state is waiting for, so the offer means the step
    on screen.
    """
    project_id = await failed_extraction(harness)
    resumed = harness.service._resume(project_id)
    assert resumed.engine.state is S.SOURCE_MODEL_EXTRACTING

    # A failure from a stage this run has no business re-running, newer than the
    # extraction that actually failed.
    stale = harness.runtime.queue.enqueue(
        job_type=JobType.SCORE_ARTICLE, run=resumed.run, payload={}, supersede=False
    )
    stale.status = JobStatus.FAILED
    harness.runtime.session.flush()

    script_extraction(harness)
    retried = harness.service.retry_failed_job(project_id, requested_by=AUTHOR)

    assert retried.job is not None
    assert retried.job.job_type == JobType.EXTRACT_SOURCE_MODEL, (
        "retried the newest failure rather than the step the run is on"
    )


async def test_a_state_waiting_on_a_person_has_nothing_to_run_again(
    harness: Harness,
) -> None:
    """The gates own themselves, and a retry there would be a category error.

    ``source_questions_required`` is waiting for answers, not for work. Offering
    to re-run something would suggest the pipeline could get past it alone.
    """
    project_id = await with_source(harness)
    script_extraction(harness)
    await harness.drain()
    assert harness.service.project_state(project_id).state is S.SOURCE_QUESTIONS_REQUIRED

    with pytest.raises(NothingToRetry, match="not waiting on work"):
        harness.service.retry_failed_job(project_id, requested_by=AUTHOR)
