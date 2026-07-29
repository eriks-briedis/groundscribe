"""Rebuilding a stage's inputs from the database (phase 09).

A worker runs in a different process from the request that queued the work, so
everything an editorial stage needs has to be reconstructible from rows. This
module is that reconstruction, and it is deliberately the *only* place that does
it: a handler that reached for its own query would be a second opinion on which
brief is current.

Two kinds of input, two ways back:

- **Documents** — source models, briefs, drafts, reviews, plans — were written as
  content-addressed snapshots holding a Pydantic model's JSON. They come back by
  validating the stored bytes against the same schema. Nothing is re-derived, so
  a stage sees exactly what the stage before it produced.
- **Rows** — concepts, versions, reviews, findings — are looked up directly, and
  "latest" always means the most recent one produced by *this run*.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.provenance import models
from groundscribe.provenance.enums import ArtifactDirection
from groundscribe.stages.ingestion import IngestedSource
from groundscribe.storage.snapshot_store import SnapshotStore


class MissingInput(LookupError):
    """A stage was asked to run before what it needs exists.

    Raised rather than returning ``None`` so the failure names the missing thing.
    A handler that quietly ran with an empty brief would produce an article
    nobody could explain.
    """


def latest_snapshot(
    session: Session, run: models.PipelineRun, artifact_type: ArtifactType
) -> ArtifactSnapshot | None:
    """The most recent snapshot of ``artifact_type`` this run produced.

    Scoped to the run and ordered by the producing execution, so "latest" means
    "latest in this pipeline" rather than "latest in the database" — which
    matters the moment two runs of one project overlap.
    """
    stmt = (
        select(ArtifactSnapshot)
        .join(models.ExecutionArtifact, models.ExecutionArtifact.snapshot_id == ArtifactSnapshot.id)
        .join(
            models.StageExecution,
            models.StageExecution.id == models.ExecutionArtifact.stage_execution_id,
        )
        .where(
            models.StageExecution.pipeline_run_id == run.id,
            models.ExecutionArtifact.direction == ArtifactDirection.OUTPUT,
            ArtifactSnapshot.artifact_type == artifact_type,
        )
        .order_by(
            models.StageExecution.started_at.desc(),
            models.StageExecution.id.desc(),
            models.ExecutionArtifact.ordinal.desc(),
        )
    )
    return session.scalars(stmt).first()


def require_snapshot(
    session: Session, run: models.PipelineRun, artifact_type: ArtifactType
) -> ArtifactSnapshot:
    """:func:`latest_snapshot`, or a failure naming what is missing."""
    snapshot = latest_snapshot(session, run, artifact_type)
    if snapshot is None:
        raise MissingInput(f"this run has produced no {artifact_type.value} yet")
    return snapshot


def document[T: BaseModel](
    snapshots: SnapshotStore, snapshot: ArtifactSnapshot, schema: type[T]
) -> T:
    """Validate a stored snapshot back into the schema that wrote it."""
    return schema.model_validate_json(snapshots.read(snapshot))


def constraints_row(session: Session, project_id: str) -> domain_models.ProjectConstraints:
    """The versioned row holding this project's bounds."""
    row = session.scalars(
        select(domain_models.ProjectConstraints)
        .where(domain_models.ProjectConstraints.project_id == project_id)
        .order_by(domain_models.ProjectConstraints.id)
    ).first()
    if row is None:
        raise MissingInput(f"project {project_id} has no constraints recorded")
    return row


def constraints(session: Session, project_id: str) -> EditorialConstraints:
    """The bounds this project currently publishes under, as a value.

    The row and the value are both wanted, by different callers: a stage context
    holds the value (phase 06 compares them), while ``IngestedSource`` holds the
    row it was ingested under. Deriving one from the other here keeps the "which
    version was in force?" question answerable from either.
    """
    return EditorialConstraints.model_validate(constraints_row(session, project_id))


def ingested_source(session: Session, snapshots: SnapshotStore, project_id: str) -> IngestedSource:
    """The project's source document, its segments, and the bounds it came in under.

    Reassembled rather than cached: the ingesting request built this object and
    then went away, and a worker that could not rebuild it would be unable to
    re-extract from a source it did not personally ingest.
    """
    document_row = session.scalars(
        select(domain_models.SourceDocument)
        .where(domain_models.SourceDocument.project_id == project_id)
        .order_by(domain_models.SourceDocument.id)
    ).first()
    if document_row is None or document_row.snapshot is None:
        raise MissingInput(f"project {project_id} has no ingested source")

    segments = tuple(
        session.scalars(
            select(domain_models.SourceSegment)
            .where(domain_models.SourceSegment.document_id == document_row.id)
            .order_by(domain_models.SourceSegment.ordinal)
        )
    )
    return IngestedSource(
        document=document_row,
        segments=segments,
        snapshot=document_row.snapshot,
        constraints=constraints_row(session, project_id),
    )


def open_answers(session: Session, project_id: str) -> tuple[domain_models.UserAnswer, ...]:
    """Every answer the author has given, in the order the questions were asked.

    All of them, not only the newest round: a rebuild is a rebuild *from the
    source plus everything the author has said*, and dropping earlier rounds
    would silently un-answer questions they already closed.
    """
    return tuple(
        session.scalars(
            select(domain_models.UserAnswer)
            .join(
                domain_models.SourceGap,
                domain_models.SourceGap.id == domain_models.UserAnswer.gap_id,
            )
            .where(domain_models.SourceGap.project_id == project_id)
            .order_by(domain_models.SourceGap.ordinal)
        )
    )


def concept(session: Session, concept_id: str) -> domain_models.ArticleConcept:
    """One article concept, by the id the API addresses articles with."""
    row = session.get(domain_models.ArticleConcept, concept_id)
    if row is None:
        raise MissingInput(f"no article concept {concept_id}")
    return row


def latest_version(session: Session, article_id: str) -> domain_models.ArticleVersion:
    """The newest version of an article, which is what every later stage works on."""
    row = session.scalars(
        select(domain_models.ArticleVersion)
        .where(domain_models.ArticleVersion.article_id == article_id)
        .order_by(domain_models.ArticleVersion.ordinal.desc())
    ).first()
    if row is None:
        raise MissingInput(f"article {article_id} has no versions yet")
    return row


def latest_review(session: Session, version_id: str) -> domain_models.Review:
    """The newest review of one article version."""
    row = session.scalars(
        select(domain_models.Review)
        .where(domain_models.Review.article_version_id == version_id)
        .order_by(domain_models.Review.round.desc())
    ).first()
    if row is None:
        raise MissingInput(f"article version {version_id} has not been reviewed")
    return row


def latest_plan(session: Session, review_id: str) -> domain_models.RevisionPlan:
    """The revision plan drawn up from one review."""
    row = session.scalars(
        select(domain_models.RevisionPlan)
        .where(domain_models.RevisionPlan.review_id == review_id)
        .order_by(domain_models.RevisionPlan.id.desc())
    ).first()
    if row is None:
        raise MissingInput(f"review {review_id} has no revision plan")
    return row


def snapshot_of(session: Session, snapshot_id: str | None) -> ArtifactSnapshot:
    """A snapshot by id, insisting it exists."""
    snapshot = session.get(ArtifactSnapshot, snapshot_id) if snapshot_id else None
    if snapshot is None:
        raise MissingInput(f"no artefact snapshot {snapshot_id}")
    return snapshot


__all__ = [
    "MissingInput",
    "concept",
    "constraints",
    "constraints_row",
    "document",
    "ingested_source",
    "latest_plan",
    "latest_review",
    "latest_snapshot",
    "latest_version",
    "open_answers",
    "require_snapshot",
    "snapshot_of",
]
