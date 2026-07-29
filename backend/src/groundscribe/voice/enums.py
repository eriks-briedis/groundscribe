"""The voice system's vocabularies (phase 10).

Three closed sets, each persisted verbatim inside profile documents and decision
records, so renaming a member rewrites the meaning of profiles already stored.
"""

from __future__ import annotations

from enum import StrEnum


class VoiceCategory(StrEnum):
    """What an instruction is about (plan/10 → VoiceProfile structure).

    Five, because that is how a person reads and edits a profile: the categories
    are the sections of the document, not an internal taxonomy. ``PROHIBITED_
    PATTERNS`` is the one that is not a facet of style but a list of things never
    to write, and it earns its own category because it is the one people add to
    most often — usually right after seeing the pattern in their own draft.
    """

    TONE = "tone"
    LANGUAGE = "language"
    STRUCTURE = "structure"
    PROHIBITED_PATTERNS = "prohibited_patterns"
    PUNCTUATION = "punctuation"


class InstructionStrength(StrEnum):
    """How firmly an instruction binds (plan/10 → Instruction strength model).

    Declared in descending order of force, and the order is meaningful: a
    resolver comparing two instructions for the same thing takes the stronger.

    - ``HARD_RULE`` — rarely violated, and *checked*. This is the only strength
      the system verifies mechanically, which is why a hard rule has to name
      something findable in the finished text.
    - ``STRONG_PREFERENCE`` — normally followed; a justified exception is
      allowed, and the model is told so rather than being left to guess whether
      it may ever deviate.
    - ``TENDENCY`` — the usual style, offered as such. Explicitly *not* a
      template: plan/10's named risk is homogenisation, and a tendency applied
      as a mandate is how that happens.
    """

    HARD_RULE = "hard_rule"
    STRONG_PREFERENCE = "strong_preference"
    TENDENCY = "tendency"

    @property
    def rank(self) -> int:
        """Descending force, for comparing two instructions about one thing."""
        return _RANKS[self]


_RANKS: dict[InstructionStrength, int] = {
    InstructionStrength.HARD_RULE: 3,
    InstructionStrength.STRONG_PREFERENCE: 2,
    InstructionStrength.TENDENCY: 1,
}


class VoiceScope(StrEnum):
    """Where a profile applies (plan/10 → voice hierarchy).

    Ordered widest to narrowest, which is also precedence order: an article
    override beats a project profile beats the author's global one. The narrower
    scope wins because it is the more deliberate act — a person who set something
    for one article meant it for that article.
    """

    GLOBAL = "global"
    PROJECT = "project"
    ARTICLE = "article"

    @property
    def precedence(self) -> int:
        """Higher wins. Named rather than compared by declaration order, so the
        rule survives someone reordering the enum."""
        return _PRECEDENCE[self]


_PRECEDENCE: dict[VoiceScope, int] = {
    VoiceScope.GLOBAL: 1,
    VoiceScope.PROJECT: 2,
    VoiceScope.ARTICLE: 3,
}


__all__ = ["InstructionStrength", "VoiceCategory", "VoiceScope"]
