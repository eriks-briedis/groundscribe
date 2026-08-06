"""Cutting a claim the source does not support, without spending a round.

IMPROVEMENTS §11. `unsupported_claims` is a publication condition and rightly so:
an article stating as fact something the source does not support is not
publishable, whatever it scored. But the condition is binary and the remedy had
one size — a fabricated mechanism and a six-word rhetorical flourish failed the
article identically, and both were answered with a full substantive round.

What that cost, measured on the run of 2026-08-06. The article scored 90.55
against a bar of 85, cleared every floor, and carried eight deductions of which
none were blocking. One publication condition failed it: six words in the opening
paragraph saying a draft beats a blank page, which the source does not support.
The round that produced that article ran 31 minutes, six model calls, 315k input
tokens and ten triage decisions by hand — to arrive at a draft failing on a
sentence a person removes in four seconds.

And the loop was anti-convergent for it. Overall by round: 91.75 → 92.05 → 91.1 →
90.55. Each round removed the claim it was sent back for and churned enough prose
to earn fresh voice deductions; three of the final eight were `voice_adherence`,
sitting at 76 against a floor of 75. One more round of the same trade would have
failed the article on voice, having been sent back for fidelity. That is not a
prompt defect. A stage told to correct an argument rewrites paragraphs, rewritten
paragraphs are re-voiced, and the rubric charges for the churn under a dimension
nobody sent the article back for.

**The permission is structural, not instructed.** The model returns edits and
never a body (:class:`ClaimsCorrected`), so the pipeline applies the
substitutions itself and a passage nobody named cannot move. Every other prose
stage returns the whole article and declares its changes, which leaves an
undeclared edit representable; here it is not. That is the lesson IMPROVEMENTS §2
records — a field a schema validates and a prompt does not describe is a field
the model fills by guessing — applied to a permission rather than to a field.

**It charges no round.** Rounds are spent in `route()` for a revision loop, and
this is not one; it is the correction of a defect the score has already localised
to a span. The entry action is `CORRECT_CLAIMS`, which is deliberately not a
`ROUTE_REVISION` destination.

**The re-score is the check.** The trigger is conservative — every floor clear, no
blocking deduction, nothing failing but unsupported claims — and the floors are a
proxy for "the argument does not need this claim". If the article comes back below
a floor it was above, the cut was load-bearing and the round was owed after all.
"""

from __future__ import annotations

from typing import ClassVar

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.drafting import DraftOutcome, store_version
from groundscribe.stages.errors import ClaimCorrectionError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import ArticleDraft, ClaimsCorrected
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
CORRECT_CLAIMS_STAGE = "correct_claims"


class CorrectClaims:
    """Remove or qualify the passages a score named, and touch nothing else."""

    name: ClassVar[str] = CORRECT_CLAIMS_STAGE
    impl_version: ClassVar[str] = "1.0"
    #: The entry was taken by `revise`, which chose this over routing. The exit
    #: carries the corrected version straight to scoring — no voice pass, because
    #: prose that only lost a clause has not been re-voiced.
    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = WorkflowAction.SUBMIT_CLAIM_CORRECTION

    def __init__(
        self,
        *,
        previous: ArticleDraft,
        parent: domain_models.ArticleVersion,
        concept: domain_models.ArticleConcept,
        claims: tuple[str, ...],
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._previous = previous
        self._parent = parent
        self._concept = concept
        self._claims = claims
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[DraftOutcome]:
        """Ask for the cuts, check every one, apply them, store the version."""
        require_permitted_provider(context, CORRECT_CLAIMS_STAGE, override=self._override)
        if self._parent.snapshot is not None:
            context.recorder.record_input(execution, self._parent.snapshot, role="article_version")

        generated = await context.generator.generate(
            execution,
            stage=CORRECT_CLAIMS_STAGE,
            template_id=CORRECT_CLAIMS_STAGE,
            template_version=self._template_version,
            variables={
                "body": self._previous.body,
                "claims": list(self._claims),
                "thesis": self._previous.thesis,
                "markers": [item.marker for item in self._previous.unresolved],
            },
            schema=ClaimsCorrected,
            override=self._override,
        )
        corrected = generated.value
        check_corrections(corrected, self._previous, self._claims)

        body = apply_corrections(self._previous.body, corrected)
        # The new version is the old one with its prose replaced, exactly as a
        # voice pass builds one: nothing else came back from the model, so
        # nothing else can have changed.
        cut = self._previous.model_copy(update={"body": body})

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.ARTICLE_VERSION,
            content=cut.model_dump(mode="json"),
            role="article_version",
            parent=self._parent.snapshot,
        )
        article, version = store_version(
            context, execution, cut, snapshot, concept=self._concept, parent=self._parent
        )
        self._record_decision(context, execution, corrected)

        return StageResult(
            value=DraftOutcome(draft=cut, article=article, version=version),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            detail={
                "corrections": len(corrected.corrections),
                "refused": len(corrected.refused),
                "characters_removed": len(self._previous.body) - len(body),
            },
        )

    def _record_decision(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        corrected: ClaimsCorrected,
    ) -> models.DecisionRecord:
        """Write down what was cut, and what the stage would not cut.

        A refusal is the more interesting half. It is the stage saying the claim
        is load-bearing — that removing it would leave the article arguing
        nothing — which is a `factual_gap` for the author to close with more
        source material, and a decision somebody may well want to disagree with.
        """
        return context.recorder.record_decision(
            execution,
            decision_type="claim_correction",
            decided_by=CORRECT_CLAIMS_STAGE,
            decided_by_type=ActorType.POLICY,
            policy_version=self.impl_version,
            inputs={
                "claims": list(self._claims),
                "cut": [
                    {"claim": item.claim, "before": item.before, "after": item.after}
                    for item in corrected.corrections
                ],
                "refused": list(corrected.refused),
            },
            outcome="corrected" if corrected.corrections else "refused",
            rationale=(
                "the score's only failure was claims the source does not support, and "
                "every floor was clear; cutting them is the remedy, and it is not a round"
            ),
        )


def check_corrections(
    corrected: ClaimsCorrected, previous: ArticleDraft, claims: tuple[str, ...]
) -> None:
    """Refuse a pass that edits anything it was not asked to.

    Four checks, and between them they are the permission. Instructing a model to
    "only remove or qualify" makes that a hope; refusing an output that did
    otherwise makes it a property.

    A correction must quote text the article actually contains, must name one of
    the claims the score failed on, must not grow the passage it replaces, and
    must not delete an unresolved marker. The last is the same rule the voice
    pass keeps: deleting a marker publishes a hole as though it were an answer.
    """
    named = set(claims)
    for correction in corrected.corrections:
        if correction.before not in previous.body:
            raise ClaimCorrectionError(
                f"the pass says it cut {correction.before!r}, which the article does not "
                "contain; an edit that cannot be located is one nobody can check"
            )
        if correction.claim not in named:
            raise ClaimCorrectionError(
                f"the pass corrected {correction.claim!r}, which is not one of the claims "
                f"the score failed on ({', '.join(sorted(named))}); this stage may only "
                "touch what the score localised"
            )
        if len(correction.after) >= len(correction.before):
            raise ClaimCorrectionError(
                f"the replacement for {correction.before!r} is not shorter than it; this "
                "stage removes and qualifies, and anything else is a rewrite wearing a "
                "correction's name"
            )

    for refusal in corrected.refused:
        if refusal not in named:
            raise ClaimCorrectionError(
                f"the pass refused {refusal!r}, which is not one of the claims the score "
                "failed on; a refusal has to be about something that was asked"
            )

    applied = apply_corrections(previous.body, corrected)
    lost = [item.marker for item in previous.unresolved if item.marker not in applied]
    if lost:
        raise ClaimCorrectionError(
            f"the pass removed the unresolved marker(s) {', '.join(lost)}; deleting a "
            "marker publishes a hole as though it were an answer"
        )


def apply_corrections(body: str, corrected: ClaimsCorrected) -> str:
    """The article with each named passage cut or narrowed, and nothing else touched.

    Applied here rather than accepted from the model, which is the whole design:
    a stage that returned prose could change anything and declare only some of
    it, and no guard can find what it was not told about.

    ``replace`` with a count of one, because a passage occurring twice is two
    passages and the score named one. Replacing both would edit text nobody
    looked at.
    """
    for correction in corrected.corrections:
        body = body.replace(correction.before, correction.after, 1)
    return body
