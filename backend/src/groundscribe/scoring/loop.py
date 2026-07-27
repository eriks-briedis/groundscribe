"""The revision loop: routing a failing score, and stalling one that has stopped (phase 08).

plan/08 → *Routing integration*, *Evaluation provenance*, and the stagnation
escalation options.

Phase 05 built the machinery — the routing policy, the rewrite ledger, the six
stagnation conditions — deliberately ignorant of where its numbers come from, so
that the rules could be proved without a database. This module is the join: it
turns a :class:`~groundscribe.scoring.scoring.ScoreOutcome` into a route phase 05
can take, and the stored evaluations back into the history phase 05's detector
reads.

Two things live here rather than in phase 05 because they only make sense once
scores exist.

**A route names the score that caused it.** ``revision_routing`` records the
category, and on its own that answers "where did this go" but not "why". The
overall, the failing conditions and the evaluation id travel with it, because the
person reading the record six weeks later is asking the second question.

**Reading a score back is a filtered operation.** An evaluation whose linkage
cannot name the version it scored is excluded from the history rather than
defaulted into it. The scoring stage refuses to write one, so the rows this
catches come from imports, older builds and migrations — the ones nobody thinks to
check, and the ones a comparison against would look entirely normal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from groundscribe.domain import models as domain_models
from groundscribe.domain import schemas as domain_schemas
from groundscribe.provenance import models
from groundscribe.scoring.scoring import SCORE_STAGE, ScoreOutcome
from groundscribe.stages.base import PipelineContext
from groundscribe.workflow.engine import RecordedRoute, WorkflowEngine
from groundscribe.workflow.machine import RewriteApproval
from groundscribe.workflow.stagnation import ScoreRound
from groundscribe.workflow.states import WorkflowAction, WorkflowState


class EscalationOption(StrEnum):
    """What a person may do about a run the loop cannot finish (plan/08).

    Eight, and two of them move no edge. Adding source material and lowering the
    threshold change an *input* and re-score; they are options in exactly the
    sense the others are, and a menu built only from the transition table would
    silently drop the two that most often turn out to be the right answer.
    """

    APPROVE_DESPITE_SCORE = "approve_despite_score"
    ADD_SOURCE_MATERIAL = "add_source_material"
    NARROW_THESIS = "narrow_thesis"
    REOPEN_BRIEF = "reopen_brief"
    REOPEN_ARCHITECTURE = "reopen_architecture"
    LOWER_THRESHOLD = "lower_threshold"
    AUTHORISE_REWRITE = "authorise_rewrite"
    ABANDON = "abandon"


#: The options in the order a person should be offered them: cheapest and least
#: destructive first, abandoning last.
ESCALATION_OPTIONS = tuple(EscalationOption)


@dataclass(frozen=True)
class Escalation:
    """One way out of a stalled run, and what taking it means.

    ``action`` is ``None`` for the two that change an input instead of moving the
    machine. That is a real distinction rather than a gap: those two need
    something supplied before the run continues, and presenting them as edges
    would suggest a person could simply take them.
    """

    option: EscalationOption
    detail: str
    action: WorkflowAction | None = None


#: What each option does, and how to explain it. Detail text lives beside the
#: action because an option a person cannot tell apart from its neighbour is not
#: a choice — "narrow thesis" and "reopen brief" take the same edge for very
#: different reasons.
_ESCALATIONS: tuple[tuple[EscalationOption, WorkflowAction | None, str], ...] = (
    (
        EscalationOption.APPROVE_DESPITE_SCORE,
        WorkflowAction.OVERRIDE_AND_APPROVE,
        "publish it as it stands; the score is advice and you are overruling it",
    ),
    (
        EscalationOption.ADD_SOURCE_MATERIAL,
        None,
        "answer what the source could not, then re-extract and score again",
    ),
    (
        EscalationOption.NARROW_THESIS,
        WorkflowAction.RETURN_TO_BRIEF,
        "argue less, and argue it properly; the brief is where scope is set",
    ),
    (
        EscalationOption.REOPEN_BRIEF,
        WorkflowAction.RETURN_TO_BRIEF,
        "the contract itself is wrong, not the article written against it",
    ),
    (
        EscalationOption.REOPEN_ARCHITECTURE,
        WorkflowAction.REOPEN_ARCHITECTURE,
        "this is the wrong shape of article, or the wrong article",
    ),
    (
        EscalationOption.LOWER_THRESHOLD,
        None,
        "score it under a rubric whose bar this article can clear, and record that you did",
    ),
    (
        EscalationOption.AUTHORISE_REWRITE,
        WorkflowAction.AUTHORISE_REWRITE,
        "spend another rewrite beyond the limit, on your authority",
    ),
    (
        EscalationOption.ABANDON,
        WorkflowAction.CANCEL,
        "the material does not support a publishable article; stop here",
    ),
)


def route_score(
    context: PipelineContext,
    outcome: ScoreOutcome,
    *,
    prefer: WorkflowState | None = None,
    approval: RewriteApproval | None = None,
) -> RecordedRoute:
    """Send a failed score to the stage that can correct it.

    Separate from the scoring stage on purpose. The run parks at
    ``revision_required`` when a score fails, and that pause is where a person may
    approve despite the score or supply what is missing; a stage that routed on
    its way out would step past it every time.
    """
    if outcome.category is None:
        raise ValueError(
            "this score passed; there is nothing to correct and no route that would mean anything"
        )
    return context.engine.route(
        outcome.category,
        prefer=prefer,
        approval=approval,
        evidence={
            "overall": outcome.assessment.overall,
            "rubric_version": outcome.assessment.rubric_version,
            "evaluation_id": outcome.evaluation.id,
            "failures": [failure.detail for failure in outcome.assessment.failures],
            "dispersion": outcome.confidence.dispersion,
        },
    )


def escalations_for(engine: WorkflowEngine) -> tuple[Escalation, ...]:
    """Every way out of the run's current state, in the order to offer them.

    Filtered against the machine, so an option shown is one that can be taken. The
    two that move no edge are always offered: they are answers to "what do I do
    with this run" that do not depend on where it is parked.
    """
    available = set(engine.available_actions())
    return tuple(
        Escalation(option=option, detail=detail, action=action)
        for option, action, detail in _ESCALATIONS
        if action is None or action in available
    )


def score_history(context: PipelineContext) -> tuple[ScoreRound, ...]:
    """The run's scores, in order, as the stagnation detector reads them.

    Built from the stored evaluations rather than a tally kept alongside them, so
    a stalled run can be explained: every round the detector fired on traces back
    to the score that produced it, and to the article version that score was of.
    """
    evaluations = [
        evaluation
        for execution in context.engine.run.stage_executions
        for evaluation in execution.evaluation_runs
        if evaluation.evaluator_id == SCORE_STAGE and _version_id(evaluation) is not None
    ]
    evaluations.sort(key=lambda evaluation: evaluation.created_at)

    overalls = {
        version_id: _overall(evaluation)
        for evaluation in evaluations
        if (version_id := _version_id(evaluation)) is not None
    }
    return tuple(
        ScoreRound(
            ordinal=ordinal,
            overall=_overall(evaluation),
            dimensions=dict(evaluation.scores.get("dimensions", {})),
            blocking_issues=_blocking_issues(evaluation),
            parent_overall=_parent_overall(context, evaluation, overalls),
        )
        for ordinal, evaluation in enumerate(evaluations)
    )


def _version_id(evaluation: models.EvaluationRun) -> str | None:
    """The article version this score was of, or ``None`` if it cannot say.

    plan/08: a score with missing linkage is not valid historical data. A round
    with no version behind it cannot be compared against its parent, cannot be
    traced to an article, and cannot be argued with — so it is not a round.
    """
    linkage = evaluation.scores.get("linkage")
    if not isinstance(linkage, Mapping):
        return None
    version_id = linkage.get("article_version_id")
    return version_id if isinstance(version_id, str) and version_id else None


def _overall(evaluation: models.EvaluationRun) -> float:
    return float(evaluation.scores.get("overall", 0.0))


def _blocking_issues(evaluation: models.EvaluationRun) -> tuple[str, ...]:
    """The blocking deductions, keyed so the same issue matches across rounds.

    Identified by dimension and passage rather than by the scorer's wording:
    plan/05's entrenchment check asks whether the *same* blocking issue survived
    two rewrites, and a scorer rephrasing its complaint would otherwise read as
    the old one being fixed and a new one appearing.
    """
    deductions = evaluation.scores.get("deductions", [])
    return tuple(
        sorted(
            {
                f"{deduction.get('dimension', '')}:{deduction.get('passage', '') or ''}"
                for deduction in deductions
                if deduction.get("severity") == "blocking"
            }
        )
    )


def _parent_overall(
    context: PipelineContext,
    evaluation: models.EvaluationRun,
    overalls: dict[str, float],
) -> float | None:
    """What the version this one branched from scored, if it was scored at all."""
    version_id = _version_id(evaluation)
    if version_id is None:
        return None
    row = context.session.get(domain_models.ArticleVersion, version_id)
    if row is None:
        return None
    # Read the lineage through the schema rather than off the row: the mixin
    # declares `parent_id` with `declared_attr`, which mypy cannot narrow on the
    # model itself. The same reason phase 07's lineage tests validate first.
    version = domain_schemas.ArticleVersion.model_validate(row)
    if version.parent_id is None:
        return None
    return overalls.get(version.parent_id)


def escalation_payload(escalations: tuple[Escalation, ...]) -> list[dict[str, Any]]:
    """The offered options in a form an intervention request can carry."""
    return [
        {
            "option": escalation.option.value,
            "detail": escalation.detail,
            "action": escalation.action.value if escalation.action is not None else None,
        }
        for escalation in escalations
    ]


__all__ = [
    "ESCALATION_OPTIONS",
    "Escalation",
    "EscalationOption",
    "escalation_payload",
    "escalations_for",
    "route_score",
    "score_history",
]
