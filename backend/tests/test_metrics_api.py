"""The observability surface, reachable from outside the process (phase 14).

plan/14 → *All observability metrics exposed*. "Exposed" is the operative word:
phase 12 shipped one capability that was built and unreachable and had to record
it as a defect (KNOWN-ISSUES §4), and a metrics module nothing serves would be
the same mistake with a bigger surface.

So this pins the seams rather than the arithmetic — that is pinned in
``test_observability_metrics``. What is asserted here is that the numbers have a
route, that the route hands back what the collector computed, that the CLI asks
the same service the API does, and that the contract phase 11 generates its
client from names them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from groundscribe.api.app import create_app
from groundscribe.api.openapi import build_schema
from groundscribe.cli import main as cli
from groundscribe.observability.metrics import METRIC_NAMES, collect_metrics
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
from service_helpers import Harness, build_harness


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def api(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


@pytest.fixture
def walk(api: TestClient, harness: Harness) -> Walkthrough:
    return Walkthrough(api, harness)


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


async def test_one_projects_metrics_have_a_route(walk: Walkthrough, api: TestClient) -> None:
    """The per-project surface: what a person drills into from a dashboard."""
    await walk.open_project()
    await walk.extract()

    response = api.get(f"/projects/{walk.project_id}/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == walk.project_id
    assert set(METRIC_NAMES) <= set(body)
    assert body["token_usage"]["input_tokens"] > 0
    assert body["runs"] == 1


async def test_the_whole_installation_has_a_route_of_its_own(
    walk: Walkthrough, api: TestClient
) -> None:
    """The figure an operator watches, as against the one they drill into.

    Not the same route with an optional parameter: "every project" is the
    question a monitoring check asks on a schedule, and making it a special case
    of a project id would mean inventing a sentinel id for it.
    """
    await walk.open_project()
    await walk.extract()

    response = api.get("/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] is None
    assert body["runs"] == 1


async def test_the_route_reports_exactly_what_the_collector_computed(
    walk: Walkthrough, api: TestClient
) -> None:
    """No second opinion between the query and the wire.

    A route that recomputed anything — rounded a rate, defaulted a null to zero —
    would make the API and the module disagree about the same installation, and
    the disagreement would surface as an operator not trusting either.
    """
    await walk.open_project()
    await walk.extract()

    served = api.get(f"/projects/{walk.project_id}/metrics").json()
    computed = collect_metrics(walk.session, project_id=walk.project_id)

    assert served == computed.model_dump(mode="json")


async def test_the_cli_reports_the_same_numbers_the_api_does(
    walk: Walkthrough, api: TestClient, cli_runner: CliRunner, harness: Harness
) -> None:
    """plan/09's parity rule, still holding: one service, two front doors.

    An operator on a machine with no browser is the reason the CLI has this at
    all, and a CLI that computed its own numbers would be a second observability
    implementation to keep honest.
    """
    await walk.open_project()
    await walk.extract()

    cli.service_factory = lambda: harness.service
    try:
        result = cli_runner.invoke(cli.app, ["project", "metrics", walk.project_id])
    finally:
        cli.service_factory = cli.default_service

    assert result.exit_code == 0, result.output
    served = api.get(f"/projects/{walk.project_id}/metrics").json()
    assert str(served["token_usage"]["input_tokens"]) in result.output
    assert "stage durations" in result.output.lower()


def test_the_metrics_routes_are_in_the_generated_contract(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/11 generates its client from the written contract, so a route missing
    from it is a screen that cannot be built."""
    harness = build_harness(db_session, snapshot_store)
    paths = build_schema(create_app(runtime_factory=lambda: harness.runtime))["paths"]

    assert "get" in paths["/metrics"]
    assert "get" in paths["/projects/{project_id}/metrics"]
