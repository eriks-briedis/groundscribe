"""Integrity of provenance payloads (phase 03).

Spec (plan/03 → Test-first specification): *snapshot hashes detect mutation* —
the phase-02 integrity check is re-used at the provenance boundary.

That is the point of storing the effective request and each response form as
content-addressed snapshots rather than as blob columns: a provenance record
that could be quietly edited on disk would be worth less than no record at all,
because it would be trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from groundscribe.provenance import queries
from groundscribe.provenance.enums import InvocationOutcome
from groundscribe.provenance.models import ModelInvocation
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest, Message
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder, seed_project

REQUEST = EffectiveRequest(
    template_id="extract_claims",
    template_version="1.0.0",
    rendered_prompt="the prompt that was actually sent",
    messages=[Message(role="user", content="the prompt that was actually sent")],
)


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    seed_project(db_session)
    return make_recorder(db_session, snapshot_store)


def _invocation(recorder: ProvenanceRecorder) -> ModelInvocation:
    run = recorder.start_run(project_id="p1")
    execution = recorder.start_stage(run, stage="extract_claims")
    return recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response={"claims": []},
    )


def test_a_stored_request_verifies_while_it_is_intact(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """Every payload snapshot the recorder writes verifies against its hash."""
    invocation = _invocation(recorder)
    for snapshot in (invocation.request_snapshot, invocation.raw_response_snapshot):
        assert snapshot is not None
        assert snapshot_store.verify(snapshot) is True


def test_editing_a_stored_request_on_disk_is_detected(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore, tmp_path: Path
) -> None:
    """A tampered prompt no longer matches its content hash.

    Content addressing is what makes this detectable at all: the record's address
    *is* the hash of its bytes, so editing the bytes invalidates the address.
    """
    invocation = _invocation(recorder)
    snapshot = invocation.request_snapshot
    assert snapshot is not None

    tampered = REQUEST.model_copy(update={"rendered_prompt": "a nicer prompt", "redacted": True})
    (tmp_path / snapshot.content_location).write_text(tampered.model_dump_json())

    assert snapshot_store.verify(snapshot) is False
    # The tampered content is still readable — detection, not prevention, is what
    # the hash gives; a caller who skips verify() gets the edited version.
    rebuilt = queries.reconstruct_effective_request(snapshot_store, invocation)
    assert rebuilt.rendered_prompt == "a nicer prompt"


def test_reconstruction_refuses_an_invocation_with_no_stored_request(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore, db_session: Session
) -> None:
    """Rebuilding what was never stored must fail rather than invent a request."""
    invocation = _invocation(recorder)
    invocation.request_snapshot = None
    db_session.flush()

    with pytest.raises(ValueError, match="no stored effective request"):
        queries.reconstruct_effective_request(snapshot_store, invocation)
