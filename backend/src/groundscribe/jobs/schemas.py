"""The wire shape of a queued job (phase 09).

Moved out of ``provenance.schemas`` with the model it validates from, for the
same reason: a job points into the provenance schema, never the other way. As
everywhere else, the schema validates directly from its row
(``from_attributes=True``), which is how schema/DB parity is checked.

The API returns this to a client polling a command it issued, so the fields are
chosen for that reader: what the job is, where it got to, which execution it
opened, and — if it failed — what went wrong. ``payload`` is what was asked for
and ``result`` what came back; both are ``dict[str, Any]`` because their inner
shape belongs to the stage that produced them, not to the queue.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from groundscribe.jobs.enums import JobStatus


class Job(BaseModel):
    """One unit of queued work, as stored and as reported."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    schema_version: int = 1
    job_type: str
    status: JobStatus = JobStatus.PENDING
    project_id: str
    pipeline_run_id: str
    stage_execution_id: str | None = None
    dedupe_key: str
    active_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 1
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime
    completed_at: datetime | None = None
    superseded_by_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None


__all__ = ["Job"]
