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

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json, golden_text, relabel
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import SourceFormat
from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.errors import AttributionRequired, IllegalTransition
from groundscribe.workflow.states import WorkflowState
from service_helpers import AUTHOR, Harness, build_harness
from stage_helpers import DEFAULT_CONSTRAINTS
from test_gap_questions import GAPS

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
    """
    project_id = new_project(harness)

    result = await harness.service.import_source(
        project_id,
        title="Read-through caching for the render pipeline",
        text=golden_text("source.md"),
        source_format=SourceFormat.MARKDOWN,
    )

    assert result.job is None
    assert result.state is S.SOURCE_INGESTED
    assert harness.client.received_requests == ()


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

    assert harness.runtime.queue.pending_count() == 0


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
    """plan/05 → replays cannot overwrite original executions."""
    project_id = await with_source(harness)
    script_extraction(harness)
    await harness.service.extract_source_model(project_id)
    (job,) = await harness.drain()
    assert job.stage_execution_id is not None

    replay = harness.service.replay_execution(job.stage_execution_id, requested_by=AUTHOR)
    original = harness.service.get_execution(job.stage_execution_id)

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


def script_extraction(harness: Harness) -> None:
    """Queue what the extraction job will ask for: a source model and its gaps.

    Both, because one job runs both stages — the workflow has no state between
    them, and "extract the source model" that could not say what was missing
    would answer half a question.

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
    harness.client.script_response("generate_gap_questions", GAPS)
