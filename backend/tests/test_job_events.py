"""The per-job progress stream (phase 09).

Spec (plan/09 → Deliverables): *SSE progress stream per job/execution*, and the
integration test *progress events stream for a running job*.

The stream is built out of the trace rather than beside it. A job's progress is
the trace of the stage execution it opened, so what a client watches live and
what an auditor reads afterwards are the same rows — two channels could disagree,
and the one a user saw would be the one nobody could reconstruct.

Two consequences the tests pin:

- **The stream is resumable by sequence.** Phase 03 gave every trace event a
  stored position within its run, which is exactly what an SSE ``Last-Event-ID``
  needs; a reconnecting client resumes rather than replays.
- **It ends when the job does.** A stream that stayed open after a terminal job
  would leave every client waiting for an event that cannot arrive.

Time is injected. A poll loop tested against a real clock is a test that is slow
when it passes and flaky when it fails.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.orm import Session

from groundscribe.jobs.enums import JobType
from groundscribe.jobs.events import JobEventStream, ProgressEvent
from groundscribe.jobs.models import Job
from groundscribe.jobs.queue import JobQueue
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from job_helpers import ManualClock, make_queue, seed_run
from provenance_helpers import make_recorder


@pytest.fixture
def queue(db_session: Session) -> JobQueue:
    return make_queue(db_session, ManualClock())


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    return make_recorder(db_session, snapshot_store)


@pytest.fixture
def run(
    db_session: Session, snapshot_store: SnapshotStore, recorder: ProvenanceRecorder
) -> models.PipelineRun:
    return seed_run(db_session, snapshot_store, recorder)


class Ticker:
    """A sleep that never sleeps, and runs a callback each time it is awaited.

    Standing in for the passage of time: each "tick" is the moment a real stream
    would have gone back to the database, so a test can make something happen
    exactly between two polls.
    """

    def __init__(self, on_tick: Callable[[int], None] | None = None) -> None:
        self.ticks = 0
        self._on_tick = on_tick

    async def __call__(self, _seconds: float) -> None:
        self.ticks += 1
        if self._on_tick is not None:
            self._on_tick(self.ticks)


def started_job(queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun) -> Job:
    """A claimed job with a stage execution open under it."""
    queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    job = queue.claim(worker_id="worker-1")
    assert job is not None
    execution = recorder.start_stage(run, stage="extract_source_truth")
    queue.attach_execution(job, execution)
    return job


def finish(queue: JobQueue, job: Job) -> None:
    """Complete a job from inside a tick, discarding the row it returns."""
    queue.complete(job, result={"snapshot_ids": ["s1"]})


def stop(queue: JobQueue, job: Job) -> None:
    """Cancel a job from inside a tick."""
    queue.cancel(job)


def progress(recorder: ProvenanceRecorder, job: Job, session: Session, **payload: Any) -> None:
    execution = session.get(models.StageExecution, job.stage_execution_id)
    assert execution is not None
    recorder.emit(
        event_type="stage.progress",
        actor_type=ActorType.SYSTEM,
        actor_id="pipeline",
        execution=execution,
        payload=payload,
    )


async def collect(stream: JobEventStream, job: Job, **kwargs: Any) -> list[ProgressEvent]:
    return [event async for event in stream.stream(job.id, **kwargs)]


# ----------------------------------------------------------------------
# What the stream carries
# ----------------------------------------------------------------------


async def test_the_stream_opens_by_saying_where_the_job_is(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun, db_session: Session
) -> None:
    """A client that connects late must not have to guess the job's state."""
    job = started_job(queue, recorder, run)
    ticker = Ticker(lambda _tick: finish(queue, job))

    events = await collect(JobEventStream(db_session, queue, sleep=ticker), job)

    assert events[0].event == "job.status"
    assert events[0].data["status"] == "running"
    assert events[0].data["job_id"] == job.id
    assert events[0].data["stage_execution_id"] == job.stage_execution_id


async def test_the_stream_carries_the_stages_own_trace(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun, db_session: Session
) -> None:
    """Progress is the stage's recorded events, not a parallel narrative."""
    job = started_job(queue, recorder, run)
    progress(recorder, job, db_session, claims=3)
    progress(recorder, job, db_session, claims=7)
    queue.complete(job)

    events = await collect(JobEventStream(db_session, queue, sleep=Ticker()), job)

    stage = [event for event in events if event.event == "stage.progress"]
    assert [event.data["payload"]["claims"] for event in stage] == [3, 7]


async def test_the_stream_ends_when_the_job_reaches_a_terminal_status(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun, db_session: Session
) -> None:
    """The last frame says how it ended, and then the stream closes.

    Without the closing frame a client would have to infer completion from
    silence, which is indistinguishable from a network that stopped.
    """
    job = started_job(queue, recorder, run)
    ticker = Ticker(lambda tick: finish(queue, job) if tick == 2 else None)

    events = await collect(JobEventStream(db_session, queue, sleep=ticker), job)

    assert events[-1].event == "job.status"
    assert events[-1].data["status"] == "succeeded"
    assert ticker.ticks == 2


async def test_a_failing_job_says_why_before_the_stream_closes(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun, db_session: Session
) -> None:
    """A stream that just stopped would tell a client nothing it can act on."""
    job = started_job(queue, recorder, run)
    queue.fail(job, error_type="EvidenceError", error_message="claim c9 is not in the source")

    events = await collect(JobEventStream(db_session, queue, sleep=Ticker()), job)

    assert events[-1].data["status"] == "failed"
    assert events[-1].data["error_type"] == "EvidenceError"
    assert "c9" in events[-1].data["error_message"]


# ----------------------------------------------------------------------
# Resumption and encoding
# ----------------------------------------------------------------------


async def test_a_reconnecting_client_resumes_rather_than_replays(
    queue: JobQueue, recorder: ProvenanceRecorder, run: models.PipelineRun, db_session: Session
) -> None:
    """plan/03 gave every event a stored position; SSE's Last-Event-ID uses it."""
    job = started_job(queue, recorder, run)
    progress(recorder, job, db_session, claims=3)
    progress(recorder, job, db_session, claims=7)
    queue.complete(job)
    stream = JobEventStream(db_session, queue, sleep=Ticker())

    first = await collect(stream, job)
    seen = max(event.sequence for event in first if event.sequence is not None)
    resumed = await collect(stream, job, after=seen)

    assert [event.data["payload"]["claims"] for event in first if event.event == "stage.progress"]
    assert [event for event in resumed if event.event == "stage.progress"] == []


async def test_a_job_with_no_execution_yet_still_streams(
    queue: JobQueue, run: models.PipelineRun, db_session: Session
) -> None:
    """A queued job has no stage to report on, and must not fail the request."""
    job = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    ticker = Ticker(lambda tick: stop(queue, job) if tick == 1 else None)

    events = await collect(JobEventStream(db_session, queue, sleep=ticker), job)

    assert [event.data["status"] for event in events] == ["pending", "cancelled"]


async def test_an_unknown_job_streams_nothing_rather_than_hanging(
    queue: JobQueue, db_session: Session
) -> None:
    """A stream for a job that does not exist ends immediately."""
    assert [
        event async for event in JobEventStream(db_session, queue, sleep=Ticker()).stream("nope")
    ] == []


def test_a_frame_is_encoded_as_the_sse_wire_format() -> None:
    """``id``/``event``/``data``, blank-line terminated, exactly as EventSource reads."""
    frame = ProgressEvent(event="stage.progress", data={"claims": 3}, sequence=4).encode()

    assert frame.startswith("id: 4\n")
    assert "event: stage.progress\n" in frame
    assert 'data: {"claims": 3}\n' in frame
    assert frame.endswith("\n\n")


def test_a_frame_without_a_position_omits_the_id() -> None:
    """Synthesised status frames have no place in the run's timeline to cite."""
    frame = ProgressEvent(event="job.status", data={"status": "pending"}).encode()

    assert not frame.startswith("id:")
    assert frame.startswith("event: job.status\n")
