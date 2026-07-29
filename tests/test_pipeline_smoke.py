"""The whole pipeline, once, end to end (phase 14).

plan/14 → *End-to-end smoke run: a full pipeline happy-path on the deterministic
fake LLM — ingest → extract → gap questions → architecture → approve → brief →
draft → review → revision plan → rewrite → voice → score → validate → human
approval → export — asserting a complete, inspectable provenance trace exists at
the end.*

Every other test in this repository holds something still. The stage suites
construct one stage and hand it the document its predecessor returned; phase 09's
end-to-end tests drive the API with a worker behind it but stop at the human
gate. This one holds nothing still and stops nowhere: it starts from pasted text
and ends with a rendered Markdown file, over HTTP, with a worker draining the
queue between commands.

Which makes it the only test in the suite that can fail for the reason a *system*
fails — a seam that works perfectly from both sides. It is deliberately not a
second copy of anyone else's assertions. What it checks is the property no single
phase could:

**Every artefact this run produced names the execution that produced it, and
every stage the plan lists ran.** That is the promise plan/00 makes in one
sentence — *every artefact references a creating execution* — and until the whole
pipeline has run once, nothing has been in a position to check it end to end.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.domain import models as domain_models
from groundscribe.provenance import models as provenance_models
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
from service_helpers import Harness, build_harness

#: Every stage plan/14's happy path names, as the pipeline names them. Written
#: out from the plan rather than read back from the run: a list gathered from
#: what happened would agree with itself no matter which stage was skipped.
REQUIRED_STAGES: tuple[str, ...] = (
    "ingest_source",
    "extract_source_truth",
    "generate_gap_questions",
    "propose_content_architecture",
    "generate_article_brief",
    "generate_initial_draft",
    "review_substantively",
    "create_revision_plan",
    "rewrite_substantively",
    "align_voice",
    "score_article",
    "validate_article",
)


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(client: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(client, harness)


async def test_a_project_runs_from_pasted_text_to_a_published_file(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/14 → the happy path, all of it, ending in an export.

    Read as a script. Each step is a command a person issues; between them a
    worker runs, which is the only reason this proves anything the in-process
    stage tests do not: a worker rebuilds every input from the row the previous
    stage wrote, so a version whose stored shape does not survive the round trip
    fails here and nowhere else.
    """
    await walk.to_approval()

    parked = client.get(f"/projects/{walk.project_id}").json()
    assert parked["state"] == "human_approval_required"
    assert "approve_final" in parked["available_actions"]

    published = await walk.approve()
    assert published["state"] == "completed"

    exported = walk.export()

    assert exported["format"] == "markdown"
    assert exported["media_type"] == "text/markdown"
    assert exported["version_id"] == walk.validated_snapshot()
    assert exported["content"].startswith("# ")


async def test_the_exported_file_is_the_version_that_passed_validation(
    walk: Walkthrough,
) -> None:
    """plan/14 → *export uses the version that passed validation and matches the
    recorded content hash* (the phase-13 rule, checked against a real run).

    The hash is recomputed here from the stored bytes rather than read off the
    row, because the row is what would be wrong if anything were: a check that
    compared the recorded hash to itself would pass on a corrupted store.
    """
    await walk.to_approval()
    await walk.approve()

    snapshot_id = walk.validated_snapshot()
    snapshot = walk.session.get(domain_models.ArtifactSnapshot, snapshot_id)
    assert snapshot is not None
    stored = walk.harness.runtime.snapshots.read(snapshot)

    exported = walk.export()

    assert exported["content_hash"] == snapshot.content_hash
    assert hashlib.sha256(stored).hexdigest() == snapshot.content_hash
    # The prose in the file is the prose in the stored version, not a re-render
    # of something adjacent to it.
    assert exported["content"].count("\n") > 0
    assert '"body"' not in exported["content"], "the export is prose, not the stored document"


async def test_every_stage_the_plan_names_left_an_execution_behind(
    walk: Walkthrough,
) -> None:
    """plan/14 → *every stage has a StageExecution*.

    Asserted against the plan's list rather than against whatever ran, and by
    *name*, so a stage silently skipped fails here saying which one.
    """
    await walk.to_approval()
    await walk.approve()

    executions = list(
        walk.session.scalars(
            select(provenance_models.StageExecution).order_by(
                provenance_models.StageExecution.ordinal
            )
        )
    )
    ran = {execution.stage for execution in executions}

    assert set(REQUIRED_STAGES) <= ran, f"never ran: {sorted(set(REQUIRED_STAGES) - ran)}"
    # And none of them was left open. An execution still `running` after the run
    # completed is an orphan, which is a real failure mode phase 09 detects — it
    # should not be the state a *successful* run finishes in.
    assert [
        execution.stage
        for execution in executions
        if execution.status is ExecutionStatus.RUNNING and execution.stage in set(REQUIRED_STAGES)
    ] == []


async def test_every_artefact_names_the_execution_that_produced_it(
    walk: Walkthrough,
) -> None:
    """plan/00 → *every artefact references a creating execution*, and plan/14 →
    *a complete provenance trace exists at the end*.

    An invariant the state machine enforces one transition at a time (phase 05)
    and a provenance test checks one record at a time (phase 03). Neither was in
    a position to check it over a whole run, because until now no test had run
    one. This is that check: every snapshot the pipeline stored, and the
    execution behind it, with no exceptions carved out.
    """
    await walk.to_approval()
    await walk.approve()

    snapshots = list(walk.session.scalars(select(domain_models.ArtifactSnapshot)))
    assert snapshots, "a completed run that stored nothing is not a completed run"

    unattributed = [snapshot.id for snapshot in snapshots if not snapshot.created_by_execution_id]
    assert unattributed == []

    # Every id resolves. A dangling reference is worse than a missing one: it
    # reads as attributed until somebody follows it.
    executions = {
        execution.id for execution in walk.session.scalars(select(provenance_models.StageExecution))
    }
    dangling = [
        snapshot.id for snapshot in snapshots if snapshot.created_by_execution_id not in executions
    ]
    assert dangling == []


async def test_the_finished_run_can_be_read_back_as_a_timeline(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/14 → *a complete, inspectable provenance trace*. Inspectable means
    over the API, by a person who was not here when it ran.

    The trace view is what phase 11 renders; asserting it against a *finished*
    run is what says the projections survive the whole pipeline rather than the
    slice each of them was built against.
    """
    await walk.to_approval()
    await walk.approve()

    trace = client.get(f"/projects/{walk.project_id}/trace").json()
    stages = [execution["stage"] for execution in trace["executions"]]

    assert set(REQUIRED_STAGES) <= set(stages)
    # Every model call the run made is reachable from the execution that made
    # it, which is the path from "why does the article say this" to the prompt.
    inspected = [
        client.get(f"/executions/{execution['id']}/inspect").json()
        for execution in trace["executions"]
        if execution["stage"] == "generate_initial_draft"
    ]
    assert inspected and inspected[0]["invocations"], "the draft has no model call behind it"
