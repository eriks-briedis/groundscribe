"""Every way this system fails, and what survives each one (phase 14).

plan/14 → *Failure-handling verification: cancelled stages, worker crashes,
timeouts, provider refusals, invalid output, partial streaming, tool failures,
validation failures, user-aborted executions, superseded jobs, orphaned
executions — all preserve partial data and remain inspectable.*

Phases 03 and 09 proved each guarantee against the component that makes it. This
file re-asks the question of the *assembled* system, driven the way a person
drives it: over HTTP, with a worker between commands, on the fake provider.
That distinction is the reason the file exists — a stage that preserves its
partial trace and a worker that preserves it too can still lose it between them,
and no component test is positioned to notice.

One property is asserted after every failure, because it is the promise:

**The failure is recorded, and everything the run had already done is still
there.** Not merely "no crash" — the records the stage wrote before it failed are
the explanation of the failure, and a system that rolled them back would destroy
the evidence precisely when it is needed. Phase 03 stated it (*write, never roll
back*); this checks it eleven times, once per way of failing.

Failure classes deliberately not invented: this system has no tool registry
(phase 04's non-goal), so a "tool failure" here is a model asking for a tool
nothing can run — which is what actually happens — and "partial streaming" is an
SSE consumer leaving mid-stream, which is the only streaming the assembled system
does. Testing a fictional version of either would prove nothing about this
software.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.domain import models as domain_models
from groundscribe.jobs.enums import JobStatus, JobType
from groundscribe.llm.fake import InjectableFailure
from groundscribe.provenance import models as provenance_models
from groundscribe.provenance.enums import ExecutionStatus, InvocationOutcome
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
from service_helpers import AUTHOR, Harness, build_harness

EXTRACTION = "extract_source_truth"


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


def executions(session: Session, stage: str) -> list[provenance_models.StageExecution]:
    return list(
        session.scalars(
            select(provenance_models.StageExecution)
            .where(provenance_models.StageExecution.stage == stage)
            .order_by(provenance_models.StageExecution.ordinal)
        )
    )


def invocations(session: Session) -> list[provenance_models.ModelInvocation]:
    return list(
        session.scalars(
            select(provenance_models.ModelInvocation).order_by(
                provenance_models.ModelInvocation.attempt_ordinal
            )
        )
    )


async def failing_extraction(walk: Walkthrough, injected: InjectableFailure) -> dict[str, Any]:
    """Start a project and drive extraction into ``injected``, however far it gets.

    Enough attempts are scripted for the repair ladder to exhaust itself: a test
    that scripted one would be asserting about the ladder running out of script,
    not about the provider failing.
    """
    await walk.open_project()
    for _ in range(6):
        walk.harness.client.script_failure(EXTRACTION, injected)

    response = walk.client.post(f"/projects/{walk.project_id}/source-model/extract", json={})
    assert response.status_code < 300, response.text
    jobs = await walk.harness.drain()
    assert jobs and jobs[0].status is JobStatus.FAILED, "the injected failure should fail the job"
    body: dict[str, Any] = walk.client.get(f"/projects/{walk.project_id}").json()
    return body


# ----------------------------------------------------------------------
# The provider misbehaves
# ----------------------------------------------------------------------


async def test_a_timeout_keeps_every_attempt_that_timed_out(walk: Walkthrough) -> None:
    """plan/14 → *timeouts* preserve partial data.

    The attempts are the evidence. A run that recorded only its last failure
    could not distinguish "the provider was slow once" from "the provider has
    been unreachable for an hour", and those are different problems.
    """
    await failing_extraction(walk, InjectableFailure.TIMEOUT)

    attempts = invocations(walk.session)
    assert len(attempts) > 1, "the ladder should have retried a timeout"
    assert all(attempt.outcome is InvocationOutcome.TIMEOUT for attempt in attempts)
    assert all(attempt.error_message for attempt in attempts)

    (execution,) = executions(walk.session, EXTRACTION)
    assert execution.status is ExecutionStatus.FAILED
    assert execution.error_type and execution.error_message


async def test_a_provider_refusal_is_kept_with_the_reason_it_gave(walk: Walkthrough) -> None:
    """plan/14 → *provider refusals*.

    A refusal is a *response*, not an error, and the reason is the only thing
    that tells an author whether to rephrase the source or stop.
    """
    await walk.open_project()
    walk.harness.client.script_refusal(EXTRACTION, "I can't help with that.")

    walk.client.post(f"/projects/{walk.project_id}/source-model/extract", json={})
    jobs = await walk.harness.drain()

    assert jobs[0].status is JobStatus.FAILED
    (attempt,) = invocations(walk.session)
    assert attempt.outcome is InvocationOutcome.REFUSED
    assert attempt.error_message == "I can't help with that."
    assert executions(walk.session, EXTRACTION)[0].status is ExecutionStatus.FAILED


async def test_invalid_output_survives_verbatim_next_to_its_failed_repair(
    walk: Walkthrough,
) -> None:
    """plan/14 → *invalid output*.

    The unparseable body is kept as it arrived. Storing only "invalid JSON"
    would leave nobody able to say *what* the model actually sent, which is the
    first question anybody asks.
    """
    await walk.open_project()
    for _ in range(6):
        walk.harness.client.script_text(EXTRACTION, "{not json at all")

    walk.client.post(f"/projects/{walk.project_id}/source-model/extract", json={})
    await walk.harness.drain()

    attempts = invocations(walk.session)
    assert len(attempts) > 1, "an unparseable body should have been retried"
    assert all(attempt.outcome is InvocationOutcome.INVALID_JSON for attempt in attempts)

    stored = [
        json.loads(walk.harness.runtime.snapshots.read(attempt.raw_response_snapshot))
        for attempt in attempts
        if attempt.raw_response_snapshot is not None
    ]
    assert stored and all(body == "{not json at all" for body in stored)


async def test_a_tool_the_pipeline_cannot_run_is_recorded_before_it_fails(
    walk: Walkthrough,
) -> None:
    """plan/14 → *tool failures*.

    There is no tool registry (phase 04 non-goal), so a model asking for a tool
    is a stage that cannot proceed. What matters is that the *request* is stored
    before the failure: a tool call nobody recorded is a decision the model made
    that the trace cannot explain.
    """
    await walk.open_project()
    walk.harness.client.script_tool_call(EXTRACTION, name="fetch_url", arguments={"url": "/x"})

    walk.client.post(f"/projects/{walk.project_id}/source-model/extract", json={})
    jobs = await walk.harness.drain()

    assert jobs[0].status is JobStatus.FAILED
    tools = list(walk.session.scalars(select(provenance_models.ToolInvocation)))
    assert [tool.tool_name for tool in tools] == ["fetch_url"]
    assert tools[0].raw_args == {"url": "/x"}
    # Linked to the call that asked for it, which is what makes it explicable.
    assert tools[0].model_invocation_id == invocations(walk.session)[-1].id


# ----------------------------------------------------------------------
# The pipeline itself refuses
# ----------------------------------------------------------------------


async def test_a_failed_validation_keeps_its_report_and_routes_for_revision(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/14 → *validation failures* preserve inspectable data.

    Driven by asking for an article four times longer than the one that gets
    written — the one lever that fails a *deterministic* check without corrupting
    the article the rest of the walk depends on. A failing score would have been
    easier and would have proved something else: it routes the run before
    validation is reached, so nothing would have been validated at all.

    Validation is deterministic, so its report *is* the reason. A run sent back
    for revision without one would be a run nobody could argue with.
    """
    await walk.open_project(target_words=1800)
    await walk.extract()
    await walk.architecture()
    await walk.brief(target_words=1800)
    await walk.draft()
    await walk.review(clean=True)
    await walk.align_voice()
    await walk.score()
    state = await walk.validate()

    assert state["state"] == "revision_required", "a failed validation must not reach the gate"

    (report,) = list(walk.session.scalars(select(domain_models.ValidationReport)))
    assert not report.passed
    assert report.snapshot_id, "the report is stored, not merely a boolean on a row"

    # The reason survives in reviewable form, naming the check that refused.
    (execution,) = executions(walk.session, "validate_article")
    (decision,) = execution.decision_records
    assert (decision.decision_type, decision.outcome) == ("final_validation", "failed")
    assert any("1800" in detail for detail in decision.inputs["findings"])

    # And the article it refused is still there to be revised, not discarded.
    versions = list(walk.session.scalars(select(domain_models.ArticleVersion)))
    assert versions and all(version.snapshot_id for version in versions)
    workspace = client.get(f"/articles/{walk.article_id}/workspace").json()
    assert workspace["validation"] is not None


async def test_cancelling_a_run_keeps_everything_it_had_already_done(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/14 → *cancelled stages* and *user-aborted executions*.

    Two entries in the plan's list and one act by a person, distinguished by
    what they leave behind: the run is ``cancelled``, never ``failed``, and every
    artefact it produced before the abort is still addressable. The system giving
    up and a person stopping the work are different facts (phase 03), and the
    difference has to survive to the assembled system or it is not a fact anybody
    can read.
    """
    await walk.open_project()
    await walk.extract()
    before = list(walk.session.scalars(select(domain_models.ArtifactSnapshot)))
    assert before, "extraction should have stored something to lose"

    cancelled = client.post(f"/projects/{walk.project_id}/cancel", json={"actor_id": AUTHOR}).json()

    assert cancelled["state"] == "cancelled"
    after = list(walk.session.scalars(select(domain_models.ArtifactSnapshot)))
    assert {snapshot.id for snapshot in before} <= {snapshot.id for snapshot in after}
    # And the extraction that produced them still reads as having succeeded: a
    # cancellation is not a retrospective failure of the work already done.
    assert executions(walk.session, EXTRACTION)[0].status is ExecutionStatus.SUCCEEDED
    # Inspectable afterwards, which is the other half of "preserved".
    trace = client.get(f"/projects/{walk.project_id}/trace").json()
    assert EXTRACTION in {execution["stage"] for execution in trace["executions"]}


# ----------------------------------------------------------------------
# The machinery around it
# ----------------------------------------------------------------------


async def test_a_worker_killed_mid_stage_leaves_the_job_and_its_stage_findable(
    walk: Walkthrough,
) -> None:
    """plan/14 → *worker crashes* and *orphaned executions*.

    A killed process runs no cleanup, so nothing marks anything: the guarantee
    has to come from what was already written. Simulated with a ``BaseException``
    the worker deliberately does not catch, because that is the honest analogue.

    Recovery *reports* the orphan rather than repairing it. What should happen to
    a stage that stopped halfway is a decision with evidence behind it, and a
    worker quietly closing it off would destroy that evidence on its way past.
    """

    class Killed(BaseException):
        """Stands in for the process going away."""

    await walk.open_project()
    walk.script("extract_source_truth", walk.source_model())
    walk.script("generate_gap_questions", {"schema_version": 1, "gaps": []})
    walk.client.post(f"/projects/{walk.project_id}/source-model/extract", json={})

    worker = walk.harness.worker
    original = worker._handlers[JobType.EXTRACT_SOURCE_MODEL]

    async def killed_midway(request: Any) -> Any:
        run = walk.session.get(provenance_models.PipelineRun, request.job.pipeline_run_id)
        assert run is not None
        request.opened(
            walk.harness.runtime.recorder.start_stage(run, stage=EXTRACTION, impl_version="1.1")
        )
        raise Killed("the process went away")

    worker._handlers[JobType.EXTRACT_SOURCE_MODEL] = killed_midway
    try:
        with pytest.raises(Killed):
            await worker.run_once()
    finally:
        worker._handlers[JobType.EXTRACT_SOURCE_MODEL] = original

    (execution,) = executions(walk.session, EXTRACTION)
    assert execution.status is ExecutionStatus.RUNNING, "nothing marked it: nothing could"

    # A zero lease is "every running job has stopped reporting", which is what a
    # real recovery decides by comparing heartbeats to a clock. Stated as a lease
    # rather than by moving a clock the assembled system does not own.
    recovered = worker.recover(lease=timedelta(0))
    assert execution.id in {orphan.id for orphan in recovered.orphaned}
    assert execution.status is ExecutionStatus.RUNNING, "recovery reports, it does not repair"


async def test_a_superseded_job_keeps_its_row_and_names_its_replacement(
    walk: Walkthrough,
) -> None:
    """plan/14 → *superseded jobs*.

    Superseding is not deleting. The queue keeps the abandoned row and links it
    to what replaced it, so "why did nothing happen when I pressed that" has an
    answer rather than an absence.
    """
    await walk.open_project()
    walk.script("extract_source_truth", walk.source_model())
    walk.script("generate_gap_questions", {"schema_version": 1, "gaps": []})

    queue = walk.harness.runtime.queue
    run = walk.session.scalars(select(provenance_models.PipelineRun)).one()
    waiting = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run)
    replacement = queue.enqueue(job_type=JobType.EXTRACT_SOURCE_MODEL, run=run, supersede=True)

    assert waiting.status is JobStatus.SUPERSEDED
    assert waiting.superseded_by_id == replacement.id
    assert walk.session.get(type(waiting), waiting.id) is not None, "the row is kept"


async def test_a_client_leaving_a_stream_early_costs_the_run_nothing(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/14 → *partial streaming*.

    The only streaming the assembled system does is SSE progress, and the failure
    is a browser closed mid-job. The stream is assembled from the trace rather
    than published alongside it (phase 09), so a consumer leaving cannot cost
    anything — and this is the test that says so rather than assuming it. The
    same stream reopened afterwards replays the whole run.
    """
    await walk.open_project()
    walk.script("extract_source_truth", walk.source_model())
    walk.script("generate_gap_questions", {"schema_version": 1, "gaps": []})
    queued = walk.client.post(f"/projects/{walk.project_id}/source-model/extract", json={}).json()
    job_id = queued["job"]["id"]

    for job in await walk.harness.drain():
        assert job.status is JobStatus.SUCCEEDED, job.error_message

    # A consumer that reads one frame and goes away, which is what a closed tab
    # is. The frames it never read are still in the trace, because they were
    # never anywhere else.
    with client.stream("GET", f"/jobs/{job_id}/events") as stream:
        first = next(iter(stream.iter_lines()))
    assert first.startswith("event:")

    with client.stream("GET", f"/jobs/{job_id}/events") as stream:
        frames = "".join(stream.iter_text())

    assert "event: job.status" in frames
    assert '"status": "succeeded"' in frames
    assert "stage.started" in frames
    assert executions(walk.session, EXTRACTION)[0].status is ExecutionStatus.SUCCEEDED
