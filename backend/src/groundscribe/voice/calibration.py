"""Proposing a first voice from what a person recognises (phase 10).

plan/10 → *generate several short variants of the same passage (differing in
depth/formality/directness/opinion/narrative); user marks what feels right/wrong;
system proposes an initial editable profile.*

The design is one swap: **recognition instead of description**. Asked how formal
their writing should be, people answer with a word that means something different
to them than to everyone else. Shown two paragraphs and asked which sounds like
them, they know immediately. Everything here exists to ask the second question.

Both ends of every dimension are offered, and that is not symmetry for its own
sake. A lone variant produces a mark nobody can interpret: "wrong" says the
author dislikes that paragraph, which is not the same as preferring the opposite
direction, and a proposal built from it would be reading a preference into a
shrug.

What comes back is a document, never a row. A first guess assembled from five
quick judgements is exactly the kind of thing that must not become permanent
without being read — which is why this module can propose and cannot save.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundscribe.provenance import models
from groundscribe.stages.base import PipelineContext
from groundscribe.stages.schemas import _Output
from groundscribe.voice.enums import InstructionStrength, VoiceCategory
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument

#: The call key and template id for the calibration prompt.
CALIBRATION_STAGE = "calibrate_voice"


class Emphasis(StrEnum):
    """Which way a variant leans on its dimension."""

    MORE = "more"
    LESS = "less"


class CalibrationDimension(StrEnum):
    """What the variants differ in (plan/10 → calibration).

    Five, and each maps to the profile category a person would look in to change
    it afterwards. A proposal scattered across the wrong sections is a proposal
    nobody edits.
    """

    DEPTH = "depth"
    FORMALITY = "formality"
    DIRECTNESS = "directness"
    OPINION = "opinion"
    NARRATIVE = "narrative"

    @property
    def category(self) -> VoiceCategory:
        return _CATEGORY[self]

    def instruction(self, emphasis: Emphasis) -> str:
        """The operational instruction this preference amounts to.

        Written out per direction rather than generated from the dimension name,
        because "leans formal" is a label and "prefer the plain word over the
        technical one where both are exact" is something a model can act on —
        which is the distinction plan/10 opens with.
        """
        return _INSTRUCTIONS[self][emphasis]


_CATEGORY: dict[CalibrationDimension, VoiceCategory] = {
    CalibrationDimension.DEPTH: VoiceCategory.STRUCTURE,
    CalibrationDimension.FORMALITY: VoiceCategory.LANGUAGE,
    CalibrationDimension.DIRECTNESS: VoiceCategory.TONE,
    CalibrationDimension.OPINION: VoiceCategory.TONE,
    CalibrationDimension.NARRATIVE: VoiceCategory.STRUCTURE,
}

_INSTRUCTIONS: dict[CalibrationDimension, dict[Emphasis, str]] = {
    CalibrationDimension.DEPTH: {
        Emphasis.MORE: (
            "Explain the mechanism, not only the result; assume the reader wants "
            "to know why it worked."
        ),
        Emphasis.LESS: (
            "State the result and move on; leave the mechanism to a link or a later section."
        ),
    },
    CalibrationDimension.FORMALITY: {
        Emphasis.MORE: (
            "Use the full form and the technical term; avoid contractions and colloquial verbs."
        ),
        Emphasis.LESS: ("Use contractions and the plain word wherever it is exactly as accurate."),
    },
    CalibrationDimension.DIRECTNESS: {
        Emphasis.MORE: "Lead with the finding. Do not build up to the point across a paragraph.",
        Emphasis.LESS: "Set the scene before the finding; let the reader arrive at it with you.",
    },
    CalibrationDimension.OPINION: {
        Emphasis.MORE: (
            "Say what you think and mark it as judgement; do not hide behind the passive."
        ),
        Emphasis.LESS: "Report what happened and let the reader draw the conclusion.",
    },
    CalibrationDimension.NARRATIVE: {
        Emphasis.MORE: "Tell it in sequence — what happened, then what broke, then what you did.",
        Emphasis.LESS: "Organise by idea rather than by chronology.",
    },
}


class Verdict(StrEnum):
    """What the author said about one variant."""

    RIGHT = "right"
    WRONG = "wrong"


@dataclass(frozen=True)
class CalibrationMark:
    """One judgement: this variant sounds like me, or it does not."""

    variant_id: str
    verdict: Verdict


class CalibrationVariant(BaseModel):
    """One short rewrite of the passage, leaning one way on one dimension."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    dimension: CalibrationDimension
    emphasis: Emphasis
    text: str = Field(min_length=1)


class CalibrationVariants(_Output):
    """Every variant offered in one calibration round.

    The pairing rule is enforced here rather than checked by the caller: a round
    missing one end of a dimension cannot produce an interpretable answer, and
    the earliest place to say so is before it is shown to anyone.
    """

    variants: tuple[CalibrationVariant, ...] = ()

    @model_validator(mode="after")
    def _dimensions_are_offered_both_ways(self) -> Self:
        seen: dict[CalibrationDimension, set[Emphasis]] = {}
        for variant in self.variants:
            seen.setdefault(variant.dimension, set()).add(variant.emphasis)
        lopsided = sorted(
            dimension.value for dimension, ends in seen.items() if len(ends) < len(Emphasis)
        )
        if lopsided:
            raise ValueError(
                f"dimension(s) {', '.join(lopsided)} were offered only one way; a lone variant "
                "produces a mark nobody can interpret — disliking a paragraph is not the same "
                "as preferring the opposite direction"
            )
        return self


@dataclass(frozen=True)
class GeneratedVariants:
    """What a calibration round produced, with the call behind it."""

    variants: tuple[CalibrationVariant, ...]
    invocations: tuple[models.ModelInvocation, ...] = ()


async def generate_variants(
    context: PipelineContext,
    execution: models.StageExecution,
    *,
    passage: str,
    dimensions: Sequence[CalibrationDimension] = tuple(CalibrationDimension),
) -> GeneratedVariants:
    """Ask for both ends of each dimension over one passage.

    Routed, rendered and recorded like any other model call. Onboarding has no
    exemption from provenance: the profile this produces will shape every article
    afterwards, so how it was arrived at is worth exactly as much as how a draft
    was.
    """
    generated = await context.generator.generate(
        execution,
        stage=CALIBRATION_STAGE,
        template_id=CALIBRATION_STAGE,
        variables={
            "passage": passage,
            "dimensions": [
                {
                    "name": dimension.value,
                    "more": dimension.instruction(Emphasis.MORE),
                    "less": dimension.instruction(Emphasis.LESS),
                }
                for dimension in dimensions
            ],
        },
        schema=CalibrationVariants,
    )
    return GeneratedVariants(variants=generated.value.variants, invocations=generated.attempts)


def propose_profile(
    variants: Sequence[CalibrationVariant],
    marks: Sequence[CalibrationMark],
    *,
    name: str,
    version: str = "1",
) -> VoiceProfileDocument:
    """Turn marks into a profile the author can edit before saving.

    A preference is recorded only where the author marked one end right and the
    other wrong. Everything else proposes nothing:

    - **Unanswered** means silence, and filling it with a default would put an
      instruction in the profile they never expressed and would not recognise.
    - **Both right** means comfortable either way, which is a real answer.
      Recording a rule anyway converts a genuine flexibility into a constraint —
      plan/10's homogenisation risk, arriving during onboarding.
    - **Both wrong** means neither paragraph sounded like them, which says
      something about the variants rather than about the author.

    The instructions are tendencies. A taste test is the weakest evidence in the
    system and should produce the weakest instructions in it.
    """
    offered = {variant.id: variant for variant in variants}
    # Marks for variants nobody offered are dropped: a stale mark from an
    # abandoned round must not shape a later proposal.
    verdicts = {mark.variant_id: mark.verdict for mark in marks if mark.variant_id in offered}

    instructions: list[VoiceInstruction] = []
    for dimension in CalibrationDimension:
        preferred = _preferred(dimension, offered, verdicts)
        if preferred is None:
            continue
        emphasis, chosen = preferred
        instructions.append(
            VoiceInstruction(
                id=f"calibrated-{dimension.value}",
                category=dimension.category,
                strength=InstructionStrength.TENDENCY,
                text=dimension.instruction(emphasis),
                rationale=f"from calibration: you marked {chosen} as sounding like you",
            )
        )

    return VoiceProfileDocument(
        name=name,
        version=version,
        description="Proposed from calibration. Edit it before saving.",
        instructions=tuple(instructions),
    )


def _preferred(
    dimension: CalibrationDimension,
    offered: dict[str, CalibrationVariant],
    verdicts: dict[str, Verdict],
) -> tuple[Emphasis, str] | None:
    """The end of ``dimension`` the author leaned toward, if they leaned at all."""
    ends = {
        variant.emphasis: variant for variant in offered.values() if variant.dimension is dimension
    }
    if len(ends) < len(Emphasis):
        return None

    liked = [
        emphasis for emphasis, variant in ends.items() if verdicts.get(variant.id) is Verdict.RIGHT
    ]
    disliked = [
        emphasis for emphasis, variant in ends.items() if verdicts.get(variant.id) is Verdict.WRONG
    ]
    if len(liked) != 1 or len(disliked) != 1:
        return None
    return liked[0], ends[liked[0]].id


__all__ = [
    "CALIBRATION_STAGE",
    "CalibrationDimension",
    "CalibrationMark",
    "CalibrationVariant",
    "CalibrationVariants",
    "Emphasis",
    "GeneratedVariants",
    "Verdict",
    "generate_variants",
    "propose_profile",
]
