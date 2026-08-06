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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from groundscribe.app.views import ComparisonRow
from groundscribe.domain.enums import AnswerResponse, FindingStatus, SourceFormat
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.experiments.metrics import ArmMetrics
from groundscribe.experiments.variables import ForkVariables
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


class Escalate(ActorAction):
    """Taking one of the ways out of a run the loop could not finish.

    ``reason`` is optional to the schema and not to the point. These are the
    decisions a person makes after the machine has said it cannot finish, and
    every one of them costs something real — another round, a rewritten brief, a
    reopened architecture. The reason travels into the transition's rationale, so
    the decision record answers *why* and not only *what*.
    """

    reason: str = ""


class ReviseArticle(ActorAction):
    """Asking the policy to route a failed score, optionally naming which way.

    ``prefer`` chooses between the destinations the failure's category already
    permits and cannot invent one — the policy refuses a state it does not list.
    It matters because the right answer differs while the category does not: a
    factual gap whose facts the author has is corrected by re-extracting, and one
    whose facts nobody has written down is corrected by asking them.
    """

    prefer: WorkflowState | None = None


class DecideFinding(ActorAction):
    """What the author decided about one review finding (plan/07 §8).

    Three decisions, because they are the three a person makes. ``proposed`` is
    where a finding starts and ``suppressed`` is the system holding one back;
    neither is chosen, so neither is offered.

    ``reason`` is what a rejection is judged by next round — the ledger refuses one
    without it, because a dismissal with no reason cannot be told from an oversight
    at exactly the moment that matters.
    """

    decision: Literal[FindingStatus.ACCEPTED, FindingStatus.REJECTED, FindingStatus.EDITED]
    reason: str = ""
    #: What the author would have the rewrite do instead, for an ``edited`` finding.
    recommended_correction: str = ""


class FindingVerdict(BaseModel):
    """One decision inside a triage submission, naming the finding it is about.

    The same three fields :class:`DecideFinding` carries, plus the id — which the
    single-finding endpoint takes from its URL and a batch cannot.
    """

    finding_id: str = Field(min_length=1)
    decision: Literal[FindingStatus.ACCEPTED, FindingStatus.REJECTED, FindingStatus.EDITED]
    reason: str = ""
    recommended_correction: str = ""


class TriageReview(ActorAction):
    """Every decision a person made about a review, handed over together.

    Triage is the pipeline's slowest human step, and it was priced per finding: a
    request, a stage execution and a screen reload each. One run recorded 34 of
    them. This is the same set of decisions as one submission.

    The batch is applied whole or not at all. Per-finding requests made partial
    application the norm — an author who mistyped the seventh had already
    committed six, and the ledger keeps a decision rather than letting it be
    taken back.
    """

    decisions: list[FindingVerdict] = Field(min_length=1)


class ContinueToArticle(ActorAction):
    """Approving one article and naming the next to write (phase 16).

    The next article is named rather than inferred: auto-advance follows the one
    the architecture selected, which is the article being finished here, so
    anything inferred would restart what the author is leaving behind.
    """

    next_article_id: str = Field(min_length=1)


class BuildDataset(BaseModel):
    """Building an evaluation corpus out of approved work (plan/12)."""

    name: str
    created_by: str
    description: str = ""
    #: Sensitive projects the caller is explicitly letting in, by id. Never a
    #: flag meaning "all of them": that is a decision made once, in a hurry,
    #: about projects that do not exist yet.
    include_sensitive: list[str] = Field(default_factory=list)


class DatasetEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ordinal: int
    project_id: str
    label: str
    stage_execution_id: str
    reference_snapshot_id: str


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    created_by: str
    created_at: datetime
    sensitive_included: list[str] = Field(default_factory=list)
    entries: list[DatasetEntryOut] = Field(default_factory=list)


class ArmIn(BaseModel):
    """One configuration to put under test."""

    label: str
    baseline: bool = False
    variables: ForkVariables = Field(default_factory=ForkVariables)


class CreateExperiment(BaseModel):
    name: str
    dataset_id: str
    created_by: str
    description: str = ""
    arms: list[ArmIn] = Field(default_factory=list)


class ArmOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    baseline: bool
    ordinal: int
    variables: dict[str, Any] = Field(default_factory=dict)


class ExperimentResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    arm_id: str
    entry_id: str
    job_id: str | None = None
    stage_execution_id: str | None = None
    status: ExecutionStatus
    error_message: str | None = None


class RecordPreference(BaseModel):
    """A person saying which arm did better on one example."""

    entry_id: str
    arm_id: str
    decided_by: str
    reason: str = ""


class PreferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entry_id: str
    preferred_arm_id: str
    decided_by: str
    reason: str


class GuaranteeOut(BaseModel):
    """One clause of the reproducibility contract (plan/12).

    Served rather than documented, because the question it answers is asked
    while looking at two executions — not while reading a README.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    title: str
    detail: str
    promised: bool
    evidence: str


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
    #: What the rule says, and what it forbids literally. Without these a screen
    #: can only list identifiers, which tells a reader a rule exists and nothing
    #: about what their prose is being held to.
    text: str = ""
    rationale: str = ""
    prohibits: str = ""
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
    named one per row, and how far the two outputs sit apart.

    ``reproducibility`` travels with the comparison because plan/12's named risk
    is a misleading reproducibility claim, and this is the screen where one gets
    made: two outputs differing is a fact, and what that difference *proves*
    depends on a contract the reader has to be holding at the time.
    """

    left: ExecutionSummary
    right: ExecutionSummary
    differences: list[ComparisonRow] = Field(default_factory=list)
    output_edit_distance: int | None = None
    reproducibility: list[GuaranteeOut] = Field(default_factory=list)


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str = ""
    status: ExecutionStatus
    dataset_id: str | None = None
    created_by: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    arms: list[ArmOut] = Field(default_factory=list)


class ExperimentReportOut(BaseModel):
    """One experiment, its per-example results, and the aggregate table.

    All three, because an aggregate a reader cannot open into the runs behind it
    is a summary they have to take on trust — which is what this whole phase
    exists to avoid.
    """

    experiment: ExperimentOut
    results: list[ExperimentResultOut] = Field(default_factory=list)
    comparison: list[ArmMetrics] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Privacy and export (phase 13)
# ----------------------------------------------------------------------


class ExportedArticleOut(BaseModel):
    """One article version rendered in a named format.

    The version id and content hash travel with the content because an exported
    file otherwise loses its provenance at the moment it leaves the system: a
    Markdown file on a desktop cannot say which run produced it.
    """

    model_config = ConfigDict(from_attributes=True)

    version_id: str
    content_hash: str
    format: str
    media_type: str
    content: str


class StageVisibilityOut(BaseModel):
    """Where one stage's material goes."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    stage: str
    provider: str
    model: str
    local: bool
    permitted: bool
    fallback_provider: str | None = None
    fallback_model: str | None = None


class ProviderVisibilityOut(BaseModel):
    """plan/13's data-flow surface: counts and routes, never content.

    A screen that displayed the confidential passages in order to warn about
    them would be the leak it was drawn to prevent, so nothing here carries
    source text.
    """

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    stages: list[StageVisibilityOut]
    routing_version: str
    confidential_segments: int
    internal_segments: int
    segments_sent: int
    segments_withheld: int
    retention_mode: str
    trace_preserves: list[str]
    leaves_this_machine: bool
    has_confidential_material: bool


class SetRoutingProfile(ActorAction):
    """Move a project onto a routing profile, or back onto the default.

    ``null`` is the default, and is meaningfully different from omitting the
    field — so it is required. A screen that left it out because the person
    cleared the box would otherwise be asking for no change at all.
    """

    profile: str | None


class TraceExportOut(BaseModel):
    """One project's execution records, with what was withheld from them."""

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    sanitised: bool
    warnings: list[str]
    withheld_payloads: int
    runs: list[dict[str, Any]]


class TraceDeletionOut(BaseModel):
    """What a deletion removed, so an irreversible act can be checked."""

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    payloads: int
    bytes_reclaimed: int
    records_kept: int
    shared_payloads: int


__all__ = [
    "ActiveInstructionOut",
    "ActorAction",
    "AnswerGap",
    "ApproveSuggestion",
    "ArmIn",
    "ArmOut",
    "BuildDataset",
    "CommandResponse",
    "CreateExperiment",
    "CreateProject",
    "DatasetEntryOut",
    "DatasetOut",
    "EffectiveVoice",
    "ExecutionComparison",
    "ExecutionSummary",
    "ExperimentOut",
    "ExperimentReportOut",
    "ExperimentResultOut",
    "ExportedArticleOut",
    "ExtractSourceModel",
    "GuaranteeOut",
    "ImportSource",
    "ModelInvocationOut",
    "PreferenceOut",
    "ProviderVisibilityOut",
    "RecordPreference",
    "RejectSuggestion",
    "SetRoutingProfile",
    "StageVisibilityOut",
    "TraceDeletionOut",
    "TraceEventOut",
    "TraceExportOut",
    "UpdateArchitecture",
    "VoiceProfileSummary",
    "VoiceSuggestionOut",
]
