"""Enqueueing, claiming and recovering jobs (phase 09).

plan/09 → *reliable claiming, duplicate-job prevention, superseded-job handling,
worker restart/resumption, orphaned-execution detection*. Every one of those is
a statement about rows, so every one of them is implemented as a statement about
rows: the queue holds no in-memory state at all, and two workers in two
processes see exactly what one worker in one process sees.

Three rules are worth stating up front, because they are what the tests pin:

1. **A claim is a conditional update, not a read followed by a write.** The
   worker that loses the race sees zero rows changed and moves on. Reading the
   pending set and then writing would let two workers run the same stage.
2. **Supersession applies to waiting work only.** A running job is already
   producing records; marking it superseded would abandon a live stage
   execution, which is the orphan this module exists to detect.
3. **A worker that dies marks nothing.** Recovery is therefore driven by the
   absence of a heartbeat, not by any signal from the process that failed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.jobs.models import Job
from groundscribe.provenance import models
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.workflow.engine import WORKFLOW_STAGE

#: How long a claim survives without a heartbeat before another worker may take
#: the job. Generous relative to the heartbeat interval: reclaiming a job whose
#: worker is merely slow would run the same stage twice, which is worse than
#: waiting.
DEFAULT_LEASE = timedelta(minutes=5)

#: Recorded on a job whose worker vanished. A distinct error type because it is
#: a distinct diagnosis: the stage did not fail, the process holding it did.
WORKER_LOST = "WorkerLost"


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_id() -> str:
    return uuid.uuid4().hex


class JobQueue:
    """The jobs table, expressed as the operations a worker and an API need."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.session = session
        self._clock = clock or _default_clock
        self._new_id = id_factory or _default_id

    # ------------------------------------------------------------------
    # Enqueueing
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        job_type: JobType,
        run: models.PipelineRun,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 1,
        supersede: bool = False,
    ) -> Job:
        """Queue work, or hand back the job already queued to do it.

        The default key is "this stage, for this run", which is the identity two
        clicks of the same button share. A caller with a finer notion of the
        same work — two articles of one project drafting independently — passes
        its own.

        ``supersede`` is for a command that *replaces* what is waiting rather
        than repeating it: a second round of answers makes the first round's
        extraction wrong, not redundant. It cannot touch a running job, so a
        caller that supersedes may still be handed work already in flight; the
        returned job is always the one to watch.
        """
        key = dedupe_key or f"{run.id}:{job_type.value}"
        existing = self.active(key)
        if existing is not None and (not supersede or existing.status is JobStatus.RUNNING):
            return existing

        now = self._clock()
        job = Job(
            id=self._new_id(),
            job_type=job_type.value,
            status=JobStatus.PENDING,
            project_id=run.project_id,
            pipeline_run_id=run.id,
            dedupe_key=key,
            active_key=key,
            payload=dict(payload or {}),
            result={},
            attempts=0,
            max_attempts=max_attempts,
            created_at=now,
        )
        if existing is not None:
            # Three ordered writes, because two constraints pull opposite ways:
            # the unique ``active_key`` must be freed *before* the replacement
            # is inserted, and the replacement must exist *before* anything
            # points a foreign key at it.
            self._release(existing, status=JobStatus.SUPERSEDED, at=now)
            self.session.flush()

        self.session.add(job)
        self.session.flush()

        if existing is not None:
            existing.superseded_by_id = job.id
            self.session.flush()
        return job

    def active(self, dedupe_key: str) -> Job | None:
        """The claimable job for ``dedupe_key``, if there is one."""
        return self.session.scalars(select(Job).where(Job.active_key == dedupe_key)).one_or_none()

    def get(self, job_id: str) -> Job | None:
        return self.session.get(Job, job_id)

    def pending_count(self) -> int:
        """How many jobs are waiting; the API reports it, the tests assert it."""
        return int(
            self.session.execute(
                select(func.count()).select_from(Job).where(Job.status == JobStatus.PENDING)
            ).scalar_one()
        )

    # ------------------------------------------------------------------
    # Claiming and running
    # ------------------------------------------------------------------

    def claim(self, *, worker_id: str) -> Job | None:
        """Take the oldest pending job, or return ``None`` if none is free.

        Candidates are read first and then claimed one at a time by conditional
        update. The read may be stale — another worker may have taken the job
        between the select and the update — and that is exactly what the
        condition is for: a stale candidate changes no rows and the loop simply
        tries the next one.
        """
        now = self._clock()
        candidates = list(
            self.session.scalars(
                select(Job).where(Job.status == JobStatus.PENDING).order_by(Job.created_at, Job.id)
            )
        )
        for job in candidates:
            # Typed as a cursor result because the row count is the answer: an
            # ORM-level update reports how many rows the condition matched, and
            # zero means another worker got there first.
            result = cast(
                "CursorResult[Any]",
                self.session.execute(
                    update(Job)
                    .where(Job.id == job.id, Job.status == JobStatus.PENDING)
                    .values(
                        status=JobStatus.RUNNING,
                        claimed_by=worker_id,
                        claimed_at=now,
                        heartbeat_at=now,
                        attempts=Job.attempts + 1,
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount:
                self.session.expire(job)
                return job
        return None

    def attach_execution(self, job: Job, execution: models.StageExecution) -> Job:
        """Name the stage execution this job opened, while it is still running.

        Assigned through the relationship rather than the id column: writing the
        column alone leaves ``job.stage_execution`` reading ``None`` until
        something expires it, and the worker asks for it moments later to anchor
        the job's own progress events.
        """
        job.stage_execution = execution
        self.session.flush()
        return job

    def heartbeat(self, job: Job) -> Job:
        """Say the worker is still alive, so the reclaimer leaves the job alone."""
        job.heartbeat_at = self._clock()
        self.session.flush()
        return job

    def complete(self, job: Job, *, result: Mapping[str, Any] | None = None) -> Job:
        """Finish a job, recording what it produced."""
        job.result = dict(result or {})
        self._release(job, status=JobStatus.SUCCEEDED, at=self._clock())
        self.session.flush()
        return job

    def fail(self, job: Job, *, error_type: str, error_message: str) -> Job:
        """Record a failure, retrying if the job has attempts left.

        The error is written either way. A job that succeeds on its second
        attempt still failed once, and a queue that kept only the final outcome
        would hide exactly the flakiness worth knowing about.
        """
        job.error_type = error_type
        job.error_message = error_message
        if job.attempts < job.max_attempts:
            job.status = JobStatus.PENDING
            job.claimed_by = None
            job.claimed_at = None
            job.heartbeat_at = None
        else:
            self._release(job, status=JobStatus.FAILED, at=self._clock())
        self.session.flush()
        return job

    def cancel(self, job: Job) -> Job:
        """Stop a job a person no longer wants run."""
        self._release(job, status=JobStatus.CANCELLED, at=self._clock())
        self.session.flush()
        return job

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def reclaim_expired(self, *, lease: timedelta = DEFAULT_LEASE) -> tuple[Job, ...]:
        """Return jobs whose worker stopped heartbeating to the queue.

        Run at worker start-up and around each poll. A crashed process leaves
        its job ``RUNNING`` forever otherwise, and — worse — leaves the stage
        execution under it running too, which is why :meth:`orphaned_executions`
        is the query that follows this one.
        """
        cutoff = self._clock() - lease
        lost = list(
            self.session.scalars(
                select(Job)
                .where(Job.status == JobStatus.RUNNING, Job.heartbeat_at < cutoff)
                .order_by(Job.created_at, Job.id)
            )
        )
        for job in lost:
            self.fail(
                job,
                error_type=WORKER_LOST,
                error_message=f"worker {job.claimed_by} stopped reporting; lease expired",
            )
        return tuple(lost)

    def orphaned_executions(self) -> tuple[models.StageExecution, ...]:
        """Stage executions still running that no running job owns.

        The engine's own execution is excluded by name: it stays open for the
        life of the run and no job ever holds it (plan/05 →
        :data:`~groundscribe.workflow.engine.WORKFLOW_STAGE`), so counting it
        would report every healthy run as broken.

        The subquery filters out nulls deliberately. ``NOT IN`` over a set
        containing NULL is never true, and an unattached job would otherwise
        silently make this query return nothing at all.
        """
        owned = select(Job.stage_execution_id).where(
            Job.status == JobStatus.RUNNING, Job.stage_execution_id.is_not(None)
        )
        return tuple(
            self.session.scalars(
                select(models.StageExecution)
                .where(
                    models.StageExecution.status == ExecutionStatus.RUNNING,
                    models.StageExecution.stage != WORKFLOW_STAGE,
                    models.StageExecution.id.not_in(owned),
                )
                .order_by(models.StageExecution.started_at, models.StageExecution.id)
            )
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _release(self, job: Job, *, status: JobStatus, at: datetime) -> None:
        """Move a job to a terminal status and free its deduplication key."""
        job.status = status
        job.active_key = None
        job.completed_at = at


__all__ = ["DEFAULT_LEASE", "WORKER_LOST", "JobQueue"]
