"""What a personal voice is, as a document (phase 10).

plan/10 → *specific operational instructions, not vague labels*.

The schema cannot tell an operational instruction from a vague one — both are
prose — but it can enforce the one place where the difference is load-bearing: a
**hard rule must name what it prohibits**. Hard rules are the strength the system
*verifies*, and verification needs something to look for in the finished text. A
hard rule phrased only as prose can go in a prompt and be hoped for, and hope is
not a mechanism.

The weaker strengths deliberately require nothing checkable. Forcing every
instruction into a machine-checkable shape would turn a voice into a linter, and
plan/10's named risk for this phase is style homogenisation.

Profiles are frozen. An article records the profile version it was written under,
and that record says nothing at all if the version can change afterwards
(plan/00 → no silent mutation). A refinement is a new version with the old one
still on disk.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundscribe.voice.enums import InstructionStrength, VoiceCategory, VoiceScope


class VoiceInstruction(BaseModel):
    """One thing the author's prose does, or never does.

    ``id`` is stable across versions and scopes, and that is its whole purpose:
    it is how an article override replaces the project's version of the same
    instruction rather than sitting beside it. Ids are chosen by whoever writes
    the instruction — ``no-em-dash``, ``open-concrete`` — because a generated id
    could not be matched across two profiles written months apart.

    ``prohibits`` holds literal strings that must not appear in finished prose.
    Literals rather than patterns on purpose: a regular expression in a voice
    profile is a thing the author cannot read, and every rule this has been
    needed for so far — an em dash, a banned product name, a hype word — is a
    literal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    category: VoiceCategory
    strength: InstructionStrength
    text: str = Field(min_length=1)
    prohibits: tuple[str, ...] = ()
    #: Why the instruction exists. Shown when the voice pass reports having
    #: applied it, and when a person is asked whether to keep it — an inferred
    #: rule nobody can remember the reason for is a rule nobody can judge.
    rationale: str = ""

    @model_validator(mode="after")
    def _hard_rules_are_checkable(self) -> Self:
        if self.strength is InstructionStrength.HARD_RULE and not self.prohibits:
            raise ValueError(
                f"hard rule {self.id!r} names nothing it prohibits; a hard rule is enforced "
                "against the finished prose, and one with nothing to check for is a strong "
                "preference wearing a stronger name"
            )
        return self

    @property
    def is_enforceable(self) -> bool:
        """Whether the finished prose can be checked against this instruction."""
        return bool(self.prohibits)


class VoiceProfileDocument(BaseModel):
    """A named, versioned set of instructions at one scope.

    Replaces the placeholder phase 07 shipped with. The two fields it kept —
    ``name`` and ``version`` — are the ones already written into article records,
    so an article drafted before this phase still names something meaningful.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    name: str = "default"
    version: str = "1"
    scope: VoiceScope = VoiceScope.GLOBAL
    description: str = ""
    instructions: tuple[VoiceInstruction, ...] = ()
    #: Instruction ids inherited from a wider scope that this profile drops.
    #: An override that could only add would be unable to relax a global rule
    #: for one article, which is the case overrides most often exist for.
    suppresses: tuple[str, ...] = ()
    first_person: bool = True

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        seen = [instruction.id for instruction in self.instructions]
        duplicated = sorted({name for name in seen if seen.count(name) > 1})
        if duplicated:
            raise ValueError(
                f"instruction id(s) {', '.join(duplicated)} appear twice in {self.name!r}; ids "
                "are how a narrower profile overrides a wider one, and a duplicate makes the "
                "override depend on iteration order"
            )
        return self

    def of(self, category: VoiceCategory) -> tuple[VoiceInstruction, ...]:
        """The instructions in one category, in the order they were written."""
        return tuple(
            instruction for instruction in self.instructions if instruction.category is category
        )

    @property
    def hard_rules(self) -> tuple[VoiceInstruction, ...]:
        """The instructions the voice pass checks its output against."""
        return tuple(
            instruction
            for instruction in self.instructions
            if instruction.strength is InstructionStrength.HARD_RULE
        )

    def with_instructions(self, *added: VoiceInstruction, version: str) -> VoiceProfileDocument:
        """A new version of this profile with ``added`` applied.

        Replacing by id rather than appending, so approving an inferred rule that
        refines an existing one produces a corrected profile instead of a
        contradictory one. Returns a new document; profiles are never edited.
        """
        replaced = {instruction.id for instruction in added}
        kept = tuple(
            instruction for instruction in self.instructions if instruction.id not in replaced
        )
        return self.model_copy(update={"instructions": kept + added, "version": version})


__all__ = ["VoiceInstruction", "VoiceProfileDocument"]
