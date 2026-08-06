"""Every command the product offers, in one place (phase 09).

plan/09 → *the single entry point both API and CLI call; issues commands to the
workflow engine, never re-implements transition rules*.

The shape of nearly every method here is the same four steps, and the order is
the point:

1. **Resume the run.** Rebuild the engine from the stored position, so guards and
   rewrite limits carry across the process boundary they now span.
2. **Ask the engine to move.** Legality is never decided here. An out-of-order
   command raises before anything is queued, so no worker inherits an
   instruction that was never valid — which is plan/09's "API embedding workflow
   rules" risk, answered structurally rather than by convention.
3. **Do or defer the work.** Anything that calls a model becomes a job. Anything
   deterministic — ingesting, validating, approving — runs here, because
   queueing microseconds of local computation buys a round trip and a second
   failure mode and nothing else.
4. **Capture the position**, so the next request or worker resumes from it.

The entry transition is taken *in the request*, not by the worker. The ``-ing``
states mean "work is in flight"; a run left in ``source_ingested`` until a worker
happened to pick the job up would leave a client unable to tell "not started"
from "started".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from groundscribe.app import rehydrate
from groundscribe.app.actions import available_actions
from groundscribe.app.advance import (
    Have,
    Step,
    auto_advance_enabled,
    next_step,
    selected_article_id,
    startable,
)
from groundscribe.app.runtime import Runtime
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import (
    AnswerResponse,
    ArtifactType,
    FindingStatus,
    SourceFormat,
)
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.experiments.datasets import DatasetBuilder
from groundscribe.experiments.metrics import ArmMetrics
from groundscribe.experiments.models import (
    EvaluationDataset,
    EvaluationDatasetEntry,
    ExperimentArm,
    ExperimentPreference,
    ExperimentResult,
)
from groundscribe.experiments.replay import Rerun as ExperimentRerun
from groundscribe.experiments.replay import plan_rerun
from groundscribe.experiments.runs import ArmSpec, ExperimentRunner, UnknownArm
from groundscribe.experiments.variables import ForkVariables
from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.jobs.models import Job
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.llm.quota import QuotaWindow, subscription_usage
from groundscribe.llm.routing import available_profiles, routing_policy
from groundscribe.observability.metrics import RunMetrics, collect_metrics
from groundscribe.privacy.export import ExportedArticle, ExportFormat, render_article
from groundscribe.privacy.material import restricted_spans
from groundscribe.privacy.retention import RetentionPolicy
from groundscribe.privacy.storage import StorageReport, storage_report
from groundscribe.privacy.traces import TraceDeletion, TraceExport, delete_traces, export_traces
from groundscribe.privacy.visibility import ProviderVisibility, provider_visibility
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, InterventionType
from groundscribe.stages.base import PipelineContext, StageRunner
from groundscribe.stages.ingestion import IngestSource
from groundscribe.stages.override import (
    OverrideCommand,
    approve_architecture,
    override_architecture,
)
from groundscribe.stages.questions import open_question_queue
from groundscribe.stages.review import open_review_ledger
from groundscribe.stages.schemas import (
    ArchitectureProposal,
    ArticleBriefDocument,
    SourceModel,
)
from groundscribe.validation.stage import ValidateFinalOutput
from groundscribe.voice.learning import VoiceLearning
from groundscribe.voice.models import VoiceProfileVersion, VoiceSuggestion
from groundscribe.voice.precedence import ResolvedVoice
from groundscribe.voice.schemas import VoiceProfileDocument
from groundscribe.voice.shipped import shipped_voice_profile
from groundscribe.voice.store import VoiceStore
from groundscribe.workflow.engine import WorkflowEngine
from groundscribe.workflow.errors import AttributionRequired, IllegalTransition
from groundscribe.workflow.policy import FailureCategory
from groundscribe.workflow.position import WorkflowPosition
from groundscribe.workflow.states import WorkflowAction, WorkflowState

A = WorkflowAction


class UnknownProject(LookupError):
    """Asked about a project that does not exist, or has no run."""


class NothingToRevise(LookupError):
    """Asked to route a failing score on a run that has none.

    Its own type because both ways to get here are ordinary: the article has not
    been scored, or its last score passed. Neither is an error in the caller's
    request and neither should read as one.
    """


class NothingToApprove(LookupError):
    """Asked to approve a revision plan that has not been written.

    Its own type because the state legitimately offers the edge: a run arrives in
    ``revision_plan_required`` before the plan exists, so "you may approve here"
    and "there is something to approve" are different questions and only the
    first is the transition table's.
    """


class UnknownFinding(LookupError):
    """Asked to decide a finding this review does not hold."""


class UndecidableFinding(ValueError):
    """Asked to set a finding to a status a person does not choose.

    ``proposed`` is where a finding starts and ``suppressed`` is the system
    holding one back; neither is a decision, and offering them as though they
    were would let a caller un-decide something the record says was decided.
    """


class NothingToAbandon(LookupError):
    """Asked to give up on a proposal by a run with nothing to fall back to.

    Its own type because it names a better answer rather than a dead end: with no
    approved architecture the proposal is ordinary work that failed, and running
    it again is what fixes it.
    """


class NothingToRetry(LookupError):
    """Asked to run the failed work again on a run that has none.

    Its own type because the two ways to get here are different situations and
    both are ordinary: nothing has failed, or the work is already queued and
    coming. Neither is an error in the caller's request, and neither should read
    as one.
    """


@dataclass(frozen=True)
class CommandResult:
    """What every command returns: where the run is, and what may be done next.

    ``job`` is present exactly when the command deferred work to a worker, which
    is how a client knows whether to watch a stream or simply re-read the state.
    """

    project_id: str
    run_id: str
    state: WorkflowState
    available_actions: tuple[str, ...]
    job: Job | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingProfiles:
    """Which routing policy a project runs against, and what else it could.

    ``selected`` is ``None`` for the shipped default, and the default is not in
    ``available`` — it is what choosing nothing means, and listing it beside the
    named profiles would present "the default" and "openai" as the same kind of
    answer when one of them is currently the other.

    ``policy_version`` is the version string of the policy actually in force, so
    a screen can show what is running without loading the file a second time and
    reaching a different conclusion.
    """

    selected: str | None
    available: tuple[str, ...]
    policy_version: str


@dataclass(frozen=True)
class ExperimentReport:
    """One experiment as a client reads it: the arms, every result, the table.

    All three together, because plan/12 asks for *per-example results* and an
    *aggregate comparison* and an aggregate a reader cannot open into the runs
    behind it is a summary they have to take on trust.
    """

    experiment: models.ExperimentRun
    arms: tuple[ExperimentArm, ...]
    results: tuple[ExperimentResult, ...]
    comparison: tuple[ArmMetrics, ...]


@dataclass(frozen=True)
class Rerun:
    """A stage queued to run again, and the job that will do it."""

    source_execution_id: str
    job: Job


@dataclass(frozen=True)
class Resumed:
    """A run rebuilt from its stored position, ready to be commanded."""

    run: models.PipelineRun
    position: WorkflowPosition
    engine: WorkflowEngine
    context: PipelineContext


class ApplicationService:
    """Commands, in the vocabulary the API and CLI both speak."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    # ------------------------------------------------------------------
    # The unit of work
    # ------------------------------------------------------------------

    def commit(self) -> None:
        """Make this command's writes permanent.

        The service exposes the boundary but does not choose it. Where a
        transaction ends is the *caller's* question — one HTTP request, one CLI
        invocation, one worker job — and a service that committed inside every
        method would make it impossible for any of them to group two writes.
        """
        self._runtime.session.commit()

    def rollback(self) -> None:
        """Discard this command's writes.

        Used when a command failed before producing anything worth keeping.
        Provenance for work that *did* happen is committed by whoever recorded
        it (phase 03 writes failures rather than rolling them back), so this
        never discards the explanation of a failure.
        """
        self._runtime.session.rollback()

    # ------------------------------------------------------------------
    # Projects and sources
    # ------------------------------------------------------------------

    def create_project(
        self,
        *,
        title: str,
        author_id: str,
        constraints: EditorialConstraints,
        description: str = "",
    ) -> CommandResult:
        """Open a project, its bounds, its pipeline run and its position.

        All four together, because a project without them is not commandable:
        the constraints decide which providers may see the material, the run
        anchors every execution record, and the position is where the next
        request resumes from.

        The author row is created if it is missing. groundscribe is local-first
        and single-author by default (plan/00), so demanding a separate user
        registration step before a person can start writing would be ceremony
        with no decision behind it.
        """
        runtime = self._runtime
        session = runtime.session
        if session.get(domain_models.User, author_id) is None:
            session.add(
                domain_models.User(id=author_id, name=author_id, email=f"{author_id}@localhost")
            )
        project = domain_models.Project(
            id=uuid.uuid4().hex, user_id=author_id, title=title, description=description
        )
        session.add(project)
        session.flush()

        run = runtime.recorder.start_run(project_id=project.id)
        position = runtime.positions.open(run)
        session.add(
            domain_models.ProjectConstraints(
                id=uuid.uuid4().hex,
                project_id=project.id,
                **constraints.model_dump(mode="json"),
            )
        )
        session.flush()
        return CommandResult(
            project_id=project.id,
            run_id=run.id,
            state=position.state,
            available_actions=available_actions(position.state),
        )

    async def import_source(
        self,
        project_id: str,
        *,
        title: str,
        text: str,
        source_format: SourceFormat = SourceFormat.PLAIN_TEXT,
        confidential: bool = False,
        uri: str | None = None,
    ) -> CommandResult:
        """Parse, hash and store one piece of source material, in this request.

        No model is involved and the next command needs the result, so deferring
        it would add latency and a failure mode for nothing. The stage takes no
        workflow edge — a run is already in ``source_ingested`` before there is
        anything to ingest.
        """
        resumed = self._resume(project_id)
        await StageRunner(resumed.context).run(
            IngestSource(
                title=title,
                text=text,
                constraints=resumed.context.constraints,
                source_format=source_format,
                confidential=confidential,
                uri=uri,
            )
        )
        settled = self._settle(resumed)
        return self.advance(project_id) or settled

    # ------------------------------------------------------------------
    # Source model
    # ------------------------------------------------------------------

    async def extract_source_model(
        self, project_id: str, *, token_budget: int | None = None
    ) -> CommandResult:
        """Build the structured source model, and say what it could not answer."""
        return self._enqueue(
            project_id,
            JobType.EXTRACT_SOURCE_MODEL,
            entry=A.EXTRACT_SOURCE_MODEL,
            payload={"token_budget": token_budget},
        )

    def answer_gap(
        self,
        project_id: str,
        *,
        gap_id: str,
        text: str,
        answered_by: str,
        response: AnswerResponse = AnswerResponse.ANSWERED,
    ) -> CommandResult:
        """Record one answer, and leave the run where it is.

        An author works through the questions the way they were asked — several
        at a sitting, in whatever order the material comes back to them — and
        :meth:`submit_answers` is what hands the round back. Recording here took
        the ``answer_questions`` edge once, which meant the first answer left the
        state its own successors needed: the second was refused as out of order,
        by the run the author's own first answer had moved.

        Not a transition, so it carries its own gate. The question is still the
        engine's — *does this run offer* ``answer_questions`` — and asking it
        that way keeps a cancelled or finished run from accepting answers nothing
        will ever read.
        """
        resumed = self._resume(project_id)
        self._require_offered(resumed, A.ANSWER_QUESTIONS)
        gap = resumed.context.session.get(domain_models.SourceGap, gap_id)
        if gap is None:
            raise UnknownProject(f"no gap {gap_id} in project {project_id}")

        queue = open_question_queue(resumed.context)
        queue.respond(gap, response=response, text=text, answered_by=answered_by)
        self._runtime.recorder.complete_stage(queue.execution)
        return self._settle(resumed)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def retry_failed_job(self, project_id: str, *, requested_by: str) -> CommandResult:
        """Queue the work that failed under this run, again.

        The situation this exists for: a job fails, and the run is left in the
        ``-ing`` state whose *only* remaining edges belong to the pipeline. The
        pipeline is not coming — its job is the thing that failed — so without
        this the run is stranded, and the author's options are to cancel it and
        re-ingest, or to edit the database.

        Deliberately not a transition. The run is already in the state the failed
        job was carrying it out of, so recovery is *re-queueing the same work*,
        not negotiating a new position with the transition table. That keeps this
        one of the few commands that cannot leave the machine somewhere the table
        does not describe.

        Attributed, because it spends a model call. Refused while anything is
        still queued, because a second copy of work that is already coming is a
        duplicate, not a recovery.
        """
        if not requested_by:
            raise AttributionRequired("a retry is a person's decision to spend another call")

        resumed = self._resume(project_id)
        session = self._runtime.session
        outstanding = session.scalars(
            select(Job)
            .where(
                Job.pipeline_run_id == resumed.run.id,
                Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
            )
            .limit(1)
        ).first()
        if outstanding is not None:
            raise NothingToRetry(
                f"{outstanding.job_type} is already {outstanding.status.value} for this run"
            )

        # Scoped to the work this state is waiting for, not merely to the newest
        # failure. A run carries its failures with it: something that failed an
        # hour ago, was fixed, and has since been superseded is still the newest
        # failed row, and retrying it re-runs a stage the run has finished with.
        #
        # Seen on a real run parked in `substantive_rewriting` whose newest failed
        # job was a `plan_revision` from an hour before — already succeeded on a
        # later attempt. The offer read as "run that step again" and would have
        # re-planned a revision that was already planned and approved.
        step = next_step(resumed.engine.state)
        if step is None:
            raise NothingToRetry(
                f"{resumed.engine.state.value} is not waiting on work to run; "
                "nothing here can be run again"
            )

        failed = session.scalars(
            select(Job)
            .where(
                Job.pipeline_run_id == resumed.run.id,
                Job.status == JobStatus.FAILED,
                Job.job_type == step.job_type,
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
        ).first()
        if failed is None:
            raise NothingToRetry(
                f"no failed {step.job_type.value} to run again in project {project_id}"
            )

        # The payload as well as the type: a job re-queued without the budget or
        # the options it was given is a different job wearing the same name.
        job = self._runtime.queue.enqueue(
            job_type=JobType(failed.job_type),
            run=resumed.run,
            payload=dict(failed.payload or {}),
            supersede=True,
        )
        self._runtime.recorder.record_user_intervention(
            resumed.engine.execution,
            user_id=requested_by,
            intervention_type=InterventionType.RETRY,
            payload={"job_id": job.id, "retried_job_id": failed.id, "job_type": failed.job_type},
        )
        return self._settle(resumed, job=job)

    def abandon_proposal(self, project_id: str, *, requested_by: str) -> CommandResult:
        """Give up on the architecture proposal in flight; keep the approved one.

        The one failure :meth:`retry_failed_job` cannot recover. Running a
        proposal again is the right answer while nothing is approved, but a
        proposal that lands over an approved architecture is refused by
        :meth:`WorkflowEngine._guard_architecture`, which wants lineage from the
        approved snapshot *and* an override naming who authorised superseding it.
        A retry arrives at the same refusal, and the state it fails in offers
        nothing else — so before this, such a run could only be cancelled.

        Refused when nothing is approved, because then this is the wrong tool:
        there is no architecture to fall back to, the proposal is ordinary work
        that failed, and a retry is what will fix it. Saying so is more use than
        moving the run somewhere it cannot proceed from either.

        Attributed, because giving up on work is a decision and the record should
        name who made it.

        Does not advance afterwards, which every other person's action does.
        ``architecture_approved`` is a state whose next step is generating the
        brief, and that is right for a run arriving there for the first time —
        but a run arriving here is recovering from a failure, and it has a brief
        and probably a draft already. Somebody who has just said "give up on
        this" has not said "start the next thing", and rewriting their brief
        because the state machine has one memory for the whole project would be
        answering a question they did not ask.
        """
        if not requested_by:
            raise AttributionRequired("abandoning a proposal is a person's decision")

        resumed = self._resume(project_id)
        if resumed.engine.approved_architecture is None:
            raise NothingToAbandon(
                f"project {project_id} has no approved architecture to fall back to; "
                "this proposal is ordinary work that failed, so run it again instead"
            )

        self._runtime.recorder.record_user_intervention(
            resumed.engine.execution,
            user_id=requested_by,
            intervention_type=InterventionType.OVERRIDE,
            payload={"abandoned": "architecture_proposal"},
        )
        resumed.engine.apply(A.ABANDON_PROPOSAL, actor_id=requested_by, actor_type=ActorType.USER)
        return self._settle(resumed)

    def submit_answers(self, project_id: str, *, submitted_by: str) -> CommandResult:
        """Hand the answered round back to the pipeline, rebuilding the source model.

        One edge for however many answers were recorded, because the rebuild
        reads all of them (:func:`~groundscribe.app.rehydrate.open_answers`).
        Rebuilding per answer would have spent a model call on each, and every
        one but the last on a source model the author's next answer contradicted.

        The rebuild *supersedes* any extraction still queued rather than joining
        it, for the same reason: an extraction queued before this round was
        submitted is answering an older question.
        """
        resumed = self._resume(project_id)
        queue = open_question_queue(resumed.context)
        queue.submit(submitted_by=submitted_by)
        self._runtime.recorder.complete_stage(queue.execution)

        job = self._runtime.queue.enqueue(
            job_type=JobType.EXTRACT_SOURCE_MODEL,
            run=resumed.run,
            payload={"token_budget": None},
            supersede=True,
        )
        return self._settle(resumed, job=job)

    # ------------------------------------------------------------------
    # Architecture
    # ------------------------------------------------------------------

    async def propose_architecture(self, project_id: str) -> CommandResult:
        """Propose how the source becomes an article or a series."""
        return self._enqueue(
            project_id, JobType.PROPOSE_ARCHITECTURE, entry=A.PROPOSE_ARCHITECTURE, payload={}
        )

    def update_architecture(
        self,
        project_id: str,
        *,
        commands: Sequence[Mapping[str, Any]],
        requested_by: str,
        reason: str = "",
        accepted_warnings: Sequence[str] = (),
    ) -> CommandResult:
        """Commit an author's edits as a new version of the architecture.

        Runs here rather than in a worker: applying overrides is deterministic
        (plan/06), and the author is waiting to see what their edit did.
        """
        resumed = self._resume(project_id)
        architecture, snapshot, proposal = self._current_architecture(resumed)
        result = override_architecture(
            resumed.context,
            architecture=architecture,
            proposal=proposal,
            snapshot=snapshot,
            commands=[OverrideCommand.model_validate(command) for command in commands],
            requested_by=requested_by,
            reason=reason,
            accepted_warnings=list(accepted_warnings),
        )
        return self._settle(
            resumed,
            detail={"warnings": [warning.model_dump(mode="json") for warning in result.warnings]},
        )

    def approve_architecture(self, project_id: str, *, approved_by: str) -> CommandResult:
        """Lock the proposed architecture; from here a change must be authorised."""
        resumed = self._resume(project_id)
        architecture, snapshot, _ = self._current_architecture(resumed)
        approve_architecture(
            resumed.context,
            architecture=architecture,
            snapshot=snapshot,
            approved_by=approved_by,
        )
        self._open_articles(resumed, architecture)
        settled = self._settle(resumed)
        return self.advance(project_id) or settled

    # ------------------------------------------------------------------
    # Article stages
    # ------------------------------------------------------------------

    async def generate_brief(self, article_id: str) -> CommandResult:
        """Turn one approved concept into the contract its draft is written to."""
        return self._enqueue_for_article(article_id, JobType.GENERATE_BRIEF, entry=A.GENERATE_BRIEF)

    def approve_brief(self, article_id: str, *, approved_by: str) -> CommandResult:
        """Accept the brief, which is what opens drafting."""
        return self._act(
            self.project_for_article(article_id), A.APPROVE_BRIEF, actor_id=approved_by
        )

    async def draft(self, article_id: str) -> CommandResult:
        """Write the first version of the article."""
        return self._enqueue_for_article(article_id, JobType.GENERATE_DRAFT)

    async def review(self, article_id: str) -> CommandResult:
        """Review the current version against the source and the brief."""
        return self._enqueue_for_article(article_id, JobType.REVIEW_ARTICLE)

    def decide_finding(
        self,
        article_id: str,
        *,
        finding_id: str,
        decision: FindingStatus,
        decided_by: str,
        reason: str = "",
        recommended_correction: str = "",
    ) -> CommandResult:
        """Accept, reject or edit one of the review's findings.

        The step the pipeline could not take for itself, and until now could not
        be taken at all: :class:`~groundscribe.stages.review.ReviewLedger` did the
        work and nothing outside the tests could reach it.

        What that cost is worth stating, because it is not obvious from any one
        stage. A finding reaches a revision plan only when it is ``accepted`` or
        ``edited``; everything arrives ``proposed``. With no way to decide one,
        every plan was built from an empty set — and a plan with nothing to do
        satisfies ``check_plan``, and a rewrite that applies nothing satisfies
        ``check_rewrite``. So the revision loop ran green and returned the article
        unchanged, which is the one failure mode worse than an error.

        Moves the run nowhere. Deciding is bookkeeping about a review that has
        already happened, and the run is parked where the review left it.

        Addressed by the finding's own id rather than by its ``ref`` and the
        article's newest review, which is what this did first and got wrong twice
        over. A ``ref`` is unique within a review and not across them — two rounds
        both number their findings from one — and the newest *version* need not be
        the reviewed one: a rewrite produces a version that no review has seen,
        so looking one up there fails with "has not been reviewed" while the
        finding sits in plain sight on the screen that offered the button.
        """
        resumed = self._resume(self.project_for_article(article_id))
        session = resumed.context.session

        finding = session.get(domain_models.ReviewIssue, finding_id)
        if finding is None or self._article_of(finding) != article_id:
            raise UnknownFinding(f"article {article_id} has no finding {finding_id}")

        ledger = open_review_ledger(resumed.context)
        if decision is FindingStatus.ACCEPTED:
            ledger.accept(finding, decided_by=decided_by)
        elif decision is FindingStatus.REJECTED:
            ledger.reject(finding, decided_by=decided_by, reason=reason)
        elif decision is FindingStatus.EDITED:
            ledger.edit(
                finding,
                decided_by=decided_by,
                recommended_correction=recommended_correction,
                reason=reason,
            )
        else:
            raise UndecidableFinding(
                f"{decision.value} is not a decision a person makes; "
                "a finding is accepted, rejected, or accepted with an edit"
            )
        self._runtime.recorder.complete_stage(ledger.execution)
        return self._settle(resumed, detail={"ref": finding.ref, "status": decision.value})

    def _article_of(self, finding: domain_models.ReviewIssue) -> str | None:
        """Which article a finding belongs to, so one cannot be decided from another."""
        session = self._runtime.session
        review = session.get(domain_models.Review, finding.review_id)
        if review is None:
            return None
        version = session.get(domain_models.ArticleVersion, review.article_version_id)
        return version.article_id if version is not None else None

    async def plan_revision(self, article_id: str) -> CommandResult:
        """Turn the review's findings into a plan a rewrite is bound by."""
        return self._enqueue_for_article(article_id, JobType.PLAN_REVISION)

    def approve_revision_plan(self, article_id: str, *, approved_by: str) -> CommandResult:
        """Authorise the rewrite the plan describes.

        Refused when there is no plan to describe it. ``revision_plan_required``
        means "a plan is expected here", not "a plan is here": the pipeline
        writes one *into* this state, so the approval edge is legal from the
        moment the run arrives and long before there is anything to approve.

        Approving nothing used to move the run to ``substantive_rewriting``
        anyway, where the rewrite failed for want of the plan it was told had
        been approved. Observed on a real run, and the message it produced —
        "review d263be31 has no revision plan" — named the missing artefact two
        steps after the click that lost it.

        The message says what to do instead, because the usual reason a plan is
        missing is that nobody has been through the findings yet.
        """
        session = self._runtime.session
        version = rehydrate.latest_version(session, article_id)
        review = rehydrate.latest_review(session, version.id)
        if not self._revision_plan_exists(article_id):
            undecided = [
                issue.ref for issue in review.issues if issue.status is FindingStatus.PROPOSED
            ]
            raise NothingToApprove(
                f"there is no revision plan for review {review.id} to approve"
                + (
                    f"; decide its findings first ({', '.join(sorted(undecided))}) "
                    "and the plan will be written from the ones you accept"
                    if undecided
                    else " — plan the revision first"
                )
            )
        return self._act(
            self.project_for_article(article_id), A.APPROVE_REVISION_PLAN, actor_id=approved_by
        )

    async def rewrite(self, article_id: str) -> CommandResult:
        """Rewrite the article under the approved plan."""
        return self._enqueue_for_article(article_id, JobType.REWRITE_ARTICLE)

    async def voice_align(self, article_id: str) -> CommandResult:
        """Make it read like the author, changing nothing it claims."""
        return self._enqueue_for_article(article_id, JobType.ALIGN_VOICE)

    async def score(self, article_id: str) -> CommandResult:
        """Score the article, and route it if it fails."""
        return self._enqueue_for_article(article_id, JobType.SCORE_ARTICLE)

    async def validate(self, article_id: str) -> CommandResult:
        """Run the final deterministic checks, in this request.

        plan/08 built final validation with no model at all — fourteen
        predicates over stored artefacts. Nothing about it benefits from a
        worker, and a person waiting to publish benefits from the answer now.
        """
        resumed = self._resume(self.project_for_article(article_id))
        session = resumed.context.session
        version = rehydrate.latest_version(session, article_id)
        version_snapshot = rehydrate.snapshot_of(session, version.snapshot_id)
        brief_snapshot = rehydrate.require_snapshot(
            session, resumed.run, ArtifactType.ARTICLE_BRIEF
        )
        source_snapshot = rehydrate.require_snapshot(
            session, resumed.run, ArtifactType.SOURCE_MODEL
        )

        resumed.engine.apply(A.VALIDATE_FINAL, actor_id=self._runtime.actor_id)
        result = await StageRunner(resumed.context).run(
            ValidateFinalOutput(
                draft=rehydrate.article_document(self._runtime.snapshots, version_snapshot),
                version=version,
                version_snapshot=version_snapshot,
                # The version that passed review is the one being validated: the
                # engine holds it, and check fourteen is precisely that the two
                # are the same artefact (plan/08).
                passed_version=version,
                brief=rehydrate.document(
                    self._runtime.snapshots, brief_snapshot, ArticleBriefDocument
                ),
                source_model=rehydrate.document(
                    self._runtime.snapshots, source_snapshot, SourceModel
                ),
                # Both lists, because they forbid words for different reasons and
                # a term is either wanted in the prose or it is not. The voice
                # profile's half was being dropped here (phase 16): its
                # ``prohibited_terms`` property exists, its docstring says it
                # serves the voice pass *and* final validation, and only the
                # first of those was ever asking.
                prohibited_terms=(
                    *resumed.context.constraints.confidential_names,
                    *self._voice_for(resumed, article_id).prohibited_terms,
                ),
            ),
            enter=False,
        )
        return self._settle(resumed, detail=dict(result.detail))

    def approve(self, article_id: str, *, approved_by: str) -> CommandResult:
        """The author publishes: the last human gate in the pipeline."""
        resumed = self._resume(self.project_for_article(article_id))
        validated = resumed.engine.validated_version
        resumed.engine.apply(
            A.APPROVE_FINAL,
            actor_id=approved_by,
            actor_type=ActorType.USER,
            artifacts=(validated,) if validated is not None else (),
            rationale="the author approved the validated article",
        )
        return self._settle(resumed)

    def approve_and_continue(
        self, article_id: str, *, approved_by: str, next_article_id: str
    ) -> CommandResult:
        """Publish this article, then start another the architecture approved.

        Approving an architecture opens an article per approved concept and the
        run carries exactly one of them here, so without this the rest were rows
        nothing could act on (phase 16).

        ``next_article_id`` is named rather than inferred. Auto-advance picks the
        article the *architecture* selected, which is the one just finished, so
        letting it choose would restart the article being left behind. Which of
        the remaining concepts is worth writing is the author's judgement and
        there is nothing in the run that encodes it.
        """
        project_id = self.project_for_article(article_id)
        if self.project_for_article(next_article_id) != project_id:
            raise UnknownProject(
                f"article {next_article_id} belongs to another project; a run writes the "
                "articles its own architecture approved"
            )

        resumed = self._resume(project_id)
        validated = resumed.engine.validated_version
        resumed.engine.apply(
            A.APPROVE_AND_CONTINUE,
            actor_id=approved_by,
            actor_type=ActorType.USER,
            artifacts=(validated,) if validated is not None else (),
            rationale="the author approved this article and chose another to write",
        )
        self._settle(resumed)
        return self._enqueue_for_article(
            next_article_id, JobType.GENERATE_BRIEF, entry=A.GENERATE_BRIEF
        )

    def revise(
        self,
        article_id: str,
        *,
        requested_by: str,
        prefer: WorkflowState | None = None,
    ) -> CommandResult:
        """Send a failed score to the stage that can correct it.

        The run parks at ``revision_required`` when a score fails, and that pause
        is deliberate: it is where a person may accept the article anyway, or go
        and supply what the score said was missing. Routing on the way out of
        scoring would step past it every time (see
        :func:`~groundscribe.scoring.loop.route_score`).

        What was missing is the person's way *onward* from that pause. Only
        ``override_and_approve`` left it, so declining to override meant the run
        stayed there — the routing policy, its seven destinations and the whole
        failure vocabulary were reachable from nothing but a test.

        The category is read from the evaluation rather than recomputed. It was
        decided when the score was made, by the scorer that saw every deduction,
        and a second derivation here would be a second opinion about a decision
        already recorded.

        ``prefer`` chooses between the destinations that category already
        permits, and cannot invent one — the policy refuses a state it does not
        list. It exists because the right answer differs while the category does
        not: a factual gap whose facts the author *has* is corrected by
        re-extracting, and one whose facts nobody has ever written down is
        corrected by asking them. Both are ``factual_gap``, and re-extracting the
        same source for the second is a loop.
        """
        resumed = self._resume(self.project_for_article(article_id))
        evaluation = self._runtime.session.scalars(
            select(models.EvaluationRun)
            .join(
                models.StageExecution,
                models.StageExecution.id == models.EvaluationRun.stage_execution_id,
            )
            .where(models.StageExecution.pipeline_run_id == resumed.run.id)
            .order_by(models.EvaluationRun.created_at.desc())
        ).first()
        routed_as = evaluation.scores.get("routed_as") if evaluation is not None else None
        if not routed_as:
            # A failing score is the usual reason to be here and not the only
            # one: a voice pass that refuses to fix a fault routes here too, and
            # it has no evaluation behind it. Its structural problems carry a
            # `suggested_route` in the same vocabulary, which is what makes them
            # routable rather than merely reportable.
            routed_as = self._blocked_voice_route(resumed)
        if not routed_as:
            raise NothingToRevise(
                f"article {article_id} has nothing to route: no failing score, and no voice "
                "pass that stopped on something it would not fix"
            )

        resumed.engine.route(
            FailureCategory(routed_as),
            prefer=prefer,
            evidence={
                "evaluation_id": evaluation.id if evaluation is not None else None,
                "overall": evaluation.scores.get("overall") if evaluation is not None else None,
                "requested_by": requested_by,
                "preferred": prefer.value if prefer is not None else None,
                "failures": [
                    failure.get("detail")
                    for failure in (evaluation.scores.get("failures", []) if evaluation else [])
                    if isinstance(failure, dict)
                ],
            },
        )
        settled = self._settle(resumed)
        return self.advance(resumed.context.project_id) or settled

    def _blocked_voice_route(self, resumed: Resumed) -> str | None:
        """The route a voice pass asked for, if a voice pass is why we are here.

        Read from the decision the stage already records, rather than from a
        second store: refusing to fix something is a policy decision and is
        written down as one. The first problem's route is taken because the pass
        reports them in the order it met them, and a pass that found several is
        describing one article rather than several destinations.

        Only when the run's *last* transition was the voice pass stopping.
        ``revision_required`` is reached four ways — a failing score, a blocked
        voice pass, failed validation, and an author rejecting a finished article
        — and only the second has no evaluation to route from. Without this
        check, the other three would be routed on whatever a voice pass
        complained about earlier in the run, which on a real project meant an
        hour-old style objection deciding where a rejected article went.
        """
        latest = self._runtime.session.scalars(
            select(models.DecisionRecord)
            .join(
                models.StageExecution,
                models.StageExecution.id == models.DecisionRecord.stage_execution_id,
            )
            .where(
                models.StageExecution.pipeline_run_id == resumed.run.id,
                models.DecisionRecord.decision_type == "workflow_transition",
            )
            .order_by(models.DecisionRecord.decided_at.desc())
        ).first()
        if latest is None or latest.outcome != WorkflowState.REVISION_REQUIRED.value:
            return None
        if latest.inputs.get("action") != WorkflowAction.VOICE_BLOCKED.value:
            return None

        record = self._runtime.session.scalars(
            select(models.DecisionRecord)
            .join(
                models.StageExecution,
                models.StageExecution.id == models.DecisionRecord.stage_execution_id,
            )
            .where(
                models.StageExecution.pipeline_run_id == resumed.run.id,
                models.DecisionRecord.decision_type == "voice_structural_return",
            )
            .order_by(models.DecisionRecord.decided_at.desc())
        ).first()
        if record is None:
            return None
        problems = record.inputs.get("problems") or []
        for problem in problems:
            route = problem.get("suggested_route") if isinstance(problem, dict) else None
            if route:
                return str(route)
        return None

    def override_and_approve(self, article_id: str, *, approved_by: str) -> CommandResult:
        """Accept an article the score refused, on a person's explicit say-so."""
        return self._act(
            self.project_for_article(article_id), A.OVERRIDE_AND_APPROVE, actor_id=approved_by
        )

    def cancel(self, project_id: str, *, cancelled_by: str) -> CommandResult:
        """Stop the run. Always available, so no run can be trapped."""
        return self._act(project_id, A.CANCEL, actor_id=cancelled_by)

    # ------------------------------------------------------------------
    # Voice (phase 10)
    # ------------------------------------------------------------------

    def save_voice_profile(
        self,
        document: VoiceProfileDocument,
        *,
        user_id: str,
        project_id: str | None = None,
        article_id: str | None = None,
    ) -> VoiceProfileVersion:
        """Put a profile version in force at its scope."""
        return self._voice().save(
            document, user_id=user_id, project_id=project_id, article_id=article_id
        )

    def voice_profiles(self, *, user_id: str) -> tuple[VoiceProfileVersion, ...]:
        """Every version this author has saved, in force or superseded."""
        return self._voice().versions(user_id=user_id)

    def effective_voice(
        self, *, user_id: str, project_id: str | None = None, article_id: str | None = None
    ) -> ResolvedVoice:
        """The voice in force here, with the source of each instruction."""
        return self._voice().resolve(user_id=user_id, project_id=project_id, article_id=article_id)

    def voice_suggestions(self, *, user_id: str) -> tuple[VoiceSuggestion, ...]:
        """Inferred rules still waiting for an answer."""
        return self._learning().open_suggestions(user_id=user_id)

    def approve_voice_suggestion(
        self, suggestion_id: str, *, approved_by: str, version: str
    ) -> VoiceProfileVersion:
        """Make an inferred rule permanent, and save the version it produces.

        The only path through this service that changes a voice. It takes an
        approver, and it writes a new version rather than editing one — the two
        properties plan/10 asks for, kept together so neither can be satisfied
        without the other.
        """
        learning = self._learning()
        suggestion = self._suggestion(suggestion_id)
        current = self.effective_voice(user_id=suggestion.user_id).profile
        stored = self._voice()
        active = stored.versions(user_id=suggestion.user_id)
        base = stored.document(active[-1]) if active else current
        updated = learning.approve(
            suggestion, profile=base, approved_by=approved_by, version=version
        )
        saved = stored.save(updated, user_id=suggestion.user_id)
        suggestion.resulting_version_id = saved.id
        self._runtime.session.flush()
        return saved

    def reject_voice_suggestion(
        self, suggestion_id: str, *, rejected_by: str, reason: str = ""
    ) -> VoiceSuggestion:
        """Record that the author said no, and why."""
        suggestion = self._suggestion(suggestion_id)
        self._learning().reject(suggestion, rejected_by=rejected_by, reason=reason)
        return suggestion

    def _voice(self) -> VoiceStore:
        return VoiceStore(
            self._runtime.session,
            snapshots=self._runtime.snapshots,
            recorder=self._runtime.recorder,
        )

    def _learning(self) -> VoiceLearning:
        return VoiceLearning(self._runtime.session, recorder=self._runtime.recorder)

    def _suggestion(self, suggestion_id: str) -> VoiceSuggestion:
        suggestion = self._runtime.session.get(VoiceSuggestion, suggestion_id)
        if suggestion is None:
            raise UnknownProject(f"no voice suggestion {suggestion_id}")
        return suggestion

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def project_state(self, project_id: str) -> CommandResult:
        """Where a run is and what may be done to it, without changing anything."""
        run = self._run_for(project_id)
        position = self._position_for(run)
        return CommandResult(
            project_id=project_id,
            run_id=run.id,
            state=position.state,
            available_actions=available_actions(position.state),
        )

    # ------------------------------------------------------------------
    # Privacy and export (phase 13)
    # ------------------------------------------------------------------

    def render_version(self, version_id: str, fmt: ExportFormat) -> ExportedArticle:
        """One stored article version, rendered in a named format.

        Addressed by *snapshot*, not by article: what a person exports is the
        version that passed validation, and naming it by id is what makes an
        export of the wrong one impossible rather than merely unlikely.
        """
        snapshot = self._runtime.session.get(ArtifactSnapshot, version_id)
        if snapshot is None:
            raise UnknownProject(f"no article version {version_id}")
        return render_article(self._runtime.snapshots, snapshot, fmt)

    def provider_visibility(self, project_id: str) -> ProviderVisibility:
        """Where this project's material goes, and what is kept of it."""
        return provider_visibility(
            self._runtime.session,
            project_id,
            constraints=rehydrate.constraints(self._runtime.session, project_id),
            # This project's policy, not the process's. The screen exists to
            # answer "where does my material go", and answering it from the
            # default file would describe some other project's providers to
            # whoever had just moved theirs.
            routing=generator_for(self._runtime, project_id).routing,
        )

    def set_routing_profile(
        self, project_id: str, *, profile: str | None, chosen_by: str
    ) -> CommandResult:
        """Point this project's stages at a routing profile, or back at the default.

        The situation this exists for: a policy that suits the installation does
        not suit one project. A source too long for a local model's context
        window is the usual way to find out, and before this the options were to
        edit the shipped config — moving every project — or to leave the run
        stranded on a stage that cannot fit its own input.

        Takes effect on the next stage to run, and deliberately not on the ones
        already recorded. Each ``StageExecution`` names the ``policy_version`` it
        ran under, so a run that changes profile half way through stays
        readable — it says so, per stage, which is what a mixed run actually was.

        Attributed, because it decides what this project's material is sent to
        and what its calls cost. Refused for a profile with no file, so the
        failure lands here, on the person who can fix it, rather than on the
        worker three stages later.

        Not a transition: the run does not move, and a project with no run at all
        can still be pointed somewhere. What comes back is where the run already
        was.
        """
        if not chosen_by:
            raise AttributionRequired("a routing profile decides where material is sent")

        project = self._runtime.session.get(domain_models.Project, project_id)
        if project is None:
            raise UnknownProject(f"no project {project_id}")

        chosen = profile or None
        if chosen is not None:
            # Loaded, not merely looked for: a file that exists and does not
            # parse is the same problem to the person choosing, and finding out
            # at the next model call would be finding out somewhere else.
            routing_policy(chosen)

        previous = project.routing_profile
        project.routing_profile = chosen
        self._runtime.session.flush()

        resumed = self._resume(project_id)
        self._runtime.recorder.record_user_intervention(
            resumed.engine.execution,
            user_id=chosen_by,
            intervention_type=InterventionType.OVERRIDE,
            payload={"routing_profile": chosen or "", "previous_routing_profile": previous or ""},
        )
        return self._settle(resumed)

    def subscription_usage(self) -> tuple[QuotaWindow, ...]:
        """What the subscription providers have consumed, per rolling window.

        Installation-wide rather than per project, because that is the shape of
        the thing being measured: a plan's rate limit is one bucket, and a run
        that exhausts it does so regardless of which project asked.
        """
        return subscription_usage(self._runtime.session)

    def routing_profiles(self, project_id: str) -> RoutingProfiles:
        """What this project runs against, and what else it could."""
        project = self._runtime.session.get(domain_models.Project, project_id)
        if project is None:
            raise UnknownProject(f"no project {project_id}")
        return RoutingProfiles(
            selected=project.routing_profile,
            available=available_profiles(),
            policy_version=generator_for(self._runtime, project_id).routing.version,
        )

    def export_traces(
        self,
        project_id: str,
        *,
        sanitise: bool = False,
        confidential_material_acknowledged: bool = False,
    ) -> TraceExport:
        """This project's execution records, as a document.

        The acknowledgement is passed straight through rather than defaulted
        here: the refusal only means anything if the caller has to say, in the
        call, that it intends to carry confidential material out.
        """
        return export_traces(
            self._runtime.session,
            self._runtime.snapshots,
            project_id,
            sanitise=sanitise,
            confidential_material_acknowledged=confidential_material_acknowledged,
        )

    def storage_report(self, project_id: str | None = None) -> StorageReport:
        """What the stored artefacts come to, broken down by kind."""
        return storage_report(self._runtime.session, project_id=project_id)

    def metrics(self, project_id: str | None = None) -> RunMetrics:
        """What this project — or the whole installation — has done and spent.

        Read straight through to the collector with nothing in between: a
        service that rounded a rate or defaulted a null to zero would make two
        answers to the same question, and an operator who notices that once
        stops trusting either (plan/14).
        """
        return collect_metrics(self._runtime.session, project_id=project_id)

    def delete_traces(self, project_id: str) -> TraceDeletion:
        """Drop this project's stored payloads, keeping what ran."""
        return delete_traces(self._runtime.session, self._runtime.snapshots, project_id)

    def get_execution(self, execution_id: str) -> models.StageExecution:
        execution = self._runtime.session.get(models.StageExecution, execution_id)
        if execution is None:
            raise UnknownProject(f"no execution {execution_id}")
        return execution

    def execution_events(self, execution_id: str) -> tuple[models.TraceEvent, ...]:
        """This execution's timeline, in order."""
        return tuple(self.get_execution(execution_id).trace_events)

    def execution_invocations(self, execution_id: str) -> tuple[models.ModelInvocation, ...]:
        """Every model call the execution made, failed attempts included."""
        return tuple(self.get_execution(execution_id).model_invocations)

    def compare_executions(
        self, left_id: str, right_id: str
    ) -> tuple[models.StageExecution, models.StageExecution]:
        """The two executions a comparison is drawn between.

        Deliberately thin: phase 12 owns what a comparison *says*. The endpoint
        exists here so the contract is stable for the frontend, and returning the
        pair keeps this from pretending to an answer it does not have yet.
        """
        return self.get_execution(left_id), self.get_execution(right_id)

    def replay_execution(self, execution_id: str, *, requested_by: str) -> Rerun:
        """Queue the stage again, exactly as it ran (phase 12)."""
        return self.fork_execution(execution_id, requested_by=requested_by)

    def fork_execution(
        self,
        execution_id: str,
        *,
        requested_by: str,
        variables: ForkVariables | None = None,
        reason: str = "",
    ) -> Rerun:
        """Queue the stage again, with whatever this fork changes (phase 12).

        One mechanism for both: a fork is a replay that carries variables, and a
        replay is a fork that carries none. The work goes to the worker like all
        model work, so the answer is a job — the execution it opens is linked to
        the original, and the job names it as soon as there is one.
        """
        execution = self.get_execution(execution_id)
        instructions = ExperimentRerun(
            source_execution_id=execution.id,
            requested_by=requested_by,
            reason=reason,
            variables=variables or ForkVariables(),
        )
        job_type, payload = plan_rerun(self._runtime.session, execution, instructions)
        job = self._runtime.queue.enqueue(
            job_type=job_type,
            run=execution.pipeline_run,
            payload=payload,
            # Keyed by the source execution, so asking twice while the first is
            # still running hands back the run already happening rather than
            # queueing a second identical one.
            dedupe_key=f"rerun:{execution.id}",
        )
        return Rerun(source_execution_id=execution.id, job=job)

    def job(self, job_id: str) -> Job:
        job = self._runtime.queue.get(job_id)
        if job is None:
            raise UnknownProject(f"no job {job_id}")
        return job

    # ------------------------------------------------------------------
    # Experimentation (phase 12)
    # ------------------------------------------------------------------

    def build_dataset(
        self,
        *,
        name: str,
        created_by: str,
        description: str = "",
        include_sensitive: Sequence[str] = (),
    ) -> EvaluationDataset:
        """Build an evaluation corpus out of the runs a person approved."""
        return self._datasets().build(
            name=name,
            created_by=created_by,
            description=description,
            include_sensitive=include_sensitive,
        )

    def datasets(self) -> tuple[EvaluationDataset, ...]:
        """Every corpus built so far, oldest first."""
        return tuple(
            self._runtime.session.scalars(
                select(EvaluationDataset).order_by(
                    EvaluationDataset.created_at, EvaluationDataset.id
                )
            )
        )

    def dataset(self, dataset_id: str) -> EvaluationDataset:
        dataset = self._runtime.session.get(EvaluationDataset, dataset_id)
        if dataset is None:
            raise UnknownProject(f"no evaluation dataset {dataset_id}")
        return dataset

    def create_experiment(
        self,
        *,
        name: str,
        dataset_id: str,
        created_by: str,
        arms: Sequence[ArmSpec],
        description: str = "",
    ) -> models.ExperimentRun:
        """Open an experiment over one corpus, with the configurations to compare."""
        return self._experiments().create(
            name=name,
            dataset=self.dataset(dataset_id),
            created_by=created_by,
            arms=arms,
            description=description,
        )

    def start_experiment(self, experiment_id: str) -> tuple[ExperimentResult, ...]:
        """Queue every arm against every example.

        The work goes to the worker like all model work, so this returns the
        pending results rather than the answer: an experiment over a corpus is
        the longest-running thing this system does, and a request that waited for
        it would be a request that times out.
        """
        return self._experiments().start(self.experiment(experiment_id))

    def experiment(self, experiment_id: str) -> models.ExperimentRun:
        experiment = self._runtime.session.get(models.ExperimentRun, experiment_id)
        if experiment is None:
            raise UnknownProject(f"no experiment {experiment_id}")
        return experiment

    def experiment_report(self, experiment_id: str) -> ExperimentReport:
        """One experiment, its per-example results and the aggregate table."""
        experiment = self.experiment(experiment_id)
        runner = self._experiments()
        runner.collect(experiment)
        return ExperimentReport(
            experiment=experiment,
            arms=runner.arms(experiment),
            results=runner.results(experiment),
            comparison=runner.compare(experiment),
        )

    def prefer_arm(
        self,
        experiment_id: str,
        *,
        entry_id: str,
        arm_id: str,
        decided_by: str,
        reason: str = "",
    ) -> ExperimentPreference:
        """Record which arm a person judged better on one example."""
        session = self._runtime.session
        entry = session.get(EvaluationDatasetEntry, entry_id)
        arm = session.get(ExperimentArm, arm_id)
        if entry is None:
            raise UnknownProject(f"no dataset entry {entry_id}")
        if arm is None:
            raise UnknownArm(f"no experiment arm {arm_id}")
        return self._experiments().prefer(
            self.experiment(experiment_id),
            entry=entry,
            arm=arm,
            decided_by=decided_by,
            reason=reason,
        )

    def _datasets(self) -> DatasetBuilder:
        return DatasetBuilder(
            self._runtime.session,
            snapshots=self._runtime.snapshots,
            clock=self._runtime.clock,
        )

    def _experiments(self) -> ExperimentRunner:
        return ExperimentRunner(
            self._runtime.session,
            queue=self._runtime.queue,
            snapshots=self._runtime.snapshots,
            clock=self._runtime.clock,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def author_of(self, project_id: str) -> str:
        """Whose voice a project is written in.

        A lookup, like ``project_for_article``: the API addresses a voice by the
        project it applies to, while profiles belong to a person, and something
        has to join the two in one place rather than in each interface.
        """
        project = self._runtime.session.get(domain_models.Project, project_id)
        if project is None:
            raise UnknownProject(f"no project {project_id}")
        return project.user_id

    def project_for_article(self, article_id: str) -> str:
        """Which project an article belongs to.

        A lookup, not a rule: the spec addresses article commands as
        ``/articles/{id}/...`` while the workflow is per-run, so something has to
        join the two. Doing it here rather than in each interface keeps the CLI
        and the API asking the same question of the same table.
        """
        article = self._runtime.session.get(domain_models.Article, article_id)
        if article is None:
            raise UnknownProject(f"no article {article_id}")
        return article.project_id

    def _enqueue_for_article(
        self,
        article_id: str,
        job_type: JobType,
        *,
        entry: WorkflowAction | None = None,
    ) -> CommandResult:
        """Queue article work, keyed so two articles of one project stay apart."""
        return self._enqueue(
            self.project_for_article(article_id),
            job_type,
            entry=entry,
            payload={"article_id": article_id},
            dedupe_key=f"{job_type.value}:{article_id}",
        )

    def _enqueue(
        self,
        project_id: str,
        job_type: JobType,
        *,
        entry: WorkflowAction | None,
        payload: Mapping[str, Any],
        dedupe_key: str | None = None,
    ) -> CommandResult:
        """Take the entry edge, then hand the model work to a worker.

        The duplicate check comes *first*, and has to: the entry edge has already
        been taken for work that is already queued, so re-taking it would be an
        illegal transition out of the very state it produced. A second click
        therefore changes nothing and is handed the job to watch — which is the
        honest answer, since the work it asked for is already happening.
        """
        resumed = self._resume(project_id)
        key = dedupe_key or f"{resumed.run.id}:{job_type.value}"
        active = self._runtime.queue.active(key)
        if active is not None:
            return self._settle(resumed, job=active)

        if entry is not None:
            resumed.engine.apply(entry, actor_id=self._runtime.actor_id)
        job = self._runtime.queue.enqueue(
            job_type=job_type,
            run=resumed.run,
            payload=dict(payload),
            dedupe_key=key,
        )
        return self._settle(resumed, job=job)

    def advance(self, project_id: str) -> CommandResult | None:
        """Start the work this run is parked waiting for, if nobody need be asked.

        ``None`` when there is nothing to start — the run is at a gate a person
        owns, at an ending, or the project has auto-advance switched off — and
        that is the ordinary case rather than a failure. Callers treat it as
        "nothing further happened", because nothing did.

        One step, not a loop. Every step here queues a job, and the worker that
        runs it calls this again when it finishes, so the run walks itself
        forward one stage per completion. Draining the whole pipeline inside one
        call would mean holding a transaction open across every model call in a
        run, and a crash anywhere in it would roll back the lot.
        """
        if not auto_advance_enabled(self._runtime, project_id):
            return None
        resumed = self._resume(project_id)
        step = next_step(resumed.engine.state)
        if step is None:
            return None
        article_id = selected_article_id(self._runtime, project_id) if step.per_article else None
        if not startable(step, self._have(resumed, article_id, step)):
            return None
        if not step.per_article:
            return self._enqueue(project_id, step.job_type, entry=step.entry, payload={})
        if article_id is None:
            return None
        return self._enqueue_for_article(article_id, step.job_type, entry=step.entry)

    def _have(self, resumed: Resumed, article_id: str | None, step: Step) -> Have:
        """What the run has produced already, for the steps that turn on it.

        Read here rather than in :mod:`~groundscribe.app.advance` because it is
        database work, and that module is the decision it feeds.
        """
        return Have(
            architecture_approved=resumed.engine.approved_architecture is not None,
            revision_plan=self._revision_plan_exists(article_id),
            triaged_review=self._review_triaged(article_id),
            ran_without_moving=self._ran_without_moving(resumed, step),
        )

    def _ran_without_moving(self, resumed: Resumed, step: Step) -> bool:
        """Whether this job already succeeded and left the run where it is.

        Answered by comparing two timestamps the system already records: when the
        newest successful job of this type finished, and when the run last took a
        transition. A stage that finished without the run moving either declined
        its exit edge or has none — and either way, running it again produces the
        same outcome at the price of another model call.

        Deliberately indifferent to *why*. The two cases found so far had
        completely different causes and identical symptoms, which is the argument
        for a check that asks about the effect instead.
        """
        session = self._runtime.session
        finished = session.scalars(
            select(Job)
            .where(
                Job.pipeline_run_id == resumed.run.id,
                Job.job_type == step.job_type,
                Job.status == JobStatus.SUCCEEDED,
                Job.completed_at.is_not(None),
            )
            .order_by(Job.completed_at.desc(), Job.id.desc())
        ).first()
        if finished is None or finished.completed_at is None:
            return False

        moved = session.scalars(
            select(models.DecisionRecord)
            .join(
                models.StageExecution,
                models.StageExecution.id == models.DecisionRecord.stage_execution_id,
            )
            .where(
                models.StageExecution.pipeline_run_id == resumed.run.id,
                models.DecisionRecord.decision_type == "workflow_transition",
                models.DecisionRecord.decided_at > finished.completed_at,
            )
            .limit(1)
        ).first()
        return moved is None

    def _review_triaged(self, article_id: str | None) -> bool:
        """Whether anyone has decided anything about the current review's findings.

        True where there is no review and where it raised nothing, because both
        mean there is nothing waiting on a person — the flag exists to stop a plan
        being built from findings nobody has read, not to stop planning at all.
        """
        if article_id is None:
            return True
        session = self._runtime.session
        try:
            version = rehydrate.latest_version(session, article_id)
            review = rehydrate.latest_review(session, version.id)
        except rehydrate.MissingInput:
            return True
        if not review.issues:
            return True
        return any(issue.status is not FindingStatus.PROPOSED for issue in review.issues)

    def _revision_plan_exists(self, article_id: str | None) -> bool:
        """Whether the current review has already been planned from.

        Asked of the *review* rather than the article: a plan belongs to the
        review it reconciles, so a new review is a new thing to plan and an
        existing one is a plan already waiting to be read.
        """
        if article_id is None:
            return False
        session = self._runtime.session
        try:
            version = rehydrate.latest_version(session, article_id)
            review = rehydrate.latest_review(session, version.id)
            rehydrate.latest_plan(session, review.id)
        except rehydrate.MissingInput:
            return False
        return True

    def _act(
        self,
        project_id: str,
        action: WorkflowAction,
        *,
        actor_id: str,
        rationale: str = "",
    ) -> CommandResult:
        """A person moves the run, and then it carries on by itself.

        The advance is what makes approving something feel like approving it: a
        person who accepts a brief has said what they think about the brief, and
        asking them to press *draft* immediately afterwards is asking them to
        confirm a decision they just made. It is skipped where the state a
        person moved into is one they own too — ``advance`` returns ``None``
        there, so this needs no condition of its own.
        """
        resumed = self._resume(project_id)
        resumed.engine.apply(
            action, actor_id=actor_id, actor_type=ActorType.USER, rationale=rationale
        )
        settled = self._settle(resumed)
        return self.advance(resumed.context.project_id) or settled

    def _settle(
        self,
        resumed: Resumed,
        *,
        job: Job | None = None,
        detail: dict[str, Any] | None = None,
    ) -> CommandResult:
        """Store where the run ended up, and report it."""
        self._runtime.positions.capture(resumed.position, resumed.engine)
        return CommandResult(
            project_id=resumed.context.project_id,
            run_id=resumed.run.id,
            state=resumed.engine.state,
            available_actions=available_actions(resumed.engine.state),
            job=job,
            detail=detail or {},
        )

    def _voice_for(self, resumed: Resumed, article_id: str) -> VoiceProfileDocument:
        """The effective voice for one article.

        Resolved here rather than imported from ``app.handlers``, which holds the
        same helper for the worker's side: handlers already imports this module,
        and a shared helper would have to move somewhere neither of them owns for
        the sake of four lines.
        """
        project = self._runtime.session.get(domain_models.Project, resumed.context.project_id)
        if project is None:  # pragma: no cover - a run always has its project
            return shipped_voice_profile()
        store = VoiceStore(
            self._runtime.session,
            snapshots=self._runtime.snapshots,
            recorder=self._runtime.recorder,
        )
        return store.resolve(
            user_id=project.user_id, project_id=project.id, article_id=article_id
        ).profile

    def _resume(self, project_id: str) -> Resumed:
        """Rebuild the engine and the stage context from stored rows."""
        return resume_run(self._runtime, self._run_for(project_id))

    def _require_offered(self, resumed: Resumed, action: WorkflowAction) -> None:
        """Refuse a command the run does not currently offer.

        For commands that *record* rather than move: answering a question writes
        provenance and takes no edge, so nothing else would ask the engine
        whether it is allowed. The refusal is the machine's own — its list of
        offered actions — rather than a state name compared here, so a command
        cannot go on being accepted after the transition table stops offering it.
        """
        if action not in resumed.engine.machine.available_actions():
            offered = ", ".join(
                sorted(offer.value for offer in resumed.engine.machine.available_actions())
            )
            raise IllegalTransition(
                f"{action.value} is not available in {resumed.engine.state.value} "
                f"(offered: {offered})"
            )

    def _run_for(self, project_id: str) -> models.PipelineRun:
        run = self._runtime.session.scalars(
            select(models.PipelineRun)
            .where(models.PipelineRun.project_id == project_id)
            .order_by(models.PipelineRun.started_at.desc(), models.PipelineRun.id.desc())
        ).first()
        if run is None:
            raise UnknownProject(f"no pipeline run for project {project_id}")
        return run

    def _position_for(self, run: models.PipelineRun) -> WorkflowPosition:
        position = self._runtime.positions.load(run)
        if position is None:
            raise UnknownProject(f"run {run.id} has no recorded position")
        return position

    def _current_architecture(
        self, resumed: Resumed
    ) -> tuple[domain_models.ContentArchitecture, ArtifactSnapshot, ArchitectureProposal]:
        """The architecture version in force, its snapshot, and its document."""
        session = resumed.context.session
        architecture = session.scalars(
            select(domain_models.ContentArchitecture)
            .where(domain_models.ContentArchitecture.project_id == resumed.context.project_id)
            .order_by(domain_models.ContentArchitecture.id.desc())
        ).first()
        if architecture is None:
            raise UnknownProject("this project has no proposed architecture")
        snapshot = rehydrate.snapshot_of(session, architecture.snapshot_id)
        proposal = rehydrate.document(self._runtime.snapshots, snapshot, ArchitectureProposal)
        return architecture, snapshot, proposal

    def _open_articles(
        self, resumed: Resumed, architecture: domain_models.ContentArchitecture
    ) -> None:
        """Give each approved concept an article row, so the API can address it.

        The id is the concept's, which is what phase 07's drafting stage already
        merges against. Creating them at approval rather than at drafting is what
        lets ``POST /articles/{id}/brief/generate`` name an article that does not
        have a draft yet — which is every article, at that point.
        """
        session = resumed.context.session
        concepts = session.scalars(
            select(domain_models.ArticleConcept)
            .where(domain_models.ArticleConcept.architecture_id == architecture.id)
            .order_by(domain_models.ArticleConcept.ordinal)
        ).all()
        for article_concept in concepts:
            session.merge(
                domain_models.Article(
                    id=article_concept.id,
                    project_id=resumed.context.project_id,
                    title=article_concept.title,
                    created_by_execution_id=resumed.engine.execution.id,
                )
            )
        session.flush()


def resume_run(runtime: Runtime, run: models.PipelineRun) -> Resumed:
    """Rebuild a run's engine and stage context from the database.

    Shared by the service and the worker's handlers, deliberately: they are the
    two processes that command the same run, and two ways of reconstituting it
    would eventually disagree about where it is.
    """
    position = runtime.positions.load(run)
    if position is None:
        raise UnknownProject(f"run {run.id} has no recorded position")

    constraints = rehydrate.constraints(runtime.session, run.project_id)
    restricted = restricted_spans(runtime.session, run.project_id)
    # The project's retention choice goes into force before the resumed run
    # records anything (phase 13). A recorder that adopted it on the *next*
    # command would write the first call of every run under the deployment
    # default — the call most likely to carry the prompt someone meant to keep
    # off disk.
    runtime.recorder.use_retention(
        RetentionPolicy(mode=constraints.trace_retention_mode, restricted=restricted)
    )
    engine = WorkflowEngine(
        recorder=runtime.recorder,
        snapshots=runtime.snapshots,
        run=run,
        state=position.state,
        policy=runtime.policy,
        # The names the project declared, plus the spans of source material a
        # person flagged out of the final output (phase 13). Both are the same
        # question to the guard — "does this text appear in what is about to be
        # published?" — and a guard handed only half the evidence passes half
        # the leaks.
        confidential=(*constraints.confidential_names, *restricted),
        actor_id=runtime.actor_id,
        execution=position.workflow_execution,
    )
    runtime.positions.apply(position, engine)
    context = PipelineContext(
        engine=engine,
        recorder=runtime.recorder,
        snapshots=runtime.snapshots,
        generator=generator_for(runtime, run.project_id),
        session=runtime.session,
        project_id=run.project_id,
        constraints=constraints,
        actor_id=runtime.actor_id,
    )
    return Resumed(run=run, position=position, engine=engine, context=context)


def generator_for(runtime: Runtime, project_id: str) -> StructuredGenerator:
    """The generator this project's stages call through (phase 15).

    Rebound here, on the way into the pipeline context, because this is the last
    point that knows both the process's clients and whose run is about to use
    them — and the first point at which "which policy" has an answer. A project
    that has chosen no profile gets the runtime's own generator unchanged, which
    is the shipped default and the case for almost every project.

    A profile naming a file that is no longer there raises rather than falling
    back. The fall-back would run the project on the provider it was moved off,
    and record a success for having done so.
    """
    profile = runtime.session.get(domain_models.Project, project_id)
    chosen = profile.routing_profile if profile is not None else None
    if not chosen:
        return runtime.generator
    return runtime.generator.with_routing(routing_policy(chosen))


__all__ = [
    "ApplicationService",
    "CommandResult",
    "Resumed",
    "RoutingProfiles",
    "UnknownProject",
    "generator_for",
    "resume_run",
]
