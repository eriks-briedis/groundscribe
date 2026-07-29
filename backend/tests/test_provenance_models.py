"""Provenance entity & hierarchy tests (phase 03).

Spec (plan/03 → Deliverables): the provenance hierarchy is
``PipelineRun → StageExecution → {InputSnapshot, ContextSelection,
ModelInvocation{...}, ToolInvocation, DecisionRecord, EvaluationRun,
UserIntervention, OutputArtifact, TraceEvents}``, modelled as typed rows with
real foreign keys rather than one unstructured event stream.

This module tests the *shape* of that substrate — parity, linkage, and the
constraints that hold regardless of who writes the rows. The behaviour of the
writer (redaction, retry chains, reconstruction) is tested against the recorder.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance import models, schemas
from groundscribe.provenance.enums import (
    ActorType,
    ArtifactDirection,
    ContextDisposition,
    ExecutionStatus,
    InterventionType,
    InvocationOutcome,
    RetryType,
    ToolInitiator,
)
from groundscribe.storage.snapshot_store import SnapshotStore

MOMENT = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _seed_owner(session: Session) -> None:
    """A user and project for the provenance records to hang from."""
    session.add(domain_models.User(id="u1", name="Ada", email="ada@example.com"))
    session.add(domain_models.Project(id="p1", user_id="u1", title="Caching write-up"))
    session.flush()


def _seed_hierarchy(session: Session) -> dict[str, BaseModel]:
    """Persist one of every provenance record, wired into a single run.

    Returns the originating Pydantic instances keyed by entity name, so a parity
    round-trip can reload each row and validate it back to an equal schema.
    """
    _seed_owner(session)
    originals: dict[str, BaseModel] = {}

    def add(name: str, schema: BaseModel, orm: object) -> None:
        session.add(orm)
        originals[name] = schema

    run = schemas.PipelineRun(
        id="run1",
        project_id="p1",
        status=ExecutionStatus.RUNNING,
        correlation_id="corr-1",
        runtime_config={"provider": "fake", "temperature": 0.2},
        started_at=MOMENT,
    )
    add("PipelineRun", run, models.PipelineRun(**run.model_dump()))

    execution = schemas.StageExecution(
        id="exec1",
        pipeline_run_id="run1",
        stage="extract_claims",
        ordinal=0,
        status=ExecutionStatus.RUNNING,
        correlation_id="corr-1",
        started_at=MOMENT,
    )
    add("StageExecution", execution, models.StageExecution(**execution.model_dump()))

    selection = schemas.ContextSelection(
        id="ctx1",
        stage_execution_id="exec1",
        strategy="recent-segments",
        strategy_version="1.0.0",
        token_budget=4096,
    )
    add("ContextSelection", selection, models.ContextSelection(**selection.model_dump()))

    item = schemas.ContextItem(
        id="ctxi1",
        context_selection_id="ctx1",
        ordinal=0,
        reference="seg-1",
        disposition=ContextDisposition.SELECTED,
        reason="highest relevance",
        score=0.91,
    )
    add("ContextItem", item, models.ContextItem(**item.model_dump()))

    invocation = schemas.ModelInvocation(
        id="inv1",
        stage_execution_id="exec1",
        attempt_ordinal=1,
        outcome=InvocationOutcome.ACCEPTED,
        provider="fake",
        model="fake-1",
        template_id="extract_claims",
        template_version="1.0.0",
        request_snapshot_id=None,
        started_at=MOMENT,
    )
    add("ModelInvocation", invocation, models.ModelInvocation(**invocation.model_dump()))

    tool = schemas.ToolInvocation(
        id="tool1",
        stage_execution_id="exec1",
        model_invocation_id="inv1",
        tool_name="fetch_url",
        tool_version="2.1.0",
        initiator=ToolInitiator.MODEL_SELECTED,
        approval_required=True,
        approved_by="u1",
        raw_args={"url": "https://example.test/a"},
        normalised_args={"url": "https://example.test/a"},
        raw_result={"status": 200},
        normalised_result={"ok": True},
        status=ExecutionStatus.SUCCEEDED,
        started_at=MOMENT,
    )
    add("ToolInvocation", tool, models.ToolInvocation(**tool.model_dump()))

    decision = schemas.DecisionRecord(
        id="dec1",
        stage_execution_id="exec1",
        decision_type="route",
        decided_by="routing-policy",
        decided_by_type=ActorType.POLICY,
        policy_version="3.2.0",
        inputs={"score": 0.71},
        outcome="rewrite",
        rationale="below acceptance threshold",
        decided_at=MOMENT,
    )
    add("DecisionRecord", decision, models.DecisionRecord(**decision.model_dump()))

    evaluation = schemas.EvaluationRun(
        id="eval1",
        stage_execution_id="exec1",
        evaluator_id="accuracy-rubric",
        evaluator_version="1.1.0",
        rubric_version="1.1.0",
        scores={"accuracy": 0.82},
        passed=True,
        created_at=MOMENT,
    )
    add("EvaluationRun", evaluation, models.EvaluationRun(**evaluation.model_dump()))

    intervention = schemas.UserIntervention(
        id="int1",
        stage_execution_id="exec1",
        user_id="u1",
        intervention_type=InterventionType.APPROVAL,
        payload={"note": "looks right"},
        occurred_at=MOMENT,
    )
    add("UserIntervention", intervention, models.UserIntervention(**intervention.model_dump()))

    event = schemas.TraceEvent(
        id="ev1",
        pipeline_run_id="run1",
        stage_execution_id="exec1",
        event_type="stage.started",
        timestamp=MOMENT,
        actor_type=ActorType.SYSTEM,
        actor_id="pipeline",
        payload={"stage": "extract_claims"},
        correlation_id="corr-1",
        causation_id=None,
        sequence=0,
    )
    add("TraceEvent", event, models.TraceEvent(**event.model_dump()))

    experiment = schemas.ExperimentRun(
        id="exp1", name="prompt-a-vs-b", status=ExecutionStatus.PENDING, created_at=MOMENT
    )
    add("ExperimentRun", experiment, models.ExperimentRun(**experiment.model_dump()))

    session.flush()
    return originals


SCHEMA_FOR: dict[str, type[BaseModel]] = {
    "PipelineRun": schemas.PipelineRun,
    "StageExecution": schemas.StageExecution,
    "ContextSelection": schemas.ContextSelection,
    "ContextItem": schemas.ContextItem,
    "ModelInvocation": schemas.ModelInvocation,
    "ToolInvocation": schemas.ToolInvocation,
    "DecisionRecord": schemas.DecisionRecord,
    "EvaluationRun": schemas.EvaluationRun,
    "UserIntervention": schemas.UserIntervention,
    "TraceEvent": schemas.TraceEvent,
    "ExperimentRun": schemas.ExperimentRun,
}


def test_every_provenance_record_round_trips_schema_to_row(db_session: Session) -> None:
    """Each provenance schema reloads from its row without loss, version included."""
    originals = _seed_hierarchy(db_session)
    db_session.commit()

    assert set(originals) == set(SCHEMA_FOR), "seeded records and schema map disagree"
    for name, original in originals.items():
        row = db_session.get(getattr(models, name), original.id)  # type: ignore[attr-defined]
        assert row is not None, f"{name} row missing after commit"
        reloaded = SCHEMA_FOR[name].model_validate(row)
        assert reloaded == original, f"{name} did not round-trip losslessly"
        assert reloaded.schema_version == 1  # type: ignore[attr-defined]


def test_the_full_hierarchy_is_navigable_from_the_pipeline_run(db_session: Session) -> None:
    """A reader can walk run → execution → every child record by relationship.

    This is the difference between provenance-as-data and provenance-as-logs: the
    question "what produced this?" is answered by traversal, not by grep.
    """
    _seed_hierarchy(db_session)
    db_session.commit()

    run = db_session.get(models.PipelineRun, "run1")
    assert run is not None
    assert [e.id for e in run.stage_executions] == ["exec1"]

    execution = run.stage_executions[0]
    assert execution.pipeline_run is run
    assert [c.id for c in execution.context_selections] == ["ctx1"]
    assert [i.id for i in execution.model_invocations] == ["inv1"]
    assert [t.id for t in execution.tool_invocations] == ["tool1"]
    assert [d.id for d in execution.decision_records] == ["dec1"]
    assert [e.id for e in execution.evaluation_runs] == ["eval1"]
    assert [u.id for u in execution.user_interventions] == ["int1"]
    assert [t.id for t in execution.trace_events] == ["ev1"]
    assert [i.id for i in execution.context_selections[0].items] == ["ctxi1"]


def test_stage_execution_records_input_and_output_artifacts(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Inputs consumed and artefacts produced are both attached to the execution."""
    _seed_hierarchy(db_session)
    source = snapshot_store.write(artifact_type=ArtifactType.SOURCE_MODEL, content=b"source text")
    produced = snapshot_store.write(artifact_type=ArtifactType.SOURCE_MODEL, content=b"claims json")

    execution = db_session.get(models.StageExecution, "exec1")
    assert execution is not None
    db_session.add(
        models.ExecutionArtifact(
            id="ea-in",
            stage_execution_id="exec1",
            snapshot_id=source.id,
            direction=ArtifactDirection.INPUT,
            role="source_document",
            ordinal=0,
        )
    )
    db_session.add(
        models.ExecutionArtifact(
            id="ea-out",
            stage_execution_id="exec1",
            snapshot_id=produced.id,
            direction=ArtifactDirection.OUTPUT,
            role="source_model",
            ordinal=0,
        )
    )
    db_session.commit()

    db_session.refresh(execution)
    assert [a.snapshot_id for a in execution.inputs] == [source.id]
    assert [a.snapshot_id for a in execution.outputs] == [produced.id]
    assert execution.inputs[0].role == "source_document"


def test_model_invocation_stores_typed_attempts_not_a_retry_count(db_session: Session) -> None:
    """Attempts are ordered child rows; a bare counter column must not exist.

    plan/03: retries are "ordered child invocations ... not a bare count". A
    ``retry_count`` column would make the cheap, uninformative modelling possible
    again, so its absence is asserted.
    """
    columns = {c.key for c in inspect(models.ModelInvocation).columns}
    assert "retry_count" not in columns
    assert "retries" not in columns
    assert {"parent_invocation_id", "attempt_ordinal", "retry_type"} <= columns


def test_policy_decisions_cannot_be_stored_without_a_policy_version(db_session: Session) -> None:
    """A decision by a versioned policy must name the version, enforced in the DB.

    plan/03: "no decision may be stored without ``decided_by`` and (for policy
    decisions) ``policy_version``". Enforcing it as a CHECK means the guarantee
    survives any writer, not just the recorder's happy path.
    """
    _seed_hierarchy(db_session)
    db_session.add(
        models.DecisionRecord(
            id="dec-bad",
            stage_execution_id="exec1",
            decision_type="route",
            decided_by="routing-policy",
            decided_by_type=ActorType.POLICY,
            policy_version=None,
            inputs={},
            outcome="rewrite",
            decided_at=MOMENT,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_decision_schema_rejects_a_policy_decision_without_a_version() -> None:
    """The same rule holds in the schema, so it fails before a round trip."""
    with pytest.raises(ValidationError, match="policy_version"):
        schemas.DecisionRecord(
            id="dec-bad",
            stage_execution_id="exec1",
            decision_type="route",
            decided_by="routing-policy",
            decided_by_type=ActorType.POLICY,
            inputs={},
            outcome="rewrite",
            decided_at=MOMENT,
        )


def test_non_policy_decisions_do_not_need_a_policy_version(db_session: Session) -> None:
    """A human decision names the human; there is no policy to version."""
    _seed_hierarchy(db_session)
    db_session.add(
        models.DecisionRecord(
            id="dec-user",
            stage_execution_id="exec1",
            decision_type="approve_brief",
            decided_by="u1",
            decided_by_type=ActorType.USER,
            inputs={},
            outcome="approved",
            decided_at=MOMENT,
        )
    )
    db_session.flush()
    stored = db_session.get(models.DecisionRecord, "dec-user")
    assert stored is not None and stored.policy_version is None


def test_invocation_attempts_chain_to_their_parent(db_session: Session) -> None:
    """A repair attempt is a child of the invocation it repairs, with its own type."""
    _seed_hierarchy(db_session)
    db_session.add(
        models.ModelInvocation(
            id="inv2",
            stage_execution_id="exec1",
            parent_invocation_id="inv1",
            attempt_ordinal=2,
            retry_type=RetryType.INVALID_SCHEMA,
            outcome=InvocationOutcome.ACCEPTED,
            provider="fake",
            model="fake-1",
            template_id="extract_claims",
            template_version="1.0.0",
            started_at=MOMENT,
        )
    )
    db_session.commit()

    root = db_session.get(models.ModelInvocation, "inv1")
    assert root is not None
    assert [child.id for child in root.attempts] == ["inv2"]
    assert root.attempts[0].parent is root


def test_tool_result_dependencies_link_to_the_artifacts_that_used_them(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A tool result records which later artefacts depended on it."""
    _seed_hierarchy(db_session)
    derived = snapshot_store.write(
        artifact_type=ArtifactType.SOURCE_MODEL, content=b"claims citing the fetch"
    )

    tool = db_session.get(models.ToolInvocation, "tool1")
    assert tool is not None
    tool.dependents.append(derived)
    db_session.commit()

    db_session.refresh(tool)
    assert [d.id for d in tool.dependents] == [derived.id]
