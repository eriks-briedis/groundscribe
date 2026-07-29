"""The privacy features, reachable from outside the process (phase 13).

Spec (plan/13 → *export formats*, *project-level trace export + deletion*,
*provider-visibility surface data available to the UI*).

Phase 12 shipped one thing that was built and unreachable, and recorded it as a
defect rather than a feature (KNOWN-ISSUES §4). This module is the answer to that
lesson applied in advance: everything this phase built is exercised through the
service, the HTTP API and the CLI, so "implemented" and "usable" are the same
claim.

The tests are deliberately about the *seams*, not about the behaviour underneath
— that is pinned in the modules of its own. What is asserted here is that each
capability has a route, that the route hands back what the module computed, and
that the refusals survive the trip: an export that raises on confidential
material must not become a 200 with a warning field nobody reads.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from groundscribe.api.app import create_app
from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality
from groundscribe.domain.enums import ArtifactType, SegmentKind
from groundscribe.privacy.export import ExportFormat
from groundscribe.provenance.enums import InvocationOutcome
from groundscribe.provenance.schemas import EffectiveRequest
from groundscribe.stages.schemas import ArticleDraft
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import seed_project
from service_helpers import Harness, build_harness

ARTICLE = ArticleDraft(
    title="Read-through caching for the render pipeline",
    thesis="A read-through cache cut p99 render latency.",
    body="## What we shipped\n\np99 latency fell from 810ms to 120ms.\n",
)

SECRET = "Northwind threatened to terminate the contract over the outage."


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def api(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


def _project(session: Session) -> str:
    return seed_project(session)


def _recorded(harness: Harness, project_id: str) -> None:
    """One run with one model call, so there is a trace to export."""
    recorder = harness.runtime.recorder
    run = recorder.start_run(project_id=project_id)
    execution = recorder.start_stage(run, stage="extract_source_truth")
    recorder.record_model_invocation(
        execution,
        request=EffectiveRequest(
            template_id="extract_source_truth",
            template_version="1",
            rendered_prompt="Summarise the postmortem.",
        ),
        provider="ollama",
        model="llama3.1:70b-instruct",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response="{}",
    )


def _flag(session: Session, project_id: str) -> None:
    document = domain_models.SourceDocument(id="doc-api", project_id=project_id, title="Postmortem")
    session.add(document)
    session.flush()
    session.add(
        domain_models.SourceSegment(
            id="seg-api",
            document_id=document.id,
            ordinal=0,
            text=SECRET,
            kind=SegmentKind.PARAGRAPH,
            confidentiality=Confidentiality.CONFIDENTIAL,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def test_the_service_renders_an_article_in_a_named_format(
    harness: Harness, db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The four formats are asked for by name, not by content negotiation."""
    _project(db_session)
    version = snapshot_store.write(
        artifact_type=ArtifactType.ARTICLE_VERSION,
        content=ARTICLE.model_dump_json().encode("utf-8"),
        created_by_execution_id="exec-api",
    )

    exported = harness.service.render_version(version.id, ExportFormat.PLAIN_TEXT)

    assert exported.format is ExportFormat.PLAIN_TEXT
    assert "##" not in exported.content
    assert exported.content_hash == version.content_hash


def test_the_service_reports_where_a_project_s_material_goes(
    harness: Harness, db_session: Session
) -> None:
    project_id = _project(db_session)
    _flag(db_session, project_id)

    surface = harness.service.provider_visibility(project_id)

    assert surface.stages
    assert surface.confidential_segments == 1


def test_the_service_exports_and_deletes_a_project_s_trace(
    harness: Harness, db_session: Session
) -> None:
    project_id = _project(db_session)
    _recorded(harness, project_id)

    exported = harness.service.export_traces(project_id)
    assert "Summarise the postmortem." in exported.to_json()

    removed = harness.service.delete_traces(project_id)
    assert removed.payloads > 0
    assert "Summarise the postmortem." not in harness.service.export_traces(project_id).to_json()


# ---------------------------------------------------------------------------
# The HTTP API
# ---------------------------------------------------------------------------


def test_an_article_can_be_fetched_in_each_format(
    api: TestClient, db_session: Session, snapshot_store: SnapshotStore
) -> None:
    _project(db_session)
    version = snapshot_store.write(
        artifact_type=ArtifactType.ARTICLE_VERSION,
        content=ARTICLE.model_dump_json().encode("utf-8"),
        created_by_execution_id="exec-api",
    )

    for fmt in ExportFormat:
        response = api.get(f"/versions/{version.id}/export", params={"format": fmt.value})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["format"] == fmt.value
        assert body["content_hash"] == version.content_hash
        assert "p99 latency fell from 810ms to 120ms." in body["content"]


def test_the_visibility_surface_is_available_to_the_ui(
    api: TestClient, db_session: Session
) -> None:
    """plan/13's exit criterion, checked at the boundary the UI actually calls."""
    project_id = _project(db_session)
    _flag(db_session, project_id)

    response = api.get(f"/projects/{project_id}/provider-visibility")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confidential_segments"] == 1
    assert body["stages"]
    assert body["retention_mode"]
    assert SECRET not in response.text


def test_exporting_a_confidential_trace_is_refused_over_http(
    api: TestClient, db_session: Session, harness: Harness
) -> None:
    """The refusal survives the trip.

    A guard that became a 200 with a warning field would be no guard at all: the
    bytes are in the response by the time anyone could read it.
    """
    project_id = _project(db_session)
    _flag(db_session, project_id)
    _recorded(harness, project_id)

    response = api.get(f"/projects/{project_id}/traces")

    assert response.status_code == 409, response.text
    assert SECRET not in response.text


def test_a_sanitised_trace_export_is_served(
    api: TestClient, db_session: Session, harness: Harness
) -> None:
    project_id = _project(db_session)
    _flag(db_session, project_id)
    _recorded(harness, project_id)

    response = api.get(f"/projects/{project_id}/traces", params={"sanitise": True})

    assert response.status_code == 200, response.text
    assert response.json()["sanitised"] is True
    assert "Summarise the postmortem." not in response.text


def test_a_trace_can_be_deleted_over_http(
    api: TestClient, db_session: Session, harness: Harness
) -> None:
    project_id = _project(db_session)
    _recorded(harness, project_id)

    response = api.delete(f"/projects/{project_id}/traces")

    assert response.status_code == 200, response.text
    assert response.json()["payloads"] > 0
