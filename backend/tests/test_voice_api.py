"""Managing a voice from outside the process (phase 10).

plan/10 → *Implementation task 7: expose profile management via the phase-09
service layer/API/CLI.*

The same rule phase 09 established applies unchanged here: routes translate,
services decide, and nothing that calls a model happens inside a request. The
one thing worth testing beyond that is the approval gate — it is the phase's
central promise, and an endpoint is exactly where a promise like that gets
quietly bypassed by a convenience.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from service_helpers import AUTHOR, Harness, build_harness
from sqlalchemy.orm import Session
from stage_helpers import DEFAULT_CONSTRAINTS

from groundscribe.api.app import create_app
from groundscribe.domain.enums import FindingStatus
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.voice.enums import InstructionStrength, VoiceCategory, VoiceScope

PROFILE: dict[str, Any] = {
    "name": "ada",
    "version": "1",
    "scope": VoiceScope.GLOBAL.value,
    "instructions": [
        {
            "id": "no-hype",
            "category": VoiceCategory.PROHIBITED_PATTERNS.value,
            "strength": InstructionStrength.HARD_RULE.value,
            "text": "Never call anything a game-changer.",
            "prohibits": ["game-changer"],
        },
        {
            "id": "direct",
            "category": VoiceCategory.TONE.value,
            "strength": InstructionStrength.TENDENCY.value,
            "text": "Lead with the finding.",
        },
    ],
}


@pytest.fixture
def harness(db_session: Session, snapshot_store: SnapshotStore) -> Harness:
    return build_harness(db_session, snapshot_store)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return TestClient(create_app(runtime_factory=lambda: harness.runtime))


def new_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "Read-through caching",
            "author_id": AUTHOR,
            "constraints": DEFAULT_CONSTRAINTS.model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    project_id: str = response.json()["project_id"]
    return project_id


# ----------------------------------------------------------------------
# Profiles
# ----------------------------------------------------------------------


def test_a_profile_can_be_saved_and_read_back(client: TestClient) -> None:
    """The minimum a person needs to own their voice from a client."""
    new_project(client)

    saved = client.post(f"/voice/profiles?user_id={AUTHOR}", json=PROFILE)
    read = client.get(f"/voice/profiles?user_id={AUTHOR}")

    assert saved.status_code == 201, saved.text
    assert saved.json()["scope"] == "global"
    assert [version["version"] for version in read.json()] == ["1"]


def test_the_effective_voice_says_where_each_instruction_came_from(
    client: TestClient,
) -> None:
    """plan/10 → *records the source + version of each active instruction*.

    Returned over the wire, because the question it answers — "why does it write
    like this, and where do I change it?" — is asked by a person looking at a
    screen, not by the resolver.
    """
    project_id = new_project(client)
    client.post(f"/voice/profiles?user_id={AUTHOR}", json=PROFILE)
    client.post(
        f"/voice/profiles?user_id={AUTHOR}&project_id={project_id}",
        json=PROFILE | {"scope": "project", "version": "2", "instructions": [PROFILE["instructions"][1]]},
    )

    effective = client.get(f"/projects/{project_id}/voice").json()

    sources = {item["instruction_id"]: item["source"] for item in effective["active"]}
    assert "global" in sources["no-hype"]
    assert "project" in sources["direct"]


def test_an_invalid_profile_is_rejected_before_it_is_stored(client: TestClient) -> None:
    """A hard rule with nothing to check for is not a hard rule.

    The schema refuses it, and the API surfaces the refusal as a 422 rather than
    storing something the voice pass could never enforce.
    """
    new_project(client)
    broken = PROFILE | {
        "instructions": [
            {
                "id": "be-good",
                "category": "tone",
                "strength": "hard_rule",
                "text": "Be good.",
            }
        ]
    }

    assert client.post(f"/voice/profiles?user_id={AUTHOR}", json=broken).status_code == 422


# ----------------------------------------------------------------------
# The gate, over HTTP
# ----------------------------------------------------------------------


def test_suggestions_are_listed_and_neither_applies_itself(
    client: TestClient, harness: Harness
) -> None:
    """plan/10 → nothing updates the permanent profile silently, endpoint included."""
    project_id = new_project(client)
    client.post(f"/voice/profiles?user_id={AUTHOR}", json=PROFILE)
    suggestion = seed_suggestion(harness, project_id)

    listed = client.get(f"/voice/suggestions?user_id={AUTHOR}").json()
    profiles_before = client.get(f"/voice/profiles?user_id={AUTHOR}").json()

    assert [item["id"] for item in listed] == [suggestion.id]
    assert [version["version"] for version in profiles_before] == ["1"]


def test_approving_a_suggestion_writes_a_new_profile_version(
    client: TestClient, harness: Harness
) -> None:
    """The one path that changes a voice, and it takes an actor."""
    project_id = new_project(client)
    client.post(f"/voice/profiles?user_id={AUTHOR}", json=PROFILE)
    suggestion = seed_suggestion(harness, project_id)

    approved = client.post(
        f"/voice/suggestions/{suggestion.id}/approve",
        json={"actor_id": AUTHOR, "version": "2"},
    )

    assert approved.status_code == 200, approved.text
    assert suggestion.status is FindingStatus.ACCEPTED
    versions = client.get(f"/voice/profiles?user_id={AUTHOR}").json()
    assert [version["version"] for version in versions] == ["1", "2"]


def test_rejecting_a_suggestion_changes_no_profile(
    client: TestClient, harness: Harness
) -> None:
    """A refusal is an answer, and it is recorded as one."""
    project_id = new_project(client)
    client.post(f"/voice/profiles?user_id={AUTHOR}", json=PROFILE)
    suggestion = seed_suggestion(harness, project_id)

    client.post(
        f"/voice/suggestions/{suggestion.id}/reject",
        json={"actor_id": AUTHOR, "reason": "I mean it in the launch posts."},
    )

    assert suggestion.status is FindingStatus.REJECTED
    assert [v["version"] for v in client.get(f"/voice/profiles?user_id={AUTHOR}").json()] == ["1"]


def test_an_unattributed_approval_is_refused(client: TestClient, harness: Harness) -> None:
    """Who made a rule permanent is the question this whole feature invites."""
    project_id = new_project(client)
    client.post(f"/voice/profiles?user_id={AUTHOR}", json=PROFILE)
    suggestion = seed_suggestion(harness, project_id)

    response = client.post(
        f"/voice/suggestions/{suggestion.id}/approve", json={"actor_id": "", "version": "2"}
    )

    assert response.status_code == 422
    assert suggestion.status is FindingStatus.PROPOSED


def seed_suggestion(harness: Harness, project_id: str) -> Any:
    """Three eligible edits and the suggestion they imply."""
    from sqlalchemy import select

    from groundscribe.domain import models as domain_models
    from groundscribe.provenance import models as provenance_models
    from groundscribe.voice.learning import VoiceLearning, detect_edit_patterns

    session = harness.runtime.session
    run = session.scalars(
        select(provenance_models.PipelineRun).where(
            provenance_models.PipelineRun.project_id == project_id
        )
    ).one()
    execution = harness.runtime.recorder.start_stage(run, stage="manual_edit")
    session.add(domain_models.Article(id="a1", project_id=project_id, title="Caching"))
    version = domain_models.ArticleVersion(
        id="v1", article_id="a1", ordinal=0, created_by_execution_id=execution.id
    )
    session.add(version)
    session.flush()

    learning = VoiceLearning(session, recorder=harness.runtime.recorder)
    for before, after in (
        ("The results were dramatic.", "The results were a 6x drop."),
        ("A dramatic reduction.", "A 40% reduction."),
        ("Dramatic latency gains.", "690ms of latency gains."),
    ):
        learning.record_edit(
            version=version, before=before, after=after, edited_by=AUTHOR, eligible=True
        )
    (pattern,) = detect_edit_patterns(learning.training_edits(AUTHOR))
    return learning.suggest(pattern, user_id=AUTHOR, category=VoiceCategory.LANGUAGE)
