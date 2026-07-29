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

from typing import TYPE_CHECKING

from sqlalchemy import JSON as JSONColumn
from sqlalchemy import Column, Float, ForeignKey, Integer, String, Table
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from groundscribe.db import Base, enum_column
from groundscribe.domain.confidentiality import Confidentiality, ConfidentialityFlags
from groundscribe.domain.enums import (
    AnswerResponse,
    ArticleDepth,
    ArtifactType,
    BranchStatus,
    ClaimClassification,
    FindingStatus,
    GapPriority,
    IssueSeverity,
    SegmentKind,
    SelectionStatus,
    SourceFormat,
)
from groundscribe.domain.retention import RetentionMode


class EntityMixin:
    """Identity, version stamp, and the (deferred) creating-execution hook."""

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)


class ConfidentialityMixin:
    """What may be sent, published, and exported, for one span of source (phase 13).

    Two columns rather than one because they are two different facts: the
    classification a person set, and any extra boundaries they named on top of
    it. Storing only the resolved set would lose which was which, and a record
    that cannot say what was chosen cannot be argued with.

    Both default to the permissive end, so material written before this phase
    reads back as publishable rather than as null. That is what keeps the
    enforcement points total — a check that had to handle "no flag" would grow a
    third branch, and the third branch is where material leaks.
    """

    confidentiality: Mapped[Confidentiality] = mapped_column(
        enum_column(Confidentiality), default=Confidentiality.PUBLISHABLE, nullable=False
    )
    excluded: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)

    @property
    def flags(self) -> ConfidentialityFlags:
        """The two columns resolved into the one question callers ask.

        Both are coalesced to the permissive default. The columns are ``NOT
        NULL``, so a *persisted* row always answers; an object that has not been
        flushed yet has not had its defaults applied, and a property that raised
        on one would make "may this be sent?" a question only answerable after a
        round trip to the database.
        """
        return ConfidentialityFlags(
            self.confidentiality or Confidentiality.PUBLISHABLE, self.excluded or ()
        )


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
        enum_column(BranchStatus), default=BranchStatus.ACTIVE, nullable=False
    )
    selection_status: Mapped[SelectionStatus] = mapped_column(
        enum_column(SelectionStatus), default=SelectionStatus.PENDING, nullable=False
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


class ProjectConstraints(LineageMixin, EntityMixin, Base):
    """One version of the bounds a project publishes under (phase 06 §1).

    Lineage rather than in-place edits: a brief generated under an 1800-word limit
    *was* generated under that limit, and rewriting the row would make the
    artefact's own record wrong. The list-valued constraints are JSON columns —
    they are read as a whole, never joined on, and two extra tables would buy
    nothing but joins.
    """

    __tablename__ = "project_constraints"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    audience: Mapped[str] = mapped_column(String, nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    depth: Mapped[ArticleDepth] = mapped_column(enum_column(ArticleDepth), nullable=False)
    target_length_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_person_allowed: Mapped[bool] = mapped_column(default=True, nullable=False)
    confidential_names: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)
    allowed_providers: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)
    trace_retention_consent: Mapped[bool] = mapped_column(default=False, nullable=False)
    # How much of this project's trace is kept (phase 13). Here rather than in a
    # config file because the constraints are versioned and branch instead of
    # being edited: "what was the retention mode when that run was recorded?"
    # stays answerable, which is the question someone asks when a trace turns
    # out to be thinner than they expected.
    trace_retention_mode: Mapped[RetentionMode] = mapped_column(
        enum_column(RetentionMode), default=RetentionMode.FULL, nullable=False
    )

    project: Mapped[Project] = relationship()


class SourceDocument(LineageMixin, EntityMixin, Base):
    __tablename__ = "source_documents"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    media_type: Mapped[str] = mapped_column(String, default="text/plain", nullable=False)
    source_format: Mapped[SourceFormat] = mapped_column(
        enum_column(SourceFormat), default=SourceFormat.PLAIN_TEXT, nullable=False
    )
    uri: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str] = mapped_column(String, default="", nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    confidential: Mapped[bool] = mapped_column(default=False, nullable=False)

    project: Mapped[Project] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])


class SourceSegment(ConfidentialityMixin, EntityMixin, Base):
    __tablename__ = "source_segments"

    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[SegmentKind] = mapped_column(
        enum_column(SegmentKind), default=SegmentKind.PARAGRAPH, nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String, default="", nullable=False)
    # Offsets into the document as ingested: what makes a citation verifiable
    # rather than merely plausible.
    char_start: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    document: Mapped[SourceDocument] = relationship()
    claims: Mapped[list[SourceClaim]] = relationship(
        secondary=source_claim_segments, back_populates="segments"
    )


class SourceClaim(ConfidentialityMixin, EntityMixin, Base):
    """A claim drawn from the source, with its own confidentiality (phase 13).

    Flagged independently of the segments behind it: extraction can narrow a
    publishable paragraph into a claim that names a customer, and if only
    segments could be flagged the only way to withhold that claim would be to
    withhold the paragraph it came from.
    """

    __tablename__ = "source_claims"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    classification: Mapped[ClaimClassification] = mapped_column(
        enum_column(ClaimClassification), nullable=False
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
    """Something the source does not say, and the question that would settle it.

    ``surfaced`` records the prioritisation *decision*, not just its input: a gap
    the policy suppressed and a gap the author was never offered look identical
    from the priority alone, and only one of them is a bug.
    """

    __tablename__ = "source_gaps"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    # The model's own label for the question ("g1"), unique only within the round
    # that produced it. The row id is generated, because a model asked about the
    # same source twice hands back the same labels — and two rounds of questions
    # have to coexist, exactly as two rounds of review findings do.
    ref: Mapped[str] = mapped_column(String, default="", nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    question: Mapped[str] = mapped_column(String, default="", nullable=False)
    why_it_matters: Mapped[str] = mapped_column(String, default="", nullable=False)
    priority: Mapped[GapPriority] = mapped_column(
        enum_column(GapPriority), default=GapPriority.OPTIONAL, nullable=False
    )
    group: Mapped[str] = mapped_column(String, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    surfaced: Mapped[bool] = mapped_column(default=False, nullable=False)
    resolved: Mapped[bool] = mapped_column(default=False, nullable=False)

    project: Mapped[Project] = relationship()


# One answer may close several grouped questions, and a question may be answered
# more than once across rounds; the link is many-to-many for the first reason.
user_answer_gaps = Table(
    "user_answer_gaps",
    Base.metadata,
    Column("answer_id", ForeignKey("user_answers.id"), primary_key=True),
    Column("gap_id", ForeignKey("source_gaps.id"), primary_key=True),
)


class UserAnswer(EntityMixin, Base):
    """The author's response to a question, kept with the question it answers.

    The question and its reason are copied onto the answer rather than read back
    through ``gap_id``: a later round may re-word the question, and an answer that
    silently re-pointed at the new wording would misrepresent what the author was
    actually asked.

    ``gap_id`` is the question that was *put*; ``gaps`` is everything the answer
    closed. They are different facts — one answer to a grouped question can settle
    several gaps — and collapsing them would lose which question the author saw.
    """

    __tablename__ = "user_answers"

    gap_id: Mapped[str] = mapped_column(ForeignKey("source_gaps.id"), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    question: Mapped[str] = mapped_column(String, default="", nullable=False)
    why_it_matters: Mapped[str] = mapped_column(String, default="", nullable=False)
    response_type: Mapped[AnswerResponse] = mapped_column(
        enum_column(AnswerResponse), default=AnswerResponse.ANSWERED, nullable=False
    )
    answered_by: Mapped[str] = mapped_column(String, default="", nullable=False)
    # Set once the rebuild this answer caused has produced its diff; nullable
    # because the answer exists before the model has been rebuilt.
    diff_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )

    gap: Mapped[SourceGap] = relationship(foreign_keys=[gap_id])
    gaps: Mapped[list[SourceGap]] = relationship(
        secondary=user_answer_gaps, order_by=SourceGap.ordinal
    )
    diff_snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[diff_snapshot_id])


class ContentArchitecture(LineageMixin, EntityMixin, Base):
    """One version of the proposed shape of the article or series (phase 06 §4).

    ``locked`` is set when a person approves it. From then on a change must fork a
    new version and name who authorised it (plan/05 → no approved architecture
    mutates silently); the flag is what makes "was this approved when it changed?"
    answerable without replaying the whole run.
    """

    __tablename__ = "content_architectures"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    locked: Mapped[bool] = mapped_column(default=False, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)

    project: Mapped[Project] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])


class ArticleConcept(EntityMixin, Base):
    """One candidate article within a content architecture.

    ``ref`` is the model's own label for the article ("a1"), unique only within
    the proposal that produced it. The row id is generated, for the reason
    :class:`SourceGap` and :class:`ReviewIssue` generate theirs — a model shaping
    the same source twice hands back the same labels — and for one reason neither
    of them has: this id is what the API addresses an *article* by, for the whole
    life of the project. An identifier that every draft, review and score hangs
    off is not a name a language model should be choosing.
    """

    __tablename__ = "article_concepts"

    architecture_id: Mapped[str] = mapped_column(
        ForeignKey("content_architectures.id"), nullable=False
    )
    ref: Mapped[str] = mapped_column(String, default="", nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    angle: Mapped[str] = mapped_column(String, default="", nullable=False)
    # The claim being argued, kept apart from the angle: the angle is how it is
    # approached, the thesis is what the article asserts, and the brief is a
    # contract against the second one.
    thesis: Mapped[str] = mapped_column(String, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    architecture: Mapped[ContentArchitecture] = relationship()


class ArticleBrief(LineageMixin, EntityMixin, Base):
    """The contract for one article; the document itself is the snapshot."""

    __tablename__ = "article_briefs"

    concept_id: Mapped[str] = mapped_column(ForeignKey("article_concepts.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    objectives: Mapped[str] = mapped_column(String, default="", nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )

    concept: Mapped[ArticleConcept] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])


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
    """One substantive review of one article version (phase 07 §8)."""

    __tablename__ = "reviews"

    article_version_id: Mapped[str] = mapped_column(
        ForeignKey("article_versions.id"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    # Which pass over this version this was. A version can be reviewed more than
    # once — after an edit, or by a second reviewer — and "the same finding again"
    # only means anything against a round number.
    round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )

    article_version: Mapped[ArticleVersion] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])
    issues: Mapped[list[ReviewIssue]] = relationship(back_populates="review")


class ReviewIssue(EntityMixin, Base):
    """One finding, and what the author decided about it (phase 07 §8).

    A row rather than an element of the review's payload, because a finding has a
    *lifecycle*: proposed, then accepted, rejected or edited by a person, and
    possibly suppressed in a later round. A lifecycle inside a JSON blob is a
    lifecycle nothing can query.

    ``fingerprint`` is what makes "the same finding again" answerable across
    rounds. It is derived from what the finding says and the evidence behind it,
    never from its id, which the reviewer renumbers freely.
    """

    __tablename__ = "review_issues"

    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    # The reviewer's own label for the finding ("i1"), unique only within its
    # review. The row id is generated, because the reviewer renumbers from one on
    # every round and two rounds of findings have to coexist.
    ref: Mapped[str] = mapped_column(String, default="", nullable=False)
    severity: Mapped[IssueSeverity] = mapped_column(enum_column(IssueSeverity), nullable=False)
    category: Mapped[str] = mapped_column(String, default="", nullable=False)
    location: Mapped[str] = mapped_column(String, default="", nullable=False)
    passage: Mapped[str] = mapped_column(String, default="", nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    evidence: Mapped[str] = mapped_column(String, default="", nullable=False)
    source_ref: Mapped[str] = mapped_column(String, default="", nullable=False)
    brief_ref: Mapped[str] = mapped_column(String, default="", nullable=False)
    recommended_correction: Mapped[str] = mapped_column(String, default="", nullable=False)
    suggested_route: Mapped[str] = mapped_column(String, default="", nullable=False)
    blocks_publication: Mapped[bool] = mapped_column(default=False, nullable=False)
    reviewer_confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, default="", nullable=False)
    status: Mapped[FindingStatus] = mapped_column(
        enum_column(FindingStatus), default=FindingStatus.PROPOSED, nullable=False
    )
    decided_by: Mapped[str] = mapped_column(String, default="", nullable=False)
    decision_reason: Mapped[str] = mapped_column(String, default="", nullable=False)

    review: Mapped[Review] = relationship(back_populates="issues")


class RevisionPlan(EntityMixin, Base):
    """What a rewrite will do, and what it must leave alone (phase 07 §9).

    Its own artefact rather than a field on the review: plan/07 requires a record
    that explains what was combined, deferred and rejected, and a plan folded into
    the review would make "what the reviewer said" and "what we decided to do about
    it" the same document.
    """

    __tablename__ = "revision_plans"

    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )

    review: Mapped[Review] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])


class VoiceProfile(LineageMixin, EntityMixin, Base):
    __tablename__ = "voice_profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="", nullable=False)

    user: Mapped[User] = relationship()


class ValidationReport(LineageMixin, EntityMixin, Base):
    """The deterministic final check of one article version (phase 08).

    ``passed`` stays on the row rather than living only inside the document: "has
    this version been validated, and did it pass" is asked of the *table* — by the
    export guard, by the approval queue — and answering it from a JSON blob would
    mean reading every blob.
    """

    __tablename__ = "validation_reports"

    article_version_id: Mapped[str] = mapped_column(
        ForeignKey("article_versions.id"), nullable=False
    )
    passed: Mapped[bool] = mapped_column(nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )

    article_version: Mapped[ArticleVersion] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])


class ArtifactSnapshot(EntityMixin, Base):
    """Immutable, content-addressed reference to a stored artefact.

    Content is held in the blob store (identified by ``content_hash`` /
    ``content_location``); this row is its metadata and lineage. Superseding an
    artefact never mutates a snapshot — it inserts a new one whose
    ``parent_snapshot_id`` links back (plan/00 → no silent mutation).
    """

    __tablename__ = "artifact_snapshots"

    artifact_type: Mapped[ArtifactType] = mapped_column(enum_column(ArtifactType), nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    content_location: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    branch_status: Mapped[BranchStatus] = mapped_column(
        enum_column(BranchStatus), default=BranchStatus.ACTIVE, nullable=False
    )
    selection_status: Mapped[SelectionStatus] = mapped_column(
        enum_column(SelectionStatus), default=SelectionStatus.PENDING, nullable=False
    )
