"""What each job type actually runs (phase 09).

The bridge between the worker — which knows about jobs, executions and failure —
and the editorial stages, which know about source models and briefs. Everything
that makes a stage runnable in a *different process* from the request that
queued it is here: rebuild the run, rebuild the stage's inputs from rows, run it,
store where the run ended up.

Two properties every handler shares:

- **The entry edge is not taken here.** The request that enqueued the job already
  took it, so the run has been visibly "in flight" since the moment the command
  was accepted. Taking it again would be an illegal transition from the state the
  first one produced.
- **The position is captured on the way out.** The worker's transitions have to
  be visible to the next request; a handler that moved the run and did not store
  where it moved it to would have done nothing at all, from the API's point of
  view.

Extraction and gap analysis run as one job, because the workflow has no state
between them: whether the run parks for the author or completes the extraction is
gap analysis's finding, and a command called "extract the source model" that
could not say what was missing would answer half a question.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from groundscribe.app import rehydrate
from groundscribe.app.runtime import Runtime
from groundscribe.app.services import Resumed, resume_run
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.experiments.replay import rerun_of
from groundscribe.experiments.variables import ForkVariables
from groundscribe.jobs.enums import JobType
from groundscribe.jobs.worker import JobHandler, JobOutcome, JobRequest
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType
from groundscribe.scoring.rubric import ScoringRubric, scoring_rubric
from groundscribe.scoring.scoring import ScoreArticle
from groundscribe.stages.architecture import ProposeContentArchitecture
from groundscribe.stages.base import StageResult, StageRunner
from groundscribe.stages.brief import GenerateArticleBrief
from groundscribe.stages.context import ContextStrategy
from groundscribe.stages.drafting import GenerateInitialDraft
from groundscribe.stages.extraction import DEFAULT_TOKEN_BUDGET, ExtractSourceTruth
from groundscribe.stages.planning import CreateRevisionPlan
from groundscribe.stages.questions import GenerateGapQuestions
from groundscribe.stages.review import ReviewSubstantively
from groundscribe.stages.rewriting import RewriteSubstantively
from groundscribe.stages.schemas import (
    ArchitectureProposal,
    ArticleBriefDocument,
    RevisionPlanDocument,
    SourceModel,
    SubstantiveReview,
    VoiceProfileDocument,
)
from groundscribe.stages.voice import AlignVoice
from groundscribe.voice.models import VoiceProfileVersion
from groundscribe.voice.shipped import shipped_voice_profile
from groundscribe.voice.store import VoiceStore

#: The voice an author who has saved nothing writes in. Empty rather than
#: opinionated: plan/10's calibration produces the first profile, and inventing
#: a default style for someone who has not chosen one would be the generic
#: humanisation this phase exists to replace.
#: The voice for a project whose own row cannot be read.
#:
#: The shipped profile rather than an empty document (phase 16). An empty one is
#: not a neutral fallback — it tells the align-voice prompt to enforce nothing,
#: the scorer to measure against nothing, and final validation to prohibit
#: nothing, which is how a run produced prose full of the patterns this system
#: exists to remove and scored it 94.
DEFAULT_VOICE = shipped_voice_profile()


def voice_for(runtime: Runtime, resumed: Resumed, article_id: str | None) -> VoiceProfileDocument:
    """The effective voice for this article, resolved from what is stored.

    Resolved per job rather than per run. A profile saved between the draft and
    the rewrite should apply to the rewrite — that is usually *why* it was
    saved — and a voice captured once at the start of a run would apply the
    author's edits to everything except the article that prompted them.
    """
    project = runtime.session.get(domain_models.Project, resumed.context.project_id)
    if project is None:  # pragma: no cover - a run always has its project
        return DEFAULT_VOICE
    store = VoiceStore(runtime.session, snapshots=runtime.snapshots, recorder=runtime.recorder)
    return store.resolve(
        user_id=project.user_id, project_id=project.id, article_id=article_id
    ).profile


def stage_handlers(runtime: Runtime) -> dict[JobType, JobHandler]:
    """The dispatch table a worker is built with."""
    return {
        JobType.EXTRACT_SOURCE_MODEL: _bind(runtime, _extract),
        JobType.PROPOSE_ARCHITECTURE: _bind(runtime, _propose_architecture),
        JobType.GENERATE_BRIEF: _bind(runtime, _generate_brief),
        JobType.GENERATE_DRAFT: _bind(runtime, _draft),
        JobType.REVIEW_ARTICLE: _bind(runtime, _review),
        JobType.PLAN_REVISION: _bind(runtime, _plan_revision),
        JobType.REWRITE_ARTICLE: _bind(runtime, _rewrite),
        JobType.ALIGN_VOICE: _bind(runtime, _align_voice),
        JobType.SCORE_ARTICLE: _bind(runtime, _score),
    }


@dataclass(frozen=True)
class Rerunning:
    """What a job knows about being a repeat of an earlier execution (phase 12).

    Empty for ordinary work, which is most of it: a stage should not have to ask
    whether it is being replayed, and the parts that do — the branch it hangs
    from, the prompt version, the routing override — are all things the stages
    already accept one run at a time.

    The variables travel whole rather than as a handful of unpacked fields,
    because each handler applies a different subset of them and the ones it
    cannot apply were already refused when the fork was requested
    (:data:`~groundscribe.experiments.replay.STAGE_VARIABLES`). Anything reaching
    a handler here is a variable that handler is expected to honour.
    """

    parent: models.StageExecution | None = None
    template_version: str | None = None
    override: RouteOverride | None = None
    variables: ForkVariables = field(default_factory=ForkVariables)

    @property
    def is_rerun(self) -> bool:
        """Whether this job is repeating an execution rather than advancing the run."""
        return self.parent is not None

    @classmethod
    def of(cls, runtime: Runtime, request: JobRequest) -> Rerunning:
        rerun = rerun_of(request.payload)
        if rerun is None:
            return cls()

        parent = runtime.session.get(models.StageExecution, rerun.source_execution_id)
        return cls(
            parent=parent,
            template_version=rerun.variables.prompt_version,
            override=rerun.variables.route_override(
                requested_by=rerun.requested_by, reason=rerun.reason
            ),
            variables=rerun.variables,
        )

    def applied(self) -> dict[str, Any]:
        """The stage keyword arguments this re-run changes, and only those."""
        options: dict[str, Any] = {}
        if self.template_version is not None:
            options["template_version"] = self.template_version
        if self.override is not None:
            options["override"] = self.override
        return options

    # ------------------------------------------------------------------
    # The variables a handler has to resolve for itself
    # ------------------------------------------------------------------

    def context_strategy(self) -> ContextStrategy:
        """The selection strategy this run reads its source under."""
        return self.variables.strategy or ContextStrategy.IN_ORDER

    def rubric(self) -> ScoringRubric:
        """The rubric this run scores under, by version."""
        return scoring_rubric(self.variables.rubric_version)

    def voice(
        self, runtime: Runtime, resumed: Resumed, article_id: str | None
    ) -> VoiceProfileDocument:
        """The voice this run writes in, resolved or named outright.

        A fork names a profile *version*, not a profile: the version is the
        immutable unit an article can cite, and naming the profile would mean the
        experiment ran under whatever happened to be active when the worker got
        to it. Naming one bypasses the scope hierarchy on purpose — the point of
        the variable is to hold the voice still while something else changes.
        """
        named = self.variables.voice_profile
        if named is None:
            return voice_for(runtime, resumed, article_id)
        store = VoiceStore(runtime.session, snapshots=runtime.snapshots, recorder=runtime.recorder)
        version = runtime.session.get(VoiceProfileVersion, named)
        if version is None:
            raise rehydrate.MissingInput(f"no voice profile version {named}")
        return store.document(version)

    # ------------------------------------------------------------------
    # Which artefact a stage reads
    # ------------------------------------------------------------------

    def recorded(self, role: str) -> domain_models.ArtifactSnapshot | None:
        """What the execution being repeated consumed in ``role``, if anything.

        plan/12 → *replay: re-execute a stage with recorded inputs and config*.
        The handlers below resolve "the newest version", "the latest brief",
        which is what advancing a run means and the opposite of what repeating
        one means: by the time a voice pass is worth replaying, the newest
        version of the article is the one that pass produced.
        """
        if self.parent is None:
            return None
        return next(
            (
                artefact.snapshot
                for artefact in self.parent.inputs
                if artefact.role == role and artefact.snapshot is not None
            ),
            None,
        )

    def reads(
        self,
        runtime: Runtime,
        *,
        role: str,
        expected: ArtifactType,
        named: str | None = None,
        current: domain_models.ArtifactSnapshot,
    ) -> domain_models.ArtifactSnapshot:
        """Which artefact this run reads, under one precedence.

        Named by the fork, else read by the original, else whatever is current.
        The order is the whole design: a variable is what an experiment
        deliberately changed, a recorded input is what the original ran on, and
        the current row is what advancing the run needs. Anything else would make
        a fork's stated change lose to a stale row, or a replay drift.

        A named snapshot's *type* is checked. A fork that pointed the rewrite at
        a brief instead of a revision plan would otherwise fail inside a prompt
        renderer, where the message names a missing template variable rather than
        the mistake that was actually made.
        """
        if named is not None:
            snapshot = rehydrate.snapshot_of(runtime.session, named)
            if snapshot.artifact_type is not expected:
                raise rehydrate.MissingInput(
                    f"snapshot {named} is a {snapshot.artifact_type.value}, not a {expected.value}"
                )
            return snapshot
        return self.recorded(role) or current

    def source_model_snapshot(
        self, runtime: Runtime, current: domain_models.ArtifactSnapshot
    ) -> domain_models.ArtifactSnapshot:
        return self.reads(
            runtime,
            role="source_model",
            expected=ArtifactType.SOURCE_MODEL,
            named=self.variables.source_model,
            current=current,
        )

    def revision_plan_snapshot(
        self, runtime: Runtime, current: domain_models.ArtifactSnapshot
    ) -> domain_models.ArtifactSnapshot:
        return self.reads(
            runtime,
            role="revision_plan",
            expected=ArtifactType.REVISION_PLAN,
            named=self.variables.revision_plan,
            current=current,
        )

    def brief_snapshot(
        self, runtime: Runtime, current: domain_models.ArtifactSnapshot
    ) -> domain_models.ArtifactSnapshot:
        return self.reads(
            runtime,
            role="article_brief",
            expected=ArtifactType.ARTICLE_BRIEF,
            current=current,
        )

    def version(
        self, runtime: Runtime, current: domain_models.ArticleVersion
    ) -> domain_models.ArticleVersion:
        """The article version this run works from, as a row.

        A row rather than a snapshot because three stages branch a new version
        from it and one reviews it — all of which need its ordinal and its
        lineage, not only its bytes. Found back through the snapshot the original
        recorded, which is the only durable link between the two.
        """
        snapshot = self.recorded("article_version")
        if snapshot is None:
            return current
        version = runtime.session.scalars(
            select(domain_models.ArticleVersion).where(
                domain_models.ArticleVersion.snapshot_id == snapshot.id
            )
        ).first()
        if version is None:
            raise rehydrate.MissingInput(
                f"snapshot {snapshot.id} was recorded as an article version but no version row "
                "holds it"
            )
        return version


def _record_rerun(runtime: Runtime, request: JobRequest) -> None:
    """Write down that this execution repeated another, and what was changed.

    On the *new* execution, because that is where a person comparing the two
    will be standing. A replay and an empty fork are the same event and get the
    same name — pretending otherwise would put a difference in the trace that
    does not exist in the work.
    """
    rerun = rerun_of(request.payload)
    execution_id = request.job.stage_execution_id
    if rerun is None or execution_id is None:
        return

    execution = runtime.session.get(models.StageExecution, execution_id)
    if execution is None:
        return

    runtime.recorder.record_decision(
        execution,
        decision_type="execution_fork" if rerun.is_fork else "execution_replay",
        decided_by=rerun.requested_by,
        decided_by_type=ActorType.USER,
        inputs={
            "source_execution_id": rerun.source_execution_id,
            "variables": rerun.variables.changes,
        },
        outcome=execution.id,
        rationale=rerun.reason,
    )


Body = Callable[[Runtime, Resumed, JobRequest], Any]


def _bind(runtime: Runtime, body: Body) -> JobHandler:
    """Wrap one stage body in the resume/capture the worker cannot do for it."""

    async def handler(request: JobRequest) -> JobOutcome:
        job = request.job
        resumed = resume_run(runtime, job.pipeline_run)
        result = await body(runtime, resumed, request)
        _record_rerun(runtime, request)
        runtime.positions.capture(resumed.position, resumed.engine)
        return JobOutcome(
            result={
                "snapshot_ids": [snapshot.id for snapshot in result.outputs],
                "state": resumed.engine.state.value,
                **result.detail,
            }
        )

    return handler


# ----------------------------------------------------------------------
# Source model
# ----------------------------------------------------------------------


async def _extract(runtime: Runtime, resumed: Resumed, request: JobRequest) -> StageResult[Any]:
    """Rebuild the source model from the source and every answer so far.

    The previous model is passed in when there is one, because phase 06 produces
    a *diff* against it: a rebuild a person cannot compare to what it replaced is
    a rebuild they have to take on trust.
    """
    session = resumed.context.session
    source = rehydrate.ingested_source(session, runtime.snapshots, resumed.context.project_id)
    previous_snapshot = rehydrate.latest_snapshot(session, resumed.run, ArtifactType.SOURCE_MODEL)
    previous = (
        rehydrate.document(runtime.snapshots, previous_snapshot, SourceModel)
        if previous_snapshot is not None
        else None
    )

    rerunning = Rerunning.of(runtime, request)
    extracted = await StageRunner(resumed.context).run(
        ExtractSourceTruth(
            **rerunning.applied(),
            source=source,
            answers=rehydrate.open_answers(session, resumed.context.project_id),
            previous=previous,
            previous_snapshot=previous_snapshot,
            token_budget=request.payload.get("token_budget") or DEFAULT_TOKEN_BUDGET,
            context_strategy=rerunning.context_strategy(),
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )
    # The same job continues into gap analysis: it is what decides whether the
    # run parks for the author, and there is no workflow state in between for a
    # second job to be queued from.
    # Gap analysis rides the same job, so a re-run repeats it too — and must not
    # move the run either: the question queue this job parked on the first time
    # has long since been answered.
    return await StageRunner(resumed.context).run(
        GenerateGapQuestions(source_model=extracted.value),
        enter=False,
        transitions=not rerunning.is_rerun,
    )


async def _propose_architecture(
    runtime: Runtime, resumed: Resumed, request: JobRequest
) -> StageResult[Any]:
    """Propose the article or series the source supports."""
    session = resumed.context.session
    rerunning = Rerunning.of(runtime, request)
    snapshot = rerunning.source_model_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.SOURCE_MODEL)
    )
    return await StageRunner(resumed.context).run(
        ProposeContentArchitecture(
            **rerunning.applied(),
            source_model=rehydrate.document(runtime.snapshots, snapshot, SourceModel),
            source_model_snapshot=snapshot,
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


# ----------------------------------------------------------------------
# Brief and draft
# ----------------------------------------------------------------------


async def _generate_brief(
    runtime: Runtime, resumed: Resumed, request: JobRequest
) -> StageResult[Any]:
    """Write the contract one article is drafted against."""
    session = resumed.context.session
    rerunning = Rerunning.of(runtime, request)
    concept = rehydrate.concept(session, _article_id(request))
    source_snapshot = rerunning.source_model_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.SOURCE_MODEL)
    )
    architecture_snapshot = rehydrate.snapshot_of(session, concept.architecture.snapshot_id)
    proposal = rehydrate.document(runtime.snapshots, architecture_snapshot, ArchitectureProposal)
    article = proposal.article(concept.ref)
    if article is None:
        raise rehydrate.MissingInput(f"the architecture does not describe article {concept.ref}")

    return await StageRunner(resumed.context).run(
        GenerateArticleBrief(
            **rerunning.applied(),
            concept=concept,
            article=article,
            source_model=rehydrate.document(runtime.snapshots, source_snapshot, SourceModel),
            architecture_snapshot=architecture_snapshot,
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


async def _draft(runtime: Runtime, resumed: Resumed, request: JobRequest) -> StageResult[Any]:
    """Write the first version against the approved brief."""
    session = resumed.context.session
    rerunning = Rerunning.of(runtime, request)
    concept = rehydrate.concept(session, _article_id(request))
    brief_snapshot = rerunning.brief_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.ARTICLE_BRIEF)
    )
    source_snapshot = rerunning.source_model_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.SOURCE_MODEL)
    )

    return await StageRunner(resumed.context).run(
        GenerateInitialDraft(
            **rerunning.applied(),
            brief=rehydrate.document(runtime.snapshots, brief_snapshot, ArticleBriefDocument),
            brief_snapshot=brief_snapshot,
            concept=concept,
            source_model=rehydrate.document(runtime.snapshots, source_snapshot, SourceModel),
            source_model_snapshot=source_snapshot,
            voice=rerunning.voice(runtime, resumed, _article_id(request)),
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


# ----------------------------------------------------------------------
# Review, plan, rewrite, voice
# ----------------------------------------------------------------------


async def _review(runtime: Runtime, resumed: Resumed, request: JobRequest) -> StageResult[Any]:
    """Review the current version against the source and the brief."""
    session = resumed.context.session
    article_id = _article_id(request)
    version = rehydrate.latest_version(session, article_id)
    rerunning = Rerunning.of(runtime, request)
    version = rerunning.version(runtime, version)
    version_snapshot = rehydrate.snapshot_of(session, version.snapshot_id)
    brief_snapshot = rerunning.brief_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.ARTICLE_BRIEF)
    )
    source_snapshot = rerunning.source_model_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.SOURCE_MODEL)
    )

    return await StageRunner(resumed.context).run(
        ReviewSubstantively(
            **rerunning.applied(),
            draft=rehydrate.article_document(runtime.snapshots, version_snapshot),
            version=version,
            version_snapshot=version_snapshot,
            brief=rehydrate.document(runtime.snapshots, brief_snapshot, ArticleBriefDocument),
            source_model=rehydrate.document(runtime.snapshots, source_snapshot, SourceModel),
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


async def _plan_revision(
    runtime: Runtime, resumed: Resumed, request: JobRequest
) -> StageResult[Any]:
    """Reconcile the review's findings into a plan the rewrite is bound by."""
    session = resumed.context.session
    rerunning = Rerunning.of(runtime, request)
    version = rehydrate.latest_version(session, _article_id(request))
    review_row = rehydrate.review_of(
        session, rerunning.recorded("review")
    ) or rehydrate.latest_review(session, version.id)
    version = session.get(domain_models.ArticleVersion, review_row.article_version_id) or version
    review_snapshot = rehydrate.snapshot_of(session, review_row.snapshot_id)
    version_snapshot = rehydrate.snapshot_of(session, version.snapshot_id)
    brief_snapshot = rerunning.brief_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.ARTICLE_BRIEF)
    )

    return await StageRunner(resumed.context).run(
        CreateRevisionPlan(
            **rerunning.applied(),
            review=rehydrate.document(runtime.snapshots, review_snapshot, SubstantiveReview),
            review_row=review_row,
            review_snapshot=review_snapshot,
            findings=review_row.issues,
            draft=rehydrate.article_document(runtime.snapshots, version_snapshot),
            brief=rehydrate.document(runtime.snapshots, brief_snapshot, ArticleBriefDocument),
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


async def _rewrite(runtime: Runtime, resumed: Resumed, request: JobRequest) -> StageResult[Any]:
    """Rewrite the article under the approved plan, branching from its parent."""
    session = resumed.context.session
    article_id = _article_id(request)
    rerunning = Rerunning.of(runtime, request)
    version = rerunning.version(runtime, rehydrate.latest_version(session, article_id))
    review_row = rehydrate.latest_review(session, version.id)
    plan_row = rehydrate.latest_plan(session, review_row.id)
    plan_snapshot = rerunning.revision_plan_snapshot(
        runtime, rehydrate.snapshot_of(session, plan_row.snapshot_id)
    )
    version_snapshot = rehydrate.snapshot_of(session, version.snapshot_id)
    brief_snapshot = rerunning.brief_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.ARTICLE_BRIEF)
    )
    source_snapshot = rerunning.source_model_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.SOURCE_MODEL)
    )

    return await StageRunner(resumed.context).run(
        RewriteSubstantively(
            **rerunning.applied(),
            plan=rehydrate.document(runtime.snapshots, plan_snapshot, RevisionPlanDocument),
            plan_snapshot=plan_snapshot,
            previous=rehydrate.article_document(runtime.snapshots, version_snapshot),
            parent=version,
            concept=rehydrate.concept(session, article_id),
            brief=rehydrate.document(runtime.snapshots, brief_snapshot, ArticleBriefDocument),
            source_model=rehydrate.document(runtime.snapshots, source_snapshot, SourceModel),
            voice=rerunning.voice(runtime, resumed, article_id),
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


async def _align_voice(runtime: Runtime, resumed: Resumed, request: JobRequest) -> StageResult[Any]:
    """Make it read like the author, changing nothing it claims."""
    session = resumed.context.session
    article_id = _article_id(request)
    rerunning = Rerunning.of(runtime, request)
    version = rerunning.version(runtime, rehydrate.latest_version(session, article_id))
    version_snapshot = rehydrate.snapshot_of(session, version.snapshot_id)
    brief_snapshot = rerunning.brief_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.ARTICLE_BRIEF)
    )

    return await StageRunner(resumed.context).run(
        AlignVoice(
            **rerunning.applied(),
            previous=rehydrate.article_document(runtime.snapshots, version_snapshot),
            parent=version,
            concept=rehydrate.concept(session, article_id),
            brief=rehydrate.document(runtime.snapshots, brief_snapshot, ArticleBriefDocument),
            voice=rerunning.voice(runtime, resumed, article_id),
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


async def _score(runtime: Runtime, resumed: Resumed, request: JobRequest) -> StageResult[Any]:
    """Score the article, which is also what decides where it goes next."""
    session = resumed.context.session
    article_id = _article_id(request)
    version = rehydrate.latest_version(session, article_id)
    rerunning = Rerunning.of(runtime, request)
    version = rerunning.version(runtime, version)
    version_snapshot = rehydrate.snapshot_of(session, version.snapshot_id)
    brief_snapshot = rerunning.brief_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.ARTICLE_BRIEF)
    )
    source_snapshot = rerunning.source_model_snapshot(
        runtime, rehydrate.require_snapshot(session, resumed.run, ArtifactType.SOURCE_MODEL)
    )

    return await StageRunner(resumed.context).run(
        ScoreArticle(
            **rerunning.applied(),
            draft=rehydrate.article_document(runtime.snapshots, version_snapshot),
            version=version,
            version_snapshot=version_snapshot,
            brief=rehydrate.document(runtime.snapshots, brief_snapshot, ArticleBriefDocument),
            source_model=rehydrate.document(runtime.snapshots, source_snapshot, SourceModel),
            brief_snapshot=brief_snapshot,
            source_model_snapshot=source_snapshot,
            voice=rerunning.voice(runtime, resumed, article_id),
            rubric=rerunning.rubric(),
        ),
        enter=False,
        on_execution=request.opened,
        parent=rerunning.parent,
        transitions=not rerunning.is_rerun,
    )


def _article_id(request: JobRequest) -> str:
    """The article a command was issued against, or a loud failure."""
    article_id = request.payload.get("article_id")
    if not isinstance(article_id, str) or not article_id:
        raise rehydrate.MissingInput(f"job {request.job.id} names no article")
    return article_id


__all__ = ["DEFAULT_VOICE", "stage_handlers"]
