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

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from golden import golden_text
from groundscribe.api.app import create_app
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


# ----------------------------------------------------------------------
# Provenance and progress
# ----------------------------------------------------------------------


async def test_an_execution_can_be_inspected_replayed_and_forked(
    client: TestClient, harness: Harness
) -> None:
    """plan/09 → ``GET /executions/{id}``, ``.../replay``, ``.../fork``."""
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
    assert replayed.json()["parent_execution_id"] == execution_id
    assert forked.json()["parent_execution_id"] == execution_id


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


def test_an_experiment_can_be_opened(client: TestClient) -> None:
    """plan/09 → ``POST /experiments``, as a stable contract for phase 12."""
    response = client.post("/experiments", json={"name": "prompt-a-vs-b"})

    assert response.status_code == 201
    assert response.json()["name"] == "prompt-a-vs-b"


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


def test_the_servable_application_is_wired_to_the_local_installation() -> None:
    """``uvicorn groundscribe.api.asgi:app`` has to be a real, complete app.

    Imported rather than exercised: building it must not touch a database — the
    runtime is a factory called per request — and asserting that here is what
    keeps the import cheap enough to be a deployment's entry point.

    Asked through its schema rather than through ``app.routes``, because an
    included router is one object there until the app is served; the schema is
    what a deployment and a client both actually see.
    """
    from groundscribe.api.asgi import app as servable
    from groundscribe.api.openapi import build_schema

    paths = build_schema(servable)["paths"]

    assert "/projects" in paths
    assert "/jobs/{job_id}/events" in paths
