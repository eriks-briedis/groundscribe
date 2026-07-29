"""Detecting a voice that has become a template (phase 10).

Spec (plan/10 → Test-first specification): *given recent articles sharing
structure, the detector flags sameness; given varied articles, it does not.*

This is the counterweight to the rest of the phase. Everything else makes the
system better at writing like one person; done well, that is also how it starts
writing the same article every time. A profile cannot detect its own
overfitting — every article obeys it perfectly — so the check looks *across*
articles at the shapes no single article gives any reason to question.

The negative case is the one that decides whether this is worth having. A
detector that fires on ordinary consistency gets ignored within a week, and an
ignored warning is worse than none: it trains the author to dismiss the whole
category. So the varied sample here is deliberately not *wildly* varied — three
pieces by one person with a real voice — and it must come back silent.
"""

from __future__ import annotations

import pytest

from groundscribe.voice.repetition import (
    RepetitionSignal,
    RepetitionThresholds,
    SampledArticle,
    detect_repetition,
)

SAME_SHAPE = (
    SampledArticle(
        id="a1",
        body=(
            "We put a read-through cache in front of the render pipeline. "
            "The p99 fell hard. It was not a performance win, but a correctness bug.\n\n"
            "## Why it was slow\n\nEvery request re-rendered the tree.\n\n"
            "## What we tried\n\nWe keyed on the path.\n\n"
            "## What I would do differently\n\nWrite the key first. "
            "And that's the whole lesson."
        ),
    ),
    SampledArticle(
        id="a2",
        body=(
            "We put a rate limiter in front of the ingest endpoint. "
            "The error budget held. It was not a capacity fix, but a contract fix.\n\n"
            "## Why it was slow\n\nEvery client retried at once.\n\n"
            "## What we tried\n\nWe bucketed by token.\n\n"
            "## What I would do differently\n\nWrite the limit first. "
            "And that's the whole lesson."
        ),
    ),
    SampledArticle(
        id="a3",
        body=(
            "We put a queue in front of the mailer. "
            "The spikes flattened out. It was not a throughput change, but a design one.\n\n"
            "## Why it was slow\n\nEvery send blocked the request.\n\n"
            "## What we tried\n\nWe batched by tenant.\n\n"
            "## What I would do differently\n\nWrite the contract first. "
            "And that's the whole lesson."
        ),
    ),
)

VARIED = (
    SampledArticle(
        id="b1",
        body=(
            "Cache keys are specifications. If the key omits an input, the cache "
            "serves a wrong answer quickly, which is worse than a slow right one.\n\n"
            "## The key that named two of three inputs\n\n"
            "Locale was an input to the render and not to the key, so readers on "
            "a non-default locale got English.\n\n"
            "## Writing it down first\n\n"
            "Listing the inputs before writing the function would have caught it."
        ),
    ),
    SampledArticle(
        id="b2",
        body=(
            "I spent a fortnight on a flaky test. The test was fine.\n\n"
            "## A clock nobody owned\n\n"
            "Two services agreed on a deadline and disagreed about what time it "
            "was. Under load the disagreement grew past the deadline.\n\n"
            "## Ownership, not accuracy\n\n"
            "The fix was deciding whose clock counted. Accuracy was never the "
            "problem; authority was."
        ),
    ),
    SampledArticle(
        id="b3",
        body=(
            "Postgres will happily let you write a migration that locks a table "
            "for four minutes at nine in the morning.\n\n"
            "## What ACCESS EXCLUSIVE actually blocks\n\n"
            "Everything, including the reads you assumed were safe. The lock "
            "queue is what turns a fast migration into an outage.\n\n"
            "## Two statements instead of one\n\n"
            "Add the column nullable, backfill in batches, then set the default."
        ),
    ),
)


def signals(articles: tuple[SampledArticle, ...]) -> set[RepetitionSignal]:
    return {finding.signal for finding in detect_repetition(articles)}


# ----------------------------------------------------------------------
# Sameness is found
# ----------------------------------------------------------------------


def test_articles_built_from_one_template_are_flagged() -> None:
    """plan/10 → *given recent articles sharing structure, the detector flags it*."""
    found = signals(SAME_SHAPE)

    assert RepetitionSignal.OPENING in found
    assert RepetitionSignal.SECTION_SEQUENCE in found
    assert RepetitionSignal.CONTRAST_PATTERN in found


def test_a_finding_names_the_articles_that_share_the_shape() -> None:
    """A warning that will not say which articles leaves the author to find them.

    Being pointed at the evidence is the whole value; "your openings repeat" is
    an opinion, and three article ids are a thing to look at.
    """
    (opening,) = [
        finding
        for finding in detect_repetition(SAME_SHAPE)
        if finding.signal is RepetitionSignal.OPENING
    ]

    assert set(opening.articles) == {"a1", "a2", "a3"}
    assert opening.count == 3
    assert "we put a" in opening.detail


def test_the_repeated_closing_line_is_its_own_finding() -> None:
    """Conclusions are listed separately by plan/10, and rightly.

    An author who notices their openings will not necessarily notice that every
    piece lands on the same note — the last sentence is the one nobody rereads.
    """
    assert RepetitionSignal.CONCLUSION in signals(SAME_SHAPE)


def test_a_recurring_rhetorical_device_is_flagged() -> None:
    """Once it is personality; four times it is a tic."""
    assert RepetitionSignal.RHETORICAL_DEVICE in signals(SAME_SHAPE)


def test_prose_with_no_variation_in_rhythm_is_flagged() -> None:
    """plan/10 → *repeated cadence*, which no single article reveals."""
    metronome = tuple(
        SampledArticle(id=f"c{index}", body=" ".join(["Short sentence here."] * 8))
        for index in range(3)
    )

    assert RepetitionSignal.CADENCE in signals(metronome)


# ----------------------------------------------------------------------
# Ordinary consistency is not
# ----------------------------------------------------------------------


def test_articles_by_one_person_with_a_voice_are_left_alone() -> None:
    """plan/10 → *given varied articles, it does not*.

    The test that decides whether this is worth shipping. These three share an
    author, a register and a subject area — which is what a voice profile is
    *for* — and differ in shape, which is what the detector is looking at.
    """
    assert detect_repetition(VARIED) == ()


def test_two_articles_are_never_enough_to_conclude_anything() -> None:
    """Two pieces opening alike is a coincidence, and a warning would say more
    than the sample supports."""
    assert detect_repetition(SAME_SHAPE[:2]) == ()


def test_the_sample_size_is_the_caller_s_to_raise() -> None:
    """An author with a long back catalogue may reasonably want more evidence."""
    thresholds = RepetitionThresholds(minimum=5)

    assert detect_repetition(SAME_SHAPE, thresholds=thresholds) == ()


def test_findings_come_back_in_a_stable_order() -> None:
    """Two runs over one sample read the same way, so a diff of warnings is a
    diff of writing rather than of iteration order."""
    first = [finding.signal for finding in detect_repetition(SAME_SHAPE)]
    second = [finding.signal for finding in detect_repetition(SAME_SHAPE)]

    assert first == second == sorted(first, key=lambda signal: signal.value)


@pytest.mark.parametrize("signal", list(RepetitionSignal))
def test_every_signal_the_spec_names_exists(signal: RepetitionSignal) -> None:
    """Six kinds of sameness, one member each, so none is quietly dropped."""
    assert signal.value in {
        "opening",
        "section_sequence",
        "contrast_pattern",
        "conclusion",
        "rhetorical_device",
        "cadence",
    }


def test_the_detector_never_proposes_a_fix() -> None:
    """plan/10 → *warn, without forcing a single template*.

    A detector wired to a corrective action would be a second style system
    fighting the first — and the author is the only one who knows whether three
    pieces open alike because the voice is stale or because they are a series.
    """
    finding = detect_repetition(SAME_SHAPE)[0]

    assert not hasattr(finding, "correction")
    assert not hasattr(finding, "apply")
