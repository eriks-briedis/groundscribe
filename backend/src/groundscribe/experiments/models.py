"""The experimentation system's own tables (phase 12).

Four rows holding what nothing else can reconstruct.

``EvaluationDataset`` and its entries are a *corpus*: the approved work a
candidate configuration is measured against. The entries reference immutable
snapshots and the executions that produced them, never articles or projects,
because a benchmark that changed when its source material was revised would make
two runs of one experiment incomparable without either of them looking different.

``ExperimentArm`` is one configuration under test — the baseline and each
candidate — carrying the fork variables that define it. ``ExperimentResult`` is
one arm run against one entry. Both are rows rather than payloads on the
experiment, because the questions asked of them ("did this arm fail on anything?",
"which arm did a person prefer here?") are asked of the *set*, and a set inside a
JSON blob is a set nothing can query.

``ExperimentPreference`` is a person saying which arm produced the better
article for one entry. It is deliberately not a score: plan/12 lists human
preference alongside pass rate and cost as a metric in its own right, precisely
because it is the one judgement no rubric can stand in for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON as JSONColumn
from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from groundscribe.db import Base, UTCDateTime, enum_column
from groundscribe.domain.models import ArtifactSnapshot, EntityMixin, Project
from groundscribe.provenance import models as provenance_models
from groundscribe.provenance.enums import ExecutionStatus


class EvaluationDataset(EntityMixin, Base):
    """A named corpus of approved work, fixed at the moment it was built.

    ``sensitive_included`` records which projects were let in by an explicit
    decision. Kept on the dataset rather than inferred from the entries because
    the decision is the fact worth auditing — a project that was sensitive when
    the dataset was built and is not any more would otherwise leave no trace of
    having needed permission.
    """

    __tablename__ = "evaluation_datasets"

    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    sensitive_included: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)

    entries: Mapped[list[EvaluationDatasetEntry]] = relationship(
        back_populates="dataset", order_by="EvaluationDatasetEntry.ordinal"
    )


class EvaluationDatasetEntry(EntityMixin, Base):
    """One approved article, as something an experiment can be run against.

    ``stage_execution_id`` is what makes the entry *runnable*: an arm forks that
    execution with its own variables. ``reference_snapshot_id`` is what it is
    measured against — the version a person approved, which is the only output in
    the system carrying a human judgement.
    """

    __tablename__ = "evaluation_dataset_entries"

    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, default="", nullable=False)
    stage_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=False
    )
    reference_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=False
    )

    dataset: Mapped[EvaluationDataset] = relationship(back_populates="entries")
    project: Mapped[Project] = relationship()
    stage_execution: Mapped[provenance_models.StageExecution] = relationship()
    reference_snapshot: Mapped[ArtifactSnapshot] = relationship(
        foreign_keys=[reference_snapshot_id]
    )


class ExperimentArm(EntityMixin, Base):
    """One configuration under test, as the variables that define it.

    The baseline is an arm like any other, with no variables. Modelling it as
    "the absence of an arm" would make every metric need two code paths, and the
    baseline is exactly the thing that has to be measured the same way as what it
    is compared against.
    """

    __tablename__ = "experiment_arms"

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment_runs.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)


class ExperimentResult(EntityMixin, Base):
    """One arm, run against one entry, and where its execution ended up.

    ``stage_execution_id`` is nullable because the result row exists from the
    moment the work is queued: an experiment that only recorded what finished
    could not tell "not run yet" from "ran and produced nothing", and the second
    is a finding.
    """

    __tablename__ = "experiment_results"

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment_runs.id"), nullable=False)
    arm_id: Mapped[str] = mapped_column(ForeignKey("experiment_arms.id"), nullable=False)
    entry_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_dataset_entries.id"), nullable=False
    )
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    stage_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage_executions.id"), nullable=True
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        enum_column(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    arm: Mapped[ExperimentArm] = relationship()
    entry: Mapped[EvaluationDatasetEntry] = relationship()
    stage_execution: Mapped[provenance_models.StageExecution | None] = relationship()


class ExperimentPreference(EntityMixin, Base):
    """A person saying which arm did better on one entry.

    Not a score. plan/12 lists human preference beside pass rate and cost because
    it is the metric no rubric can stand in for — and because an experiment whose
    only evidence is the system's own scoring is an experiment marking its own
    work.
    """

    __tablename__ = "experiment_preferences"

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment_runs.id"), nullable=False)
    entry_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_dataset_entries.id"), nullable=False
    )
    preferred_arm_id: Mapped[str] = mapped_column(ForeignKey("experiment_arms.id"), nullable=False)
    decided_by: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, default="", nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    preferred_arm: Mapped[ExperimentArm] = relationship()


__all__ = [
    "EvaluationDataset",
    "EvaluationDatasetEntry",
    "ExperimentArm",
    "ExperimentPreference",
    "ExperimentResult",
]
