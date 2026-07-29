"""The jobs table (phase 09).

Phase 03 left a shell here-shaped in ``provenance.models`` for phase 09 to fill.
Filling it moved it: a job holds foreign keys into the provenance schema
(``pipeline_runs``, ``stage_executions``) and into the editorial one
(``projects``), so it depends on both — and the dependency must run that way
round, never back, or the provenance models would have to know what a queue is.

``records.py`` still classifies ``jobs`` as an execution record. That partition
is about what a *row* is, not about which module declares it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON as JSONColumn
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from groundscribe.db import Base, UTCDateTime, enum_column
from groundscribe.jobs.enums import JobStatus
from groundscribe.provenance.models import PipelineRun, ProvenanceRecord, StageExecution


class Job(Base, ProvenanceRecord):
    """One unit of pipeline work, waiting for or held by a worker.

    Two keys, not one, and the pair is the whole deduplication design:

    - ``dedupe_key`` is what the work *is* — this stage, for this run — and it
      stays on the row forever, so history can be grouped by it.
    - ``active_key`` is the same string only while the job is claimable, and it
      carries a unique constraint. Clearing it on a terminal status is what lets
      the same work be enqueued again later while making it impossible for two
      live jobs to describe the same work at once.

    The alternative — a partial unique index over "status in (pending, running)"
    — says the same thing, but as a predicate the database has to be told how to
    express, differently on each backend. A nullable unique column is portable
    and enforced identically on SQLite and PostgreSQL (plan/00 → Tech stack).

    ``stage_execution_id`` is set the moment the worker opens the stage, before
    any work happens. That is what makes a crashed job's partial records
    findable: an execution nobody can name is an execution nobody can recover.
    """

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("active_key", name="uq_jobs_active_key"),)

    # Not an enum column: job types are names of work, the way ``stage`` on a
    # stage execution is. The closed set lives in ``jobs.enums`` where the
    # dispatch table can see it, and the column stays free of a CHECK that would
    # need a migration every time a stage is added.
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus), default=JobStatus.PENDING, nullable=False
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    pipeline_run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    stage_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=True
    )
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    active_key: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # The liveness signal. A worker that stops updating it has stopped, whatever
    # its own opinion on the matter; nothing else can be known about a process
    # that died without saying so.
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    pipeline_run: Mapped[PipelineRun] = relationship()
    stage_execution: Mapped[StageExecution | None] = relationship()
    superseded_by: Mapped[Job | None] = relationship(remote_side="Job.id")


__all__ = ["Job"]
