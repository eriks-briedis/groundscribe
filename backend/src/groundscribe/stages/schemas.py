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

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundscribe.domain.enums import ClaimClassification, GapPriority


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


class SourceGapQuestion(BaseModel):
    """One thing the source does not say, phrased as a question for the author.

    ``why_it_matters`` is mandatory and non-blank. plan/06 requires every surfaced
    question to state why it matters, and enforcing it here means a model that
    forgets is told so through the repair ladder — where a stage-level check would
    only tell the author, who is the person the reason exists for.

    ``group`` is how the risk plan/06 names — over-questioning — is mitigated at
    the presentation layer: related questions arrive together instead of as six
    separate demands.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    why_it_matters: str
    priority: GapPriority
    addresses: tuple[str, ...] = ()
    group: str = ""

    @model_validator(mode="after")
    def _questions_justify_themselves(self) -> Self:
        if not self.why_it_matters.strip():
            raise ValueError(
                f"gap {self.id!r} must say why it matters: a question the author cannot "
                "judge the importance of is a question they will not answer"
            )
        return self


class GapReport(_Output):
    """Everything extraction could not settle from the source alone (phase 06 §3)."""

    gaps: tuple[SourceGapQuestion, ...] = ()

    def by_priority(self, priority: GapPriority) -> tuple[SourceGapQuestion, ...]:
        """The gaps at one priority, in the order the model produced them."""
        return tuple(gap for gap in self.gaps if gap.priority is priority)


class RiskLevel(StrEnum):
    """A graded risk, used where a boolean would lose the middle case.

    Thin content is the example that forces three levels: "definitely thin" and
    "definitely not" are easy, and the article that is *probably* thin is exactly
    the one an author needs to be warned about rather than blocked on.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProposedArticle(BaseModel):
    """One candidate article: a thesis, what supports it, and what it risks."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    thesis: str
    supporting_claim_ids: tuple[str, ...] = Field(min_length=1)
    evidence_summary: str = ""
    standalone: bool = True
    reader_knowledge_assumed: str = ""
    overlaps_with: tuple[str, ...] = ()
    thin_content_risk: RiskLevel = RiskLevel.LOW
    platform_fit: str = ""


class RejectedAlternative(BaseModel):
    """An architecture that was considered and dropped, and why."""

    model_config = ConfigDict(extra="forbid")

    description: str
    reason_rejected: str

    @model_validator(mode="after")
    def _rejection_is_explained(self) -> Self:
        if not self.reason_rejected.strip():
            raise ValueError(
                "an alternative must say why it was rejected: considered-and-dropped "
                "with no reason is not a record of a decision"
            )
        return self


class ArchitectureDecision(BaseModel):
    """Why this shape was chosen over the others (phase 06 §4).

    ``alternatives_considered`` is required and non-empty. Choosing means
    rejecting: a proposal that rejected nothing did not choose, it reported the
    first idea it had, and the two are indistinguishable afterwards unless the
    schema insists.
    """

    model_config = ConfigDict(extra="forbid")

    selected: str
    rationale: str = ""
    alternatives_considered: tuple[RejectedAlternative, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    uncertainties: tuple[str, ...] = ()


class SeriesConsiderations(BaseModel):
    """How several articles relate, if they do."""

    model_config = ConfigDict(extra="forbid")

    is_series: bool = False
    reading_order: tuple[str, ...] = ()
    shared_material: tuple[str, ...] = ()
    sequencing_rationale: str = ""


class ArchitectureProposal(_Output):
    """The proposed shape of the article or series (phase 06 §4)."""

    articles: tuple[ProposedArticle, ...] = Field(min_length=1)
    competing_theses: tuple[str, ...] = ()
    series: SeriesConsiderations = SeriesConsiderations()
    decision: ArchitectureDecision

    @model_validator(mode="after")
    def _the_decision_is_actionable(self) -> Self:
        ids = [article.id for article in self.articles]
        duplicated = sorted({key for key in ids if ids.count(key) > 1})
        if duplicated:
            raise ValueError(f"article ids must be unique; repeated: {', '.join(duplicated)}")
        if self.decision.selected not in ids:
            raise ValueError(
                f"the decision selected {self.decision.selected!r}, which is not one of the "
                f"proposed articles ({', '.join(ids)}); a decision naming nothing that exists "
                "cannot be acted on"
            )
        if self.series.is_series and set(self.series.reading_order) != set(ids):
            raise ValueError(
                "a series must give a reading order covering every article it contains; "
                f"ordered: {', '.join(self.series.reading_order) or 'none'}"
            )
        return self

    def article(self, article_id: str) -> ProposedArticle | None:
        """The proposed article with ``article_id``, if there is one."""
        return next((article for article in self.articles if article.id == article_id), None)

    def cited_claim_ids(self) -> frozenset[str]:
        """Every source claim the proposal argues from."""
        return frozenset(
            claim_id for article in self.articles for claim_id in article.supporting_claim_ids
        )


__all__ = [
    "ArchitectureDecision",
    "ArchitectureProposal",
    "DevelopmentEvent",
    "Evidence",
    "ExtractedClaim",
    "GapReport",
    "Lesson",
    "PotentialArgument",
    "ProductFact",
    "ProposedArticle",
    "PublicationConstraint",
    "RejectedAlternative",
    "RiskLevel",
    "SeriesConsiderations",
    "SourceGapQuestion",
    "SourceModel",
]
