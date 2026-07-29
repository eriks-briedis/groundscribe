"""Manual edit distance (phase 12).

plan/12 → *the difference between the pipeline's proposed final article and the
user-approved article (character-level, sentence add/remove, structural changes,
claim changes, voice corrections) as a quality signal (high score + heavy editing
→ weak rubric)*.

The measure exists because of the last clause. Everything else the system knows
about quality it learned from itself — a rubric scoring prose a model wrote,
against dimensions a model was asked about. What the author *actually changed
before publishing* is the one judgement in the pipeline that came from outside
it, and an article that scored 91 and then had a third of it rewritten by hand is
the system telling us its rubric is measuring the wrong thing.

Five measures rather than one number, because they mean different things. A
reworded sentence is a voice problem; a deleted claim is a factual one; a moved
heading is a structural one. Collapsing them would produce a figure that goes up
for every reason and therefore explains none of them.
"""

from __future__ import annotations

from groundscribe.experiments.edit_distance import (
    HEAVY_EDIT_RATIO,
    measure_manual_edit,
    rubric_signal,
)

PROPOSED = """# Warming the cache

The cache warms in 400 ms on a cold start.

## Why it matters

Requests then hit memory.
"""


def test_an_article_nobody_touched_is_zero_in_every_measure() -> None:
    """The identity case, asserted because it is the one a reader assumes.

    A distance that reported movement between a text and itself would make every
    other number here unreadable — there would be no baseline to judge "heavy"
    against.
    """
    distance = measure_manual_edit(PROPOSED, PROPOSED)

    assert distance.characters == 0
    assert distance.character_ratio == 0.0
    assert distance.sentences_added == 0
    assert distance.sentences_removed == 0
    assert distance.structural_changes == 0
    assert distance.claim_changes == 0
    assert distance.voice_corrections == 0
    assert not distance.heavy


def test_a_reworded_sentence_costs_characters_and_nothing_else() -> None:
    """Rewording is not adding, and the measures must not confuse the two.

    An author tightening a sentence has changed how the article reads; an author
    adding one has changed what it says. Counting a rewrite as a removal plus an
    addition would make the two indistinguishable in the aggregate, which is the
    aggregate an experiment is judged on.
    """
    approved = PROPOSED.replace("Requests then hit memory.", "Requests then hit memory directly.")

    distance = measure_manual_edit(PROPOSED, approved)

    assert distance.characters > 0
    assert distance.sentences_added == 0
    assert distance.sentences_removed == 0
    assert distance.structural_changes == 0


def test_a_sentence_added_and_a_sentence_deleted_are_counted_apart() -> None:
    """plan/12 → *sentence add/remove*, as two numbers.

    They answer different questions. Material the author had to supply says the
    brief was under-served; material they cut says the article over-reached, and
    a revision loop that responded to both the same way would be wrong half the
    time.
    """
    added = measure_manual_edit(
        PROPOSED, PROPOSED + "\nA warm cache serves the same request in 3 ms.\n"
    )
    removed = measure_manual_edit(PROPOSED, PROPOSED.replace("Requests then hit memory.\n", ""))

    assert (added.sentences_added, added.sentences_removed) == (1, 0)
    assert (removed.sentences_added, removed.sentences_removed) == (0, 1)


def test_re_levelling_a_heading_is_structural_even_though_the_prose_is_identical() -> None:
    """plan/12 → *structural changes*, measured over the shape and not the words.

    The case that proves the measure is its own: not one word of prose moved, so
    every other measure is nearly silent, and the author still changed how the
    article is organised.
    """
    approved = PROPOSED.replace("## Why it matters", "### Why it matters")

    distance = measure_manual_edit(PROPOSED, approved)

    assert distance.structural_changes >= 1
    assert distance.sentences_added == 0
    assert distance.sentences_removed == 0


def test_a_claim_the_author_cut_is_a_claim_change() -> None:
    """plan/12 → *claim changes*.

    Measured against the source claims rather than against the prose, because
    the question is not "did a sentence go" but "does the article still rest on
    this". An author removing the one paragraph that carried a claim has changed
    what the article asserts, whatever the word count says.
    """
    claims = ("the p99 latency fell from 900 ms to 120 ms",)
    proposed = PROPOSED + "\nThe p99 latency fell from 900 ms to 120 ms after the change.\n"

    unchanged = measure_manual_edit(proposed, proposed, claims=claims)
    cut = measure_manual_edit(proposed, PROPOSED, claims=claims)

    assert unchanged.claim_changes == 0
    assert cut.claim_changes == 1


def test_removing_a_word_the_voice_profile_prohibits_is_a_voice_correction() -> None:
    """plan/12 → *voice corrections*.

    A hand edit that deletes a term the active profile already prohibits is not
    a preference the author is expressing — it is the voice pass having failed,
    caught by the person who had to fix it. Counted per instance removed, because
    a profile the model ignored twice in one article is worse news than one it
    ignored once.
    """
    proposed = "We leverage the cache to utilise memory. We leverage it again on retry."
    approved = "We use the cache to reach memory. We use it again on retry."

    distance = measure_manual_edit(proposed, approved, prohibited=("leverage", "utilise"))

    assert distance.voice_corrections == 3


def test_a_prohibited_word_the_author_left_alone_is_not_a_correction() -> None:
    """Only what the author actually removed counts.

    Otherwise the measure would report the voice pass's failures whether or not
    anyone minded them, and the signal would stop being about the author's own
    judgement — which is the only reason it carries weight.
    """
    text = "We leverage the cache."

    distance = measure_manual_edit(text, text, prohibited=("leverage",))

    assert distance.voice_corrections == 0


def test_a_high_score_the_author_then_rewrote_is_flagged_as_a_weak_rubric() -> None:
    """plan/12 → *high score + heavy editing → weak rubric*.

    The flag is about the rubric, not the article. Nothing here says the prose
    was bad; it says the number that called it good disagreed with the person who
    published it, and that gap is what a rubric version is revised against.
    """
    approved = """# Cold starts

Starting cold, the cache takes 400 ms to fill.

## Why that matters

Until it fills, every request pays for a round trip to storage.
"""
    distance = measure_manual_edit(PROPOSED, approved)
    assert distance.character_ratio >= HEAVY_EDIT_RATIO, "the fixture has to be a heavy edit"

    signal = rubric_signal(distance, overall=91.0, threshold=85.0)

    assert signal.weak_rubric
    assert "91" in signal.detail


def test_a_high_score_the_author_barely_touched_is_not_flagged() -> None:
    """The rubric and the author agreed; there is nothing to report."""
    approved = PROPOSED.replace("Requests then hit memory.", "Requests then hit memory directly.")

    signal = rubric_signal(measure_manual_edit(PROPOSED, approved), overall=91.0, threshold=85.0)

    assert not signal.weak_rubric


def test_a_low_score_the_author_rewrote_is_not_a_rubric_failure() -> None:
    """A poor score followed by heavy editing is the rubric working.

    Flagging it would invert the signal: the loudest complaints would come from
    the cases the system got right, and the measure would be discarded as noise
    before it ever caught the case it exists for.
    """
    approved = "Nothing of the original survives this rewrite at all, not one line."

    signal = rubric_signal(measure_manual_edit(PROPOSED, approved), overall=61.0, threshold=85.0)

    assert not signal.weak_rubric
