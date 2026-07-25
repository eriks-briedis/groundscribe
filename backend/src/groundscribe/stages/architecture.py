"""Content-architecture proposal (phase 06 §4).

plan/06 → *ProposeContentArchitecture*: what article, or series, the source model
can honestly support — the distinct arguments, their evidence, their overlap,
whether each stands alone, what the reader must already know, the platform's
constraints, the competing theses, the thin-content risk — plus a structured
decision record naming the choice, its alternatives and why they were rejected,
a confidence, the uncertainties, and the policy version.

Two things this stage does that the schema cannot.

**It checks the argument against the source model.** A proposal arguing from a
claim id the source model does not contain is structurally valid and unbuildable;
the same failure mode as extraction citing a segment that was never sent, and
caught the same way.

**It records the choice as a decision, not just as data.** The proposal is an
artefact; the *decision* is a provenance record naming the policy version that
made it. Phase 05 already refuses to store a policy decision without a version,
and "why is the article shaped like this?" is exactly the question the record
exists to answer.

The stage parks the run at ``ARCHITECTURE_REVIEW_REQUIRED``. Approval is a human
control point (plan/00 → human control at high-leverage decisions), and it is the
gate that makes the lock in §5 mean something.
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
from groundscribe.stages.errors import EvidenceError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import ArchitectureProposal, SourceModel
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
ARCHITECTURE_STAGE = "propose_content_architecture"


class ArchitectureOutcome:
    """The proposal, and the rows it was persisted as.

    A small object rather than a tuple: callers need the proposal (to render), the
    architecture row (to approve and lock) and the concepts (to brief), and three
    positional values would be three chances to take the wrong one.
    """

    __slots__ = ("architecture", "concepts", "proposal")

    def __init__(
        self,
        *,
        proposal: ArchitectureProposal,
        architecture: domain_models.ContentArchitecture,
        concepts: tuple[domain_models.ArticleConcept, ...],
    ) -> None:
        self.proposal = proposal
        self.architecture = architecture
        self.concepts = concepts

    def concept(self, concept_id: str) -> domain_models.ArticleConcept | None:
        """The stored concept with ``concept_id``, if there is one."""
        return next((concept for concept in self.concepts if concept.id == concept_id), None)


class ProposeContentArchitecture:
    """Decide what the source can support, and park it for a person to approve."""

    name: ClassVar[str] = ARCHITECTURE_STAGE
    impl_version: ClassVar[str] = "1.0"
    entry_action: ClassVar[WorkflowAction | None] = WorkflowAction.PROPOSE_ARCHITECTURE
    exit_action: ClassVar[WorkflowAction | None] = WorkflowAction.SUBMIT_ARCHITECTURE

    def __init__(
        self,
        *,
        source_model: SourceModel,
        source_model_snapshot: ArtifactSnapshot,
        parent: domain_models.ContentArchitecture | None = None,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._source_model = source_model
        self._source_model_snapshot = source_model_snapshot
        self._parent = parent
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[ArchitectureOutcome]:
        """Propose, check the claims, persist, and record the decision."""
        require_permitted_provider(context, ARCHITECTURE_STAGE, override=self._override)
        context.recorder.record_input(execution, self._source_model_snapshot, role="source_model")

        generated = await context.generator.generate(
            execution,
            stage=ARCHITECTURE_STAGE,
            template_id=ARCHITECTURE_STAGE,
            template_version=self._template_version,
            variables={
                "source_model": self._source_model.model_dump(mode="json"),
                "audience": context.constraints.audience,
                "platform": context.constraints.platform,
                "depth": context.constraints.depth.value,
                "target_length_words": context.constraints.target_length_words,
            },
            schema=ArchitectureProposal,
            override=self._override,
        )
        proposal = generated.value
        check_claims(proposal, self._source_model)

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.CONTENT_ARCHITECTURE,
            content=proposal.model_dump(mode="json"),
            role="content_architecture",
            parent=self._parent.snapshot if self._parent is not None else None,
        )
        architecture, concepts = self._store(context, execution, proposal, snapshot)
        self._record_decision(context, execution, proposal)

        return StageResult(
            value=ArchitectureOutcome(
                proposal=proposal, architecture=architecture, concepts=concepts
            ),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            detail={
                "articles": len(proposal.articles),
                "selected": proposal.decision.selected,
                "confidence": proposal.decision.confidence,
            },
        )

    def _store(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        proposal: ArchitectureProposal,
        snapshot: ArtifactSnapshot,
    ) -> tuple[domain_models.ContentArchitecture, tuple[domain_models.ArticleConcept, ...]]:
        """Persist the architecture and its article concepts."""
        architecture = domain_models.ContentArchitecture(
            id=uuid.uuid4().hex,
            project_id=context.project_id,
            summary=proposal.decision.rationale or proposal.articles[0].thesis,
            snapshot_id=snapshot.id,
            created_by_execution_id=execution.id,
            parent_id=self._parent.id if self._parent is not None else None,
        )
        context.session.add(architecture)
        context.session.flush()

        concepts = tuple(
            domain_models.ArticleConcept(
                id=article.id,
                architecture_id=architecture.id,
                title=article.title,
                angle=article.evidence_summary,
                thesis=article.thesis,
                ordinal=ordinal,
                created_by_execution_id=execution.id,
            )
            for ordinal, article in enumerate(proposal.articles)
        )
        context.session.add_all(concepts)
        context.session.flush()
        return architecture, concepts

    def _record_decision(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        proposal: ArchitectureProposal,
    ) -> models.DecisionRecord:
        """Record the shape decision with everything needed to review it."""
        decision = proposal.decision
        return context.recorder.record_decision(
            execution,
            decision_type="content_architecture",
            decided_by=ARCHITECTURE_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "articles": [
                    {
                        "id": article.id,
                        "thesis": article.thesis,
                        "supporting_claim_ids": list(article.supporting_claim_ids),
                        "thin_content_risk": article.thin_content_risk.value,
                    }
                    for article in proposal.articles
                ],
                "alternatives_considered": [
                    alternative.model_dump(mode="json")
                    for alternative in decision.alternatives_considered
                ],
                "competing_theses": list(proposal.competing_theses),
                "confidence": decision.confidence,
                "uncertainties": list(decision.uncertainties),
                "series": proposal.series.model_dump(mode="json"),
            },
            outcome=decision.selected,
            rationale=decision.rationale,
        )


def check_claims(proposal: ArchitectureProposal, source_model: SourceModel) -> None:
    """Fail unless every claim the architecture argues from exists in the source model.

    The architecture is where scope is decided, and a scope resting on a claim
    nobody extracted would be discovered three stages later as an article that
    cannot cite its own thesis.
    """
    known = {claim.id for claim in source_model.claims}
    dangling = sorted(proposal.cited_claim_ids() - known)
    if dangling:
        raise EvidenceError(
            f"the architecture argues from {', '.join(dangling)}, which "
            f"{'is' if len(dangling) == 1 else 'are'} not in the source model; an article "
            "cannot be built on a claim nobody extracted"
        )


__all__ = [
    "ARCHITECTURE_STAGE",
    "ArchitectureOutcome",
    "ProposeContentArchitecture",
    "check_claims",
]
