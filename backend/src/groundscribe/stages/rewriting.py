"""Substantive rewrite: applying an approved plan (phase 07 §10).

plan/07 → *RewriteSubstantively*: apply the approved revision plan — structure,
order, evidence amount, thesis wording, examples and scope are all fair game — but
do not alter the source model or invent facts. The result is a new
``ArticleVersion`` linked to its parent, and *several rewrites may branch from the
same parent*, which is how two prompts, models or strategies get compared.

The rewrite inherits drafting's checks unchanged, because it can break exactly the
same promises: a claim nobody extracted, a qualification dropped, material the
brief excluded printed anyway. It adds two of its own, and both exist because the
plan is a decision a *person* approved:

- every required change is applied, and a skipped one is named with a reason. The
  difference between "I judged this unnecessary" and "I forgot" is the whole reason
  the plan named it.
- a claim the plan promised would survive is still argued. That promise is the only
  thing standing between "revise the article" and "quietly drop the inconvenient
  fact".

The source model is never touched here, and there is nothing to enforce because
there is nothing to touch: this stage writes one artefact type and reads the source
model only to check itself against it.
"""

from __future__ import annotations

from typing import ClassVar

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.drafting import DraftOutcome, check_draft, store_version
from groundscribe.stages.errors import RewriteContractError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.payload import claims_in_scope, source_model_payload
from groundscribe.stages.schemas import (
    ArticleBriefDocument,
    ArticleDraft,
    RevisionPlanDocument,
    RewrittenArticle,
    SourceModel,
    VoiceProfileDocument,
)
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
REWRITE_STAGE = "rewrite_substantively"


class RewriteSubstantively:
    """Rewrite one article version against the plan the author approved."""

    name: ClassVar[str] = REWRITE_STAGE
    impl_version: ClassVar[str] = "1.0"
    #: The entry was the author approving the plan. The exit is reported with the
    #: result so a comparison branch — a second rewrite of the same parent, run to
    #: compare prompts — does not move the machine twice.
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None

    def __init__(
        self,
        *,
        plan: RevisionPlanDocument,
        plan_snapshot: ArtifactSnapshot,
        previous: ArticleDraft,
        parent: domain_models.ArticleVersion,
        concept: domain_models.ArticleConcept,
        brief: ArticleBriefDocument,
        source_model: SourceModel,
        voice: VoiceProfileDocument,
        transitions: bool = True,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._plan = plan
        self._plan_snapshot = plan_snapshot
        self._previous = previous
        self._parent = parent
        self._concept = concept
        self._brief = brief
        self._source_model = source_model
        self._voice = voice
        self._transitions = transitions
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[DraftOutcome]:
        """Rewrite, check it against the plan and the brief, store the new version."""
        require_permitted_provider(context, REWRITE_STAGE, override=self._override)
        context.recorder.record_input(execution, self._plan_snapshot, role="revision_plan")
        if self._parent.snapshot is not None:
            context.recorder.record_input(execution, self._parent.snapshot, role="article_version")

        generated = await context.generator.generate(
            execution,
            stage=REWRITE_STAGE,
            template_id=REWRITE_STAGE,
            template_version=self._template_version,
            variables={
                "plan": self._plan.model_dump(mode="json"),
                "previous": self._previous.model_dump(mode="json"),
                "brief": self._brief.model_dump(mode="json"),
                # `check_draft` re-validates the rewrite against the *whole*
                # model afterwards, so narrowing here cannot let an invented
                # claim through — it only stops the rewriter being shown material
                # the architecture routed to another article.
                "source_model": source_model_payload(
                    self._source_model,
                    claim_ids=claims_in_scope(
                        self._previous.claims_used, self._brief.cited_claim_ids()
                    ),
                ),
                "voice": self._voice.model_dump(mode="json"),
            },
            schema=RewrittenArticle,
            override=self._override,
        )
        rewritten = generated.value
        check_draft(rewritten, self._source_model, self._brief)
        check_rewrite(rewritten, self._plan)

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.ARTICLE_VERSION,
            content=rewritten.model_dump(mode="json"),
            role="article_version",
            parent=self._parent.snapshot,
        )
        article, version = store_version(
            context, execution, rewritten, snapshot, concept=self._concept, parent=self._parent
        )

        return StageResult(
            value=DraftOutcome(draft=rewritten, article=article, version=version),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            exit_action=WorkflowAction.SUBMIT_REWRITE if self._transitions else None,
            detail={
                "words": rewritten.word_count,
                "changes_applied": len(rewritten.changes_applied),
                "changes_skipped": len(rewritten.changes_skipped),
                "branched_from": self._parent.id,
            },
        )


def check_rewrite(rewritten: RewrittenArticle, plan: RevisionPlanDocument) -> None:
    """Refuse a rewrite that ignored the plan or dropped a protected claim."""
    applied = set(rewritten.changes_applied)
    missing = sorted(change.id for change in plan.required_changes if change.id not in applied)
    if missing:
        raise RewriteContractError(
            f"the rewrite did not apply {', '.join(missing)}, which the plan marked required; "
            "the rewriter is not the one who decides which feedback mattered"
        )

    abandoned = sorted(set(plan.claims_that_must_not_change) - set(rewritten.claims_used))
    if abandoned:
        raise RewriteContractError(
            f"the rewrite no longer argues {', '.join(abandoned)}, which the plan promised "
            "would not change; that promise is what separates revising an article from "
            "quietly dropping an inconvenient fact"
        )


__all__ = ["REWRITE_STAGE", "RewriteSubstantively", "check_rewrite"]
