"""Pydantic schemas for the editorial domain entities (phase 02).

These are the authoritative in-memory / wire shapes for the 17 core editorial
entities from *Domain model → Core editorial entities*. Each carries a first-class
``schema_version`` (plan/00 → versioning is first-class) and a nullable
``created_by_execution_id`` — the "every artefact references a creating execution"
invariant is only *enforced* in phase 05, once executions exist, so the field is
optional here (plan/02 → Non-goals).

``from_attributes=True`` lets every schema validate directly from its SQLAlchemy
row, which is how schema/DB parity is checked in ``test_domain_models``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from groundscribe.domain.enums import BranchStatus, ClaimClassification, SelectionStatus


class _Entity(BaseModel):
    """Common base: identity, provenance hook, and version stamp."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    schema_version: int = 1
    # Nullable until phase 05 enforces the creating-execution invariant.
    created_by_execution_id: str | None = None


class _Lineage(_Entity):
    """Base for artefacts that branch: parent link plus branch/selection status.

    Supersession never overwrites — it forks a new artefact whose ``parent_id``
    points back, so a single parent can carry multiple children (plan/00 →
    immutable, branching snapshots over destructive edits).
    """

    parent_id: str | None = None
    branch_status: BranchStatus = BranchStatus.ACTIVE
    selection_status: SelectionStatus = SelectionStatus.PENDING


class User(_Entity):
    name: str
    email: str


class Project(_Entity):
    user_id: str
    title: str
    description: str = ""


class SourceDocument(_Lineage):
    """A piece of raw source material; a re-ingested version branches from it."""

    project_id: str
    title: str
    media_type: str = "text/plain"
    uri: str | None = None


class SourceSegment(_Entity):
    """An addressable span of a source document; claims cite these."""

    document_id: str
    ordinal: int
    text: str


class SourceClaim(_Entity):
    """A claim extracted from the source, classified by how well it is grounded.

    ``classification`` is required (every claim must be classified) and
    ``segment_ids`` retains the originating passages so provenance can trace a
    claim back to source.
    """

    project_id: str
    text: str
    classification: ClaimClassification
    segment_ids: list[str] = Field(default_factory=list)


class SourceGap(_Entity):
    """A missing piece of information the source does not answer."""

    project_id: str
    description: str
    resolved: bool = False


class UserAnswer(_Entity):
    """The author's answer to a source gap."""

    gap_id: str
    text: str


class ContentArchitecture(_Lineage):
    """A proposed structure for the article(s) drawn from the source model."""

    project_id: str
    summary: str


class ArticleConcept(_Entity):
    """One candidate article within a content architecture."""

    architecture_id: str
    title: str
    angle: str = ""


class ArticleBrief(_Lineage):
    """The scoped, approved plan for a single article."""

    concept_id: str
    scope: str
    objectives: str = ""


class Article(_Entity):
    project_id: str
    title: str
    status: str = "draft"


class ArticleVersion(_Lineage):
    """An immutable draft of an article; rewrites branch from a parent version."""

    article_id: str
    ordinal: int
    snapshot_id: str | None = None


class Review(_Lineage):
    """A structured editorial review of an article version."""

    article_version_id: str
    verdict: str


class ReviewIssue(_Entity):
    """A single issue raised by a review."""

    review_id: str
    severity: str
    description: str


class RevisionPlan(_Entity):
    """The plan for addressing a review's issues."""

    review_id: str
    summary: str


class VoiceProfile(_Lineage):
    """A captured personal voice; refinements branch from a parent profile."""

    user_id: str
    name: str
    description: str = ""


class ValidationReport(_Lineage):
    """The final factual/scope validation of an article version."""

    article_version_id: str
    passed: bool
