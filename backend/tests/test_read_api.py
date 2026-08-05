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
# The way in
# ----------------------------------------------------------------------


async def test_the_projects_can_be_listed(walk: Walkthrough, client: TestClient) -> None:
    """Every screen phase 11 built addresses a project by id, and nothing said
    where the first id comes from.

    An interface whose entry point is "paste a uuid" is not an interface. The
    list is what a person opens the app to, so it carries enough to choose
    between projects — what each is called, where its run has got to — and
    nothing that would need a second query per row.
    """
    await walk.open_project()
    await walk.extract()

    projects = read(client, "/projects")

    (project,) = [item for item in projects["projects"] if item["id"] == walk.project_id]
    assert project["title"] == "Read-through caching"
    assert project["author_id"] == AUTHOR
    assert project["state"] == "source_model_ready"
    assert project["opened_at"]


async def test_the_newest_project_is_first(walk: Walkthrough, client: TestClient) -> None:
    """A list ordered by id would be ordered by uuid, which is no order at all."""
    await walk.open_project()
    first = walk.project_id
    await walk.open_project()
    second = walk.project_id

    listed = [item["id"] for item in read(client, "/projects")["projects"]]

    assert listed.index(second) < listed.index(first)


async def test_an_empty_installation_lists_nothing_rather_than_failing(
    client: TestClient,
) -> None:
    """The first thing a new installation does is ask this question."""
    assert read(client, "/projects") == {"projects": []}


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


async def test_every_offered_action_says_how_it_is_performed(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Actions rendered strictly from backend ``available_actions``*.

    A name alone is not enough to act on. Something has to know that
    ``approve_final`` is ``POST /articles/{id}/approve``, and if that knowledge
    sits in the frontend it is a second, drifting copy of the API — the mapping
    the plan's *no routing logic in the frontend* guard exists to prevent. So the
    backend says it, per action, resolved against the thing being looked at.
    """
    await walk.to_approval()

    workspace = read(client, f"/articles/{walk.article_id}/workspace")

    links = {link["action"]: link for link in workspace["action_links"]}
    assert set(links) == set(workspace["available_actions"])
    assert links["approve_final"]["method"] == "POST"
    assert links["approve_final"]["path"] == f"/articles/{walk.article_id}/approve"
    assert links["approve_final"]["requires_actor"] is True


async def test_an_action_the_api_cannot_perform_is_shown_without_a_way_to_take_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """Some offered actions belong to the pipeline, not to a person.

    ``fail`` is a legal transition in every non-terminal state and no endpoint
    performs it. Reporting it with a null path is what lets the interface show
    the true set of transitions while offering buttons only for what a person can
    actually do — a list filtered by the backend would leave the client unable to
    tell "not yours" from "not offered".
    """
    await walk.open_project()

    links = {
        link["action"]: link
        for link in read(client, f"/projects/{walk.project_id}/dashboard")["action_links"]
    }

    assert links["fail"]["path"] is None
    assert links["cancel"]["path"] == f"/projects/{walk.project_id}/cancel"


async def test_a_run_waiting_on_work_names_the_command_that_starts_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """A state whose name ends in ``-ing`` is waiting for a worker to be given the job.

    The transition into it was taken when the author approved the brief, so no
    *action* remains to describe what happens next — but a command still has to
    be issued. The backend names it rather than leaving each client to work out
    which endpoint corresponds to which state.
    """
    await walk.open_project()
    await walk.extract()
    await walk.architecture()
    await walk.brief()

    workspace = read(client, f"/articles/{walk.article_id}/workspace")

    assert workspace["state"] == "draft_generating"
    assert workspace["pending_command"]["path"] == f"/articles/{walk.article_id}/draft"
    assert workspace["pending_command"]["method"] == "POST"


async def test_a_question_carries_the_endpoint_that_answers_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """Answering is addressed per question, so the link belongs on the question."""
    await walk.open_project()
    await walk.extract(blocking=True)

    (question, *_) = read(client, f"/projects/{walk.project_id}/questions")["questions"]

    assert question["answer_path"] == (
        f"/projects/{walk.project_id}/source-gaps/{question['id']}/answer"
    )


async def test_the_dashboard_says_where_the_work_has_got_to_and_who_it_waits_on(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → the interface displays backend state; the strip is backend state.

    A run parked on questions is a person's turn, three phases from where it
    started and five from the end. Every part of that sentence comes from the
    workflow's own map, so a screen can render a progress strip without knowing
    that a pipeline has phases at all.
    """
    await walk.open_project()
    await walk.extract(blocking=True)

    journey = read(client, f"/projects/{walk.project_id}/dashboard")["journey"]

    assert journey["waiting_on"] == "you"
    assert journey["headline"] == "Your turn: answer what the source could not."
    assert [step["status"] for step in journey["steps"]][:2] == ["current", "upcoming"]
    assert [step["title"] for step in journey["steps"]] == [
        "Source",
        "Architecture",
        "Brief",
        "Draft",
        "Review",
        "Voice",
        "Score",
        "Publish",
    ]


async def test_a_human_edge_with_no_button_is_not_reported_as_the_pipelines(
    walk: Walkthrough, client: TestClient
) -> None:
    """``answer_questions`` is the author's, and is taken on the question screen.

    No endpoint on the dashboard performs it, and the natural reading of a
    pathless link — "the pipeline will get to it" — is exactly wrong for the one
    kind of link a run is parked waiting for.
    """
    await walk.open_project()
    await walk.extract(blocking=True)

    links = {
        link["action"]: link
        for link in read(client, f"/projects/{walk.project_id}/dashboard")["action_links"]
    }

    assert links["answer_questions"]["path"] is None
    assert links["answer_questions"]["taken_by"] == "you"
    # `fail` is the machine's, and is pathless for a different reason: nobody is
    # offered a button that fails their own run.
    assert links["fail"]["taken_by"] == "pipeline"
    assert links["cancel"]["taken_by"] == "you"


async def test_the_question_queue_carries_the_one_command_that_ends_the_round(
    walk: Walkthrough, client: TestClient
) -> None:
    """Answering collects; submitting spends the model call. Two commands, one round."""
    await walk.open_project()
    await walk.extract(blocking=True)

    queue = read(client, f"/projects/{walk.project_id}/questions")

    assert queue["submit"]["path"] == f"/projects/{walk.project_id}/source-questions/submit"
    assert queue["submit"]["requires_actor"] is True


async def test_the_queue_offers_no_submit_once_the_round_is_over(
    walk: Walkthrough, client: TestClient
) -> None:
    """The same rule as the answer links, from the same place: what the run offers."""
    await walk.open_project()
    await walk.extract(blocking=True)
    await walk.answer()

    assert read(client, f"/projects/{walk.project_id}/questions")["submit"] is None


async def test_a_question_the_run_has_moved_past_offers_no_way_to_answer_it(
    walk: Walkthrough, client: TestClient
) -> None:
    """A queue outlives the pause it was raised in, and its answer links do not.

    Answering re-enters extraction, which asks again — and a round that surfaces
    nothing blocking completes the source model. The earlier round's questions
    stay on screen, because plan/11 keeps settled work visible, but the run has
    left the only state ``answer_questions`` is legal in. Offering a path that
    every client can only discover is closed by posting to it would make the
    interface hold an opinion the transition table already contradicts.
    """
    await walk.open_project()
    await walk.extract(blocking=True)
    await walk.answer()

    queue = read(client, f"/projects/{walk.project_id}/questions")["questions"]
    unresolved = [question for question in queue if not question["resolved"]]

    assert read(client, f"/projects/{walk.project_id}")["state"] == "source_model_ready"
    assert unresolved, "the walk left questions nobody answered; without them this proves nothing"
    assert [question["answer_path"] for question in unresolved] == [None] * len(unresolved)


async def test_an_answered_question_offers_no_way_to_answer_it_again(
    walk: Walkthrough, client: TestClient
) -> None:
    """A closed question is a record, not a form.

    The gap it closed may have been one of several — an answer may close others
    with it — so "already answered" is read from the gap rather than from whether
    this particular question has an answer of its own attached.
    """
    await walk.open_project()
    await walk.extract(blocking=True)
    await walk.answer()

    queue = read(client, f"/projects/{walk.project_id}/questions")["questions"]
    resolved = [question for question in queue if question["resolved"]]

    assert [question["answer_path"] for question in resolved] == [None] * len(resolved)
    assert resolved and all(question["answer"] for question in resolved)


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


async def test_the_workspace_says_where_material_is_added(
    walk: Walkthrough, client: TestClient
) -> None:
    """Ingestion is a command with no workflow action behind it.

    Nothing about a project's state makes importing legal or illegal, so it never
    appears in ``available_actions`` and never gets a link from there. Without one
    the interface has to build the URL itself, which is the copy of the API this
    phase has spent its time not making.
    """
    await walk.open_project()

    workspace = read(client, f"/projects/{walk.project_id}/source-workspace")

    assert workspace["import_command"]["method"] == "POST"
    assert workspace["import_command"]["path"] == f"/projects/{walk.project_id}/sources"
    assert workspace["import_command"]["requires_actor"] is False


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


async def test_the_board_says_how_the_architecture_is_edited_and_approved(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → the board's cards carry *merge/split/delete/reorder/rename/
    edit-thesis/reassign-evidence/approve*.

    Seven operations and two endpoints, all of them the backend's: the same
    reasoning as ``action_links``. A board that knew the URL and the vocabulary
    would be a second copy of the override API, and phase 06 made that
    vocabulary closed precisely so an eighth operation could not be invented at
    a call site.
    """
    await walk.open_project()
    await walk.extract()
    await walk.architecture(approve=False)

    board = read(client, f"/projects/{walk.project_id}/architecture")

    assert board["operations"] == [
        "merge",
        "split",
        "remove",
        "reorder",
        "rename",
        "edit_thesis",
        "reassign_evidence",
    ]
    assert board["edit_command"]["method"] == "PUT"
    assert board["edit_command"]["path"] == (
        f"/projects/{walk.project_id}/architecture/{board['current_version_id']}"
    )
    assert board["approve_command"]["path"] == (
        f"/projects/{walk.project_id}/architecture/{board['current_version_id']}/approve"
    )
    assert board["approve_command"]["requires_actor"] is True


async def test_an_empty_board_offers_nothing_to_edit(walk: Walkthrough, client: TestClient) -> None:
    """Before anything is proposed there is no version to address."""
    await walk.open_project()

    board = read(client, f"/projects/{walk.project_id}/architecture")

    assert board["current_version_id"] is None
    assert board["edit_command"] is None
    assert board["approve_command"] is None


async def test_an_approved_architecture_is_not_offered_for_approval_again(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → a board offers what the run may do, not what it once could.

    A version that exists is not a version this run may still act on. Offered
    after approval, the button fails when pressed — the same failure the empty
    board's ``None`` exists to prevent, arriving from the other side.

    Editing survives approval, because it does not need the same edge: an
    approved architecture is reopened rather than rejected, and both are ways to
    commit an author's edits.
    """
    await walk.open_project()
    await walk.extract()
    await walk.architecture()

    board = read(client, f"/projects/{walk.project_id}/architecture")

    assert board["current_version_id"] is not None
    assert board["approve_command"] is None
    assert board["edit_command"] is not None


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


async def test_the_workspace_carries_the_source_evidence_the_article_rests_on(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Article workspace: … source evidence …*.

    The claims the brief was written from, beside the prose written from them.
    Without it, checking a figure means leaving the article, finding the source
    workspace and matching claim ids by eye — which is the moment a person stops
    checking.
    """
    await walk.to_approval()

    workspace = read(client, f"/articles/{walk.article_id}/workspace")

    assert workspace["source_evidence"], "the brief cites claims; the workspace should carry them"
    (claim, *_) = workspace["source_evidence"]
    assert claim["text"]
    assert claim["segment_ids"]


async def test_a_score_carries_the_confidence_it_was_reached_with(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → the approval view shows *scores, confidence*.

    plan/08 records how far repeat passes sat apart precisely so a number can be
    doubted; a projection that dropped it would show the number alone, which is
    the false precision the rubric work was built to avoid.
    """
    await walk.to_approval()

    (score, *_) = read(client, f"/articles/{walk.article_id}/workspace")["scores"]

    assert score["confidence"]["repeats"] >= 1
    assert score["confidence"]["repeat_scores"]


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


async def test_the_timeline_returns_the_runs_executions_newest_first(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Execution timeline — chronological expandable trace events*.

    Pinned on ``started_at`` rather than on ``ordinal``: nothing assigns the
    ordinal, so every row carries 0 and an assertion about it holds whatever
    order the rows arrive in — including the shuffle a sort by id produces.
    """
    await walk.open_project()
    await walk.extract()

    trace = read(client, f"/projects/{walk.project_id}/trace")

    stages = [execution["stage"] for execution in trace["executions"]]
    assert "extract_source_truth" in stages
    started = [execution["started_at"] for execution in trace["executions"]]
    assert started == sorted(started, reverse=True), "the stage that just ran belongs on top"
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


async def test_the_trace_names_the_filters_it_understands(
    walk: Walkthrough, client: TestClient
) -> None:
    """plan/11 → *Trace filters*, listed by the side that implements them.

    The frontend renders one control per filter, and the only way for it to do
    that without keeping its own copy of the vocabulary — which would go stale
    the moment a filter is added — is for the response to say what the filters
    are.
    """
    await walk.open_project()

    trace = read(client, f"/projects/{walk.project_id}/trace")

    assert trace["filters_available"] == [
        "failed",
        "schema_repair",
        "fallback_model",
        "blocking_finding",
        "user_override",
        "high_cost",
        "low_confidence_score",
        "confidential_warning",
        "repeated_issue",
    ]


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
