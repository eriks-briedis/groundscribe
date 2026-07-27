"""Turning a failing score into a route, and a stalled loop into a decision (phase 08).

Spec (plan/08 → *Routing integration*, *Evaluation provenance*, and the routing /
stagnation-escalation tests): on fail, route to the correcting stage emitting a
`DecisionRecord`; enforce rewrite limits and stagnation escalation.

Phase 05 already owns the mechanics — which category goes where, what a round
costs, when the loop has stopped paying for itself — and those are tested there.
What is tested here is the *join*: that a score produces the category phase 05
routes on, that the decision record names the score which caused it, and that the
stored evaluations can be read back as the history the stagnation detector needs.

That last one is where "evaluation provenance" stops being bookkeeping. The
detector compares a round against the one before it and against the version it
branched from, so a score that cannot name the version it scored is not a weaker
data point — it is one that silently drops out of the comparison, or worse, gets
compared against as though it belonged.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from groundscribe.provenance.enums import ActorType
from groundscribe.scoring.loop import (
    ESCALATION_OPTIONS,
    EscalationOption,
    escalations_for,
    route_score,
    score_history,
)
from groundscribe.scoring.scoring import SCORE_STAGE
from groundscribe.stages.base import StageRunner
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.policy import FailureCategory, LimitKind
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from pipeline_helpers import AUTHOR
from test_drafting import Drafted
from test_scoring import golden_score, score

#: Which failure class each of the golden deductions carries, by index.
ROUTES = {
    0: FailureCategory.FACTUAL_GAP,
    1: FailureCategory.SUBSTANTIVE_ISSUE,
    2: FailureCategory.MINOR_LOCAL,
    3: FailureCategory.STYLE_ISSUE,
}


def only_deduction(index: int, **overrides: Any) -> dict[str, Any]:
    """The golden sheet reduced to one deduction, so the route is unambiguous."""
    sheet = golden_score()
    deduction = sheet["deductions"][index] | {"severity": "major"} | overrides
    sheet["deductions"] = [deduction]
    return sheet


# ---------------------------------------------------------------------------
# Routing a failing score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", sorted(ROUTES))
async def test_each_failure_class_routes_to_the_stage_that_corrects_it(
    db_session: Session, snapshot_store: SnapshotStore, index: int
) -> None:
    """plan/08: on fail, route to the correcting stage.

    Which stage that is belongs to phase 05's policy and is tested there. What is
    asserted here is that the score hands it a category it can act on, for every
    class of deduction a scorer can raise.
    """
    drafted, result = await score(db_session, snapshot_store, only_deduction(index))
    assert result.value.category is ROUTES[index]

    routed = route_score(drafted.context, result.value)

    assert drafted.context.engine.state is not WorkflowState.REVISION_REQUIRED
    assert routed.route.outcome.category is ROUTES[index]
    assert drafted.context.engine.state is routed.route.outcome.target


async def test_the_routing_decision_names_the_score_that_caused_it(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A route with no score behind it cannot be argued with afterwards.

    The decision record is the artefact someone reads six weeks later asking why
    this article went back to the source stage. "factual_gap" answers that only if
    the number and the failing conditions travel with it.
    """
    drafted, result = await score(db_session, snapshot_store)
    routed = route_score(drafted.context, result.value)
    decision = routed.decision

    assert decision is not None
    assert decision.decision_type == "revision_routing"
    assert decision.decided_by_type is ActorType.POLICY
    assert decision.inputs["category"] == FailureCategory.FACTUAL_GAP.value
    assert decision.inputs["overall"] == pytest.approx(88.0)
    assert decision.inputs["evaluation_id"] == result.value.evaluation.id
    assert decision.inputs["rubric_version"] == result.value.assessment.rubric_version
    # The conditions that failed, not merely the fact that some did.
    assert any("factual_fidelity" in failure for failure in decision.inputs["failures"])


async def test_a_passing_score_is_not_routed(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Nothing to correct, and no route that would mean anything."""
    from test_scoring import passing_score

    drafted, result = await score(db_session, snapshot_store, passing_score())

    with pytest.raises(ValueError, match="passed"):
        route_score(drafted.context, result.value)


async def test_a_route_past_its_limit_stalls_and_asks_for_a_person(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: enforce rewrite limits. Phase 05 counts them; this is the join."""
    drafted, result = await score(db_session, snapshot_store, only_deduction(1))
    engine = drafted.context.engine
    for _ in range(engine.machine.policy.limits.substantive):
        engine.machine.ledger.spend(LimitKind.SUBSTANTIVE)

    routed = route_score(drafted.context, result.value)

    assert routed.route.escalated is True
    assert engine.state is WorkflowState.STALLED
    assert routed.route.reason


# ---------------------------------------------------------------------------
# The escalation options
# ---------------------------------------------------------------------------


def test_the_escalation_options_are_the_ones_the_spec_names() -> None:
    """plan/08 lists them, and a menu that quietly loses one loses a way out."""
    assert {option.value for option in EscalationOption} == {
        "approve_despite_score",
        "add_source_material",
        "narrow_thesis",
        "reopen_brief",
        "reopen_architecture",
        "lower_threshold",
        "authorise_rewrite",
        "abandon",
    }


async def test_a_stalled_run_offers_every_escalation_it_can_actually_take(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An option offered has to be one the machine will accept.

    Two of the eight change an *input* rather than move the machine — adding
    source material, lowering the threshold — and they are offered with no action
    attached rather than left out. A person looking for a way forward should see
    the whole menu, and "re-score it under a rubric that permits this" is a real
    answer to a stalled loop even though no edge represents it.
    """
    drafted, result = await score(db_session, snapshot_store, only_deduction(1))
    engine = drafted.context.engine
    for _ in range(engine.machine.policy.limits.substantive):
        engine.machine.ledger.spend(LimitKind.SUBSTANTIVE)
    route_score(drafted.context, result.value)

    offered = escalations_for(engine)

    assert {escalation.option for escalation in offered} == set(EscalationOption)
    available = set(engine.available_actions)
    for escalation in offered:
        if escalation.action is not None:
            assert escalation.action in available, escalation.option
    assert {e.option for e in offered if e.action is None} == {
        EscalationOption.ADD_SOURCE_MATERIAL,
        EscalationOption.LOWER_THRESHOLD,
    }
    assert all(escalation.detail for escalation in offered)
    assert ESCALATION_OPTIONS == tuple(EscalationOption)


# ---------------------------------------------------------------------------
# Score history, and what stagnation is computed from
# ---------------------------------------------------------------------------


async def test_the_history_is_read_back_from_the_stored_evaluations(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The stagnation detector's input is the score record, not a parallel tally.

    Phase 05 deliberately made `ScoreRound` ignorant of where its numbers came
    from. This is the function that supplies them, and reading them from the
    stored evaluations is what makes a stalled run explainable: every round the
    detector fired on can be traced back to the score that produced it.
    """
    drafted, result = await score(db_session, snapshot_store)

    history = score_history(drafted.context)

    (round_one,) = history
    assert round_one.ordinal == 0
    assert round_one.overall == pytest.approx(88.0)
    assert round_one.dimensions["scope_discipline"] == pytest.approx(78.0)
    assert round_one.blocking_issues
    assert round_one.parent_overall is None


async def test_a_score_that_cannot_name_its_version_is_left_out_of_the_history(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: a score with missing linkage is not valid historical data.

    The scoring stage refuses to write one, so the row here is hand-inserted —
    which is the case that matters. A score that arrived from an import, an older
    build or a migration is exactly the one nobody will think to check, and
    including it would let the detector compare against a number with no article
    behind it.
    """
    drafted, result = await score(db_session, snapshot_store)
    execution = result.execution
    assert execution is not None
    drafted.context.recorder.record_evaluation(
        execution,
        evaluator_id=SCORE_STAGE,
        evaluator_version="0.9",
        rubric_version="1",
        scores={"overall": 40.0, "dimensions": {}},
        passed=False,
    )

    history = score_history(drafted.context)

    assert len(history) == 1
    assert history[0].overall == pytest.approx(88.0)


async def test_a_rewrite_is_compared_against_the_version_it_branched_from(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/05's `parent_overall`: a round can beat the last attempt and still not
    beat the version it forked from, and only the lineage says which."""
    drafted, first = await score(db_session, snapshot_store)
    route_score(drafted.context, first.value)
    await rescore(drafted, db_session, snapshot_store, fidelity=94.0)

    history = score_history(drafted.context)

    assert [item.ordinal for item in history] == [0, 1]
    assert history[1].overall == pytest.approx(89.5)
    assert history[1].parent_overall == pytest.approx(first.value.assessment.overall)


async def test_a_stagnant_history_stalls_the_run_and_records_why(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: stagnation escalation requires a human decision.

    Two rounds that barely move is what plan/05's threshold calls stagnation, and
    the point of surfacing it here is that the numbers came from real scores — the
    detector is not being handed a fixture, it is being handed the run's own
    history.
    """
    drafted, first = await score(db_session, snapshot_store)
    route_score(drafted.context, first.value)
    second = await rescore(drafted, db_session, snapshot_store, fidelity=88.5)
    route_score(drafted.context, second.value)
    await rescore(drafted, db_session, snapshot_store, fidelity=89.0)

    checked = drafted.context.engine.check_stagnation(score_history(drafted.context))

    assert checked.check.findings
    assert drafted.context.engine.state is WorkflowState.STALLED
    assert checked.decision is not None
    assert checked.decision.decision_type == "stagnation"
    assert {escalation.option for escalation in escalations_for(drafted.context.engine)} == set(
        EscalationOption
    )


#: The edges from wherever a route lands back to scoring. Walked rather than
#: assigned, for the reason phase 05's helpers walk them: a loop that only closes
#: when the state is set by hand is not a loop.
BACK_TO_SCORING: dict[WorkflowState, tuple[WorkflowAction, ...]] = {
    WorkflowState.REVISION_PLAN_REQUIRED: (WorkflowAction.APPROVE_REVISION_PLAN,),
    WorkflowState.SUBSTANTIVE_REWRITING: (
        WorkflowAction.SUBMIT_REWRITE,
        WorkflowAction.ACCEPT_REVIEW,
        WorkflowAction.SUBMIT_VOICE_PASS,
    ),
    WorkflowState.SOURCE_MODEL_EXTRACTING: (
        WorkflowAction.COMPLETE_EXTRACTION,
        WorkflowAction.PROPOSE_ARCHITECTURE,
        WorkflowAction.SUBMIT_ARCHITECTURE,
        WorkflowAction.APPROVE_ARCHITECTURE,
        WorkflowAction.GENERATE_BRIEF,
        WorkflowAction.SUBMIT_BRIEF,
        WorkflowAction.APPROVE_BRIEF,
        WorkflowAction.SUBMIT_DRAFT,
        WorkflowAction.ACCEPT_REVIEW,
        WorkflowAction.SUBMIT_VOICE_PASS,
    ),
    WorkflowState.VOICE_ALIGNING: (WorkflowAction.SUBMIT_VOICE_PASS,),
}


def walk_back_to_scoring(drafted: Drafted) -> None:
    """Take the real edges from the current state until the article is scored again."""
    engine = drafted.context.engine
    while engine.state is not WorkflowState.SCORING:
        steps = BACK_TO_SCORING.get(engine.state)
        if steps is None:  # pragma: no cover - a mapping gap is a test bug
            raise AssertionError(f"no path back to scoring from {engine.state.value}")
        for action in steps:
            engine.apply(action, actor_id=AUTHOR, actor_type=ActorType.USER)


async def rescore(
    drafted: Drafted,
    db_session: Session,
    snapshot_store: SnapshotStore,
    *,
    fidelity: float,
) -> Any:
    """Route the last failure, walk back round the loop, and score a branched version."""
    from groundscribe.domain import models as domain_models
    from groundscribe.scoring.scoring import ScoreArticle
    from test_drafting import VOICE

    walk_back_to_scoring(drafted)
    parent = drafted.result.value.version
    child = domain_models.ArticleVersion(
        id=uuid.uuid4().hex,
        article_id=parent.article_id,
        ordinal=parent.ordinal + 1,
        snapshot_id=drafted.result.outputs[0].id,
        created_by_execution_id=parent.created_by_execution_id,
        parent_id=parent.id,
    )
    db_session.add(child)
    db_session.flush()

    sheet = golden_score()
    sheet["dimensions"]["factual_fidelity"] |= {"score": fidelity}
    sheet["deductions"] = [
        deduction
        for deduction in sheet["deductions"]
        if deduction["dimension"] != "factual_fidelity"
    ]
    drafted.model_client.script_response(SCORE_STAGE, sheet)
    return await StageRunner(drafted.context).run(
        ScoreArticle(
            draft=drafted.result.value.draft,
            version=child,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            brief_snapshot=drafted.briefed.brief_snapshot,
            source_model=drafted.briefed.source_model,
            source_model_snapshot=drafted.briefed.source_model_snapshot,
            voice=VOICE,
        )
    )
