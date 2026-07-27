"""Initial drafting (phase 07 §7).

plan/07 → *GenerateInitialDraft*: write the article from the approved source
model, the locked architecture, the brief, the active voice profile and the
project's constraints — and do not resolve missing facts. Omit them, qualify them,
mark them visibly, or ask to go back to gap analysis.

The interesting problem is that "this draft invented nothing" is not a property of
prose anyone can check. So the draft declares what it did — the claims it used, the
qualifications it applied, what it omitted and why, what it could not resolve — and
the stage checks those declarations against the source model and the brief. Three
failures are refused outright, and all three read as perfectly good English:

- a claim nobody extracted (the draft is arguing from something that does not
  exist);
- a claim the source says needs qualifying, stated without its conditions;
- material the brief excluded by name, printed anyway.

The draft cannot route itself back to the author. The phase-05 table has no edge
from ``draft_generating`` to the source stages — adding one is phase-05's business
— so a blocking unresolved fact is *recorded as a request* and the draft still goes
to review with its markers intact. Phase 08's routing is what acts on it. This is
the honest arrangement: the draft is evidence that a fact is missing, and evidence
is more useful in front of a reviewer than withheld.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from sqlalchemy import func, select

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType, BranchStatus
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.errors import DraftContractError, EvidenceError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import (
    ArticleBriefDocument,
    ArticleDraft,
    SourceModel,
    VoiceProfileDocument,
)
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
DRAFT_STAGE = "generate_initial_draft"


class DraftOutcome:
    """The draft, and the immutable version it was stored as."""

    __slots__ = ("article", "draft", "version")

    def __init__(
        self,
        *,
        draft: ArticleDraft,
        article: domain_models.Article,
        version: domain_models.ArticleVersion,
    ) -> None:
        self.draft = draft
        self.article = article
        self.version = version


class GenerateInitialDraft:
    """Write the first version of one article against its brief."""

    name: ClassVar[str] = DRAFT_STAGE
    impl_version: ClassVar[str] = "1.0"
    exit_action: ClassVar[WorkflowAction | None] = WorkflowAction.SUBMIT_DRAFT

    #: An instance attribute: a re-draft is entered from wherever the run already
    #: is, and only the first one comes through the brief's approval.
    entry_action: WorkflowAction | None

    def __init__(
        self,
        *,
        brief: ArticleBriefDocument,
        brief_snapshot: ArtifactSnapshot,
        concept: domain_models.ArticleConcept,
        source_model: SourceModel,
        voice: VoiceProfileDocument,
        source_model_snapshot: ArtifactSnapshot | None = None,
        entry_action: WorkflowAction | None = None,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._brief = brief
        self._brief_snapshot = brief_snapshot
        self._concept = concept
        self._source_model = source_model
        self._source_model_snapshot = source_model_snapshot
        self._voice = voice
        self.entry_action = entry_action
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[DraftOutcome]:
        """Draft, check the declarations, store the version, ask for gaps if blocked."""
        require_permitted_provider(context, DRAFT_STAGE, override=self._override)
        context.recorder.record_input(execution, self._brief_snapshot, role="article_brief")
        if self._source_model_snapshot is not None:
            context.recorder.record_input(
                execution, self._source_model_snapshot, role="source_model"
            )

        generated = await context.generator.generate(
            execution,
            stage=DRAFT_STAGE,
            template_id=DRAFT_STAGE,
            template_version=self._template_version,
            variables={
                "brief": self._brief.model_dump(mode="json"),
                "source_model": self._source_model.model_dump(mode="json"),
                "voice": self._voice.model_dump(mode="json"),
                "first_person_allowed": context.constraints.first_person_allowed,
                "target_length_words": self._brief.target_length_words,
            },
            schema=ArticleDraft,
            override=self._override,
        )
        draft = generated.value
        check_draft(draft, self._source_model, self._brief)

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.ARTICLE_VERSION,
            content=draft.model_dump(mode="json"),
            role="article_version",
        )
        article, version = store_version(context, execution, draft, snapshot, concept=self._concept)
        self._request_gap_return(context, execution, draft)

        return StageResult(
            value=DraftOutcome(draft=draft, article=article, version=version),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            detail={
                "words": draft.word_count,
                "claims_used": len(draft.claims_used),
                "unresolved": len(draft.unresolved),
                "finish_reason": draft.finish_reason,
                "voice_profile": self._voice.name,
                "voice_profile_version": self._voice.version,
            },
        )

    def _request_gap_return(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        draft: ArticleDraft,
    ) -> models.DecisionRecord | None:
        """Ask for the author when a marker is blocking, rather than routing there.

        Recorded as a decision *and* announced as an intervention request: the
        decision is the reviewable artefact, the event is what phase 09's queue
        reads. The draft still goes to review — a marked hole in front of a
        reviewer is more useful than a stage that refused to produce anything.
        """
        blocking = [item for item in draft.unresolved if item.blocking]
        if not blocking:
            return None

        record = context.recorder.record_decision(
            execution,
            decision_type="gap_return",
            decided_by=DRAFT_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "blocking": [item.question for item in blocking],
                "markers": [item.marker for item in blocking],
                "claim_ids": sorted({cid for item in blocking for cid in item.claim_ids}),
            },
            outcome="return_to_gap_analysis_requested",
            rationale=(
                "the draft could not settle a fact the article would be wrong without; "
                "it is marked in the prose rather than invented, and a person decides "
                "whether to answer it or accept the article without it"
            ),
        )
        context.recorder.emit(
            event_type="intervention.requested",
            actor_type=ActorType.SYSTEM,
            actor_id=DRAFT_STAGE,
            execution=execution,
            payload={
                "reason": "the draft has blocking unresolved facts",
                "questions": [item.question for item in blocking],
            },
        )
        return record


def store_version(
    context: PipelineContext,
    execution: models.StageExecution,
    draft: ArticleDraft,
    snapshot: ArtifactSnapshot,
    *,
    concept: domain_models.ArticleConcept,
    parent: domain_models.ArticleVersion | None = None,
) -> tuple[domain_models.Article, domain_models.ArticleVersion]:
    """Persist one immutable article version, superseding its parent if it has one.

    Shared by drafting and rewriting because a version is a version: the two stages
    differ in what they write, not in how a version relates to the one before it,
    and two copies of this would eventually disagree about lineage.

    The article is keyed on the *concept* rather than on the draft's title. A
    rewrite is free to retitle the piece, and a row per title would scatter one
    article's versions across several identities.

    The ordinal is counted from what is stored rather than taken from the parent's:
    two rewrites branching from one parent are the third and fourth versions of the
    article, not the second one twice (plan/07 → multiple rewrites may branch from
    the same parent).
    """
    article = _article_for(context, execution, draft, concept=concept, parent=parent)
    if parent is not None:
        parent.branch_status = BranchStatus.SUPERSEDED

    stored = context.session.execute(
        select(func.count())
        .select_from(domain_models.ArticleVersion)
        .where(domain_models.ArticleVersion.article_id == article.id)
    ).scalar_one()
    version = domain_models.ArticleVersion(
        id=uuid.uuid4().hex,
        article_id=article.id,
        ordinal=int(stored),
        snapshot_id=snapshot.id,
        created_by_execution_id=execution.id,
        parent_id=parent.id if parent is not None else None,
    )
    context.session.add(version)
    context.session.flush()
    return article, version


def _article_for(
    context: PipelineContext,
    execution: models.StageExecution,
    draft: ArticleDraft,
    *,
    concept: domain_models.ArticleConcept,
    parent: domain_models.ArticleVersion | None,
) -> domain_models.Article:
    """The article a new version belongs to, created on the first version of it."""
    if parent is not None:
        existing = context.session.get(domain_models.Article, parent.article_id)
        if existing is not None:
            return existing

    return context.session.merge(
        domain_models.Article(
            id=concept.id,
            project_id=context.project_id,
            title=draft.title,
            created_by_execution_id=execution.id,
        )
    )


def check_draft(
    draft: ArticleDraft, source_model: SourceModel, brief: ArticleBriefDocument
) -> None:
    """Refuse a draft whose declarations contradict the source model or the brief.

    Every one of these is invisible in the prose and obvious against the record,
    which is the whole reason the draft declares anything at all.
    """
    known = {claim.id for claim in source_model.claims}
    dangling = sorted(set(draft.claims_used) - known)
    if dangling:
        raise EvidenceError(
            f"the draft argues from {', '.join(dangling)}, which "
            f"{'is' if len(dangling) == 1 else 'are'} not in the source model"
        )

    omitted = {claim_id for item in draft.omitted for claim_id in item.claim_ids}
    unqualified = sorted(
        claim_id
        for claim_id in draft.claims_used
        if (claim := source_model.claim(claim_id)) is not None
        and claim.qualification_required
        and claim_id not in draft.qualifications_applied
        and claim_id not in omitted
    )
    if unqualified:
        raise DraftContractError(
            f"the draft uses {', '.join(unqualified)} without the qualification the source "
            "model requires; a conditional claim stated flat is the failure the source "
            "model exists to prevent"
        )

    check_excluded_material(draft.body, brief)


def check_excluded_material(body: str, brief: ArticleBriefDocument) -> None:
    """Refuse prose containing material the brief excluded by name.

    Separate from the rest of :func:`check_draft` because it is the only one of
    those checks that reads the *prose*. Every later stage that returns a new body —
    a rewrite, a voice pass — can reintroduce an excluded phrase without adding a
    single claim, so the check has to travel with the body rather than with the
    declarations around it.

    Matched against what the brief excluded by name, not against a general notion
    of confidentiality; phase 13 owns that.
    """
    leaked = [excluded for excluded in brief.excluded_material if excluded in body]
    if leaked:
        raise DraftContractError(
            f"the draft contains material the brief excluded: {'; '.join(leaked)}"
        )


__all__ = [
    "DRAFT_STAGE",
    "DraftOutcome",
    "GenerateInitialDraft",
    "check_draft",
    "check_excluded_material",
    "store_version",
]
