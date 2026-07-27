"""The scoring rubric: weights, the overall score, and what passes (phase 08).

Spec (plan/08 → *ScoreArticle stage*, *Passing policy (versioned)*, *Editorial-
score vs routing-result separation*, and the score-math / threshold-policy /
hard-failure-not-masked tests).

Two things are deliberately kept apart here, and the separation is the whole
design. The **overall score** is a weighted combination of seven dimensions on a
0-100 scale. The **verdict** is a set of conditions every one of which must hold.
A high average cannot buy its way past a failing dimension, because averaging is
not how the verdict is computed — plan/08's "an artefact may score 87 yet route
`fail`" is not an edge case to be handled, it is the normal consequence of
answering two different questions.

The weights are per content type and versioned, because "how much does evidence
matter?" has a different answer for an overview than for a deep dive, and a
scoring change that cannot be named is a change nobody can attribute a score
regression to.

Exercised against a throwaway rubric so the tests state the contract rather than
today's editorial judgement; two tests then hold the shipped
``config/scoring-rubric.yaml`` to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.domain.enums import ArticleDepth
from groundscribe.scoring.rubric import (
    ScoreDimension,
    ScoringRubric,
    ScoringRubricError,
    default_scoring_rubric,
)

#: A rubric whose numbers are round, so the arithmetic in the tests is checkable
#: by hand. Deep dives are weighted differently from the default on purpose.
CONFIG = """
version: "test-1"
description: Rubric used by the rubric tests.
weights:
  default:
    factual_fidelity: 0.4
    thesis_and_focus: 0.1
    structure_and_coherence: 0.1
    evidence_and_specificity: 0.1
    reader_value: 0.1
    scope_discipline: 0.1
    voice_adherence: 0.1
  deep_dive:
    factual_fidelity: 0.2
    thesis_and_focus: 0.1
    structure_and_coherence: 0.1
    evidence_and_specificity: 0.3
    reader_value: 0.1
    scope_discipline: 0.1
    voice_adherence: 0.1
passing:
  overall: 85.0
  minimums:
    factual_fidelity: 90.0
    thesis_and_focus: 80.0
    scope_discipline: 80.0
    voice_adherence: 75.0
"""

#: Every dimension at 100, so a single lowered dimension is the only variable.
PERFECT = dict.fromkeys(ScoreDimension, 100.0)


@pytest.fixture
def rubric(tmp_path: Path) -> ScoringRubric:
    path = tmp_path / "scoring-rubric.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return ScoringRubric.from_yaml(path)


def scores(**overrides: float) -> dict[ScoreDimension, float]:
    """A perfect score sheet with named dimensions lowered."""
    return PERFECT | {ScoreDimension(key): value for key, value in overrides.items()}


# ---------------------------------------------------------------------------
# Score math
# ---------------------------------------------------------------------------


def test_the_overall_score_is_the_weighted_combination(rubric: ScoringRubric) -> None:
    """plan/08 test-first spec: weighted overall computed correctly."""
    sheet = scores(factual_fidelity=50.0, reader_value=0.0)

    # 0.4*50 + 0.1*100*5 + 0.1*0 = 20 + 50 = 70.
    assert rubric.overall(sheet) == pytest.approx(70.0)
    assert rubric.overall(PERFECT) == pytest.approx(100.0)


def test_a_content_type_changes_what_the_same_article_scores(rubric: ScoringRubric) -> None:
    """plan/08: configurable weights *per content type*.

    The same score sheet under two weight sets is the point of having them. A
    thin-on-evidence article is a worse deep dive than it is an overview, and a
    rubric that could not say so would be one rubric pretending to be several.
    """
    sheet = scores(evidence_and_specificity=0.0)

    assert rubric.overall(sheet) == pytest.approx(90.0)
    assert rubric.overall(sheet, depth=ArticleDepth.DEEP_DIVE) == pytest.approx(70.0)


def test_an_unweighted_content_type_uses_the_default_set(rubric: ScoringRubric) -> None:
    """A content type nobody has tuned still scores, and says which set it used."""
    sheet = scores(evidence_and_specificity=0.0)

    assert rubric.overall(sheet, depth=ArticleDepth.OVERVIEW) == pytest.approx(90.0)
    assert rubric.weights_for(ArticleDepth.OVERVIEW).content_type == "default"
    assert rubric.weights_for(ArticleDepth.DEEP_DIVE).content_type == "deep_dive"


def test_the_weight_set_names_itself_and_its_rubric_version(rubric: ScoringRubric) -> None:
    """plan/08: the weight set is versioned and captured.

    A score is a number produced by a judgement, and the judgement moves. Without
    the version on the score, a run that scored 84 last month and 87 today is
    indistinguishable from a rubric that got more generous.
    """
    resolved = rubric.weights_for(ArticleDepth.DEEP_DIVE)

    assert resolved.rubric_version == "test-1"
    assert resolved.content_type == "deep_dive"
    assert resolved.weight(ScoreDimension.EVIDENCE_AND_SPECIFICITY) == pytest.approx(0.3)


def test_weights_that_do_not_sum_to_one_are_refused(tmp_path: Path) -> None:
    """A weight set that sums to anything else silently rescales every score."""
    path = tmp_path / "scoring-rubric.yaml"
    path.write_text(
        CONFIG.replace("factual_fidelity: 0.4", "factual_fidelity: 0.5"), encoding="utf-8"
    )

    with pytest.raises(ScoringRubricError, match="sum"):
        ScoringRubric.from_yaml(path)


def test_a_weight_set_missing_a_dimension_is_refused(tmp_path: Path) -> None:
    """A dimension with no weight is one the rubric scores and then ignores."""
    path = tmp_path / "scoring-rubric.yaml"
    path.write_text(CONFIG.replace("    reader_value: 0.1\n", "", 1), encoding="utf-8")

    with pytest.raises(ScoringRubricError, match="reader_value"):
        ScoringRubric.from_yaml(path)


def test_a_rubric_without_a_default_weight_set_is_refused(tmp_path: Path) -> None:
    """Every content type must score, including the ones nobody tuned."""
    path = tmp_path / "scoring-rubric.yaml"
    path.write_text(CONFIG.replace("  default:", "  practitioner:", 1), encoding="utf-8")

    with pytest.raises(ScoringRubricError, match="default"):
        ScoringRubric.from_yaml(path)


def test_a_score_outside_the_scale_is_refused(rubric: ScoringRubric) -> None:
    """0-100 is the scale; a dimension at 120 would lift an overall past it."""
    with pytest.raises(ScoringRubricError, match="0 and 100"):
        rubric.overall(scores(reader_value=120.0))


# ---------------------------------------------------------------------------
# The passing policy
# ---------------------------------------------------------------------------


def test_an_article_meeting_every_condition_passes(rubric: ScoringRubric) -> None:
    """plan/08: overall ≥ 85, each named dimension above its floor, no blockers."""
    assessment = rubric.assess(scores(reader_value=60.0))

    assert assessment.overall == pytest.approx(96.0)
    assert assessment.passed is True
    assert assessment.failures == ()
    assert assessment.rubric_version == "test-1"


def test_a_single_dimension_below_its_floor_fails_a_high_overall(rubric: ScoringRubric) -> None:
    """plan/08 test-first spec: a below-threshold dimension fails even with a high overall.

    Voice adherence is weighted at a tenth, so a bad one barely moves the number
    it is judged by — which is exactly why the floor is not expressed as a weight.
    """
    assessment = rubric.assess(scores(voice_adherence=40.0))

    assert assessment.overall == pytest.approx(94.0)
    assert assessment.overall >= rubric.passing.overall
    assert assessment.passed is False
    assert [failure.dimension for failure in assessment.failures] == [
        ScoreDimension.VOICE_ADHERENCE
    ]
    assert assessment.failures[0].threshold == pytest.approx(75.0)
    assert assessment.failures[0].actual == pytest.approx(40.0)


def test_high_scores_elsewhere_cannot_mask_failing_factual_fidelity(
    rubric: ScoringRubric,
) -> None:
    """plan/08: high scores in other dimensions must not mask a critical weakness.

    Fidelity is the dimension the whole product exists to protect, so this is
    stated as its own test rather than folded into the one above: the failure it
    guards against is an article that is wrong being published because it is
    well written.
    """
    sheet = dict.fromkeys(ScoreDimension, 100.0) | {ScoreDimension.FACTUAL_FIDELITY: 89.0}
    assessment = rubric.assess(sheet)

    assert assessment.overall == pytest.approx(95.6)
    assert assessment.passed is False
    assert [failure.dimension for failure in assessment.failures] == [
        ScoreDimension.FACTUAL_FIDELITY
    ]


def test_an_overall_below_the_bar_fails_with_every_dimension_healthy(
    rubric: ScoringRubric,
) -> None:
    """No single floor breached, and still not good enough overall."""
    assessment = rubric.assess(
        scores(
            structure_and_coherence=20.0,
            evidence_and_specificity=20.0,
            reader_value=20.0,
        )
    )

    assert assessment.passed is False
    assert [failure.dimension for failure in assessment.failures] == [None]
    assert assessment.failures[0].threshold == pytest.approx(85.0)


def test_a_blocking_issue_fails_an_otherwise_perfect_article(rubric: ScoringRubric) -> None:
    """plan/08: no blocking issues, whatever the numbers say."""
    assessment = rubric.assess(PERFECT, blocking_issues=("f3: the p99 figure is unsupported",))

    assert assessment.overall == pytest.approx(100.0)
    assert assessment.passed is False
    assert "blocking" in assessment.failures[0].detail


def test_an_unsupported_major_claim_fails_a_passing_score(rubric: ScoringRubric) -> None:
    """plan/08: the editorial score and the routing result are separate answers.

    The spec's own example: 87 and a `fail`. Both are surfaced, because a caller
    shown only the verdict cannot tell a near-miss from a disaster, and one shown
    only the score cannot tell why a good-looking article was sent back.
    """
    assessment = rubric.assess(
        scores(structure_and_coherence=30.0, reader_value=40.0),
        unsupported_claims=("c7",),
    )

    assert assessment.overall == pytest.approx(87.0)
    assert assessment.overall >= rubric.passing.overall
    assert assessment.passed is False
    assert "c7" in assessment.failures[0].detail


# ---------------------------------------------------------------------------
# The shipped rubric
# ---------------------------------------------------------------------------


def test_the_shipped_weights_are_the_percentages_the_spec_names() -> None:
    """plan/08 lists them, and a rubric is exactly the sort of thing that drifts."""
    shipped = default_scoring_rubric().weights_for(None)

    assert {dimension: shipped.weight(dimension) for dimension in ScoreDimension} == {
        ScoreDimension.FACTUAL_FIDELITY: pytest.approx(0.25),
        ScoreDimension.THESIS_AND_FOCUS: pytest.approx(0.15),
        ScoreDimension.STRUCTURE_AND_COHERENCE: pytest.approx(0.15),
        ScoreDimension.EVIDENCE_AND_SPECIFICITY: pytest.approx(0.15),
        ScoreDimension.READER_VALUE: pytest.approx(0.10),
        ScoreDimension.SCOPE_DISCIPLINE: pytest.approx(0.10),
        ScoreDimension.VOICE_ADHERENCE: pytest.approx(0.10),
    }


def test_the_shipped_thresholds_are_the_ones_the_spec_names() -> None:
    """overall ≥ 85, fidelity ≥ 90, thesis ≥ 80, scope ≥ 80, voice ≥ 75."""
    passing = default_scoring_rubric().passing

    assert passing.overall == pytest.approx(85.0)
    assert passing.minimums == {
        ScoreDimension.FACTUAL_FIDELITY: pytest.approx(90.0),
        ScoreDimension.THESIS_AND_FOCUS: pytest.approx(80.0),
        ScoreDimension.SCOPE_DISCIPLINE: pytest.approx(80.0),
        ScoreDimension.VOICE_ADHERENCE: pytest.approx(75.0),
    }


def test_an_unknown_content_type_in_the_weights_is_refused(tmp_path: Path) -> None:
    """A typo would otherwise be a weight set that silently never applies.

    Which looks exactly like a rubric that does not work: the tuned numbers are
    right there in the file, and every article is scored by the default.
    """
    path = tmp_path / "scoring-rubric.yaml"
    path.write_text(CONFIG.replace("  deep_dive:", "  deepdive:", 1), encoding="utf-8")

    with pytest.raises(ScoringRubricError, match="deepdive"):
        ScoringRubric.from_yaml(path)


def test_a_missing_rubric_file_is_reported_as_a_rubric_error(tmp_path: Path) -> None:
    """The caller asked for a rubric; an OSError tells it nothing about which."""
    with pytest.raises(ScoringRubricError, match="cannot read"):
        ScoringRubric.from_yaml(tmp_path / "absent.yaml")


def test_malformed_yaml_is_reported_as_a_rubric_error(tmp_path: Path) -> None:
    """One exception type for "this rubric is unusable", whatever made it so."""
    path = tmp_path / "scoring-rubric.yaml"
    path.write_text("version: [unclosed\n", encoding="utf-8")

    with pytest.raises(ScoringRubricError, match="invalid YAML"):
        ScoringRubric.from_yaml(path)


def test_scoring_a_sheet_missing_a_dimension_is_refused(rubric: ScoringRubric) -> None:
    """A dimension defaulted to zero would read as "judged and found worthless".

    It means "not judged", and the two differ by 100 points on a dimension that
    might be weighted at a quarter of the whole score.
    """
    sheet = dict(PERFECT)
    del sheet[ScoreDimension.SCOPE_DISCIPLINE]

    with pytest.raises(ScoringRubricError, match="scope_discipline"):
        rubric.assess(sheet)
