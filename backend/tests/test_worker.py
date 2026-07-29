"""The worker loop and the progress it publishes (phase 09).

Spec (plan/09 → Test-first specification):

- *Job lifecycle*: the worker claims, runs and completes.
- *Worker crash retains trace*: killing a worker mid-stage preserves the partial
  ``StageExecution`` + invocations + trace events; the job is resumable and
  orphan-detectable.
- *SSE*: progress events stream for a running job.

The worker under test knows nothing about editorial stages. It is given handlers
and runs them, which is the seam that matters: the thing that must be reliable
under crashes is the claiming, recording and failure handling, and a test that
had to drive a model to exercise any of it would be testing something else.

"Killing" a worker is simulated with a ``BaseException`` the worker does not
catch, because that is the honest analogue: a process that is killed runs no
cleanup, marks nothing, and leaves recovery entirely to the rows it had already
written.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.jobs.queue import JobQueue
from groundscribe.jobs.worker import JobOutcome, JobRequest, UnknownJobType, Worker
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, ExecutionStatus, InvocationOutcome
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest
from groundscribe.storage.snapshot_store import SnapshotStore
from job_helpers import ManualClock, make_queue, seed_run
from provenance_helpers import make_recorder

LEASE = timedelta(seconds=30)

Handler = Callable[[JobRequest], Awaitable[JobOutcome]]


class Killed(BaseException):
    """Stands in for the process going away: not an error the worker may catch."""


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def queue(db_session: Session, clock: ManualClock) -> JobQueue:
    return make_queue(db_session, clock)


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    return make_recorder(db_session, snapshot_store)


@pytest.fixture
def run(
    db_session: Session, snapshot_store: SnapshotStore, recorder: ProvenanceRecorder
) -> models.PipelineRun:
    return seed_run(db_session, snapshot_store, recorder)


def build_worker(
    queue: JobQueue,
    recorder: ProvenanceRecorder,
    handlers: dict[JobType, Handler],
    *,
    worker_id: str = "worker-1",
) -> Worker:
    return Worker(queue=queue, recorder=recorder, handlers=handlers, worker_id=worker_id)


def partial_work(
    recorder: ProvenanceRecorder, request: JobRequest, run: models.PipelineRun
) -> models.StageExecution:
    """Open a stage and record enough that losing it would be a real loss."""
    execution = recorder.start_stage(run, stage="extract_source_truth", impl_version="1.1")
    request.opened(execution)
    recorder.record_model_invocation(
        execution,
        request=EffectiveRequest(
            template_id="extract_source_truth",
            template_version="1.0.0",
            rendered_prompt="extract the claims",
        ),
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
    )
    recorder.emit(
        event_type="stage.progress",
        actor_type=ActorType.SYSTEM,
        actor_id="pipeline",
        execution=execution,
        payload={"claims": 3},
    )
    return execution


# ----------------------------------------------------------------------
# Claim → run → complete
# ----------------------------------------------------------------------


async def test_the_worker_runs_a_claimed_job_and_records_what_it_produced(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun
) -> None:
    """The happy path, end to end, with nothing left pending."""
    seen: list[str] = []

    async def handler(request: JobRequest) -> JobOutcome:
        seen.append(request.job.id)
        return JobOutcome(result={"snapshot_ids": ["s1"]})

    queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    worker = build_worker(queue, recorder, {JobType.EXTRACT_SOURCE_MODEL: handler})

    job = await worker.run_once()

    assert job is not None
    assert seen == [job.id]
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"snapshot_ids": ["s1"]}
    assert queue.pending_count() == 0


async def test_an_idle_worker_reports_that_it_found_nothing(
    queue: JobQueue, recorder: ProvenanceRecorder
) -> None:
    """No work is not an error, and must not be a busy loop's exception."""
    assert await build_worker(queue, recorder, {}).run_once() is None


async def test_the_execution_is_attached_before_the_work_is_done(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun
) -> None:
    """plan/09 → orphan detection needs the link *while* the stage runs.

    A job that recorded its execution only on completion would leave the one
    case that matters — the stage that never completed — with nothing pointing
    at the records it left behind.
    """
    attached: list[str | None] = []

    async def handler(request: JobRequest) -> JobOutcome:
        partial_work(recorder, request, run)
        attached.append(request.job.stage_execution_id)
        return JobOutcome()

    queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    worker = build_worker(queue, recorder, {JobType.EXTRACT_SOURCE_MODEL: handler})

    job = await worker.run_once()

    assert job is not None
    assert attached == [job.stage_execution_id]
    assert job.stage_execution_id is not None


async def test_an_unknown_job_type_fails_the_job_rather_than_stalling_it(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun
) -> None:
    """A job nothing can run must say so, not sit pending forever."""
    queue.enqueue(job_type=JobType.SCORE_ARTICLE, run=run)
    worker = build_worker(queue, recorder, {})

    job = await worker.run_once()

    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error_type == UnknownJobType.__name__


# ----------------------------------------------------------------------
# Failure and crash
# ----------------------------------------------------------------------


async def test_a_failing_stage_fails_the_job_and_keeps_its_partial_trace(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun, db_session: Session
) -> None:
    """plan/09 → *partial execution data preserved on failure*.

    The records written before the failure are the explanation of it. A worker
    that rolled back on the way out would leave a failed job and no way to say
    why it failed.
    """
    executions: list[models.StageExecution] = []

    async def handler(request: JobRequest) -> JobOutcome:
        executions.append(partial_work(recorder, request, run))
        raise ValueError("the model returned a claim that is not in the source")

    queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    worker = build_worker(queue, recorder, {JobType.EXTRACT_SOURCE_MODEL: handler})

    job = await worker.run_once()

    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error_type == "ValueError"
    assert "not in the source" in (job.error_message or "")

    execution = executions[0]
    assert execution.status is ExecutionStatus.FAILED
    assert len(execution.model_invocations) == 1
    assert "stage.progress" in {event.event_type for event in execution.trace_events}


async def test_a_killed_worker_leaves_the_job_reclaimable_and_its_stage_orphaned(
    queue: JobQueue,
    recorder: ProvenanceRecorder,
    run: models.PipelineRun,
    clock: ManualClock,
) -> None:
    """plan/09 → *worker crashes; the job is resumable/orphan-detectable*.

    Nothing marks the job, because a killed process marks nothing. What has to
    survive is everything already written, and the two queries that find it: the
    lease reclaimer, and the orphan check that names the stage left running.
    """
    executions: list[models.StageExecution] = []

    async def handler(request: JobRequest) -> JobOutcome:
        executions.append(partial_work(recorder, request, run))
        raise Killed

    enqueued = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run, max_attempts=2)
    worker = build_worker(queue, recorder, {JobType.EXTRACT_SOURCE_MODEL: handler})

    with pytest.raises(Killed):
        await worker.run_once()

    execution = executions[0]
    assert enqueued.status is JobStatus.RUNNING
    assert execution.status is ExecutionStatus.RUNNING
    assert len(execution.model_invocations) == 1
    # Nothing is detectable yet, and that is correct: a job holding a valid
    # lease is indistinguishable from one whose worker is merely slow. Calling
    # it orphaned here would mean re-running stages that are still going.
    assert queue.orphaned_executions() == ()

    clock.advance(timedelta(minutes=10))
    (reclaimed,) = queue.reclaim_expired(lease=LEASE)

    assert reclaimed is enqueued
    assert reclaimed.status is JobStatus.PENDING
    assert queue.orphaned_executions() == (execution,)
    assert queue.claim(worker_id="worker-2") is reclaimed


# ----------------------------------------------------------------------
# Progress
# ----------------------------------------------------------------------


async def test_the_worker_publishes_the_jobs_lifecycle_onto_the_trace(
    queue: JobQueue,
    recorder: ProvenanceRecorder,
    run: models.PipelineRun,
    db_session: Session,
) -> None:
    """plan/09 → *emits SSE progress event*.

    Progress is not a side channel. It is written to the same trace as
    everything else, so what a client watched live and what an auditor reads
    afterwards are one record rather than two that can disagree.
    """

    async def handler(request: JobRequest) -> JobOutcome:
        partial_work(recorder, request, run)
        return JobOutcome(result={"snapshot_ids": ["s1"]})

    queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    worker = build_worker(queue, recorder, {JobType.EXTRACT_SOURCE_MODEL: handler})

    job = await worker.run_once()

    assert job is not None
    events = [event.event_type for event in run.stage_executions[-1].trace_events]
    assert "job.started" in events
    assert "job.completed" in events
    claimed = {
        event.event_type
        for event in run_events(run, db_session)
        if event.payload.get("job_id") == job.id
    }
    assert "job.claimed" in claimed


def run_events(run: models.PipelineRun, db_session: Session) -> list[models.TraceEvent]:
    """Every event of the run, in the order it was recorded."""
    return list(
        db_session.scalars(
            select(models.TraceEvent)
            .where(models.TraceEvent.correlation_id == run.correlation_id)
            .order_by(models.TraceEvent.sequence)
        )
    )


# ----------------------------------------------------------------------
# Draining
# ----------------------------------------------------------------------


async def test_the_worker_drains_the_queue_and_then_stops(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun
) -> None:
    """``run_until_idle`` is what a test, a CLI one-shot and a shutdown all need."""
    ran: list[str] = []

    async def handler(request: JobRequest) -> JobOutcome:
        ran.append(request.job.job_type)
        return JobOutcome()

    handlers: dict[JobType, Handler] = dict.fromkeys(
        (JobType.EXTRACT_SOURCE_MODEL, JobType.PROPOSE_ARCHITECTURE), handler
    )
    queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    queue.enqueue(job_type=JobType.PROPOSE_ARCHITECTURE, run=run)

    done = await build_worker(queue, recorder, handlers).run_until_idle()

    assert len(done) == 2
    assert ran == ["extract_source_model", "propose_architecture"]


async def test_recovery_runs_before_the_worker_takes_new_work(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun, clock: ManualClock
) -> None:
    """plan/09 → *worker restart/resumption*.

    A worker starting up is the only thing in the system positioned to notice
    that a previous one died, so it looks before it claims.
    """
    lost = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run, max_attempts=2)
    queue.claim(worker_id="worker-0")
    clock.advance(timedelta(minutes=10))

    async def handler(request: JobRequest) -> JobOutcome:
        return JobOutcome()

    worker = build_worker(queue, recorder, {JobType.EXTRACT_SOURCE_MODEL: handler})
    recovered = worker.recover(lease=LEASE)

    assert recovered.reclaimed == (lost,)
    assert recovered.orphaned == ()
    assert await worker.run_once() is lost


def test_a_job_request_exposes_only_what_a_handler_needs(
    queue: JobQueue, run: models.PipelineRun, db_session: Session
) -> None:
    """The handler seam, pinned: a job, a session, and a way to say what it opened.

    Deliberately not the engine or a stage: the worker is the wrong place to
    know what an editorial stage is, and a request that carried one would make
    every future stage the worker's business.
    """
    job = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    opened: list[models.StageExecution] = []

    request = JobRequest(job=job, session=db_session, opened=opened.append)

    assert request.job is job
    assert request.session is db_session
    assert request.payload == job.payload
