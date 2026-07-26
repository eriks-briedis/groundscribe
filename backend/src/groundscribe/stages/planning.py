"""Revision planning: accepted feedback into instructions (phase 07 §9).

plan/07 → *CreateRevisionPlan*: convert accepted feedback into a coherent plan —
required versus optional changes, sections to preserve, claims that must not
change, sections to remove or move, whether the brief or architecture must reopen,
and the expected effect on scores — reconciling contradictory findings, stored as
its own immutable artefact whose record explains what was combined, deferred or
rejected.

This stage exists to prevent one specific failure, which plan/07 names in its
risks: *a rewriter blindly applying reviewer suggestions*. Two findings that
disagree, applied in order, produce an article that argues with itself; a finding
the author rejected, applied anyway, overrides the person the article belongs to.
So the plan is a filter with a memory, and the stage enforces two rules the model
cannot be trusted to keep on its own:

- every finding the author accepted is either addressed by a change or explained
  in a reconciliation. Dropping one silently would hand the rewriter half a
  decision with nothing to show the other half existed;
- a claim promised as unchangeable must be a claim that exists, or the promise
  protects nothing.

Planning does not move the run. Approving the plan is the author's act — the same
shape as approving an architecture — and a plan that concludes *the brief was
wrong* says so and asks for a person rather than rewriting against a contract it
disagrees with.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import ClassVar

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType, FindingStatus
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, InterventionType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.errors import PlanContractError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import (
    ArticleBriefDocument,
    ArticleDraft,
    RevisionPlanDocument,
    SubstantiveReview,
)
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
PLAN_STAGE = "create_revision_plan"


class PlanOutcome:
    """The plan, the row it was stored as, and the review it came from."""

    __slots__ = ("plan", "review_id", "row", "snapshot")

    def __init__(
        self,
        *,
        plan: RevisionPlanDocument,
        row: domain_models.RevisionPlan,
        review_id: str,
        snapshot: ArtifactSnapshot,
    ) -> None:
        self.plan = plan
        self.row = row
        self.review_id = review_id
        self.snapshot = snapshot


class CreateRevisionPlan:
    """Turn the findings the author accepted into what the rewrite will do."""

    name: ClassVar[str] = PLAN_STAGE
    impl_version: ClassVar[str] = "1.0"
    #: No edges. The run is already parked at ``revision_plan_required``, and
    #: leaving it is the author's decision, not the planner's.
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None

    def __init__(
        self,
        *,
        review: SubstantiveReview,
        review_row: domain_models.Review,
        review_snapshot: ArtifactSnapshot,
        findings: Sequence[domain_models.ReviewIssue],
        draft: ArticleDraft,
        brief: ArticleBriefDocument,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._review = review
        self._review_row = review_row
        self._review_snapshot = review_snapshot
        self._findings = tuple(findings)
        self._draft = draft
        self._brief = brief
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[PlanOutcome]:
        """Plan the revision, check nothing was dropped, store it for approval."""
        require_permitted_provider(context, PLAN_STAGE, override=self._override)
        context.recorder.record_input(execution, self._review_snapshot, role="review")

        accepted = tuple(finding for finding in self._findings if finding.status.is_actionable)
        dismissed = tuple(
            finding for finding in self._findings if finding.status is FindingStatus.REJECTED
        )
        generated = await context.generator.generate(
            execution,
            stage=PLAN_STAGE,
            template_id=PLAN_STAGE,
            template_version=self._template_version,
            variables={
                "accepted": [_finding_view(finding) for finding in accepted],
                "dismissed": [_finding_view(finding) for finding in dismissed],
                "draft": self._draft.model_dump(mode="json"),
                "brief": self._brief.model_dump(mode="json"),
                "verdict": self._review.verdict,
            },
            schema=RevisionPlanDocument,
            override=self._override,
        )
        planned = generated.value
        check_plan(planned, accepted=accepted, draft=self._draft)

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.REVISION_PLAN,
            content=planned.model_dump(mode="json"),
            role="revision_plan",
        )
        row = domain_models.RevisionPlan(
            id=uuid.uuid4().hex,
            review_id=self._review_row.id,
            summary=planned.summary,
            snapshot_id=snapshot.id,
            created_by_execution_id=execution.id,
        )
        context.session.add(row)
        context.session.flush()

        self._record_decision(context, execution, planned)
        if planned.reopen_brief or planned.reopen_architecture:
            self._request_reopen(context, execution, planned)

        return StageResult(
            value=PlanOutcome(
                plan=planned, row=row, review_id=self._review_row.id, snapshot=snapshot
            ),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            detail={
                "required_changes": len(planned.required_changes),
                "optional_changes": len(planned.optional_changes),
                "reconciliations": len(planned.reconciliations),
            },
        )

    def _record_decision(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        planned: RevisionPlanDocument,
    ) -> models.DecisionRecord:
        """Record what was combined, deferred and rejected, and why.

        plan/07 asks for a record that *explains* the reconciliation, not one that
        merely stores the result. The rationales travel with it, because the
        argument a reader will have later is about whether the contradiction was
        resolved the right way — not about whether it was noticed.
        """
        return context.recorder.record_decision(
            execution,
            decision_type="revision_plan",
            decided_by=PLAN_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "required_changes": len(planned.required_changes),
                "optional_changes": len(planned.optional_changes),
                "reconciliations": [
                    item.model_dump(mode="json") for item in planned.reconciliations
                ],
                "preserve_sections": list(planned.preserve_sections),
                "claims_that_must_not_change": list(planned.claims_that_must_not_change),
                "reopen_brief": planned.reopen_brief,
                "reopen_architecture": planned.reopen_architecture,
            },
            outcome=planned.summary,
            rationale=planned.expected_score_effect,
        )

    def _request_reopen(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        planned: RevisionPlanDocument,
    ) -> None:
        """Announce that the feedback points upstream of the prose.

        A plan concluding the brief or architecture was wrong is not a rewrite
        instruction; the phase-05 table has edges for both (`return_to_brief`,
        `reopen_architecture`) and both are a person's to take.
        """
        context.recorder.emit(
            event_type="intervention.requested",
            actor_type=ActorType.SYSTEM,
            actor_id=PLAN_STAGE,
            execution=execution,
            payload={
                "reason": "the revision plan says the problem is upstream of the prose",
                "reopen_brief": planned.reopen_brief,
                "reopen_architecture": planned.reopen_architecture,
            },
        )


def approve_revision_plan(
    context: PipelineContext, *, plan: PlanOutcome, approved_by: str
) -> models.DecisionRecord:
    """Approve a plan, which is what opens the rewrite (phase 07 §9 → §10)."""
    if not approved_by:
        raise ValueError("approved_by is required: an anonymous approval is unreviewable")

    execution = context.engine.begin_stage("approve_revision_plan", impl_version="1.0")
    context.recorder.record_user_intervention(
        execution,
        user_id=approved_by,
        intervention_type=InterventionType.APPROVAL,
        payload={"revision_plan_id": plan.row.id, "summary": plan.plan.summary},
    )
    recorded = context.engine.apply(
        WorkflowAction.APPROVE_REVISION_PLAN,
        actor_id=approved_by,
        actor_type=ActorType.USER,
        artifacts=(plan.snapshot,),
        rationale="the author approved the revision plan",
    )
    context.recorder.complete_stage(execution)
    return recorded.decision


def check_plan(
    planned: RevisionPlanDocument,
    *,
    accepted: Sequence[domain_models.ReviewIssue],
    draft: ArticleDraft,
) -> None:
    """Refuse a plan that drops a decision or protects a claim that is not there."""
    addressed = planned.addressed_findings()
    dropped = sorted(finding.ref for finding in accepted if finding.ref not in addressed)
    if dropped:
        raise PlanContractError(
            f"the plan neither applies nor explains {', '.join(dropped)}, which the author "
            "accepted; a half-applied decision handed to a rewriter is worse than none, "
            "because nothing downstream shows the other half existed"
        )

    unknown = sorted(set(planned.claims_that_must_not_change) - set(draft.claims_used))
    if unknown:
        raise PlanContractError(
            f"the plan promises to preserve {', '.join(unknown)}, which the draft does not "
            "use; a promise about something that is not there protects nothing"
        )


def _finding_view(finding: domain_models.ReviewIssue) -> dict[str, object]:
    """One finding as the planner sees it, decision included."""
    return {
        "ref": finding.ref,
        "severity": finding.severity.value,
        "category": finding.category,
        "location": finding.location,
        "description": finding.description,
        "recommended_correction": finding.recommended_correction,
        "status": finding.status.value,
        "decision_reason": finding.decision_reason,
    }


__all__ = [
    "PLAN_STAGE",
    "CreateRevisionPlan",
    "PlanOutcome",
    "approve_revision_plan",
    "check_plan",
]
