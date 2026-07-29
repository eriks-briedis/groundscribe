"""Exporting and deleting one project's trace (phase 13).

Spec (plan/13 → *Project-level trace export + deletion; sanitised trace export
with warnings before exporting confidential material; sanitised execution report*.
Test-first: *Excluded-from-trace export* — sanitised exports omit trace-excluded
content and warn before exporting any confidential material).

Three properties, in the order they matter.

**A warning arrives before the file does.** An export that produced confidential
material and mentioned it afterwards has already produced it: by the time the
warning is read, the bytes are on disk, in a chat window, or attached to a
ticket. So a full export of a project holding confidential material raises unless
the caller has said, in the call, that it knows.

**Sanitising removes content and keeps the record.** The execution still appears,
the model call still appears, the decision still appears; what a sanitised export
drops is the payload text. A trace that omitted the *fact* would be a different
kind of document — a smaller trace is useful, a wrong one is not — and that is
the same rule the retention modes hold to.

**Deletion is of content, not of history.** Trace events are append-only by
construction (phase 03 rejects updates and deletes at the mapper), so "delete my
traces" cannot mean "make it look like nothing ran", and it should not: the
record that a call happened is what makes cost and repair-rate numbers true.
What deletion removes is every stored payload, and it must not remove a blob a
*different* project still references — content addressing means two projects that
sent the same request share one blob.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.confidentiality import Confidentiality
from groundscribe.domain.enums import SegmentKind
from groundscribe.privacy.traces import (
    ConfidentialExportRefused,
    delete_traces,
    export_traces,
)
from groundscribe.provenance.enums import InvocationOutcome
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest
from groundscribe.storage.blob_store import BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder

PROMPT = "Summarise the March cache postmortem for a senior audience."

SUMMARY = "a read-through cache cut p99 latency"

RAW = f'{{"summary":"{SUMMARY}"}}'


def _project(session: Session, suffix: str, *, confidential: bool = False) -> str:
    user = domain_models.User(id=f"user-{suffix}", name="Ada", email="ada@example.com")
    project = domain_models.Project(id=f"proj-{suffix}", user_id=user.id, title="Postmortem")
    session.add_all([user, project])
    session.flush()
    if confidential:
        document = domain_models.SourceDocument(
            id=f"doc-{suffix}", project_id=project.id, title="Postmortem"
        )
        session.add(document)
        session.flush()
        session.add(
            domain_models.SourceSegment(
                id=f"seg-{suffix}",
                document_id=document.id,
                ordinal=0,
                text="Northwind threatened to terminate the contract over the outage.",
                kind=SegmentKind.PARAGRAPH,
                confidentiality=Confidentiality.CONFIDENTIAL,
            )
        )
        session.flush()
    return project.id


def _record(recorder: ProvenanceRecorder, project_id: str, *, prompt: str = PROMPT) -> None:
    """One run with one stage and one model call."""
    run = recorder.start_run(project_id=project_id)
    execution = recorder.start_stage(run, stage="extract_source_truth")
    recorder.record_model_invocation(
        execution,
        request=EffectiveRequest(
            template_id="extract_source_truth", template_version="1", rendered_prompt=prompt
        ),
        provider="ollama",
        model="llama3.1:70b-instruct",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response=RAW,
        parsed_response={"summary": "s"},
    )
    recorder.complete_stage(execution)


# ---------------------------------------------------------------------------
# What an export contains
# ---------------------------------------------------------------------------


def test_an_export_holds_this_project_s_runs_and_no_others(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Project-level means project-level."""
    recorder = make_recorder(db_session, snapshot_store)
    mine = _project(db_session, "mine")
    theirs = _project(db_session, "theirs")
    _record(recorder, mine)
    _record(recorder, theirs, prompt="A different project's prompt entirely.")

    exported = export_traces(db_session, snapshot_store, mine)

    assert len(exported.runs) == 1
    assert exported.project_id == mine
    assert "A different project's prompt entirely." not in exported.to_json()


def test_a_full_export_carries_the_payloads(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """It is a trace export; the prompt is most of what makes it worth having."""
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "full")
    _record(recorder, project_id)

    exported = export_traces(db_session, snapshot_store, project_id)

    assert PROMPT in exported.to_json()
    # The response is a JSON string inside a JSON document, so its quoting is
    # re-escaped; the sentence it contains is what a reader is looking for.
    assert SUMMARY in exported.to_json()
    assert not exported.sanitised


def test_a_sanitised_export_drops_the_payloads_and_keeps_the_calls(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The record survives sanitising; the content does not.

    A trace that omitted the *fact* of a call would be a wrong trace rather than
    a smaller one — the same rule the retention modes hold to.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "sanitised")
    _record(recorder, project_id)

    exported = export_traces(db_session, snapshot_store, project_id, sanitise=True)
    body = exported.to_json()

    assert exported.sanitised
    assert PROMPT not in body
    assert SUMMARY not in body
    assert "extract_source_truth" in body
    assert "llama3.1:70b-instruct" in body


def test_a_sanitised_export_says_that_something_was_removed(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Silence and absence look identical; only one of them is honest.

    A reader of a sanitised export must be able to tell "there was no prompt"
    from "the prompt was withheld".
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "marked")
    _record(recorder, project_id)

    exported = export_traces(db_session, snapshot_store, project_id, sanitise=True)

    assert "WITHHELD" in exported.to_json()
    assert exported.withheld_payloads == 3


# ---------------------------------------------------------------------------
# The warning
# ---------------------------------------------------------------------------


def test_a_project_with_confidential_material_is_flagged_before_export(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The warning is the point, and it has to come first."""
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "warned", confidential=True)
    _record(recorder, project_id)

    with pytest.raises(ConfidentialExportRefused) as raised:
        export_traces(db_session, snapshot_store, project_id)

    assert "confidential" in str(raised.value)


def test_the_refusal_does_not_quote_the_material(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An export refusal that printed the secret would be its own leak."""
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "quiet", confidential=True)
    _record(recorder, project_id)

    with pytest.raises(ConfidentialExportRefused) as raised:
        export_traces(db_session, snapshot_store, project_id)

    assert "Northwind" not in str(raised.value)


def test_a_caller_that_says_it_knows_gets_the_export(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A refusal with no way past it is a feature nobody can use.

    The acknowledgement is a keyword in the call rather than a mode set
    elsewhere, so it appears at the site a reviewer reads.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "ack", confidential=True)
    _record(recorder, project_id)

    exported = export_traces(
        db_session, snapshot_store, project_id, confidential_material_acknowledged=True
    )

    assert exported.warnings
    assert PROMPT in exported.to_json()


def test_a_sanitised_export_needs_no_acknowledgement(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Nothing is being carried out, so there is nothing to warn about.

    Making the safe path the one that never prompts is what stops the
    acknowledgement becoming a box people tick without reading.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "safe", confidential=True)
    _record(recorder, project_id)

    exported = export_traces(db_session, snapshot_store, project_id, sanitise=True)

    assert exported.sanitised
    assert PROMPT not in exported.to_json()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_deletion_removes_the_stored_payloads(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "deleted")
    _record(recorder, project_id)

    removed = delete_traces(db_session, snapshot_store, project_id)

    assert removed.payloads == 3
    exported = export_traces(db_session, snapshot_store, project_id)
    assert PROMPT not in exported.to_json()


def test_deletion_keeps_the_record_that_the_call_happened(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """ "Delete my traces" cannot mean "make it look like nothing ran".

    Trace events are append-only by construction (phase 03 rejects deletes at
    the mapper), and the record of a call is what makes every cost and
    repair-rate number computed from it true.
    """
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "kept")
    _record(recorder, project_id)

    delete_traces(db_session, snapshot_store, project_id)
    exported = export_traces(db_session, snapshot_store, project_id)

    assert len(exported.runs) == 1
    assert "llama3.1:70b-instruct" in exported.to_json()


def test_deletion_leaves_another_project_s_identical_payload_readable(
    db_session: Session, snapshot_store: SnapshotStore, tmp_path: Path
) -> None:
    """Content addressing means two projects can share one blob.

    Deleting one project's trace must not reach through the deduplication into
    another project's, which is the failure a naive "remove the file" would
    cause and which nothing would report.
    """
    recorder = make_recorder(db_session, snapshot_store)
    mine = _project(db_session, "sharer")
    theirs = _project(db_session, "sharee")
    _record(recorder, mine)
    _record(recorder, theirs)

    delete_traces(db_session, snapshot_store, mine)

    assert PROMPT in export_traces(db_session, snapshot_store, theirs).to_json()


def test_deletion_reports_what_it_did(db_session: Session, snapshot_store: SnapshotStore) -> None:
    """An irreversible operation that says nothing is one nobody can check."""
    recorder = make_recorder(db_session, snapshot_store)
    project_id = _project(db_session, "reported")
    _record(recorder, project_id)

    removed = delete_traces(db_session, snapshot_store, project_id)

    assert removed.project_id == project_id
    assert removed.payloads > 0
    assert removed.bytes_reclaimed > 0


def test_deleting_a_project_with_nothing_recorded_is_not_an_error(
    db_session: Session, snapshot_store: SnapshotStore, blob_store: BlobStore
) -> None:
    """Idempotent: running it twice is how people use a delete command."""
    project_id = _project(db_session, "empty")

    assert delete_traces(db_session, snapshot_store, project_id).payloads == 0
