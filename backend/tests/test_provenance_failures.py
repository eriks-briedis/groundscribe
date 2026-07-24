"""Failure-retention and branch-comparison tests (phase 03).

Spec (plan/03 → Test-first specification):

- **Failed executions retain their trace** — a simulated failure or cancellation
  preserves the partial ``StageExecution``, its invocations and its trace events.
- **Branch comparison references correct parent** — comparing two executions
  references each side's true parent.

The failure case is the one that matters most and is easiest to get wrong: the
natural implementation of "the stage failed" is to roll back the transaction,
which discards precisely the records needed to explain the failure.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.provenance import models, queries
from groundscribe.provenance.enums import ExecutionStatus, InvocationOutcome, RetryType
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest, Message
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder, seed_project

REQUEST = EffectiveRequest(
    template_id="draft_article",
    template_version="1.0.0",
    rendered_prompt="draft the article",
    messages=[Message(role="user", content="draft the article")],
)


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    seed_project(db_session)
    return make_recorder(db_session, snapshot_store)


def _stage_that_went_wrong(
    recorder: ProvenanceRecorder,
) -> tuple[models.PipelineRun, models.StageExecution]:
    """A stage with two attempts, the second still failing."""
    run = recorder.start_run(project_id="p1")
    execution = recorder.start_stage(run, stage="draft_article")
    first = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.TIMEOUT,
        error_message="deadline exceeded",
    )
    recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.PROVIDER_ERROR,
        parent=first,
        retry_type=RetryType.NETWORK,
        error_message="connection reset",
    )
    return run, execution


def test_a_failed_stage_keeps_its_partial_work(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Failure records an ending; it does not discard what led there."""
    run, execution = _stage_that_went_wrong(recorder)
    recorder.fail_stage(execution, error_type="ProviderError", error_message="connection reset")
    db_session.commit()

    stored = db_session.get(models.StageExecution, execution.id)
    assert stored is not None
    assert stored.status is ExecutionStatus.FAILED
    assert stored.error_type == "ProviderError"
    assert stored.error_message == "connection reset"
    assert stored.completed_at is not None

    # The attempts that led to the failure are exactly what a reader needs.
    assert len(stored.model_invocations) == 2
    assert [i.outcome for i in stored.model_invocations] == [
        InvocationOutcome.TIMEOUT,
        InvocationOutcome.PROVIDER_ERROR,
    ]
    types = [e.event_type for e in queries.timeline(db_session, run.correlation_id)]
    assert types[-1] == "stage.failed"
    assert "model.invoked" in types


def test_a_cancelled_stage_is_distinguishable_from_a_failed_one(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """A human stopping the work is not the system giving up, and reads differently."""
    run, execution = _stage_that_went_wrong(recorder)
    recorder.cancel_stage(execution, reason="author changed the brief")
    db_session.commit()

    stored = db_session.get(models.StageExecution, execution.id)
    assert stored is not None
    assert stored.status is ExecutionStatus.CANCELLED
    assert stored.model_invocations, "cancellation must not discard partial work"
    types = [e.event_type for e in queries.timeline(db_session, run.correlation_id)]
    assert types[-1] == "stage.cancelled"


def test_a_failed_run_records_the_failure_and_keeps_its_stages(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """The run-level ending is recorded too, without disturbing the stages below."""
    run, execution = _stage_that_went_wrong(recorder)
    recorder.fail_stage(execution, error_type="ProviderError", error_message="connection reset")
    recorder.fail_run(run, error_type="ProviderError", error_message="stage draft_article failed")
    db_session.commit()

    stored = db_session.get(models.PipelineRun, run.id)
    assert stored is not None
    assert stored.status is ExecutionStatus.FAILED
    assert stored.error_type == "ProviderError"
    assert [e.id for e in stored.stage_executions] == [execution.id]


def test_completing_a_stage_and_run_records_their_endings(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """The happy path is recorded as explicitly as the unhappy one."""
    run = recorder.start_run(project_id="p1")
    execution = recorder.start_stage(run, stage="draft_article")
    recorder.complete_stage(execution)
    recorder.complete_run(run)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.completed_at is not None
    assert run.status is ExecutionStatus.SUCCEEDED
    assert run.completed_at is not None
    types = [e.event_type for e in queries.timeline(db_session, run.correlation_id)]
    assert types == ["run.started", "stage.started", "stage.completed", "run.completed"]


def test_nothing_is_lost_when_the_session_is_reopened(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """The retained trace is on disk, not merely in the identity map."""
    run, execution = _stage_that_went_wrong(recorder)
    recorder.fail_stage(execution, error_type="ProviderError", error_message="connection reset")
    db_session.commit()
    # Ids are captured before the identity map is dropped, so the assertions
    # below can only be satisfied by rows that are genuinely on disk.
    execution_id, correlation_id = execution.id, run.correlation_id
    db_session.expunge_all()

    invocations = db_session.execute(
        select(models.ModelInvocation).where(
            models.ModelInvocation.stage_execution_id == execution_id
        )
    ).scalars()
    assert len({i.id for i in invocations}) == 2
    assert queries.timeline(db_session, correlation_id)


def test_comparing_two_branches_references_each_side_s_true_parent(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Two rewrites of one draft: each side's parent is the draft, not each other."""
    run = recorder.start_run(project_id="p1")
    draft = recorder.start_stage(run, stage="draft_article")
    left = recorder.start_stage(run, stage="rewrite", ordinal=1, parent=draft)
    right = recorder.start_stage(run, stage="rewrite", ordinal=2, parent=draft)

    comparison = queries.compare_executions(db_session, left, right)

    assert comparison.left_execution_id == left.id
    assert comparison.right_execution_id == right.id
    assert comparison.left_parent_id == draft.id
    assert comparison.right_parent_id == draft.id
    assert comparison.common_ancestor_id == draft.id


def test_branches_at_different_depths_report_their_own_parents(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """A rewrite of a rewrite reports its immediate parent, and the shared root."""
    run = recorder.start_run(project_id="p1")
    root = recorder.start_stage(run, stage="draft_article")
    first_rewrite = recorder.start_stage(run, stage="rewrite", ordinal=1, parent=root)
    second_rewrite = recorder.start_stage(run, stage="rewrite", ordinal=2, parent=first_rewrite)
    other = recorder.start_stage(run, stage="rewrite", ordinal=3, parent=root)

    comparison = queries.compare_executions(db_session, second_rewrite, other)

    assert comparison.left_parent_id == first_rewrite.id
    assert comparison.right_parent_id == root.id
    assert comparison.common_ancestor_id == root.id


def test_unrelated_executions_have_no_common_ancestor(
    recorder: ProvenanceRecorder, db_session: Session
) -> None:
    """Comparing across runs is answerable, and the answer is "nothing in common"."""
    run_a = recorder.start_run(project_id="p1")
    run_b = recorder.start_run(project_id="p1")
    left = recorder.start_stage(run_a, stage="draft_article")
    right = recorder.start_stage(run_b, stage="draft_article")

    comparison = queries.compare_executions(db_session, left, right)

    assert comparison.left_parent_id is None
    assert comparison.right_parent_id is None
    assert comparison.common_ancestor_id is None
