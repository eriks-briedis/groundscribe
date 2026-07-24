"""SQLAlchemy ORM models for execution provenance (phase 03).

The typed substrate behind *observable provenance is part of the product*. Every
record here is a row with real foreign keys, not an entry in an event blob: the
question "which prompt, model call, tool result and decision produced this
paragraph?" must be answerable by traversal, and a single unstructured stream
cannot answer it without parsing.

Structure mirrors the spec's hierarchy::

    PipelineRun
      └── StageExecution
            ├── ExecutionArtifact (inputs consumed / outputs produced)
            ├── ContextSelection → ContextItem
            ├── ModelInvocation (self-chained retry/repair attempts)
            │     └── request / raw / parsed / validated response snapshots
            ├── ToolInvocation → dependent artefact snapshots
            ├── DecisionRecord
            ├── EvaluationRun
            ├── UserIntervention
            └── TraceEvent

Conventions follow phase 02: string primary keys, non-native value-stored enums
so the DDL is portable between SQLite and PostgreSQL, and a ``schema_version`` on
every row. Timestamps use :class:`~groundscribe.db.UTCDateTime` so the instant
recorded does not depend on the backend.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON as JSONColumn
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from groundscribe.db import Base, UTCDateTime
from groundscribe.domain.models import ArtifactSnapshot, Project, User
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


def _enum(enum_cls: type[Any]) -> Enum:
    """A portable, value-stored enum column type (as in the editorial models)."""
    return Enum(enum_cls, native_enum=False, values_callable=lambda e: [m.value for m in e])


class ProvenanceRecord:
    """Identity and version stamp shared by every execution record.

    Deliberately *not* the editorial ``EntityMixin``: these rows carry no
    ``created_by_execution_id`` because an execution record is not produced by an
    execution — it is the execution. The reference runs the other way.
    """

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# A tool result may be depended on by many artefacts, and an artefact may rest on
# several tool results; the link is what answers "if this fetch was wrong, what
# else is wrong?" (plan/03 → which later artefacts depended on the result).
tool_result_dependencies = Table(
    "tool_result_dependencies",
    Base.metadata,
    Column("tool_invocation_id", ForeignKey("tool_invocations.id"), primary_key=True),
    Column("snapshot_id", ForeignKey("artifact_snapshots.id"), primary_key=True),
)


class PipelineRun(ProvenanceRecord, Base):
    """One end-to-end execution of the editorial pipeline for a project."""

    __tablename__ = "pipeline_runs"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False
    )
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    runtime_config: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    project: Mapped[Project] = relationship()
    stage_executions: Mapped[list[StageExecution]] = relationship(
        back_populates="pipeline_run", order_by="StageExecution.ordinal"
    )


class StageExecution(ProvenanceRecord, Base):
    """One stage of a run — the anchor every other provenance record hangs from."""

    __tablename__ = "stage_executions"

    pipeline_run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    parent_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False
    )
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    pipeline_run: Mapped[PipelineRun] = relationship(back_populates="stage_executions")
    parent: Mapped[StageExecution | None] = relationship(
        back_populates="branches", remote_side="StageExecution.id"
    )
    branches: Mapped[list[StageExecution]] = relationship(back_populates="parent")
    artifacts: Mapped[list[ExecutionArtifact]] = relationship(
        back_populates="stage_execution", order_by="ExecutionArtifact.ordinal"
    )
    context_selections: Mapped[list[ContextSelection]] = relationship(
        back_populates="stage_execution"
    )
    model_invocations: Mapped[list[ModelInvocation]] = relationship(
        back_populates="stage_execution", order_by="ModelInvocation.attempt_ordinal"
    )
    tool_invocations: Mapped[list[ToolInvocation]] = relationship(back_populates="stage_execution")
    decision_records: Mapped[list[DecisionRecord]] = relationship(back_populates="stage_execution")
    evaluation_runs: Mapped[list[EvaluationRun]] = relationship(back_populates="stage_execution")
    user_interventions: Mapped[list[UserIntervention]] = relationship(
        back_populates="stage_execution"
    )
    trace_events: Mapped[list[TraceEvent]] = relationship(
        back_populates="stage_execution", order_by="TraceEvent.sequence"
    )

    @property
    def inputs(self) -> list[ExecutionArtifact]:
        """Snapshots this execution consumed."""
        return [a for a in self.artifacts if a.direction is ArtifactDirection.INPUT]

    @property
    def outputs(self) -> list[ExecutionArtifact]:
        """Snapshots this execution produced."""
        return [a for a in self.artifacts if a.direction is ArtifactDirection.OUTPUT]


class ExecutionArtifact(ProvenanceRecord, Base):
    """A snapshot consumed or produced by a stage execution.

    One table for both directions: it is the same fact read from either end, and
    two tables would duplicate the role/ordinal columns for no gain.
    """

    __tablename__ = "execution_artifacts"

    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("artifact_snapshots.id"), nullable=False)
    direction: Mapped[ArtifactDirection] = mapped_column(_enum(ArtifactDirection), nullable=False)
    role: Mapped[str] = mapped_column(String, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    stage_execution: Mapped[StageExecution] = relationship(back_populates="artifacts")
    snapshot: Mapped[ArtifactSnapshot] = relationship()


class ContextSelection(ProvenanceRecord, Base):
    """What was offered to a model, under which versioned strategy."""

    __tablename__ = "context_selections"

    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String, nullable=False)
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)

    stage_execution: Mapped[StageExecution] = relationship(back_populates="context_selections")
    items: Mapped[list[ContextItem]] = relationship(
        back_populates="context_selection", order_by="ContextItem.ordinal"
    )


class ContextItem(ProvenanceRecord, Base):
    """One context candidate and what became of it."""

    __tablename__ = "context_items"

    context_selection_id: Mapped[str] = mapped_column(
        ForeignKey("context_selections.id"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reference: Mapped[str] = mapped_column(String, nullable=False)
    disposition: Mapped[ContextDisposition] = mapped_column(
        _enum(ContextDisposition), nullable=False
    )
    reason: Mapped[str] = mapped_column(String, default="", nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    context_selection: Mapped[ContextSelection] = relationship(back_populates="items")


class ModelInvocation(ProvenanceRecord, Base):
    """A single call to a model, including the attempts that failed.

    There is no retry-count column by design (plan/03 → retries are ordered,
    typed child invocations): attempts chain through ``parent_invocation_id`` and
    each says *why* it exists via ``retry_type``.
    """

    __tablename__ = "model_invocations"

    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    parent_invocation_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_invocations.id"), nullable=True
    )
    attempt_ordinal: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    retry_type: Mapped[RetryType | None] = mapped_column(_enum(RetryType), nullable=True)
    outcome: Mapped[InvocationOutcome] = mapped_column(_enum(InvocationOutcome), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    template_id: Mapped[str] = mapped_column(String, nullable=False)
    template_version: Mapped[str] = mapped_column(String, nullable=False)
    # Three separate references, never one "response" column: a response that
    # parses but fails validation must survive next to its repaired successor.
    request_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    raw_response_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    parsed_response_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    validated_response_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    stage_execution: Mapped[StageExecution] = relationship(back_populates="model_invocations")
    request_snapshot: Mapped[ArtifactSnapshot | None] = relationship(
        foreign_keys=[request_snapshot_id]
    )
    raw_response_snapshot: Mapped[ArtifactSnapshot | None] = relationship(
        foreign_keys=[raw_response_snapshot_id]
    )
    parsed_response_snapshot: Mapped[ArtifactSnapshot | None] = relationship(
        foreign_keys=[parsed_response_snapshot_id]
    )
    validated_response_snapshot: Mapped[ArtifactSnapshot | None] = relationship(
        foreign_keys=[validated_response_snapshot_id]
    )
    parent: Mapped[ModelInvocation | None] = relationship(
        back_populates="attempts", remote_side="ModelInvocation.id"
    )
    attempts: Mapped[list[ModelInvocation]] = relationship(
        back_populates="parent", order_by="ModelInvocation.attempt_ordinal"
    )


class ToolInvocation(ProvenanceRecord, Base):
    """A tool call made during a stage, with everything needed to judge it."""

    __tablename__ = "tool_invocations"

    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    model_invocation_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_invocations.id"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    tool_version: Mapped[str] = mapped_column(String, nullable=False)
    initiator: Mapped[ToolInitiator] = mapped_column(_enum(ToolInitiator), nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # Raw is what crossed the boundary; normalised is what the pipeline acted on.
    # Bugs live in the gap between them, so both are kept.
    raw_args: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    normalised_args: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, default=dict, nullable=False
    )
    raw_result: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    normalised_result: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, default=dict, nullable=False
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    stage_execution: Mapped[StageExecution] = relationship(back_populates="tool_invocations")
    model_invocation: Mapped[ModelInvocation | None] = relationship()
    dependents: Mapped[list[ArtifactSnapshot]] = relationship(
        secondary=tool_result_dependencies, order_by="ArtifactSnapshot.id"
    )


class DecisionRecord(ProvenanceRecord, Base):
    """A routing/approval/selection decision and who or what made it."""

    __tablename__ = "decision_records"
    __table_args__ = (
        # Enforced in the database, not only in the writer: an unversioned policy
        # decision cannot be reviewed or reproduced, and the guarantee has to
        # survive migrations, admin scripts and future writers alike.
        CheckConstraint(
            "decided_by_type <> 'policy' OR policy_version IS NOT NULL",
            name="ck_decision_records_policy_version_present",
        ),
    )

    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    decision_type: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[str] = mapped_column(String, nullable=False)
    decided_by_type: Mapped[ActorType] = mapped_column(_enum(ActorType), nullable=False)
    policy_version: Mapped[str | None] = mapped_column(String, nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(String, default="", nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    stage_execution: Mapped[StageExecution] = relationship(back_populates="decision_records")


class EvaluationRun(ProvenanceRecord, Base):
    """A scoring pass over a stage's output, under a versioned rubric."""

    __tablename__ = "evaluation_runs"

    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    evaluator_id: Mapped[str] = mapped_column(String, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String, nullable=False)
    rubric_version: Mapped[str] = mapped_column(String, nullable=False)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    stage_execution: Mapped[StageExecution] = relationship(back_populates="evaluation_runs")


class UserIntervention(ProvenanceRecord, Base):
    """A point where a human stepped into the run."""

    __tablename__ = "user_interventions"

    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    intervention_type: Mapped[InterventionType] = mapped_column(
        _enum(InterventionType), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    stage_execution: Mapped[StageExecution] = relationship(back_populates="user_interventions")
    user: Mapped[User] = relationship()


class TraceEvent(ProvenanceRecord, Base):
    """An append-only event in the run's timeline.

    ``(correlation_id, sequence)`` is unique so the total order within a run is a
    stored fact rather than an artefact of clock resolution — two events written
    in the same microsecond still have a defined order, and a concurrent writer
    that would duplicate a position fails loudly instead of silently reordering
    history.
    """

    __tablename__ = "trace_events"
    __table_args__ = (
        UniqueConstraint("correlation_id", "sequence", name="uq_trace_events_correlation_seq"),
    )

    pipeline_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("pipeline_runs.id"), nullable=True
    )
    stage_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(_enum(ActorType), nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    pipeline_run: Mapped[PipelineRun | None] = relationship()
    stage_execution: Mapped[StageExecution | None] = relationship(back_populates="trace_events")


class ExperimentRun(ProvenanceRecord, Base):
    """Shell for the experimentation system; filled in phase 12."""

    __tablename__ = "experiment_runs"

    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Job(ProvenanceRecord, Base):
    """Shell for the DB-backed job queue; filled in phase 09."""

    __tablename__ = "jobs"

    job_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False
    )
    pipeline_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("pipeline_runs.id"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    pipeline_run: Mapped[PipelineRun | None] = relationship()
