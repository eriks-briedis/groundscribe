"""The command API (phase 09).

Spec (plan/09):

- *API command → transition*: each command endpoint drives the correct state
  transition and returns updated state + ``available_actions``.
- *Async enqueue*: long-running commands enqueue a job and return immediately,
  with no model call inside the request.
- *SSE*: progress events stream for a running job.
- Risk: *API embedding workflow rules — forbidden*.

The routes are thin by design, so these tests are mostly about the seam: that
every command reaches the service, that the workflow's own refusals surface as
the right status codes rather than as 500s, and that nothing in the request
touches a model. What each command *means* is tested against the service and the
stages; re-asserting it here would test FastAPI.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from golden import golden_text
from groundscribe.api.app import create_app
from groundscribe.app import bootstrap
from groundscribe.app.runtime import Runtime
from groundscribe.db import Base
from groundscribe.storage.snapshot_store import SnapshotStore
from service_helpers import AUTHOR, Harness, build_harness
from stage_helpers import DEFAULT_CONSTRAINTS
from test_services import script_extraction


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    """A client over the rolled-back session the harness already holds.

    The app is built around a runtime *factory*; handing it one that always
    yields this test's runtime is what lets HTTP requests and the worker in the
    same test see each other's rows.
    """
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


def create_project(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/projects",
        json={
            "title": "Read-through caching",
            "author_id": AUTHOR,
            "constraints": DEFAULT_CONSTRAINTS.model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def with_source(client: TestClient) -> str:
    project = create_project(client)
    project_id = str(project["project_id"])
    response = client.post(
        f"/projects/{project_id}/sources",
        json={
            "title": "Read-through caching for the render pipeline",
            "text": golden_text("source.md"),
            "source_format": "markdown",
        },
    )
    assert response.status_code == 200, response.text
    return project_id


# ----------------------------------------------------------------------
# Commands, state and actions
# ----------------------------------------------------------------------


def test_creating_a_project_returns_its_state_and_what_may_be_done_to_it(
    client: TestClient,
) -> None:
    """plan/09 → every command response carries ``available_actions``."""
    body = create_project(client)

    assert body["state"] == "source_ingested"
    assert "extract_source_model" in body["available_actions"]
    assert body["job"] is None


def test_a_long_running_command_returns_a_job_and_calls_no_model(
    client: TestClient, harness: Harness
) -> None:
    """plan/09 → *no model call inside the request*.

    The assertion that matters is the last one. Everything else here would still
    pass if the route quietly awaited the stage.
    """
    project_id = with_source(client)

    response = client.post(f"/projects/{project_id}/source-model/extract", json={})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "source_model_extracting"
    assert body["job"]["status"] == "pending"
    assert body["job"]["job_type"] == "extract_source_model"
    assert harness.client.received_requests == ()


def test_reading_a_project_reports_where_it_is(client: TestClient) -> None:
    """A client that reconnects asks the run, not its own memory of it."""
    project_id = with_source(client)
    client.post(f"/projects/{project_id}/source-model/extract", json={})

    body = client.get(f"/projects/{project_id}").json()

    assert body["state"] == "source_model_extracting"
    assert "fork_execution" in body["available_actions"]


# ----------------------------------------------------------------------
# What the workflow refuses, the API refuses too
# ----------------------------------------------------------------------


def test_an_out_of_order_command_is_a_conflict_not_a_crash(client: TestClient) -> None:
    """plan/09 risk: the rules stay in the engine, and its refusal is the answer.

    409 because the request is well-formed and the *run* is in the wrong state —
    a client that fixes its JSON will get the same answer, and one that waits may
    not.

    Proposing an architecture before the source model exists is the cleanest case
    to test it with: the engine refuses on the way in, before anything is looked
    up, so the answer is unambiguously the workflow's.
    """
    project_id = with_source(client)

    response = client.post(f"/projects/{project_id}/architecture/propose")

    assert response.status_code == 409
    assert "propose_architecture" in response.json()["detail"]


def test_an_unattributed_human_action_is_rejected_as_unprocessable(client: TestClient) -> None:
    """plan/05's attribution rule surfaces as a 422, not a 500."""
    project_id = with_source(client)

    response = client.post(f"/projects/{project_id}/cancel", json={"actor_id": ""})

    assert response.status_code == 422


def test_an_unknown_project_is_a_404(client: TestClient) -> None:
    """A missing thing is missing, whatever layer noticed."""
    assert client.get("/projects/nope").status_code == 404


def busy_client(error: Exception) -> TestClient:
    """A client whose every command collides with something holding the database.

    Raised from the runtime factory rather than from a flush deep inside a
    command: what is being pinned is how the *application* reports a database it
    could not write to, and that answer must not depend on which statement was
    the unlucky one.
    """

    def runtime_factory() -> Runtime:
        raise error

    return TestClient(create_app(runtime_factory=runtime_factory), raise_server_exceptions=False)


def test_a_database_held_by_a_running_job_is_reported_as_busy_not_broken() -> None:
    """KNOWN-ISSUES §1: a job holds SQLite's write lock for as long as it runs.

    With a real provider attached that is however long the model takes, and every
    command issued during it waits out the busy timeout and then fails. That is a
    *timing* failure: the request was well-formed, nothing was written, and the
    same request works once the job commits. 503 says exactly that and names when
    to come back; a 500 tells a person their installation is broken.
    """
    locked = OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))

    response = busy_client(locked).post("/projects/any/cancel", json={"actor_id": AUTHOR})

    assert response.status_code == 503
    assert int(response.headers["retry-after"]) > 0
    assert "busy" in response.json()["detail"]


def test_a_database_that_is_actually_broken_is_still_a_500() -> None:
    """Only contention is a 503.

    The same exception class carries a missing table and a syntax error, and
    neither of those clears on its own. Reporting them as "try again" would turn
    a broken installation into an infinite retry loop.
    """
    missing = OperationalError("SELECT 1", {}, sqlite3.OperationalError("no such table: projects"))

    response = busy_client(missing).post("/projects/any/cancel", json={"actor_id": AUTHOR})

    assert response.status_code == 500


# ----------------------------------------------------------------------
# Provenance and progress
# ----------------------------------------------------------------------


async def test_an_execution_can_be_inspected_replayed_and_forked(
    client: TestClient, harness: Harness
) -> None:
    """plan/09 → ``GET /executions/{id}``, ``.../replay``, ``.../fork``.

    The two commands answer with a *job* since phase 12 gave them work to do:
    re-running a stage calls a model, and phase 09's own rule is that a request
    never does. What they queue is asserted where it belongs, in
    ``test_replay_fork``; here it is the seam.
    """
    project_id = with_source(client)
    script_extraction(harness)
    client.post(f"/projects/{project_id}/source-model/extract", json={})
    (job,) = await harness.drain()
    execution_id = job.stage_execution_id
    assert execution_id is not None

    inspected = client.get(f"/executions/{execution_id}")
    events = client.get(f"/executions/{execution_id}/events")
    invocations = client.get(f"/executions/{execution_id}/invocations")
    replayed = client.post(f"/executions/{execution_id}/replay", json={"actor_id": AUTHOR})
    forked = client.post(f"/executions/{execution_id}/fork", json={"actor_id": AUTHOR})

    assert inspected.json()["stage"] == "extract_source_truth"
    assert events.json()[0]["event_type"] == "stage.started"
    assert invocations.json()[0]["template_id"] == "extract_source_truth"
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["source_execution_id"] == execution_id
    assert forked.json()["job"]["job_type"] == "extract_source_model"


async def test_two_executions_can_be_compared(client: TestClient, harness: Harness) -> None:
    """plan/09 → ``GET /executions/compare``; phase 12 deepens what it says."""
    project_id = with_source(client)
    script_extraction(harness)
    client.post(f"/projects/{project_id}/source-model/extract", json={})
    (job,) = await harness.drain()
    assert job.stage_execution_id is not None

    response = client.get(
        "/executions/compare",
        params={"left": job.stage_execution_id, "right": job.stage_execution_id},
    )

    assert response.status_code == 200
    assert response.json()["left"]["id"] == job.stage_execution_id


def test_an_experiment_needs_a_corpus_and_a_baseline(client: TestClient) -> None:
    """plan/09 → ``POST /experiments``, as phase 12 filled it in.

    The endpoint phase 09 held open now asks for the two things an experiment
    cannot be run without: a corpus to run over, and something to compare
    against. Both are refused as bad payloads rather than defaulted, because an
    experiment with an invented baseline reports differences from whichever arm
    happened to be listed first. The happy path lives with the rest of phase 12,
    which has a corpus to point at.
    """
    response = client.post("/experiments", json={"name": "prompt-a-vs-b"})

    assert response.status_code == 422
    assert "dataset_id" in response.text


async def test_a_jobs_progress_is_streamed_as_server_sent_events(
    client: TestClient, harness: Harness
) -> None:
    """plan/09 → *SSE progress works per job*."""
    project_id = with_source(client)
    script_extraction(harness)
    body = client.post(f"/projects/{project_id}/source-model/extract", json={}).json()
    await harness.drain()

    with client.stream("GET", f"/jobs/{body['job']['id']}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = "".join(response.iter_text())

    assert "event: job.status" in frames
    assert "event: stage.started" in frames
    assert frames.endswith("\n\n")


def test_a_screen_still_answers_while_a_job_holds_the_database(tmp_path: Path) -> None:
    """The whole interface, during the minutes a stage takes (KNOWN-ISSUES §1).

    Against a real file, because this is a property of two connections and one
    lock — the suite's shared in-memory database cannot contend with itself, so
    it can neither show the failure nor prove the fix.

    A worker holding its transaction stands in for the job: it has claimed, it is
    somewhere inside a model call, and it will not commit for a while. Every
    ``GET`` in the application has to answer during that, out of the last
    committed snapshot, without waiting for a lock it does not want.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("groundscribe.db.BUSY_TIMEOUT_MS", 500)
    monkeypatch.setenv("GROUNDSCRIBE_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'gs.db'}")
    monkeypatch.setenv("GROUNDSCRIBE_BLOB_ROOT", str(tmp_path / "blobs"))
    # The engines are cached per process, so a test that points the environment
    # somewhere else has to say so — and put it back, or every later test inherits
    # this file.
    bootstrap.engine.cache_clear()
    bootstrap.reading_engine.cache_clear()
    writing = bootstrap.engine()
    Base.metadata.create_all(writing)

    # Wired exactly as `asgi.served_app` wires it: the point is the production
    # arrangement, not that two factories can be passed.
    client = TestClient(
        create_app(
            runtime_factory=bootstrap.build_runtime,
            reader_factory=lambda: bootstrap.build_runtime(reading=True),
        ),
        raise_server_exceptions=False,
    )
    created = client.post(
        "/projects",
        json={
            "title": "Read-through caching",
            "author_id": AUTHOR,
            "constraints": DEFAULT_CONSTRAINTS.model_dump(mode="json"),
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["project_id"]

    job = writing.connect()
    job.begin()
    job.exec_driver_sql("UPDATE projects SET title = 'mid-stage'")
    try:
        dashboard = client.get(f"/projects/{project_id}/dashboard")
        questions = client.get(f"/projects/{project_id}/questions")
    finally:
        job.rollback()
        job.close()
        writing.dispose()
        monkeypatch.undo()
        bootstrap.engine.cache_clear()
        bootstrap.reading_engine.cache_clear()

    assert dashboard.status_code == 200, dashboard.text
    assert questions.status_code == 200, questions.text
    # The committed title, not the one the job has not finished writing.
    assert dashboard.json()["project"]["title"] == "Read-through caching"


def test_the_servable_application_is_wired_to_the_local_installation() -> None:
    """``uvicorn --factory groundscribe.api.asgi:served_app`` must be a real app.

    Built rather than exercised: building it must not touch a database — the
    runtime is a factory called per request — and asserting that here is what
    keeps it cheap enough to be a deployment's entry point.

    Asked through its schema rather than through ``app.routes``, because an
    included router is one object there until the app is served; the schema is
    what a deployment and a client both actually see.
    """
    from groundscribe.api.asgi import served_app
    from groundscribe.api.openapi import build_schema

    # A password, because serving without one is refused (phase 13's slice); the
    # value is irrelevant to what this asserts.
    paths = build_schema(served_app(environ={"GROUNDSCRIBE_PASSWORD": "x"}))["paths"]

    assert "/projects" in paths
    assert "/jobs/{job_id}/events" in paths
