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

    ``taken_by`` is the other half of that, and the two are independent: an edge
    with no path may still be a person's to take, somewhere else in the
    application. Without it an interface has to guess, and the natural guess —
    "no button, so the pipeline must be doing it" — is wrong exactly where it
    matters, on the edges a run is parked waiting for.
    """

    action: str
    method: str | None = None
    path: str | None = None
    requires_actor: bool = False
    #: ``"you"`` or ``"pipeline"``, from the transition table's own actor.
    taken_by: str = "pipeline"


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


class ScoreConfidenceView(BaseModel):
    """How far the repeat passes of one scoring run sat apart.

    plan/08 runs a score more than once precisely so disagreement is visible;
    dropping the spread here would leave the interface showing a single number
    with nothing to doubt it by.
    """

    repeats: int = 1
    repeat_scores: list[float] = Field(default_factory=list)
    dispersion: float | None = None
    stdev: float | None = None


class ScoreView(BaseModel):
    """One scoring pass, as the review-history table shows it."""

    execution_id: str
    overall: float
    passed: bool
    rubric_version: str
    evaluator_version: str
    dimensions: dict[str, float] = Field(default_factory=dict)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    confidence: ScoreConfidenceView = Field(default_factory=ScoreConfidenceView)
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
    #: Where to run this stage again (phase 16).
    #:
    #: A link rather than a path the interface assembles, for the reason every
    #: other command is: plan/11 forbids the frontend from addressing commands
    #: itself, and a rerun button that built its own URL would be the first place
    #: that rule was broken. Always offered — a replay takes no workflow edge and
    #: moves no run, so there is no state in which it is unavailable.
    rerun_command: ActionLink | None = None
    #: Where to run it again with one thing changed (phase 16).
    #:
    #: Its own link rather than a flag on the one above, because they are
    #: different requests: a replay carries no body beyond the actor, and a fork
    #: carries a closed vocabulary of variables the backend validates. Offering
    #: one link for both would mean the interface deciding which it was.
    fork_command: ActionLink | None = None
    #: Whether the run will act on what a rerun produces.
    #:
    #: False on a finished run, and the difference matters enough to say: a
    #: replay never moves the run — that is what makes it safe to offer at all —
    #: so on a terminal one it writes a version nothing will ever score, validate
    #: or approve. The version is real and can be read and exported; it is simply
    #: not going anywhere, and a button that reads identically in both cases
    #: promises something it cannot do.
    #:
    #: Answered here rather than by the interface reading the state, because
    #: plan/11 forbids the frontend from branching on a workflow state and a test
    #: enforces it. Which states are terminal is the machine's to know.
    rerun_feeds_pipeline: bool = True


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
    #: Which routing policy this project's stages run against (phase 15).
    #: ``None`` is the shipped default, and is the honest answer rather than a
    #: name — the default file's identity is that it has none, and inventing one
    #: here would make "default" look like a profile somebody chose.
    routing_profile: str | None = None


class ProjectCard(BaseModel):
    """One project as the way-in lists it.

    Enough to choose between projects and nothing that needs a second query per
    row: a list that cost a dashboard's worth of work per project would be the
    slowest screen in the application and the first one anybody sees.
    """

    id: str
    title: str
    description: str = ""
    author_id: str
    run_id: str
    state: WorkflowState
    articles: int = 0
    opened_at: datetime


class ProjectIndex(BaseModel):
    """Every project this installation holds, newest first."""

    projects: list[ProjectCard] = Field(default_factory=list)


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
    #: Whether the run starts pipeline-owned work by itself (phase 16). On this
    #: view because a screen showing a run that is moving on its own has to be
    #: able to say *why* nobody is being asked to press anything.
    auto_advance: bool = True


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


class JourneyStep(BaseModel):
    """One phase of the pipeline, seen from where the run currently is."""

    id: str
    title: str
    blurb: str
    #: ``"done"``, ``"current"`` or ``"upcoming"``.
    status: str


class ProjectJourney(BaseModel):
    """How far the work has got, at the size a person follows it.

    Published rather than left to the interface: which phase a state belongs to,
    and who a run is waiting for, are facts about the machine (plan/05). A screen
    that grouped twenty-three states into eight phases of its own would be the
    second opinion plan/11 forbids — and would be wrong the day a state is added.
    """

    steps: list[JourneyStep] = Field(default_factory=list)
    #: What is happening now, in a sentence written for a person.
    headline: str = ""
    #: ``"you"``, ``"pipeline"`` or ``"nobody"``.
    waiting_on: str = "pipeline"


class RoutingProfilesView(BaseModel):
    """Which routing policy a project runs against, and what else it could.

    ``selected`` is ``None`` for the shipped default, and the default is absent
    from ``available``: it is what not choosing means, and listing it beside the
    named profiles would make "the default" and "openai" look like the same kind
    of answer when one of them is currently the other.

    Carried on the dashboard rather than served from its own read, for the reason
    every other screen is fed by one composed read: a screen that assembled
    itself from four GETs would have four chances to be half-loaded, and this is
    the one panel whose whole job is to state a fact accurately.
    """

    selected: str | None = None
    available: list[str] = Field(default_factory=list)
    #: The version string of the policy actually in force, so a screen can say
    #: what is running without loading the file again and disagreeing.
    policy_version: str = ""
    #: How to change it, addressed by the backend like every other command.
    command: ActionLink | None = None


class PrivacyView(BaseModel):
    """What can be done with this project's trace, addressed by the backend.

    Two commands rather than one, because they are opposite acts with opposite
    risks: exporting produces bytes that leave the machine, and deleting destroys
    payloads that cannot be recovered. Both were reachable only from the API
    until phase 16 — the privacy capability existed in the code and not in the
    product, which is the same as not having it.

    ``holds_confidential`` is here so a screen can warn *before* the refusal
    rather than only after it. The backend still refuses a full export of
    confidential material without an explicit acknowledgement (plan/13); this
    lets the interface say what is about to happen instead of surprising someone
    with a 409.
    """

    holds_confidential: bool = False
    retention_mode: str = ""
    export_command: ActionLink | None = None
    delete_command: ActionLink | None = None


class ProjectDashboard(BaseModel):
    """plan/11 → *Project dashboard*, assembled from rows and nothing else."""

    project: ProjectSummary
    run_id: str
    state: WorkflowState
    journey: ProjectJourney
    available_actions: list[str] = Field(default_factory=list)
    action_links: list[ActionLink] = Field(default_factory=list)
    pending_command: ActionLink | None = None
    #: Present only when a job failed and nothing is queued in its place — which
    #: is the one situation a run cannot leave on its own. Absent the rest of the
    #: time, so an interface never offers to re-run work that is already coming.
    retry_command: ActionLink | None = None
    constraints: ConstraintsView
    routing: RoutingProfilesView
    privacy: PrivacyView
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
    #: Where material is added. Ingestion answers to no workflow action — nothing
    #: about a run's state makes it legal or illegal — so it would otherwise be
    #: the one command a client had to address on its own.
    import_command: ActionLink | None = None


class QuestionQueue(BaseModel):
    """plan/11 → *Question queue*."""

    questions: list[QuestionView] = Field(default_factory=list)
    #: Where the answered round is handed back, and ``None`` when the run is not
    #: taking answers. One command for the round rather than one per answer: the
    #: rebuild reads every answer on record, so it is worth exactly one model
    #: call however many questions the author got through.
    submit: ActionLink | None = None


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
    """plan/11 → *Architecture board*, including the versions to compare.

    ``operations``, ``edit_command`` and ``approve_command`` are served for the
    reason ``ActionLink`` is: the seven edits are phase 06's closed vocabulary,
    and a client holding its own copy of the list or of the URL would be a second
    definition of the override API.
    """

    current_version_id: str | None = None
    versions: list[ArchitectureVersionView] = Field(default_factory=list)
    proposal: dict[str, Any] | None = None
    operations: list[str] = Field(default_factory=list)
    edit_command: ActionLink | None = None
    approve_command: ActionLink | None = None


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
    #: Where to post the author's decision, supplied rather than constructed.
    #:
    #: Present only while the finding is undecided: the ledger keeps a decision
    #: on the record rather than letting it be taken back, so an interface that
    #: offered the control again would be offering something the backend refuses.
    decide_command: ActionLink | None = None


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
    #: The project's other approved concepts, and whether anything was written
    #: for each (phase 16).
    #:
    #: Here because this is the screen where approving happens, and approving is
    #: where the author decides whether the project is finished or whether one of
    #: the others is worth writing. A screen that had to fetch the dashboard to
    #: populate that choice would be assembling itself from two reads, which is
    #: the thing one composed read per screen exists to avoid.
    siblings: list[ArticleCard] = Field(default_factory=list)
    #: Where a refused score is sent back to be corrected, and where this article
    #: is published while starting another (phase 16).
    #:
    #: Their own fields rather than names the interface picks out of
    #: ``action_links``, because a screen that matched on an action name would be
    #: deciding which action it was looking at — the thing plan/11 forbids and
    #: ``guards.test.ts`` enforces. Both need a control of their own: one offers a
    #: choice between the destinations a category permits, the other needs a
    #: second article id no action bar can supply.
    revise_command: ActionLink | None = None
    continue_command: ActionLink | None = None
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
    #: The claims the brief was written from, beside the prose written from them.
    source_evidence: list[ClaimView] = Field(default_factory=list)
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
    "ProjectCard",
    "ProjectDashboard",
    "ProjectIndex",
    "ProjectSummary",
    "QuestionQueue",
    "QuestionView",
    "ReviewHistory",
    "ReviewRound",
    "RoutingProfilesView",
    "ScoreConfidenceView",
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
