"""Running a baseline against a candidate, over a corpus (phase 12).

plan/12 → *ExperimentRun: baseline vs one+ candidate configurations over an
evaluation dataset; per-example results, aggregate comparison, human-preference
decisions.*

An arm is a fork, and that is the whole mechanism: plan/12 calls fork "the
primary improvement mechanism", so an experiment that invented a second way to
vary a configuration would have two definitions of what a candidate is, and they
would drift.

Three things the tests hold in place.

**The baseline runs too.** It would be cheaper to reuse the numbers the original
execution already recorded, and it would be wrong: hosted models are
nondeterministic, so a candidate compared against a stored figure is being
compared against a different draw as well as a different configuration.

**A result exists before it succeeds.** Rows are written when the work is
queued, not when it finishes, because an experiment that only recorded what
completed could not tell "not run yet" from "ran and produced nothing" — and the
second is a finding about the candidate.

**Two arms are not the same request twice.** The replay endpoint deduplicates by
source execution, which is right for a person clicking twice and catastrophic
here: it would silently collapse a two-arm experiment into one arm and report
that the configurations agreed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.experiments.datasets import DatasetBuilder
from groundscribe.experiments.models import EvaluationDataset
from groundscribe.experiments.runs import ArmSpec, ExperimentRunner, UnknownArm
from groundscribe.experiments.variables import ForkVariables
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
from service_helpers import AUTHOR, Harness, build_harness

VOICE = "align_voice"
CANDIDATE_MODEL = "llama3.1:8b-instruct"


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


@pytest.fixture
def runner(harness: Harness) -> ExperimentRunner:
    return ExperimentRunner(
        harness.runtime.session,
        queue=harness.runtime.queue,
        snapshots=harness.runtime.snapshots,
        clock=harness.runtime.clock,
    )


async def corpus(walk: Walkthrough) -> EvaluationDataset:
    """One approved article, as a dataset of one entry."""
    await walk.to_approval()
    await walk.command("POST", f"/articles/{walk.article_id}/approve", json={"actor_id": AUTHOR})
    return DatasetBuilder(walk.session, snapshots=walk.harness.runtime.snapshots).build(
        name="one article", created_by=AUTHOR
    )


def two_arms() -> tuple[ArmSpec, ...]:
    return (
        ArmSpec(label="baseline", baseline=True),
        ArmSpec(
            label="the small model",
            variables=ForkVariables(model=CANDIDATE_MODEL),
        ),
    )


async def run_both_arms(walk: Walkthrough, runner: ExperimentRunner, dataset: EvaluationDataset):
    """Open the experiment, queue every arm, and let the worker finish them."""
    experiment = runner.create(
        name="cheaper model?", dataset=dataset, created_by=AUTHOR, arms=two_arms()
    )
    # One scripted answer per arm, because each arm really runs the stage.
    for _ in experiment.arms:
        walk.script(VOICE, walk.voice_pass(snapshot_id=walk.approved_input()))
    runner.start(experiment)
    await walk.harness.drain()
    runner.collect(experiment)
    return experiment


async def test_an_experiment_with_no_baseline_is_refused(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """A comparison needs something to compare against.

    Refused at creation rather than at aggregation, where the table would simply
    come out with no row marked as the thing everything else is measured from —
    and be read as though the first arm were it.
    """
    dataset = await corpus(walk)

    with pytest.raises(ValueError, match="baseline"):
        runner.create(
            name="no control",
            dataset=dataset,
            created_by=AUTHOR,
            arms=(ArmSpec(label="candidate", variables=ForkVariables(temperature=0.9)),),
        )


async def test_starting_an_experiment_queues_one_run_per_arm_per_example(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """Including the baseline, which is re-run rather than remembered.

    The result rows exist as soon as the work is queued: an experiment that only
    recorded what finished could not tell "not run yet" from "ran and produced
    nothing", and the second is a finding.
    """
    dataset = await corpus(walk)
    experiment = runner.create(
        name="cheaper model?", dataset=dataset, created_by=AUTHOR, arms=two_arms()
    )

    results = runner.start(experiment)

    assert len(results) == 2 * len(dataset.entries)
    assert {result.arm.label for result in results} == {"baseline", "the small model"}
    assert all(result.status is ExecutionStatus.PENDING for result in results)
    assert all(result.job_id for result in results)
    assert all(result.stage_execution_id is None for result in results)


async def test_two_arms_over_one_example_are_two_runs(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """The deduplication that is right for a person clicking twice is wrong here.

    Left alone, both arms would fork the same execution under the same key, the
    queue would hand back the first job for the second arm, and the experiment
    would report that two configurations produced identical output.
    """
    dataset = await corpus(walk)

    experiment = await run_both_arms(walk, runner, dataset)

    executions = [result.stage_execution_id for result in experiment.results]
    assert all(executions)
    assert len(set(executions)) == 2


async def test_a_finished_arm_records_the_execution_it_produced(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """Which is what makes a per-example result inspectable.

    plan/12's whole claim is that a comparison can be traced back to the runs
    behind it, so a result carrying only a number would be a number nobody can
    argue with.
    """
    dataset = await corpus(walk)

    experiment = await run_both_arms(walk, runner, dataset)

    for result in experiment.results:
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.stage_execution_id
        assert result.error_message is None


async def test_the_candidate_really_ran_under_its_own_variables(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """Otherwise the experiment measures nothing and says the change did nothing.

    Asserted against the model recorded on the invocation, not against what the
    arm declared: the arm's declaration is the input to the question.
    """
    dataset = await corpus(walk)

    experiment = await run_both_arms(walk, runner, dataset)

    models_used = {
        result.arm.label: [call.model for call in result.stage_execution.model_invocations]
        for result in experiment.results
        if result.stage_execution is not None
    }
    assert models_used["the small model"] == [CANDIDATE_MODEL]
    assert CANDIDATE_MODEL not in models_used["baseline"]


async def test_a_person_prefers_one_arm_on_one_example(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """plan/12 → *human-preference decisions*.

    The metric no rubric stands in for, and the only evidence in an experiment
    that did not come from the system marking its own work.
    """
    dataset = await corpus(walk)
    experiment = await run_both_arms(walk, runner, dataset)
    (entry,) = dataset.entries
    candidate = next(arm for arm in experiment.arms if not arm.baseline)

    preference = runner.prefer(
        experiment, entry=entry, arm=candidate, decided_by=AUTHOR, reason="tighter opening"
    )

    assert preference.preferred_arm_id == candidate.id
    assert preference.decided_by == AUTHOR
    assert preference.reason == "tighter opening"


async def test_preferring_an_arm_from_another_experiment_is_refused(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """A judgement filed against the wrong comparison is worse than none.

    It would count toward a preference rate for an arm nobody was shown, in an
    experiment nobody was asked about.
    """
    dataset = await corpus(walk)
    experiment = await run_both_arms(walk, runner, dataset)
    elsewhere = runner.create(name="another", dataset=dataset, created_by=AUTHOR, arms=two_arms())
    (entry,) = dataset.entries

    with pytest.raises(UnknownArm):
        runner.prefer(experiment, entry=entry, arm=elsewhere.arms[0], decided_by=AUTHOR)


async def test_the_comparison_reports_one_row_per_arm(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """plan/12 → *per-example results, aggregate comparison*.

    Both, from the same rows. An aggregate a reader cannot open into the
    examples behind it is a summary they have to take on trust, which is the
    thing this whole phase exists to avoid.
    """
    dataset = await corpus(walk)
    experiment = await run_both_arms(walk, runner, dataset)
    (entry,) = dataset.entries
    candidate = next(arm for arm in experiment.arms if not arm.baseline)
    runner.prefer(experiment, entry=entry, arm=candidate, decided_by=AUTHOR)

    comparison = runner.compare(experiment)

    assert [row.label for row in comparison] == ["baseline", "the small model"]
    assert [row.baseline for row in comparison] == [True, False]
    assert all(row.examples == 1 and row.completed == 1 for row in comparison)
    assert comparison[0].human_preference == 0.0
    assert comparison[1].human_preference == 1.0


async def test_the_comparison_measures_each_arm_against_the_approved_article(
    walk: Walkthrough, runner: ExperimentRunner
) -> None:
    """The manual edit distance, in the only place it has a reference to use.

    plan/12 defines it as the difference between what the pipeline proposed and
    what the author approved. Inside an experiment the approved version is the
    dataset entry, which is exactly what makes the corpus worth keeping.
    """
    dataset = await corpus(walk)
    experiment = await run_both_arms(walk, runner, dataset)

    comparison = runner.compare(experiment)

    assert all(row.manual_edit_distance is not None for row in comparison)
    assert all(row.total_cost_usd is not None for row in comparison)
    assert all(row.mean_latency_ms is not None for row in comparison)
