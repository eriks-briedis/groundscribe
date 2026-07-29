"""The voice hierarchy and its precedence (phase 10).

Spec (plan/10 → Test-first specification): *article override beats project
profile beats global; the effective instruction set records each instruction's
source + version.*

The second half is the harder requirement and the one these tests lean on. A
resolver that merely returned the winning instructions would be easy and useless:
the question a person actually asks of a voice pass is "why did it write it that
way, and where do I go to change it?" — and that question is unanswerable from a
merged set. So the resolution keeps, for every active instruction, which profile
it came from, at which scope, in which version.
"""

from __future__ import annotations

import pytest

from groundscribe.voice.enums import InstructionStrength, VoiceCategory, VoiceScope
from groundscribe.voice.precedence import resolve_voice
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument


def instruction(
    instruction_id: str,
    text: str,
    *,
    category: VoiceCategory = VoiceCategory.TONE,
    strength: InstructionStrength = InstructionStrength.TENDENCY,
    prohibits: tuple[str, ...] = (),
) -> VoiceInstruction:
    return VoiceInstruction(
        id=instruction_id,
        category=category,
        strength=strength,
        text=text,
        prohibits=prohibits,
    )


def profile(
    scope: VoiceScope,
    *instructions: VoiceInstruction,
    name: str | None = None,
    version: str = "1",
    suppresses: tuple[str, ...] = (),
) -> VoiceProfileDocument:
    return VoiceProfileDocument(
        name=name or f"{scope.value}-profile",
        version=version,
        scope=scope,
        instructions=instructions,
        suppresses=suppresses,
    )


GLOBAL_TONE = instruction("tone", "Plain and direct.")
PROJECT_TONE = instruction("tone", "Plain, and warmer than usual — this is a tutorial.")
ARTICLE_TONE = instruction("tone", "Sharp. This one is an argument, not a walkthrough.")


# ----------------------------------------------------------------------
# Precedence
# ----------------------------------------------------------------------


def test_the_narrower_scope_wins() -> None:
    """plan/10 → article override beats project beats global.

    The narrower scope wins because it is the more deliberate act: someone who
    set a voice for one article meant it for that article, and had the wider
    profile in front of them when they did.
    """
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE),
        project_profile=profile(VoiceScope.PROJECT, PROJECT_TONE),
        article_profile=profile(VoiceScope.ARTICLE, ARTICLE_TONE),
    )

    assert [active.instruction.text for active in resolved.active] == [ARTICLE_TONE.text]


def test_a_project_profile_wins_where_no_article_override_exists() -> None:
    """Precedence is per instruction, not per profile.

    An article override that replaced the whole profile would force a person
    editing one sentence of tone to restate every rule they still wanted.
    """
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE, instruction("hedge", "No hedging.")),
        project_profile=profile(VoiceScope.PROJECT, PROJECT_TONE),
    )

    assert {active.instruction.id: active.scope for active in resolved.active} == {
        "tone": VoiceScope.PROJECT,
        "hedge": VoiceScope.GLOBAL,
    }


def test_instructions_only_a_wider_scope_declares_still_apply() -> None:
    """Inheritance, not replacement: an override adds to what it did not mention."""
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE),
        article_profile=profile(VoiceScope.ARTICLE, instruction("lists", "Avoid bullet lists.")),
    )

    assert sorted(active.instruction.id for active in resolved.active) == ["lists", "tone"]


def test_a_narrower_scope_can_drop_an_inherited_instruction() -> None:
    """An override that could only add could never relax a global rule.

    That is the case overrides most often exist for: one article where the
    author's usual prohibition is exactly the right word.
    """
    resolved = resolve_voice(
        global_profile=profile(
            VoiceScope.GLOBAL,
            instruction(
                "no-jargon",
                "Never use the internal name.",
                strength=InstructionStrength.HARD_RULE,
                prohibits=("Rivet",),
            ),
        ),
        article_profile=profile(VoiceScope.ARTICLE, suppresses=("no-jargon",)),
    )

    assert resolved.active == ()
    assert [dropped.instruction.id for dropped in resolved.suppressed] == ["no-jargon"]


def test_dropping_an_instruction_is_recorded_rather_than_silent() -> None:
    """A rule that vanished without trace is a rule nobody can ask about.

    The suppression carries the profile that asked for it, so "why is the ban I
    set not in force here?" has an answer that names the override.
    """
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE),
        article_profile=profile(
            VoiceScope.ARTICLE, name="this-one", version="4", suppresses=("tone",)
        ),
    )

    (dropped,) = resolved.suppressed
    assert dropped.scope is VoiceScope.ARTICLE
    assert dropped.profile_name == "this-one"
    assert dropped.profile_version == "4"


def test_suppressing_something_nobody_declared_is_not_an_error() -> None:
    """An override outliving the rule it relaxed is untidy, not broken.

    Failing here would turn tidying up a global profile into a change that breaks
    every article override mentioning the rule that went away.
    """
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE),
        article_profile=profile(VoiceScope.ARTICLE, suppresses=("long-gone",)),
    )

    assert [active.instruction.id for active in resolved.active] == ["tone"]
    assert resolved.suppressed == ()


# ----------------------------------------------------------------------
# Provenance of the effective set
# ----------------------------------------------------------------------


def test_every_active_instruction_names_where_it_came_from() -> None:
    """plan/10 → *records the source + version of each active instruction*.

    The requirement that makes the resolver worth having. "Why did it write it
    that way, and where do I change it?" cannot be answered from a merged set.
    """
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE, name="ada", version="7"),
        project_profile=profile(
            VoiceScope.PROJECT, instruction("lists", "Bullets are fine here."), version="2"
        ),
    )

    by_id = {active.instruction.id: active for active in resolved.active}

    assert by_id["tone"].scope is VoiceScope.GLOBAL
    assert by_id["tone"].profile_name == "ada"
    assert by_id["tone"].profile_version == "7"
    assert by_id["lists"].scope is VoiceScope.PROJECT
    assert by_id["lists"].profile_version == "2"


def test_an_overridden_instruction_keeps_the_one_it_replaced() -> None:
    """Precedence is a decision, and a decision with no record is an accident.

    Without this a reader of the trace sees the winning instruction and no sign
    that anything was chosen over anything.
    """
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE),
        article_profile=profile(VoiceScope.ARTICLE, ARTICLE_TONE),
    )

    (active,) = resolved.active
    assert active.overrides is not None
    assert active.overrides.scope is VoiceScope.GLOBAL
    assert active.overrides.instruction.text == GLOBAL_TONE.text


def test_the_resolution_is_a_profile_the_stages_can_use() -> None:
    """The voice pass takes a profile, so resolution has to produce one.

    Naming it after the scopes it came from, so a stage recording "written under
    voice X" records something a person can look up rather than a synthetic id.
    """
    resolved = resolve_voice(
        global_profile=profile(VoiceScope.GLOBAL, GLOBAL_TONE, name="ada", version="7"),
        article_profile=profile(VoiceScope.ARTICLE, ARTICLE_TONE, name="this-one", version="2"),
    )

    effective = resolved.profile

    assert effective.scope is VoiceScope.ARTICLE
    assert [i.text for i in effective.instructions] == [ARTICLE_TONE.text]
    assert "ada@7" in effective.version
    assert "this-one@2" in effective.version


def test_no_profiles_at_all_resolves_to_an_empty_voice() -> None:
    """A person who has not set a voice yet still gets to write.

    plan/10's calibration produces the first profile; requiring one before
    anything could run would make onboarding a precondition, not a first result.
    """
    resolved = resolve_voice()

    assert resolved.active == ()
    assert resolved.profile.instructions == ()


@pytest.mark.parametrize(
    ("scope", "expected"),
    [(VoiceScope.GLOBAL, 1), (VoiceScope.PROJECT, 2), (VoiceScope.ARTICLE, 3)],
)
def test_precedence_is_declared_rather_than_inferred_from_order(
    scope: VoiceScope, expected: int
) -> None:
    """Reordering the enum must not silently reorder the hierarchy."""
    assert scope.precedence == expected
