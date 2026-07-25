"""The stage contract: what an editorial stage is, and how it is run (phase 06).

plan/06 → *Deliverables*: ``PipelineStage`` + ``PipelineContext`` +
``StageResult``. Three types, with one job each:

- **``PipelineContext``** is the environment shared by every stage of a run: the
  workflow engine, the recorder, the snapshot store, the structured generator and
  the project the run is for. Built once per run and passed down, so a stage
  cannot reach around it to a session or a client of its own — which is what
  keeps every model call routed, recorded and redacted.
- **``StageResult``** is what a stage produced: the validated value plus the
  snapshots it wrote. The snapshots are part of the result rather than left in the
  database because the *runner* hands them to the exit transition, and phase 05's
  guards can only check artefacts they are given.
- **``PipelineStage``** is the protocol. A stage names itself, its implementation
  version, and the workflow edges either side of it.

The edges are declared by the stage and taken by the runner, not taken inside the
stage. A stage that moved the machine itself could move it on the way *out* of a
failure, and the ordering that makes the guards meaningful — guard, move, record —
would then be restated once per stage, which is once per opportunity to get it
wrong.

Both edges are optional: source ingestion produces artefacts before the run has
anywhere to move, and giving it the same contract as an advancing stage is
cheaper than a second protocol for the one stage that does not transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.llm.generation import StructuredGenerator
from groundscribe.provenance import models
from groundscribe.provenance.enums import ExecutionStatus
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import TokenUsage
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.engine import WorkflowEngine
from groundscribe.workflow.states import WorkflowAction


@dataclass(frozen=True)
class PipelineContext:
    """Everything a stage may use, and nothing it should reach around.

    Frozen and shared for the length of a run. The stage execution is *not* held
    here: it is per-invocation, and a context that carried one would have to be
    rebuilt for every stage — inviting a stage to record against the wrong
    execution by holding on to a stale copy.

    The session is here because editorial rows are the *stages'* to write, while
    provenance rows are the recorder's; a stage that had to reach into the recorder
    for a session would be reaching around the redaction chokepoint that owns it.
    """

    engine: WorkflowEngine
    recorder: ProvenanceRecorder
    snapshots: SnapshotStore
    generator: StructuredGenerator
    session: Session
    project_id: str
    constraints: EditorialConstraints
    actor_id: str = "pipeline"


@dataclass(frozen=True)
class StageResult[T]:
    """What one stage run produced.

    ``value`` is the stage's own validated output — a source model, an
    architecture proposal, a brief. ``outputs`` are the snapshots it persisted, in
    the order they were written, and they are what the runner hands to the exit
    transition.

    ``usage`` totals what the stage's model calls consumed, so cost is answerable
    per stage without summing invocations by hand.

    ``exit_action`` lets a stage choose its outgoing edge from what it *found*
    rather than declaring it up front. Gap analysis is the case that needs it:
    whether the run parks for the author or completes the extraction depends on
    whether anything blocking is missing, which is exactly what the stage
    computes. ``None`` means "take the edge the stage declared".

    ``execution`` is filled in by the runner, not by the stage. A stage is handed
    its execution and should not have to remember to hand it back, and a result
    naming an execution it did not run under would be worse than one naming none.
    """

    value: T
    outputs: tuple[ArtifactSnapshot, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    invocations: tuple[models.ModelInvocation, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    exit_action: WorkflowAction | None = None
    execution: models.StageExecution | None = None


@runtime_checkable
class PipelineStage[T](Protocol):
    """One editorial stage: schema-constrained work with a place in the workflow.

    Runtime-checkable so conformance is asserted in tests as well as by mypy —
    a protocol enforced only statically degrades the first time something is
    duck-typed past it.
    """

    @property
    def name(self) -> str:
        """The stage name recorded on its execution, e.g. ``extract_source_truth``."""
        ...

    @property
    def impl_version(self) -> str:
        """The build of this stage, recorded so behaviour changes are attributable."""
        ...

    @property
    def entry_action(self) -> WorkflowAction | None:
        """The edge taken before the stage runs, or ``None`` if it takes none."""
        ...

    @property
    def exit_action(self) -> WorkflowAction | None:
        """The edge taken after the stage succeeds, carrying its outputs."""
        ...

    async def run(
        self, context: PipelineContext, execution: models.StageExecution
    ) -> StageResult[T]:
        """Do the work, recording against ``execution``."""
        ...


class StageRunner:
    """Runs stages inside the workflow: transition in, execute, transition out.

    One object per run. It owns the order — entry edge, open execution, run,
    complete, exit edge with the outputs — because that order is what phase 05's
    guards depend on, and a stage that owned it would restate the ordering once
    per stage.
    """

    def __init__(self, context: PipelineContext) -> None:
        self._context = context

    async def run[T](self, stage: PipelineStage[T]) -> StageResult[T]:
        """Run one stage end to end, or leave the run where it was.

        A failure fails the *stage* and re-raises. It deliberately does not fail
        the run: whether a failed stage is retried, replayed against a different
        model, or abandoned is a decision for the caller (phase 09's worker), and
        a runner that ended the run would take that decision away.
        """
        context = self._context
        if stage.entry_action is not None:
            context.engine.apply(stage.entry_action, actor_id=context.actor_id)

        execution = context.engine.begin_stage(stage.name, impl_version=stage.impl_version)
        try:
            result = await stage.run(context, execution)
        except Exception as exc:
            # The generator already fails the stage when its ladder is exhausted;
            # failing it twice would stamp a second, later completion over the
            # first and lose when the stage actually stopped.
            if execution.status is ExecutionStatus.RUNNING:
                context.recorder.fail_stage(
                    execution, error_type=type(exc).__name__, error_message=str(exc)
                )
            raise

        context.recorder.complete_stage(execution)
        exit_action = result.exit_action or stage.exit_action
        if exit_action is not None:
            context.engine.apply(exit_action, actor_id=context.actor_id, artifacts=result.outputs)
        return replace(result, execution=execution)


__all__ = ["PipelineContext", "PipelineStage", "StageResult", "StageRunner"]
