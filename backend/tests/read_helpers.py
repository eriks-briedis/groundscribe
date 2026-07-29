"""Driving a project far enough that the reads have something to show (phase 11).

The frontend's screens are artefact-first, so the tests behind them need real
artefacts: a source model built from a real extraction, a brief a draft was
actually written against, findings a real reviewer raised, a score sheet a rubric
actually assessed. Hand-built rows would let a projection pass while showing a
shape no run produces.

So this walks the pipeline the way a person does — over HTTP, with a worker
draining the queue between commands — and stops wherever the caller asks. Each
step is one method, because most screens do not need the whole walk and the ones
that do should pay for it explicitly.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json, golden_text, relabel
from groundscribe.domain import models as domain_models
from groundscribe.jobs.enums import JobStatus
from groundscribe.provenance import models as provenance_models
from service_helpers import AUTHOR, Harness
from stage_helpers import DEFAULT_CONSTRAINTS
from test_gap_questions import gap
from test_rewrite import golden_rewrite

#: A first round with a blocking question, and a second with none. The pair is
#: what lets a caller choose between "the queue has something in it" and "the
#: source model is finished".
BLOCKING_GAPS: dict[str, Any] = {
    "schema_version": 1,
    "gaps": [gap("g1", "blocking"), gap("g2", "optional", "Which parser?", "Colour only.")],
}
SETTLED_GAPS: dict[str, Any] = {
    "schema_version": 1,
    "gaps": [gap("g3", "optional", "Which parser?", "Colour only.")],
}

#: A phrase the golden draft contains, and a plainer way of saying it. The voice
#: pass is scripted from the *stored* body rather than from the golden file, so
#: this is the only thing about it fixed in advance.
VOICE_BEFORE = "That number is the reason anyone would read this."
VOICE_AFTER = "That number is why anyone would read this."


class Walkthrough:
    """One project, driven over HTTP, with the worker run between commands."""

    def __init__(self, client: TestClient, harness: Harness) -> None:
        self.client = client
        self.harness = harness
        self.project_id = ""
        self.article_id = ""

    # ------------------------------------------------------------------
    # Issuing commands
    # ------------------------------------------------------------------

    async def command(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue one command and let the worker finish whatever it queued."""
        response = self.client.request(method, path, **kwargs)
        assert response.status_code < 300, response.text
        body: dict[str, Any] = response.json()
        if body.get("job"):
            for job in await self.harness.drain():
                assert job.status is JobStatus.SUCCEEDED, job.error_message
            body = self.client.get(f"/projects/{self.project_id}").json()
        return body

    def script(self, stage: str, payload: dict[str, Any]) -> None:
        self.harness.client.script_response(stage, payload)

    # ------------------------------------------------------------------
    # The walk
    # ------------------------------------------------------------------

    async def open_project(self, *, confidential: bool = False) -> str:
        """Create the project and ingest the golden source into it."""
        created = await self.command(
            "POST",
            "/projects",
            json={
                "title": "Read-through caching",
                "author_id": AUTHOR,
                "constraints": DEFAULT_CONSTRAINTS.model_dump(mode="json"),
            },
        )
        self.project_id = str(created["project_id"])
        await self.command(
            "POST",
            f"/projects/{self.project_id}/sources",
            json={
                "title": "Read-through caching for the render pipeline",
                "text": golden_text("source.md"),
                "source_format": "markdown",
                "confidential": confidential,
            },
        )
        return self.project_id

    async def extract(self, *, blocking: bool = False) -> dict[str, Any]:
        """Build the source model, optionally parking on a blocking question."""
        self.script("extract_source_truth", self.source_model())
        self.script("generate_gap_questions", BLOCKING_GAPS if blocking else SETTLED_GAPS)
        return await self.command(
            "POST", f"/projects/{self.project_id}/source-model/extract", json={}
        )

    async def answer(self) -> dict[str, Any]:
        """Answer the surfaced question, which rebuilds the source model."""
        self.script("extract_source_truth", self.source_model())
        self.script("generate_gap_questions", SETTLED_GAPS)
        return await self.command(
            "POST",
            f"/projects/{self.project_id}/source-gaps/{self.surfaced_gap()}/answer",
            json={"text": "Cold-cache p99 was 640ms.", "answered_by": AUTHOR},
        )

    async def architecture(self, *, approve: bool = True) -> dict[str, Any]:
        """Propose the shape of the article, and approve it unless told not to."""
        self.script("propose_content_architecture", golden_json("architecture.json"))
        proposed = await self.command("POST", f"/projects/{self.project_id}/architecture/propose")
        if not approve:
            return proposed
        approved = await self.command(
            "POST",
            f"/projects/{self.project_id}/architecture/current/approve",
            json={"actor_id": AUTHOR},
        )
        self.article_id = self.first_article()
        return approved

    async def brief(self, *, approve: bool = True) -> dict[str, Any]:
        """Generate the brief the article is drafted against."""
        self.script("generate_article_brief", golden_json("brief.json"))
        briefed = await self.command("POST", f"/articles/{self.article_id}/brief/generate")
        if not approve:
            return briefed
        return await self.command(
            "POST", f"/articles/{self.article_id}/brief/approve", json={"actor_id": AUTHOR}
        )

    async def draft(self) -> dict[str, Any]:
        self.script("generate_initial_draft", golden_json("draft.json", suite="draft_to_voice"))
        return await self.command("POST", f"/articles/{self.article_id}/draft")

    async def review(self) -> dict[str, Any]:
        self.script("review_substantively", golden_json("review.json", suite="draft_to_voice"))
        return await self.command("POST", f"/articles/{self.article_id}/review")

    async def revise(self) -> dict[str, Any]:
        """Plan the revision, approve it, and rewrite under it."""
        self.script(
            "create_revision_plan", golden_json("revision_plan.json", suite="draft_to_voice")
        )
        await self.command("POST", f"/articles/{self.article_id}/revision-plan")
        await self.command(
            "POST",
            f"/articles/{self.article_id}/revision-plan/approve",
            json={"actor_id": AUTHOR},
        )
        self.script("rewrite_substantively", golden_rewrite())
        return await self.command("POST", f"/articles/{self.article_id}/rewrite")

    async def align_voice(self) -> dict[str, Any]:
        """Run the voice pass, scripted from whatever the stored body now says."""
        self.script("align_voice", self.voice_pass())
        return await self.command("POST", f"/articles/{self.article_id}/voice-align")

    async def score(self) -> dict[str, Any]:
        self.script("score_article", golden_json("score.json", suite="draft_to_voice"))
        return await self.command("POST", f"/articles/{self.article_id}/score")

    async def validate(self) -> dict[str, Any]:
        return await self.command("POST", f"/articles/{self.article_id}/validate")

    async def to_approval(self) -> dict[str, Any]:
        """The whole walk, ending where a person decides whether to publish."""
        await self.open_project()
        await self.extract()
        await self.architecture()
        await self.brief()
        await self.draft()
        await self.review()
        await self.revise()
        await self.align_voice()
        await self.score()
        return await self.validate()

    # ------------------------------------------------------------------
    # Reading ids a real client would have been handed
    # ------------------------------------------------------------------

    def source_model(self) -> dict[str, Any]:
        """The golden source model, with its labels resolved to real segment ids."""
        segments = self.session.scalars(
            select(domain_models.SourceSegment).order_by(domain_models.SourceSegment.ordinal)
        ).all()
        return relabel(
            golden_json("source_model.json"),
            {f"S{segment.ordinal}": segment.id for segment in segments},
        )

    def voice_pass(self) -> dict[str, Any]:
        """A style-only pass over the body currently stored for the article."""
        version = self.session.scalars(
            select(domain_models.ArticleVersion)
            .where(domain_models.ArticleVersion.article_id == self.article_id)
            .order_by(domain_models.ArticleVersion.ordinal.desc())
        ).first()
        assert version is not None and version.snapshot is not None
        # Read as JSON rather than through a schema: the stored version is an
        # ``ArticleDraft`` after a draft and a ``RewrittenArticle`` after a
        # rewrite, and the voice pass only needs the prose either way.
        stored = json.loads(self.harness.runtime.snapshots.read(version.snapshot))
        body = str(stored["body"])
        return {
            "schema_version": 1,
            "body": body.replace(VOICE_BEFORE, VOICE_AFTER),
            "changes": [
                {
                    "kind": "word_choice",
                    "before": VOICE_BEFORE,
                    "after": VOICE_AFTER,
                    "reason": "Plainer, and one clause shorter.",
                }
            ],
            "structural_problems": [],
        }

    def surfaced_gap(self) -> str:
        row = self.session.scalars(
            select(domain_models.SourceGap)
            .where(domain_models.SourceGap.surfaced.is_(True))
            .order_by(domain_models.SourceGap.ordinal)
        ).first()
        assert row is not None, "the blocking round should have surfaced a question"
        return row.id

    def first_article(self) -> str:
        row = self.session.scalars(select(domain_models.Article)).first()
        assert row is not None, "approving the architecture should have opened an article"
        return row.id

    def executions(self, stage: str) -> list[str]:
        """The ids of every execution of one stage, oldest first."""
        return [
            execution.id
            for execution in self.session.scalars(
                select(provenance_models.StageExecution)
                .where(provenance_models.StageExecution.stage == stage)
                .order_by(provenance_models.StageExecution.ordinal)
            )
        ]

    @property
    def session(self) -> Session:
        return self.harness.runtime.session


__all__ = [
    "BLOCKING_GAPS",
    "SETTLED_GAPS",
    "VOICE_AFTER",
    "VOICE_BEFORE",
    "Walkthrough",
]
