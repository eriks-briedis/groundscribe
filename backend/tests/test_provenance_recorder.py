"""Recorder behaviour tests (phase 03).

Spec (plan/03 → Test-first specification):

- **Redaction before persistence** — a secret injected into a payload is absent
  from the stored record, but the record still exists.
- **Tool invocations retain args + results** — name, version, raw and normalised
  args/results, timing, initiator, approval, and which later artefacts depended
  on the result.
- **Decision records name a policy or actor** — no decision without
  ``decided_by``, and no policy decision without ``policy_version``.
- Context-selection records carry candidate/selected/excluded/truncated plus the
  strategy version.

Also covers plan/00's *every artefact references a creating execution*, which is
listed there as a phase-03 provenance test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.db import Base
from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance import models
from groundscribe.provenance.enums import (
    ActorType,
    ArtifactDirection,
    ContextDisposition,
    ExecutionStatus,
    InterventionType,
    InvocationOutcome,
    ToolInitiator,
)
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import ContextCandidate, EffectiveRequest, Message
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder, seed_project

SECRET = "sk-live-supersecret0123456789"


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    seed_project(db_session)
    return make_recorder(db_session, snapshot_store)


def _stage(recorder: ProvenanceRecorder) -> models.StageExecution:
    run = recorder.start_run(project_id="p1", runtime_config={"provider": "fake"})
    return recorder.start_stage(run, stage="extract_claims")


def _request(prompt: str = "extract the claims") -> EffectiveRequest:
    return EffectiveRequest(
        template_id="extract_claims",
        template_version="1.0.0",
        rendered_prompt=prompt,
        messages=[Message(role="user", content=prompt)],
    )


def _everything_stored(session: Session, blob_root: Path) -> str:
    """Every persisted byte: all column values of all rows, plus every blob."""
    fragments: list[str] = []
    for table in Base.metadata.sorted_tables:
        for row in session.execute(select(table)):
            fragments.extend(str(value) for value in row)
    fragments.extend(path.read_text() for path in blob_root.rglob("*") if path.is_file())
    return "\n".join(fragments)


def _record_everything(recorder: ProvenanceRecorder, execution: models.StageExecution) -> None:
    """Write one of every record type, with the secret injected into each payload."""
    invocation = recorder.record_model_invocation(
        execution,
        request=_request(f"use the key {SECRET} to continue"),
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response=f'{{"echo":"{SECRET}"}}',
        parsed_response={"echo": SECRET},
        validated_response={"echo": SECRET},
    )
    tool = recorder.record_tool_invocation(
        execution,
        tool_name="fetch_url",
        tool_version="2.1.0",
        initiator=ToolInitiator.MODEL_SELECTED,
        raw_args={"authorization": f"Bearer {SECRET}"},
        normalised_args={"url": f"https://example.test/?api_key={SECRET}"},
        raw_result={"body": SECRET},
        normalised_result={"body": SECRET},
        status=ExecutionStatus.SUCCEEDED,
        model_invocation=invocation,
    )
    recorder.record_context_selection(
        execution,
        strategy="recent-segments",
        strategy_version="1.0.0",
        candidates=[
            ContextCandidate(
                reference="seg-1",
                disposition=ContextDisposition.SELECTED,
                reason=f"mentions {SECRET}",
            )
        ],
    )
    recorder.record_decision(
        execution,
        decision_type="route",
        decided_by="routing-policy",
        decided_by_type=ActorType.POLICY,
        policy_version="3.2.0",
        outcome="rewrite",
        inputs={"observed": SECRET},
    )
    recorder.record_evaluation(
        execution,
        evaluator_id="accuracy",
        evaluator_version="1.0.0",
        rubric_version="1.0.0",
        scores={"accuracy": 0.8, "note": SECRET},
        passed=True,
    )
    recorder.record_user_intervention(
        execution,
        user_id="u1",
        intervention_type=InterventionType.APPROVAL,
        payload={"comment": SECRET},
    )
    recorder.emit(
        event_type="stage.note",
        actor_type=ActorType.SYSTEM,
        actor_id="pipeline",
        execution=execution,
        payload={"detail": SECRET},
    )
    snapshot = recorder.record_output(
        execution,
        artifact_type=ArtifactType.SOURCE_MODEL,
        content={"claims": [{"text": SECRET}]},
        role="source_model",
    )
    recorder.record_tool_dependency(tool, snapshot)


def test_no_injected_secret_reaches_any_persisted_record(
    recorder: ProvenanceRecorder, db_session: Session, tmp_path: Path
) -> None:
    """The secret is absent from every column of every row and from every blob.

    Deliberately a whole-database sweep rather than a per-field assertion: the
    guarantee is that *no* write path leaks, and a per-field test would silently
    stop covering whichever path a later phase adds.
    """
    execution = _stage(recorder)
    _record_everything(recorder, execution)
    db_session.flush()

    stored = _everything_stored(db_session, tmp_path)
    assert SECRET not in stored
    assert "REDACTED" in stored


def test_the_records_still_exist_after_redaction(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Redaction removes secrets, not records — the other half of the guarantee."""
    execution = _stage(recorder)
    _record_everything(recorder, execution)
    db_session.flush()

    for model in (
        models.ModelInvocation,
        models.ToolInvocation,
        models.ContextSelection,
        models.DecisionRecord,
        models.EvaluationRun,
        models.UserIntervention,
        models.TraceEvent,
        models.ExecutionArtifact,
    ):
        assert db_session.execute(select(model)).scalars().all(), model.__name__


def test_redaction_preserves_the_shape_of_what_was_recorded(
    recorder: ProvenanceRecorder,
) -> None:
    """A redacted tool call still shows which keys were sent and what came back."""
    execution = _stage(recorder)
    tool = recorder.record_tool_invocation(
        execution,
        tool_name="fetch_url",
        tool_version="2.1.0",
        initiator=ToolInitiator.PIPELINE_MANDATED,
        raw_args={"url": "https://example.test/a", "api_key": SECRET},
        normalised_args={"url": "https://example.test/a"},
        raw_result={"status": 200, "body": "ok"},
        normalised_result={"ok": True},
        status=ExecutionStatus.SUCCEEDED,
    )
    assert set(tool.raw_args) == {"url", "api_key"}
    assert tool.raw_args["url"] == "https://example.test/a"
    assert SECRET not in str(tool.raw_args)
    assert tool.raw_result == {"status": 200, "body": "ok"}


def test_tool_invocations_retain_everything_needed_to_judge_them(
    recorder: ProvenanceRecorder,
) -> None:
    """Name, version, initiator, approval, both arg forms, both result forms, timing."""
    execution = _stage(recorder)
    tool = recorder.record_tool_invocation(
        execution,
        tool_name="fetch_url",
        tool_version="2.1.0",
        initiator=ToolInitiator.MODEL_SELECTED,
        raw_args={"url": "https://example.test/a"},
        normalised_args={"url": "https://example.test/a", "timeout": 30},
        raw_result={"status": 200},
        normalised_result={"ok": True},
        status=ExecutionStatus.SUCCEEDED,
        approval_required=True,
        approved_by="u1",
    )

    assert (tool.tool_name, tool.tool_version) == ("fetch_url", "2.1.0")
    assert tool.initiator is ToolInitiator.MODEL_SELECTED
    assert tool.approval_required is True
    assert tool.approved_by == "u1"
    assert tool.raw_args != tool.normalised_args
    assert tool.raw_result != tool.normalised_result
    assert tool.completed_at is not None
    assert tool.started_at <= tool.completed_at


def test_a_tool_result_records_which_artefacts_depended_on_it(
    recorder: ProvenanceRecorder,
) -> None:
    """ "If this fetch was wrong, what else is wrong?" must be answerable."""
    execution = _stage(recorder)
    tool = recorder.record_tool_invocation(
        execution,
        tool_name="fetch_url",
        tool_version="2.1.0",
        initiator=ToolInitiator.MODEL_SELECTED,
        raw_args={},
        normalised_args={},
        raw_result={"status": 200},
        normalised_result={"ok": True},
        status=ExecutionStatus.SUCCEEDED,
    )
    derived = recorder.record_output(
        execution,
        artifact_type=ArtifactType.SOURCE_MODEL,
        content={"claims": ["rests on the fetch"]},
    )
    recorder.record_tool_dependency(tool, derived)

    assert [d.id for d in tool.dependents] == [derived.id]


def test_context_selection_records_what_was_left_out_as_well_as_what_was_used(
    recorder: ProvenanceRecorder,
) -> None:
    """Excluded and truncated candidates are recorded, under a versioned strategy."""
    execution = _stage(recorder)
    selection = recorder.record_context_selection(
        execution,
        strategy="recent-segments",
        strategy_version="2.0.0",
        token_budget=4096,
        candidates=[
            ContextCandidate(reference="seg-1", disposition=ContextDisposition.SELECTED, score=0.9),
            ContextCandidate(
                reference="seg-2", disposition=ContextDisposition.EXCLUDED, reason="off topic"
            ),
            ContextCandidate(
                reference="seg-3", disposition=ContextDisposition.TRUNCATED, reason="over budget"
            ),
        ],
    )

    assert (selection.strategy, selection.strategy_version) == ("recent-segments", "2.0.0")
    assert selection.token_budget == 4096
    assert [item.reference for item in selection.items] == ["seg-1", "seg-2", "seg-3"]
    assert [item.ordinal for item in selection.items] == [0, 1, 2]
    assert [item.disposition for item in selection.items] == [
        ContextDisposition.SELECTED,
        ContextDisposition.EXCLUDED,
        ContextDisposition.TRUNCATED,
    ]
    assert selection.items[1].reason == "off topic"


def test_a_decision_must_name_who_made_it(recorder: ProvenanceRecorder) -> None:
    """An unattributed decision cannot be reviewed, so it cannot be stored."""
    execution = _stage(recorder)
    with pytest.raises(ValueError, match="decided_by"):
        recorder.record_decision(
            execution,
            decision_type="route",
            decided_by="",
            decided_by_type=ActorType.SYSTEM,
            outcome="rewrite",
        )


def test_a_policy_decision_must_name_its_policy_version(recorder: ProvenanceRecorder) -> None:
    """ "The policy decided" is not reproducible without knowing which policy."""
    execution = _stage(recorder)
    with pytest.raises(ValueError, match="policy_version"):
        recorder.record_decision(
            execution,
            decision_type="route",
            decided_by="routing-policy",
            decided_by_type=ActorType.POLICY,
            outcome="rewrite",
        )


def test_a_user_decision_needs_no_policy_version(recorder: ProvenanceRecorder) -> None:
    """A human decision names the human; there is no policy to version."""
    execution = _stage(recorder)
    decision = recorder.record_decision(
        execution,
        decision_type="approve_brief",
        decided_by="u1",
        decided_by_type=ActorType.USER,
        outcome="approved",
        rationale="scope looks right",
    )
    assert decision.policy_version is None
    assert decision.decided_by == "u1"


def test_every_recorded_artefact_references_its_creating_execution(
    recorder: ProvenanceRecorder,
) -> None:
    """plan/00: every artefact references a creating execution — asserted here.

    Phase 05 enforces it as a state-machine invariant; this is the provenance
    test plan/00 assigns to phase 03, covering the write path that exists now.
    """
    execution = _stage(recorder)
    snapshot = recorder.record_output(
        execution,
        artifact_type=ArtifactType.SOURCE_MODEL,
        content={"claims": []},
        role="source_model",
    )
    invocation = recorder.record_model_invocation(
        execution,
        request=_request(),
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response={"claims": []},
    )

    assert snapshot.created_by_execution_id == execution.id
    assert invocation.request_snapshot is not None
    assert invocation.request_snapshot.created_by_execution_id == execution.id
    assert invocation.raw_response_snapshot is not None
    assert invocation.raw_response_snapshot.created_by_execution_id == execution.id


def test_inputs_and_outputs_are_attached_with_direction_and_role(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """The execution records what it consumed as well as what it produced."""
    execution = _stage(recorder)
    source = snapshot_store.write(artifact_type=ArtifactType.SOURCE_DOCUMENT, content=b"raw notes")
    recorder.record_input(execution, source, role="source_document")
    recorder.record_output(
        execution, artifact_type=ArtifactType.SOURCE_MODEL, content={"claims": []}
    )

    assert [a.direction for a in execution.artifacts] == [
        ArtifactDirection.INPUT,
        ArtifactDirection.OUTPUT,
    ]
    assert execution.inputs[0].snapshot_id == source.id
    assert execution.inputs[0].role == "source_document"


def test_output_snapshots_can_supersede_a_parent_without_overwriting_it(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """A rewrite forks from its parent; the parent's bytes stay readable."""
    execution = _stage(recorder)
    first = recorder.record_output(
        execution, artifact_type=ArtifactType.ARTICLE_VERSION, content={"body": "draft one"}
    )
    second = recorder.record_output(
        execution,
        artifact_type=ArtifactType.ARTICLE_VERSION,
        content={"body": "draft two"},
        parent=first,
    )

    assert second.parent_snapshot_id == first.id
    assert b"draft one" in snapshot_store.read(first)


def test_the_default_clock_and_id_factory_produce_real_values(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Injection is for tests; production gets a wall clock and unique ids.

    Worth pinning because the defaults are the code path that actually ships and
    the one every other test replaces.
    """
    seed_project(db_session)
    plain = ProvenanceRecorder(db_session, snapshot_store)

    first = plain.start_run(project_id="p1")
    second = plain.start_run(project_id="p1")

    assert first.id != second.id
    assert first.correlation_id != second.correlation_id
    assert first.started_at.tzinfo is not None
    assert first.started_at <= second.started_at
