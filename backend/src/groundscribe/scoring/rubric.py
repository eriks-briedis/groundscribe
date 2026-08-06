"""The versioned scoring rubric: weights, the overall score, and what passes (phase 08).

plan/08 → *ScoreArticle stage*, *Passing policy (versioned)*, *Editorial-score vs
routing-result separation*.

Two numbers come out of scoring an article and they are not the same kind of
thing. The **overall** is a weighted combination of seven dimensions on a 0-100
scale — a summary, useful for comparing versions and spotting trends, and
explicitly not an objective measurement. The **verdict** is a conjunction: every
threshold met, no blocking issue, no unsupported major claim. Nothing averages
into the verdict, which is why an article can score 87 and still fail. That is
not a special case to be handled; it is the arithmetic of asking two different
questions.

Weights are per content type because "how much does evidence density matter?" has
a different answer for an overview than for a deep dive. The content type is
:class:`~groundscribe.domain.enums.ArticleDepth` — the axis the domain already
models and every project already declares — rather than a second vocabulary that
would have to be kept in step with the first.

Loaded from a file for the reason phases 04 and 05 load theirs: these are
editorial judgements an author should be able to read, diff and tune without a
deploy. Versioned because a score is the output of a judgement that moves, and a
run that scored 84 last month and 87 today is otherwise indistinguishable from a
rubric that got more generous.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError, model_validator

from groundscribe.domain.enums import ArticleDepth
from groundscribe.paths import config_root

#: Filename of the shipped scoring rubric under the config root.
SCORING_RUBRIC_FILENAME = "scoring-rubric.yaml"

#: The key of the weight set used by any content type without one of its own.
DEFAULT_CONTENT_TYPE = "default"

#: Tolerance on the weights-sum check. Editorial weights are written as decimals
#: by hand, so 0.25 + 0.15*3 + 0.1*3 has to be allowed to land a float epsilon
#: away from 1.0 without anyone having to know that.
_SUM_TOLERANCE = 1e-9


class ScoringRubricError(Exception):
    """The rubric is missing, malformed, or was handed a score it cannot use."""


def _flat_minimums_hint(raw: object) -> str | None:
    """Recognise a version-1 rubric and say what to do about it, or ``None``.

    ``minimums`` became per content type, so a rubric written against the flat
    shape fails to load — correctly and loudly, because a rubric that fell back
    to no floors would present as a passing policy that quietly stopped applying.

    But the schema error for it is "Input should be a valid dictionary", four
    times, naming the four floors and not the change. A person reading that goes
    looking for a typo in a file they have not touched. This is the same rule the
    truncation message keeps: when a message can name the fix, it should.
    """
    if not isinstance(raw, Mapping):
        return None
    passing = raw.get("passing")
    if not isinstance(passing, Mapping):
        return None
    minimums = passing.get("minimums")
    if not isinstance(minimums, Mapping) or not minimums:
        return None
    if all(isinstance(value, Mapping) for value in minimums.values()):
        return None
    return (
        "`passing.minimums` is now per content type, and this file has the flat "
        f"version-1 shape. Nest what is there under `default:` — "
        f"`minimums: {{default: {{...}}}}` — and add per-type blocks for "
        f"{', '.join(sorted(depth.value for depth in ArticleDepth))} where the "
        "floors differ. A content type states only what it changes, and `null` "
        "removes a floor for it."
    )


class ScoreDimension(StrEnum):
    """What an article is scored on (plan/08).

    Seven dimensions, each corrected by a different kind of change. A dimension
    that moved for the same reasons as another would not be a dimension, it would
    be a second reading of one — and it would double that reading's weight.
    """

    FACTUAL_FIDELITY = "factual_fidelity"
    THESIS_AND_FOCUS = "thesis_and_focus"
    STRUCTURE_AND_COHERENCE = "structure_and_coherence"
    EVIDENCE_AND_SPECIFICITY = "evidence_and_specificity"
    READER_VALUE = "reader_value"
    SCOPE_DISCIPLINE = "scope_discipline"
    VOICE_ADHERENCE = "voice_adherence"


class DimensionWeights(RootModel[Mapping[ScoreDimension, float]]):
    """One content type's weighting of the seven dimensions.

    A root model so the config reads as the mapping it is — ``deep_dive:
    {factual_fidelity: 0.2, …}`` rather than a ``weights:`` key nested inside a
    ``weights:`` key.

    Every dimension must carry a weight and they must sum to one. Both are
    refused at load rather than at score time: a missing dimension is one the
    rubric asks a model to judge and then discards, and a set summing to anything
    else rescales every score it ever produces — neither fails, they just make
    every number quietly wrong.
    """

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def _covers_every_dimension_exactly_once(self) -> Self:
        missing = sorted(
            dimension.value for dimension in ScoreDimension if dimension not in self.root
        )
        if missing:
            raise ValueError(f"weights are missing dimensions: {', '.join(missing)}")

        total = sum(self.root.values())
        if abs(total - 1.0) > _SUM_TOLERANCE:
            raise ValueError(f"weights must sum to 1.0; these sum to {total:g}")
        return self


@dataclass(frozen=True)
class ResolvedWeights:
    """The weight set one score was computed under, and where it came from.

    Carries the rubric version and the content type alongside the numbers so the
    evaluation record is written without a second lookup — the same shape as
    phase 04's :class:`~groundscribe.llm.routing.ResolvedRoute` and phase 05's
    :class:`~groundscribe.workflow.policy.RoutingOutcome`.
    """

    content_type: str
    rubric_version: str
    weights: Mapping[ScoreDimension, float]
    used_default: bool = False

    def weight(self, dimension: ScoreDimension) -> float:
        """This set's weight for ``dimension``; validated to exist at load time."""
        return self.weights[dimension]


class PassingPolicy(BaseModel):
    """Every condition an article must meet, not a score to beat.

    ``minimums`` are floors on individual dimensions, and they exist because a
    weight is the wrong instrument for a hard requirement: voice adherence is a
    tenth of the overall, so a terrible voice score moves the number it would be
    judged by about as much as a rounding error. plan/08's "high scores in other
    dimensions must not mask a critical weakness" is enforced here, by not
    letting the overall be the only test.

    **Per content type, as the weights already are.** They were not, and the two
    disagreed about something that matters: ``weights`` says evidence density is
    worth 0.05 to an overview and 0.25 to a deep dive — "an overview citing every
    number would be a deep dive that failed to notice" — while ``minimums`` was
    one flat set for every article the system writes. Three dimensions therefore
    had no floor anywhere, and the conjunction that protects against publishing
    something *wrong* had nothing to say about publishing something *empty*.

    IMPROVEMENTS §9 measured that. A 92-claim source became five articles; the
    one arguing that concreteness is the product was allocated 14 claims and
    every concrete artefact was routed elsewhere. The scorer named the defect
    exactly — ``evidence_and_specificity`` 86, "names categories of traceable
    material but does not show a concrete inspected artefact" — and the article
    passed at 92.85, because that dimension had no floor to fall below.

    A single global floor could not have fixed it, which is why this is a schema
    change rather than a number. To fail that article it would have to sit at
    87-88, above the floors on focus (80), scope (80) and voice (75) and just
    under factual fidelity (90) — asserting that specificity is nearly as
    non-negotiable as accuracy. That is not true of every article, and the
    weights already knew it.

    **Merged over the default, not replacing it.** Weights replace wholesale
    because they must sum to 1.0; floors have no such constraint, so a content
    type states only what it changes. Restating ``factual_fidelity: 90`` in every
    block is four copies of one editorial decision, and they would drift.

    A floor set to ``None`` for a content type is *removed* for it. That is the
    case the merge exists to express: an overview is not held to a deep dive's
    evidence density, and "no floor here" has to be sayable without restating the
    other four.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall: float = Field(default=85.0, ge=0.0, le=100.0)
    minimums: Mapping[str, Mapping[ScoreDimension, float | None]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _floors_are_addressed_to_a_content_type_that_exists(self) -> Self:
        """Refuse a floor set nothing will ever resolve to.

        The same rule the weights keep, for the same reason: a typo in a content
        type is a floor that silently never applies, which looks exactly like a
        rubric that is not being enforced.
        """
        known = {depth.value for depth in ArticleDepth} | {DEFAULT_CONTENT_TYPE}
        unknown = sorted(set(self.minimums) - known)
        if unknown:
            raise ValueError(
                f"unknown content type(s) in minimums: {', '.join(unknown)}; "
                f"expected one of {', '.join(sorted(known))}"
            )
        return self

    def minimums_for(self, depth: ArticleDepth | None) -> Mapping[ScoreDimension, float]:
        """The floors in force for one content type: the default, then its own.

        Resolved the same way :meth:`ScoringRubric.weights_for` resolves weights,
        so a depth nobody has tuned is still judged rather than being judged by
        nothing. ``None`` entries drop out here, which is what makes them mean
        "not floored" rather than "floored at zero".
        """
        merged = dict(self.minimums.get(DEFAULT_CONTENT_TYPE, {}))
        if depth is not None:
            merged |= dict(self.minimums.get(depth.value, {}))
        return {
            dimension: floor for dimension, floor in merged.items() if floor is not None
        }


class FailureKind(StrEnum):
    """Which passing condition a failure is, as a value rather than a sentence.

    The conditions have always been distinguishable — they are constructed in
    five separate blocks — but only their prose survived into the assessment, so
    anything downstream asking "what *kind* of failure is this" had to match on
    the wording of a message written for a person to read. That is a coupling
    that breaks silently: reword the message to read better and the matcher
    stops matching, with no test between the two.

    It matters now because one condition earns a different answer from the rest.
    An unsupported claim is a defect the scorer has already localised to a span,
    and an article failing on nothing else can be corrected by cutting it
    (:data:`~groundscribe.workflow.states.WorkflowAction.CORRECT_CLAIMS`). A
    dimension below its floor cannot.
    """

    OVERALL = "overall"
    DIMENSION_FLOOR = "dimension_floor"
    BLOCKING_ISSUE = "blocking_issue"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    UNMET_REQUIREMENT = "unmet_requirement"


@dataclass(frozen=True)
class ScoreFailure:
    """One condition the article did not meet.

    ``dimension`` is ``None`` for the conditions that are not about a dimension —
    the overall floor, a blocking issue, an unsupported claim — because those are
    failures of the article rather than of one reading of it.

    ``subject`` is what the condition was about, unwrapped from the sentence: the
    claim, the blocking issue or the requirement, verbatim as the scorer named
    it. A stage asked to cut an unsupported claim needs the claim, and parsing it
    back out of ``detail`` would mean parsing a message whose job is to read
    well.
    """

    detail: str
    kind: FailureKind = FailureKind.OVERALL
    dimension: ScoreDimension | None = None
    threshold: float | None = None
    actual: float | None = None
    subject: str = ""


@dataclass(frozen=True)
class ScoreAssessment:
    """The editorial score and the verdict, side by side.

    Both, always. A caller shown only the verdict cannot tell a near-miss from a
    disaster; one shown only the score cannot tell why a good-looking article was
    sent back (plan/08 → *the interface shows both*).
    """

    overall: float
    passed: bool
    dimensions: Mapping[ScoreDimension, float]
    weights: ResolvedWeights
    failures: tuple[ScoreFailure, ...] = ()
    #: The floors this article was actually held to, after the content type was
    #: resolved. Carried rather than looked up again, for the reason
    #: :class:`ResolvedWeights` is: a score is read alongside what produced it,
    #: and "evidence was not floored for an overview" and "evidence had a floor
    #: and cleared it" are different facts about the same passing article.
    floors: Mapping[ScoreDimension, float] = field(default_factory=dict)
    #: Dimensions there was nothing to judge against (phase 16).
    #:
    #: Kept on the assessment rather than dropped silently, because "94 for voice"
    #: and "no voice profile, so voice was not judged" are different facts and
    #: only one of them is true. A reader shown the first cannot recover the
    #: second, and every score in the system is meant to be shown with what
    #: produced it.
    unassessable: tuple[ScoreDimension, ...] = ()

    @property
    def rubric_version(self) -> str:
        """The rubric version this assessment was made under."""
        return self.weights.rubric_version


class ScoringRubric(BaseModel):
    """The versioned rubric: what counts, how much, and what passes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    description: str = ""
    weights: Mapping[str, DimensionWeights]
    passing: PassingPolicy = PassingPolicy()

    @model_validator(mode="after")
    def _every_content_type_can_be_scored(self) -> Self:
        """Refuse a rubric with no default, or with a content type nothing declares.

        The default is what makes the content-type axis optional rather than
        exhaustive: a depth nobody has tuned still scores. An unknown key is the
        other half — a typo in a content type would otherwise be a weight set that
        silently never applies, which looks exactly like a rubric that does not
        work.
        """
        if DEFAULT_CONTENT_TYPE not in self.weights:
            raise ValueError(
                f"the rubric has no {DEFAULT_CONTENT_TYPE!r} weight set; a content type "
                "nobody has tuned would have no way to be scored"
            )
        known = {depth.value for depth in ArticleDepth} | {DEFAULT_CONTENT_TYPE}
        unknown = sorted(set(self.weights) - known)
        if unknown:
            raise ValueError(
                f"unknown content type(s) in weights: {', '.join(unknown)}; "
                f"expected one of {', '.join(sorted(known))}"
            )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> ScoringRubric:
        """Load a scoring rubric from a YAML file."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ScoringRubricError(f"cannot read scoring rubric {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ScoringRubricError(f"invalid YAML in scoring rubric {path}: {exc}") from exc
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ScoringRubricError(
                f"invalid scoring rubric {path}: {_flat_minimums_hint(raw) or exc}"
            ) from exc

    def weights_for(self, depth: ArticleDepth | None) -> ResolvedWeights:
        """The weight set for one content type, falling back to the default."""
        key = depth.value if depth is not None else DEFAULT_CONTENT_TYPE
        tuned = self.weights.get(key)
        return ResolvedWeights(
            content_type=key if tuned is not None else DEFAULT_CONTENT_TYPE,
            rubric_version=self.version,
            weights=(tuned or self.weights[DEFAULT_CONTENT_TYPE]).root,
            used_default=tuned is None,
        )

    def overall(
        self, dimensions: Mapping[ScoreDimension, float], *, depth: ArticleDepth | None = None
    ) -> float:
        """The weighted overall for one score sheet, on a 0-100 scale."""
        resolved = self.weights_for(depth)
        checked = _checked(dimensions)
        return sum(checked[key] * resolved.weight(key) for key in ScoreDimension)

    def assess(
        self,
        dimensions: Mapping[ScoreDimension, float],
        *,
        depth: ArticleDepth | None = None,
        blocking_issues: Sequence[str] = (),
        unsupported_claims: Sequence[str] = (),
        unmet_requirements: Sequence[str] = (),
        unassessable: Collection[ScoreDimension] = (),
    ) -> ScoreAssessment:
        """Score the article and decide the verdict, reporting both.

        Every failing condition is collected rather than stopping at the first.
        The verdict is one bit, but the person deciding what to do about it needs
        to know whether one dimension slipped or the article is wrong in four
        ways, and phase 08's routing picks a correcting stage from the failures.

        ``unassessable`` names dimensions there was nothing to judge against —
        voice adherence with no voice profile is the case it was written for. They
        are dropped from the weighted overall, which is renormalised over what
        remains, and their floors are not applied: a dimension nobody could
        measure cannot be below a threshold.

        Excluded rather than scored zero *or* scored generously, because both of
        those are claims. A run with an empty profile returned 94 for voice and
        that number was an artefact of an empty input, not a judgement; scoring it
        zero would have been the same mistake pointing the other way.
        """
        resolved = self.weights_for(depth)
        checked = _checked(dimensions)
        skipped = tuple(sorted(set(unassessable), key=lambda item: item.value))
        counted = [key for key in ScoreDimension if key not in set(skipped)]
        if not counted:
            raise ScoringRubricError(
                "every dimension was reported unassessable; an overall over nothing is a "
                "number with no article behind it"
            )
        # Renormalised, so removing a tenth of the weight does not silently cost
        # the article a tenth of its score. The remaining dimensions keep their
        # proportions to each other, which is what the weights were expressing.
        divisor = sum(resolved.weight(key) for key in counted)
        overall = sum(checked[key] * resolved.weight(key) for key in counted) / divisor

        failures: list[ScoreFailure] = []
        if overall < self.passing.overall:
            failures.append(
                ScoreFailure(
                    detail=(
                        f"the overall score is {overall:.1f}, below the "
                        f"{self.passing.overall:g} this rubric requires"
                    ),
                    kind=FailureKind.OVERALL,
                    threshold=self.passing.overall,
                    actual=overall,
                )
            )
        floors = self.passing.minimums_for(depth)
        # Named by the article's own depth, not by ``resolved.content_type``.
        # They are two resolutions and they can disagree: a depth with floors of
        # its own but no weight set of its own resolves to `default` for weights
        # while being floored by its own block, and a message saying "the 88 a
        # default article is held to" sends a reader looking in the wrong place
        # for a number written under their depth's name.
        held_to = depth.value if depth is not None else DEFAULT_CONTENT_TYPE
        failures.extend(
            ScoreFailure(
                detail=(
                    f"{dimension.value} scored {checked[dimension]:.1f}, below the "
                    f"{minimum:g} a {held_to} article is held to"
                ),
                kind=FailureKind.DIMENSION_FLOOR,
                dimension=dimension,
                threshold=minimum,
                actual=checked[dimension],
            )
            for dimension in counted
            if (minimum := floors.get(dimension)) is not None
            and checked[dimension] < minimum
        )
        failures.extend(
            ScoreFailure(
                detail=f"a blocking issue is unresolved: {issue}",
                kind=FailureKind.BLOCKING_ISSUE,
                subject=issue,
            )
            for issue in blocking_issues
        )
        failures.extend(
            ScoreFailure(
                detail=f"the article rests on the unsupported major claim {claim}",
                kind=FailureKind.UNSUPPORTED_CLAIM,
                subject=claim,
            )
            for claim in unsupported_claims
        )
        # plan/08: "optional stylistic preferences don't force a rewrite *unless the
        # rubric marks them required*". A requirement the project stated outright is
        # not a preference to be weighed against the score — it either holds or the
        # article is not publishable, however well it reads.
        failures.extend(
            ScoreFailure(
                detail=f"a requirement the rubric marks as required is unmet: {unmet}",
                kind=FailureKind.UNMET_REQUIREMENT,
                subject=unmet,
            )
            for unmet in unmet_requirements
        )

        return ScoreAssessment(
            overall=overall,
            passed=not failures,
            dimensions=checked,
            weights=resolved,
            failures=tuple(failures),
            floors=floors,
            unassessable=skipped,
        )


def _checked(dimensions: Mapping[ScoreDimension, float]) -> dict[ScoreDimension, float]:
    """One score per dimension, each on the scale, or an error naming what is wrong.

    A missing dimension defaulted to zero would read as "judged and found
    worthless" when it means "not judged", and an out-of-range score would lift
    an overall past the top of its own scale — so both are refused rather than
    coerced.
    """
    missing = sorted(dimension.value for dimension in ScoreDimension if dimension not in dimensions)
    if missing:
        raise ScoringRubricError(f"no score for dimension(s): {', '.join(missing)}")

    out_of_range = sorted(
        f"{dimension.value}={dimensions[dimension]:g}"
        for dimension in ScoreDimension
        if not 0.0 <= dimensions[dimension] <= 100.0
    )
    if out_of_range:
        raise ScoringRubricError(f"scores must be between 0 and 100: {', '.join(out_of_range)}")
    return {dimension: float(dimensions[dimension]) for dimension in ScoreDimension}


def default_scoring_rubric() -> ScoringRubric:
    """The shipped scoring rubric from the config root."""
    return ScoringRubric.from_yaml(config_root() / SCORING_RUBRIC_FILENAME)


def scoring_rubric(version: str | None = None) -> ScoringRubric:
    """The rubric of one version, or the shipped one when no version is named.

    Versions are files, exactly as prompt versions are, and for the same reason:
    a rubric that scored an article has to stay readable after a newer one
    replaces it, or every historical score becomes a number under a document that
    no longer exists. ``scoring-rubric.yaml`` is whichever version is current;
    ``scoring-rubric-<version>.yaml`` is a superseded or candidate one kept beside
    it.

    A version that cannot be found is an error rather than a fallback. Falling
    back to the shipped rubric would score an experiment's candidate arm under
    the baseline's rubric and then report the two as comparable.
    """
    shipped = default_scoring_rubric()
    if version is None or version == shipped.version:
        return shipped

    path = config_root() / f"scoring-rubric-{version}.yaml"
    if not path.exists():
        raise ScoringRubricError(
            f"no scoring rubric version {version!r}: neither {SCORING_RUBRIC_FILENAME} "
            f"(which is version {shipped.version!r}) nor {path.name} provides it"
        )

    rubric = ScoringRubric.from_yaml(path)
    if rubric.version != version:
        raise ScoringRubricError(
            f"{path.name} declares version {rubric.version!r}, not {version!r}; a rubric "
            "recorded against a score under a name it does not answer to is a score nobody "
            "can reproduce"
        )
    return rubric


__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "SCORING_RUBRIC_FILENAME",
    "DimensionWeights",
    "PassingPolicy",
    "ResolvedWeights",
    "ScoreAssessment",
    "ScoreDimension",
    "ScoreFailure",
    "ScoringRubric",
    "ScoringRubricError",
    "default_scoring_rubric",
    "scoring_rubric",
]
