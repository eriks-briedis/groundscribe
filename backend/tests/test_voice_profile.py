"""The voice profile and its strength model (phase 10).

Spec (plan/10 → Deliverables):

- *VoiceProfile structure: tone, language, structure, prohibited patterns,
  punctuation — each as specific operational instructions, not vague labels.*
- *Instruction strength model: hard rules (rarely violated), strong preferences
  (normally followed, justified exceptions allowed), tendencies (usual style, not
  mandatory templates).*

The distinction the tests defend is the one the spec puts first: an instruction
is *operational* or it is decoration. "Write with warmth" cannot be followed,
checked, or argued with; "cut adverbs before verbs of speech" can. The schema
cannot tell prose apart from prose, but it can insist that a hard rule says what
it prohibits — which is the subset that has to be machine-checkable, because a
hard rule the system cannot verify is a hard rule it cannot enforce.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from groundscribe.voice.enums import InstructionStrength, VoiceCategory, VoiceScope
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument


def instruction(
    instruction_id: str = "no-em-dash",
    *,
    category: VoiceCategory = VoiceCategory.PUNCTUATION,
    strength: InstructionStrength = InstructionStrength.HARD_RULE,
    text: str = "Never use an em dash; use a colon or split the sentence.",
    prohibits: tuple[str, ...] = ("—",),
) -> VoiceInstruction:
    return VoiceInstruction(
        id=instruction_id,
        category=category,
        strength=strength,
        text=text,
        prohibits=prohibits,
    )


# ----------------------------------------------------------------------
# Categories and instructions
# ----------------------------------------------------------------------


def test_the_five_categories_are_the_ones_the_spec_names() -> None:
    """A closed set, because the profile is presented and edited category by category."""
    assert {category.value for category in VoiceCategory} == {
        "tone",
        "language",
        "structure",
        "prohibited_patterns",
        "punctuation",
    }


def test_the_three_strengths_are_the_ones_the_spec_names() -> None:
    """Hard rule, strong preference, tendency — and nothing between them."""
    assert [strength.value for strength in InstructionStrength] == [
        "hard_rule",
        "strong_preference",
        "tendency",
    ]


def test_an_instruction_must_actually_say_something() -> None:
    """An empty instruction is a category with no content, not a permissive one."""
    with pytest.raises(ValidationError):
        VoiceInstruction(
            id="empty", category=VoiceCategory.TONE, strength=InstructionStrength.TENDENCY, text=""
        )


def test_a_hard_rule_must_say_what_it_prohibits() -> None:
    """plan/10 → hard rules are *enforced*, and enforcement needs something to check.

    A hard rule phrased only as prose can be put in a prompt and hoped for. This
    is the one strength where hope is not the mechanism, so the schema refuses a
    hard rule that names nothing the system can look for in the finished text.
    """
    with pytest.raises(ValidationError):
        VoiceInstruction(
            id="be-direct",
            category=VoiceCategory.TONE,
            strength=InstructionStrength.HARD_RULE,
            text="Be direct.",
        )


def test_a_preference_and_a_tendency_need_no_checkable_form() -> None:
    """The weaker strengths are judgement, and judgement is the model's job.

    Requiring a literal for them would push every soft instruction into a shape
    that invites mechanical enforcement — which is exactly the homogenisation
    plan/10 lists as this phase's risk.
    """
    preference = instruction(
        "short-sentences",
        category=VoiceCategory.LANGUAGE,
        strength=InstructionStrength.STRONG_PREFERENCE,
        text="Prefer sentences under 25 words; break longer ones at the conjunction.",
        prohibits=(),
    )
    tendency = instruction(
        "open-concrete",
        category=VoiceCategory.STRUCTURE,
        strength=InstructionStrength.TENDENCY,
        text="Usually open on a concrete incident rather than a definition.",
        prohibits=(),
    )

    assert preference.strength is InstructionStrength.STRONG_PREFERENCE
    assert tendency.prohibits == ()


def test_an_instruction_knows_whether_it_can_be_checked() -> None:
    """The stage asks this, rather than re-deriving it from the strength."""
    assert instruction().is_enforceable
    assert not instruction("soft", strength=InstructionStrength.TENDENCY, prohibits=()).is_enforceable


# ----------------------------------------------------------------------
# The profile
# ----------------------------------------------------------------------


def test_a_profile_groups_its_instructions_by_category() -> None:
    """How a person reads and edits one, so how it is presented."""
    profile = VoiceProfileDocument(
        name="ada",
        version="2",
        scope=VoiceScope.GLOBAL,
        instructions=(
            instruction(),
            instruction(
                "no-hype",
                category=VoiceCategory.PROHIBITED_PATTERNS,
                text="Never call anything a game-changer, revolutionary or seamless.",
                prohibits=("game-changer", "revolutionary", "seamless"),
            ),
        ),
    )

    assert [i.id for i in profile.of(VoiceCategory.PUNCTUATION)] == ["no-em-dash"]
    assert [i.id for i in profile.of(VoiceCategory.PROHIBITED_PATTERNS)] == ["no-hype"]
    assert profile.of(VoiceCategory.TONE) == ()


def test_two_instructions_cannot_share_an_id() -> None:
    """Ids are how a narrower profile overrides a wider one.

    Duplicates would make an override ambiguous — and silently so, since the
    resolver would simply take whichever it saw last.
    """
    with pytest.raises(ValidationError):
        VoiceProfileDocument(
            name="ada", version="1", instructions=(instruction(), instruction())
        )


def test_a_profile_lists_the_hard_rules_it_can_enforce() -> None:
    """What the voice pass checks its output against, without filtering by hand."""
    profile = VoiceProfileDocument(
        name="ada",
        version="1",
        instructions=(
            instruction(),
            instruction(
                "short", strength=InstructionStrength.STRONG_PREFERENCE, prohibits=("very",)
            ),
        ),
    )

    assert [rule.id for rule in profile.hard_rules] == ["no-em-dash"]


def test_a_profile_is_immutable() -> None:
    """plan/00 → no silent mutation. A profile is superseded, never edited.

    An article that says which profile version it was written under has said
    nothing at all if that version can change afterwards.
    """
    profile = VoiceProfileDocument(name="ada", version="1", instructions=(instruction(),))

    with pytest.raises(ValidationError):
        profile.name = "someone else"  # type: ignore[misc]


def test_an_empty_profile_is_allowed_and_says_nothing() -> None:
    """The state a new user starts in, before calibration has proposed anything.

    Not an error: a system that required a voice before it would write anything
    would make onboarding a precondition rather than a first result.
    """
    profile = VoiceProfileDocument(name="unset", version="0")

    assert profile.instructions == ()
    assert profile.hard_rules == ()
