"""Driving a real project through the API and a worker (phase 09).

Every other test in this phase holds one piece still and checks it. This one
holds nothing still: an HTTP client issues commands, a separate worker drains the
queue between them, and the run walks the pipeline on the strength of what is
stored in the database. It is the only test that would notice a seam that works
perfectly on both sides and not at all in the middle.

The property it exists to protect is stated once and checked after every command:
**the request never calls a model, and the worker always does**. That is what
plan/09 moved, and it is the kind of thing that stops being true quietly.

Scope, deliberately: the walk stops where the golden data stops being able to
answer honestly — at the revision pause the golden review asks for. What the
later stages *do* is already covered end to end by phases 07 and 08; repeating it
here would mean inventing scripted model answers to prove something about
FastAPI, which is not what this file is for.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json, golden_text, relabel
from groundscribe.api.app import create_app
from groundscribe.domain import models as domain_models
from groundscribe.jobs.enums import JobStatus
from groundscribe.storage.snapshot_store import SnapshotStore
from service_helpers import AUTHOR, Harness, build_harness
from stage_helpers import DEFAULT_CONSTRAINTS
from test_gap_questions import gap

#: A first round that stops the run for the author, and a second that does not.
#: The pair is what makes the answer step meaningful: without the blocking gap
#: there is nothing to answer, and without the clean second round the run would
#: never leave the question pause.
BLOCKING_GAPS: dict[str, Any] = {
    "schema_version": 1,
    "gaps": [gap("g1", "blocking"), gap("g2", "optional", "Which parser?", "Colour only.")],
}
SETTLED_GAPS: dict[str, Any] = {
    "schema_version": 1,
    "gaps": [gap("g3", "optional", "Which parser?", "Colour only.")],
}


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


class Pipeline:
    """One project, driven over HTTP, with a worker running between commands."""

    def __init__(self, client: TestClient, harness: Harness) -> None:
        self._client = client
        self._harness = harness
        self.project_id = ""

    async def command(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue one command, then let the worker do whatever it queued.

        The assertion in the middle is the whole point of this file: between the
        request returning and the worker starting, the model must not have been
        called.
        """
        before = len(self._harness.client.received_requests)
        response = self._client.request(method, path, **kwargs)
        assert response.status_code < 300, response.text
        assert len(self._harness.client.received_requests) == before, (
            f"{method} {path} called a model inside the request"
        )

        body: dict[str, Any] = response.json()
        if body.get("job"):
            for job in await self._harness.drain():
                assert job.status is JobStatus.SUCCEEDED, job.error_message
            body = self._client.get(f"/projects/{self.project_id}").json()
        return body

    def script(self, stage: str, payload: dict[str, Any]) -> None:
        self._harness.client.script_response(stage, payload)

    def source_model(self) -> dict[str, Any]:
        """The golden source model, with its segment labels resolved to real ids."""
        segments = self._harness.runtime.session.scalars(
            select(domain_models.SourceSegment).order_by(domain_models.SourceSegment.ordinal)
        ).all()
        return relabel(
            golden_json("source_model.json"),
            {f"S{segment.ordinal}": segment.id for segment in segments},
        )

    def surfaced_gap(self) -> str:
        gap_row = self._harness.runtime.session.scalars(
            select(domain_models.SourceGap)
            .where(domain_models.SourceGap.surfaced.is_(True))
            .order_by(domain_models.SourceGap.ordinal)
        ).first()
        assert gap_row is not None, "the first round should have surfaced a blocking question"
        return gap_row.id

    def article_id(self) -> str:
        article = self._harness.runtime.session.scalars(select(domain_models.Article)).first()
        assert article is not None, "approving the architecture should have opened an article"
        return article.id


async def test_a_project_walks_the_pipeline_over_http_with_a_worker_behind_it(
    client: TestClient, harness: Harness
) -> None:
    """plan/09 → the whole point of the phase, exercised as a system.

    Read as a script: each step is a command a person issues, and the state after
    it is what the API told them. Nothing here reaches past the HTTP boundary
    except to script what the model will say and to read ids a real client would
    have been given.
    """
    pipeline = Pipeline(client, harness)

    created = await pipeline.command(
        "POST",
        "/projects",
        json={
            "title": "Read-through caching",
            "author_id": AUTHOR,
            "constraints": DEFAULT_CONSTRAINTS.model_dump(mode="json"),
        },
    )
    pipeline.project_id = created["project_id"]
    assert created["state"] == "source_ingested"

    await pipeline.command(
        "POST",
        f"/projects/{pipeline.project_id}/sources",
        json={
            "title": "Read-through caching for the render pipeline",
            "text": golden_text("source.md"),
            "source_format": "markdown",
        },
    )

    # Extraction: the model is called by the worker, and the run parks because
    # the source does not answer everything.
    pipeline.script("extract_source_truth", pipeline.source_model())
    pipeline.script("generate_gap_questions", BLOCKING_GAPS)
    extracted = await pipeline.command(
        "POST", f"/projects/{pipeline.project_id}/source-model/extract", json={}
    )
    assert extracted["state"] == "source_questions_required"
    assert "answer_questions" in extracted["available_actions"]

    # The author answers, and the source model is rebuilt rather than patched.
    pipeline.script("extract_source_truth", pipeline.source_model())
    pipeline.script("generate_gap_questions", SETTLED_GAPS)
    answered = await pipeline.command(
        "POST",
        f"/projects/{pipeline.project_id}/source-gaps/{pipeline.surfaced_gap()}/answer",
        json={"text": "Cold-cache p99 was 640ms.", "answered_by": AUTHOR},
    )
    assert answered["state"] == "source_model_ready"

    pipeline.script("propose_content_architecture", golden_json("architecture.json"))
    proposed = await pipeline.command(
        "POST", f"/projects/{pipeline.project_id}/architecture/propose"
    )
    assert proposed["state"] == "architecture_review_required"

    approved = await pipeline.command(
        "POST",
        f"/projects/{pipeline.project_id}/architecture/current/approve",
        json={"actor_id": AUTHOR},
    )
    assert approved["state"] == "architecture_approved"

    article_id = pipeline.article_id()
    pipeline.script("generate_article_brief", golden_json("brief.json"))
    briefed = await pipeline.command("POST", f"/articles/{article_id}/brief/generate")
    assert briefed["state"] == "brief_review_required"

    opened = await pipeline.command(
        "POST", f"/articles/{article_id}/brief/approve", json={"actor_id": AUTHOR}
    )
    assert opened["state"] == "draft_generating"

    pipeline.script("generate_initial_draft", golden_json("draft.json", suite="draft_to_voice"))
    drafted = await pipeline.command("POST", f"/articles/{article_id}/draft")
    assert drafted["state"] == "substantive_reviewing"

    pipeline.script("review_substantively", golden_json("review.json", suite="draft_to_voice"))
    reviewed = await pipeline.command("POST", f"/articles/{article_id}/review")
    assert reviewed["state"] == "revision_plan_required"
    assert "approve_revision_plan" in reviewed["available_actions"]


async def test_the_whole_run_is_reconstructible_afterwards(
    client: TestClient, harness: Harness
) -> None:
    """plan/00 → observable provenance is part of the product, over HTTP too.

    A shorter walk, asserting the thing a person actually does with a finished
    stage: open it, read what it did, and see the model calls behind it.
    """
    pipeline = Pipeline(client, harness)
    created = await pipeline.command(
        "POST",
        "/projects",
        json={
            "title": "Read-through caching",
            "author_id": AUTHOR,
            "constraints": DEFAULT_CONSTRAINTS.model_dump(mode="json"),
        },
    )
    pipeline.project_id = created["project_id"]
    await pipeline.command(
        "POST",
        f"/projects/{pipeline.project_id}/sources",
        json={
            "title": "Read-through caching for the render pipeline",
            "text": golden_text("source.md"),
            "source_format": "markdown",
        },
    )
    pipeline.script("extract_source_truth", pipeline.source_model())
    pipeline.script("generate_gap_questions", SETTLED_GAPS)

    queued = client.post(f"/projects/{pipeline.project_id}/source-model/extract", json={}).json()
    (job, *_) = await harness.drain()
    assert job.stage_execution_id is not None

    events = client.get(f"/executions/{job.stage_execution_id}/events").json()
    invocations = client.get(f"/executions/{job.stage_execution_id}/invocations").json()
    with client.stream("GET", f"/jobs/{queued['job']['id']}/events") as stream:
        frames = "".join(stream.iter_text())

    assert [event["event_type"] for event in events][:2] == ["stage.started", "job.started"]
    assert [invocation["provider"] for invocation in invocations] == ["ollama"]
    assert "event: job.status" in frames
    assert '"status": "succeeded"' in frames
