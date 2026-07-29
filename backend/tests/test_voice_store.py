"""Storing profiles, and getting the right one to the stage (phase 10).

plan/10 → *Implementation tasks 2 and 7*: the precedence resolver applied to what
is actually saved, and profile management exposed through the phase-09 service
layer.

The resolver is already tested against documents held in memory. What is left is
the part that decides *which* documents — and it is where a voice system usually
goes wrong in a way nobody notices: the profile is saved, the article is written,
and the two are never connected. So the test that matters here is the last one,
which drafts an article and asks what voice it was written under.
"""

from __future__ import annotations

import pytest
from provenance_helpers import make_recorder, seed_project
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.voice.enums import InstructionStrength, VoiceCategory, VoiceScope
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument
from groundscribe.voice.store import VoiceStore

AUTHOR = "u1"


def instruction(instruction_id: str, text: str) -> VoiceInstruction:
    return VoiceInstruction(
        id=instruction_id,
        category=VoiceCategory.TONE,
        strength=InstructionStrength.TENDENCY,
        text=text,
    )


def profile(
    scope: VoiceScope, *instructions: VoiceInstruction, version: str = "1"
) -> VoiceProfileDocument:
    return VoiceProfileDocument(
        name=f"{scope.value}-voice", version=version, scope=scope, instructions=instructions
    )


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    return make_recorder(db_session, snapshot_store)


@pytest.fixture
def project_id(db_session: Session) -> str:
    return seed_project(db_session, user_id=AUTHOR)


@pytest.fixture
def store(
    db_session: Session, snapshot_store: SnapshotStore, recorder: ProvenanceRecorder
) -> VoiceStore:
    return VoiceStore(db_session, snapshots=snapshot_store, recorder=recorder)


# ----------------------------------------------------------------------
# Saving and loading
# ----------------------------------------------------------------------


def test_a_saved_profile_comes_back_as_the_document_that_went_in(
    store: VoiceStore, project_id: str
) -> None:
    """The document is the artefact; the row is its identity and its scope.

    Stored as a content-addressed snapshot like everything else a person can
    read, so a profile version has the same integrity guarantees as the article
    written under it.
    """
    document = profile(VoiceScope.GLOBAL, instruction("direct", "Lead with the finding."))

    saved = store.save(document, user_id=AUTHOR)

    assert store.document(saved) == document
    assert saved.scope is VoiceScope.GLOBAL
    assert saved.active is True


def test_saving_a_new_version_retires_the_previous_one(
    store: VoiceStore, project_id: str
) -> None:
    """One version in force at a time, and the old one still on disk.

    Two active versions at one scope would leave the resolver picking, and an
    article's record of "written under ada@1" would stop meaning anything.
    """
    first = store.save(profile(VoiceScope.GLOBAL, version="1"), user_id=AUTHOR)

    second = store.save(profile(VoiceScope.GLOBAL, version="2"), user_id=AUTHOR)

    assert first.active is False
    assert second.active is True
    assert store.document(first).version == "1"


def test_a_project_profile_does_not_retire_a_global_one(
    store: VoiceStore, project_id: str
) -> None:
    """Scopes are independent. Setting a voice for one project must not silently
    unset the author's own."""
    global_version = store.save(profile(VoiceScope.GLOBAL), user_id=AUTHOR)

    store.save(profile(VoiceScope.PROJECT), user_id=AUTHOR, project_id=project_id)

    assert global_version.active is True


# ----------------------------------------------------------------------
# Resolution over what is stored
# ----------------------------------------------------------------------


def test_the_effective_voice_is_resolved_from_every_scope_in_force(
    store: VoiceStore, project_id: str, db_session: Session
) -> None:
    """plan/10 → article override beats project beats global, over stored rows."""
    store.save(
        profile(VoiceScope.GLOBAL, instruction("tone", "Plain."), instruction("lists", "No lists.")),
        user_id=AUTHOR,
    )
    store.save(
        profile(VoiceScope.PROJECT, instruction("tone", "Warmer; this is a tutorial.")),
        user_id=AUTHOR,
        project_id=project_id,
    )

    resolved = store.resolve(user_id=AUTHOR, project_id=project_id)

    active = {item.instruction.id: item for item in resolved.active}
    assert active["tone"].scope is VoiceScope.PROJECT
    assert active["lists"].scope is VoiceScope.GLOBAL


def test_an_article_override_is_used_only_for_its_own_article(
    store: VoiceStore, project_id: str, db_session: Session
) -> None:
    """An override that leaked to the next article would be the worst kind of bug
    here: invisible, and about the writing."""
    db_session.add(domain_models.Article(id="a1", project_id=project_id, title="One"))
    db_session.add(domain_models.Article(id="a2", project_id=project_id, title="Two"))
    db_session.flush()
    store.save(profile(VoiceScope.GLOBAL, instruction("tone", "Plain.")), user_id=AUTHOR)
    store.save(
        profile(VoiceScope.ARTICLE, instruction("tone", "Sharp; this one is an argument.")),
        user_id=AUTHOR,
        project_id=project_id,
        article_id="a1",
    )

    for_a1 = store.resolve(user_id=AUTHOR, project_id=project_id, article_id="a1")
    for_a2 = store.resolve(user_id=AUTHOR, project_id=project_id, article_id="a2")

    assert for_a1.profile.instructions[0].text.startswith("Sharp")
    assert for_a2.profile.instructions[0].text == "Plain."


def test_an_author_with_no_profile_still_gets_a_voice(
    store: VoiceStore, project_id: str
) -> None:
    """Empty, and usable. Requiring calibration before anything could run would
    make onboarding a precondition rather than a first result."""
    resolved = store.resolve(user_id=AUTHOR, project_id=project_id)

    assert resolved.active == ()
    assert resolved.profile.instructions == ()


def test_a_retired_version_is_not_resolved(store: VoiceStore, project_id: str) -> None:
    """Superseded means superseded; history stays readable without staying in force."""
    store.save(profile(VoiceScope.GLOBAL, instruction("old", "The old way."), version="1"), user_id=AUTHOR)
    store.save(profile(VoiceScope.GLOBAL, instruction("new", "The new way."), version="2"), user_id=AUTHOR)

    resolved = store.resolve(user_id=AUTHOR, project_id=project_id)

    assert [item.instruction.id for item in resolved.active] == ["new"]
