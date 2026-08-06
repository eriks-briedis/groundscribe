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
    escalations_at,
    escalations_for,
    high_water_version,
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

#: What the golden sheet already scores for factual fidelity. Named so the tie
#: test says *why* the two rounds tie, rather than repeating a number that would
#: silently stop tying if the golden data moved.
GOLDEN_FIDELITY = 88.0

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
    available = set(engine.available_actions())
    for escalation in offered:
        if escalation.action is not None:
            assert escalation.action in available, escalation.option
    assert {e.option for e in offered if e.action is None} == {
        EscalationOption.ADD_SOURCE_MATERIAL,
        EscalationOption.LOWER_THRESHOLD,
    }
    assert all(escalation.detail for escalation in offered)
    assert tuple(EscalationOption) == ESCALATION_OPTIONS


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
    drafted, _ = await score(db_session, snapshot_store)

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


def test_every_escalation_that_moves_the_machine_has_somewhere_to_send_it() -> None:
    """An option a person is offered and cannot take is worse than one withheld.

    `STALLED` has permitted four user actions since phase 05 and two of them had
    a URL, so the menu — when anything had rendered it — would have shown six
    options of which four went nowhere. That was invisible while nothing stalled
    a run on purpose. Stagnation detection is wired in now, so this is the state
    a person actually lands in.

    Asserted against `ACTION_ENDPOINTS` rather than against a list of paths: the
    failure being guarded is an action added to the menu without an endpoint, and
    a test naming the endpoints would have to be edited by whoever added it.
    """
    from groundscribe.app.actions import ACTION_ENDPOINTS

    unreachable = [
        escalation.option.value
        for escalation in escalations_at(WorkflowState.STALLED)
        if escalation.action is not None and escalation.action not in ACTION_ENDPOINTS
    ]

    assert not unreachable, f"offered with no endpoint: {', '.join(unreachable)}"


def test_a_run_that_is_going_somewhere_is_offered_no_way_out() -> None:
    """The menu is gated on the run having stopped, and that is not fussiness.

    Every option costs something a person has to mean — another round, a
    rewritten brief, a reopened architecture, an abandoned run. Offering them
    beside the ordinary controls would put "give up" next to "approve the brief"
    on a run going perfectly well.
    """
    from groundscribe.app.reads import _escalations

    assert _escalations(WorkflowState.STALLED, project_id="p", article_id="a")
    for state in (
        WorkflowState.SCORING,
        WorkflowState.REVISION_REQUIRED,
        WorkflowState.BRIEF_REVIEW_REQUIRED,
        WorkflowState.HUMAN_APPROVAL_REQUIRED,
    ):
        assert _escalations(state, project_id="p", article_id="a") == [], state


def test_reopening_the_architecture_is_addressed_at_the_project() -> None:
    """A project-level decision offered from an article screen.

    Reopening reconsiders how the source is divided, and every article of the run
    is downstream of the answer — so the link has to carry a project id from a
    view built around an article. IMPROVEMENTS §6 filed this edge as one with no
    way to take it.
    """
    from groundscribe.app.reads import _escalations

    offered = {
        view.option: view
        for view in _escalations(WorkflowState.STALLED, project_id="proj", article_id="art")
    }

    reopen = offered[EscalationOption.REOPEN_ARCHITECTURE.value]
    assert reopen.link is not None
    assert reopen.link.path == "/projects/proj/architecture/reopen"
    assert reopen.link.requires_actor is True

    rewrite = offered[EscalationOption.AUTHORISE_REWRITE.value]
    assert rewrite.link is not None
    assert rewrite.link.path == "/articles/art/authorise-rewrite"

    # The two that change an input rather than move the machine keep a null link
    # and their sentence, which is the whole reason they are in the menu.
    for option in (EscalationOption.ADD_SOURCE_MATERIAL, EscalationOption.LOWER_THRESHOLD):
        assert offered[option.value].link is None
        assert offered[option.value].detail


async def test_the_best_round_is_the_one_the_run_keeps(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The loop's last round is not its best one, and the last one is what shipped.

    Measured on the run of 2026-08-06: 91.75, 92.05, 91.1, 90.55. Each round
    removed the unsupported claim it was sent back for and churned enough prose
    to earn fresh voice deductions, so the high-water mark was two rounds before
    the end — and every stage downstream reads `rehydrate.latest_version`, which
    meant a person arriving at the stalled run was handed the worst article the
    loop had produced.
    """
    drafted, first = await score(db_session, snapshot_store)
    route_score(drafted.context, first.value)
    best = await rescore(drafted, db_session, snapshot_store, fidelity=97.0)
    route_score(drafted.context, best.value)
    await rescore(drafted, db_session, snapshot_store, fidelity=89.0)

    high_water = high_water_version(drafted.context)

    assert high_water is not None
    assert high_water.overall == pytest.approx(
        max(round_.overall for round_ in score_history(drafted.context))
    )
    assert high_water.version_id == best.value.evaluation.scores["linkage"]["article_version_id"]


async def test_a_tie_goes_to_the_earlier_round(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Two rounds at the same score means the later one bought nothing.

    And it cost a review, a plan, a rewrite, a voice pass and a score to buy it.
    The earlier version is the one with fewer rewrites behind it, which is the
    one less likely to have drifted.
    """
    drafted, first = await score(db_session, snapshot_store)
    route_score(drafted.context, first.value)
    second = await rescore(drafted, db_session, snapshot_store, fidelity=GOLDEN_FIDELITY)

    high_water = high_water_version(drafted.context)
    history = score_history(drafted.context)

    assert history[0].overall == pytest.approx(history[1].overall)
    assert high_water is not None
    assert high_water.evaluation_id == first.value.evaluation.id
    assert high_water.evaluation_id != second.value.evaluation.id


#: The edges from wherever a route lands back to scoring. Walked rather than
#: assigned, for the reason phase 05's helpers walk them: a loop that only closes
#: when the state is set by hand is not a loop.
BACK_TO_SCORING: dict[WorkflowState, tuple[WorkflowAction, ...]] = {
    # Where a substantive failure lands now. The review is what a plan and a
    # rewrite both read, and until this was a destination the run arrived at
    # them without one.
    WorkflowState.SUBSTANTIVE_REVIEWING: (
        WorkflowAction.ACCEPT_REVIEW,
        WorkflowAction.SUBMIT_VOICE_PASS,
    ),
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


# ----------------------------------------------------------------------
# The scorer has to be told what the routes mean
# ----------------------------------------------------------------------


def test_the_scoring_prompt_defines_every_route_it_asks_for() -> None:
    """A route the prompt never explains is one the scorer picks by its name.

    ``suggested_route`` decides where a failing article goes, and v1 asked for it
    while defining none of the five values. Observed on a real run: an article
    asserting six things its source never contained came back ``factual_gap``,
    which sends the run upstream to re-extract a source with nothing wrong with
    it — and, because the stages after extraction follow, regenerates the
    architecture, brief and draft to correct six sentences.

    Asserted against the enum rather than a fixed list, so adding a category
    without explaining it fails here rather than on someone's article.
    """
    from groundscribe.paths import prompts_root
    from groundscribe.prompts.store import PromptStore

    rendered = PromptStore(prompts_root()).render(
        SCORE_STAGE,
        {
            "draft": "{}",
            "brief": "{}",
            "source_model": "{}",
            "voice": "{}",
            "dimensions": ["factual_fidelity"],
        },
    )

    for category in FailureCategory:
        assert category.value in rendered.rendered_prompt, (
            f"the scorer is asked to choose {category.value} and never told what it means"
        )


def test_the_scoring_prompt_says_an_overclaim_is_not_a_factual_gap() -> None:
    """The one distinction the routing turns on, pinned as text.

    ``factual_gap`` is corrected upstream and ``substantive_issue`` in the prose,
    and the policy deliberately forbids the first from reaching a rewrite. So a
    claim the source does not support has exactly one correct route, and the
    prompt has to say which — the names alone suggest the wrong one.
    """
    from groundscribe.paths import prompts_root
    from groundscribe.prompts.store import PromptStore

    rendered = PromptStore(prompts_root()).render(
        SCORE_STAGE,
        {
            "draft": "{}",
            "brief": "{}",
            "source_model": "{}",
            "voice": "{}",
            "dimensions": ["factual_fidelity"],
        },
    )
    prompt = rendered.rendered_prompt

    unsupported = prompt.index("claim the source does not support")
    assert prompt.rindex(FailureCategory.SUBSTANTIVE_ISSUE.value, 0, unsupported) > prompt.rindex(
        FailureCategory.FACTUAL_GAP.value, 0, unsupported
    ), "an unsupported claim is described under substantive_issue, not factual_gap"


# ----------------------------------------------------------------------
# A failing score is judging text no review has read
# ----------------------------------------------------------------------


def test_a_substantive_failure_is_reviewed_before_it_is_planned() -> None:
    """The version a score fails is one nothing has reviewed, so review it.

    ``align_voice`` produces an article version and takes the run straight to
    scoring — it is the only stage whose output no review sees. By the time a
    score fails, the newest review therefore describes the text as it stood
    before the voice pass reworded it.

    Both of the other destinations read the *current* version's review:
    ``_plan_revision`` and ``_rewrite`` each call ``latest_review`` on
    ``latest_version``. Observed on a real run, whose first score-driven
    substantive route failed with "article version f480c80f has not been
    reviewed" — and it would have failed for any article, because the path into
    scoring always runs through a voice pass.
    """
    from groundscribe.workflow.policy import default_workflow_policy

    rule = default_workflow_policy().rule(FailureCategory.SUBSTANTIVE_ISSUE)

    assert rule.target is WorkflowState.SUBSTANTIVE_REVIEWING
    # Kept, for a caller that knows a review of the current version exists.
    assert WorkflowState.REVISION_PLAN_REQUIRED in rule.alternatives
    assert WorkflowState.SUBSTANTIVE_REWRITING in rule.alternatives


def test_reviewing_leads_back_to_the_stages_that_need_it() -> None:
    """Routing there is only useful if the review's own exits carry the run on.

    Neither exit is a person's: a review that finds something substantive asks
    for a plan, and one that finds only polish says the substance is settled. So
    the round costs a review call and no clicks.
    """
    from groundscribe.workflow.transitions import targets_for

    assert targets_for(
        WorkflowState.SUBSTANTIVE_REVIEWING, WorkflowAction.REQUIRE_REVISION_PLAN
    ) == (WorkflowState.REVISION_PLAN_REQUIRED,)
    assert targets_for(WorkflowState.SUBSTANTIVE_REVIEWING, WorkflowAction.ACCEPT_REVIEW) == (
        WorkflowState.VOICE_ALIGNING,
    )


def test_the_round_is_bounded_like_every_other_substantive_one() -> None:
    """Review, plan and rewrite spend the same budget, so the loop cannot run away.

    A review that finds only polish sends the run to voice and back to the same
    failing score, which would route again. Three rounds, and then a person has
    to authorise the next one.
    """
    from groundscribe.workflow.policy import LimitKind, default_workflow_policy

    policy = default_workflow_policy()
    assert policy.rule(FailureCategory.SUBSTANTIVE_ISSUE).limit is LimitKind.SUBSTANTIVE
    assert policy.limits.substantive == 3


# ---------------------------------------------------------------------------
# Correcting a claim instead of spending a round (IMPROVEMENTS §11)
# ---------------------------------------------------------------------------


def test_a_removable_claim_is_not_a_routing_destination() -> None:
    """The correction edge is its own action, and that is the saving.

    `route_revision` charges a round against the rewrite ledger. Making this a
    ninth destination of it would put a deletion on the same budget as the three
    substantive rewrites it exists to avoid — which is the arithmetic
    IMPROVEMENTS §11 is about: one claim, one round, and the round makes the
    article worse on the way past.
    """
    from groundscribe.workflow.transitions import targets_for

    routed = targets_for(WorkflowState.REVISION_REQUIRED, WorkflowAction.ROUTE_REVISION)
    assert WorkflowState.CLAIMS_CORRECTING not in routed
    assert targets_for(WorkflowState.REVISION_REQUIRED, WorkflowAction.CORRECT_CLAIMS) == (
        WorkflowState.CLAIMS_CORRECTING,
    )


def test_a_correction_returns_to_scoring_without_a_voice_pass() -> None:
    """The re-score is the check, and it is the only check the shortcut needs.

    An article that comes back below a floor it was above had a load-bearing
    claim cut, and the round was owed after all — so the cheap path cannot hide a
    bad cut, it can only take one and be told.
    """
    from groundscribe.workflow.transitions import targets_for

    assert targets_for(WorkflowState.CLAIMS_CORRECTING, WorkflowAction.SUBMIT_CLAIM_CORRECTION) == (
        WorkflowState.SCORING,
    )


def test_the_trigger_is_computed_and_not_chosen_by_the_scorer() -> None:
    """`suggested_route` is a field a model fills in. This route is not on it.

    A route that skips the rewrite ledger is not one any scorer should be able to
    elect, so the vocabulary the scorer answers in stays closed and the decision
    is made from the rubric's verdict instead.
    """
    assert not any(
        category.value == WorkflowAction.CORRECT_CLAIMS.value for category in FailureCategory
    )


@pytest.mark.parametrize(
    ("failures", "deductions", "expected"),
    [
        ([("unsupported_claim", "u001")], [], ("u001",)),
        ([("unsupported_claim", "u001"), ("unsupported_claim", "u002")], [], ("u001", "u002")),
        # A dimension under its floor is a second problem, and cutting a sentence
        # does not address it.
        ([("unsupported_claim", "u001"), ("dimension_floor", "")], [], ()),
        ([("overall", "")], [], ()),
        # A blocking deduction is the scorer saying this is not publishable
        # regardless, which is a judgement about the article and not about a span.
        ([("unsupported_claim", "u001")], ["blocking"], ()),
        ([("unsupported_claim", "u001")], ["major", "minor"], ("u001",)),
    ],
)
def test_the_trigger_is_narrow_on_purpose(
    failures: list[tuple[str, str]], deductions: list[str], expected: tuple[str, ...]
) -> None:
    """Each rejected case is a different way of being wrong about a cut.

    "Cut the sentence" is the wrong answer whenever the claim is load-bearing,
    and the floors are the available proxy for that — a claim the argument needs
    is one whose removal shows up in `thesis_and_focus`. A proxy is what it is,
    which is why the trigger declines everything it is not sure about.
    """
    from groundscribe.provenance import models
    from groundscribe.scoring.loop import claims_to_correct

    evaluation = models.EvaluationRun(
        passed=False,
        scores={
            "failures": [{"kind": kind, "subject": subject} for kind, subject in failures],
            "deductions": [{"severity": severity} for severity in deductions],
        },
    )

    assert claims_to_correct(evaluation) == expected


def test_a_score_written_before_kinds_existed_triggers_nothing() -> None:
    """Those rows cannot say which condition failed, and guessing is the coupling.

    Matching on the prose of a message written for a person to read is how this
    breaks silently: reword the message to read better and the matcher stops
    matching, with no test between the two.
    """
    from groundscribe.provenance import models
    from groundscribe.scoring.loop import claims_to_correct

    legacy = models.EvaluationRun(
        passed=False,
        scores={
            "failures": [{"detail": "the article rests on the unsupported major claim u001"}],
            "deductions": [],
        },
    )

    assert claims_to_correct(legacy) == ()
    assert claims_to_correct(None) == ()
