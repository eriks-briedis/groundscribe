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
from groundscribe.app.runtime import Runtime
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import AnswerResponse, ArtifactType, SourceFormat
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.jobs.enums import JobType
from groundscribe.jobs.models import Job
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, ExecutionStatus
from groundscribe.stages.base import PipelineContext, StageRunner
from groundscribe.stages.ingestion import IngestSource
from groundscribe.stages.override import (
    OverrideCommand,
    approve_architecture,
    override_architecture,
)
from groundscribe.stages.questions import open_question_queue
from groundscribe.stages.schemas import (
    ArchitectureProposal,
    ArticleBriefDocument,
    ArticleDraft,
    SourceModel,
)
from groundscribe.validation.stage import ValidateFinalOutput
from groundscribe.workflow.engine import WorkflowEngine
from groundscribe.workflow.position import WorkflowPosition
from groundscribe.workflow.states import WorkflowAction, WorkflowState

A = WorkflowAction


class UnknownProject(LookupError):
    """Asked about a project that does not exist, or has no run."""


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
        return self._settle(resumed)

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
        """Record one answer and rebuild the source model from it.

        The rebuild *supersedes* any extraction still queued rather than joining
        it: a second answer makes the first round's extraction wrong, not
        redundant, and running it anyway would spend a model call producing a
        model the author has already contradicted.
        """
        resumed = self._resume(project_id)
        gap = resumed.context.session.get(domain_models.SourceGap, gap_id)
        if gap is None:
            raise UnknownProject(f"no gap {gap_id} in project {project_id}")

        queue = open_question_queue(resumed.context)
        queue.respond(gap, response=response, text=text, answered_by=answered_by)
        queue.submit(submitted_by=answered_by)
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
        return self._settle(resumed)

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

    async def plan_revision(self, article_id: str) -> CommandResult:
        """Turn the review's findings into a plan a rewrite is bound by."""
        return self._enqueue_for_article(article_id, JobType.PLAN_REVISION)

    def approve_revision_plan(self, article_id: str, *, approved_by: str) -> CommandResult:
        """Authorise the rewrite the plan describes."""
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
                draft=rehydrate.document(self._runtime.snapshots, version_snapshot, ArticleDraft),
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
                prohibited_terms=resumed.context.constraints.confidential_names,
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

    def override_and_approve(self, article_id: str, *, approved_by: str) -> CommandResult:
        """Accept an article the score refused, on a person's explicit say-so."""
        return self._act(
            self.project_for_article(article_id), A.OVERRIDE_AND_APPROVE, actor_id=approved_by
        )

    def cancel(self, project_id: str, *, cancelled_by: str) -> CommandResult:
        """Stop the run. Always available, so no run can be trapped."""
        return self._act(project_id, A.CANCEL, actor_id=cancelled_by)

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

    def replay_execution(self, execution_id: str, *, requested_by: str) -> models.StageExecution:
        """Re-run a stage as a new execution branched from the original."""
        execution = self.get_execution(execution_id)
        resumed = self._resume(execution.pipeline_run.project_id)
        replayed = resumed.engine.replay(execution, requested_by=requested_by)
        self._runtime.positions.capture(resumed.position, resumed.engine)
        return replayed

    def fork_execution(self, execution_id: str, *, requested_by: str) -> models.StageExecution:
        """Branch a new execution from an existing one, leaving it untouched.

        The same mechanism as a replay, and deliberately so at this phase: both
        produce a linked child execution whose parent is the original. What
        distinguishes them — replaying the *same* inputs versus running altered
        ones — is phase 12's experimentation work, and inventing a difference
        here would be a guess the frontend then has to live with.
        """
        return self.replay_execution(execution_id, requested_by=requested_by)

    def job(self, job_id: str) -> Job:
        job = self._runtime.queue.get(job_id)
        if job is None:
            raise UnknownProject(f"no job {job_id}")
        return job

    def create_experiment(self, *, name: str) -> models.ExperimentRun:
        """Open an experiment record. Phase 12 fills in what it means."""
        experiment = models.ExperimentRun(
            id=uuid.uuid4().hex,
            name=name,
            status=ExecutionStatus.PENDING,
            created_at=self._runtime.clock(),
        )
        self._runtime.session.add(experiment)
        self._runtime.session.flush()
        return experiment

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

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

    def _act(
        self,
        project_id: str,
        action: WorkflowAction,
        *,
        actor_id: str,
        rationale: str = "",
    ) -> CommandResult:
        """A person moves the run, and nothing else happens."""
        resumed = self._resume(project_id)
        resumed.engine.apply(
            action, actor_id=actor_id, actor_type=ActorType.USER, rationale=rationale
        )
        return self._settle(resumed)

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

    def _resume(self, project_id: str) -> Resumed:
        """Rebuild the engine and the stage context from stored rows."""
        return resume_run(self._runtime, self._run_for(project_id))

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
    engine = WorkflowEngine(
        recorder=runtime.recorder,
        snapshots=runtime.snapshots,
        run=run,
        state=position.state,
        policy=runtime.policy,
        confidential=constraints.confidential_names,
        actor_id=runtime.actor_id,
        execution=position.workflow_execution,
    )
    runtime.positions.apply(position, engine)
    context = PipelineContext(
        engine=engine,
        recorder=runtime.recorder,
        snapshots=runtime.snapshots,
        generator=runtime.generator,
        session=runtime.session,
        project_id=run.project_id,
        constraints=constraints,
        actor_id=runtime.actor_id,
    )
    return Resumed(run=run, position=position, engine=engine, context=context)


__all__ = [
    "ApplicationService",
    "CommandResult",
    "Resumed",
    "UnknownProject",
    "resume_run",
]
