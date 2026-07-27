"""The stage contract and its wiring into the workflow (phase 06).

Spec (plan/06 → Deliverables, Implementation tasks 1 and 8): a ``PipelineStage``
protocol with ``PipelineContext`` and ``StageResult``, and each stage wired into
the state machine and provenance.

What is pinned here is the *contract*, not any particular stage: a stage declares
its name, its implementation version and the workflow edges either side of it;
the runner takes the entry edge, opens the execution, runs the stage, completes
it, and takes the exit edge carrying whatever the stage produced. That last part
is why the phase-05 artefact guards apply to real stage output rather than to
hand-built snapshots — a stage cannot emit an artefact with no creating execution
and still move the run.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.llm.routing import default_routing_policy
from groundscribe.prompts import PromptStore, prompts_root
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, ExecutionStatus
from groundscribe.stages.architecture import ARCHITECTURE_STAGE
from groundscribe.stages.base import PipelineContext, PipelineStage, StageResult, StageRunner
from groundscribe.stages.brief import BRIEF_STAGE
from groundscribe.stages.drafting import DRAFT_STAGE
from groundscribe.stages.extraction import EXTRACTION_STAGE
from groundscribe.stages.planning import PLAN_STAGE
from groundscribe.stages.questions import GAP_STAGE
from groundscribe.stages.review import REVIEW_STAGE
from groundscribe.stages.rewriting import REWRITE_STAGE
from groundscribe.stages.voice import VOICE_STAGE
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.errors import ArtifactProvenanceError
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from stage_helpers import build_context

#: Every stage that calls a model. Each one's name is simultaneously its prompt
#: template id and its routing key, so this tuple is the whole list of names that
#: must exist in three places at once. Later phases append to it as their stages
#: arrive — a stage added here without config is exactly the mistake this catches.
MODEL_STAGES = (
    EXTRACTION_STAGE,
    GAP_STAGE,
    ARCHITECTURE_STAGE,
    BRIEF_STAGE,
    DRAFT_STAGE,
    REVIEW_STAGE,
    PLAN_STAGE,
    REWRITE_STAGE,
    VOICE_STAGE,
)


class _RecordingStage:
    """A stage that writes one artefact through the recorder, as a real one does."""

    name: ClassVar[str] = "extract_source_truth"
    impl_version: ClassVar[str] = "1.0"
    entry_action: ClassVar[WorkflowAction | None] = WorkflowAction.EXTRACT_SOURCE_MODEL
    exit_action: ClassVar[WorkflowAction | None] = WorkflowAction.COMPLETE_EXTRACTION

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[str]:
        snapshot = context.recorder.record_output(
            execution,
            artifact_type=ArtifactType.SOURCE_MODEL,
            content={"claims": []},
            role="source_model",
        )
        return StageResult(value="done", outputs=(snapshot,))


class _UnprovenancedStage(_RecordingStage):
    """A stage that emits a snapshot nobody can trace back to an execution."""

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[str]:
        orphan = context.snapshots.write(artifact_type=ArtifactType.SOURCE_MODEL, content=b"{}")
        return StageResult(value="orphan", outputs=(orphan,))


class _ExplodingStage(_RecordingStage):
    """A stage that records something, then fails part-way through."""

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[str]:
        context.recorder.emit(
            event_type="stage.progress",
            actor_type=ActorType.SYSTEM,
            actor_id="test",
            execution=execution,
        )
        raise RuntimeError("the model said something unusable")


class _EdgelessStage(_RecordingStage):
    """A stage that produces artefacts without moving the run (source ingestion)."""

    entry_action: ClassVar[WorkflowAction | None] = None
    exit_action: ClassVar[WorkflowAction | None] = None


def test_a_stage_satisfies_the_protocol_and_a_partial_one_does_not() -> None:
    """The protocol is runtime-checkable, so duck-typing past it is caught in tests."""

    class _NotAStage:
        name = "nope"

    assert isinstance(_RecordingStage(), PipelineStage)
    assert not isinstance(_NotAStage(), PipelineStage)


async def test_the_runner_walks_the_workflow_around_the_stage(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Entry edge, execution, exit edge — the stage sits inside the machine."""
    context = build_context(db_session, snapshot_store)

    result = await StageRunner(context).run(_RecordingStage())

    assert result.value == "done"
    assert context.engine.state is WorkflowState.SOURCE_MODEL_READY
    actions = [outcome.action for outcome in context.engine.machine.history]
    assert actions == [WorkflowAction.EXTRACT_SOURCE_MODEL, WorkflowAction.COMPLETE_EXTRACTION]


async def test_the_stage_execution_records_the_stage_and_its_implementation_version(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 → stage-execution metadata includes the stage implementation version."""
    context = build_context(db_session, snapshot_store)

    result = await StageRunner(context).run(_RecordingStage())

    execution = result.execution
    assert execution is not None
    assert execution.stage == "extract_source_truth"
    assert execution.impl_version == "1.0"
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.pipeline_run_id == context.engine.run.id
    assert [artifact.snapshot_id for artifact in execution.outputs] == [
        snapshot.id for snapshot in result.outputs
    ]


async def test_the_produced_artefacts_are_handed_to_the_exit_transition(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The engine's guards run on what the stage produced, not on nothing.

    plan/05's invariant "every generated artefact references a creating
    execution" is only enforceable if the stage's outputs reach the transition, so
    a stage emitting an untraceable snapshot must not be able to move the run.
    """
    context = build_context(db_session, snapshot_store)

    with pytest.raises(ArtifactProvenanceError):
        await StageRunner(context).run(_UnprovenancedStage())

    assert context.engine.state is WorkflowState.SOURCE_MODEL_EXTRACTING


async def test_a_failing_stage_keeps_its_trace_and_does_not_advance_the_run(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/03 → failed executions retain their trace; the workflow stays put."""
    context = build_context(db_session, snapshot_store)

    with pytest.raises(RuntimeError, match="unusable"):
        await StageRunner(context).run(_ExplodingStage())

    failed = [e for e in context.engine.run.stage_executions if e.stage == "extract_source_truth"]
    assert len(failed) == 1
    assert failed[0].status is ExecutionStatus.FAILED
    assert failed[0].error_type == "RuntimeError"
    assert [event.event_type for event in failed[0].trace_events] == [
        "stage.started",
        "stage.progress",
        "stage.failed",
    ]
    assert context.engine.state is WorkflowState.SOURCE_MODEL_EXTRACTING


async def test_a_stage_may_declare_no_workflow_edges(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Not every stage moves the run: ingestion happens before extraction starts.

    The edges are optional so a stage that only produces artefacts (plan/06 §1
    source ingestion) uses the same contract as one that advances the machine,
    rather than needing a second protocol.
    """
    context = build_context(db_session, snapshot_store)

    result = await StageRunner(context).run(_EdgelessStage())

    assert result.execution is not None
    assert result.execution.status is ExecutionStatus.SUCCEEDED
    assert context.engine.state is WorkflowState.SOURCE_INGESTED
    assert context.engine.machine.history == []


def test_every_model_stage_ships_a_prompt_and_an_explicit_route() -> None:
    """A stage's name is its template id and its routing key; all three must agree.

    An unrouted stage does not fail — it silently resolves to the conservative
    default and records ``used_default``. That is the right behaviour at runtime
    and the wrong thing to discover in production, so the shipped config is held
    to naming every stage that calls a model.
    """
    prompts = PromptStore(prompts_root())
    routing = default_routing_policy()

    for stage in MODEL_STAGES:
        metadata = prompts.metadata(stage)
        assert metadata.current_version in metadata.versions
        template = prompts_root() / stage / f"{metadata.current_version}.jinja2"
        assert template.is_file(), f"{stage} declares {metadata.current_version} but has no file"

        resolved = routing.resolve(stage)
        assert resolved.used_default is False, f"{stage} is not routed explicitly"
        assert resolved.primary.model
        assert resolved.fallback is not None, f"{stage} has no fallback to degrade to"
