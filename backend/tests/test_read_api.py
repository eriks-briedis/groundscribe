"""The reads every screen is assembled from (phase 11).

Spec (plan/11): the frontend is *artefact-first* — a dashboard, a source
workspace, a question queue, an architecture board, an article workspace, a
review history, an execution timeline, a stage inspector, a lineage graph and a
run comparison — and it *displays backend state and submits commands; it never
re-implements pipeline-transition rules*.

Phase 09 delivered the commands. A command answers "where is the run and what may
be done to it", which is everything a client needs to *act* and nothing it needs
to *show*. These tests pin the other half: read-only projections of artefacts
that already exist, assembled from stored rows so the UI can render them without
deriving anything.

Two properties hold across the whole file, and they are what keep the projections
honest:

- **A read changes nothing.** No state moves, no job is queued, no model is
  called. A projection that transitioned would be a second state machine wearing
  a ``GET``.
- **Nothing is invented.** Every field traces to a row or a stored snapshot.
  Where the run has not produced something yet, the projection says so with a
  ``null`` rather than a plausible default.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.storage.snapshot_store import SnapshotStore
from read_helpers import Walkthrough
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


def read(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    """A read that must succeed, returned as the document the UI receives."""
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ----------------------------------------------------------------------
# Project dashboard
# ----------------------------------------------------------------------


async def test_the_dashboard_says_where_the_project_is_and_what_it_may_do(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Project dashboard*: status, current stage, approval state.

    The actions come from the same place a command's do — the backend — because
    the dashboard is the first screen that could be tempted to work them out.
    """
    await walk.open_project()
    await walk.extract()

    dashboard = read(client, f"/projects/{walk.project_id}/dashboard")

    assert dashboard["project"]["id"] == walk.project_id
    assert dashboard["project"]["title"] == "Read-through caching"
    assert dashboard["state"] == "source_model_ready"
    assert (
        dashboard["available_actions"]
        == read(client, f"/projects/{walk.project_id}")["available_actions"]
    )


async def test_the_dashboard_counts_the_source_instead_of_estimating_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *source completeness*, and unresolved questions with it."""
    await walk.open_project()
    await walk.extract(blocking=True)

    dashboard = read(client, f"/projects/{walk.project_id}/dashboard")

    assert dashboard["source"]["documents"] == 1
    assert dashboard["source"]["segments"] > 0
    assert dashboard["source"]["claims"] > 0
    assert dashboard["source"]["unresolved_questions"] >= 1
    assert [question["question"] for question in dashboard["questions"]]


async def test_the_dashboard_reports_the_articles_and_what_they_have_cost(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *proposed articles, revision counts, token/cost summaries*."""
    await walk.to_approval()

    dashboard = read(client, f"/projects/{walk.project_id}/dashboard")

    # The approved architecture opens an article per concept, so the walk's is
    # one of several; the dashboard has to carry them all.
    (article,) = [item for item in dashboard["articles"] if item["id"] == walk.article_id]
    assert len(dashboard["articles"]) > 1
    assert article["versions"] >= 2
    assert article["latest_score"]["passed"] is True
    assert dashboard["usage"]["model_calls"] > 0
    assert dashboard["usage"]["input_tokens"] > 0


async def test_the_dashboard_shows_failures_rather_than_hiding_them(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *recent failures*, which is the whole point of listing them."""
    await walk.open_project()
    # Nothing was scripted, so the extraction fails inside the worker and its
    # partial trace survives (plan/03 → failures are recorded, not rolled back).
    client.post(f"/projects/{walk.project_id}/source-model/extract", json={})
    await walk.harness.drain()

    dashboard = read(client, f"/projects/{walk.project_id}/dashboard")

    assert [failure["stage"] for failure in dashboard["recent_failures"]]
    assert all(failure["error_message"] for failure in dashboard["recent_failures"])


# ----------------------------------------------------------------------
# Source workspace
# ----------------------------------------------------------------------


async def test_the_source_workspace_shows_the_claims_and_the_segments_behind_them(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *sources, extracted facts, claims, evidence, provenance links*."""
    await walk.open_project()
    await walk.extract()

    workspace = read(client, f"/projects/{walk.project_id}/source-workspace")

    (document,) = workspace["documents"]
    assert document["title"] == "Read-through caching for the render pipeline"
    assert len(document["segments"]) > 1
    claim = workspace["claims"][0]
    assert claim["segment_ids"], "a claim with no evidence is not traceable"
    assert {segment["id"] for segment in document["segments"]} >= set(claim["segment_ids"])
    assert workspace["source_model"]["summary"]
    assert workspace["provenance"]["source_model_execution_id"]


async def test_the_workspace_marks_confidential_material_and_who_may_see_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *confidential material, provider-visibility rules*."""
    await walk.open_project(confidential=True)

    workspace = read(client, f"/projects/{walk.project_id}/source-workspace")

    (document,) = workspace["documents"]
    assert document["confidential"] is True
    assert workspace["provider_visibility"]["allowed_providers"]
    assert "confidential_names" in workspace["provider_visibility"]


# ----------------------------------------------------------------------
# Question queue
# ----------------------------------------------------------------------


async def test_the_question_queue_carries_why_each_question_matters(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *blocking + high-value questions, the reason each matters*."""
    await walk.open_project()
    await walk.extract(blocking=True)

    queue = read(client, f"/projects/{walk.project_id}/questions")

    blocking = [item for item in queue["questions"] if item["priority"] == "blocking"]
    assert blocking, "a blocking question should reach the queue"
    assert all(item["why_it_matters"] for item in queue["questions"])
    assert all(item["answer"] is None for item in queue["questions"])


async def test_an_answered_question_carries_the_answer_and_what_it_changed(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *answer status, resulting source-model changes*."""
    await walk.open_project()
    await walk.extract(blocking=True)
    await walk.answer()

    queue = read(client, f"/projects/{walk.project_id}/questions")

    answered = [item for item in queue["questions"] if item["answer"] is not None]
    assert answered, "the answered question should still be in the queue, with its answer"
    assert answered[0]["answer"]["answered_by"] == AUTHOR
    assert answered[0]["answer"]["response_type"] == "answered"


# ----------------------------------------------------------------------
# Architecture board
# ----------------------------------------------------------------------


async def test_the_architecture_board_offers_the_concepts_and_their_versions(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *article-concept cards … compare-versions*."""
    await walk.open_project()
    await walk.extract()
    await walk.architecture(approve=False)

    board = read(client, f"/projects/{walk.project_id}/architecture")

    (version,) = board["versions"]
    assert version["id"] == board["current_version_id"]
    assert version["locked"] is False
    assert [concept["title"] for concept in version["concepts"]]
    assert all(concept["thesis"] for concept in version["concepts"])
    assert board["proposal"]["decision"]["selected"]


async def test_an_approved_architecture_says_who_locked_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/05 → an approved architecture does not change silently; the board shows it."""
    await walk.open_project()
    await walk.extract()
    await walk.architecture()

    board = read(client, f"/projects/{walk.project_id}/architecture")

    current = [item for item in board["versions"] if item["id"] == board["current_version_id"]]
    assert current[0]["locked"] is True
    assert current[0]["locked_by"] == AUTHOR


# ----------------------------------------------------------------------
# Article workspace
# ----------------------------------------------------------------------


async def test_the_article_workspace_gathers_what_is_needed_to_judge_a_version(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *brief, current version, findings, plan, voice rules, scores*."""
    await walk.to_approval()

    workspace = read(client, f"/articles/{walk.article_id}/workspace")

    assert workspace["article"]["id"] == walk.article_id
    assert workspace["brief"]["thesis"]
    assert workspace["current_version"]["body"]
    assert [finding["description"] for finding in workspace["findings"]]
    assert workspace["revision_plan"]["summary"]
    assert workspace["voice"]["active"] is not None
    assert workspace["scores"][-1]["passed"] is True
    assert workspace["validation"]["passed"] is True
    assert workspace["producing_execution"]["stage"]


async def test_the_workspace_diffs_a_version_against_the_one_before_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *previous version, diff* — computed from stored bodies, not guessed."""
    await walk.to_approval()

    workspace = read(client, f"/articles/{walk.article_id}/workspace")

    assert workspace["previous_version"]["ordinal"] < workspace["current_version"]["ordinal"]
    diff = workspace["diff"]
    assert diff["added"] > 0 or diff["removed"] > 0
    assert {line["kind"] for line in diff["lines"]} <= {"equal", "added", "removed"}
    assert any(line["kind"] != "equal" for line in diff["lines"])


async def test_a_workspace_read_offers_only_the_actions_the_backend_offers(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Actions rendered strictly from backend ``available_actions``*."""
    await walk.to_approval()

    workspace = read(client, f"/articles/{walk.article_id}/workspace")
    state = read(client, f"/projects/{walk.project_id}")

    assert workspace["state"] == state["state"] == "human_approval_required"
    assert workspace["available_actions"] == state["available_actions"]


async def test_the_workspace_shows_everything_an_approval_needs(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → the human-approval view: rounds, concerns, interventions, cost."""
    await walk.to_approval()

    approval = read(client, f"/articles/{walk.article_id}/workspace")["approval"]

    assert approval["rewrite_rounds"] >= 1
    assert approval["usage"]["cost_usd"] is not None
    assert [item["intervention_type"] for item in approval["interventions"]]
    assert [version["template_id"] for version in approval["model_versions"]], (
        "an approval that cannot name the prompts behind the article is not informed"
    )


# ----------------------------------------------------------------------
# Review history
# ----------------------------------------------------------------------


async def test_the_review_history_shows_the_scores_and_the_issue_lifecycle(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *score progression table + issue history* (new/resolved/reopened)."""
    await walk.to_approval()

    history = read(client, f"/articles/{walk.article_id}/reviews")

    (round_one, *_) = history["rounds"]
    assert round_one["verdict"] == "revise"
    assert {issue["lifecycle"] for issue in round_one["issues"]} <= {
        "new",
        "repeated",
        "resolved",
    }
    assert history["scores"][-1]["overall"] > 0
    assert history["scores"][-1]["rubric_version"]


# ----------------------------------------------------------------------
# Lineage
# ----------------------------------------------------------------------


async def test_the_lineage_graph_links_a_version_to_the_one_it_came_from(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Lineage graph — branching causal relationships*."""
    await walk.to_approval()

    lineage = read(client, f"/articles/{walk.article_id}/lineage")

    assert len(lineage["nodes"]) >= 2
    assert lineage["edges"], "versions after the first must name their parent"
    ids = {node["id"] for node in lineage["nodes"]}
    assert all({edge["from"], edge["to"]} <= ids for edge in lineage["edges"])
    assert all(node["execution_id"] for node in lineage["nodes"])


# ----------------------------------------------------------------------
# Execution timeline, filters and the stage inspector
# ----------------------------------------------------------------------


async def test_the_timeline_returns_the_runs_executions_in_order(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Execution timeline — chronological expandable trace events*."""
    await walk.open_project()
    await walk.extract()

    trace = read(client, f"/projects/{walk.project_id}/trace")

    stages = [execution["stage"] for execution in trace["executions"]]
    assert "extract_source_truth" in stages
    ordinals = [execution["ordinal"] for execution in trace["executions"]]
    assert ordinals == sorted(ordinals)
    assert trace["filters_applied"] == []


async def test_a_trace_filter_returns_only_what_it_names(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Trace filters* — failed executions among them."""
    await walk.open_project()
    client.post(f"/projects/{walk.project_id}/source-model/extract", json={})
    await walk.harness.drain()

    filtered = read(client, f"/projects/{walk.project_id}/trace", filter="failed")

    assert filtered["filters_applied"] == ["failed"]
    assert filtered["executions"], "the failed extraction should match"
    assert all(execution["status"] == "failed" for execution in filtered["executions"])
    assert all("failed" in execution["matched_filters"] for execution in filtered["executions"])


async def test_an_unknown_trace_filter_is_refused_rather_than_ignored(
    walk: Walkthrough, client: TestClient
) -> None:
    """A filter silently dropped would show a person more than they asked for."""
    await walk.open_project()

    response = client.get(f"/projects/{walk.project_id}/trace", params={"filter": "nonsense"})

    assert response.status_code == 422


async def test_the_stage_inspector_returns_every_layer_of_one_execution(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Stage inspector*: inputs, context, request, response, outputs, cost."""
    await walk.open_project()
    await walk.extract()
    (execution_id, *_) = walk.executions("extract_source_truth")

    inspection = read(client, f"/executions/{execution_id}/inspect")

    assert inspection["summary"]["stage"] == "extract_source_truth"
    assert inspection["outputs"], "the extraction produced a source model"
    assert inspection["outputs"][0]["content"], "an output nobody can read explains nothing"
    (invocation, *_) = inspection["invocations"]
    assert invocation["effective_request"] is not None
    assert invocation["raw_response"] is not None
    assert invocation["template_id"] == "extract_source_truth"
    assert inspection["usage"]["input_tokens"] > 0
    assert inspection["events"], "the execution's own timeline"


async def test_the_inspector_shows_a_failure_with_what_it_managed_to_record(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/03 → partial traces survive a failure; the inspector is where they are read."""
    await walk.open_project()
    client.post(f"/projects/{walk.project_id}/source-model/extract", json={})
    await walk.harness.drain()
    (execution_id, *_) = walk.executions("extract_source_truth")

    inspection = read(client, f"/executions/{execution_id}/inspect")

    assert inspection["summary"]["status"] == "failed"
    assert inspection["error"]["message"]


# ----------------------------------------------------------------------
# Run comparison
# ----------------------------------------------------------------------


async def test_a_comparison_names_what_differs_between_two_executions(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Run comparison — side-by-side config/prompt/output/cost/latency*."""
    await walk.open_project()
    await walk.extract(blocking=True)
    await walk.answer()
    first, second = walk.executions("extract_source_truth")[:2]

    comparison = read(client, "/executions/compare", left=first, right=second)

    assert comparison["left"]["id"] == first
    assert comparison["right"]["id"] == second
    differences = {row["field"]: row for row in comparison["differences"]}
    assert differences["stage"]["same"] is True
    assert differences["model"]["same"] is True
    assert "cost_usd" in differences
    assert "latency_ms" in differences
    assert comparison["output_edit_distance"] is not None


# ----------------------------------------------------------------------
# What a read must never do
# ----------------------------------------------------------------------


async def test_a_read_moves_nothing_and_calls_nothing(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → the frontend *displays* backend state; a ``GET`` may not act.

    Every projection in one test, because the property is about the class of
    endpoint rather than about any one of them: after reading all of them, the
    run is where it was, no job was queued and no model was called.
    """
    await walk.to_approval()
    before = read(client, f"/projects/{walk.project_id}")
    calls = len(walk.harness.client.received_requests)
    (execution_id, *_) = walk.executions("extract_source_truth")

    for path in (
        f"/projects/{walk.project_id}/dashboard",
        f"/projects/{walk.project_id}/source-workspace",
        f"/projects/{walk.project_id}/questions",
        f"/projects/{walk.project_id}/architecture",
        f"/projects/{walk.project_id}/trace",
        f"/articles/{walk.article_id}/workspace",
        f"/articles/{walk.article_id}/reviews",
        f"/articles/{walk.article_id}/lineage",
        f"/executions/{execution_id}/inspect",
    ):
        assert client.get(path).status_code == 200, path

    after = read(client, f"/projects/{walk.project_id}")
    assert after == before
    assert len(walk.harness.client.received_requests) == calls
    assert walk.harness.runtime.queue.claim(worker_id="reader") is None


async def test_reading_something_that_does_not_exist_says_so(client: TestClient) -> None:
    """A projection that invented an empty document would hide a broken link."""
    assert client.get("/projects/nope/dashboard").status_code == 404
    assert client.get("/articles/nope/workspace").status_code == 404
    assert client.get("/executions/nope/inspect").status_code == 404
