"""Article-brief generation (phase 06 §6).

plan/06 → *GenerateArticleBrief*: per approved article, a brief-as-contract
carrying title, thesis, audience, reader knowledge, reader problem, opening
direction, argument structure, evidence per section, required examples, claims
requiring qualification, required conclusion, length, platform constraints, the
active voice profile, style overrides, excluded material, reserved material and a
definition of done — mandatory and optional distinguished throughout.

The brief is the last artefact produced before anything is written, and everything
after it is judged against it: phase 07 drafts to it, phase 08 validates against
its definition of done. That is why this stage checks rather than trusts. Three
clauses can be dropped without the brief looking wrong on its own —

- a qualification the source model demanded, which if lost licenses the draft to
  state a conditional number flat;
- a publication constraint the source stated, which if lost licenses publishing
  it;
- the project's target length, which is the author's to set and not the model's
  to revise;

— and each of them is invisible except against the source model or the project's
constraints. A brief is a contract; a contract with a clause quietly missing is
worse than no contract, because everyone downstream behaves as though it is there.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.errors import BriefContractError, EvidenceError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import ArticleBriefDocument, ProposedArticle, SourceModel
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
BRIEF_STAGE = "generate_article_brief"


class BriefOutcome:
    """The brief, and the row it was persisted as."""

    __slots__ = ("brief", "row")

    def __init__(self, *, brief: ArticleBriefDocument, row: domain_models.ArticleBrief) -> None:
        self.brief = brief
        self.row = row

    @property
    def brief_concept_id(self) -> str:
        """The article concept this brief is a contract for."""
        return self.row.concept_id


class GenerateArticleBrief:
    """Turn one approved concept into the contract the draft is written against."""

    name: ClassVar[str] = BRIEF_STAGE
    impl_version: ClassVar[str] = "1.0"
    entry_action: ClassVar[WorkflowAction | None] = WorkflowAction.GENERATE_BRIEF
    exit_action: ClassVar[WorkflowAction | None] = WorkflowAction.SUBMIT_BRIEF

    def __init__(
        self,
        *,
        concept: domain_models.ArticleConcept,
        article: ProposedArticle,
        source_model: SourceModel,
        architecture_snapshot: ArtifactSnapshot,
        parent: domain_models.ArticleBrief | None = None,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._concept = concept
        self._article = article
        self._source_model = source_model
        self._architecture_snapshot = architecture_snapshot
        self._parent = parent
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[BriefOutcome]:
        """Generate the brief, check it against the source model, and store it."""
        require_permitted_provider(context, BRIEF_STAGE, override=self._override)
        context.recorder.record_input(
            execution, self._architecture_snapshot, role="content_architecture"
        )

        required = required_qualifications(self._article, self._source_model)
        constraints = [
            constraint.description for constraint in self._source_model.publication_constraints
        ]
        generated = await context.generator.generate(
            execution,
            stage=BRIEF_STAGE,
            template_id=BRIEF_STAGE,
            template_version=self._template_version,
            variables={
                "article": self._article.model_dump(mode="json"),
                "source_model": self._source_model.model_dump(mode="json"),
                "audience": context.constraints.audience,
                "platform": context.constraints.platform,
                "depth": context.constraints.depth.value,
                "target_length_words": context.constraints.target_length_words,
                "first_person_allowed": context.constraints.first_person_allowed,
                "voice_profile": "default",
                "publication_constraints": constraints,
                "claims_requiring_qualification": sorted(required),
            },
            schema=ArticleBriefDocument,
            override=self._override,
        )
        brief = generated.value
        check_brief(brief, self._source_model, context, required=required)

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.ARTICLE_BRIEF,
            content=brief.model_dump(mode="json"),
            role="article_brief",
            parent=self._parent.snapshot if self._parent is not None else None,
        )
        row = self._store(context, execution, brief, snapshot)
        self._record_decision(context, execution, brief)

        return StageResult(
            value=BriefOutcome(brief=brief, row=row),
            outputs=(snapshot,),
            invocations=generated.attempts,
            detail={
                "sections": len(brief.argument_structure),
                "mandatory_criteria": len(brief.mandatory_criteria),
                "target_length_words": brief.target_length_words,
            },
        )

    def _store(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        brief: ArticleBriefDocument,
        snapshot: ArtifactSnapshot,
    ) -> domain_models.ArticleBrief:
        """Persist the brief row against its concept."""
        row = domain_models.ArticleBrief(
            id=uuid.uuid4().hex,
            concept_id=self._concept.id,
            scope=brief.thesis,
            objectives=brief.required_conclusion,
            snapshot_id=snapshot.id,
            created_by_execution_id=execution.id,
            parent_id=self._parent.id if self._parent is not None else None,
        )
        context.session.add(row)
        context.session.flush()
        return row

    def _record_decision(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        brief: ArticleBriefDocument,
    ) -> models.DecisionRecord:
        """Record what "finished" was defined to mean for this article.

        The definition of done is a decision, not a description: phase 08 validates
        against it, and an article that failed will be argued about in terms of what
        the brief committed to. Recording it as a decision means that argument has a
        dated, attributed record to refer to.
        """
        return context.recorder.record_decision(
            execution,
            decision_type="article_brief",
            decided_by=BRIEF_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "concept_id": self._concept.id,
                "definition_of_done": [
                    criterion.model_dump(mode="json") for criterion in brief.definition_of_done
                ],
                "mandatory_criteria": len(brief.mandatory_criteria),
                "claims_requiring_qualification": list(brief.claims_requiring_qualification),
                "excluded_material": list(brief.excluded_material),
                "target_length_words": brief.target_length_words,
            },
            outcome=brief.title,
            rationale=brief.thesis,
        )


def required_qualifications(article: ProposedArticle, source_model: SourceModel) -> frozenset[str]:
    """The claims this article argues that the source model says need qualifying."""
    return frozenset(
        claim_id
        for claim_id in article.supporting_claim_ids
        if (claim := source_model.claim(claim_id)) is not None and claim.qualification_required
    )


def check_brief(
    brief: ArticleBriefDocument,
    source_model: SourceModel,
    context: PipelineContext,
    *,
    required: frozenset[str],
) -> None:
    """Refuse a brief that has dropped a clause the source or the project set.

    Ordered cheapest-to-most-specific, and all three raise rather than warn: unlike
    an architecture override — where the author is present and entitled to overrule
    the system — nobody is looking at this. The brief goes straight to a drafter
    that will follow it exactly.
    """
    known = {claim.id for claim in source_model.claims}
    dangling = sorted(brief.cited_claim_ids() - known)
    if dangling:
        raise EvidenceError(
            f"the brief argues from {', '.join(dangling)}, which "
            f"{'is' if len(dangling) == 1 else 'are'} not in the source model"
        )

    cited_and_qualified = sorted(
        (required | _qualified_among(brief.cited_claim_ids(), source_model))
        - set(brief.claims_requiring_qualification)
    )
    if cited_and_qualified:
        raise BriefContractError(
            f"the brief cites {', '.join(cited_and_qualified)} without carrying forward the "
            "qualification the source model requires; a draft written to this brief would "
            "state a conditional claim flat"
        )

    missing = [
        constraint.description
        for constraint in source_model.publication_constraints
        if constraint.description not in brief.excluded_material
    ]
    if missing:
        raise BriefContractError(
            f"the brief does not exclude material the source says is unpublishable: "
            f"{'; '.join(missing)}"
        )

    target = context.constraints.target_length_words
    if target is not None and brief.target_length_words != target:
        raise BriefContractError(
            f"the brief sets a length of {brief.target_length_words} words, but the project "
            f"constrains articles to {target}; length is the author's to set, not the model's"
        )


def _qualified_among(claim_ids: frozenset[str], source_model: SourceModel) -> frozenset[str]:
    """Those of ``claim_ids`` the source model marked as needing qualification."""
    return frozenset(
        claim_id
        for claim_id in claim_ids
        if (claim := source_model.claim(claim_id)) is not None and claim.qualification_required
    )


__all__ = [
    "BRIEF_STAGE",
    "BriefOutcome",
    "GenerateArticleBrief",
    "check_brief",
    "required_qualifications",
]
