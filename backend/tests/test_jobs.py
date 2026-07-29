"""Job-queue tests (phase 09).

Spec (plan/09 → Test-first specification, *Job lifecycle*): claim → run →
complete; a claimed job isn't double-claimed; duplicate enqueue is prevented; a
superseded job is marked, not run twice. Plus the two *Failure handling* cases
the deliverables name and the state machine cannot answer on its own: a worker
that dies mid-job, and the executions such a death leaves behind.

Everything here is asserted against stored rows rather than in-memory objects.
The queue's whole reason to exist is that a second process — a worker that was
not running when the job was enqueued — can pick the work up, and an assertion
that only held inside one session would prove nothing about that.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import pytest
from job_helpers import ManualClock, make_queue, seed_run
from provenance_helpers import make_recorder
from sqlalchemy.orm import Session

from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.jobs.queue import JobQueue
from groundscribe.provenance import models
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.engine import WORKFLOW_STAGE

LEASE = timedelta(seconds=30)


@pytest.fixture
def run(db_session: Session, snapshot_store: SnapshotStore) -> models.PipelineRun:
    return seed_run(db_session, snapshot_store)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def queue(db_session: Session, clock: ManualClock) -> JobQueue:
    return make_queue(db_session, clock)


def enqueue(
    queue: JobQueue,
    run: models.PipelineRun,
    *,
    job_type: JobType = JobType.EXTRACT_SOURCE_MODEL,
    payload: Mapping[str, Any] | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 1,
) -> models.Job:
    """Enqueue with the defaults these tests are not about."""
    return queue.enqueue(
        job_type=job_type,
        run=run,
        payload=payload,
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )


# ----------------------------------------------------------------------
# Claim → run → complete
# ----------------------------------------------------------------------


def test_an_enqueued_job_is_pending_and_belongs_to_its_run(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """Enqueueing records the work without doing any of it."""
    job = enqueue(queue, run, payload={"token_budget": 4000})

    assert job.status is JobStatus.PENDING
    assert job.job_type == JobType.EXTRACT_SOURCE_MODEL
    assert job.pipeline_run_id == run.id
    assert job.project_id == run.project_id
    assert job.payload == {"token_budget": 4000}
    assert job.claimed_by is None
    assert job.stage_execution_id is None


def test_claiming_marks_the_job_running_and_names_the_worker(
    queue: JobQueue, run: models.PipelineRun, clock: ManualClock
) -> None:
    """A claim is a stored fact: which worker holds the job, and since when."""
    enqueue(queue, run)

    claimed = queue.claim(worker_id="worker-1")

    assert claimed is not None
    assert claimed.status is JobStatus.RUNNING
    assert claimed.claimed_by == "worker-1"
    assert claimed.claimed_at == clock.now
    assert claimed.heartbeat_at == clock.now
    assert claimed.attempts == 1


def test_a_claimed_job_is_not_claimed_twice(queue: JobQueue, run: models.PipelineRun) -> None:
    """Two workers racing for one job: exactly one may have it.

    plan/09 → *reliable claiming*. The claim is a conditional update on the
    status the queue read, so the loser sees no rows changed and moves on rather
    than running the same stage a second time.
    """
    enqueue(queue, run)

    first = queue.claim(worker_id="worker-1")
    second = queue.claim(worker_id="worker-2")

    assert first is not None
    assert second is None


def test_jobs_are_claimed_oldest_first(
    queue: JobQueue, run: models.PipelineRun, clock: ManualClock
) -> None:
    """The queue is a queue: the work that has waited longest goes first."""
    first = enqueue(queue, run, job_type=JobType.EXTRACT_SOURCE_MODEL)
    clock.advance(timedelta(seconds=5))
    second = enqueue(queue, run, job_type=JobType.PROPOSE_ARCHITECTURE)

    assert queue.claim(worker_id="w") is first
    assert queue.claim(worker_id="w") is second


def test_completing_a_job_records_what_it_produced(
    queue: JobQueue, run: models.PipelineRun, clock: ManualClock
) -> None:
    """A finished job carries its own summary; the caller does not re-derive it."""
    enqueue(queue, run)
    job = queue.claim(worker_id="worker-1")
    assert job is not None
    clock.advance(timedelta(seconds=3))

    completed = queue.complete(job, result={"snapshot_ids": ["s1"]})

    assert completed.status is JobStatus.SUCCEEDED
    assert completed.result == {"snapshot_ids": ["s1"]}
    assert completed.completed_at == clock.now


# ----------------------------------------------------------------------
# Duplicate prevention and supersession
# ----------------------------------------------------------------------


def test_enqueueing_the_same_work_twice_returns_the_job_already_queued(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """plan/09 → *duplicate-job prevention*.

    Two identical commands are one piece of work. The second caller is handed
    the job the first one created, so an impatient double-click cannot make the
    same stage run twice against the same run.
    """
    first = enqueue(queue, run)
    second = enqueue(queue, run)

    assert second.id == first.id
    assert queue.pending_count() == 1


def test_a_running_job_still_blocks_a_duplicate(queue: JobQueue, run: models.PipelineRun) -> None:
    """Work in flight is work already queued; re-enqueueing joins it."""
    first = enqueue(queue, run)
    queue.claim(worker_id="worker-1")

    assert enqueue(queue, run).id == first.id


def test_the_same_work_may_be_enqueued_again_once_it_has_finished(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """Deduplication bounds concurrency, not history: a re-run is legitimate."""
    first = enqueue(queue, run)
    claimed = queue.claim(worker_id="worker-1")
    assert claimed is not None
    queue.complete(claimed)

    second = enqueue(queue, run)

    assert second.id != first.id
    assert second.status is JobStatus.PENDING


def test_superseding_marks_the_waiting_job_and_queues_its_replacement(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """plan/09 → *a superseded job is marked, not run twice*.

    The answer that arrived second is the one to act on, but the first job is
    kept and labelled rather than deleted: "we chose not to run this, and here is
    what replaced it" is a fact about the run.
    """
    stale = enqueue(queue, run, payload={"answers": ["a1"]})

    fresh = queue.enqueue(
        job_type=JobType.EXTRACT_SOURCE_MODEL,
        run=run,
        payload={"answers": ["a1", "a2"]},
        supersede=True,
    )

    assert stale.status is JobStatus.SUPERSEDED
    assert stale.superseded_by_id == fresh.id
    assert queue.claim(worker_id="worker-1") is fresh
    assert queue.claim(worker_id="worker-2") is None


def test_supersession_will_not_abandon_work_already_running(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """A running job is not superseded: it is already producing records.

    Marking it would leave a stage execution in flight that nothing owns — the
    orphan this module detects a few tests below — so the replacement joins the
    work in progress instead, and the caller learns which job to watch.
    """
    running = enqueue(queue, run)
    queue.claim(worker_id="worker-1")

    replacement = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run, supersede=True)

    assert replacement.id == running.id
    assert running.status is JobStatus.RUNNING


def test_different_work_on_the_same_run_is_not_deduplicated(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """The key is the work, not the run: a draft and a review may queue together."""
    enqueue(queue, run, job_type=JobType.GENERATE_DRAFT)
    enqueue(queue, run, job_type=JobType.SCORE_ARTICLE)

    assert queue.pending_count() == 2


def test_an_explicit_key_separates_work_the_type_alone_would_merge(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """Two articles of one project each draft independently."""
    enqueue(queue, run, job_type=JobType.GENERATE_DRAFT, dedupe_key="draft:a1")
    enqueue(queue, run, job_type=JobType.GENERATE_DRAFT, dedupe_key="draft:a2")

    assert queue.pending_count() == 2


# ----------------------------------------------------------------------
# Failure, retry and worker restart
# ----------------------------------------------------------------------


def test_failing_a_job_with_attempts_left_returns_it_to_the_queue(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """plan/09 → *resumption*: a transient failure is retried, not lost."""
    enqueue(queue, run, max_attempts=2)
    job = queue.claim(worker_id="worker-1")
    assert job is not None

    queue.fail(job, error_type="LLMTimeoutError", error_message="provider timed out")

    assert job.status is JobStatus.PENDING
    assert job.claimed_by is None
    assert queue.claim(worker_id="worker-2") is job


def test_failing_a_job_out_of_attempts_keeps_the_error(
    queue: JobQueue, run: models.PipelineRun
) -> None:
    """The last failure is recorded on the job, not only in the trace."""
    enqueue(queue, run)
    job = queue.claim(worker_id="worker-1")
    assert job is not None

    queue.fail(job, error_type="EvidenceError", error_message="claim c9 is not in the source")

    assert job.status is JobStatus.FAILED
    assert job.error_type == "EvidenceError"
    assert job.error_message == "claim c9 is not in the source"
    assert queue.claim(worker_id="worker-2") is None


def test_an_expired_lease_returns_the_job_to_the_queue(
    queue: JobQueue, run: models.PipelineRun, clock: ManualClock
) -> None:
    """plan/09 → *worker crashes, resumption*.

    A worker that dies marks nothing. What is left behind is a running job whose
    heartbeat stopped, and the next worker to start reclaims it — which is the
    only recovery available when the process that held it is gone.
    """
    enqueue(queue, run, max_attempts=2)
    queue.claim(worker_id="worker-1")
    clock.advance(timedelta(minutes=5))

    reclaimed = queue.reclaim_expired(lease=LEASE)

    assert [job.status for job in reclaimed] == [JobStatus.PENDING]
    assert queue.claim(worker_id="worker-2") is reclaimed[0]


def test_a_heartbeat_keeps_a_long_job_out_of_the_reclaimer(
    queue: JobQueue, run: models.PipelineRun, clock: ManualClock
) -> None:
    """Slow is not dead: a worker that says so keeps its job."""
    enqueue(queue, run)
    job = queue.claim(worker_id="worker-1")
    assert job is not None

    clock.advance(timedelta(seconds=20))
    queue.heartbeat(job)
    clock.advance(timedelta(seconds=20))

    assert queue.reclaim_expired(lease=LEASE) == ()
    assert job.status is JobStatus.RUNNING


def test_a_lost_job_out_of_attempts_fails_instead_of_looping(
    queue: JobQueue, run: models.PipelineRun, clock: ManualClock
) -> None:
    """A job that kills every worker it touches stops rather than cycling."""
    enqueue(queue, run)
    queue.claim(worker_id="worker-1")
    clock.advance(timedelta(minutes=5))

    (lost,) = queue.reclaim_expired(lease=LEASE)

    assert lost.status is JobStatus.FAILED
    assert lost.error_type == "WorkerLost"


# ----------------------------------------------------------------------
# Orphaned executions
# ----------------------------------------------------------------------


def test_a_running_execution_no_job_owns_is_orphaned(
    queue: JobQueue,
    run: models.PipelineRun,
    db_session: Session,
    snapshot_store: SnapshotStore,
) -> None:
    """plan/09 → *orphaned-execution detection*.

    A stage is only ever opened by a worker holding a job. One left running while
    its job is not is work that stopped without saying so, and it has to be
    findable — the partial records under it are evidence, but only if something
    goes looking for them.
    """
    recorder = make_recorder(db_session, snapshot_store)
    execution = recorder.start_stage(run, stage="extract_source_truth")
    enqueue(queue, run)
    job = queue.claim(worker_id="worker-1")
    assert job is not None
    queue.attach_execution(job, execution)
    queue.fail(job, error_type="WorkerLost", error_message="lease expired")

    assert queue.orphaned_executions() == (execution,)
    assert execution.status is ExecutionStatus.RUNNING


def test_an_execution_whose_job_is_running_is_not_orphaned(
    queue: JobQueue,
    run: models.PipelineRun,
    db_session: Session,
    snapshot_store: SnapshotStore,
) -> None:
    """Work in progress is not work abandoned."""
    recorder = make_recorder(db_session, snapshot_store)
    execution = recorder.start_stage(run, stage="extract_source_truth")
    enqueue(queue, run)
    job = queue.claim(worker_id="worker-1")
    assert job is not None
    queue.attach_execution(job, execution)

    assert queue.orphaned_executions() == ()


def test_the_workflows_own_execution_is_never_orphaned(
    queue: JobQueue,
    run: models.PipelineRun,
    db_session: Session,
    snapshot_store: SnapshotStore,
) -> None:
    """The engine's execution stays open for the life of the run, by design.

    It is not stage work and no job ever owns it (plan/05 →
    :data:`WORKFLOW_STAGE`), so an orphan check that counted it would report
    every healthy run as broken.
    """
    make_recorder(db_session, snapshot_store).start_stage(run, stage=WORKFLOW_STAGE)

    assert queue.orphaned_executions() == ()
