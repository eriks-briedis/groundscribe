"""What may be sent, published, and exported (phase 13).

plan/13 → *Confidentiality flags*: source claims and segments carry publishable /
internal / confidential / excluded-from-model-input / excluded-from-final-output /
excluded-from-exported-traces, enforced at final validation and at export.

Six names, two axes. Keeping them apart is what makes the flags enforceable
rather than decorative:

- :class:`Confidentiality` is a **classification**. A span is exactly one of
  publishable, internal or confidential, and it is the field a person sets.
- :class:`Exclusion` is a set of **switches**, each naming one boundary the
  material must not cross. A span may carry any combination.

A classification *implies* exclusions; an explicit flag may only add to them.
Resolution is a union, never a replacement — see :class:`ConfidentialityFlags`.

The three boundaries are deliberately separate questions. Internal material is
the case that proves it: a postmortem's internal detail is often exactly what
makes the public write-up accurate, so it must reach the model and must not reach
the article. A vocabulary with one "sensitive" flag would have to choose between
publishing it and throwing it away.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Confidentiality(StrEnum):
    """How sensitive one span of source material is.

    Ordered by strength, and exactly one applies. The classification is what a
    person sets; the boundaries it implies are derived, so marking something
    confidential is a complete instruction rather than the first of four.
    """

    PUBLISHABLE = "publishable"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"


class Exclusion(StrEnum):
    """One boundary material must not cross.

    Three, because they are three separate decisions with three separate
    consequences: sending something to a provider, printing it in the article,
    and leaving it in an exported trace are not the same act and are not made
    safe by the same rule.
    """

    MODEL_INPUT = "excluded_from_model_input"
    FINAL_OUTPUT = "excluded_from_final_output"
    EXPORTED_TRACES = "excluded_from_exported_traces"


#: What each classification means on its own, with no further flags.
#:
#: Confidential implies all three: a person who marked a passage confidential has
#: said everything they intend to say about it, and a system that then asked them
#: to tick three more boxes would leak whatever they forgot. Internal implies the
#: output boundary only — that is the distinction it exists to draw.
IMPLIED_EXCLUSIONS: dict[Confidentiality, frozenset[Exclusion]] = {
    Confidentiality.PUBLISHABLE: frozenset(),
    Confidentiality.INTERNAL: frozenset({Exclusion.FINAL_OUTPUT}),
    Confidentiality.CONFIDENTIAL: frozenset(Exclusion),
}


@dataclass(frozen=True, init=False)
class ConfidentialityFlags:
    """A classification and any extra boundaries, resolved into one answer.

    A frozen value rather than a row: ingestion derives a segment's flags from
    its document's, extraction derives a claim's from the segments behind it, and
    equality is how a caller asks whether anything actually changed.

    ``excluded`` is stored as given and resolved on read. Keeping the person's
    explicit choice separate from what their classification implies means a
    reader of the record can still tell which was which — the same reason every
    other decision in this codebase records its inputs rather than its result.
    """

    classification: Confidentiality
    excluded: frozenset[Exclusion]

    def __init__(
        self,
        classification: Confidentiality | str = Confidentiality.PUBLISHABLE,
        excluded: Iterable[Exclusion | str] = (),
    ) -> None:
        # Hand-written (``init=False``) so the two columns behind these fields can
        # be handed over as they come out of the database — a classification
        # string and a JSON list — without every caller converting first.
        object.__setattr__(self, "classification", Confidentiality(classification))
        object.__setattr__(self, "excluded", frozenset(Exclusion(item) for item in excluded))

    @property
    def exclusions(self) -> frozenset[Exclusion]:
        """Every boundary this material must not cross.

        The union of what the classification implies and what was flagged
        explicitly. A union rather than a precedence rule because subtraction is
        the failure worth making impossible: "confidential, but do send it" is a
        contradiction, and resolving it belongs in the classification where one
        edit says so plainly.
        """
        return IMPLIED_EXCLUSIONS[self.classification] | self.excluded

    def excludes(self, boundary: Exclusion) -> bool:
        """Whether this material is barred from ``boundary``."""
        return boundary in self.exclusions

    @property
    def may_be_sent_to_a_provider(self) -> bool:
        """Whether this material may be included in a request to a model."""
        return not self.excludes(Exclusion.MODEL_INPUT)

    @property
    def may_be_published(self) -> bool:
        """Whether this material may appear in the finished article."""
        return not self.excludes(Exclusion.FINAL_OUTPUT)

    @property
    def may_be_exported_in_traces(self) -> bool:
        """Whether this material may survive into an exported trace."""
        return not self.excludes(Exclusion.EXPORTED_TRACES)


__all__ = [
    "IMPLIED_EXCLUSIONS",
    "Confidentiality",
    "ConfidentialityFlags",
    "Exclusion",
]
