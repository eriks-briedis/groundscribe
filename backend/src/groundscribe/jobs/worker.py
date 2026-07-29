"""The worker: claim a job, run it, record what happened (phase 09).

plan/09 → *Implement the worker process loop + SSE event emission*, and the
failure handling around it.

The worker knows nothing about editorial stages. It is handed a table of
handlers and runs one; everything it does either side of that call is about
jobs, executions, failure and progress. Keeping the seam there is what stops the
background system from acquiring an opinion about drafting, and it is why the
crash behaviour can be tested without a model.

Three rules shape the code:

1. **Attach the execution before doing the work.** The job's link to the stage
   it opened is what makes a crashed job's records findable. Written on
   completion, it would be missing in the one case that needs it.
2. **Catch ``Exception``, never ``BaseException``.** A stage that fails is the
   worker's business and gets marked, retried and explained. A process being
   killed is not: it runs no cleanup, and pretending otherwise would only add a
   path that never executes when it matters.
3. **Write, never roll back.** The records a stage wrote before failing are the
   explanation of the failure. Phase 03 took the same position and for the same
   reason.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session

from groundscribe.jobs.enums import JobType
from groundscribe.jobs.models import Job
from groundscribe.jobs.queue import DEFAULT_LEASE, JobQueue
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, ExecutionStatus
from groundscribe.provenance.recorder import ProvenanceRecorder


class UnknownJobType(Exception):
    """Raised when nothing is registered to run a job's type.

    A failure rather than a silent skip: a job no worker can run would otherwise
    sit pending forever, and "queued" and "unrunnable" would look identical to
    everyone waiting on it.
    """


@dataclass(frozen=True)
class JobRequest:
    """What a handler is given, and the only way back to the worker.

    ``opened`` is how the handler reports the stage execution it started. It is a
    callback rather than a return value because it has to arrive *before* the
    work does, not after it: an execution named only on completion is missing
    precisely when a crash makes it matter.
    """

    job: Job
    session: Session
    opened: Callable[[models.StageExecution], None]

    @property
    def payload(self) -> dict[str, Any]:
        """What the command asked for, as the API enqueued it."""
        return self.job.payload


@dataclass(frozen=True)
class JobOutcome:
    """What a handler produced, summarised onto the job row."""

    result: dict[str, Any] = field(default_factory=dict)


class JobHandler(Protocol):
    """Runs one kind of job. Implemented by the application layer, not here."""

    def __call__(self, request: JobRequest, /) -> Awaitable[JobOutcome]: ...


@dataclass(frozen=True)
class Recovered:
    """What a starting worker found left behind by one that stopped."""

    reclaimed: tuple[Job, ...] = ()
    orphaned: tuple[models.StageExecution, ...] = ()


class Worker:
    """Drains the job queue, one job at a time, recording each as it goes."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        recorder: ProvenanceRecorder,
        handlers: Mapping[JobType, JobHandler],
        worker_id: str = "worker",
    ) -> None:
        self._queue = queue
        self._recorder = recorder
        self._handlers = dict(handlers)
        self.worker_id = worker_id

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover(self, *, lease: timedelta = DEFAULT_LEASE) -> Recovered:
        """Look for work a previous worker abandoned, before taking any new work.

        A starting worker is the only thing in the system positioned to notice
        that another one died — the dead process cannot report it, and nothing
        else is watching the heartbeats.

        Orphaned executions are reported rather than repaired. What should
        happen to a stage that stopped halfway is a decision with evidence
        behind it (replay it, fork from it, abandon it), and a worker quietly
        closing them off would destroy the evidence on its way past.
        """
        reclaimed = self._queue.reclaim_expired(lease=lease)
        return Recovered(reclaimed=reclaimed, orphaned=self._queue.orphaned_executions())

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    async def run_once(self) -> Job | None:
        """Claim and run one job, or report that there was nothing to do."""
        job = self._queue.claim(worker_id=self.worker_id)
        if job is None:
            return None

        self._emit(job, "job.claimed", run=job.pipeline_run)
        try:
            handler = self._handler_for(job)
            outcome = await handler(
                JobRequest(
                    job=job, session=self._queue.session, opened=lambda e: self._open(job, e)
                )
            )
        except Exception as exc:
            return self._fail(job, exc)

        self._queue.complete(job, result=outcome.result)
        self._emit(job, "job.completed", payload={"result": outcome.result})
        return job

    async def run_until_idle(self, *, limit: int | None = None) -> tuple[Job, ...]:
        """Run jobs until the queue is empty.

        The shape a one-shot CLI invocation, a test and a graceful shutdown all
        want. A long-lived worker polls on top of this rather than instead of
        it, so "drain everything currently queued" has exactly one
        implementation.
        """
        done: list[Job] = []
        while limit is None or len(done) < limit:
            job = await self.run_once()
            if job is None:
                break
            done.append(job)
        return tuple(done)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _handler_for(self, job: Job) -> JobHandler:
        try:
            job_type = JobType(job.job_type)
        except ValueError as exc:
            raise UnknownJobType(f"{job.job_type!r} is not a known job type") from exc
        handler = self._handlers.get(job_type)
        if handler is None:
            raise UnknownJobType(f"no handler is registered for {job_type.value!r}")
        return handler

    def _open(self, job: Job, execution: models.StageExecution) -> None:
        """Record the execution against the job, then announce the job started."""
        self._queue.attach_execution(job, execution)
        self._emit(job, "job.started", execution=execution)

    def _fail(self, job: Job, exc: Exception) -> Job:
        """Fail the job and the stage under it, keeping everything already written."""
        execution = job.stage_execution
        if execution is not None and execution.status is ExecutionStatus.RUNNING:
            self._recorder.fail_stage(
                execution, error_type=type(exc).__name__, error_message=str(exc)
            )
        self._queue.fail(job, error_type=type(exc).__name__, error_message=str(exc))
        self._emit(
            job,
            "job.failed",
            execution=execution,
            payload={"error_type": type(exc).__name__, "error_message": str(exc)},
        )
        return job

    def _emit(
        self,
        job: Job,
        event_type: str,
        *,
        execution: models.StageExecution | None = None,
        run: models.PipelineRun | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append a job lifecycle event to the run's timeline.

        Anchored to the stage execution whenever there is one, because that is
        what the per-job stream reads; before the stage exists — and if it never
        does — the run is the only anchor available.
        """
        anchor = execution or job.stage_execution
        self._recorder.emit(
            event_type=event_type,
            actor_type=ActorType.SYSTEM,
            actor_id=self.worker_id,
            execution=anchor,
            run=None if anchor is not None else (run or job.pipeline_run),
            payload={"job_id": job.id, "job_type": job.job_type, **(payload or {})},
        )


__all__ = ["JobOutcome", "JobRequest", "Recovered", "UnknownJobType", "Worker"]
