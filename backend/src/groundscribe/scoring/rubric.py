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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall: float = Field(default=85.0, ge=0.0, le=100.0)
    minimums: Mapping[ScoreDimension, float] = Field(default_factory=dict)


@dataclass(frozen=True)
class ScoreFailure:
    """One condition the article did not meet.

    ``dimension`` is ``None`` for the conditions that are not about a dimension —
    the overall floor, a blocking issue, an unsupported claim — because those are
    failures of the article rather than of one reading of it.
    """

    detail: str
    dimension: ScoreDimension | None = None
    threshold: float | None = None
    actual: float | None = None


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
            raise ScoringRubricError(f"invalid scoring rubric {path}: {exc}") from exc

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
    ) -> ScoreAssessment:
        """Score the article and decide the verdict, reporting both.

        Every failing condition is collected rather than stopping at the first.
        The verdict is one bit, but the person deciding what to do about it needs
        to know whether one dimension slipped or the article is wrong in four
        ways, and phase 08's routing picks a correcting stage from the failures.
        """
        resolved = self.weights_for(depth)
        checked = _checked(dimensions)
        overall = sum(checked[key] * resolved.weight(key) for key in ScoreDimension)

        failures: list[ScoreFailure] = []
        if overall < self.passing.overall:
            failures.append(
                ScoreFailure(
                    detail=(
                        f"the overall score is {overall:.1f}, below the "
                        f"{self.passing.overall:g} this rubric requires"
                    ),
                    threshold=self.passing.overall,
                    actual=overall,
                )
            )
        failures.extend(
            ScoreFailure(
                detail=(
                    f"{dimension.value} scored {checked[dimension]:.1f}, below its "
                    f"floor of {minimum:g}"
                ),
                dimension=dimension,
                threshold=minimum,
                actual=checked[dimension],
            )
            for dimension in ScoreDimension
            if (minimum := self.passing.minimums.get(dimension)) is not None
            and checked[dimension] < minimum
        )
        failures.extend(
            ScoreFailure(detail=f"a blocking issue is unresolved: {issue}")
            for issue in blocking_issues
        )
        failures.extend(
            ScoreFailure(detail=f"the article rests on the unsupported major claim {claim}")
            for claim in unsupported_claims
        )
        # plan/08: "optional stylistic preferences don't force a rewrite *unless the
        # rubric marks them required*". A requirement the project stated outright is
        # not a preference to be weighed against the score — it either holds or the
        # article is not publishable, however well it reads.
        failures.extend(
            ScoreFailure(detail=f"a requirement the rubric marks as required is unmet: {unmet}")
            for unmet in unmet_requirements
        )

        return ScoreAssessment(
            overall=overall,
            passed=not failures,
            dimensions=checked,
            weights=resolved,
            failures=tuple(failures),
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
