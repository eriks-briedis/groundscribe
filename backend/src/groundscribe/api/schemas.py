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

from groundscribe.app.views import ComparisonRow
from groundscribe.domain.enums import AnswerResponse, FindingStatus, SourceFormat
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.jobs.schemas import Job
from groundscribe.provenance.enums import ActorType, ExecutionStatus, InvocationOutcome
from groundscribe.voice.enums import VoiceScope
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


class ApproveSuggestion(BaseModel):
    """Making an inferred rule permanent, and naming the version it becomes."""

    actor_id: str
    version: str


class RejectSuggestion(BaseModel):
    """Declining an inferred rule, with the reason kept."""

    actor_id: str
    reason: str = ""


class VoiceProfileSummary(BaseModel):
    """One stored profile version, as a client lists them."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    profile_id: str
    scope: VoiceScope
    project_id: str | None = None
    article_id: str | None = None
    version: str
    active: bool


class ActiveInstructionOut(BaseModel):
    """One instruction in force, and where it came from.

    The source travels to the client because the question it answers — "why does
    it write like this, and where do I change it?" — is asked by a person looking
    at a screen, not by the resolver.
    """

    instruction_id: str
    category: str
    strength: str
    source: str
    overrides: str


class EffectiveVoice(BaseModel):
    """The resolved voice, and the profiles it was resolved from."""

    sources: tuple[str, ...]
    active: tuple[ActiveInstructionOut, ...]
    suppressed: tuple[str, ...] = ()


class VoiceSuggestionOut(BaseModel):
    """An inferred rule awaiting an answer, with the edits behind it."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    habit: str
    instruction: dict[str, Any]
    evidence: dict[str, Any]
    status: FindingStatus
    decided_by: str
    reason: str


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


class RerunResponse(BaseModel):
    """What a replay or a fork answers with (phase 12).

    The job, because the work is a model call and phase 09 keeps those out of
    the request. The execution it produces is opened by the worker and named by
    the job the moment it starts — a request that pre-created one would invent a
    status between "not run" and "running" for every client to interpret.
    """

    source_execution_id: str
    job: Job


class ExecutionComparison(BaseModel):
    """Two executions side by side (plan/11 → *Run comparison*).

    The summaries are what phase 09 returned; ``differences`` and the distance
    are what a comparison screen needs on top of them — the fields that differ,
    named one per row, and how far the two outputs sit apart. Only what both
    sides recorded is compared: human preference is phase 12's, and a column for
    it here would be a promise this phase cannot keep.
    """

    left: ExecutionSummary
    right: ExecutionSummary
    differences: list[ComparisonRow] = Field(default_factory=list)
    output_edit_distance: int | None = None


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: ExecutionStatus
    created_at: datetime


__all__ = [
    "ActiveInstructionOut",
    "ActorAction",
    "AnswerGap",
    "ApproveSuggestion",
    "CommandResponse",
    "CreateExperiment",
    "CreateProject",
    "EffectiveVoice",
    "ExecutionComparison",
    "ExecutionSummary",
    "ExperimentOut",
    "ExtractSourceModel",
    "ImportSource",
    "ModelInvocationOut",
    "RejectSuggestion",
    "TraceEventOut",
    "UpdateArchitecture",
    "VoiceProfileSummary",
    "VoiceSuggestionOut",
]
