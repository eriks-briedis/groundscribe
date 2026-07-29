"""Persisting a run's position across processes (phase 09).

Phase 05 deliberately kept the workflow's position in memory and said so:
"A run's state lives in the engine for the length of the call; phase 09 owns the
jobs table, the worker, and the resumption that needs it stored." This is that
resumption.

The property under test is not "the state round-trips" — a single column would
do that. It is that an engine rebuilt from the row *behaves* like the one that
was saved: its guards still fire, its rewrite ledger still counts, and it still
knows which version passed validation. A position that stored only the state
name would silently drop every one of those and look correct while doing it.

Each test therefore saves, throws the engine away, resumes, and then tries to do
something the original engine would have refused.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance.enums import ActorType, ExecutionStatus
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.engine import WorkflowEngine
from groundscribe.workflow.errors import ExportMismatchError, SilentMutationError
from groundscribe.workflow.policy import LimitKind
from groundscribe.workflow.position import PositionStore, WorkflowPosition
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from provenance_helpers import make_recorder, seed_project
from workflow_helpers import sample_policy

A = WorkflowAction
S = WorkflowState

AUTHOR = "ada"


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    seed_project(db_session, user_id=AUTHOR)
    return make_recorder(db_session, snapshot_store)


@pytest.fixture
def positions(db_session: Session) -> PositionStore:
    return PositionStore(db_session)


class Resumable:
    """A run whose engine can be thrown away and rebuilt from the database.

    Standing in for the process boundary the tests are about: the API enqueues a
    command in one process and a worker picks it up in another, with nothing
    shared between them but rows.
    """

    def __init__(
        self,
        recorder: ProvenanceRecorder,
        snapshots: SnapshotStore,
        positions: PositionStore,
        *,
        confidential: tuple[str, ...] = (),
    ) -> None:
        self._recorder = recorder
        self._snapshots = snapshots
        self._positions = positions
        self._confidential = confidential
        self.run = recorder.start_run(project_id="p1")
        self.position = positions.open(self.run)
        self.engine = self._build()

    def _build(self) -> WorkflowEngine:
        engine = WorkflowEngine(
            recorder=self._recorder,
            snapshots=self._snapshots,
            run=self.run,
            state=self.position.state,
            policy=sample_policy(),
            confidential=self._confidential,
            execution=self.position.workflow_execution,
        )
        self._positions.apply(self.position, engine)
        return engine

    def hand_over(self) -> WorkflowEngine:
        """Save what the engine knows, discard it, and rebuild from the row."""
        self._positions.capture(self.position, self.engine)
        self.engine = self._build()
        return self.engine

    def snapshot(
        self,
        artifact_type: ArtifactType,
        content: bytes = b"{}",
        *,
        parent: ArtifactSnapshot | None = None,
    ) -> ArtifactSnapshot:
        return self._snapshots.write(
            artifact_type=artifact_type,
            content=content,
            created_by_execution_id=self.engine.execution.id,
            parent=parent,
        )


@pytest.fixture
def resumable(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore, positions: PositionStore
) -> Resumable:
    return Resumable(recorder, snapshot_store, positions)


# ----------------------------------------------------------------------
# The position itself
# ----------------------------------------------------------------------


def test_a_new_run_starts_at_the_beginning(
    recorder: ProvenanceRecorder, positions: PositionStore
) -> None:
    """Opening a run records where it starts, so nothing has to infer it."""
    run = recorder.start_run(project_id="p1")

    position = positions.open(run)

    assert position.state is S.SOURCE_INGESTED
    assert position.pipeline_run_id == run.id
    assert positions.load(run) is position


def test_the_state_the_engine_reached_is_what_resumes(resumable: Resumable) -> None:
    """The obvious half: a run resumes where it was left, not where it began."""
    resumable.engine.apply(A.EXTRACT_SOURCE_MODEL)

    resumed = resumable.hand_over()

    assert resumable.position.state is S.SOURCE_MODEL_EXTRACTING
    assert resumed.state is S.SOURCE_MODEL_EXTRACTING


def test_resuming_keeps_the_run_on_one_workflow_execution(
    resumable: Resumable, db_session: Session
) -> None:
    """Every command of a run records against the same workflow execution.

    Opening a new one per command would scatter a run's transitions across a
    dozen executions and break the timeline an SSE stream reads. The execution
    id is part of the position for exactly that reason.
    """
    original = resumable.engine.execution

    resumed = resumable.hand_over()

    assert resumed.execution is original
    assert resumed.execution.status is ExecutionStatus.RUNNING


# ----------------------------------------------------------------------
# What a resumed engine must still refuse
# ----------------------------------------------------------------------


def test_a_resumed_engine_still_guards_the_approved_architecture(resumable: Resumable) -> None:
    """plan/05 → no approved architecture changes silently, across processes too.

    The guard compares a candidate against the architecture currently approved.
    A resumed engine that had forgotten which one that was would wave through
    precisely the replacement the guard exists to catch.
    """
    approved = resumable.snapshot(ArtifactType.CONTENT_ARCHITECTURE)
    resumable.engine.apply(A.EXTRACT_SOURCE_MODEL)
    resumable.engine.apply(A.COMPLETE_EXTRACTION)
    resumable.engine.apply(A.PROPOSE_ARCHITECTURE)
    resumable.engine.apply(A.SUBMIT_ARCHITECTURE)
    resumable.engine.apply(
        A.APPROVE_ARCHITECTURE, actor_id=AUTHOR, actor_type=ActorType.USER, artifacts=(approved,)
    )

    resumed = resumable.hand_over()
    replacement = resumable.snapshot(ArtifactType.CONTENT_ARCHITECTURE, b'{"v": 2}')

    assert resumed.approved_architecture is approved
    with pytest.raises(SilentMutationError):
        resumed.apply(A.GENERATE_BRIEF, artifacts=(replacement,))


def test_a_resumed_engine_still_knows_which_version_passed_validation(
    resumable: Resumable,
) -> None:
    """plan/05 → export must use the version that was checked.

    The strongest reason the position is more than a state name: approval is a
    *human* action taken in a later request than the validation that earned it,
    so the two never share an engine in a running system.
    """
    validated = resumable.snapshot(ArtifactType.ARTICLE_VERSION, b"the checked article")
    engine = resumable.engine
    for action in (
        A.EXTRACT_SOURCE_MODEL,
        A.COMPLETE_EXTRACTION,
        A.PROPOSE_ARCHITECTURE,
        A.SUBMIT_ARCHITECTURE,
    ):
        engine.apply(action)
    engine.apply(A.APPROVE_ARCHITECTURE, actor_id=AUTHOR, actor_type=ActorType.USER)
    engine.apply(A.GENERATE_BRIEF)
    engine.apply(A.SUBMIT_BRIEF)
    engine.apply(A.APPROVE_BRIEF, actor_id=AUTHOR, actor_type=ActorType.USER)
    engine.apply(A.SUBMIT_DRAFT)
    engine.apply(A.ACCEPT_REVIEW)
    engine.apply(A.SUBMIT_VOICE_PASS)
    engine.apply(A.SCORE_PASSED)
    engine.apply(A.VALIDATE_FINAL)
    engine.apply(A.VALIDATION_PASSED, artifacts=(validated,))

    resumed = resumable.hand_over()

    assert resumed.validated_version is validated
    # Forked from the validated version, so it clears the lineage guard and the
    # export guard is what stops it — which is the guard under test here. That
    # it clears lineage at all is the second half of the point: the resumed
    # engine also restored *which* version the run is currently working from.
    other = resumable.snapshot(
        ArtifactType.ARTICLE_VERSION, b"a different article", parent=validated
    )
    with pytest.raises(ExportMismatchError):
        resumed.apply(
            A.APPROVE_FINAL, actor_id=AUTHOR, actor_type=ActorType.USER, artifacts=(other,)
        )


def test_a_resumed_engine_remembers_the_rounds_already_spent(resumable: Resumable) -> None:
    """plan/05 → rewrite limits bound the *run*, not one process's memory of it.

    Every round of the revision loop is a separate worker job. A ledger that
    reset between them would make the 3/2/1 limits unenforceable in exactly the
    system they were written for.
    """
    engine = resumable.engine
    engine.machine.ledger.spend(LimitKind.SUBSTANTIVE)
    engine.machine.ledger.spend(LimitKind.SUBSTANTIVE)
    engine.machine.ledger.grant(LimitKind.ARCHITECTURE)

    resumed = resumable.hand_over()

    assert resumed.machine.ledger.spent(LimitKind.SUBSTANTIVE) == 2
    assert resumed.machine.ledger.approved(LimitKind.ARCHITECTURE) == 1
    assert resumed.machine.ledger.spent(LimitKind.STYLE) == 0


def test_the_position_row_names_the_artefacts_rather_than_copying_them(
    resumable: Resumable, db_session: Session
) -> None:
    """What is stored is snapshot ids, so nothing is duplicated or can diverge."""
    approved = resumable.snapshot(ArtifactType.CONTENT_ARCHITECTURE)
    resumable.engine.apply(A.EXTRACT_SOURCE_MODEL)
    resumable.engine.apply(A.COMPLETE_EXTRACTION)
    resumable.engine.apply(A.PROPOSE_ARCHITECTURE)
    resumable.engine.apply(A.SUBMIT_ARCHITECTURE)
    resumable.engine.apply(
        A.APPROVE_ARCHITECTURE, actor_id=AUTHOR, actor_type=ActorType.USER, artifacts=(approved,)
    )
    resumable.hand_over()
    db_session.flush()

    stored = db_session.get(WorkflowPosition, resumable.position.id)

    assert stored is not None
    assert stored.approved_architecture_id == approved.id
    assert stored.state is S.ARCHITECTURE_APPROVED
