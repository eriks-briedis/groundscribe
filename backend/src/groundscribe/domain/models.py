"""SQLAlchemy ORM models for the editorial domain (phase 02).

The persistent backbone every later stage writes to. Field names mirror the
Pydantic schemas in :mod:`groundscribe.domain.schemas` so a row validates back to
its schema without loss (schema/DB parity). Enums are stored as their string
*value* via a non-native VARCHAR + CHECK, keeping the column portable between
SQLite and Postgres and readable in raw provenance dumps (plan/00 → Tech stack).

Table names are pluralised to dodge reserved words (``users``, not ``user``) so
the same DDL runs unquoted on Postgres.

Each cross-table foreign key carries a many-to-one ``relationship`` so the unit
of work knows to insert a parent before its children (SQLAlchemy orders inserts
by relationship dependencies, not by bare FK columns) and later phases can
navigate the graph.

``created_by_execution_id`` is a plain nullable column, not yet a foreign key:
the executions table does not exist until phase 03 and the "every artefact
references a creating execution" invariant is enforced in phase 05 (plan/02 →
Non-goals).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, Enum, ForeignKey, Integer, String, Table
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from groundscribe.db import Base
from groundscribe.domain.enums import (
    ArtifactType,
    BranchStatus,
    ClaimClassification,
    SelectionStatus,
)


def _enum(enum_cls: type[Any]) -> Enum:
    """A portable, value-stored enum column type.

    ``native_enum=False`` renders a ``VARCHAR`` + ``CHECK`` (no Postgres native
    enum type to migrate); ``values_callable`` stores the StrEnum *value* rather
    than its member name, so the DB holds the same stable string the code uses.
    """
    return Enum(enum_cls, native_enum=False, values_callable=lambda e: [m.value for m in e])


class EntityMixin:
    """Identity, version stamp, and the (deferred) creating-execution hook."""

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)


class LineageMixin:
    """Self-referential branching lineage for artefacts that fork.

    ``parent_id`` is a self-FK built per-table via ``declared_attr`` (a mixin
    cannot know its own table name up front). Supersession forks a child rather
    than mutating the parent, so one parent may carry many children.
    """

    if TYPE_CHECKING:
        # Provided by the concrete mapped class; declared here for the self-FK below.
        __tablename__: str

    @declared_attr.directive
    @classmethod
    def parent_id(cls) -> Mapped[str | None]:
        return mapped_column(ForeignKey(f"{cls.__tablename__}.id"), nullable=True)

    branch_status: Mapped[BranchStatus] = mapped_column(
        _enum(BranchStatus), default=BranchStatus.ACTIVE, nullable=False
    )
    selection_status: Mapped[SelectionStatus] = mapped_column(
        _enum(SelectionStatus), default=SelectionStatus.PENDING, nullable=False
    )


# Many-to-many: a claim cites one or more source segments; a segment may support
# several claims. The link is what lets provenance trace a claim back to source.
source_claim_segments = Table(
    "source_claim_segments",
    Base.metadata,
    Column("claim_id", ForeignKey("source_claims.id"), primary_key=True),
    Column("segment_id", ForeignKey("source_segments.id"), primary_key=True),
)


class User(EntityMixin, Base):
    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)


class Project(EntityMixin, Base):
    __tablename__ = "projects"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="", nullable=False)

    user: Mapped[User] = relationship()


class SourceDocument(LineageMixin, EntityMixin, Base):
    __tablename__ = "source_documents"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    media_type: Mapped[str] = mapped_column(String, default="text/plain", nullable=False)
    uri: Mapped[str | None] = mapped_column(String, nullable=True)

    project: Mapped[Project] = relationship()


class SourceSegment(EntityMixin, Base):
    __tablename__ = "source_segments"

    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)

    document: Mapped[SourceDocument] = relationship()
    claims: Mapped[list[SourceClaim]] = relationship(
        secondary=source_claim_segments, back_populates="segments"
    )


class SourceClaim(EntityMixin, Base):
    __tablename__ = "source_claims"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    classification: Mapped[ClaimClassification] = mapped_column(
        _enum(ClaimClassification), nullable=False
    )

    project: Mapped[Project] = relationship()
    segments: Mapped[list[SourceSegment]] = relationship(
        secondary=source_claim_segments,
        back_populates="claims",
        order_by=SourceSegment.id,
    )
    # Read-only view of the linked segment ids, for schema parity.
    segment_ids: AssociationProxy[list[str]] = association_proxy("segments", "id")


class SourceGap(EntityMixin, Base):
    __tablename__ = "source_gaps"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    resolved: Mapped[bool] = mapped_column(default=False, nullable=False)

    project: Mapped[Project] = relationship()


class UserAnswer(EntityMixin, Base):
    __tablename__ = "user_answers"

    gap_id: Mapped[str] = mapped_column(ForeignKey("source_gaps.id"), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)

    gap: Mapped[SourceGap] = relationship()


class ContentArchitecture(LineageMixin, EntityMixin, Base):
    __tablename__ = "content_architectures"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)

    project: Mapped[Project] = relationship()


class ArticleConcept(EntityMixin, Base):
    __tablename__ = "article_concepts"

    architecture_id: Mapped[str] = mapped_column(
        ForeignKey("content_architectures.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    angle: Mapped[str] = mapped_column(String, default="", nullable=False)

    architecture: Mapped[ContentArchitecture] = relationship()


class ArticleBrief(LineageMixin, EntityMixin, Base):
    __tablename__ = "article_briefs"

    concept_id: Mapped[str] = mapped_column(ForeignKey("article_concepts.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    objectives: Mapped[str] = mapped_column(String, default="", nullable=False)

    concept: Mapped[ArticleConcept] = relationship()


class Article(EntityMixin, Base):
    __tablename__ = "articles"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="draft", nullable=False)

    project: Mapped[Project] = relationship()


class ArticleVersion(LineageMixin, EntityMixin, Base):
    __tablename__ = "article_versions"

    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )

    article: Mapped[Article] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])


class Review(LineageMixin, EntityMixin, Base):
    __tablename__ = "reviews"

    article_version_id: Mapped[str] = mapped_column(
        ForeignKey("article_versions.id"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String, nullable=False)

    article_version: Mapped[ArticleVersion] = relationship()


class ReviewIssue(EntityMixin, Base):
    __tablename__ = "review_issues"

    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)

    review: Mapped[Review] = relationship()


class RevisionPlan(EntityMixin, Base):
    __tablename__ = "revision_plans"

    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)

    review: Mapped[Review] = relationship()


class VoiceProfile(LineageMixin, EntityMixin, Base):
    __tablename__ = "voice_profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="", nullable=False)

    user: Mapped[User] = relationship()


class ValidationReport(LineageMixin, EntityMixin, Base):
    __tablename__ = "validation_reports"

    article_version_id: Mapped[str] = mapped_column(
        ForeignKey("article_versions.id"), nullable=False
    )
    passed: Mapped[bool] = mapped_column(nullable=False)

    article_version: Mapped[ArticleVersion] = relationship()


class ArtifactSnapshot(EntityMixin, Base):
    """Immutable, content-addressed reference to a stored artefact.

    Content is held in the blob store (identified by ``content_hash`` /
    ``content_location``); this row is its metadata and lineage. Superseding an
    artefact never mutates a snapshot — it inserts a new one whose
    ``parent_snapshot_id`` links back (plan/00 → no silent mutation).
    """

    __tablename__ = "artifact_snapshots"

    artifact_type: Mapped[ArtifactType] = mapped_column(_enum(ArtifactType), nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    content_location: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    branch_status: Mapped[BranchStatus] = mapped_column(
        _enum(BranchStatus), default=BranchStatus.ACTIVE, nullable=False
    )
    selection_status: Mapped[SelectionStatus] = mapped_column(
        _enum(SelectionStatus), default=SelectionStatus.PENDING, nullable=False
    )
