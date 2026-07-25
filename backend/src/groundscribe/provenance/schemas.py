"""Pydantic schemas for the execution-provenance entities (phase 03).

The in-memory / wire shapes of the records that explain how an artefact was
produced. As in phase 02 every record carries a ``schema_version`` and validates
directly from its SQLAlchemy row (``from_attributes=True``), which is how parity
is checked.

Unlike the editorial schemas these carry no ``created_by_execution_id``: an
execution record is not *produced by* an execution, it *is* the execution. The
link runs the other way — editorial artefacts point at the execution that made
them.

Payload fields are typed ``dict[str, Any]``: their inner shape belongs to the
provider, tool or stage that produced them, and pinning it here would force a
schema migration every time a provider changed a response envelope. They are
still redacted before persistence, which is the property that matters.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundscribe.provenance.enums import (
    ActorType,
    ArtifactDirection,
    ContextDisposition,
    ExecutionStatus,
    InterventionType,
    InvocationOutcome,
    RetryType,
    ToolInitiator,
)


class _Record(BaseModel):
    """Common base: identity and version stamp."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    schema_version: int = 1


class Message(BaseModel):
    """One message in the sequence sent to a model, with its role."""

    model_config = ConfigDict(frozen=True)

    role: str
    content: str


class ToolDefinition(BaseModel):
    """A tool as *offered* to the model, versioned.

    Recorded even when the model never calls it: the set of tools on offer
    changes what the model does, so a request without them is not the request
    that was sent.

    ``requires_approval`` travels with the offer rather than with the call: what
    a tool needed *at the moment it was offered* is the fact a later audit asks
    about, and the policy may have changed since.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False


class EffectiveRequest(BaseModel):
    """Everything needed to reissue a model call exactly as it was made.

    plan/03 → *exact effective request*: template id and version, the rendered
    prompt, the full message sequence with roles, the tool definitions supplied,
    the structured-output schema, and provider-specific request config.

    ``template_variables`` and ``output_schema_version`` are what plan/04's
    renderer adds: the same template rendered with different inputs is a
    different call, and the rendered text alone cannot always say which variable
    produced which fragment. Both default to empty so records written before the
    renderer existed still validate.

    ``redacted`` marks the persisted form. It is not decoration: a reader must be
    able to tell a stored request (secrets removed) from the live one, rather
    than assuming byte-identity with what crossed the wire.
    """

    template_id: str
    template_version: str
    rendered_prompt: str
    template_variables: dict[str, Any] = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    tool_definitions: list[ToolDefinition] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    output_schema_version: int | None = None
    provider_config: dict[str, Any] = Field(default_factory=dict)
    redacted: bool = False


class ContextCandidate(BaseModel):
    """A candidate offered to context selection, and what became of it."""

    reference: str
    disposition: ContextDisposition
    reason: str = ""
    score: float | None = None


class PipelineRun(_Record):
    """One end-to-end execution of the editorial pipeline for a project.

    ``runtime_config`` captures the effective runtime configuration the run
    started with (providers, limits, feature flags) so a later replay can tell
    whether a difference in output came from the input or from the setup.
    """

    project_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    correlation_id: str
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


class StageExecution(_Record):
    """One stage of a run: the unit everything else in this module hangs from.

    ``parent_execution_id`` records the execution this one branched from — a
    re-run, a rewrite, or an experiment arm — so two branches can be compared
    against their true parents rather than against each other.

    ``impl_version`` is the build of the stage that ran. It defaults to empty
    because the engine's own executions have no stage implementation behind them,
    and because records written before the field existed must still validate.
    """

    pipeline_run_id: str
    parent_execution_id: str | None = None
    stage: str
    impl_version: str = ""
    ordinal: int = 0
    status: ExecutionStatus = ExecutionStatus.PENDING
    correlation_id: str
    started_at: datetime
    completed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


class ExecutionArtifact(_Record):
    """A snapshot consumed or produced by a stage execution.

    One record type covers both directions: an artefact's relationship to an
    execution is the same fact read from either end, and splitting it into two
    tables would duplicate the role/ordinal columns for no gain.
    """

    stage_execution_id: str
    snapshot_id: str
    direction: ArtifactDirection
    role: str = ""
    ordinal: int = 0


class ContextSelection(_Record):
    """What was offered to a model, and under which versioned strategy."""

    stage_execution_id: str
    strategy: str
    strategy_version: str
    token_budget: int | None = None


class ContextItem(_Record):
    """One context candidate and what became of it.

    Excluded and truncated candidates are recorded, not just selected ones: what
    the model *could not see* explains as many surprising outputs as what it did.
    """

    context_selection_id: str
    ordinal: int
    reference: str
    disposition: ContextDisposition
    reason: str = ""
    score: float | None = None


class ModelInvocation(_Record):
    """A single call to a model, including the ones that failed.

    Attempts form a chain via ``parent_invocation_id`` + ``attempt_ordinal``, and
    ``retry_type`` says *why* this attempt exists. There is deliberately no
    retry-count field: a count cannot distinguish rate limiting from a model that
    keeps emitting an invalid enum (plan/03 → retry ordering).

    The raw, parsed and validated responses are three separate snapshot
    references so a useful-but-invalid response survives next to its repair.
    """

    stage_execution_id: str
    parent_invocation_id: str | None = None
    attempt_ordinal: int = 1
    retry_type: RetryType | None = None
    outcome: InvocationOutcome
    provider: str
    model: str
    template_id: str
    template_version: str
    request_snapshot_id: str | None = None
    raw_response_snapshot_id: str | None = None
    parsed_response_snapshot_id: str | None = None
    validated_response_snapshot_id: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class ToolInvocation(_Record):
    """A tool call made during a stage, with everything needed to judge it.

    Both raw and normalised args/results are kept: the raw form is what actually
    crossed the boundary, the normalised form is what the pipeline acted on, and
    a bug can live in the gap between them.
    """

    stage_execution_id: str
    model_invocation_id: str | None = None
    tool_name: str
    tool_version: str
    initiator: ToolInitiator
    approval_required: bool = False
    approved_by: str | None = None
    raw_args: dict[str, Any] = Field(default_factory=dict)
    normalised_args: dict[str, Any] = Field(default_factory=dict)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    normalised_result: dict[str, Any] = Field(default_factory=dict)
    status: ExecutionStatus = ExecutionStatus.PENDING
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class DecisionRecord(_Record):
    """A routing/approval/selection decision and who or what made it.

    ``decided_by`` is mandatory, and a decision attributed to a policy must name
    the policy version — an unattributed or unversioned decision cannot be
    reviewed or reproduced, which is the whole point of recording it.
    """

    stage_execution_id: str
    decision_type: str
    decided_by: str
    decided_by_type: ActorType
    policy_version: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    rationale: str = ""
    decided_at: datetime

    @model_validator(mode="after")
    def _decisions_are_attributable(self) -> Self:
        if not self.decided_by:
            raise ValueError("decided_by is required: an unattributed decision is unreviewable")
        if self.decided_by_type is ActorType.POLICY and not self.policy_version:
            raise ValueError("policy_version is required for decisions made by a policy")
        return self


class EvaluationRun(_Record):
    """A scoring pass over a stage's output, under a versioned rubric.

    Evaluation data is its own record category — linked to the execution that
    produced the scored artefact, but not folded into it (plan/03 → editorial /
    execution / evaluation records stay separate).
    """

    stage_execution_id: str
    evaluator_id: str
    evaluator_version: str
    rubric_version: str
    scores: dict[str, Any] = Field(default_factory=dict)
    passed: bool
    created_at: datetime


class UserIntervention(_Record):
    """A point where a human stepped into the run."""

    stage_execution_id: str
    user_id: str
    intervention_type: InterventionType
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class TraceEvent(_Record):
    """An append-only event in the run's timeline.

    ``correlation_id`` groups the events of one run; ``causation_id`` names the
    event that caused this one, so the timeline can be read as a causal chain
    rather than a flat log. ``sequence`` gives a total order within a correlation
    that does not depend on clock resolution.
    """

    pipeline_run_id: str | None = None
    stage_execution_id: str | None = None
    event_type: str
    timestamp: datetime
    actor_type: ActorType
    actor_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str
    causation_id: str | None = None
    sequence: int = 0


class ExperimentRun(_Record):
    """Shell for the experimentation system; filled in phase 12.

    Present now so provenance records can reference an experiment from the start
    rather than being retrofitted with the link later.
    """

    name: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    created_at: datetime


class Job(_Record):
    """Shell for the DB-backed job queue; filled in phase 09."""

    job_type: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    pipeline_run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
