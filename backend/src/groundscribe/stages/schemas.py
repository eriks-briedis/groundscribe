"""The structured outputs the editorial stages ask models for (phase 06).

plan/00 → *structured outputs where decisions matter*: everything a later stage
routes, scores or publishes on is a typed schema, and only article prose is free
text. These are those schemas for §2 to §6 — the source model, the gap report, the
content architecture and the article brief.

Three conventions run through all of them.

**``extra="forbid"`` everywhere.** A model that invents a field is telling us the
prompt and the schema disagree, and silently dropping the field would hide that
until someone noticed the data was never there. The repair ladder turns it into a
correction round instead.

**Rules that can be checked are checked here**, not in the stage: a
directly-supported fact with no evidence, a gap question with no reason, an
architecture with no rejected alternatives. Expressed as validators, these become
repair feedback the model can act on (phase 04) rather than a stage failure a
person has to diagnose.

**Evidence is a list of segment ids**, never quoted prose alone. A quote can be
paraphrased into something the source never said; a segment id either exists in
the ingested document or it does not, and the stage checks which.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundscribe.domain.enums import ClaimClassification


class _Output(BaseModel):
    """Base for every stage output: strict, and versioned."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1


class Evidence(BaseModel):
    """The source passages supporting one claim, with an optional verbatim quote."""

    model_config = ConfigDict(extra="forbid")

    segment_ids: tuple[str, ...] = Field(min_length=1)
    quote: str = ""


class ExtractedClaim(BaseModel):
    """One claim from the source, classified by how well it is grounded.

    ``qualification_required`` travels with the claim rather than being inferred
    later from the classification: a directly-supported fact measured on one
    machine still needs qualifying, and only the extraction that read the source
    knows that.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    classification: ClaimClassification
    evidence: tuple[Evidence, ...] = ()
    qualification_required: bool = False

    @model_validator(mode="after")
    def _facts_must_cite_something(self) -> Self:
        if self.classification is ClaimClassification.DIRECTLY_SUPPORTED_FACT and not self.evidence:
            raise ValueError(
                f"claim {self.id!r} is classified as a directly supported fact but cites no "
                "source passage; classify it as an interpretation or attach evidence"
            )
        return self


class ProductFact(BaseModel):
    """Something true of the product, as stated by the source."""

    model_config = ConfigDict(extra="forbid")

    statement: str
    segment_ids: tuple[str, ...] = ()


class DevelopmentEvent(BaseModel):
    """One step in how the work actually went, in order."""

    model_config = ConfigDict(extra="forbid")

    ordinal: int
    summary: str
    segment_ids: tuple[str, ...] = ()


class PublicationConstraint(BaseModel):
    """Something the source says may not be published, and why."""

    model_config = ConfigDict(extra="forbid")

    description: str
    reason: str = ""
    segment_ids: tuple[str, ...] = ()


class Lesson(BaseModel):
    """A lesson the author drew, tied to the claims it rests on."""

    model_config = ConfigDict(extra="forbid")

    statement: str
    claim_ids: tuple[str, ...] = ()


class PotentialArgument(BaseModel):
    """A thesis the material could support, with its supporting claims."""

    model_config = ConfigDict(extra="forbid")

    thesis: str
    claim_ids: tuple[str, ...] = ()


class SourceModel(_Output):
    """The structured source of truth extracted from a document (phase 06 §2).

    Authoritative in the sense plan/00 means it: generated prose is disposable and
    regenerable, this is not. Every later stage is checked *against* this, so a
    field that is wrong here is wrong everywhere downstream.
    """

    summary: str
    product_facts: tuple[ProductFact, ...] = ()
    development_history: tuple[DevelopmentEvent, ...] = ()
    claims: tuple[ExtractedClaim, ...] = Field(min_length=1)
    publication_constraints: tuple[PublicationConstraint, ...] = ()
    lessons: tuple[Lesson, ...] = ()
    potential_arguments: tuple[PotentialArgument, ...] = ()

    @model_validator(mode="after")
    def _claim_ids_are_unique(self) -> Self:
        ids = [claim.id for claim in self.claims]
        duplicated = sorted({claim_id for claim_id in ids if ids.count(claim_id) > 1})
        if duplicated:
            raise ValueError(
                f"claim ids must be unique; repeated: {', '.join(duplicated)} — "
                "lessons and arguments reference claims by id"
            )
        return self

    def claim(self, claim_id: str) -> ExtractedClaim | None:
        """The claim with ``claim_id``, if the model has one."""
        return next((claim for claim in self.claims if claim.id == claim_id), None)

    def cited_segment_ids(self) -> frozenset[str]:
        """Every source segment this model points at, from any of its parts."""
        cited: set[str] = set()
        for claim in self.claims:
            for item in claim.evidence:
                cited.update(item.segment_ids)
        for fact in self.product_facts:
            cited.update(fact.segment_ids)
        for event in self.development_history:
            cited.update(event.segment_ids)
        for constraint in self.publication_constraints:
            cited.update(constraint.segment_ids)
        return frozenset(cited)


__all__ = [
    "DevelopmentEvent",
    "Evidence",
    "ExtractedClaim",
    "Lesson",
    "PotentialArgument",
    "ProductFact",
    "PublicationConstraint",
    "SourceModel",
]
