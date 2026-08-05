"""Substantive review, and the author's authority over it (phase 07 §8).

plan/07 → *ReviewSubstantively* and *Review acceptance*: argument and accuracy
rather than sentence polish, findings carrying the full structured field set, and
an author free to accept, reject or edit each one — because reviewer output is
evidence, not an unquestionable instruction.

Three decisions shape this module.

**Findings are rows, not payload.** The review document is snapshotted like every
other artefact, but each finding is also a row with a status the author sets. A
finding the author has to argue with is a thing with a lifecycle, and a lifecycle
inside a JSON blob is a lifecycle nothing can query.

**Nothing is deleted.** A rejected finding keeps its text and gains a reason. That
is what makes the next round able to tell "already argued and dismissed" from
"never raised" — and the *reason* is what makes the dismissal reviewable rather
than merely recorded.

**A repeat is suppressed, not dropped.** When the reviewer raises a point the
author already dismissed, with the same evidence behind it, the finding is stored
and marked suppressed rather than hidden. The reviewer is entitled to reopen a
point it has learned something new about, and the fingerprint includes the evidence
precisely so that "something new" produces a different finding.

The exit edge depends on what was found, so it is chosen from the result: anything
blocking or major needs a plan, and a review that found only polish means the
substance is settled and the article goes to voice alignment.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import ClassVar

from sqlalchemy import func, select

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType, FindingStatus, IssueSeverity
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import InterventionType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.errors import EvidenceError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import (
    ArticleBriefDocument,
    ArticleDraft,
    ReviewFinding,
    ReviewIssueReport,
    SourceModel,
    SubstantiveReview,
)
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
REVIEW_STAGE = "review_substantively"

#: The stage a person's decisions about findings are recorded under.
ACCEPTANCE_STAGE = "accept_review_findings"


def forces_iteration(severity: IssueSeverity) -> bool:
    """Whether a finding at this severity is worth another revision round.

    A one-line wrapper over the enum, kept because *this* is the rule plan/07 names
    ("optional never forces a full iteration") and a reader looking for it should
    find it under that name rather than as a property three files away.
    """
    return severity.forces_iteration


class ReviewOutcome:
    """The review, the row it was stored as, and its findings as rows."""

    __slots__ = ("findings", "review", "row")

    def __init__(
        self,
        *,
        review: SubstantiveReview,
        row: domain_models.Review,
        findings: tuple[domain_models.ReviewIssue, ...],
    ) -> None:
        self.review = review
        self.row = row
        self.findings = findings

    def report(self) -> ReviewIssueReport:
        """What the author has decided so far about this round's findings."""
        return ReviewIssueReport(
            accepted=tuple(f.ref for f in self.findings if f.status is FindingStatus.ACCEPTED),
            rejected=tuple(f.ref for f in self.findings if f.status is FindingStatus.REJECTED),
            edited=tuple(f.ref for f in self.findings if f.status is FindingStatus.EDITED),
            suppressed=tuple(f.ref for f in self.findings if f.status is FindingStatus.SUPPRESSED),
        )


class ReviewSubstantively:
    """Review one article version for argument and accuracy."""

    name: ClassVar[str] = REVIEW_STAGE
    impl_version: ClassVar[str] = "1.0"

    #: The stage declares no edges. The entry was taken by whatever produced the
    #: version, and the *exit* depends on what the review found, so it is reported
    #: with the result the way gap analysis reports its own.
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None

    def __init__(
        self,
        *,
        draft: ArticleDraft,
        version: domain_models.ArticleVersion,
        version_snapshot: ArtifactSnapshot,
        brief: ArticleBriefDocument,
        source_model: SourceModel,
        previous_findings: Sequence[domain_models.ReviewIssue] = (),
        transitions: bool = True,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._draft = draft
        self._version = version
        self._version_snapshot = version_snapshot
        self._brief = brief
        self._source_model = source_model
        self._previous = tuple(previous_findings)
        self._transitions = transitions
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[ReviewOutcome]:
        """Review, check the references, store the findings, choose the next edge."""
        require_permitted_provider(context, REVIEW_STAGE, override=self._override)
        context.recorder.record_input(execution, self._version_snapshot, role="article_version")

        generated = await context.generator.generate(
            execution,
            stage=REVIEW_STAGE,
            template_id=REVIEW_STAGE,
            template_version=self._template_version,
            variables={
                "draft": self._draft.model_dump(mode="json"),
                "brief": self._brief.model_dump(mode="json"),
                "source_model": self._source_model.model_dump(mode="json"),
                "dismissed": [
                    {"description": finding.description, "reason": finding.decision_reason}
                    for finding in self._previous
                    if finding.status is FindingStatus.REJECTED
                ],
            },
            schema=SubstantiveReview,
            override=self._override,
        )
        assessed = generated.value
        check_findings(assessed, self._source_model)

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.REVIEW,
            content=assessed.model_dump(mode="json"),
            role="review",
        )
        row, findings = self._store(context, execution, assessed, snapshot)

        return StageResult(
            value=ReviewOutcome(review=assessed, row=row, findings=findings),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            # What was found decides where the run goes: anything blocking or major
            # needs a plan, and a review that found only polish means the substance
            # is settled (plan/05's `accept_review` edge says exactly that).
            exit_action=self._exit_for(assessed),
            detail={
                "issues": len(assessed.issues),
                "iteration_forcing": len(assessed.iteration_forcing),
                "round": row.round,
            },
        )

    def _store(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        assessed: SubstantiveReview,
        snapshot: ArtifactSnapshot,
    ) -> tuple[domain_models.Review, tuple[domain_models.ReviewIssue, ...]]:
        """Persist the review and its findings, suppressing settled repeats."""
        dismissed = {
            finding.fingerprint: finding
            for finding in self._previous
            if finding.status is FindingStatus.REJECTED
        }
        row = domain_models.Review(
            id=uuid.uuid4().hex,
            article_version_id=self._version.id,
            verdict=assessed.verdict,
            round=self._round(context),
            snapshot_id=snapshot.id,
            created_by_execution_id=execution.id,
        )
        context.session.add(row)
        context.session.flush()

        findings = tuple(
            self._finding_row(execution, row, issue, dismissed) for issue in assessed.issues
        )
        context.session.add_all(findings)
        context.session.flush()
        return row, findings

    def _exit_for(self, assessed: SubstantiveReview) -> WorkflowAction | None:
        """Where the run goes next, or nowhere.

        ``transitions=False`` is for a re-review that is not the run advancing —
        the author asked for a second opinion on the version already under
        discussion — and claiming an edge for it would move the machine on the
        strength of a question rather than a change.
        """
        if not self._transitions:
            return None
        return (
            WorkflowAction.REQUIRE_REVISION_PLAN
            if assessed.requires_iteration
            else WorkflowAction.ACCEPT_REVIEW
        )

    def _round(self, context: PipelineContext) -> int:
        """Which review round this is for the version, counted from what is stored.

        Counted rather than passed in: the caller that knows about the previous
        findings is not always the caller that knows how many rounds preceded them,
        and a number derived from the database cannot disagree with it.
        """
        stored = context.session.execute(
            select(func.count())
            .select_from(domain_models.Review)
            .where(domain_models.Review.article_version_id == self._version.id)
        ).scalar_one()
        return int(stored)

    def _finding_row(
        self,
        execution: models.StageExecution,
        review: domain_models.Review,
        issue: ReviewFinding,
        dismissed: dict[str, domain_models.ReviewIssue],
    ) -> domain_models.ReviewIssue:
        """One finding as a row, suppressed if the author already dismissed it."""
        fingerprint = issue.fingerprint()
        settled = dismissed.get(fingerprint)
        return domain_models.ReviewIssue(
            id=uuid.uuid4().hex,
            review_id=review.id,
            ref=issue.id,
            severity=issue.severity,
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
            fingerprint=fingerprint,
            status=FindingStatus.SUPPRESSED if settled else FindingStatus.PROPOSED,
            decision_reason=(
                f"already dismissed in an earlier round ({settled.decision_reason}) and raised "
                "again with no new evidence"
                if settled
                else ""
            ),
            created_by_execution_id=execution.id,
        )


class ReviewLedger:
    """The author's decisions about findings, recorded as they are made.

    Every method writes a user intervention as well as setting the status: the
    status is what the planner reads, the intervention is what the human-control
    view reads, and they answer different questions about the same act.
    """

    def __init__(self, context: PipelineContext, execution: models.StageExecution) -> None:
        self._context = context
        self._execution = execution
        self._decided: list[domain_models.ReviewIssue] = []

    @property
    def execution(self) -> models.StageExecution:
        """The execution these decisions are recorded against."""
        return self._execution

    def accept(self, finding: domain_models.ReviewIssue, *, decided_by: str) -> None:
        """Take the finding as it stands."""
        self._decide(finding, FindingStatus.ACCEPTED, decided_by, "", InterventionType.APPROVAL)

    def reject(self, finding: domain_models.ReviewIssue, *, decided_by: str, reason: str) -> None:
        """Disagree with the finding, keeping it and the disagreement on the record.

        The reason is mandatory. A rejection with no reason cannot be distinguished
        from an oversight next round, which is exactly when it matters.
        """
        if not reason.strip():
            raise ValueError(
                "a rejected finding needs a reason: without one, the next round cannot "
                "tell a considered dismissal from an oversight"
            )
        self._decide(
            finding, FindingStatus.REJECTED, decided_by, reason, InterventionType.REJECTION
        )

    def edit(
        self,
        finding: domain_models.ReviewIssue,
        *,
        decided_by: str,
        recommended_correction: str,
        reason: str = "",
    ) -> None:
        """Accept the finding but rewrite what it asks for."""
        finding.recommended_correction = recommended_correction
        self._decide(finding, FindingStatus.EDITED, decided_by, reason, InterventionType.EDIT)

    def accepted(self) -> tuple[domain_models.ReviewIssue, ...]:
        """The findings a revision plan should be built from, in decision order."""
        return tuple(finding for finding in self._decided if finding.status.is_actionable)

    def _decide(
        self,
        finding: domain_models.ReviewIssue,
        status: FindingStatus,
        decided_by: str,
        reason: str,
        intervention: InterventionType,
    ) -> None:
        if not decided_by:
            raise ValueError("decided_by is required: an unattributed decision is unreviewable")

        finding.status = status
        finding.decided_by = decided_by
        finding.decision_reason = reason
        self._context.session.flush()
        self._decided.append(finding)

        self._context.recorder.record_user_intervention(
            self._execution,
            user_id=decided_by,
            intervention_type=intervention,
            payload={
                "finding_id": finding.id,
                "finding_ref": finding.ref,
                "status": status.value,
                "reason": reason,
                "severity": finding.severity.value,
            },
        )


def open_review_ledger(context: PipelineContext) -> ReviewLedger:
    """Open a ledger with its own stage execution to record decisions against."""
    execution = context.engine.begin_stage(ACCEPTANCE_STAGE, impl_version="1.0")
    return ReviewLedger(context, execution)


def check_findings(review: SubstantiveReview, source_model: SourceModel) -> None:
    """Refuse a review whose findings point at claims that do not exist.

    A finding citing a claim nobody extracted cannot be acted on *or* argued with:
    the author cannot check it, and the rewriter cannot correct against it.

    Each bad reference is quoted, because ``source_ref`` holds one claim id and a
    reviewer that puts several there produces a *single* value naming none of
    them. Joined bare, four such values read as seventeen unknown ids, every one
    of which is in the source model — which is a message that sends the reader
    looking for the wrong bug. Quoted, ``'c010, c014, c033'`` says what it is.
    """
    known = {claim.id for claim in source_model.claims}
    dangling = sorted(review.cited_claim_ids() - known)
    if dangling:
        raise EvidenceError(
            f"the review cites {', '.join(repr(ref) for ref in dangling)}, which "
            f"{'is' if len(dangling) == 1 else 'are'} not in the source model; a finding "
            "pointing at nothing can be neither checked nor corrected "
            "(source_ref holds one claim id — further claims go in evidence)"
        )


__all__ = [
    "ACCEPTANCE_STAGE",
    "REVIEW_STAGE",
    "ReviewLedger",
    "ReviewOutcome",
    "ReviewSubstantively",
    "check_findings",
    "forces_iteration",
    "open_review_ledger",
]
