"""Scoring one article version, and what the score is allowed to be (phase 08).

Spec (plan/08 → *ScoreArticle stage*, *Evidence-backed deductions*, *Editorial-
score vs routing-result separation*, *Evaluation provenance*, and the evidence-
backed-deduction test).

Three decisions carry this module.

**The model is not asked for the overall.** `ArticleScore` has no field for it.
The scorer judges seven dimensions and explains its deductions; the *rubric*
combines them, under a named version, with weights nobody asked a model about. A
scorer that returned its own overall would be free to disagree with the weights,
and the disagreement would be invisible — it would look like a score.

**A deduction has to explain the score it is part of.** Every material deduction
names a passage, the source or brief requirement it fails, and how it fails it. A
deduction claiming more points than its dimension actually lost is refused: the
deductions are what a person reads to understand a number, and a set that does
not add up is a worse explanation than none.

**Severity decides iteration, not the score.** An optional stylistic preference
deducts points and does not buy a rewrite (plan/08 → *optional stylistic
preferences don't force a rewrite unless the rubric marks them required*).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain.enums import IssueSeverity
from groundscribe.llm.routing import RouteOverride, default_routing_policy
from groundscribe.scoring.rubric import ScoreDimension
from groundscribe.scoring.scoring import SCORE_STAGE, ScoreArticle, ScoreOutcome
from groundscribe.stages.base import StageResult, StageRunner
from groundscribe.stages.errors import ScoreContractError
from groundscribe.stages.schemas import ArticleScore
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.policy import FailureCategory
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from pipeline_helpers import AUTHOR
from test_drafting import VOICE, Drafted, draft

#: The model a second scoring pass is sent to. Any model that is *not* the one
#: the routing config already chose for scoring — the information a repeat pass
#: carries is in two models disagreeing, not in either of their names, so this
#: follows the config rather than restating a string.
#:
#: Derived from the scoring model rather than read from another route, because
#: "some other route's model" is only reliably different while the config happens
#: to spread stages across several models. Version 10 routes every stage to one
#: local model, at which point that assumption silently stopped holding and this
#: test started asserting a model differed from itself.
SECOND_OPINION_MODEL = f"{default_routing_policy().stages[SCORE_STAGE].primary.model}-alt"


def golden_score(**overrides: Any) -> dict[str, Any]:
    """The golden score sheet, with one field varied per test.

    The golden article scores 88.0 and fails, which is the separation plan/08
    describes rather than an awkward fixture: fidelity at 88 is under its floor of
    90, scope discipline at 78 is under its floor of 80, and one deduction is
    blocking. A good average and a `fail`, from one score sheet.
    """
    return golden_json("score.json", suite="draft_to_voice") | overrides


def passing_score(**overrides: Any) -> dict[str, Any]:
    """The same sheet with the two floors cleared and the blocker resolved."""
    sheet = golden_score()
    dimensions = {key: value | {"score": 95.0} for key, value in sheet["dimensions"].items()}
    return sheet | {"dimensions": dimensions, "deductions": []} | overrides


async def score(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
) -> tuple[Drafted, StageResult[ScoreOutcome]]:
    """Draft the golden article, accept the review, align it, then score it."""
    drafted = await draft(db_session, snapshot_store)
    # The route into scoring runs through review acceptance and a voice pass; the
    # test takes the edges directly, because what is under test is the score.
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.context.engine.apply(WorkflowAction.SUBMIT_VOICE_PASS)
    drafted.model_client.script_response(
        SCORE_STAGE, payload if payload is not None else golden_score()
    )
    result = await StageRunner(drafted.context).run(
        ScoreArticle(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            brief_snapshot=drafted.briefed.brief_snapshot,
            source_model=drafted.briefed.source_model,
            source_model_snapshot=drafted.briefed.source_model_snapshot,
            voice=VOICE,
        )
    )
    return drafted, result


# ---------------------------------------------------------------------------
# The score sheet
# ---------------------------------------------------------------------------


async def test_a_version_scores_into_dimensions_and_a_weighted_overall(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08 golden test: a representative version scores at schema level."""
    _, result = await score(db_session, snapshot_store)
    assessment = result.value.assessment

    assert isinstance(result.value.score, ArticleScore)
    assert set(result.value.score.dimensions) == set(ScoreDimension)
    assert assessment.overall == pytest.approx(88.0)
    assert assessment.dimensions[ScoreDimension.FACTUAL_FIDELITY] == pytest.approx(88.0)
    assert assessment.weights.content_type == "default"
    assert assessment.rubric_version


def test_the_scorer_is_not_asked_for_the_overall() -> None:
    """The rubric owns the combination, so there is no field to disagree through."""
    assert "overall" not in ArticleScore.model_fields

    with pytest.raises(ValueError, match="overall"):
        ArticleScore.model_validate(golden_score(overall=91.0))


def test_a_score_missing_a_dimension_is_refused() -> None:
    """Seven dimensions, or the weights apply to something that was never judged."""
    sheet = golden_score()
    del sheet["dimensions"]["reader_value"]

    with pytest.raises(ValueError, match="reader_value"):
        ArticleScore.model_validate(sheet)


# ---------------------------------------------------------------------------
# Evidence-backed deductions
# ---------------------------------------------------------------------------


async def test_every_material_deduction_carries_its_evidence(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: dimension, passage, requirement, mismatch, severity, route, confidence."""
    _, result = await score(db_session, snapshot_store)
    deduction = result.value.score.deductions[0]

    assert deduction.dimension is ScoreDimension.FACTUAL_FIDELITY
    assert deduction.points == pytest.approx(12.0)
    assert deduction.severity is IssueSeverity.BLOCKING
    assert deduction.passage and deduction.requirement and deduction.mismatch
    assert deduction.recommended_correction
    assert deduction.suggested_route is FailureCategory.FACTUAL_GAP
    assert deduction.source_ref == "c1"
    assert 0.0 <= deduction.confidence <= 1.0


def test_a_deduction_explaining_nothing_is_refused() -> None:
    """A deduction with no passage and no requirement is a number with an opinion."""
    sheet = golden_score()
    sheet["deductions"][0] |= {"passage": "", "requirement": ""}

    with pytest.raises(ValueError, match="passage"):
        ArticleScore.model_validate(sheet)


def test_a_deduction_claiming_more_than_its_dimension_lost_is_refused() -> None:
    """The deductions are what explain the number; a set that overshoots explains nothing.

    Not required to account for every point — a scorer may knock two off for
    something it does not consider material — but it may not claim thirty points
    off a dimension that scored ninety.
    """
    sheet = golden_score()
    sheet["deductions"][0] |= {"points": 30.0}

    with pytest.raises(ValueError, match="factual_fidelity"):
        ArticleScore.model_validate(sheet)


async def test_an_optional_preference_does_not_force_a_rewrite(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: optional stylistic preferences don't force a rewrite."""
    sheet = passing_score()
    sheet["deductions"] = [
        golden_score()["deductions"][3] | {"points": 5.0, "rubric_required": False}
    ]

    _, result = await score(db_session, snapshot_store, sheet)

    assert result.value.score.deductions[0].severity is IssueSeverity.OPTIONAL
    assert result.value.score.forces_iteration == ()
    assert result.value.assessment.passed is True
    assert result.value.category is None


async def test_a_preference_the_rubric_marks_required_does_force_one(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: "unless the rubric marks them required" — the same finding, promoted.

    Severity is the scorer's judgement of how bad something is; `rubric_required`
    is the project's judgement of whether it is negotiable. A house style that
    genuinely blocks publication is not made blocking by asking the model to feel
    more strongly about it.
    """
    sheet = passing_score()
    sheet["deductions"] = [
        golden_score()["deductions"][3] | {"points": 5.0, "rubric_required": True}
    ]

    _, result = await score(db_session, snapshot_store, sheet)

    assert result.value.score.deductions[0].severity is IssueSeverity.OPTIONAL
    assert len(result.value.score.forces_iteration) == 1
    assert result.value.category is FailureCategory.STYLE_ISSUE


# ---------------------------------------------------------------------------
# The verdict, and where the run goes
# ---------------------------------------------------------------------------


async def test_a_failing_score_parks_the_run_for_routing(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/05's table: a failed score lands in `revision_required`, not in a stage.

    The scoring stage does not route. `revision_required` is where a person can
    approve despite the score, add source material or narrow the thesis, and a
    stage that routed on its way out would step past that pause every time.
    """
    drafted, result = await score(db_session, snapshot_store)

    assert result.value.assessment.passed is False
    assert drafted.context.engine.state is WorkflowState.REVISION_REQUIRED
    assert result.value.category is FailureCategory.FACTUAL_GAP


async def test_a_passing_score_moves_the_run_to_passed(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Every condition met: the article is done being revised."""
    drafted, result = await score(db_session, snapshot_store, passing_score())

    assert result.value.assessment.passed is True
    assert result.value.assessment.failures == ()
    assert drafted.context.engine.state is WorkflowState.PASSED
    assert result.value.category is None


async def test_the_failure_class_comes_from_the_worst_deduction(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Routing needs one category, and the article has four problems.

    The most severe iteration-forcing deduction wins, ties broken by points. A
    factual failure outranks a scope failure not because it costs more points but
    because publishing something wrong is a different kind of error from
    publishing something wide.
    """
    sheet = golden_score()
    sheet["deductions"] = [sheet["deductions"][1], sheet["deductions"][2]]

    _, result = await score(db_session, snapshot_store, sheet)

    assert result.value.category is FailureCategory.SUBSTANTIVE_ISSUE


async def test_a_failing_score_with_no_deduction_to_blame_is_still_routed(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A sheet that fails on the numbers alone still has to go somewhere.

    Substantive revision is the conservative destination: it is the only route
    that can address a broadly mediocre article, and guessing anything narrower
    would send a person to fix a thing that is not the problem.
    """
    sheet = golden_score()
    sheet["dimensions"] = {
        key: value | {"score": 70.0} for key, value in sheet["dimensions"].items()
    }
    sheet["deductions"] = []

    _, result = await score(db_session, snapshot_store, sheet)

    assert result.value.assessment.passed is False
    assert result.value.category is FailureCategory.SUBSTANTIVE_ISSUE


# ---------------------------------------------------------------------------
# Evaluation provenance
# ---------------------------------------------------------------------------


async def test_the_score_is_recorded_as_an_evaluation_run(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08 → *Evaluation provenance*: the score is a record, not a return value."""
    _, result = await score(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    (evaluation,) = execution.evaluation_runs
    assert evaluation.evaluator_id == SCORE_STAGE
    assert evaluation.evaluator_version == ScoreArticle.impl_version
    assert evaluation.rubric_version == result.value.assessment.rubric_version
    assert evaluation.passed is False

    # Both answers are stored, because both are asked afterwards.
    assert evaluation.scores["overall"] == pytest.approx(88.0)
    assert evaluation.scores["dimensions"]["scope_discipline"] == pytest.approx(78.0)
    assert evaluation.scores["failures"]
    assert evaluation.scores["content_type"] == "default"


async def test_the_score_names_every_version_it_was_computed_from(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: every score links to the exact inputs and policies behind it.

    Without this a score is a number with a date on it. The comparison phase 12
    wants to make — did the rubric change, or did the article get worse? — is only
    answerable if each score names the rubric, the weights, the prompt and the
    threshold policy it was produced under.
    """
    _, result = await score(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    linkage = result.value.evaluation.scores["linkage"]
    assert linkage["article_version_id"]
    assert linkage["brief_snapshot_id"]
    assert linkage["source_model_snapshot_id"]
    assert linkage["voice_profile"] == VOICE.name
    assert linkage["voice_profile_version"] == VOICE.version
    assert linkage["rubric_version"] == result.value.assessment.rubric_version
    assert linkage["content_type"] == "default"
    assert linkage["passing_policy"]["overall"] == pytest.approx(85.0)

    # The reviewer call itself is recoverable through the execution, as for any
    # other stage: prompt version, model, params, raw and parsed response.
    invocation = execution.model_invocations[-1]
    assert invocation.template_id == SCORE_STAGE
    assert invocation.template_version
    assert invocation.request_snapshot is not None
    assert invocation.raw_response_snapshot is not None
    assert invocation.validated_response_snapshot is not None


async def test_a_score_that_cannot_name_its_inputs_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: a score with missing linkage is not valid historical data.

    Refused at the point of writing rather than filtered at the point of reading.
    A score already in the table with no way to say what produced it will be
    compared against by something eventually, and the comparison will look fine.
    """
    drafted = await draft(db_session, snapshot_store)
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.context.engine.apply(WorkflowAction.SUBMIT_VOICE_PASS)
    drafted.model_client.script_response(SCORE_STAGE, golden_score())

    with pytest.raises(ScoreContractError, match="linkage"):
        await StageRunner(drafted.context).run(
            ScoreArticle(
                draft=drafted.result.value.draft,
                version=drafted.result.value.version,
                version_snapshot=drafted.result.outputs[0],
                brief=drafted.briefed.brief,
                source_model=drafted.briefed.source_model,
                source_model_snapshot=drafted.briefed.source_model_snapshot,
                voice=VOICE,
                # The brief is in the run; this score just cannot say *which
                # version* of it. An unnamed input is the linkage failure.
            )
        )


# ---------------------------------------------------------------------------
# Score confidence and instability
# ---------------------------------------------------------------------------


async def test_repeated_scoring_reports_every_pass_and_their_dispersion(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: repeated scoring for high-stakes reviews reports repeats + dispersion.

    The dispersion is the product, not a diagnostic. plan/08's named risk is false
    score precision, and three passes landing on 88, 79 and 84 say something a
    single 84 cannot: that this article is on a boundary the scorer itself cannot
    locate, and the number should be read with that in mind.
    """
    drafted = await draft(db_session, snapshot_store)
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.context.engine.apply(WorkflowAction.SUBMIT_VOICE_PASS)
    for fidelity in (88.0, 79.0, 84.0):
        sheet = golden_score()
        sheet["dimensions"]["factual_fidelity"] |= {"score": fidelity}
        sheet["deductions"] = [
            deduction
            for deduction in sheet["deductions"]
            if deduction["dimension"] != "factual_fidelity"
        ]
        drafted.model_client.script_response(SCORE_STAGE, sheet)

    result = await StageRunner(drafted.context).run(
        ScoreArticle(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            brief_snapshot=drafted.briefed.brief_snapshot,
            source_model=drafted.briefed.source_model,
            source_model_snapshot=drafted.briefed.source_model_snapshot,
            voice=VOICE,
            repeats=3,
        )
    )
    confidence = result.value.confidence

    # 0.25 * (88, 79, 84) + 66.0, the other six dimensions unchanged.
    assert confidence.repeat_scores == pytest.approx((88.0, 85.75, 87.0))
    assert confidence.dispersion == pytest.approx(2.25)
    assert confidence.stdev is not None and confidence.stdev > 0.0
    # The assessment is over the mean of the passes, not over whichever ran last.
    assert result.value.assessment.overall == pytest.approx(86.916667, abs=1e-4)
    assert result.value.assessment.dimensions[ScoreDimension.FACTUAL_FIDELITY] == pytest.approx(
        83.666667, abs=1e-4
    )


async def test_a_single_pass_reports_no_dispersion_rather_than_zero_confidence(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """One sample has no spread; saying its dispersion is 0.0 would claim agreement.

    A dispersion of zero across three passes means three scorers agreed. Across
    one it means nothing was compared, and reporting the same number for both
    would let "we did not check" read as "we checked and it was stable".
    """
    _, result = await score(db_session, snapshot_store)
    confidence = result.value.confidence

    assert confidence.repeat_scores == pytest.approx((88.0,))
    assert confidence.dispersion is None
    assert confidence.stdev is None


async def test_the_dispersion_is_recorded_with_the_score(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A confidence figure computed and not stored is one nobody reading the run sees."""
    drafted = await draft(db_session, snapshot_store)
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.context.engine.apply(WorkflowAction.SUBMIT_VOICE_PASS)
    for fidelity in (88.0, 80.0):
        sheet = golden_score()
        sheet["dimensions"]["factual_fidelity"] |= {"score": fidelity}
        sheet["deductions"] = [
            deduction
            for deduction in sheet["deductions"]
            if deduction["dimension"] != "factual_fidelity"
        ]
        drafted.model_client.script_response(SCORE_STAGE, sheet)

    result = await StageRunner(drafted.context).run(
        ScoreArticle(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            brief_snapshot=drafted.briefed.brief_snapshot,
            source_model=drafted.briefed.source_model,
            source_model_snapshot=drafted.briefed.source_model_snapshot,
            voice=VOICE,
            repeats=2,
        )
    )
    stored = result.value.evaluation.scores["confidence"]

    assert stored["repeats"] == 2
    assert stored["repeat_scores"] == pytest.approx([88.0, 86.0])
    assert stored["dispersion"] == pytest.approx(2.0)
    # Each pass is kept as its own artefact: an average nobody can decompose is a
    # number with no evidence behind it.
    assert len(result.outputs) == 2


async def test_repeat_passes_may_run_against_different_models(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/08: repeated *or multi-model* scoring.

    At temperature 0 with a fixed seed — which is how scoring is routed — the
    second sample of one model is the first sample again. Two models disagreeing
    is the only repeat that carries information, so the passes after the first
    take their own routes.
    """
    drafted = await draft(db_session, snapshot_store)
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.context.engine.apply(WorkflowAction.SUBMIT_VOICE_PASS)
    for _ in range(2):
        drafted.model_client.script_response(SCORE_STAGE, golden_score())

    result = await StageRunner(drafted.context).run(
        ScoreArticle(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            brief_snapshot=drafted.briefed.brief_snapshot,
            source_model=drafted.briefed.source_model,
            source_model_snapshot=drafted.briefed.source_model_snapshot,
            voice=VOICE,
            repeats=2,
            repeat_overrides=(
                RouteOverride(
                    model=SECOND_OPINION_MODEL,
                    requested_by=AUTHOR,
                    reason="a second model, because one model at temperature 0 agrees with itself",
                ),
            ),
        )
    )
    execution = result.execution
    assert execution is not None
    first, second = execution.model_invocations

    # Which strings these are is the routing config's business; that the second
    # pass ran against a *different* model is what carries the information.
    assert first.model == default_routing_policy().stages[SCORE_STAGE].primary.model
    assert second.model == SECOND_OPINION_MODEL
    assert first.model != second.model
    assert len(result.value.confidence.repeat_scores) == 2


async def test_a_requirement_the_brief_stated_fails_an_article_that_scores_well(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """What a brief clause is worth, and the whole reason to write one.

    An article can be accurate, focused, in scope and well-voiced while showing
    nothing — every dimension high and the piece still empty. Three of the seven
    dimensions have no floor, so the overall carries them, and a well-written
    abstraction scores like a well-written article.

    A requirement the brief states outright is not weighed against that. It holds
    or the article is not publishable, however well it reads — which is what
    makes "show the mechanism working once" a contract rather than advice.

    Measured on a real run: 92.85 overall, every floor cleared, and a deduction
    saying the article never showed a concrete artefact. It passed.
    """
    sheet = passing_score()
    sheet["deductions"] = [
        golden_score()["deductions"][3]
        | {
            "points": 3.0,
            "severity": "minor",
            "rubric_required": True,
            "requirement": "shows the routing mechanism working once, on a real case",
            "mismatch": "the article names routing categories and shows no routed case",
        }
    ]

    _, result = await score(db_session, snapshot_store, sheet)

    assert result.value.assessment.overall >= 85.0, "it scores well, which is the point"
    assert result.value.assessment.passed is False
    assert any(
        "required is unmet" in failure.detail for failure in result.value.assessment.failures
    )


def test_the_brief_prompt_asks_for_a_worked_example_only_where_it_belongs() -> None:
    """Conditional, or it buys padding.

    An article whose thesis is a position or a report owes the reader no worked
    example, and a brief that demanded one everywhere would be the same failure
    pointing the other way — prose added to satisfy a contract rather than a
    reader.
    """
    from groundscribe.paths import prompts_root
    from groundscribe.prompts.store import PromptStore

    rendered = (
        PromptStore(prompts_root())
        .render(
            "generate_article_brief",
            {
                "article": "{}",
                "source_model": "{}",
                "audience": "engineers",
                "platform": "blog",
                "depth": "practitioner",
                "target_length_words": 1800,
                "first_person_allowed": True,
                "voice_profile": "{}",
                "publication_constraints": [],
                "claims_requiring_qualification": [],
            },
        )
        .rendered_prompt
    )

    assert "shows that mechanism working once" in rendered
    # And says when not to, in the same breath.
    assert "Do **not** add it otherwise" in rendered
    assert "padding" in rendered
    # A criterion the source cannot supply is a contract the draft cannot meet.
    assert "reserved for other articles" in rendered


def test_the_scoring_prompt_does_not_let_a_target_become_a_requirement() -> None:
    """`rubric_required` fails an article whatever it scored, so its scope matters.

    It is for a clause the brief lists under its definition of done. A number the
    brief carries is not that: `target_length_words` is what the author asked
    for, and length is checked afterwards against a 25% tolerance because the
    target is an estimate rather than a measurement.

    Observed on a live run: a 1759-word draft against a 1800-word target was
    marked required and failed, on a brief that contained no length clause at
    all. The scorer had inferred a requirement from a bare parameter — and the
    article it stopped was the better of the two the pipeline had produced.
    """
    from groundscribe.paths import prompts_root
    from groundscribe.prompts.store import PromptStore

    rendered = (
        PromptStore(prompts_root())
        .render(
            SCORE_STAGE,
            {
                "draft": "{}",
                "brief": "{}",
                "source_model": "{}",
                "voice": "{}",
                "dimensions": ["factual_fidelity"],
            },
        )
        .rendered_prompt
    )

    assert "definition\n  of done" in rendered or "definition of done" in rendered
    assert "target_length_words" in rendered
    assert "never mark that required" in rendered
