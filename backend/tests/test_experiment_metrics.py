"""What an experiment reports, and what it refuses to report (phase 12).

plan/12 → *Experiment metrics: pass rate, human preference, unsupported-claim
rate, average revision rounds, cost, latency, schema-failure rate, reviewer
disagreement, manual-edit distance, final acceptance rate, stagnation frequency,
confidentiality-validation failures*, tested as *experiment aggregates compute
each metric correctly over per-example results*.

Twelve numbers, and the arithmetic is the easy half. The half worth testing is
what happens when there is nothing to average.

A rate over no observations is not zero. "No example was scored" and "no example
passed" are opposite findings, and a table reporting 0% for both is a table that
will be read wrong exactly once, by somebody deciding whether to adopt a cheaper
model. Every rate here is ``None`` until something has been observed, and every
test below asserts that separately from asserting the arithmetic.

The denominators are chosen per metric for the same reason. Pass rate is over
*scored* examples, human preference over *decided* ones, and the schema-failure
rate is per model call rather than per example — a run that repaired three
responses out of four calls has a different problem from one that failed once.
"""

from __future__ import annotations

from groundscribe.experiments.edit_distance import ManualEditDistance
from groundscribe.experiments.metrics import METRIC_NAMES, ExampleEvidence, aggregate_arm


def evidence(entry: str = "e1", **overrides: object) -> ExampleEvidence:
    """One example's result, defaulting to a run that did nothing notable."""
    return ExampleEvidence(entry_id=entry, succeeded=True, **overrides)  # type: ignore[arg-type]


def distance(ratio: float) -> ManualEditDistance:
    return ManualEditDistance(
        characters=int(ratio * 1000),
        character_ratio=ratio,
        sentences_added=0,
        sentences_removed=0,
        structural_changes=0,
        claim_changes=0,
        voice_corrections=0,
    )


def test_every_metric_the_plan_names_is_reported() -> None:
    """A guard, because twelve is more than anyone checks by eye.

    Asserted against the field names rather than against a list written twice:
    a metric dropped from the model fails here by name instead of quietly
    vanishing from every comparison table.
    """
    metrics = aggregate_arm(arm_id="a", label="baseline", baseline=True, evidence=())

    assert set(METRIC_NAMES) <= set(metrics.model_dump())
    assert len(METRIC_NAMES) == 12


def test_an_arm_with_nothing_to_measure_reports_nothing_rather_than_zero() -> None:
    """The distinction the whole module turns on.

    Zero pass rate reads as "it failed everything". Nothing measured reads as
    "we have not looked", and only one of those is a reason to reject a
    candidate configuration.
    """
    metrics = aggregate_arm(arm_id="a", label="candidate", baseline=False, evidence=())

    assert metrics.examples == 0
    assert metrics.pass_rate is None
    assert metrics.human_preference is None
    assert metrics.unsupported_claim_rate is None
    assert metrics.mean_latency_ms is None
    assert metrics.reviewer_disagreement is None
    assert metrics.manual_edit_distance is None


def test_the_pass_rate_counts_only_the_examples_that_were_scored() -> None:
    """An unscored example is not a failure; it is an absence.

    Counting it as a failure would make an arm that crashed on half the corpus
    look like an arm that wrote badly, and the two call for different work.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", scored=True, passed=True),
            evidence("e2", scored=True, passed=False),
            evidence("e3"),
        ),
    )

    assert metrics.examples == 3
    assert metrics.pass_rate == 0.5


def test_human_preference_is_the_share_of_the_entries_a_person_decided() -> None:
    """Over decided entries, not over the corpus.

    An experiment where somebody looked at two examples out of fifty and
    preferred both should report agreement on what was looked at — not 4%,
    which is a claim about the forty-eight nobody opened.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", decided=True, preferred=True),
            evidence("e2", decided=True, preferred=False),
            evidence("e3"),
        ),
    )

    assert metrics.human_preference == 0.5


def test_the_schema_failure_rate_is_per_model_call() -> None:
    """Per call, because that is the thing that failed.

    A stage that repaired three responses out of four calls has a different
    problem from one that needed a single retry, and a per-example rate reports
    both as "one example had trouble".
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", model_calls=4, schema_failures=3),
            evidence("e2", model_calls=4, schema_failures=1),
        ),
    )

    assert metrics.schema_failure_rate == 0.5


def test_cost_totals_and_latency_averages() -> None:
    """Cost is a bill and latency is an experience.

    Summing latency would report a number nobody waited; averaging cost would
    hide what running the corpus actually charged.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", cost_usd=0.02, latency_ms=1000),
            evidence("e2", cost_usd=0.04, latency_ms=3000),
        ),
    )

    assert metrics.total_cost_usd == 0.06
    assert metrics.mean_latency_ms == 2000


def test_a_provider_that_reported_no_cost_is_not_a_free_one() -> None:
    """Unreported cost stays unreported.

    Treating a silent provider as free is how a candidate configuration wins a
    comparison by not answering the question.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(evidence("e1", cost_usd=None), evidence("e2", cost_usd=None)),
    )

    assert metrics.total_cost_usd is None


def test_reviewer_disagreement_averages_the_dispersion_between_score_passes() -> None:
    """plan/12 → *reviewer disagreement*, which phase 08 already measures.

    The spread between repeat passes over one article is what the system knows
    about how confident its own scoring was; averaging it across the corpus is
    what makes two configurations comparable on it.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", score_dispersion=4.0),
            evidence("e2", score_dispersion=6.0),
            evidence("e3"),
        ),
    )

    assert metrics.reviewer_disagreement == 5.0


def test_the_manual_edit_distance_averages_what_the_author_rewrote() -> None:
    """plan/12's quality signal, aggregated.

    The ratio rather than the character count, because a corpus of articles of
    different lengths would otherwise be an average dominated by the longest
    one.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", edit_distance=distance(0.10)),
            evidence("e2", edit_distance=distance(0.30)),
        ),
    )

    assert metrics.manual_edit_distance == 0.2


def test_revision_rounds_acceptance_and_stagnation_come_out_of_the_run() -> None:
    """The three metrics that are about the run rather than one call.

    Reported per example anyway, because an experiment compares arms and an arm
    is a set of examples: the alternative is a metric that exists on the
    experiment and cannot be attributed to anything inside it.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", revision_rounds=3, accepted=True),
            evidence("e2", revision_rounds=1, accepted=False, stagnated=True),
        ),
    )

    assert metrics.average_revision_rounds == 2.0
    assert metrics.final_acceptance_rate == 0.5
    assert metrics.stagnation_frequency == 0.5


def test_confidentiality_failures_are_counted_and_never_averaged() -> None:
    """One is too many, and a rate would make one out of fifty look small.

    Every other metric here is a tendency. This one is a list of times the
    system nearly published something it had been told not to, and it is
    reported as a count for the same reason a smoke alarm is not a percentage.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", confidentiality_failures=1),
            *[evidence(f"e{index}") for index in range(2, 51)],
        ),
    )

    assert metrics.confidentiality_failures == 1


def test_an_unsupported_claim_rate_counts_examples_not_claims() -> None:
    """One article inventing six figures is one bad article.

    Counting claims would let a single catastrophic example outweigh the rest
    of the corpus, which is the opposite of what a rate is for.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", scored=True, unsupported_claims=6),
            evidence("e2", scored=True, unsupported_claims=0),
        ),
    )

    assert metrics.unsupported_claim_rate == 0.5


def test_a_failed_run_is_counted_as_an_example_that_did_not_complete() -> None:
    """Kept in the denominator of nothing, and in the record of everything.

    An arm that crashed on half the corpus and scored perfectly on the rest is
    not a good arm, and a table that dropped its failures would say it was.
    """
    metrics = aggregate_arm(
        arm_id="a",
        label="candidate",
        baseline=False,
        evidence=(
            evidence("e1", scored=True, passed=True),
            ExampleEvidence(entry_id="e2", succeeded=False),
        ),
    )

    assert metrics.examples == 2
    assert metrics.completed == 1
    assert metrics.pass_rate == 1.0
