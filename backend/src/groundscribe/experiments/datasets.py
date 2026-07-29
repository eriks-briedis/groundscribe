"""Turning approved work into something a candidate configuration is tested against (phase 12).

plan/12 → *Evaluation datasets: built from approved historical runs; entries
reference immutable snapshots (not mutable project state); sensitive projects
excluded unless explicitly approved.*

**Approved, not finished.** A run reaching ``COMPLETED`` is a run a person took
the ``approve_final`` edge on; nothing else in the system carries a human
judgement about an article, and a benchmark of judgements the pipeline made about
itself would measure consistency rather than quality.

**Snapshots and executions, not projects.** An entry holds the execution that
produced the approved version (so an arm can fork it) and the snapshot of that
version (so it can be compared against). Both are immutable. An entry naming the
*article* would drift every time the article was revised, and two runs of one
experiment would differ with nothing to say why.

**Sensitivity is a property of the source, and inclusion is a decision.**
Confidential source material, declared confidential names, or a project that
never consented to its trace being retained: any of the three keeps a project out
until somebody names it. Exclusion is silent — one sensitive project must not
block everybody else's evidence, because a safety rule that stops all work is a
safety rule that gets switched off — but the inclusion is written onto the
dataset, because that is the fact an audit asks about.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.experiments.models import EvaluationDataset, EvaluationDatasetEntry
from groundscribe.provenance import models as provenance_models
from groundscribe.provenance.enums import ArtifactDirection
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.position import WorkflowPosition
from groundscribe.workflow.states import WorkflowState


class SensitiveProject(LookupError):
    """A project was named as an exception when it did not need one."""


@dataclass(frozen=True)
class DatasetCandidate:
    """One approved run, and whether it may be used as evaluation data."""

    project_id: str
    run_id: str
    stage_execution_id: str
    reference_snapshot_id: str
    label: str
    sensitive: bool
    reason: str = ""


class DatasetBuilder:
    """Reads approved runs and writes the corpus an experiment is scored over."""

    def __init__(
        self,
        session: Session,
        *,
        snapshots: SnapshotStore,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._snapshots = snapshots
        self._clock = clock or (lambda: datetime.now(UTC))
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def candidates(self) -> tuple[DatasetCandidate, ...]:
        """Every approved run, with the reason any of them would be held back.

        The rejected ones are returned rather than filtered away: a dataset that
        came out smaller than expected is a question, and "which runs were left
        out and why" should not require re-deriving the rule by hand.
        """
        positions = self._session.scalars(
            select(WorkflowPosition)
            .where(WorkflowPosition.state == WorkflowState.COMPLETED)
            .order_by(WorkflowPosition.id)
        ).all()
        return tuple(
            candidate
            for candidate in (self._candidate(position) for position in positions)
            if candidate is not None
        )

    def reference(self, entry: EvaluationDatasetEntry) -> dict[str, Any]:
        """The approved article one entry is measured against."""
        payload: dict[str, Any] = json.loads(self._snapshots.read(entry.reference_snapshot))
        return payload

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        name: str,
        created_by: str,
        description: str = "",
        include_sensitive: Sequence[str] = (),
    ) -> EvaluationDataset:
        """Build the corpus, letting in only the sensitive projects that were named."""
        candidates = self.candidates()
        named = list(dict.fromkeys(include_sensitive))
        unnecessary = sorted(
            set(named) - {candidate.project_id for candidate in candidates if candidate.sensitive}
        )
        if unnecessary:
            raise SensitiveProject(
                f"{', '.join(unnecessary)} was named as an exception but is not sensitive; "
                "an exception nobody needed means the next one will be missing"
            )

        dataset = EvaluationDataset(
            id=self._new_id(),
            name=name,
            description=description,
            created_by=created_by,
            created_at=self._clock(),
            sensitive_included=named,
        )
        self._session.add(dataset)
        self._session.flush()

        allowed = set(named)
        for ordinal, candidate in enumerate(
            item for item in candidates if not item.sensitive or item.project_id in allowed
        ):
            self._session.add(
                EvaluationDatasetEntry(
                    id=self._new_id(),
                    dataset_id=dataset.id,
                    ordinal=ordinal,
                    project_id=candidate.project_id,
                    label=candidate.label,
                    stage_execution_id=candidate.stage_execution_id,
                    reference_snapshot_id=candidate.reference_snapshot_id,
                )
            )
        self._session.flush()
        self._session.refresh(dataset)
        return dataset

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _candidate(self, position: WorkflowPosition) -> DatasetCandidate | None:
        """One approved run as a candidate, or nothing if it left no article."""
        snapshot_id = position.validated_version_id or position.article_version_id
        if snapshot_id is None:
            return None
        producing = self._producing_execution(snapshot_id)
        if producing is None:
            return None

        project_id = position.pipeline_run.project_id
        sensitive, reason = self._sensitivity(project_id)
        project = self._session.get(domain_models.Project, project_id)
        return DatasetCandidate(
            project_id=project_id,
            run_id=position.pipeline_run_id,
            stage_execution_id=producing.id,
            reference_snapshot_id=snapshot_id,
            label=project.title if project is not None else project_id,
            sensitive=sensitive,
            reason=reason,
        )

    def _producing_execution(self, snapshot_id: str) -> provenance_models.StageExecution | None:
        """The execution that wrote a snapshot — the one an arm forks.

        The *output* link rather than the version row's ``created_by_execution_id``,
        because a fork needs the execution whose job can be repeated, and that is
        exactly what the artefact-direction record names.
        """
        return self._session.scalars(
            select(provenance_models.StageExecution)
            .join(
                provenance_models.ExecutionArtifact,
                provenance_models.ExecutionArtifact.stage_execution_id
                == provenance_models.StageExecution.id,
            )
            .where(
                provenance_models.ExecutionArtifact.snapshot_id == snapshot_id,
                provenance_models.ExecutionArtifact.direction == ArtifactDirection.OUTPUT,
            )
            .order_by(provenance_models.StageExecution.started_at.desc())
        ).first()

    def _sensitivity(self, project_id: str) -> tuple[bool, str]:
        """Whether this project's material may be re-run, and why not if it may not."""
        constraints = self._session.scalars(
            select(domain_models.ProjectConstraints)
            .where(domain_models.ProjectConstraints.project_id == project_id)
            .order_by(domain_models.ProjectConstraints.id.desc())
        ).first()
        confidential = self._session.scalars(
            select(domain_models.SourceDocument).where(
                domain_models.SourceDocument.project_id == project_id,
                domain_models.SourceDocument.confidential.is_(True),
            )
        ).first()

        reasons: list[str] = []
        if confidential is not None:
            reasons.append("its source material is marked confidential")
        if constraints is not None and constraints.confidential_names:
            reasons.append(
                f"it declares {len(constraints.confidential_names)} confidential name(s)"
            )
        if constraints is not None and not constraints.trace_retention_consent:
            reasons.append("it has not consented to its trace being retained")
        return bool(reasons), "; ".join(reasons)


__all__ = ["DatasetBuilder", "DatasetCandidate", "SensitiveProject"]
