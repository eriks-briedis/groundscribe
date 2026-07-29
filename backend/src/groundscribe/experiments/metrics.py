"""The twelve numbers an experiment compares two configurations on (phase 12).

plan/12 → *Experiment metrics: pass rate, human preference, unsupported-claim
rate, average revision rounds, cost, latency, schema-failure rate, reviewer
disagreement, manual-edit distance, final acceptance rate, stagnation frequency,
confidentiality-validation failures.*

Split in two on purpose. :class:`ExampleEvidence` is what one arm did to one
example, gathered from the rows a run left behind; :func:`aggregate_arm` is
arithmetic over a sequence of those and knows nothing about a database. The seam
is where the interesting mistakes live — a metric is wrong far more often because
of what went into the denominator than because a sum was miscounted — and it
means every one of them can be tested against numbers a reader can check by hand.

**Nothing observed is not zero.** Every rate here is ``None`` until there is
something to divide. A table showing 0% pass rate for an arm that was never
scored says the arm failed everything, which is the reading somebody will act on.

**Each denominator is a claim, so each is chosen rather than inherited.** Pass
rate counts scored examples, human preference counts decided entries, the
schema-failure rate counts model calls, and confidentiality failures are not a
rate at all: one is too many, and one out of fifty rendered as 2% looks like
noise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean

from pydantic import BaseModel, ConfigDict

from groundscribe.experiments.edit_distance import ManualEditDistance

#: The metrics plan/12 names, as the fields that must appear on every arm's row.
#: A list rather than a comment, so a metric quietly dropped from the model fails
#: a test by name instead of vanishing from every comparison table.
METRIC_NAMES: tuple[str, ...] = (
    "pass_rate",
    "human_preference",
    "unsupported_claim_rate",
    "average_revision_rounds",
    "total_cost_usd",
    "mean_latency_ms",
    "schema_failure_rate",
    "reviewer_disagreement",
    "manual_edit_distance",
    "final_acceptance_rate",
    "stagnation_frequency",
    "confidentiality_failures",
)


@dataclass(frozen=True)
class ExampleEvidence:
    """What one arm did to one example, as everything the metrics need.

    A flat record rather than a handle on the database. Gathering is a separate
    job with separate failure modes, and a metric that reached for its own rows
    could not be checked against numbers written by hand.

    ``succeeded`` is kept apart from every other field because a failed run is
    still an example: an arm that crashed on half the corpus and scored perfectly
    on the rest is not a good arm, and dropping its failures would say it was.
    """

    entry_id: str
    succeeded: bool
    scored: bool = False
    passed: bool = False
    unsupported_claims: int = 0
    score_dispersion: float | None = None
    revision_rounds: int = 0
    stagnated: bool = False
    accepted: bool = False
    cost_usd: float | None = None
    latency_ms: int | None = None
    model_calls: int = 0
    schema_failures: int = 0
    confidentiality_failures: int = 0
    edit_distance: ManualEditDistance | None = None
    #: Whether a person compared the arms on this entry at all, kept apart from
    #: which one they chose: undecided and not-preferred are different facts.
    decided: bool = False
    preferred: bool = False


class ArmMetrics(BaseModel):
    """One configuration's row in the comparison table."""

    model_config = ConfigDict(frozen=True)

    arm_id: str
    label: str
    baseline: bool
    examples: int
    completed: int

    pass_rate: float | None = None
    human_preference: float | None = None
    unsupported_claim_rate: float | None = None
    average_revision_rounds: float | None = None
    total_cost_usd: float | None = None
    mean_latency_ms: float | None = None
    schema_failure_rate: float | None = None
    reviewer_disagreement: float | None = None
    manual_edit_distance: float | None = None
    final_acceptance_rate: float | None = None
    stagnation_frequency: float | None = None
    confidentiality_failures: int = 0


def aggregate_arm(
    *, arm_id: str, label: str, baseline: bool, evidence: Sequence[ExampleEvidence]
) -> ArmMetrics:
    """Reduce one arm's per-example results to the twelve numbers."""
    scored = [item for item in evidence if item.scored]
    decided = [item for item in evidence if item.decided]
    calls = sum(item.model_calls for item in evidence)
    costs = [item.cost_usd for item in evidence if item.cost_usd is not None]
    latencies = [item.latency_ms for item in evidence if item.latency_ms is not None]
    dispersions = [item.score_dispersion for item in evidence if item.score_dispersion is not None]
    distances = [item.edit_distance for item in evidence if item.edit_distance is not None]

    return ArmMetrics(
        arm_id=arm_id,
        label=label,
        baseline=baseline,
        examples=len(evidence),
        completed=sum(1 for item in evidence if item.succeeded),
        pass_rate=_share(sum(1 for item in scored if item.passed), len(scored)),
        human_preference=_share(sum(1 for item in decided if item.preferred), len(decided)),
        unsupported_claim_rate=_share(
            sum(1 for item in scored if item.unsupported_claims), len(scored)
        ),
        average_revision_rounds=_mean([float(item.revision_rounds) for item in evidence]),
        total_cost_usd=round(sum(costs), 10) if costs else None,
        mean_latency_ms=_mean([float(value) for value in latencies]),
        schema_failure_rate=_share(sum(item.schema_failures for item in evidence), calls),
        reviewer_disagreement=_mean(dispersions),
        # The ratio rather than the character count: a corpus of articles of
        # different lengths would otherwise average out as the longest one.
        manual_edit_distance=_mean([item.character_ratio for item in distances]),
        final_acceptance_rate=_share(sum(1 for item in evidence if item.accepted), len(evidence)),
        stagnation_frequency=_share(sum(1 for item in evidence if item.stagnated), len(evidence)),
        confidentiality_failures=sum(item.confidentiality_failures for item in evidence),
    )


def _share(part: int, whole: int) -> float | None:
    """A proportion, or nothing when there was nothing to divide."""
    return part / whole if whole else None


def _mean(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None


__all__ = ["METRIC_NAMES", "ArmMetrics", "ExampleEvidence", "aggregate_arm"]
