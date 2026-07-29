"""Running a stage again, as itself or with something changed (phase 12).

A replay and a fork are one operation with and without a change, so they are one
mechanism here: queue the *same job the original ran*, pointed at the execution
it came from, carrying whatever variables the caller wants altered.

Three decisions worth stating.

**The original job's payload is the recorded input.** plan/12 asks a replay to
re-execute "with recorded inputs + config"; the payload the first job ran with is
exactly that, so it is copied rather than reconstructed. A stage nobody queued —
final validation runs in the request, because it calls no model — therefore has
nothing to replay, and says so instead of inventing a payload.

**The new execution is opened by the work, not by the request.** Phase 03's model
is that an execution exists because a stage started; a request that pre-created
one would invent a fourth status between "not run" and "running" for every client
to interpret. So these endpoints answer with a *job*, and the job names its
execution the moment the worker opens it.

**The link is the parent.** The re-run execution branches from the original,
which is what makes the pair comparable at all — and what stops a replay from
being mistaken for the thing it replayed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.experiments.variables import ForkVariables
from groundscribe.jobs.enums import JobType
from groundscribe.jobs.models import Job
from groundscribe.provenance import models
from groundscribe.scoring.scoring import SCORE_STAGE
from groundscribe.stages.architecture import ARCHITECTURE_STAGE
from groundscribe.stages.brief import BRIEF_STAGE
from groundscribe.stages.drafting import DRAFT_STAGE
from groundscribe.stages.extraction import EXTRACTION_STAGE
from groundscribe.stages.planning import PLAN_STAGE
from groundscribe.stages.review import REVIEW_STAGE
from groundscribe.stages.rewriting import REWRITE_STAGE
from groundscribe.stages.voice import VOICE_STAGE

#: Which job runs which stage. The inverse of the worker's dispatch table, and
#: tested against it: a stage added to one and not the other is a stage that can
#: run but never be re-run.
STAGE_JOBS: dict[str, JobType] = {
    EXTRACTION_STAGE: JobType.EXTRACT_SOURCE_MODEL,
    ARCHITECTURE_STAGE: JobType.PROPOSE_ARCHITECTURE,
    BRIEF_STAGE: JobType.GENERATE_BRIEF,
    DRAFT_STAGE: JobType.GENERATE_DRAFT,
    REVIEW_STAGE: JobType.REVIEW_ARTICLE,
    PLAN_STAGE: JobType.PLAN_REVISION,
    REWRITE_STAGE: JobType.REWRITE_ARTICLE,
    VOICE_STAGE: JobType.ALIGN_VOICE,
    SCORE_STAGE: JobType.SCORE_ARTICLE,
}

#: Where a re-run's instructions sit inside the job payload.
RERUN_KEY = "rerun"


class NotRerunnable(LookupError):
    """This execution cannot be run again, and the message says why."""


class Rerun(BaseModel):
    """The instructions one re-run carries, stored in the job's payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_execution_id: str
    requested_by: str
    reason: str = ""
    variables: ForkVariables = ForkVariables()

    @property
    def is_fork(self) -> bool:
        """Whether anything was actually changed. An empty fork is a replay."""
        return not self.variables.empty


def plan_rerun(
    session: Session, execution: models.StageExecution, rerun: Rerun
) -> tuple[JobType, dict[str, Any]]:
    """The job type and payload that will run ``execution``'s stage again.

    Raises rather than guessing. A stage with no job behind it, or one whose
    original job has been pruned, cannot be replayed *faithfully* — and a replay
    that ran against inputs somebody reconstructed would be a different
    experiment wearing the same name.
    """
    job_type = STAGE_JOBS.get(execution.stage)
    if job_type is None:
        raise NotRerunnable(f"{execution.stage} is not run by a job — nothing was queued to repeat")

    original = session.scalars(
        select(Job).where(Job.stage_execution_id == execution.id).order_by(Job.created_at.desc())
    ).first()
    if original is None:
        raise NotRerunnable(
            f"no job recorded for execution {execution.id}; its inputs cannot be recovered"
        )

    payload = dict(original.payload)
    payload[RERUN_KEY] = rerun.model_dump(mode="json")
    return job_type, payload


def rerun_of(payload: dict[str, Any]) -> Rerun | None:
    """The re-run instructions in a job payload, if this job is one."""
    recorded = payload.get(RERUN_KEY)
    return Rerun.model_validate(recorded) if recorded else None


__all__ = ["RERUN_KEY", "STAGE_JOBS", "NotRerunnable", "Rerun", "plan_rerun", "rerun_of"]
