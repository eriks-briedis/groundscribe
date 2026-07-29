"""What a screen is given to render (phase 11).

plan/11 builds an *artefact-first* interface: a person opens a project and reads
the source model, the brief, the version, the findings, the score sheet and the
trace behind all of them. Phase 09's command envelope answers a different
question — where the run is and what may be done to it — so these are the shapes
the reads return.

Three decisions hold across the file.

**Projections, not entities.** Each model is assembled for one screen out of rows
that already exist. Nothing here is stored, nothing is derived that the domain
does not already know, and a field the run has not produced yet is ``None``
rather than a plausible default: an empty brief and a brief that says nothing are
different facts, and only one of them means "not written yet".

**Documents stay documents.** A brief, a source model, a revision plan and a
score sheet are rendered as the stored JSON, typed as an object. Re-declaring
each of them here would create a second definition of an artefact whose first
definition is the schema that wrote it, and the two would drift on the first
change to either.

**They live in the app layer, not the API layer.** A projection is a view of the
domain, not of HTTP. Keeping them here lets the CLI ask the same questions the
web app does, and stops the API from becoming the only place that knows how to
read the system it serves.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.workflow.states import WorkflowState


class TraceFilter(StrEnum):
    """The trace filters plan/11 names, as a closed vocabulary.

    Closed because a filter the API does not recognise has to be refused rather
    than ignored: a person who asked to see only failed executions and was shown
    everything would draw conclusions from a list they did not request.
    """

    FAILED = "failed"
    SCHEMA_REPAIR = "schema_repair"
    FALLBACK_MODEL = "fallback_model"
    BLOCKING_FINDING = "blocking_finding"
    USER_OVERRIDE = "user_override"
    HIGH_COST = "high_cost"
    LOW_CONFIDENCE_SCORE = "low_confidence_score"
    CONFIDENTIAL_WARNING = "confidential_warning"
    REPEATED_ISSUE = "repeated_issue"


class Lifecycle(StrEnum):
    """What became of one review finding across rounds (plan/11 → *Review history*)."""

    NEW = "new"
    REPEATED = "repeated"
    RESOLVED = "resolved"


class DiffKind(StrEnum):
    """What one line of a version diff is."""

    EQUAL = "equal"
    ADDED = "added"
    REMOVED = "removed"


# ----------------------------------------------------------------------
# Shared pieces
# ----------------------------------------------------------------------


class ActionLink(BaseModel):
    """One offered action, and how a client performs it.

    ``path`` is ``None`` where no endpoint takes the action — the machine's own
    edges, and article actions seen from a project screen where no article is in
    view. Reported rather than filtered out, so the interface can show the true
    set of transitions and offer buttons only for what a person can actually do.
    """

    action: str
    method: str | None = None
    path: str | None = None
    requires_actor: bool = False


class UsageSummary(BaseModel):
    """What a set of model calls consumed.

    ``cost_usd`` stays ``None`` when no call reported one, because zero is the
    claim that the work was free (phase 03 draws the same distinction, and a
    total that flattened it would be the first place it was lost).
    """

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class ScoreView(BaseModel):
    """One scoring pass, as the review-history table shows it."""

    execution_id: str
    overall: float
    passed: bool
    rubric_version: str
    evaluator_version: str
    dimensions: dict[str, float] = Field(default_factory=dict)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class ExecutionRef(BaseModel):
    """The execution behind an artefact, named so a screen can link to it."""

    id: str
    stage: str
    impl_version: str
    ordinal: int
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


class InterventionView(BaseModel):
    """A point where a person stepped in, and what they did."""

    id: str
    intervention_type: str
    user_id: str
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ModelVersionView(BaseModel):
    """Which model answered under which prompt version, per stage.

    An approval that cannot name the prompt behind the prose is not an informed
    one (plan/11 → the approval view lists *model/prompt versions*).
    """

    stage: str
    provider: str
    model: str
    template_id: str
    template_version: str


# ----------------------------------------------------------------------
# Project dashboard
# ----------------------------------------------------------------------


class ProjectSummary(BaseModel):
    id: str
    title: str
    description: str = ""
    author_id: str


class ConstraintsView(BaseModel):
    """The bounds the project publishes under, including who may see the source."""

    audience: str
    platform: str
    depth: str
    target_length_words: int | None = None
    first_person_allowed: bool = True
    allowed_providers: list[str] = Field(default_factory=list)
    confidential_names: list[str] = Field(default_factory=list)
    trace_retention_consent: bool = False


class SourceCompleteness(BaseModel):
    """How much of the source has been turned into structure, counted not guessed."""

    documents: int = 0
    confidential_documents: int = 0
    segments: int = 0
    claims: int = 0
    unresolved_questions: int = 0
    answered_questions: int = 0


class AnswerView(BaseModel):
    text: str
    question: str
    why_it_matters: str = ""
    response_type: str
    answered_by: str = ""
    diff_snapshot_id: str | None = None


class QuestionView(BaseModel):
    """One thing the source does not say, and what became of asking about it."""

    id: str
    question: str
    why_it_matters: str = ""
    description: str = ""
    priority: str
    group: str = ""
    ordinal: int = 0
    surfaced: bool = False
    resolved: bool = False
    answer: AnswerView | None = None
    #: Where an answer to *this* question is posted. Answering is addressed per
    #: question, so the link belongs to the question rather than to the queue.
    answer_path: str | None = None


class JobView(BaseModel):
    id: str
    job_type: str
    status: str
    attempts: int = 0
    created_at: datetime


class FailureView(BaseModel):
    execution_id: str
    stage: str
    error_type: str | None = None
    error_message: str | None = None
    occurred_at: datetime


class ArticleCard(BaseModel):
    """One article as the dashboard lists it."""

    id: str
    title: str
    status: str
    versions: int = 0
    rewrite_rounds: int = 0
    open_findings: int = 0
    latest_score: ScoreView | None = None
    validated: bool | None = None


class ProjectDashboard(BaseModel):
    """plan/11 → *Project dashboard*, assembled from rows and nothing else."""

    project: ProjectSummary
    run_id: str
    state: WorkflowState
    available_actions: list[str] = Field(default_factory=list)
    action_links: list[ActionLink] = Field(default_factory=list)
    pending_command: ActionLink | None = None
    constraints: ConstraintsView
    source: SourceCompleteness
    articles: list[ArticleCard] = Field(default_factory=list)
    questions: list[QuestionView] = Field(default_factory=list)
    active_jobs: list[JobView] = Field(default_factory=list)
    recent_failures: list[FailureView] = Field(default_factory=list)
    usage: UsageSummary


# ----------------------------------------------------------------------
# Source workspace
# ----------------------------------------------------------------------


class SegmentView(BaseModel):
    """One addressable span of the source — what a claim cites."""

    id: str
    ordinal: int
    kind: str
    text: str
    char_start: int
    char_end: int


class DocumentView(BaseModel):
    id: str
    title: str
    source_format: str
    media_type: str
    uri: str | None = None
    confidential: bool = False
    content_hash: str = ""
    created_by_execution_id: str | None = None
    segments: list[SegmentView] = Field(default_factory=list)


class ClaimView(BaseModel):
    id: str
    text: str
    classification: str
    segment_ids: list[str] = Field(default_factory=list)


class SourceProvenance(BaseModel):
    """Where the structured source came from, so a person can open it."""

    source_model_execution_id: str | None = None
    source_model_snapshot_id: str | None = None
    extracted_at: datetime | None = None


class SourceWorkspace(BaseModel):
    """plan/11 → *Source workspace*: sources, structure, and who may see them."""

    documents: list[DocumentView] = Field(default_factory=list)
    claims: list[ClaimView] = Field(default_factory=list)
    unknowns: list[QuestionView] = Field(default_factory=list)
    source_model: dict[str, Any] | None = None
    provider_visibility: ConstraintsView
    provenance: SourceProvenance


class QuestionQueue(BaseModel):
    """plan/11 → *Question queue*."""

    questions: list[QuestionView] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Architecture board
# ----------------------------------------------------------------------


class ConceptView(BaseModel):
    id: str
    title: str
    angle: str = ""
    thesis: str = ""
    ordinal: int = 0


class ArchitectureVersionView(BaseModel):
    id: str
    summary: str
    locked: bool = False
    locked_by: str | None = None
    parent_id: str | None = None
    created_by_execution_id: str | None = None
    concepts: list[ConceptView] = Field(default_factory=list)


class ArchitectureBoard(BaseModel):
    """plan/11 → *Architecture board*, including the versions to compare."""

    current_version_id: str | None = None
    versions: list[ArchitectureVersionView] = Field(default_factory=list)
    proposal: dict[str, Any] | None = None


# ----------------------------------------------------------------------
# Article workspace
# ----------------------------------------------------------------------


class ArticleSummary(BaseModel):
    id: str
    project_id: str
    title: str
    status: str


class VersionView(BaseModel):
    """One stored version of the article, with the prose it holds."""

    id: str
    ordinal: int
    title: str = ""
    thesis: str = ""
    body: str = ""
    snapshot_id: str | None = None
    parent_id: str | None = None
    created_by_execution_id: str | None = None


class DiffLine(BaseModel):
    kind: DiffKind
    text: str


class DiffView(BaseModel):
    """A line-level diff between two versions, computed from the stored bodies.

    Computed here rather than in the browser so the two sides of a comparison are
    the artefacts as stored, not as some component last rendered them.
    """

    added: int = 0
    removed: int = 0
    lines: list[DiffLine] = Field(default_factory=list)


class FindingView(BaseModel):
    """One review finding, with its decision and its history."""

    id: str
    ref: str = ""
    severity: str
    category: str = ""
    location: str = ""
    passage: str = ""
    description: str
    evidence: str = ""
    source_ref: str = ""
    brief_ref: str = ""
    recommended_correction: str = ""
    suggested_route: str = ""
    blocks_publication: bool = False
    reviewer_confidence: float = 0.0
    fingerprint: str = ""
    status: str
    decided_by: str = ""
    decision_reason: str = ""
    lifecycle: Lifecycle = Lifecycle.NEW


class ActiveInstructionView(BaseModel):
    """One voice instruction in force, and where it came from."""

    instruction_id: str
    category: str
    strength: str
    instruction: str = ""
    source: str
    overrides: str = ""


class VoiceView(BaseModel):
    sources: list[str] = Field(default_factory=list)
    active: list[ActiveInstructionView] = Field(default_factory=list)
    suppressed: list[str] = Field(default_factory=list)


class ValidationView(BaseModel):
    passed: bool
    validator_version: str = ""
    checks_run: list[str] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    corrections: list[dict[str, Any]] = Field(default_factory=list)


class LineageNode(BaseModel):
    id: str
    kind: str
    label: str = ""
    ordinal: int = 0
    execution_id: str | None = None


class LineageEdge(BaseModel):
    """One causal link, named ``from``/``to`` because a graph is read that way."""

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    kind: str = "parent"


class LineageGraph(BaseModel):
    """plan/11 → *Lineage graph — branching causal relationships between artefacts*."""

    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)


class ApprovalView(BaseModel):
    """Everything plan/11 requires a person to see before approving.

    Gathered onto the workspace rather than served from its own endpoint: the
    approval screen is the article screen with nothing hidden, and a second
    endpoint would let the two disagree about which version is being approved.
    """

    rewrite_rounds: int = 0
    remaining_concerns: list[str] = Field(default_factory=list)
    interventions: list[InterventionView] = Field(default_factory=list)
    model_versions: list[ModelVersionView] = Field(default_factory=list)
    usage: UsageSummary


class ArticleWorkspace(BaseModel):
    """plan/11 → *Article workspace*, and the approval view built on it."""

    article: ArticleSummary
    run_id: str
    state: WorkflowState
    available_actions: list[str] = Field(default_factory=list)
    action_links: list[ActionLink] = Field(default_factory=list)
    pending_command: ActionLink | None = None
    brief: dict[str, Any] | None = None
    current_version: VersionView | None = None
    previous_version: VersionView | None = None
    diff: DiffView | None = None
    findings: list[FindingView] = Field(default_factory=list)
    revision_plan: dict[str, Any] | None = None
    voice: VoiceView
    scores: list[ScoreView] = Field(default_factory=list)
    validation: ValidationView | None = None
    producing_execution: ExecutionRef | None = None
    lineage: LineageGraph
    approval: ApprovalView


# ----------------------------------------------------------------------
# Review history
# ----------------------------------------------------------------------


class ReviewRound(BaseModel):
    review_id: str
    round: int
    verdict: str
    version_id: str
    version_ordinal: int
    execution_id: str | None = None
    issues: list[FindingView] = Field(default_factory=list)


class ReviewHistory(BaseModel):
    """plan/11 → *Review history*: score progression and what happened to each issue."""

    rounds: list[ReviewRound] = Field(default_factory=list)
    scores: list[ScoreView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Trace and the stage inspector
# ----------------------------------------------------------------------


class TraceExecution(BaseModel):
    """One execution as the timeline lists it, with why a filter matched it."""

    id: str
    stage: str
    impl_version: str = ""
    ordinal: int = 0
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None
    events: int = 0
    invocations: int = 0
    usage: UsageSummary
    matched_filters: list[TraceFilter] = Field(default_factory=list)


class TraceView(BaseModel):
    """plan/11 → *Execution timeline* and *Trace filters*.

    ``filters_available`` is the vocabulary itself, served with every response.
    A client that kept its own list would render controls for filters the
    backend had renamed or removed, and nothing would notice until someone
    ticked one.
    """

    executions: list[TraceExecution] = Field(default_factory=list)
    filters_applied: list[TraceFilter] = Field(default_factory=list)
    filters_available: list[TraceFilter] = Field(default_factory=lambda: list(TraceFilter))


class ArtifactView(BaseModel):
    """A snapshot an execution consumed or produced, with what it holds."""

    snapshot_id: str
    artifact_type: str
    role: str = ""
    direction: str
    ordinal: int = 0
    content_hash: str = ""
    size: int = 0
    content: Any = None


class ContextItemView(BaseModel):
    ordinal: int
    reference: str
    disposition: str
    reason: str = ""
    score: float | None = None


class ContextSelectionView(BaseModel):
    """What was offered to the model, and what was left out."""

    id: str
    strategy: str
    strategy_version: str
    token_budget: int | None = None
    items: list[ContextItemView] = Field(default_factory=list)


class InvocationView(BaseModel):
    """One model call, with every payload phase 03 kept for it."""

    id: str
    parent_invocation_id: str | None = None
    attempt_ordinal: int = 1
    retry_type: str | None = None
    outcome: str
    provider: str
    model: str
    template_id: str
    template_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    effective_request: Any = None
    raw_response: Any = None
    parsed_response: Any = None
    validated_response: Any = None


class ToolCallView(BaseModel):
    id: str
    tool_name: str
    tool_version: str
    initiator: str
    approval_required: bool = False
    approved_by: str | None = None
    status: str
    raw_args: dict[str, Any] = Field(default_factory=dict)
    normalised_args: dict[str, Any] = Field(default_factory=dict)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    normalised_result: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class DecisionView(BaseModel):
    id: str
    decision_type: str
    decided_by: str
    decided_by_type: str
    policy_version: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    rationale: str = ""
    decided_at: datetime


class EvaluationView(BaseModel):
    id: str
    evaluator_id: str
    evaluator_version: str
    rubric_version: str
    passed: bool
    scores: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class EventView(BaseModel):
    id: str
    event_type: str
    timestamp: datetime
    actor_type: str
    actor_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str
    causation_id: str | None = None
    sequence: int


class ErrorView(BaseModel):
    type: str | None = None
    message: str | None = None


class StageInspection(BaseModel):
    """plan/11 → *Stage inspector*: every layer of one execution, in one document."""

    summary: ExecutionRef
    inputs: list[ArtifactView] = Field(default_factory=list)
    outputs: list[ArtifactView] = Field(default_factory=list)
    context_selections: list[ContextSelectionView] = Field(default_factory=list)
    invocations: list[InvocationView] = Field(default_factory=list)
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    decisions: list[DecisionView] = Field(default_factory=list)
    evaluations: list[EvaluationView] = Field(default_factory=list)
    interventions: list[InterventionView] = Field(default_factory=list)
    events: list[EventView] = Field(default_factory=list)
    usage: UsageSummary
    duration_ms: int | None = None
    error: ErrorView | None = None


class ComparisonRow(BaseModel):
    """One field of two executions, side by side."""

    field: str
    left: str | None = None
    right: str | None = None
    same: bool


__all__ = [
    "ActionLink",
    "ActiveInstructionView",
    "AnswerView",
    "ApprovalView",
    "ArchitectureBoard",
    "ArchitectureVersionView",
    "ArticleCard",
    "ArticleSummary",
    "ArticleWorkspace",
    "ArtifactView",
    "ClaimView",
    "ComparisonRow",
    "ConceptView",
    "ConstraintsView",
    "ContextItemView",
    "ContextSelectionView",
    "DecisionView",
    "DiffKind",
    "DiffLine",
    "DiffView",
    "DocumentView",
    "ErrorView",
    "EvaluationView",
    "EventView",
    "ExecutionRef",
    "FailureView",
    "FindingView",
    "InterventionView",
    "InvocationView",
    "JobView",
    "Lifecycle",
    "LineageEdge",
    "LineageGraph",
    "LineageNode",
    "ModelVersionView",
    "ProjectDashboard",
    "ProjectSummary",
    "QuestionQueue",
    "QuestionView",
    "ReviewHistory",
    "ReviewRound",
    "ScoreView",
    "SegmentView",
    "SourceCompleteness",
    "SourceProvenance",
    "SourceWorkspace",
    "StageInspection",
    "ToolCallView",
    "TraceExecution",
    "TraceFilter",
    "TraceView",
    "UsageSummary",
    "ValidationView",
    "VersionView",
    "VoiceView",
]
