"""The engine: transitions with provenance and the guards that need it (phase 05).

Spec (plan/05), the invariants that cannot be checked without stored artefacts:

- every transition emits a ``DecisionRecord`` naming its triggering policy/actor,
  and a routing decision must identify its triggering policy or actor;
- every generated artefact references a creating execution — the transition is
  *rejected* otherwise;
- an approved architecture cannot change silently: changes require a new
  versioned snapshot plus an override record;
- every article version retains lineage;
- final export must use the version that passed validation;
- confidential source material cannot appear in publishable output (engine-level
  guard; full enforcement in phase 13);
- replays cannot overwrite original executions;
- failed executions retain their trace.

The engine is the machine plus a recorder. Rules that can be proved without a
database live in ``test_workflow_machine``; what is here is what genuinely needs
one.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance.enums import ActorType, ExecutionStatus
from groundscribe.provenance.queries import timeline
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.engine import Override, WorkflowEngine
from groundscribe.workflow.errors import (
    ArtifactProvenanceError,
    AttributionRequired,
    ConfidentialMaterialError,
    ExportMismatchError,
    LineageError,
    SilentMutationError,
)
from groundscribe.workflow.machine import RewriteApproval
from groundscribe.workflow.policy import FailureCategory
from groundscribe.workflow.stagnation import ScoreRound
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from provenance_helpers import make_recorder, seed_project
from workflow_helpers import fail_again, sample_policy

A = WorkflowAction
S = WorkflowState
C = FailureCategory


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    # The author is seeded under the id the tests attribute overrides to: an
    # intervention is a foreign key to a real user, not a free-text name.
    seed_project(db_session, user_id="ada")
    return make_recorder(db_session, snapshot_store)


def build_engine(
    recorder: ProvenanceRecorder,
    snapshots: SnapshotStore,
    *,
    state: WorkflowState = S.SOURCE_INGESTED,
    confidential: tuple[str, ...] = (),
) -> WorkflowEngine:
    run = recorder.start_run(project_id="p1")
    return WorkflowEngine(
        recorder=recorder,
        snapshots=snapshots,
        run=run,
        state=state,
        policy=sample_policy(),
        confidential=confidential,
    )


@pytest.fixture
def flow(recorder: ProvenanceRecorder, snapshot_store: SnapshotStore) -> WorkflowEngine:
    """The workflow engine under test.

    Named ``flow`` rather than ``engine`` because conftest already binds that
    name to the SQLAlchemy engine every other fixture hangs off.
    """
    return build_engine(recorder, snapshot_store)


def snapshot(
    flow: WorkflowEngine,
    snapshots: SnapshotStore,
    artifact_type: ArtifactType,
    content: bytes = b"{}",
    *,
    parent: ArtifactSnapshot | None = None,
    orphan: bool = False,
) -> ArtifactSnapshot:
    """A snapshot produced by the engine's execution, or a deliberate orphan."""
    return snapshots.write(
        artifact_type=artifact_type,
        content=content,
        created_by_execution_id=None if orphan else flow.execution.id,
        parent=parent,
    )


# ---------------------------------------------------------------------------
# Decision records
# ---------------------------------------------------------------------------


def test_every_transition_emits_a_decision_naming_its_policy(flow: WorkflowEngine) -> None:
    """plan/05: every transition emits a decision record naming its policy/actor."""
    recorded = flow.apply(A.EXTRACT_SOURCE_MODEL)
    decision = recorded.decision
    assert decision.decision_type == "workflow_transition"
    assert decision.decided_by_type is ActorType.POLICY
    assert decision.policy_version == "test-1"
    assert decision.outcome == S.SOURCE_MODEL_EXTRACTING.value
    assert decision.inputs["from"] == S.SOURCE_INGESTED.value
    assert decision.inputs["action"] == A.EXTRACT_SOURCE_MODEL.value


def test_a_transition_carries_the_edge_rationale_into_the_record(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """The table's stated reason for an edge is why the run took it."""
    flow = build_engine(recorder, snapshot_store, state=S.SUBSTANTIVE_REVIEWING)
    assert flow.apply(A.ACCEPT_REVIEW).decision.rationale


def test_a_user_transition_is_attributed_to_the_person(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    flow = build_engine(recorder, snapshot_store, state=S.ARCHITECTURE_REVIEW_REQUIRED)
    decision = flow.apply(
        A.APPROVE_ARCHITECTURE, actor_id="ada", actor_type=ActorType.USER
    ).decision
    assert decision.decided_by == "ada"
    assert decision.decided_by_type is ActorType.USER
    assert decision.policy_version is None


def test_a_user_transition_without_a_name_is_refused(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """Phase 03 will not store a decision nobody is accountable for."""
    flow = build_engine(recorder, snapshot_store, state=S.ARCHITECTURE_REVIEW_REQUIRED)
    with pytest.raises(AttributionRequired):
        flow.apply(A.APPROVE_ARCHITECTURE, actor_type=ActorType.USER)
    assert flow.state is S.ARCHITECTURE_REVIEW_REQUIRED


def test_transitions_appear_on_the_run_timeline(flow: WorkflowEngine, db_session: Session) -> None:
    flow.apply(A.EXTRACT_SOURCE_MODEL)
    flow.apply(A.COMPLETE_EXTRACTION)
    moves = [
        event
        for event in timeline(db_session, flow.run.correlation_id)
        if event.event_type == "workflow.transitioned"
    ]
    assert [event.payload["to"] for event in moves] == [
        S.SOURCE_MODEL_EXTRACTING.value,
        S.SOURCE_MODEL_READY.value,
    ]


def test_the_engine_offers_the_actions_of_its_current_state(flow: WorkflowEngine) -> None:
    assert flow.available_actions() == (A.CANCEL, A.EXTRACT_SOURCE_MODEL, A.FAIL)


# ---------------------------------------------------------------------------
# Routing and stagnation
# ---------------------------------------------------------------------------


def test_a_routing_decision_identifies_the_policy_that_made_it(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """plan/05: a routing decision must identify its triggering policy or actor."""
    flow = build_engine(recorder, snapshot_store, state=S.REVISION_REQUIRED)
    decision = flow.route(C.STYLE_ISSUE).decision
    assert decision.decision_type == "revision_routing"
    assert decision.decided_by_type is ActorType.POLICY
    assert decision.policy_version == "test-1"
    assert decision.inputs["category"] == C.STYLE_ISSUE.value
    assert decision.outcome == S.VOICE_ALIGNING.value


def test_an_approved_extra_round_is_attributed_to_the_approver(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """An overridden limit is the person's decision, not the policy's."""
    flow = build_engine(recorder, snapshot_store, state=S.REVISION_REQUIRED)
    for _ in range(3):
        flow.route(C.SUBSTANTIVE_ISSUE)
        fail_again(flow.machine)

    recorded = flow.route(
        C.SUBSTANTIVE_ISSUE, approval=RewriteApproval(approved_by="ada", reason="one more")
    )
    assert recorded.decision.decided_by == "ada"
    assert recorded.decision.decided_by_type is ActorType.USER
    assert recorded.decision.rationale == "one more"


def test_an_exhausted_limit_records_the_stall_and_asks_for_a_person(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore, db_session: Session
) -> None:
    flow = build_engine(recorder, snapshot_store, state=S.REVISION_REQUIRED)
    for _ in range(3):
        flow.route(C.SUBSTANTIVE_ISSUE)
        fail_again(flow.machine)

    recorded = flow.route(C.SUBSTANTIVE_ISSUE)
    assert recorded.route.escalated
    assert recorded.decision.outcome == S.STALLED.value
    assert recorded.decision.rationale
    assert any(
        event.event_type == "intervention.requested"
        for event in timeline(db_session, flow.run.correlation_id)
    )


def test_a_stagnation_stall_records_the_findings_that_caused_it(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """plan/05: stagnation routes to a human decision — with its evidence."""
    flow = build_engine(recorder, snapshot_store, state=S.REVISION_REQUIRED)
    history = [
        ScoreRound(ordinal=i, overall=score) for i, score in enumerate((70.0, 71.0, 71.5), 1)
    ]
    recorded = flow.check_stagnation(history)
    assert recorded.check.stalled
    assert recorded.decision is not None
    assert "no_improvement" in recorded.decision.inputs["signals"]
    assert flow.state is S.STALLED


def test_a_healthy_check_records_nothing_and_moves_nothing(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """A non-event is not a decision; recording one would bury the real ones."""
    flow = build_engine(recorder, snapshot_store, state=S.REVISION_REQUIRED)
    history = [
        ScoreRound(ordinal=i, overall=score) for i, score in enumerate((60.0, 70.0, 80.0), 1)
    ]
    recorded = flow.check_stagnation(history)
    assert recorded.decision is None
    assert flow.state is S.REVISION_REQUIRED


# ---------------------------------------------------------------------------
# Artefact guards
# ---------------------------------------------------------------------------


def test_an_artefact_without_a_creating_execution_blocks_the_transition(
    flow: WorkflowEngine, snapshot_store: SnapshotStore
) -> None:
    """plan/05: every generated artefact references a creating execution."""
    orphan = snapshot(flow, snapshot_store, ArtifactType.SOURCE_MODEL, orphan=True)
    with pytest.raises(ArtifactProvenanceError):
        flow.apply(A.EXTRACT_SOURCE_MODEL, artifacts=(orphan,))
    assert flow.state is S.SOURCE_INGESTED


def test_a_blocked_transition_records_no_decision(
    flow: WorkflowEngine, snapshot_store: SnapshotStore, db_session: Session
) -> None:
    """A guard that fired after the write would leave a decision for a move
    that never happened."""
    orphan = snapshot(flow, snapshot_store, ArtifactType.SOURCE_MODEL, orphan=True)
    before = len(timeline(db_session, flow.run.correlation_id))
    with pytest.raises(ArtifactProvenanceError):
        flow.apply(A.EXTRACT_SOURCE_MODEL, artifacts=(orphan,))
    assert len(timeline(db_session, flow.run.correlation_id)) == before


def test_an_attributed_artefact_is_linked_to_the_transition(
    flow: WorkflowEngine, snapshot_store: SnapshotStore
) -> None:
    """The artefacts a transition rested on are part of why it happened."""
    produced = snapshot(flow, snapshot_store, ArtifactType.SOURCE_MODEL)
    recorded = flow.apply(A.EXTRACT_SOURCE_MODEL, artifacts=(produced,))
    assert produced.id in recorded.decision.inputs["artifacts"]
    assert produced.id in {artifact.snapshot_id for artifact in flow.execution.artifacts}


# ---------------------------------------------------------------------------
# No silent mutation of an approved architecture
# ---------------------------------------------------------------------------


def approve_architecture(flow: WorkflowEngine, snapshots: SnapshotStore) -> ArtifactSnapshot:
    approved = snapshot(flow, snapshots, ArtifactType.CONTENT_ARCHITECTURE, b'{"v":1}')
    flow.apply(
        A.APPROVE_ARCHITECTURE,
        actor_id="ada",
        actor_type=ActorType.USER,
        artifacts=(approved,),
    )
    return approved


def test_an_approved_architecture_cannot_be_replaced_silently(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """plan/05: an approved architecture cannot change without a new snapshot
    and an override record."""
    flow = build_engine(recorder, snapshot_store, state=S.ARCHITECTURE_REVIEW_REQUIRED)
    approve_architecture(flow, snapshot_store)

    unrelated = snapshot(flow, snapshot_store, ArtifactType.CONTENT_ARCHITECTURE, b'{"v":2}')
    with pytest.raises(SilentMutationError):
        flow.apply(A.GENERATE_BRIEF, artifacts=(unrelated,))
    assert flow.state is S.ARCHITECTURE_APPROVED


def test_a_forked_architecture_still_needs_an_override_record(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """A new version is necessary but not sufficient; someone must own the change."""
    flow = build_engine(recorder, snapshot_store, state=S.ARCHITECTURE_REVIEW_REQUIRED)
    approved = approve_architecture(flow, snapshot_store)
    revised = snapshot(
        flow, snapshot_store, ArtifactType.CONTENT_ARCHITECTURE, b'{"v":2}', parent=approved
    )
    with pytest.raises(SilentMutationError):
        flow.apply(A.GENERATE_BRIEF, artifacts=(revised,))


def test_a_forked_architecture_with_an_override_is_allowed(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    flow = build_engine(recorder, snapshot_store, state=S.ARCHITECTURE_REVIEW_REQUIRED)
    approved = approve_architecture(flow, snapshot_store)
    revised = snapshot(
        flow, snapshot_store, ArtifactType.CONTENT_ARCHITECTURE, b'{"v":2}', parent=approved
    )
    recorded = flow.apply(
        A.GENERATE_BRIEF,
        artifacts=(revised,),
        override=Override(requested_by="ada", reason="the third article was out of scope"),
    )
    assert recorded.override is not None
    assert recorded.override.decision_type == "architecture_override"
    assert recorded.override.decided_by == "ada"
    assert flow.approved_architecture is not None
    assert flow.approved_architecture.id == revised.id


def test_an_override_cannot_launder_an_unrelated_architecture(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """Lineage is the evidence; an override without it is a replacement."""
    flow = build_engine(recorder, snapshot_store, state=S.ARCHITECTURE_REVIEW_REQUIRED)
    approve_architecture(flow, snapshot_store)
    unrelated = snapshot(flow, snapshot_store, ArtifactType.CONTENT_ARCHITECTURE, b'{"v":9}')
    with pytest.raises(SilentMutationError):
        flow.apply(
            A.GENERATE_BRIEF,
            artifacts=(unrelated,),
            override=Override(requested_by="ada", reason="trust me"),
        )


def test_an_unattributed_override_is_refused() -> None:
    with pytest.raises(ValueError, match="requested_by"):
        Override(requested_by="")


# ---------------------------------------------------------------------------
# Article lineage, validated export, confidential material
# ---------------------------------------------------------------------------


def test_a_successor_article_version_must_retain_its_lineage(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """plan/05: every article version retains lineage."""
    flow = build_engine(recorder, snapshot_store, state=S.DRAFT_GENERATING)
    first = snapshot(flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"draft":1}')
    flow.apply(A.SUBMIT_DRAFT, artifacts=(first,))
    flow.apply(A.REQUIRE_REVISION_PLAN)
    flow.apply(A.APPROVE_REVISION_PLAN, actor_id="ada", actor_type=ActorType.USER)

    orphaned = snapshot(flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"draft":2}')
    with pytest.raises(LineageError):
        flow.apply(A.SUBMIT_REWRITE, artifacts=(orphaned,))

    forked = snapshot(
        flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"draft":2}', parent=first
    )
    assert flow.apply(A.SUBMIT_REWRITE, artifacts=(forked,)).state is S.SUBSTANTIVE_REVIEWING


def test_export_must_use_the_version_that_passed_validation(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """plan/05: final export must use the version that passed validation."""
    flow = build_engine(recorder, snapshot_store, state=S.FINAL_VALIDATING)
    passed = snapshot(flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"final":1}')
    flow.apply(A.VALIDATION_PASSED, artifacts=(passed,))

    other = snapshot(
        flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"final":2}', parent=passed
    )
    with pytest.raises(ExportMismatchError):
        flow.apply(A.APPROVE_FINAL, actor_id="ada", actor_type=ActorType.USER, artifacts=(other,))
    assert flow.state is S.HUMAN_APPROVAL_REQUIRED

    completed = flow.apply(
        A.APPROVE_FINAL, actor_id="ada", actor_type=ActorType.USER, artifacts=(passed,)
    )
    assert completed.state is S.COMPLETED


def test_approving_an_export_with_no_validated_version_is_refused(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    flow = build_engine(recorder, snapshot_store, state=S.HUMAN_APPROVAL_REQUIRED)
    version = snapshot(flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"final":1}')
    with pytest.raises(ExportMismatchError):
        flow.apply(A.APPROVE_FINAL, actor_id="ada", actor_type=ActorType.USER, artifacts=(version,))


def test_confidential_material_cannot_reach_the_published_output(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """plan/05 engine-level guard; full enforcement lands in phase 13."""
    flow = build_engine(
        recorder, snapshot_store, state=S.FINAL_VALIDATING, confidential=("Project Zephyr",)
    )
    leaking = snapshot(
        flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"body":"as Project Zephyr showed"}'
    )
    flow.apply(A.VALIDATION_PASSED, artifacts=(leaking,))
    with pytest.raises(ConfidentialMaterialError, match="Project Zephyr"):
        flow.apply(A.APPROVE_FINAL, actor_id="ada", actor_type=ActorType.USER, artifacts=(leaking,))
    assert flow.state is S.HUMAN_APPROVAL_REQUIRED


def test_clean_output_passes_the_confidentiality_guard(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    flow = build_engine(
        recorder, snapshot_store, state=S.FINAL_VALIDATING, confidential=("Project Zephyr",)
    )
    clean = snapshot(
        flow, snapshot_store, ArtifactType.ARTICLE_VERSION, b'{"body":"a caching write-up"}'
    )
    flow.apply(A.VALIDATION_PASSED, artifacts=(clean,))
    assert (
        flow.apply(
            A.APPROVE_FINAL, actor_id="ada", actor_type=ActorType.USER, artifacts=(clean,)
        ).state
        is S.COMPLETED
    )


# ---------------------------------------------------------------------------
# Executions: replay and failure
# ---------------------------------------------------------------------------


def test_a_replay_creates_a_new_execution_linked_to_the_original(
    flow: WorkflowEngine, recorder: ProvenanceRecorder
) -> None:
    """plan/05: replays cannot overwrite original executions."""
    original = flow.begin_stage("extract_claims")
    recorder.complete_stage(original)

    replay = flow.replay(original, requested_by="ada")
    assert replay.id != original.id
    assert replay.parent_execution_id == original.id
    assert original.status is ExecutionStatus.SUCCEEDED
    assert original.completed_at is not None


def test_a_replay_is_recorded_as_the_requesters_decision(
    flow: WorkflowEngine, recorder: ProvenanceRecorder
) -> None:
    original = flow.begin_stage("extract_claims")
    recorder.complete_stage(original)
    flow.replay(original, requested_by="ada")
    decisions = [
        record
        for record in flow.execution.decision_records
        if record.decision_type == "execution_replay"
    ]
    assert [record.decided_by for record in decisions] == ["ada"]


def test_a_failed_execution_keeps_its_trace(flow: WorkflowEngine, db_session: Session) -> None:
    """plan/05: failed executions retain their trace."""
    stage = flow.begin_stage("draft_article")
    flow.fail(error_type="provider_error", error_message="the model timed out", execution=stage)

    assert stage.status is ExecutionStatus.FAILED
    assert flow.state is S.FAILED
    events = [
        event
        for event in timeline(db_session, flow.run.correlation_id)
        if event.stage_execution_id == stage.id
    ]
    assert {event.event_type for event in events} >= {"stage.started", "stage.failed"}


def test_a_stage_execution_belongs_to_the_run(flow: WorkflowEngine) -> None:
    stage = flow.begin_stage("extract_claims")
    assert stage.pipeline_run_id == flow.run.id
    assert stage.correlation_id == flow.run.correlation_id
