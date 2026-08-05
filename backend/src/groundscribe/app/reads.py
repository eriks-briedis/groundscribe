"""Assembling what the screens read (phase 11).

plan/11 → the frontend *displays backend state and submits commands; it never
re-implements pipeline-transition rules*. That only works if the backend can
answer what a screen needs in one question, so this module answers those
questions — one method per screen, each of them a query and an arrangement.

Three rules hold everywhere here, and they are the reason this is its own module
rather than more methods on :class:`~groundscribe.app.services.ApplicationService`:

1. **A read changes nothing.** No transition is applied, no job is queued, no
   model is called, and no engine is resumed — resuming one opens a workflow
   execution, and a ``GET`` that wrote provenance would make reading the system
   part of its history.
2. **Nothing is derived that the domain already decides.** ``available_actions``
   comes from the transition table; a score's verdict comes from the rubric; a
   version's lineage comes from its parent link. Where a projection appears to
   compute something — a diff, a total, an issue's lifecycle — it is arranging
   stored facts, never judging them.
3. **Absence is reported, not filled in.** A run that has not produced a brief
   gets ``None``, so the interface can say "not written yet" instead of showing
   an empty document that looks like one.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.app import rehydrate
from groundscribe.app.actions import (
    ACTION_ENDPOINTS,
    RETRY_FAILED,
    STATE_COMMANDS,
    SUBMIT_ANSWERS,
    available_actions,
    resolve,
)
from groundscribe.app.runtime import Runtime
from groundscribe.app.views import (
    ActionLink,
    ActiveInstructionView,
    AnswerView,
    ApprovalView,
    ArchitectureBoard,
    ArchitectureVersionView,
    ArticleCard,
    ArticleSummary,
    ArticleWorkspace,
    ArtifactView,
    ClaimView,
    ComparisonRow,
    ConceptView,
    ConstraintsView,
    ContextItemView,
    ContextSelectionView,
    DecisionView,
    DiffKind,
    DiffLine,
    DiffView,
    DocumentView,
    ErrorView,
    EvaluationView,
    EventView,
    ExecutionRef,
    FailureView,
    FindingView,
    InterventionView,
    InvocationView,
    JobView,
    JourneyStep,
    Lifecycle,
    LineageEdge,
    LineageGraph,
    LineageNode,
    ModelVersionView,
    PrivacyView,
    ProjectCard,
    ProjectDashboard,
    ProjectIndex,
    ProjectJourney,
    ProjectSummary,
    QuestionQueue,
    QuestionView,
    ReviewHistory,
    ReviewRound,
    RoutingProfilesView,
    ScoreConfidenceView,
    ScoreView,
    SegmentView,
    SourceCompleteness,
    SourceProvenance,
    SourceWorkspace,
    StageInspection,
    ToolCallView,
    TraceExecution,
    TraceFilter,
    TraceView,
    UsageSummary,
    ValidationView,
    VersionView,
    VoiceView,
)
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType, FindingStatus
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.jobs.enums import JobStatus
from groundscribe.jobs.models import Job
from groundscribe.llm.routing import RoutingConfigError, available_profiles, routing_policy
from groundscribe.privacy.material import restricted_spans
from groundscribe.provenance import models
from groundscribe.provenance.enums import (
    ExecutionStatus,
    InterventionType,
    InvocationOutcome,
    RetryType,
)
from groundscribe.provenance.redaction import PLACEHOLDER
from groundscribe.scoring.scoring import SCORE_STAGE
from groundscribe.stages.override import OverrideOperation
from groundscribe.stages.rewriting import REWRITE_STAGE
from groundscribe.voice.store import VoiceStore
from groundscribe.workflow.journey import STATE_HEADLINES, journey_of, waiting_on
from groundscribe.workflow.position import WorkflowPosition
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from groundscribe.workflow.transitions import TERMINAL_STATES, is_taken_by_user

#: What counts as an expensive call, in dollars. A threshold has to be a number
#: somewhere; it is here, named, rather than inside the filter that uses it, so a
#: deployment that disagrees can see what it is disagreeing with.
HIGH_COST_USD = 0.05

#: How far two repeat scores of the same article may sit apart before the score
#: is worth doubting. plan/08 records the dispersion precisely so this question
#: can be asked of it (multi-model scoring exists to make disagreement visible).
LOW_CONFIDENCE_DISPERSION = 5.0

#: How many failures the dashboard carries. It is a summary; the trace screen is
#: where a person goes for all of them.
RECENT_FAILURES = 5

#: The marker redaction leaves behind. Its presence in a stored payload is the
#: evidence that something confidential reached the boundary and was removed.
REDACTION_MARKER = PLACEHOLDER.split("{", 1)[0]


class UnknownArtefact(LookupError):
    """Asked to show something that does not exist.

    Its own type rather than a bare ``LookupError`` so the API can answer 404
    without catching everything a query might raise.
    """


class ProjectionReader:
    """Every read the interface makes, over one session."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    @property
    def _session(self) -> Session:
        """The session, reached through the runtime rather than copied from it.

        Copied at construction, a reader would need a database the moment it was
        built — including where it is built and never asked anything, which is
        every request that only compares two executions it was handed.
        """
        return self._runtime.session

    # ------------------------------------------------------------------
    # Project dashboard
    # ------------------------------------------------------------------

    def projects(self) -> ProjectIndex:
        """Every project, newest first — the screen the application opens on.

        Ordered by when the run was opened rather than by id, because an id is a
        uuid and ordering by one is no order at all. A project whose run has no
        recorded position is listed without a state rather than omitted: it
        exists, and hiding it would make a broken project invisible instead of
        visible and odd.
        """
        cards: list[ProjectCard] = []
        for project in self._session.scalars(
            select(domain_models.Project).order_by(domain_models.Project.id)
        ):
            run = self._session.scalars(
                select(models.PipelineRun)
                .where(models.PipelineRun.project_id == project.id)
                .order_by(models.PipelineRun.started_at.desc(), models.PipelineRun.id.desc())
            ).first()
            if run is None:
                continue
            position = self._runtime.positions.load(run)
            if position is None:
                continue
            cards.append(
                ProjectCard(
                    id=project.id,
                    title=project.title,
                    description=project.description,
                    author_id=project.user_id,
                    run_id=run.id,
                    state=position.state,
                    articles=len(self._articles(project.id)),
                    opened_at=run.started_at,
                )
            )
        return ProjectIndex(projects=sorted(cards, key=lambda card: card.opened_at, reverse=True))

    def dashboard(self, project_id: str) -> ProjectDashboard:
        """plan/11 → *Project dashboard*."""
        project = self._project(project_id)
        run = self._run(project_id)
        position = self._position(run)
        constraints = rehydrate.constraints_row(self._session, project_id)
        questions = self._questions(project_id)

        return ProjectDashboard(
            project=ProjectSummary(
                id=project.id,
                title=project.title,
                description=project.description,
                author_id=project.user_id,
                routing_profile=project.routing_profile,
            ),
            run_id=run.id,
            state=position.state,
            journey=_journey(position.state),
            available_actions=list(available_actions(position.state)),
            action_links=_action_links(position.state, project_id=project_id, article_id=None),
            pending_command=_pending_command(
                position.state, project_id=project_id, article_id=None
            ),
            retry_command=self._retry_command(run, project_id),
            constraints=_constraints_view(constraints),
            routing=_routing_view(project),
            privacy=self._privacy_view(project_id, constraints),
            source=self._completeness(project_id, questions),
            articles=[self._card(article) for article in self._articles(project_id)],
            questions=[question for question in questions if not question.resolved],
            active_jobs=[_job_view(job) for job in self._active_jobs(run)],
            recent_failures=[
                FailureView(
                    execution_id=execution.id,
                    stage=execution.stage,
                    error_type=execution.error_type,
                    error_message=execution.error_message,
                    occurred_at=execution.completed_at or execution.started_at,
                )
                for execution in self._executions(run)
                if execution.status is ExecutionStatus.FAILED
            ][-RECENT_FAILURES:],
            usage=_usage(self._invocations(run)),
        )

    # ------------------------------------------------------------------
    # Source workspace and the question queue
    # ------------------------------------------------------------------

    def source_workspace(self, project_id: str) -> SourceWorkspace:
        """plan/11 → *Source workspace*."""
        self._project(project_id)
        run = self._run(project_id)
        constraints = rehydrate.constraints_row(self._session, project_id)
        snapshot = rehydrate.latest_snapshot(self._session, run, ArtifactType.SOURCE_MODEL)
        producer = self._producer_of(snapshot)

        return SourceWorkspace(
            documents=[
                DocumentView(
                    id=document.id,
                    title=document.title,
                    source_format=document.source_format.value,
                    media_type=document.media_type,
                    uri=document.uri,
                    confidential=document.confidential,
                    content_hash=document.content_hash,
                    created_by_execution_id=document.created_by_execution_id,
                    segments=[
                        SegmentView(
                            id=segment.id,
                            ordinal=segment.ordinal,
                            kind=segment.kind.value,
                            text=segment.text,
                            char_start=segment.char_start,
                            char_end=segment.char_end,
                        )
                        for segment in self._segments(document.id)
                    ],
                )
                for document in self._documents(project_id)
            ],
            claims=self._claims(project_id),
            unknowns=[
                question for question in self._questions(project_id) if not question.resolved
            ],
            source_model=self._document(snapshot),
            provider_visibility=_constraints_view(constraints),
            import_command=ActionLink(
                action="import_source",
                method="POST",
                path=f"/projects/{project_id}/sources",
            ),
            provenance=SourceProvenance(
                source_model_execution_id=producer.id if producer else None,
                source_model_snapshot_id=snapshot.id if snapshot else None,
                extracted_at=producer.completed_at if producer else None,
            ),
        )

    def questions(self, project_id: str) -> QuestionQueue:
        """plan/11 → *Question queue*, answered ones included.

        Answered questions stay: plan/07's rule that resolved criticism remains
        visible is the same rule, and a queue that dropped what it had settled
        would hide the reasoning behind the source model it produced.
        """
        self._project(project_id)
        state = self._position(self._run(project_id)).state
        return QuestionQueue(
            questions=self._questions(project_id),
            submit=(
                ActionLink(
                    action=WorkflowAction.ANSWER_QUESTIONS.value,
                    method=SUBMIT_ANSWERS.method,
                    path=resolve(SUBMIT_ANSWERS, project_id=project_id, article_id=None),
                    requires_actor=SUBMIT_ANSWERS.requires_actor,
                    taken_by="you",
                )
                if WorkflowAction.ANSWER_QUESTIONS.value in available_actions(state)
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Architecture board
    # ------------------------------------------------------------------

    def architecture(self, project_id: str) -> ArchitectureBoard:
        """plan/11 → *Architecture board*, every version of it."""
        self._project(project_id)
        actions = available_actions(self._position(self._run(project_id)).state)
        versions = list(
            self._session.scalars(
                select(domain_models.ContentArchitecture)
                .where(domain_models.ContentArchitecture.project_id == project_id)
                .order_by(domain_models.ContentArchitecture.id)
            )
        )
        current = versions[-1] if versions else None
        return ArchitectureBoard(
            current_version_id=current.id if current else None,
            versions=[
                ArchitectureVersionView(
                    id=version.id,
                    summary=version.summary,
                    locked=version.locked,
                    locked_by=version.locked_by,
                    parent_id=version.parent_id,
                    created_by_execution_id=version.created_by_execution_id,
                    concepts=[
                        ConceptView(
                            id=concept.id,
                            title=concept.title,
                            angle=concept.angle,
                            thesis=concept.thesis,
                            ordinal=concept.ordinal,
                        )
                        for concept in self._concepts(version.id)
                    ],
                )
                for version in versions
            ],
            proposal=self._document(current.snapshot if current else None),
            operations=[operation.value for operation in OverrideOperation],
            edit_command=(
                _architecture_command("PUT", project_id, current.id if current else None, "")
                if _may_edit_architecture(actions)
                else None
            ),
            approve_command=(
                _architecture_command(
                    "POST", project_id, current.id if current else None, "/approve", actor=True
                )
                if WorkflowAction.APPROVE_ARCHITECTURE.value in actions
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Article workspace
    # ------------------------------------------------------------------

    def article_workspace(self, article_id: str) -> ArticleWorkspace:
        """plan/11 → *Article workspace*, and the approval view built on it."""
        article = self._article(article_id)
        run = self._run(article.project_id)
        position = self._position(run)
        versions = self._versions(article_id)
        current = versions[-1] if versions else None
        previous = versions[-2] if len(versions) > 1 else None
        review = self._latest_review(versions)
        plan = self._latest_plan(versions)
        scores = self._scores(run, article_id)
        validation = self._validation(versions)
        findings = [_finding_view(issue) for issue in review.issues] if review else []

        current_view = self._version_view(current) if current else None
        previous_view = self._version_view(previous) if previous else None

        return ArticleWorkspace(
            article=ArticleSummary(
                id=article.id,
                project_id=article.project_id,
                title=article.title,
                status=article.status,
            ),
            siblings=[
                self._card(other)
                for other in self._articles(article.project_id)
                if other.id != article.id
            ],
            revise_command=_offered(
                WorkflowAction.ROUTE_REVISION, position.state, article_id=article_id
            ),
            continue_command=_offered(
                WorkflowAction.APPROVE_AND_CONTINUE, position.state, article_id=article_id
            ),
            run_id=run.id,
            state=position.state,
            available_actions=list(available_actions(position.state)),
            action_links=_action_links(
                position.state, project_id=article.project_id, article_id=article_id
            ),
            pending_command=_pending_command(
                position.state, project_id=article.project_id, article_id=article_id
            ),
            brief=self._document(self._brief_snapshot(article_id)),
            current_version=current_view,
            previous_version=previous_view,
            diff=(
                _diff(previous_view.body, current_view.body)
                if current_view and previous_view
                else None
            ),
            findings=findings,
            revision_plan=self._document(plan.snapshot if plan else None),
            source_evidence=self._evidence_for(article.project_id, article_id, findings),
            voice=self._voice(article.project_id, article_id),
            scores=scores,
            validation=validation,
            producing_execution=self._execution_ref(
                current.created_by_execution_id if current else None,
                live=position.state not in TERMINAL_STATES,
            ),
            lineage=self.lineage(article_id),
            approval=ApprovalView(
                rewrite_rounds=self._rewrites(run),
                remaining_concerns=_remaining_concerns(findings, validation),
                interventions=[
                    _intervention_view(intervention)
                    for execution in self._executions(run)
                    for intervention in execution.user_interventions
                ],
                model_versions=_model_versions(self._executions(run)),
                usage=_usage(self._invocations(run)),
            ),
        )

    def review_history(self, article_id: str) -> ReviewHistory:
        """plan/11 → *Review history*: the rounds, and what each finding did.

        A finding is ``repeated`` when its fingerprint was raised in an earlier
        round — phase 07 computes that fingerprint from what the finding says and
        the evidence behind it, precisely so "the same finding again" survives a
        reviewer renumbering its ids.
        """
        article = self._article(article_id)
        run = self._run(article.project_id)
        versions = self._versions(article_id)
        by_version = {version.id: version for version in versions}
        seen: set[str] = set()
        rounds: list[ReviewRound] = []

        for review in self._reviews(versions):
            issues: list[FindingView] = []
            for issue in review.issues:
                view = _finding_view(issue)
                view.lifecycle = _lifecycle(issue, seen)
                issues.append(view)
            seen.update(issue.fingerprint for issue in review.issues if issue.fingerprint)
            version = by_version[review.article_version_id]
            rounds.append(
                ReviewRound(
                    review_id=review.id,
                    round=review.round,
                    verdict=review.verdict,
                    version_id=version.id,
                    version_ordinal=version.ordinal,
                    execution_id=review.created_by_execution_id,
                    issues=issues,
                )
            )

        return ReviewHistory(
            rounds=rounds,
            scores=self._scores(run, article_id),
            warnings=self._stagnation_warnings(run),
        )

    def lineage(self, article_id: str) -> LineageGraph:
        """plan/11 → *Lineage graph*: how each version came from the last."""
        self._article(article_id)
        versions = self._versions(article_id)
        known = {version.id for version in versions}
        return LineageGraph(
            nodes=[
                LineageNode(
                    id=version.id,
                    kind="article_version",
                    label=f"v{version.ordinal}",
                    ordinal=version.ordinal,
                    execution_id=version.created_by_execution_id,
                )
                for version in versions
            ],
            edges=[
                LineageEdge(source=parent, target=version.id, kind="supersedes")
                for version in versions
                for parent in [version.parent_id]
                if parent is not None and parent in known
            ],
        )

    # ------------------------------------------------------------------
    # Trace, inspector and comparison
    # ------------------------------------------------------------------

    def trace(self, project_id: str, *, filters: Sequence[TraceFilter] = ()) -> TraceView:
        """plan/11 → *Execution timeline* and *Trace filters*.

        An execution is listed with the filters it matched rather than merely
        because it matched: a person looking at a filtered list needs to see
        *why* each row is there, especially when two filters are applied at once.

        Newest first, which is the opposite of every other list here and the
        right way round for this one: the timeline is read while a run is moving,
        and what a person came to see is the stage that just ran. Ascending puts
        it below however many hundred rows the run has accumulated.
        """
        self._project(project_id)
        run = self._run(project_id)
        wanted = list(dict.fromkeys(filters))

        rows: list[TraceExecution] = []
        for execution in reversed(self._executions(run)):
            matched = [item for item in TraceFilter if self._matches(execution, item)]
            if wanted and not set(wanted) <= set(matched):
                continue
            rows.append(
                TraceExecution(
                    id=execution.id,
                    stage=execution.stage,
                    impl_version=execution.impl_version,
                    ordinal=execution.ordinal,
                    status=execution.status,
                    started_at=execution.started_at,
                    completed_at=execution.completed_at,
                    error_type=execution.error_type,
                    error_message=execution.error_message,
                    events=len(execution.trace_events),
                    invocations=len(execution.model_invocations),
                    usage=_usage(execution.model_invocations),
                    matched_filters=matched,
                )
            )
        return TraceView(executions=rows, filters_applied=wanted)

    def inspect(self, execution_id: str) -> StageInspection:
        """plan/11 → *Stage inspector*: one execution, every layer of it."""
        execution = self._session.get(models.StageExecution, execution_id)
        if execution is None:
            raise UnknownArtefact(f"no execution {execution_id}")

        return StageInspection(
            summary=_execution_ref(execution),
            inputs=[self._artifact_view(item) for item in execution.inputs],
            outputs=[self._artifact_view(item) for item in execution.outputs],
            context_selections=[
                ContextSelectionView(
                    id=selection.id,
                    strategy=selection.strategy,
                    strategy_version=selection.strategy_version,
                    token_budget=selection.token_budget,
                    items=[
                        ContextItemView(
                            ordinal=item.ordinal,
                            reference=item.reference,
                            disposition=item.disposition.value,
                            reason=item.reason,
                            score=item.score,
                        )
                        for item in selection.items
                    ],
                )
                for selection in execution.context_selections
            ],
            invocations=[self._invocation_view(call) for call in execution.model_invocations],
            tool_calls=[
                ToolCallView(
                    id=call.id,
                    tool_name=call.tool_name,
                    tool_version=call.tool_version,
                    initiator=call.initiator.value,
                    approval_required=call.approval_required,
                    approved_by=call.approved_by,
                    status=call.status.value,
                    raw_args=call.raw_args,
                    normalised_args=call.normalised_args,
                    raw_result=call.raw_result,
                    normalised_result=call.normalised_result,
                    started_at=call.started_at,
                    completed_at=call.completed_at,
                    error_message=call.error_message,
                )
                for call in execution.tool_invocations
            ],
            decisions=[
                DecisionView(
                    id=decision.id,
                    decision_type=decision.decision_type,
                    decided_by=decision.decided_by,
                    decided_by_type=decision.decided_by_type.value,
                    policy_version=decision.policy_version,
                    inputs=decision.inputs,
                    outcome=decision.outcome,
                    rationale=decision.rationale,
                    decided_at=decision.decided_at,
                )
                for decision in execution.decision_records
            ],
            evaluations=[
                EvaluationView(
                    id=evaluation.id,
                    evaluator_id=evaluation.evaluator_id,
                    evaluator_version=evaluation.evaluator_version,
                    rubric_version=evaluation.rubric_version,
                    passed=evaluation.passed,
                    scores=evaluation.scores,
                    created_at=evaluation.created_at,
                )
                for evaluation in execution.evaluation_runs
            ],
            interventions=[
                _intervention_view(intervention) for intervention in execution.user_interventions
            ],
            events=[
                EventView(
                    id=event.id,
                    event_type=event.event_type,
                    timestamp=event.timestamp,
                    actor_type=event.actor_type.value,
                    actor_id=event.actor_id,
                    payload=event.payload,
                    correlation_id=event.correlation_id,
                    causation_id=event.causation_id,
                    sequence=event.sequence,
                )
                for event in execution.trace_events
            ],
            usage=_usage(execution.model_invocations),
            duration_ms=_duration_ms(execution.started_at, execution.completed_at),
            error=(
                ErrorView(type=execution.error_type, message=execution.error_message)
                if execution.error_type or execution.error_message
                else None
            ),
        )

    def comparison(
        self, left: models.StageExecution, right: models.StageExecution
    ) -> tuple[list[ComparisonRow], int | None]:
        """Two executions field by field, and how far their outputs sit apart.

        Only what both sides recorded is compared. Preference and human judgement
        belong to phase 12's experimentation work; inventing a column for them
        here would be a promise this phase cannot keep.
        """
        left_call = left.model_invocations[0] if left.model_invocations else None
        right_call = right.model_invocations[0] if right.model_invocations else None
        left_usage, right_usage = _usage(left.model_invocations), _usage(right.model_invocations)

        rows = [
            _row("stage", left.stage, right.stage),
            _row("impl_version", left.impl_version, right.impl_version),
            _row("status", left.status.value, right.status.value),
            _row("provider", _attr(left_call, "provider"), _attr(right_call, "provider")),
            _row("model", _attr(left_call, "model"), _attr(right_call, "model")),
            _row("template_id", _attr(left_call, "template_id"), _attr(right_call, "template_id")),
            _row(
                "template_version",
                _attr(left_call, "template_version"),
                _attr(right_call, "template_version"),
            ),
            _row("input_tokens", str(left_usage.input_tokens), str(right_usage.input_tokens)),
            _row("output_tokens", str(left_usage.output_tokens), str(right_usage.output_tokens)),
            _row("cost_usd", _text(left_usage.cost_usd), _text(right_usage.cost_usd)),
            _row(
                "latency_ms",
                _text(_duration_ms(left.started_at, left.completed_at)),
                _text(_duration_ms(right.started_at, right.completed_at)),
            ),
            _row("output_hash", self._output_hash(left), self._output_hash(right)),
        ]
        return rows, self._output_distance(left, right)

    # ------------------------------------------------------------------
    # Trace filters
    # ------------------------------------------------------------------

    def _matches(self, execution: models.StageExecution, item: TraceFilter) -> bool:
        """Whether one execution is what a filter is looking for.

        One arm per filter, with no fallback: the type checker holds the match
        exhaustive, so a filter added to the vocabulary and not to this method
        fails to type-check rather than silently matching nothing.
        """
        match item:
            case TraceFilter.FAILED:
                return execution.status is ExecutionStatus.FAILED
            case TraceFilter.SCHEMA_REPAIR:
                return any(
                    call.retry_type in (RetryType.INVALID_SCHEMA, RetryType.CONTENT_REPAIR)
                    or call.outcome
                    in (InvocationOutcome.INVALID_JSON, InvocationOutcome.INVALID_SCHEMA)
                    for call in execution.model_invocations
                )
            case TraceFilter.FALLBACK_MODEL:
                return any(
                    call.retry_type is RetryType.MODEL_FALLBACK
                    for call in execution.model_invocations
                )
            case TraceFilter.BLOCKING_FINDING:
                return self._blocked(execution)
            case TraceFilter.USER_OVERRIDE:
                return any(
                    intervention.intervention_type is InterventionType.OVERRIDE
                    for intervention in execution.user_interventions
                )
            case TraceFilter.HIGH_COST:
                cost = _usage(execution.model_invocations).cost_usd
                return cost is not None and cost >= HIGH_COST_USD
            case TraceFilter.LOW_CONFIDENCE_SCORE:
                spreads = [
                    spread
                    for spread in map(_dispersion, execution.evaluation_runs)
                    if spread is not None
                ]
                return any(spread > LOW_CONFIDENCE_DISPERSION for spread in spreads)
            case TraceFilter.CONFIDENTIAL_WARNING:
                return self._redacted(execution)
            case TraceFilter.REPEATED_ISSUE:
                return self._repeats_a_finding(execution)

    def _blocked(self, execution: models.StageExecution) -> bool:
        """Did this execution produce a finding, or a report, that blocks publication?"""
        produced = {item.snapshot_id for item in execution.outputs}
        reviews = self._session.scalars(
            select(domain_models.Review).where(domain_models.Review.snapshot_id.in_(produced))
        ).all()
        if any(issue.blocks_publication for review in reviews for issue in review.issues):
            return True
        reports = self._session.scalars(
            select(domain_models.ValidationReport).where(
                domain_models.ValidationReport.snapshot_id.in_(produced)
            )
        ).all()
        return any(not report.passed for report in reports)

    def _redacted(self, execution: models.StageExecution) -> bool:
        """Was confidential material found at this boundary and removed?

        Answered from the stored payloads rather than from a flag, because
        redaction happens before persistence (plan/00) and leaves the only
        evidence there is: the placeholder it wrote in place of the material.
        Reading the payloads costs something, which is why this is a filter a
        person asks for and never part of the default listing.
        """
        candidates: list[ArtifactSnapshot | None] = [item.snapshot for item in execution.artifacts]
        for call in execution.model_invocations:
            candidates.extend(
                [
                    call.request_snapshot,
                    call.raw_response_snapshot,
                    call.parsed_response_snapshot,
                    call.validated_response_snapshot,
                ]
            )
        return any(
            REDACTION_MARKER in self._runtime.snapshots.read(snapshot).decode("utf-8", "replace")
            for snapshot in candidates
            if snapshot is not None
        )

    def _repeats_a_finding(self, execution: models.StageExecution) -> bool:
        """Did this execution raise a finding an earlier round had already raised?"""
        produced = {item.snapshot_id for item in execution.outputs}
        reviews = list(
            self._session.scalars(
                select(domain_models.Review).where(domain_models.Review.snapshot_id.in_(produced))
            )
        )
        if not reviews:
            return False

        fingerprints = {
            issue.fingerprint for review in reviews for issue in review.issues if issue.fingerprint
        }
        earlier = self._session.scalars(
            select(domain_models.ReviewIssue)
            .join(
                domain_models.Review, domain_models.Review.id == domain_models.ReviewIssue.review_id
            )
            .where(
                domain_models.ReviewIssue.fingerprint.in_(fingerprints),
                domain_models.Review.round < max(review.round for review in reviews),
            )
        ).all()
        return bool(earlier)

    # ------------------------------------------------------------------
    # Rows, and the documents behind them
    # ------------------------------------------------------------------

    def _project(self, project_id: str) -> domain_models.Project:
        project = self._session.get(domain_models.Project, project_id)
        if project is None:
            raise UnknownArtefact(f"no project {project_id}")
        return project

    def _article(self, article_id: str) -> domain_models.Article:
        article = self._session.get(domain_models.Article, article_id)
        if article is None:
            raise UnknownArtefact(f"no article {article_id}")
        return article

    def _run(self, project_id: str) -> models.PipelineRun:
        run = self._session.scalars(
            select(models.PipelineRun)
            .where(models.PipelineRun.project_id == project_id)
            .order_by(models.PipelineRun.started_at.desc(), models.PipelineRun.id.desc())
        ).first()
        if run is None:
            raise UnknownArtefact(f"no pipeline run for project {project_id}")
        return run

    def _privacy_view(
        self, project_id: str, constraints: domain_models.ProjectConstraints
    ) -> PrivacyView:
        """What may be done with this project's trace, and the warning first.

        ``holds_confidential`` is computed rather than assumed from the
        constraints' name list: a span is confidential because somebody flagged
        *it*, and a project can hold restricted material without ever naming a
        person or a customer.
        """
        return PrivacyView(
            holds_confidential=bool(restricted_spans(self._session, project_id)),
            retention_mode=constraints.trace_retention_mode.value,
            export_command=ActionLink(
                action="export_traces",
                method="GET",
                path=f"/projects/{project_id}/traces",
                taken_by="you",
            ),
            delete_command=ActionLink(
                action="delete_traces",
                method="DELETE",
                path=f"/projects/{project_id}/traces",
                taken_by="you",
            ),
        )

    def _retry_command(self, run: models.PipelineRun, project_id: str) -> ActionLink | None:
        """Offered only where a run cannot get itself moving again.

        That is a job that failed with nothing queued behind it. While work is
        pending or running the run is fine and the offer would be a duplicate;
        with nothing failed there is nothing to run again. Both conditions are
        read from the queue rather than from the state, because "stuck" is a
        fact about the *work*, not about the position.
        """
        statuses = set(
            self._session.scalars(select(Job.status).where(Job.pipeline_run_id == run.id))
        )
        if JobStatus.FAILED not in statuses or statuses & {JobStatus.PENDING, JobStatus.RUNNING}:
            return None
        return ActionLink(
            action="retry_failed_job",
            method=RETRY_FAILED.method,
            path=resolve(RETRY_FAILED, project_id=project_id, article_id=None),
            requires_actor=RETRY_FAILED.requires_actor,
            taken_by="you",
        )

    def _position(self, run: models.PipelineRun) -> WorkflowPosition:
        """Where the run is, read from its row.

        Read rather than resumed: rebuilding the engine would open a workflow
        execution, and a screen refresh that appended to a run's provenance would
        make the trace a record of who was watching.
        """
        position = self._runtime.positions.load(run)
        if position is None:
            raise UnknownArtefact(f"run {run.id} has no recorded position")
        return position

    def _executions(self, run: models.PipelineRun) -> list[models.StageExecution]:
        """The run's executions, oldest first, in the same total order the
        relationship uses (``PipelineRun.stage_executions``).

        ``started_at`` is not decoration here. Nothing assigns ``ordinal``, so
        every row carries the default 0 and ``ordinal, id`` degrades to a sort by
        a random hex id — the run's history comes back shuffled, on SQLite too,
        and the caller that asks for "the last stage" gets an arbitrary one.
        """
        return list(
            self._session.scalars(
                select(models.StageExecution)
                .where(models.StageExecution.pipeline_run_id == run.id)
                .order_by(
                    models.StageExecution.ordinal,
                    models.StageExecution.started_at,
                    models.StageExecution.id,
                )
            )
        )

    def _invocations(self, run: models.PipelineRun) -> list[models.ModelInvocation]:
        return [call for execution in self._executions(run) for call in execution.model_invocations]

    def _active_jobs(self, run: models.PipelineRun) -> list[Job]:
        return list(
            self._session.scalars(
                select(Job)
                .where(
                    Job.pipeline_run_id == run.id,
                    Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
                )
                .order_by(Job.created_at, Job.id)
            )
        )

    def _documents(self, project_id: str) -> list[domain_models.SourceDocument]:
        return list(
            self._session.scalars(
                select(domain_models.SourceDocument)
                .where(domain_models.SourceDocument.project_id == project_id)
                .order_by(domain_models.SourceDocument.id)
            )
        )

    def _segments(self, document_id: str) -> list[domain_models.SourceSegment]:
        return list(
            self._session.scalars(
                select(domain_models.SourceSegment)
                .where(domain_models.SourceSegment.document_id == document_id)
                .order_by(domain_models.SourceSegment.ordinal)
            )
        )

    def _claims(self, project_id: str) -> list[ClaimView]:
        """The claims extraction found, read from the source model it wrote.

        From the document rather than from a table: phase 06 made the source
        model an artefact, not a set of rows, and a projection that queried
        ``source_claims`` would show an empty workspace for every project the
        system has ever built.
        """
        run = self._run(project_id)
        document = self._document(
            rehydrate.latest_snapshot(self._session, run, ArtifactType.SOURCE_MODEL)
        )
        if document is None:
            return []
        return [
            ClaimView(
                id=str(claim.get("id", "")),
                text=str(claim.get("text", "")),
                classification=str(claim.get("classification", "")),
                segment_ids=[
                    segment_id
                    for evidence in claim.get("evidence", [])
                    for segment_id in evidence.get("segment_ids", [])
                ],
            )
            for claim in document.get("claims", [])
        ]

    def _concepts(self, architecture_id: str) -> list[domain_models.ArticleConcept]:
        return list(
            self._session.scalars(
                select(domain_models.ArticleConcept)
                .where(domain_models.ArticleConcept.architecture_id == architecture_id)
                .order_by(domain_models.ArticleConcept.ordinal)
            )
        )

    def _articles(self, project_id: str) -> list[domain_models.Article]:
        return list(
            self._session.scalars(
                select(domain_models.Article)
                .where(domain_models.Article.project_id == project_id)
                .order_by(domain_models.Article.id)
            )
        )

    def _versions(self, article_id: str) -> list[domain_models.ArticleVersion]:
        return list(
            self._session.scalars(
                select(domain_models.ArticleVersion)
                .where(domain_models.ArticleVersion.article_id == article_id)
                .order_by(domain_models.ArticleVersion.ordinal)
            )
        )

    def _reviews(
        self, versions: Sequence[domain_models.ArticleVersion]
    ) -> list[domain_models.Review]:
        """Every review of this article, in the order the rounds happened."""
        if not versions:
            return []
        order = {version.id: version.ordinal for version in versions}
        reviews = self._session.scalars(
            select(domain_models.Review).where(
                domain_models.Review.article_version_id.in_([version.id for version in versions])
            )
        ).all()
        # By the version reviewed, then by round: a review's own ``round`` counts
        # passes over *one* version, so ordering by it alone would interleave the
        # rounds of two versions and call the second review of v1 the first of v2.
        return sorted(
            reviews,
            key=lambda review: (order[review.article_version_id], review.round, review.id),
        )

    def _latest_review(
        self, versions: Sequence[domain_models.ArticleVersion]
    ) -> domain_models.Review | None:
        reviews = self._reviews(versions)
        return reviews[-1] if reviews else None

    def _latest_plan(
        self, versions: Sequence[domain_models.ArticleVersion]
    ) -> domain_models.RevisionPlan | None:
        """The newest plan drawn from any review of this article.

        Not only from the *latest* review: the round that settles an article asks
        for no rewrite and so produces no plan, and a workspace that showed
        nothing there would hide the plan the current version was written to.
        """
        reviews = self._reviews(versions)
        if not reviews:
            return None
        return self._session.scalars(
            select(domain_models.RevisionPlan)
            .where(domain_models.RevisionPlan.review_id.in_([review.id for review in reviews]))
            .order_by(domain_models.RevisionPlan.id.desc())
        ).first()

    def _validation(
        self, versions: Sequence[domain_models.ArticleVersion]
    ) -> ValidationView | None:
        if not versions:
            return None
        report = self._session.scalars(
            select(domain_models.ValidationReport)
            .where(
                domain_models.ValidationReport.article_version_id.in_(
                    [version.id for version in versions]
                )
            )
            .order_by(domain_models.ValidationReport.id.desc())
        ).first()
        if report is None:
            return None
        document = self._document(report.snapshot) or {}
        return ValidationView(
            passed=report.passed,
            validator_version=str(document.get("validator_version", "")),
            checks_run=list(document.get("checks_run", [])),
            findings=list(document.get("findings", [])),
            corrections=list(document.get("corrections", [])),
        )

    def _brief_snapshot(self, article_id: str) -> ArtifactSnapshot | None:
        brief = self._session.scalars(
            select(domain_models.ArticleBrief)
            .where(domain_models.ArticleBrief.concept_id == article_id)
            .order_by(domain_models.ArticleBrief.id.desc())
        ).first()
        return brief.snapshot if brief else None

    def _scores(self, run: models.PipelineRun, article_id: str) -> list[ScoreView]:
        """Every scoring pass over this article's versions, oldest first.

        Matched through the linkage the score recorded — phase 08 refuses to
        write an evaluation that cannot say which version it scored — rather than
        by assuming the newest evaluation of the run belongs to the article being
        looked at.
        """
        versions = {version.id for version in self._versions(article_id)}
        found: list[ScoreView] = []
        for execution in self._executions(run):
            if execution.stage != SCORE_STAGE:
                continue
            for evaluation in execution.evaluation_runs:
                linkage = evaluation.scores.get("linkage", {})
                if linkage.get("article_version_id") not in versions:
                    continue
                found.append(
                    ScoreView(
                        execution_id=execution.id,
                        overall=float(evaluation.scores.get("overall", 0.0)),
                        passed=evaluation.passed,
                        rubric_version=evaluation.rubric_version,
                        evaluator_version=evaluation.evaluator_version,
                        dimensions={
                            name: float(value)
                            for name, value in evaluation.scores.get("dimensions", {}).items()
                        },
                        failures=list(evaluation.scores.get("failures", [])),
                        confidence=_confidence(evaluation),
                        created_at=evaluation.created_at,
                    )
                )
        return found

    def _rewrites(self, run: models.PipelineRun) -> int:
        """How many times this run rewrote the article.

        Counted from the executions rather than from the workflow ledger: the
        ledger counts the *routed revisions* a limit is spent on (plan/05's
        3/2/1), and a rewrite the author asked for after a review is a rewrite
        that happened without spending one. The approval view is asking what was
        done to the article, not what the limit has left.
        """
        return sum(
            1
            for execution in self._executions(run)
            if execution.stage == REWRITE_STAGE and execution.status is ExecutionStatus.SUCCEEDED
        )

    def _evidence_for(
        self, project_id: str, article_id: str, findings: Sequence[FindingView]
    ) -> list[ClaimView]:
        """The source claims this article was built on, and argued about.

        Two sources, both the run's own: the claims the brief names as its
        argument, and the claims the reviewer pointed at. Anything else in the
        source model belongs to the source workspace — this is the evidence for
        *this* article.
        """
        brief = self._document(self._brief_snapshot(article_id)) or {}
        wanted = {
            str(claim_id)
            for section in brief.get("argument_structure", [])
            for claim_id in section.get("claim_ids", [])
        }
        wanted.update(finding.source_ref for finding in findings if finding.source_ref)
        return [claim for claim in self._claims(project_id) if claim.id in wanted]

    def _stagnation_warnings(self, run: models.PipelineRun) -> list[str]:
        """What the loop itself said about going round again.

        Read from the decisions the routing policy recorded rather than
        recomputed: stagnation is phase 08's judgement, and a second opinion
        formed here could disagree with the one that actually stalled the run.
        """
        return [
            decision.rationale or decision.outcome
            for execution in self._executions(run)
            for decision in execution.decision_records
            if "stagnation" in decision.decision_type or "stalled" in decision.outcome
        ]

    def _questions(self, project_id: str) -> list[QuestionView]:
        gaps = list(
            self._session.scalars(
                select(domain_models.SourceGap)
                .where(domain_models.SourceGap.project_id == project_id)
                .order_by(domain_models.SourceGap.ordinal, domain_models.SourceGap.id)
            )
        )
        answers = {
            answer.gap_id: answer for answer in rehydrate.open_answers(self._session, project_id)
        }
        # Whether an answer can be taken at all belongs to the run, not to the
        # question: answering re-enters extraction, and a run that has since left
        # the pause has no such edge. Read from the transition table so the queue
        # cannot offer what a POST would refuse (rule 2 above).
        open_for_answers = WorkflowAction.ANSWER_QUESTIONS.value in available_actions(
            self._position(self._run(project_id)).state
        )
        return [
            QuestionView(
                id=gap.id,
                question=gap.question,
                why_it_matters=gap.why_it_matters,
                description=gap.description,
                priority=gap.priority.value,
                group=gap.group,
                ordinal=gap.ordinal,
                surfaced=gap.surfaced,
                resolved=gap.resolved,
                answer=_answer_view(answers.get(gap.id)),
                # Gone once the gap is closed, whether this question's own answer
                # closed it or another one did: a settled question is a record.
                answer_path=(
                    f"/projects/{project_id}/source-gaps/{gap.id}/answer"
                    if open_for_answers and not gap.resolved
                    else None
                ),
            )
            for gap in gaps
        ]

    def _completeness(
        self, project_id: str, questions: Sequence[QuestionView]
    ) -> SourceCompleteness:
        documents = self._documents(project_id)
        return SourceCompleteness(
            documents=len(documents),
            confidential_documents=sum(1 for document in documents if document.confidential),
            segments=sum(len(self._segments(document.id)) for document in documents),
            claims=len(self._claims(project_id)),
            unresolved_questions=sum(1 for question in questions if not question.resolved),
            answered_questions=sum(1 for question in questions if question.answer is not None),
        )

    def _card(self, article: domain_models.Article) -> ArticleCard:
        versions = self._versions(article.id)
        review = self._latest_review(versions)
        validation = self._validation(versions)
        scores = self._scores(self._run(article.project_id), article.id)
        return ArticleCard(
            id=article.id,
            title=article.title,
            status=article.status,
            versions=len(versions),
            rewrite_rounds=self._rewrites(self._run(article.project_id)),
            open_findings=(
                sum(1 for issue in review.issues if issue.blocks_publication) if review else 0
            ),
            latest_score=scores[-1] if scores else None,
            validated=validation.passed if validation else None,
        )

    def _voice(self, project_id: str, article_id: str) -> VoiceView:
        author = self._project(project_id).user_id
        resolved = VoiceStore(
            self._session,
            snapshots=self._runtime.snapshots,
            recorder=self._runtime.recorder,
        ).resolve(user_id=author, project_id=project_id, article_id=article_id)
        return VoiceView(
            sources=list(resolved.sources),
            active=[
                ActiveInstructionView(
                    instruction_id=active.instruction.id,
                    category=active.instruction.category.value,
                    strength=active.instruction.strength.value,
                    instruction=active.instruction.text,
                    source=active.source,
                    overrides=active.overrides.source if active.overrides else "",
                )
                for active in resolved.active
            ],
            suppressed=[item.instruction.id for item in resolved.suppressed],
        )

    def _version_view(self, version: domain_models.ArticleVersion) -> VersionView:
        document = self._document(version.snapshot) or {}
        return VersionView(
            id=version.id,
            ordinal=version.ordinal,
            title=str(document.get("title", "")),
            thesis=str(document.get("thesis", "")),
            body=str(document.get("body", "")),
            snapshot_id=version.snapshot_id,
            parent_id=version.parent_id,
            created_by_execution_id=version.created_by_execution_id,
        )

    def _execution_ref(self, execution_id: str | None, *, live: bool = True) -> ExecutionRef | None:
        if execution_id is None:
            return None
        execution = self._session.get(models.StageExecution, execution_id)
        return _execution_ref(execution, live=live) if execution else None

    def _producer_of(self, snapshot: ArtifactSnapshot | None) -> models.StageExecution | None:
        """The execution that wrote a snapshot, so a screen can link back to it."""
        if snapshot is None:
            return None
        return self._session.scalars(
            select(models.StageExecution)
            .join(
                models.ExecutionArtifact,
                models.ExecutionArtifact.stage_execution_id == models.StageExecution.id,
            )
            .where(models.ExecutionArtifact.snapshot_id == snapshot.id)
            .order_by(models.StageExecution.ordinal.desc())
        ).first()

    def _artifact_view(self, artifact: models.ExecutionArtifact) -> ArtifactView:
        snapshot = artifact.snapshot
        return ArtifactView(
            snapshot_id=snapshot.id,
            artifact_type=snapshot.artifact_type.value,
            role=artifact.role,
            direction=artifact.direction.value,
            ordinal=artifact.ordinal,
            content_hash=snapshot.content_hash,
            size=snapshot.size,
            content=self._payload(snapshot),
        )

    def _invocation_view(self, call: models.ModelInvocation) -> InvocationView:
        return InvocationView(
            id=call.id,
            parent_invocation_id=call.parent_invocation_id,
            attempt_ordinal=call.attempt_ordinal,
            retry_type=call.retry_type.value if call.retry_type else None,
            outcome=call.outcome.value,
            provider=call.provider,
            model=call.model,
            template_id=call.template_id,
            template_version=call.template_version,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cost_usd=call.cost_usd,
            started_at=call.started_at,
            completed_at=call.completed_at,
            error_message=call.error_message,
            effective_request=self._payload(call.request_snapshot),
            raw_response=self._payload(call.raw_response_snapshot),
            parsed_response=self._payload(call.parsed_response_snapshot),
            validated_response=self._payload(call.validated_response_snapshot),
        )

    def _document(self, snapshot: ArtifactSnapshot | None) -> dict[str, Any] | None:
        """A stored artefact as the object it is, or ``None`` where there is none."""
        payload = self._payload(snapshot)
        return payload if isinstance(payload, dict) else None

    def _payload(self, snapshot: ArtifactSnapshot | None) -> Any:
        """Whatever a snapshot holds: an object where it is JSON, text otherwise.

        Raw model responses are kept verbatim, valid JSON or not (phase 03 keeps
        an unparseable body precisely because it is the evidence), so the
        inspector has to be able to show one.
        """
        if snapshot is None:
            return None
        raw = self._runtime.snapshots.read(snapshot)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw.decode("utf-8", "replace")

    def _output_hash(self, execution: models.StageExecution) -> str | None:
        outputs = execution.outputs
        return outputs[-1].snapshot.content_hash if outputs else None

    def _output_distance(
        self, left: models.StageExecution, right: models.StageExecution
    ) -> int | None:
        """How many lines separate the two outputs, or ``None`` if one is missing."""
        first, second = self._output_text(left), self._output_text(right)
        if first is None or second is None:
            return None
        return _line_distance(first, second)

    def _output_text(self, execution: models.StageExecution) -> str | None:
        outputs = execution.outputs
        if not outputs:
            return None
        payload = self._payload(outputs[-1].snapshot)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, indent=2, sort_keys=True)


# ----------------------------------------------------------------------
# Arrangements that need no session
# ----------------------------------------------------------------------


def _action_links(
    state: WorkflowState, *, project_id: str | None, article_id: str | None
) -> list[ActionLink]:
    """Every action the state offers, each with the endpoint that performs it.

    One link per *offered* action, never one per endpoint: the list of actions is
    the transition table's answer and this only annotates it. An action no
    endpoint takes keeps a null path rather than being dropped, so a client can
    show the true set of transitions and still offer buttons for only the ones a
    person can take.
    """
    links: list[ActionLink] = []
    for name in available_actions(state):
        action = _action_or_none(name)
        endpoint = ACTION_ENDPOINTS.get(action) if action is not None else None
        path = resolve(endpoint, project_id=project_id, article_id=article_id)
        links.append(
            ActionLink(
                action=name,
                method=endpoint.method if endpoint and path else None,
                path=path,
                requires_actor=bool(endpoint and endpoint.requires_actor and path),
                taken_by=(
                    "you" if action is not None and is_taken_by_user(state, action) else "pipeline"
                ),
            )
        )
    return links


def _journey(state: WorkflowState) -> ProjectJourney:
    """The pipeline at the size a person follows it, from the workflow's own map."""
    return ProjectJourney(
        steps=[
            JourneyStep(id=step.id, title=step.title, blurb=step.blurb, status=step.status)
            for step in journey_of(state)
        ],
        headline=STATE_HEADLINES[state],
        waiting_on=waiting_on(state),
    )


def _pending_command(
    state: WorkflowState, *, project_id: str | None, article_id: str | None
) -> ActionLink | None:
    """The command that starts the work this state is waiting for, if any."""
    endpoint = STATE_COMMANDS.get(state)
    path = resolve(endpoint, project_id=project_id, article_id=article_id)
    if endpoint is None or path is None:
        return None
    return ActionLink(action=state.value, method=endpoint.method, path=path)


def _offered(action: WorkflowAction, state: WorkflowState, *, article_id: str) -> ActionLink | None:
    """One action's link, when this state offers it, or nothing.

    Named actions get their own field on a view where the screen needs a control
    of its own for them — a choice of destination, a second id. The alternative
    is the interface picking the action out of ``action_links`` by name, which is
    the frontend deciding which action it is looking at.
    """
    if action.value not in available_actions(state):
        return None
    endpoint = ACTION_ENDPOINTS.get(action)
    path = resolve(endpoint, project_id=None, article_id=article_id)
    if endpoint is None or path is None:
        return None
    return ActionLink(
        action=action.value,
        method=endpoint.method,
        path=path,
        requires_actor=endpoint.requires_actor,
        taken_by="you",
    )


def _action_or_none(name: str) -> WorkflowAction | None:
    """The workflow action a name refers to, or nothing.

    ``available_actions`` also carries the two execution affordances, which are
    not transitions and have no member here; they act on an execution a screen
    already has in hand, so they are addressed from the trace rather than from
    the run.
    """
    try:
        return WorkflowAction(name)
    except ValueError:
        return None


def _may_edit_architecture(actions: Sequence[str]) -> bool:
    """Whether an author may commit edits to the architecture from here.

    Editing has no action of its own: ``override_architecture`` reopens an
    approved architecture and rejects a merely-proposed one, taking whichever
    edge the run is standing on. So the offer follows the same pair, rather than
    a single name that would be right in one state and absent in the other.
    """
    return any(
        action.value in actions
        for action in (WorkflowAction.REOPEN_ARCHITECTURE, WorkflowAction.REJECT_ARCHITECTURE)
    )


def _architecture_command(
    method: str, project_id: str, version_id: str | None, suffix: str, *, actor: bool = False
) -> ActionLink | None:
    """Where an architecture version is edited or approved, or nothing.

    ``None`` before anything is proposed: there is no version to address, and a
    URL with a hole in it would be a button that fails when pressed.

    The caller gates on the run's state for the other half of that rule. A
    version that exists is not a version this run may still act on, and an
    approve button offered after approval fails exactly the same way a URL with
    a hole in it does.
    """
    if version_id is None:
        return None
    return ActionLink(
        action="edit_architecture" if method == "PUT" else "approve_architecture",
        method=method,
        path=f"/projects/{project_id}/architecture/{version_id}{suffix}",
        requires_actor=actor,
    )


def _constraints_view(row: domain_models.ProjectConstraints) -> ConstraintsView:
    return ConstraintsView(
        audience=row.audience,
        platform=row.platform,
        depth=row.depth.value,
        target_length_words=row.target_length_words,
        first_person_allowed=row.first_person_allowed,
        allowed_providers=list(row.allowed_providers),
        confidential_names=list(row.confidential_names),
        trace_retention_consent=row.trace_retention_consent,
        auto_advance=row.auto_advance,
    )


def _routing_view(project: domain_models.Project) -> RoutingProfilesView:
    """Which routing policy this project runs against, and what else it could.

    The policy is loaded to read its version, which is a file read per dashboard
    — cheap, and the alternative is worse: caching it here would mean a screen
    reporting the version of a file somebody has since edited, on the one panel
    whose entire job is to say accurately what is running.

    A selected profile whose file has gone is reported as selected with no
    version rather than raising. The dashboard is where a person would go to
    *fix* that, and a read that failed would take the screen down with it.
    """
    try:
        version = routing_policy(project.routing_profile).version
    except RoutingConfigError:
        version = ""
    return RoutingProfilesView(
        selected=project.routing_profile,
        available=list(available_profiles()),
        policy_version=version,
        command=ActionLink(
            action="set_routing_profile",
            method="PUT",
            path=f"/projects/{project.id}/routing-profile",
            requires_actor=True,
            taken_by="you",
        ),
    )


def _answer_view(answer: domain_models.UserAnswer | None) -> AnswerView | None:
    if answer is None:
        return None
    return AnswerView(
        text=answer.text,
        question=answer.question,
        why_it_matters=answer.why_it_matters,
        response_type=answer.response_type.value,
        answered_by=answer.answered_by,
        diff_snapshot_id=answer.diff_snapshot_id,
    )


def _job_view(job: Job) -> JobView:
    return JobView(
        id=job.id,
        job_type=job.job_type,
        status=job.status.value,
        attempts=job.attempts,
        created_at=job.created_at,
    )


def _finding_view(issue: domain_models.ReviewIssue) -> FindingView:
    return FindingView(
        id=issue.id,
        ref=issue.ref,
        severity=issue.severity.value,
        category=issue.category,
        location=issue.location,
        passage=issue.passage,
        description=issue.description,
        evidence=issue.evidence,
        source_ref=issue.source_ref,
        brief_ref=issue.brief_ref,
        recommended_correction=issue.recommended_correction,
        suggested_route=issue.suggested_route,
        blocks_publication=issue.blocks_publication,
        reviewer_confidence=issue.reviewer_confidence,
        fingerprint=issue.fingerprint,
        status=issue.status.value,
        decided_by=issue.decided_by,
        decision_reason=issue.decision_reason,
    )


def _lifecycle(issue: domain_models.ReviewIssue, seen: set[str]) -> Lifecycle:
    """What this finding is, relative to the rounds before it."""
    if issue.status is FindingStatus.SUPPRESSED:
        return Lifecycle.RESOLVED
    if issue.fingerprint and issue.fingerprint in seen:
        return Lifecycle.REPEATED
    return Lifecycle.NEW


def _intervention_view(intervention: models.UserIntervention) -> InterventionView:
    return InterventionView(
        id=intervention.id,
        intervention_type=intervention.intervention_type.value,
        user_id=intervention.user_id,
        occurred_at=intervention.occurred_at,
        payload=intervention.payload,
    )


def _execution_ref(execution: models.StageExecution, *, live: bool = True) -> ExecutionRef:
    """One execution, named so a screen can link to it and run it again.

    ``live`` says whether the run this belongs to can still act on a rerun's
    output. Defaulted to ``True`` so a caller that has no position in hand — the
    inspector reaches an execution by id, from anywhere — does not have to
    pretend to know; the callers that *do* know pass it.
    """
    return ExecutionRef(
        id=execution.id,
        stage=execution.stage,
        impl_version=execution.impl_version,
        ordinal=execution.ordinal,
        status=execution.status,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        error_type=execution.error_type,
        error_message=execution.error_message,
        rerun_command=ActionLink(
            action="replay_execution",
            method="POST",
            path=f"/executions/{execution.id}/replay",
            requires_actor=True,
            taken_by="you",
        ),
        fork_command=ActionLink(
            action="fork_execution",
            method="POST",
            path=f"/executions/{execution.id}/fork",
            requires_actor=True,
            taken_by="you",
        ),
        rerun_feeds_pipeline=live,
    )


def _model_versions(executions: Iterable[models.StageExecution]) -> list[ModelVersionView]:
    """Which model and prompt version stood behind each stage, once each."""
    seen: dict[tuple[str, str, str, str, str], ModelVersionView] = {}
    for execution in executions:
        for call in execution.model_invocations:
            key = (
                execution.stage,
                call.provider,
                call.model,
                call.template_id,
                call.template_version,
            )
            seen.setdefault(
                key,
                ModelVersionView(
                    stage=execution.stage,
                    provider=call.provider,
                    model=call.model,
                    template_id=call.template_id,
                    template_version=call.template_version,
                ),
            )
    return list(seen.values())


def _remaining_concerns(
    findings: Sequence[FindingView], validation: ValidationView | None
) -> list[str]:
    """What is still outstanding against the version about to be approved."""
    concerns = [finding.description for finding in findings if finding.blocks_publication]
    if validation is not None:
        concerns.extend(str(finding.get("detail", "")) for finding in validation.findings)
    return [concern for concern in concerns if concern]


def _usage(invocations: Sequence[models.ModelInvocation]) -> UsageSummary:
    costs = [call.cost_usd for call in invocations if call.cost_usd is not None]
    return UsageSummary(
        model_calls=len(invocations),
        input_tokens=sum(call.input_tokens for call in invocations),
        output_tokens=sum(call.output_tokens for call in invocations),
        cost_usd=sum(costs) if costs else None,
    )


def _confidence(evaluation: models.EvaluationRun) -> ScoreConfidenceView:
    """What the repeat passes of this score said, as the evaluation recorded it."""
    recorded = evaluation.scores.get("confidence", {})
    return ScoreConfidenceView(
        repeats=int(recorded.get("repeats", 1)),
        repeat_scores=[float(value) for value in recorded.get("repeat_scores", [])],
        dispersion=recorded.get("dispersion"),
        stdev=recorded.get("stdev"),
    )


def _dispersion(evaluation: models.EvaluationRun) -> float | None:
    value = evaluation.scores.get("confidence", {}).get("dispersion")
    return float(value) if isinstance(value, int | float) else None


def _duration_ms(started: datetime, finished: datetime | None) -> int | None:
    if finished is None:
        return None
    return int((finished - started).total_seconds() * 1000)


def _diff(before: str, after: str) -> DiffView:
    """A line-level diff of two stored bodies."""
    lines: list[DiffLine] = []
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            lines.extend(
                DiffLine(kind=DiffKind.EQUAL, text=line) for line in after.splitlines()[j1:j2]
            )
            continue
        for line in before.splitlines()[i1:i2]:
            lines.append(DiffLine(kind=DiffKind.REMOVED, text=line))
            removed += 1
        for line in after.splitlines()[j1:j2]:
            lines.append(DiffLine(kind=DiffKind.ADDED, text=line))
            added += 1
    return DiffView(added=added, removed=removed, lines=lines)


def _line_distance(before: str, after: str) -> int:
    """How many lines differ between two texts.

    Line-level rather than character-level: the comparison screen reports how far
    apart two outputs are, and a Levenshtein distance over two long articles
    costs quadratic time to answer a question nobody asks that precisely.
    """
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    return sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
    )


def _row(field: str, left: str | None, right: str | None) -> ComparisonRow:
    return ComparisonRow(field=field, left=left, right=right, same=left == right)


def _attr(call: models.ModelInvocation | None, name: str) -> str | None:
    value = getattr(call, name, None) if call is not None else None
    return str(value) if value is not None else None


def _text(value: object | None) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "HIGH_COST_USD",
    "LOW_CONFIDENCE_DISPERSION",
    "ProjectionReader",
    "UnknownArtefact",
]
