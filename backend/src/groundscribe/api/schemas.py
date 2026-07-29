"""What the API accepts and returns (phase 09).

Separate from the domain and provenance schemas on purpose. Those are the shapes
groundscribe *stores*; these are the shapes it *publishes*, and they are the
contract phase 11's generated client is built from. Collapsing them would make
every column rename a breaking API change.

Every command answers with the same envelope — where the run is, what may be done
to it next, and the job if one was queued — so a client has one response shape to
handle rather than one per endpoint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from groundscribe.domain.enums import AnswerResponse, SourceFormat
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.jobs.schemas import Job
from groundscribe.provenance.enums import ActorType, ExecutionStatus, InvocationOutcome
from groundscribe.workflow.states import WorkflowState


class CommandResponse(BaseModel):
    """The answer to every command: position, affordances, and any queued work."""

    project_id: str
    run_id: str
    state: WorkflowState
    available_actions: tuple[str, ...]
    job: Job | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CreateProject(BaseModel):
    """Open a project. The constraints are required, not defaulted.

    A project's audience, platform and permitted providers decide what may be
    written and who may see the material; guessing them would mean guessing
    whether an external provider is allowed to read the source.
    """

    title: str
    author_id: str
    constraints: EditorialConstraints
    description: str = ""


class ImportSource(BaseModel):
    """Add one piece of source material to a project."""

    title: str
    text: str
    source_format: SourceFormat = SourceFormat.PLAIN_TEXT
    confidential: bool = False
    uri: str | None = None


class ExtractSourceModel(BaseModel):
    """Build the structured source model."""

    token_budget: int | None = None


class AnswerGap(BaseModel):
    """One answer to one surfaced question."""

    text: str = ""
    answered_by: str
    response: AnswerResponse = AnswerResponse.ANSWERED


class UpdateArchitecture(BaseModel):
    """An author's edits to a proposed architecture."""

    commands: list[dict[str, Any]]
    requested_by: str
    reason: str = ""
    accepted_warnings: list[str] = Field(default_factory=list)


class ActorAction(BaseModel):
    """A human action. The actor is mandatory: an anonymous decision is unreviewable."""

    actor_id: str


class CreateExperiment(BaseModel):
    name: str


class ExecutionSummary(BaseModel):
    """One stage execution, as a client sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    pipeline_run_id: str
    parent_execution_id: str | None = None
    stage: str
    impl_version: str
    ordinal: int
    status: ExecutionStatus
    correlation_id: str
    started_at: datetime
    completed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


class TraceEventOut(BaseModel):
    """One event in a run's timeline."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: str
    timestamp: datetime
    actor_type: ActorType
    actor_id: str
    payload: dict[str, Any]
    correlation_id: str
    causation_id: str | None = None
    sequence: int


class ModelInvocationOut(BaseModel):
    """One model call, including the attempts that failed.

    The failed ones are published, not filtered: a client showing only accepted
    calls would under-report exactly the runs that cost the most.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_invocation_id: str | None = None
    attempt_ordinal: int
    outcome: InvocationOutcome
    provider: str
    model: str
    template_id: str
    template_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class ExecutionComparison(BaseModel):
    """Two executions, side by side. Phase 12 says what the difference means."""

    left: ExecutionSummary
    right: ExecutionSummary


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: ExecutionStatus
    created_at: datetime


__all__ = [
    "ActorAction",
    "AnswerGap",
    "CommandResponse",
    "CreateExperiment",
    "CreateProject",
    "ExecutionComparison",
    "ExecutionSummary",
    "ExperimentOut",
    "ExtractSourceModel",
    "ImportSource",
    "ModelInvocationOut",
    "TraceEventOut",
    "UpdateArchitecture",
]
