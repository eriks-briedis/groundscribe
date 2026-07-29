"""The numbers an operator reads off a running installation (phase 14).

plan/14 → *Observability surface: the spec's metrics (stage duration, token
usage, estimated cost, retry count, validation failures, schema-repair frequency,
score change, rewrite count, accepted/rejected issues, stagnation frequency,
override frequency, question response rate, model fallback frequency, context
truncation frequency, tool failure frequency, human edit distance, final approval
rate) exposed*.

Two properties hold across the file, and they are what the tests are shaped
around.

**A metric is a query over the trace, never a counter beside it.** Nothing here
increments anything at runtime. Every number is derived from provenance rows that
already exist, so a metric cannot drift from the record it summarises — and a
metric that looks wrong is answerable by opening the same rows it read.

**Nothing observed is ``None``, not zero.** The same rule phase 12 set for
experiment metrics, for the same reason: an installation that has never called a
tool reporting a 0% tool-failure rate is stating a fact it does not have. Zero is
reserved for things that were counted and came to nothing.

Split in two, as plan/12's metrics are: :class:`RunObservations` is what the rows
say, :func:`summarise` is arithmetic over it. The seam is where the interesting
mistakes live — a rate is wrong far more often because of its denominator than
because a sum was miscounted — so the arithmetic is checked against numbers a
reader can verify by hand, and the gathering is checked against a real run.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.domain.enums import FindingStatus
from groundscribe.observability.metrics import (
    METRIC_NAMES,
    RunMetrics,
    RunObservations,
    collect_metrics,
    summarise,
)
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import WALK_USAGE, Walkthrough
from service_helpers import AUTHOR, Harness, build_harness


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


#: An installation that has done nothing at all. Every gathering test starts from
#: this and adds only the facts it is about, so a number that moved is a number
#: the test put there.
NOTHING = RunObservations()


# ----------------------------------------------------------------------
# What the surface must contain
# ----------------------------------------------------------------------


def test_every_metric_plan_14_names_is_on_the_surface() -> None:
    """plan/14 → the seventeen metrics, exposed.

    Named exhaustively rather than counted: a metric quietly dropped from the
    model should fail here by name, which is the only way a reader of this test
    can tell which one went missing.
    """
    assert METRIC_NAMES == (
        "stage_durations",
        "token_usage",
        "estimated_cost_usd",
        "retry_count",
        "validation_failures",
        "schema_repair_frequency",
        "score_change",
        "rewrite_count",
        "issue_decisions",
        "stagnation_frequency",
        "override_frequency",
        "question_response_rate",
        "model_fallback_frequency",
        "context_truncation_frequency",
        "tool_failure_frequency",
        "human_edit_distance",
        "final_approval_rate",
    )
    assert set(METRIC_NAMES) <= set(RunMetrics.model_fields)


def test_an_installation_that_has_done_nothing_reports_nothing_rather_than_zero() -> None:
    """plan/12's rule, carried forward: a rate with no denominator is unknown.

    The exception is a count. "No stage has run" is honestly zero executions;
    "0% of model calls needed repair" is a claim about calls that never happened.
    """
    metrics = summarise(NOTHING)

    assert metrics.schema_repair_frequency is None
    assert metrics.stagnation_frequency is None
    assert metrics.override_frequency is None
    assert metrics.question_response_rate is None
    assert metrics.model_fallback_frequency is None
    assert metrics.context_truncation_frequency is None
    assert metrics.tool_failure_frequency is None
    assert metrics.human_edit_distance is None
    assert metrics.final_approval_rate is None
    assert metrics.score_change is None
    assert metrics.estimated_cost_usd is None

    assert metrics.retry_count == 0
    assert metrics.rewrite_count == 0
    assert metrics.validation_failures == 0
    assert metrics.token_usage.total == 0
    assert metrics.stage_durations == ()


# ----------------------------------------------------------------------
# The arithmetic, on numbers a reader can check
# ----------------------------------------------------------------------


def test_stage_duration_is_reported_per_stage_rather_than_as_one_number() -> None:
    """A mean over every stage answers no question anybody has.

    "The pipeline takes 40 seconds" cannot be acted on; "the rewrite takes 30 of
    them" can. So the metric is a breakdown, ordered slowest first — which is the
    order the question is asked in.
    """
    metrics = summarise(
        replace(
            NOTHING,
            stage_runs=(
                ("generate_initial_draft", 4_000),
                ("rewrite_substantively", 10_000),
                ("rewrite_substantively", 20_000),
            ),
        )
    )

    slowest, next_slowest = metrics.stage_durations
    assert (slowest.stage, slowest.executions) == ("rewrite_substantively", 2)
    assert (slowest.total_ms, slowest.mean_ms) == (30_000, 15_000.0)
    assert (next_slowest.stage, next_slowest.total_ms) == ("generate_initial_draft", 4_000)


def test_token_usage_counts_the_attempts_that_failed_too() -> None:
    """A run that reported only its accepted calls would under-report exactly the
    runs that cost the most — the ones that needed repairing. The same reasoning
    the ``ModelInvocation`` row was given per-attempt usage for (phase 03)."""
    metrics = summarise(replace(NOTHING, input_tokens=1_200, output_tokens=800))

    assert (metrics.token_usage.input_tokens, metrics.token_usage.output_tokens) == (1_200, 800)
    assert metrics.token_usage.total == 2_000


def test_cost_is_the_sum_of_what_was_reported_and_unknown_otherwise() -> None:
    """Not every provider reports a cost, and "free" is a different claim from
    "unknown" — the distinction the nullable ``cost_usd`` column exists for."""
    assert summarise(replace(NOTHING, costs=(0.012, 0.003))).estimated_cost_usd == 0.015
    assert summarise(replace(NOTHING, model_calls=4)).estimated_cost_usd is None


def test_retries_are_counted_and_repairs_are_a_share_of_the_calls_made() -> None:
    """Retries are a count because a person asks "how many?"; repairs are a rate
    because the question is "how often does this model fail to conform?", which
    ten repairs in twelve calls and ten in a thousand answer differently."""
    metrics = summarise(replace(NOTHING, model_calls=8, retries=3, repairs=2, fallbacks=1))

    assert metrics.retry_count == 3
    assert metrics.schema_repair_frequency == 0.25
    assert metrics.model_fallback_frequency == 0.125


def test_the_score_change_is_the_distance_the_revision_loop_moved_the_article() -> None:
    """First score to last, signed: the loop is meant to improve the article, and
    a negative number is the thing worth seeing."""
    assert summarise(replace(NOTHING, scores=(61.0, 74.5))).score_change == 13.5
    assert summarise(replace(NOTHING, scores=(74.5, 61.0))).score_change == -13.5
    # One score is not a change. Reporting 0.0 would say the loop ran and
    # achieved nothing, which is a different and false claim.
    assert summarise(replace(NOTHING, scores=(74.5,))).score_change is None


def test_findings_are_counted_by_what_the_author_decided_about_them() -> None:
    """plan/14 names "accepted/rejected issues"; the row has five states and
    collapsing the other three would lose the ones phase 07 kept them for."""
    metrics = summarise(
        replace(
            NOTHING,
            issue_statuses=(
                FindingStatus.ACCEPTED,
                FindingStatus.ACCEPTED,
                FindingStatus.REJECTED,
                FindingStatus.EDITED,
                FindingStatus.SUPPRESSED,
                FindingStatus.PROPOSED,
            ),
        )
    )

    decisions = metrics.issue_decisions
    assert (decisions.accepted, decisions.rejected) == (2, 1)
    assert (decisions.edited, decisions.suppressed, decisions.proposed) == (1, 1, 1)


def test_the_frequencies_each_count_the_thing_they_are_a_share_of() -> None:
    """Every denominator here is a choice, so each is pinned separately.

    Stagnation is per run, because a run stalls; overrides are per human
    intervention, because the question is how often a person had to overrule the
    system rather than how often they touched it; truncation is per context item,
    because that is what was dropped.
    """
    metrics = summarise(
        replace(
            NOTHING,
            runs=4,
            stalled_runs=1,
            interventions=10,
            overrides=2,
            context_items=50,
            truncated_context_items=5,
            tool_calls=8,
            tool_failures=2,
            surfaced_questions=4,
            answered_questions=3,
        )
    )

    assert metrics.stagnation_frequency == 0.25
    assert metrics.override_frequency == 0.2
    assert metrics.context_truncation_frequency == 0.1
    assert metrics.tool_failure_frequency == 0.25
    assert metrics.question_response_rate == 0.75


def test_the_final_approval_rate_is_measured_against_the_gate_it_reached() -> None:
    """Against runs that arrived at the human gate, never against runs started.

    A project abandoned before it was ever finished is not a rejection, and
    counting it as one would make the number say the pipeline produces articles
    people refuse rather than articles people have not looked at yet.
    """
    metrics = summarise(replace(NOTHING, runs=9, approval_gates=4, final_approvals=3))

    assert metrics.final_approval_rate == 0.75


def test_the_human_edit_distance_is_the_share_of_the_article_a_person_rewrote() -> None:
    """Averaged as a ratio rather than a character count: a corpus of articles of
    different lengths would otherwise average out as the longest one (phase 12)."""
    assert summarise(replace(NOTHING, edit_ratios=(0.1, 0.3))).human_edit_distance == 0.2


# ----------------------------------------------------------------------
# The gathering, against a run that actually happened
# ----------------------------------------------------------------------


async def test_a_real_run_reports_what_it_spent_and_what_it_did(
    walk: Walkthrough, client: TestClient
) -> None:
    """Every number here is checked against something else the run recorded,
    rather than against a constant this test would have to be edited to keep.

    That is the point of running it over a real walk at all: a collector that
    read the wrong column would still return plausible numbers, and only a
    cross-check against the invocations themselves catches it.
    """
    await walk.to_approval()
    session = walk.session

    metrics = collect_metrics(session, project_id=walk.project_id)
    calls = len(walk.harness.client.received_requests)

    assert calls > 0, "the walk should have called a model"
    assert metrics.token_usage.input_tokens == WALK_USAGE.input_tokens * calls
    assert metrics.token_usage.output_tokens == WALK_USAGE.output_tokens * calls
    assert metrics.estimated_cost_usd == pytest.approx(float(WALK_USAGE.cost_usd or 0.0) * calls)

    # Every stage the walk ran appears, and the durations are real spans rather
    # than zeros — the recorder's clock advances, so a collector subtracting the
    # wrong pair of timestamps would show nothing.
    stages = {duration.stage for duration in metrics.stage_durations}
    assert {"generate_initial_draft", "rewrite_substantively", "score_article"} <= stages
    assert all(duration.total_ms > 0 for duration in metrics.stage_durations)

    # What the walk did, counted: one rewrite round, one validation, and it
    # passed.
    assert metrics.rewrite_count == 1
    assert metrics.validation_failures == 0

    # The reviewer's findings are still *proposed*, and that is the walk being
    # reported accurately rather than the metric being wrong: this run planned a
    # revision straight from the review without going through the acceptance
    # stage, so nobody has decided about them. A surface that folded "nobody has
    # looked" into "rejected" would lose the distinction phase 07 built five
    # finding states to keep.
    assert metrics.issue_decisions.proposed > 0
    assert metrics.issue_decisions.accepted == metrics.issue_decisions.rejected == 0

    # Nothing in the happy path repairs, falls back, truncates or calls a tool,
    # so those report the honest zero-of-many rather than the dishonest None.
    assert metrics.schema_repair_frequency == 0.0
    assert metrics.model_fallback_frequency == 0.0
    assert metrics.tool_failure_frequency is None
    assert metrics.retry_count == 0

    # The walk stops at the gate without deciding, so the rate has a denominator
    # and no numerator — which is not the same as having no denominator.
    assert metrics.final_approval_rate == 0.0

    approved = client.post(f"/articles/{walk.article_id}/approve", json={"actor_id": AUTHOR})
    assert approved.status_code == 200, approved.text
    assert collect_metrics(session, project_id=walk.project_id).final_approval_rate == 1.0


async def test_a_question_the_author_answered_is_a_question_answered(
    walk: Walkthrough,
) -> None:
    """The response rate counts the questions actually put to a person.

    Not every generated gap: phase 06 stores the ones the policy suppressed too,
    and dividing by those would report an author ignoring questions they were
    never shown.
    """
    await walk.open_project()
    await walk.extract(blocking=True)

    parked = collect_metrics(walk.session, project_id=walk.project_id)
    assert parked.question_response_rate == 0.0

    await walk.answer()

    assert collect_metrics(walk.session, project_id=walk.project_id).question_response_rate == 1.0


async def test_metrics_are_scoped_to_one_project_and_summable_over_all_of_them(
    walk: Walkthrough, client: TestClient, harness: Harness
) -> None:
    """An installation-wide figure is what an operator watches; a per-project one
    is what they drill into. Both, and the first must be the sum of the second —
    a scope filter that leaked would show the same total at every level."""
    await walk.open_project()
    await walk.extract()
    first = walk.project_id

    second_walk = Walkthrough(client, harness)
    await second_walk.open_project()
    await second_walk.extract()

    one = collect_metrics(walk.session, project_id=first)
    other = collect_metrics(walk.session, project_id=second_walk.project_id)
    everything = collect_metrics(walk.session)

    assert one.token_usage.total > 0
    assert everything.token_usage.total == one.token_usage.total + other.token_usage.total
    assert everything.runs == one.runs + other.runs == 2
