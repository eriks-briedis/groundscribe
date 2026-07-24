"""Trace-event tests (phase 03).

Spec (plan/03 → Deliverables): ``TraceEvent`` is **append-only** and carries
``event_type, timestamp, actor_type, actor_id, payload, schema_version,
correlation_id, causation_id``.

Append-only is the property that makes a trace worth reading. A timeline that can
be edited after the fact proves nothing about what happened, so the guarantee is
enforced at the mapper — an UPDATE or DELETE against a stored event raises rather
than silently succeeding.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.provenance import queries
from groundscribe.provenance.enums import (
    ActorType,
    InterventionType,
    InvocationOutcome,
    ToolInitiator,
)
from groundscribe.provenance.enums import ExecutionStatus as Status
from groundscribe.provenance.models import AppendOnlyViolation, TraceEvent
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest, Message
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import START, make_recorder, seed_project

REQUEST = EffectiveRequest(
    template_id="extract_claims",
    template_version="1.0.0",
    rendered_prompt="a very distinctive prompt string",
    messages=[Message(role="user", content="a very distinctive prompt string")],
)


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    seed_project(db_session)
    return make_recorder(db_session, snapshot_store)


def test_a_stored_trace_event_cannot_be_updated(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Rewriting history must fail loudly, not quietly succeed."""
    run = recorder.start_run(project_id="p1")
    event = recorder.emit(
        event_type="note", actor_type=ActorType.SYSTEM, actor_id="pipeline", run=run
    )

    event.payload = {"tampered": True}
    with pytest.raises(AppendOnlyViolation):
        db_session.flush()


def test_a_stored_trace_event_cannot_be_deleted(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Deleting an event would let an actor erase the evidence of their own action."""
    run = recorder.start_run(project_id="p1")
    event = recorder.emit(
        event_type="note", actor_type=ActorType.SYSTEM, actor_id="pipeline", run=run
    )

    db_session.delete(event)
    with pytest.raises(AppendOnlyViolation):
        db_session.flush()


def test_events_of_one_run_share_a_correlation_and_are_totally_ordered(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """One correlation per run, and a stored sequence that does not rely on the clock."""
    run = recorder.start_run(project_id="p1")
    execution = recorder.start_stage(run, stage="extract_claims")
    recorder.emit(
        event_type="note",
        actor_type=ActorType.SYSTEM,
        actor_id="pipeline",
        execution=execution,
    )

    timeline = queries.timeline(db_session, run.correlation_id)
    assert len(timeline) >= 3
    assert {e.correlation_id for e in timeline} == {run.correlation_id}
    assert [e.sequence for e in timeline] == list(range(len(timeline)))


def test_a_causal_path_can_be_walked_back_to_its_root(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """``causation_id`` answers "what triggered this?", which order alone cannot."""
    run = recorder.start_run(project_id="p1")
    first = recorder.emit(
        event_type="user.requested", actor_type=ActorType.USER, actor_id="u1", run=run
    )
    second = recorder.emit(
        event_type="policy.routed",
        actor_type=ActorType.POLICY,
        actor_id="routing-policy",
        run=run,
        caused_by=first,
    )
    third = recorder.emit(
        event_type="stage.queued",
        actor_type=ActorType.SYSTEM,
        actor_id="pipeline",
        run=run,
        caused_by=second,
    )

    path = queries.causal_path(db_session, third)
    assert [e.id for e in path] == [first.id, second.id, third.id]
    assert first.causation_id is None


def test_a_causal_path_stops_at_a_cause_it_cannot_find(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """A dangling cause truncates the explanation instead of failing to give one.

    Trace retention (phase 13) will eventually age events out, and an event whose
    cause has been aged away is still worth explaining as far as it goes.
    """
    run = recorder.start_run(project_id="p1")
    orphan = TraceEvent(
        id="ev-orphan",
        pipeline_run_id=run.id,
        event_type="mystery",
        timestamp=START,
        actor_type=ActorType.SYSTEM,
        actor_id="pipeline",
        payload={},
        correlation_id=run.correlation_id,
        causation_id="an-event-that-is-not-here",
        sequence=99,
    )
    db_session.add(orphan)
    db_session.flush()

    assert [e.id for e in queries.causal_path(db_session, orphan)] == ["ev-orphan"]


def test_an_unanchored_event_is_refused(recorder: ProvenanceRecorder) -> None:
    """An event with no run cannot be correlated, so it cannot be part of a timeline."""
    with pytest.raises(ValueError, match="anchored"):
        recorder.emit(event_type="orphan", actor_type=ActorType.SYSTEM, actor_id="pipeline")


def test_the_lifecycle_writes_its_own_timeline(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Starting a run and a stage records the events; callers need not remember to.

    A trace that depends on every call site emitting correctly is a trace with
    holes in it exactly where something went wrong.
    """
    run = recorder.start_run(project_id="p1")
    recorder.start_stage(run, stage="extract_claims")

    types = [e.event_type for e in queries.timeline(db_session, run.correlation_id)]
    assert types == ["run.started", "stage.started"]


def test_recording_a_model_call_appends_an_event_that_references_it(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """The trace points at the record; it does not become a second copy of it.

    plan/03 keeps execution records out of one unstructured stream. An event that
    embedded the prompt would be exactly that stream, and would let the two
    copies drift.
    """
    run = recorder.start_run(project_id="p1")
    execution = recorder.start_stage(run, stage="extract_claims")
    invocation = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
    )

    timeline = queries.timeline(db_session, run.correlation_id)
    invoked = [e for e in timeline if e.event_type == "model.invoked"]
    assert len(invoked) == 1
    assert invoked[0].payload["model_invocation_id"] == invocation.id
    assert invoked[0].actor_type is ActorType.MODEL
    assert "a very distinctive prompt string" not in str(invoked[0].payload)


def test_tool_calls_decisions_and_interventions_each_append_an_event(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Everything a reader would ask "when did that happen?" about is on the timeline."""
    run = recorder.start_run(project_id="p1")
    execution = recorder.start_stage(run, stage="extract_claims")
    recorder.record_tool_invocation(
        execution,
        tool_name="fetch_url",
        tool_version="1.0.0",
        initiator=ToolInitiator.PIPELINE_MANDATED,
        raw_args={},
        normalised_args={},
        raw_result={},
        normalised_result={},
        status=Status.SUCCEEDED,
    )
    recorder.record_decision(
        execution,
        decision_type="route",
        decided_by="u1",
        decided_by_type=ActorType.USER,
        outcome="accept",
    )
    recorder.record_user_intervention(
        execution,
        user_id="u1",
        intervention_type=InterventionType.APPROVAL,
    )

    types = [e.event_type for e in queries.timeline(db_session, run.correlation_id)]
    assert types == [
        "run.started",
        "stage.started",
        "tool.invoked",
        "decision.recorded",
        "intervention.recorded",
    ]
