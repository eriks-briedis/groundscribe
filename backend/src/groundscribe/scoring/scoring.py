"""Scoring one article version against the rubric (phase 08).

plan/08 → *ScoreArticle stage*, *Evidence-backed deductions*, *Editorial-score vs
routing-result separation*, *Evaluation provenance*.

The stage asks a model for seven dimension scores and the deductions behind them,
then hands those numbers to the rubric. It does not ask for the overall and it
does not ask whether the article passes: both are computed here, under a named
rubric version, from weights and thresholds a person configured. What the model
contributes is judgement about the article; what the rubric contributes is the
policy for turning judgement into a decision, and keeping them apart is what makes
a score comparable to the one before it.

**The stage does not route.** A failing score takes the ``score_failed`` edge to
``revision_required`` and stops. That state is where a person may approve despite
the score, add source material or narrow the thesis, and a stage that routed on
its way out would step past that pause on every failure. The category the failure
*should* route to is computed and reported; :func:`route_failure` applies it when
a caller — or a person — decides to proceed.

**A score that cannot name its inputs is not written.** plan/08 requires every
score to link to the exact article, source model, brief, voice profile, rubric and
threshold policy behind it. That is checked before the evaluation row is created
rather than filtered when it is read: an unlinked score sitting in the table will
eventually be compared against, and the comparison will look perfectly fine.
"""

from __future__ import annotations

from typing import Any, ClassVar

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType, IssueSeverity
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models
from groundscribe.scoring.rubric import (
    ScoreAssessment,
    ScoringRubric,
    default_scoring_rubric,
)
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.errors import ScoreContractError
from groundscribe.stages.extraction import require_permitted_provider
from groundscribe.stages.schemas import (
    ArticleBriefDocument,
    ArticleDraft,
    ArticleScore,
    ScoreDeduction,
    SourceModel,
    VoiceProfileDocument,
)
from groundscribe.workflow.policy import FailureCategory
from groundscribe.workflow.states import WorkflowAction

#: The stage name, routing key and prompt template id.
SCORE_STAGE = "score_article"

#: Every linkage key a stored score must carry. plan/08 lists them; naming them
#: here makes "fully linked" a checkable property rather than a habit.
REQUIRED_LINKAGE = (
    "article_version_id",
    "brief_snapshot_id",
    "source_model_snapshot_id",
    "voice_profile",
    "rubric_version",
    "content_type",
    "passing_policy",
)

#: Severity order for picking which failure a run is routed on. Not the enum's
#: declaration order — this is a ranking, and relying on member order would make
#: reordering the enum for readability silently change routing.
_SEVERITY_RANK = {
    IssueSeverity.BLOCKING: 3,
    IssueSeverity.MAJOR: 2,
    IssueSeverity.MINOR: 1,
    IssueSeverity.OPTIONAL: 0,
}


class ScoreOutcome:
    """The score sheet, the verdict, the failure class, and the record of both."""

    __slots__ = ("assessment", "category", "evaluation", "score")

    def __init__(
        self,
        *,
        score: ArticleScore,
        assessment: ScoreAssessment,
        evaluation: models.EvaluationRun,
        category: FailureCategory | None,
    ) -> None:
        self.score = score
        self.assessment = assessment
        self.evaluation = evaluation
        self.category = category


class ScoreArticle:
    """Score one article version, and decide nothing else."""

    name: ClassVar[str] = SCORE_STAGE
    impl_version: ClassVar[str] = "1.0"

    #: No entry edge: the run arrives at scoring from a voice pass or a rewrite,
    #: and whichever it was has already taken it. The exit depends on the verdict.
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
        voice: VoiceProfileDocument,
        brief_snapshot: ArtifactSnapshot | None = None,
        source_model_snapshot: ArtifactSnapshot | None = None,
        rubric: ScoringRubric | None = None,
        transitions: bool = True,
        template_version: str | None = None,
        override: RouteOverride | None = None,
    ) -> None:
        self._draft = draft
        self._version = version
        self._version_snapshot = version_snapshot
        self._brief = brief
        self._brief_snapshot = brief_snapshot
        self._source_model = source_model
        self._source_model_snapshot = source_model_snapshot
        self._voice = voice
        self._rubric = rubric if rubric is not None else default_scoring_rubric()
        self._transitions = transitions
        self._template_version = template_version
        self._override = override

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[ScoreOutcome]:
        """Score the version, assess it against the rubric, record both answers."""
        require_permitted_provider(context, SCORE_STAGE, override=self._override)
        context.recorder.record_input(execution, self._version_snapshot, role="article_version")

        generated = await context.generator.generate(
            execution,
            stage=SCORE_STAGE,
            template_id=SCORE_STAGE,
            template_version=self._template_version,
            variables={
                "draft": self._draft.model_dump(mode="json"),
                "brief": self._brief.model_dump(mode="json"),
                "source_model": self._source_model.model_dump(mode="json"),
                "voice": self._voice.model_dump(mode="json"),
                "dimensions": [dimension.value for dimension in self._rubric_dimensions()],
            },
            schema=ArticleScore,
            override=self._override,
        )
        scored = generated.value
        assessment = self._rubric.assess(
            scored.scores,
            depth=context.constraints.depth,
            blocking_issues=[deduction.mismatch for deduction in scored.blocking],
            unsupported_claims=scored.unsupported_claims,
            unmet_requirements=[
                deduction.requirement or deduction.mismatch
                for deduction in scored.deductions
                if deduction.rubric_required
            ],
        )
        category = failure_category(scored) if not assessment.passed else None

        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.ARTICLE_SCORE,
            content=scored.model_dump(mode="json"),
            role="article_score",
            parent=self._version_snapshot,
        )
        evaluation = self._record(context, execution, scored, assessment, category)

        return StageResult(
            value=ScoreOutcome(
                score=scored, assessment=assessment, evaluation=evaluation, category=category
            ),
            outputs=(snapshot,),
            invocations=generated.attempts,
            usage=generated.usage,
            exit_action=self._exit_for(assessment),
            detail={
                "overall": assessment.overall,
                "passed": assessment.passed,
                "failures": len(assessment.failures),
                "category": category.value if category is not None else None,
                "rubric_version": assessment.rubric_version,
                "content_type": assessment.weights.content_type,
            },
        )

    def _rubric_dimensions(self) -> tuple[Any, ...]:
        """The dimensions the prompt asks about, taken from the rubric it scores under."""
        return tuple(self._rubric.weights_for(None).weights)

    def _exit_for(self, assessment: ScoreAssessment) -> WorkflowAction | None:
        """The edge the verdict earns, or none when the run is not advancing.

        ``transitions=False`` is for a re-score that is a question rather than a
        step — scoring the same version under a different rubric to compare them —
        and letting that move the machine would make the loop's history a fiction.
        """
        if not self._transitions:
            return None
        return WorkflowAction.SCORE_PASSED if assessment.passed else WorkflowAction.SCORE_FAILED

    def _record(
        self,
        context: PipelineContext,
        execution: models.StageExecution,
        scored: ArticleScore,
        assessment: ScoreAssessment,
        category: FailureCategory | None,
    ) -> models.EvaluationRun:
        """Write the evaluation, refusing one that cannot say what produced it."""
        linkage = {
            "article_version_id": self._version.id,
            "article_snapshot_id": self._version_snapshot.id,
            "brief_snapshot_id": _snapshot_id(self._brief_snapshot),
            "source_model_snapshot_id": _snapshot_id(self._source_model_snapshot),
            "voice_profile": self._voice.name,
            "voice_profile_version": self._voice.version,
            "rubric_version": assessment.rubric_version,
            "content_type": assessment.weights.content_type,
            "weights": {
                dimension.value: weight for dimension, weight in assessment.weights.weights.items()
            },
            "passing_policy": self._rubric.passing.model_dump(mode="json"),
            "template_id": SCORE_STAGE,
            "template_version": _template_version(execution),
        }
        check_linkage(linkage)

        return context.recorder.record_evaluation(
            execution,
            evaluator_id=SCORE_STAGE,
            evaluator_version=self.impl_version,
            rubric_version=assessment.rubric_version,
            scores={
                "overall": assessment.overall,
                "passed": assessment.passed,
                "content_type": assessment.weights.content_type,
                "dimensions": {
                    dimension.value: value for dimension, value in assessment.dimensions.items()
                },
                "failures": [
                    {
                        "detail": failure.detail,
                        "dimension": failure.dimension.value if failure.dimension else None,
                        "threshold": failure.threshold,
                        "actual": failure.actual,
                    }
                    for failure in assessment.failures
                ],
                "deductions": [
                    deduction.model_dump(mode="json") for deduction in scored.deductions
                ],
                "routed_as": category.value if category is not None else None,
                "linkage": linkage,
            },
            passed=assessment.passed,
        )


def failure_category(scored: ArticleScore) -> FailureCategory:
    """The single class a failing score is routed on.

    Routing needs one destination and a bad article usually has several problems,
    so the worst iteration-forcing deduction wins: severity first, points as the
    tie-break. Severity leads because a factual failure and a scope failure are
    different *kinds* of error — publishing something wrong is not a larger version
    of publishing something wide — and the points a scorer assigned are its opinion
    about magnitude, which is the weaker signal of the two.

    A sheet that fails on the numbers alone, with nothing iteration-forcing to
    blame, routes to substantive revision. It is the only destination that can
    address a broadly mediocre article, and guessing something narrower would send
    a person to fix a thing that is not the problem.
    """
    forcing = scored.forces_iteration
    if not forcing:
        return FailureCategory.SUBSTANTIVE_ISSUE
    worst = max(forcing, key=_rank)
    return worst.suggested_route


def _rank(deduction: ScoreDeduction) -> tuple[int, float]:
    return (_SEVERITY_RANK[deduction.severity], deduction.points)


def check_linkage(linkage: dict[str, Any]) -> None:
    """Refuse a score that cannot name everything it was computed from.

    plan/08: a score with missing linkage is not valid historical data. Checked
    before the row is written rather than when it is read — an unlinked score in
    the table will be compared against eventually, and nothing about the
    comparison will look wrong.
    """
    missing = sorted(key for key in REQUIRED_LINKAGE if not linkage.get(key))
    if missing:
        raise ScoreContractError(
            f"the score cannot name its {', '.join(missing)}; a score whose linkage is "
            "incomplete is not comparable to any other score, and storing it would let "
            "something compare it anyway"
        )


def _snapshot_id(snapshot: ArtifactSnapshot | None) -> str | None:
    return snapshot.id if snapshot is not None else None


def _template_version(execution: models.StageExecution) -> str:
    """The prompt version the scoring call actually ran under."""
    scoring = [
        invocation
        for invocation in execution.model_invocations
        if invocation.template_id == SCORE_STAGE
    ]
    return scoring[-1].template_version if scoring else ""


__all__ = [
    "REQUIRED_LINKAGE",
    "SCORE_STAGE",
    "ScoreArticle",
    "ScoreOutcome",
    "check_linkage",
    "failure_category",
]
