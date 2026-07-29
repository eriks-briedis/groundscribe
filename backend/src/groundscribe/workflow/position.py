"""Where a run has got to, stored so another process can pick it up (phase 09).

Phase 05 kept the workflow's position in the engine for the length of a call and
named this phase as the owner of persisting it. Two things forced the question
now: a worker runs a stage in a different process from the request that asked
for it, and a human approval arrives in a different request from the validation
that earned it.

**A row, not columns on ``pipeline_runs``.** The position could have lived on
the run — one row per run already exists — but the dependency would then run the
wrong way: ``pipeline_runs`` belongs to the provenance schema, and putting a
:class:`~groundscribe.workflow.states.WorkflowState` on it would make provenance
import the workflow. The workflow already imports provenance. This is the same
call phase 09 made for the jobs table, for the same reason.

**Ids, never copies.** The three artefact references are foreign keys to
snapshots. Copying the artefacts would create a second version of something the
snapshot store already owns immutably, and two copies of an immutable thing is
one copy too many.

**The two counter maps are JSON.** ``rounds`` and ``grants`` are small
``LimitKind → int`` mappings read as a whole and never joined on; two extra
tables would buy joins and nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import JSON as JSONColumn
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm import Session as SASession

from groundscribe.db import Base, enum_column
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.provenance import models
from groundscribe.workflow.engine import WorkflowEngine
from groundscribe.workflow.policy import LimitKind
from groundscribe.workflow.states import WorkflowState


class WorkflowPosition(Base):
    """One run's live position: its state, its ledger and what it has approved."""

    __tablename__ = "workflow_positions"
    __table_args__ = (
        # One position per run, enforced in the database. Two rows would mean
        # two answers to "where is this run?", and nothing could say which was
        # authoritative.
        UniqueConstraint("pipeline_run_id", name="uq_workflow_positions_run"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    pipeline_run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    state: Mapped[WorkflowState] = mapped_column(enum_column(WorkflowState), nullable=False)
    # The stage execution every transition of this run is recorded against.
    workflow_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=True
    )
    approved_architecture_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    article_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    validated_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    rounds: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    grants: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)

    pipeline_run: Mapped[models.PipelineRun] = relationship()
    workflow_execution: Mapped[models.StageExecution | None] = relationship()
    approved_architecture: Mapped[ArtifactSnapshot | None] = relationship(
        foreign_keys=[approved_architecture_id]
    )
    article_version: Mapped[ArtifactSnapshot | None] = relationship(
        foreign_keys=[article_version_id]
    )
    validated_version: Mapped[ArtifactSnapshot | None] = relationship(
        foreign_keys=[validated_version_id]
    )


def _default_id() -> str:
    return uuid.uuid4().hex


class PositionStore:
    """Reads and writes the position row, and moves an engine in and out of it."""

    def __init__(self, session: SASession, *, id_factory: Callable[[], str] | None = None) -> None:
        self._session = session
        self._new_id = id_factory or _default_id

    def open(
        self,
        run: models.PipelineRun,
        *,
        state: WorkflowState = WorkflowState.SOURCE_INGESTED,
    ) -> WorkflowPosition:
        """Record where a new run starts, so nothing downstream has to infer it."""
        position = WorkflowPosition(
            id=self._new_id(), pipeline_run_id=run.id, state=state, rounds={}, grants={}
        )
        self._session.add(position)
        self._session.flush()
        return position

    def load(self, run: models.PipelineRun) -> WorkflowPosition | None:
        """This run's position, or ``None`` if it was never opened."""
        return self._session.scalars(
            select(WorkflowPosition).where(WorkflowPosition.pipeline_run_id == run.id)
        ).one_or_none()

    def capture(self, position: WorkflowPosition, engine: WorkflowEngine) -> WorkflowPosition:
        """Write everything the engine learned back onto the row.

        The artefacts are assigned through their relationships rather than by
        writing the foreign-key columns. Writing the column alone leaves the
        loaded relationship pointing at the *previous* artefact until something
        expires it — so a position that had just approved a new architecture
        would keep handing the old one back to the guard that resumes from it.

        The ledger is copied by value: the engine's dictionary keeps moving
        after this returns, and a row holding a live reference to it would
        record a future the caller has not committed to yet.
        """
        position.state = engine.state
        position.workflow_execution = engine.execution
        position.approved_architecture = engine.approved_architecture
        position.article_version = engine.article_version
        position.validated_version = engine.validated_version
        ledger = engine.machine.ledger
        position.rounds = {kind.value: count for kind, count in ledger.rounds.items()}
        position.grants = {kind.value: count for kind, count in ledger.grants.items()}
        self._session.flush()
        return position

    def apply(self, position: WorkflowPosition, engine: WorkflowEngine) -> WorkflowEngine:
        """Re-seed a freshly built engine from the row.

        The guards' memory is restored *and* the ledger, because both bound
        behaviour rather than merely describing it: without the first, a
        replacement architecture slips past the silent-mutation guard; without
        the second, the 3/2/1 rewrite limits reset on every job and stop being
        limits at all.
        """
        engine.restore(
            architecture=position.approved_architecture,
            article_version=position.article_version,
            validated_version=position.validated_version,
        )
        ledger = engine.machine.ledger
        ledger.rounds = {LimitKind(key): int(value) for key, value in position.rounds.items()}
        ledger.grants = {LimitKind(key): int(value) for key, value in position.grants.items()}
        return engine


__all__ = ["PositionStore", "WorkflowPosition"]
