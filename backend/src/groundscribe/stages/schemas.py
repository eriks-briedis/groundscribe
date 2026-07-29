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

from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundscribe.domain.enums import ClaimClassification, GapPriority, IssueSeverity
from groundscribe.scoring.rubric import ScoreDimension
from groundscribe.voice.schemas import VoiceProfileDocument
from groundscribe.workflow.policy import FailureCategory


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


class BriefSection(BaseModel):
    """One section of the argument, and what it is for.

    ``mandatory`` is the clause that makes a brief a contract rather than an
    outline: an optional section may be dropped by the draft without the article
    failing its own definition of done, and a mandatory one may not.
    """

    model_config = ConfigDict(extra="forbid")

    heading: str
    purpose: str
    claim_ids: tuple[str, ...] = ()
    required_examples: tuple[str, ...] = ()
    mandatory: bool = True


class DoneCriterion(BaseModel):
    """One condition the finished article must meet."""

    model_config = ConfigDict(extra="forbid")

    description: str
    mandatory: bool = True


class ArticleBriefDocument(_Output):
    """The brief-as-contract for one approved article (phase 06 §6).

    Every field plan/06 names is here and none are optional-by-omission: a brief
    missing a clause is a brief that licenses the draft to decide that clause for
    itself, which is precisely the scope drift the product exists to prevent.

    The definition of done is what makes it a contract. Phase 08's final
    validation checks the article against it, so a definition consisting entirely
    of optional criteria defines nothing at all — and is refused here rather than
    discovered as a validation that always passes.
    """

    title: str
    thesis: str
    audience: str
    reader_knowledge: str = ""
    reader_problem: str = ""
    opening_direction: str = ""
    argument_structure: tuple[BriefSection, ...] = Field(min_length=1)
    claims_requiring_qualification: tuple[str, ...] = ()
    required_conclusion: str = ""
    target_length_words: int = Field(gt=0)
    platform_constraints: tuple[str, ...] = ()
    voice_profile: str = "default"
    style_overrides: tuple[str, ...] = ()
    excluded_material: tuple[str, ...] = ()
    reserved_material: tuple[str, ...] = ()
    definition_of_done: tuple[DoneCriterion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_contract_binds_something(self) -> Self:
        if not any(criterion.mandatory for criterion in self.definition_of_done):
            raise ValueError(
                "definition_of_done needs at least one mandatory criterion: a definition "
                "of done where everything is optional defines nothing"
            )
        if not any(section.mandatory for section in self.argument_structure):
            raise ValueError(
                "argument_structure needs at least one mandatory section: an argument "
                "every part of which may be dropped is not an argument"
            )
        return self

    @property
    def mandatory_criteria(self) -> tuple[DoneCriterion, ...]:
        """The criteria the article must meet to be finished."""
        return tuple(criterion for criterion in self.definition_of_done if criterion.mandatory)

    @property
    def optional_criteria(self) -> tuple[DoneCriterion, ...]:
        """The criteria that improve the article without gating it."""
        return tuple(criterion for criterion in self.definition_of_done if not criterion.mandatory)

    def cited_claim_ids(self) -> frozenset[str]:
        """Every source claim the brief's sections rest on."""
        return frozenset(
            claim_id for section in self.argument_structure for claim_id in section.claim_ids
        )


# The voice profile used to be a placeholder here — a name, a tone and a list of
# phrases to avoid, which was the minimum a drafting prompt needed before there
# was a voice system. Phase 10 built the real one in ``groundscribe.voice``:
# categorised instructions carrying strengths, resolved across three scopes. It
# is imported above so every consumer of the old name gets the new document.


class UnresolvedMarker(BaseModel):
    """A fact the draft could not settle, marked where the reader can see it.

    ``blocking`` separates "the article is worse without this" from "the article is
    *wrong* without this". Only the second is worth stopping a person for.
    """

    model_config = ConfigDict(extra="forbid")

    marker: str
    question: str
    blocking: bool = False
    claim_ids: tuple[str, ...] = ()


class OmittedMaterial(BaseModel):
    """Something the draft deliberately left out, and why."""

    model_config = ConfigDict(extra="forbid")

    description: str
    reason: str
    claim_ids: tuple[str, ...] = ()


class ArticleDraft(_Output):
    """One version of the article: prose, plus what the drafter did to produce it.

    ``body`` is the only free-text field in the system (plan/00 → structured
    outputs where decisions matter, free-form text only for article prose).
    Everything beside it exists because "the draft did not invent anything" is not
    checkable by reading the prose: the declarations are, against the source model
    and the brief.
    """

    title: str
    thesis: str
    body: str
    claims_used: tuple[str, ...] = ()
    qualifications_applied: tuple[str, ...] = ()
    unresolved: tuple[UnresolvedMarker, ...] = ()
    omitted: tuple[OmittedMaterial, ...] = ()
    finish_reason: str = "stop"

    @model_validator(mode="after")
    def _markers_are_visible(self) -> Self:
        for item in self.unresolved:
            if not item.marker.strip():
                raise ValueError("an unresolved marker needs text a reader can see")
            if item.marker not in self.body:
                raise ValueError(
                    f"the unresolved marker {item.marker!r} does not appear in the body; "
                    "a marker nobody can see is an invented fact with extra steps"
                )
        return self

    @property
    def word_count(self) -> int:
        """Words in the body, as a reader would count them."""
        return len(self.body.split())


class ReviewFinding(BaseModel):
    """One thing a substantive review found, with everything needed to weigh it.

    The field set is plan/07's, and the length of it is the point: a finding the
    author cannot locate, cannot check, and cannot act on is a complaint. Every
    field here answers one of "where", "why should I believe you" and "what would
    fix it".

    ``suggested_route`` is a :class:`~groundscribe.workflow.policy.FailureCategory`
    rather than free text, because phase 08 routes on it. A reviewer that invented
    its own routing vocabulary would produce findings nothing could act on.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    severity: IssueSeverity
    category: str
    location: str = ""
    passage: str = ""
    description: str
    evidence: str = ""
    source_ref: str = ""
    brief_ref: str = ""
    recommended_correction: str = ""
    suggested_route: FailureCategory
    blocks_publication: bool = False
    reviewer_confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _findings_are_weighable(self) -> Self:
        if self.severity is IssueSeverity.BLOCKING and not self.blocks_publication:
            raise ValueError(
                f"finding {self.id!r} is blocking but says blocks_publication is false; "
                "those are one judgement, and two answers to it cannot both be acted on"
            )
        if not any((self.evidence.strip(), self.source_ref.strip(), self.brief_ref.strip())):
            raise ValueError(
                f"finding {self.id!r} points at no evidence, source claim or brief clause; "
                "a criticism with nothing behind it is an opinion the author cannot weigh"
            )
        return self

    def fingerprint(self) -> str:
        """A stable identity for "the same finding", across rounds.

        Built from what the finding *says* — its category, where it points, and the
        evidence behind it — and deliberately not from its id, which the reviewer
        renumbers freely between rounds. Evidence is included so that raising the
        same point with something new behind it produces a different fingerprint:
        that is what lets a dismissed finding be reopened honestly (plan/07 → not
        reintroduced without new evidence).
        """
        material = "\u241f".join(
            (self.category, self.location, self.passage, self.source_ref, self.evidence.strip())
        )
        return sha256(material.encode("utf-8")).hexdigest()[:32]


class SubstantiveReview(_Output):
    """A review of one article version: argument and accuracy, not sentence polish."""

    verdict: str
    summary: str = ""
    dimensions_assessed: tuple[str, ...] = ()
    issues: tuple[ReviewFinding, ...] = ()

    @model_validator(mode="after")
    def _finding_ids_are_unique(self) -> Self:
        ids = [issue.id for issue in self.issues]
        duplicated = sorted({key for key in ids if ids.count(key) > 1})
        if duplicated:
            raise ValueError(f"finding ids must be unique; repeated: {', '.join(duplicated)}")
        return self

    @property
    def iteration_forcing(self) -> tuple[ReviewFinding, ...]:
        """The findings that are worth another revision round."""
        return tuple(issue for issue in self.issues if issue.severity.forces_iteration)

    @property
    def requires_iteration(self) -> bool:
        """Whether this review asks for a rewrite at all."""
        return bool(self.iteration_forcing)

    def cited_claim_ids(self) -> frozenset[str]:
        """Every source claim the findings point at."""
        return frozenset(issue.source_ref for issue in self.issues if issue.source_ref)


class ReviewIssueReport(BaseModel):
    """What the author did with a round's findings, summarised for the planner."""

    model_config = ConfigDict(frozen=True)

    accepted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    suppressed: tuple[str, ...] = ()

    @property
    def actionable(self) -> tuple[str, ...]:
        """Findings the revision plan is built from: accepted, and accepted-with-edits."""
        return (*self.accepted, *self.edited)

    @property
    def dismissed(self) -> tuple[str, ...]:
        """Findings the author argued with, kept so the next round can see them."""
        return self.rejected


class ReconciliationKind(StrEnum):
    """What a planner did with feedback it could not simply apply (phase 07 §9).

    Three, because there are three honest answers to two findings that disagree:
    resolve them into one change, hold one back for a later round, or decline it.
    A fourth — apply both — is what produces the article that argues with itself.
    """

    COMBINED = "combined"
    DEFERRED = "deferred"
    REJECTED = "rejected"


class PlanChange(BaseModel):
    """One change the rewrite must make, and the findings that asked for it.

    ``required`` is the distinction the rewriter acts on: a required change is the
    plan, an optional one is a suggestion the rewrite may take if it is already in
    the neighbourhood.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    finding_refs: tuple[str, ...] = Field(min_length=1)
    required: bool = True
    sections: tuple[str, ...] = ()


class Reconciliation(BaseModel):
    """Feedback that could not be applied as given, and why."""

    model_config = ConfigDict(extra="forbid")

    kind: ReconciliationKind
    finding_refs: tuple[str, ...] = Field(min_length=1)
    rationale: str

    @model_validator(mode="after")
    def _reconciliations_are_explained(self) -> Self:
        if not self.rationale.strip():
            raise ValueError(
                f"a {self.kind.value} reconciliation needs a rationale: without one it is "
                "two findings in a box, and nobody can judge whether the contradiction was "
                "resolved the right way"
            )
        return self


class RevisionPlanDocument(_Output):
    """What the rewrite will do, and what it must leave alone (phase 07 §9).

    The plan exists because a rewriter handed raw review findings applies them
    blindly — including the two that contradict each other. Everything here is
    either an instruction the rewrite follows or a boundary it may not cross.
    """

    summary: str
    changes: tuple[PlanChange, ...] = ()
    preserve_sections: tuple[str, ...] = ()
    claims_that_must_not_change: tuple[str, ...] = ()
    sections_to_remove: tuple[str, ...] = ()
    sections_to_move: tuple[str, ...] = ()
    reopen_brief: bool = False
    reopen_architecture: bool = False
    expected_score_effect: str = ""
    reconciliations: tuple[Reconciliation, ...] = ()

    @property
    def required_changes(self) -> tuple[PlanChange, ...]:
        """The changes the rewrite must make."""
        return tuple(change for change in self.changes if change.required)

    @property
    def optional_changes(self) -> tuple[PlanChange, ...]:
        """The changes the rewrite may make if it is already there."""
        return tuple(change for change in self.changes if not change.required)

    def addressed_findings(self) -> frozenset[str]:
        """Every finding this plan acts on or explains away."""
        applied = {ref for change in self.changes for ref in change.finding_refs}
        explained = {ref for item in self.reconciliations for ref in item.finding_refs}
        return frozenset(applied | explained)


class RewrittenArticle(ArticleDraft):
    """A draft rewritten against an approved revision plan (phase 07 §10).

    The same shape as the draft it replaces — a rewrite is still an article, and a
    second schema would mean two definitions of what an article version is — plus
    what it did with the plan.

    Declaring skipped changes is what keeps the rewriter honest. An optional change
    may be skipped; skipping it *silently* may not, because the difference between
    "I judged this unnecessary" and "I forgot" is the whole reason the plan named it.
    """

    changes_applied: tuple[str, ...] = ()
    changes_skipped: tuple[str, ...] = ()
    skip_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _skips_are_explained(self) -> Self:
        if self.changes_skipped and not self.skip_reasons:
            raise ValueError(
                f"the rewrite skipped {', '.join(self.changes_skipped)} without a reason; "
                "a skipped change with no reason cannot be told from a forgotten one"
            )
        return self


class VoiceChangeKind(StrEnum):
    """The edits a voice pass is permitted to make (phase 07 §11).

    This enum *is* the permission list. plan/07 names what a style pass may change
    and what it may not, and expressing the permitted set as a closed vocabulary
    means a prohibited change — a new claim, a changed thesis, a removed
    qualification — has no member to be declared as. Refusing prohibited changes
    would still leave them representable; this way they are not.
    """

    RHYTHM = "rhythm"
    WORD_CHOICE = "word_choice"
    FLOW = "flow"
    REPETITION = "repetition"
    FORMALITY = "formality"
    TRANSITION = "transition"
    PHRASING = "phrasing"
    ABSTRACTION = "abstraction"
    AI_PATTERN = "ai_pattern"


class VoiceChange(BaseModel):
    """One stylistic edit, quoted from both sides so it can be checked."""

    model_config = ConfigDict(extra="forbid")

    kind: VoiceChangeKind
    before: str
    after: str = ""
    reason: str = ""


class StructuralProblem(BaseModel):
    """Something a style pass found that a style pass must not fix."""

    model_config = ConfigDict(extra="forbid")

    location: str
    description: str
    suggested_route: FailureCategory = FailureCategory.SUBSTANTIVE_ISSUE


class VoicePass(_Output):
    """A style-only pass: new prose, the edits that produced it, and what it could not fix.

    Deliberately without a thesis, claims or qualifications. The next version is
    built by copying the previous one and replacing its ``body``, so there is
    nothing here through which a voice pass could change what the article asserts.
    """

    body: str
    changes: tuple[VoiceChange, ...] = ()
    structural_problems: tuple[StructuralProblem, ...] = ()

    @model_validator(mode="after")
    def _edits_are_declared(self) -> Self:
        if not self.changes and not self.structural_problems:
            raise ValueError(
                "a voice pass must declare at least one change, or report the structural "
                "problem that stopped it; an undeclared edit cannot be checked"
            )
        return self


class DimensionScore(BaseModel):
    """One dimension's score, and why it is that number.

    The rationale is not decoration. A dimension score with no reasoning behind it
    cannot be argued with, and plan/08's whole mitigation for false precision is
    that a score is always shown with the reasoning and evidence that produced it.
    """

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=100.0)
    rationale: str = Field(min_length=1)


class ScoreDeduction(BaseModel):
    """One material deduction, with everything needed to check it (phase 08).

    The field set is plan/08's: dimension, passage, the source or brief requirement
    it fails, how it fails it, severity, the route that would correct it, and the
    scorer's confidence. The same reasoning as phase 07's ``ReviewFinding`` — a
    deduction the author cannot locate and cannot check is a number with an opinion
    attached.

    ``rubric_required`` is deliberately separate from ``severity``. Severity is the
    scorer's judgement of how bad something is; ``rubric_required`` is the
    project's judgement of whether it is negotiable at all. A house style that
    genuinely blocks publication is not made blocking by asking a model to feel
    more strongly about it (plan/08 → *unless the rubric marks them required*).
    """

    model_config = ConfigDict(extra="forbid")

    dimension: ScoreDimension
    points: float = Field(gt=0.0, le=100.0)
    severity: IssueSeverity
    passage: str = ""
    requirement: str = ""
    mismatch: str = Field(min_length=1)
    recommended_correction: str = ""
    suggested_route: FailureCategory
    source_ref: str = ""
    brief_ref: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rubric_required: bool = False

    @model_validator(mode="after")
    def _deductions_are_locatable(self) -> Self:
        if not self.passage.strip() and not self.requirement.strip():
            raise ValueError(
                f"the {self.dimension.value} deduction names no passage and no requirement; "
                "a deduction nobody can locate is a number with an opinion attached"
            )
        return self

    @property
    def forces_iteration(self) -> bool:
        """Whether this deduction is worth another revision round.

        Severity decides it, except where the rubric has taken the decision out of
        the scorer's hands. An optional preference costs points and nothing else.
        """
        return self.severity.forces_iteration or self.rubric_required


class ArticleScore(_Output):
    """What a scorer judged about one article version (phase 08).

    Conspicuously without an overall. The scorer judges the seven dimensions and
    explains its deductions; the *rubric* combines them, under a named version,
    with weights nobody asked a model about. A scorer returning its own overall
    could disagree with the configured weights, and the disagreement would be
    invisible — it would look like a score.

    ``unsupported_claims`` is asked for directly rather than inferred from the
    deductions. plan/08 makes "no unsupported major claims" a passing condition in
    its own right, and a condition derived from a deduction's category would be a
    condition a scorer could dodge by choosing a different category.
    """

    summary: str = ""
    dimensions: Mapping[ScoreDimension, DimensionScore]
    deductions: tuple[ScoreDeduction, ...] = ()
    unsupported_claims: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _the_deductions_explain_the_scores(self) -> Self:
        """Every dimension judged, and no deduction claiming more than was lost.

        The second check is what keeps the deductions an *explanation* rather than
        a parallel set of complaints. A scorer is not required to account for every
        point — two off for something immaterial is fine — but thirty points off a
        dimension that scored ninety describes a different article from the one it
        just scored.
        """
        missing = sorted(
            dimension.value for dimension in ScoreDimension if dimension not in self.dimensions
        )
        if missing:
            raise ValueError(f"no score for dimension(s): {', '.join(missing)}")

        for dimension in ScoreDimension:
            claimed = sum(
                deduction.points
                for deduction in self.deductions
                if deduction.dimension is dimension
            )
            lost = 100.0 - self.dimensions[dimension].score
            if claimed > lost + 1e-9:
                raise ValueError(
                    f"the {dimension.value} deductions claim {claimed:g} points but the "
                    f"dimension only lost {lost:g}; deductions that overshoot the score "
                    "explain a different article from the one that was scored"
                )
        return self

    @property
    def scores(self) -> dict[ScoreDimension, float]:
        """The bare numbers, in the shape the rubric combines."""
        return {dimension: value.score for dimension, value in self.dimensions.items()}

    @property
    def forces_iteration(self) -> tuple[ScoreDeduction, ...]:
        """The deductions worth another revision round."""
        return tuple(deduction for deduction in self.deductions if deduction.forces_iteration)

    @property
    def blocking(self) -> tuple[ScoreDeduction, ...]:
        """The deductions that block publication outright."""
        return tuple(
            deduction
            for deduction in self.deductions
            if deduction.severity is IssueSeverity.BLOCKING
        )


__all__ = [
    "ArchitectureDecision",
    "ArchitectureProposal",
    "ArticleBriefDocument",
    "ArticleDraft",
    "ArticleScore",
    "BriefSection",
    "DevelopmentEvent",
    "DimensionScore",
    "DoneCriterion",
    "Evidence",
    "ExtractedClaim",
    "GapReport",
    "Lesson",
    "OmittedMaterial",
    "PlanChange",
    "PotentialArgument",
    "ProductFact",
    "ProposedArticle",
    "PublicationConstraint",
    "Reconciliation",
    "ReconciliationKind",
    "RejectedAlternative",
    "ReviewFinding",
    "ReviewIssueReport",
    "RevisionPlanDocument",
    "RewrittenArticle",
    "RiskLevel",
    "ScoreDeduction",
    "SeriesConsiderations",
    "SourceGapQuestion",
    "SourceModel",
    "StructuralProblem",
    "SubstantiveReview",
    "UnresolvedMarker",
    "VoiceChange",
    "VoiceChangeKind",
    "VoicePass",
    "VoiceProfileDocument",
]
