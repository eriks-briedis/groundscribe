"""Proposing a first voice from what a person recognises (phase 10).

Spec (plan/10 → Test-first specification): *variant generation covers the
differing dimensions; user marks produce a proposed profile the user can edit
before saving.*

Nobody can write their own style guide cold. Asked "how formal should this be?"
people answer with a word that means something different to them than to anyone
else; shown two paragraphs and asked which one sounds like them, they know
immediately. Calibration is that swap — recognition instead of description — and
everything here follows from it.

Two things the tests hold firmly. The proposal is a **proposal**: it comes back
as a document to edit, not a saved profile, because a first guess derived from
five quick judgements is exactly the kind of thing that should not become
permanent without being read. And it proposes **tendencies**, because a taste
test is the weakest evidence in the system and should produce the weakest
instructions in it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.voice.calibration import (
    CALIBRATION_STAGE,
    CalibrationDimension,
    CalibrationMark,
    CalibrationVariants,
    Emphasis,
    Verdict,
    generate_variants,
    propose_profile,
)
from groundscribe.voice.enums import InstructionStrength, VoiceCategory
from stage_helpers import scripted_context

PASSAGE = (
    "We put a read-through cache in front of the render pipeline in March, and "
    "p99 on the article pages fell from 810ms to 120ms."
)


def variant(dimension: str, emphasis: str, text: str) -> dict[str, Any]:
    return {
        "id": f"{dimension}-{emphasis}",
        "dimension": dimension,
        "emphasis": emphasis,
        "text": text,
    }


#: Two variants per dimension, which is what makes a mark informative: "this one
#: is right" says nothing without the other one to have preferred it over.
SCRIPTED: dict[str, Any] = {
    "schema_version": 1,
    "variants": [
        variant("depth", "more", "The render step is deterministic for a given input, which is "),
        variant("depth", "less", "We added a cache and it got faster."),
        variant("formality", "more", "A read-through cache was introduced in March."),
        variant("formality", "less", "We stuck a cache in front of it in March."),
        variant("directness", "more", "The cache key was wrong. That is the whole article."),
        variant("directness", "less", "There are a few things worth saying about cache keys."),
        variant("opinion", "more", "Treating invalidation as an afterthought is a mistake."),
        variant("opinion", "less", "Invalidation is often handled after the fact."),
        variant("narrative", "more", "In March we shipped it, and by Thursday it was wrong."),
        variant("narrative", "less", "Read-through caching reduces render latency."),
    ],
}


async def calibrate(
    db_session: Session, snapshot_store: SnapshotStore, payload: dict[str, Any] | None = None
) -> Any:
    """Run variant generation against the scripted model."""
    context, client = scripted_context(db_session, snapshot_store)
    client.script_response(CALIBRATION_STAGE, payload if payload is not None else SCRIPTED)
    execution = context.engine.begin_stage(CALIBRATION_STAGE, impl_version="1.0")
    return await generate_variants(context, execution, passage=PASSAGE)


def marks(**verdicts: str) -> tuple[CalibrationMark, ...]:
    """Marks in the shorthand these tests read best in: ``depth_more="right"``."""
    return tuple(
        CalibrationMark(variant_id=key.replace("_", "-"), verdict=Verdict(value))
        for key, value in verdicts.items()
    )


# ----------------------------------------------------------------------
# Variants
# ----------------------------------------------------------------------


def test_the_dimensions_are_the_ones_the_spec_names() -> None:
    """plan/10 → *differing in depth/formality/directness/opinion/narrative*."""
    assert [dimension.value for dimension in CalibrationDimension] == [
        "depth",
        "formality",
        "directness",
        "opinion",
        "narrative",
    ]


async def test_generation_offers_both_ends_of_every_dimension(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/10 → *variant generation covers the differing dimensions*.

    Both ends, not one. "Does this sound like you?" about a single paragraph is
    a question about the paragraph; the same question about a pair is a question
    about the author.
    """
    generated = await calibrate(db_session, snapshot_store)

    offered = {(item.dimension, item.emphasis) for item in generated.variants}

    assert offered == {
        (dimension, emphasis)
        for dimension in CalibrationDimension
        for emphasis in (Emphasis.MORE, Emphasis.LESS)
    }


def test_a_dimension_offered_only_one_way_is_refused() -> None:
    """A lone variant produces a mark nobody can interpret.

    Marking it "wrong" says the author dislikes that paragraph, which is not the
    same as preferring the other direction — and a proposal built on it would be
    reading a preference into a shrug.

    Asserted against the schema rather than through the generator, because the
    schema is where the rule lives: a lopsided round is an invalid response, and
    the phase-04 repair ladder then does what it does with any invalid response.
    """
    with pytest.raises(ValidationError, match="depth"):
        CalibrationVariants.model_validate(
            {"schema_version": 1, "variants": SCRIPTED["variants"][:1]}
        )


async def test_the_calibration_call_is_recorded_like_any_other(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Onboarding is a model call, and provenance does not have an exemption for it."""
    generated = await calibrate(db_session, snapshot_store)

    assert generated.invocations
    assert generated.invocations[0].template_id == CALIBRATION_STAGE


# ----------------------------------------------------------------------
# From marks to a proposal
# ----------------------------------------------------------------------


async def test_a_clear_preference_becomes_an_instruction(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/10 → *user marks produce a proposed profile*.

    One end right, the other wrong: the only shape of answer that says which
    direction the author leans.
    """
    generated = await calibrate(db_session, snapshot_store)

    proposed = propose_profile(
        generated.variants,
        marks(directness_more="right", directness_less="wrong"),
        name="ada",
    )

    (instruction,) = proposed.instructions
    assert instruction.id == "calibrated-directness"
    assert instruction.category is VoiceCategory.TONE
    # Operational, not a label: "leans direct" is a word the author would have to
    # interpret, and plan/10 opens by ruling exactly that out.
    assert instruction.text == CalibrationDimension.DIRECTNESS.instruction(Emphasis.MORE)
    assert "Lead with the finding" in instruction.text


async def test_a_proposal_is_made_of_tendencies(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A taste test is the weakest evidence here, so it makes the weakest rules.

    Proposing a hard rule from five quick judgements would let onboarding stop an
    article months later, for a reason the author could not remember agreeing to.
    """
    generated = await calibrate(db_session, snapshot_store)

    proposed = propose_profile(
        generated.variants, marks(depth_more="right", depth_less="wrong"), name="ada"
    )

    assert all(
        instruction.strength is InstructionStrength.TENDENCY
        for instruction in proposed.instructions
    )


async def test_a_dimension_the_author_did_not_decide_proposes_nothing(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Silence is not a preference.

    Filling in the unanswered dimensions with defaults would put instructions in
    the profile that the author never expressed and would not recognise.
    """
    generated = await calibrate(db_session, snapshot_store)

    proposed = propose_profile(
        generated.variants, marks(depth_more="right", depth_less="wrong"), name="ada"
    )

    assert [instruction.id for instruction in proposed.instructions] == ["calibrated-depth"]


async def test_liking_both_ends_proposes_nothing(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An author comfortable either way has told us they have no rule here.

    Recording one anyway would take a genuine flexibility and turn it into a
    constraint — the exact mechanism behind plan/10's homogenisation risk.
    """
    generated = await calibrate(db_session, snapshot_store)

    proposed = propose_profile(
        generated.variants, marks(formality_more="right", formality_less="right"), name="ada"
    )

    assert proposed.instructions == ()


async def test_the_proposal_is_not_saved_anywhere(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/10 → *a proposed profile the user can edit before saving*.

    It is a document, not a row. A first guess from five quick judgements is
    exactly the kind of thing that must not become permanent unread.
    """
    from groundscribe.voice.models import VoiceProfileVersion

    generated = await calibrate(db_session, snapshot_store)
    propose_profile(
        generated.variants, marks(opinion_more="right", opinion_less="wrong"), name="ada"
    )

    assert db_session.query(VoiceProfileVersion).count() == 0


async def test_the_proposal_says_what_it_was_inferred_from(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An author editing a proposal needs to know why each line is there."""
    generated = await calibrate(db_session, snapshot_store)

    proposed = propose_profile(
        generated.variants, marks(narrative_more="right", narrative_less="wrong"), name="ada"
    )

    (instruction,) = proposed.instructions
    assert "calibration" in instruction.rationale
    assert "narrative-more" in instruction.rationale


async def test_marks_for_variants_nobody_offered_are_ignored(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A stale mark from an abandoned round must not shape a later proposal."""
    generated = await calibrate(db_session, snapshot_store)

    proposed = propose_profile(
        generated.variants,
        marks(depth_more="right", depth_less="wrong") + marks(ghost_more="right"),
        name="ada",
    )

    assert [instruction.id for instruction in proposed.instructions] == ["calibrated-depth"]


def test_every_dimension_maps_to_a_category_a_person_would_look_in() -> None:
    """A proposal scattered across the wrong sections is a proposal nobody edits."""
    assert CalibrationDimension.DEPTH.category is VoiceCategory.STRUCTURE
    assert CalibrationDimension.FORMALITY.category is VoiceCategory.LANGUAGE
    assert CalibrationDimension.DIRECTNESS.category is VoiceCategory.TONE
    assert CalibrationDimension.OPINION.category is VoiceCategory.TONE
    assert CalibrationDimension.NARRATIVE.category is VoiceCategory.STRUCTURE
