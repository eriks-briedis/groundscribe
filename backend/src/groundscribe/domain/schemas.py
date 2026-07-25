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

from groundscribe.domain.enums import (
    AnswerResponse,
    ArticleDepth,
    BranchStatus,
    ClaimClassification,
    GapPriority,
    SegmentKind,
    SelectionStatus,
    SourceFormat,
)


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


class EditorialConstraints(BaseModel):
    """The bounds a project publishes under (phase 06 §1).

    A *value*, not a record: :class:`ProjectConstraints` is the versioned row that
    holds one of these. Splitting them is what lets ingestion ask "are these the
    constraints already in force?" by comparing values, and version them only when
    the answer is no.

    ``allowed_providers`` is an allow-list. A project that has not named a
    provider has not consented to it seeing the material, so the default is that
    nothing external may — local-first by default (plan/00), enforced end to end
    in phase 13.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    audience: str
    platform: str
    depth: ArticleDepth
    target_length_words: int | None = None
    first_person_allowed: bool = True
    confidential_names: tuple[str, ...] = ()
    allowed_providers: tuple[str, ...] = ()
    trace_retention_consent: bool = False

    def permits_provider(self, provider: str) -> bool:
        """Whether ``provider`` may be sent this project's material."""
        return provider in self.allowed_providers


class ProjectConstraints(_Lineage, EditorialConstraints):
    """One version of a project's constraints.

    Branching rather than editing: a brief generated under an 1800-word limit was
    generated under that limit, and rewriting the row would make the artefact's
    own record wrong.
    """

    model_config = ConfigDict(from_attributes=True)

    project_id: str


class SourceDocument(_Lineage):
    """A piece of raw source material; a re-ingested version branches from it.

    ``content_hash`` addresses the *whole* document as ingested, so a claim of the
    form "this was extracted from that source" is checkable byte-for-byte;
    ``snapshot_id`` points at the stored content itself.
    """

    project_id: str
    title: str
    media_type: str = "text/plain"
    source_format: SourceFormat = SourceFormat.PLAIN_TEXT
    uri: str | None = None
    content_hash: str = ""
    snapshot_id: str | None = None
    confidential: bool = False


class SourceSegment(_Entity):
    """An addressable span of a source document; claims cite these.

    The character offsets are into the document as ingested, and are what makes a
    citation verifiable rather than merely plausible: the text can be sliced back
    out of the original and compared to ``content_hash``.
    """

    document_id: str
    ordinal: int
    text: str
    kind: SegmentKind = SegmentKind.PARAGRAPH
    content_hash: str = ""
    char_start: int = 0
    char_end: int = 0


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
    """A missing piece of information the source does not answer.

    ``surfaced`` records the prioritisation decision itself: a gap the policy
    suppressed and a gap the author was never offered look identical from the
    priority alone, and only one of those is a bug.
    """

    project_id: str
    description: str
    question: str = ""
    why_it_matters: str = ""
    priority: GapPriority = GapPriority.OPTIONAL
    group: str = ""
    ordinal: int = 0
    surfaced: bool = False
    resolved: bool = False


class UserAnswer(_Entity):
    """The author's response to a question, kept with the question it answers.

    The question and its reason are copied here rather than read back through
    ``gap_id``: a later round may re-word the question, and an answer that
    silently re-pointed at the new wording would misrepresent what the author was
    actually asked.
    """

    gap_id: str
    text: str
    question: str = ""
    why_it_matters: str = ""
    response_type: AnswerResponse = AnswerResponse.ANSWERED
    answered_by: str = ""
    diff_snapshot_id: str | None = None


class ContentArchitecture(_Lineage):
    """A proposed structure for the article(s) drawn from the source model.

    ``locked`` is set on approval: from then on a change must fork a new version
    and name who authorised it (plan/05 → no approved architecture mutates
    silently).
    """

    project_id: str
    summary: str
    snapshot_id: str | None = None
    locked: bool = False
    locked_by: str | None = None


class ArticleConcept(_Entity):
    """One candidate article within a content architecture."""

    architecture_id: str
    title: str
    angle: str = ""
    thesis: str = ""
    ordinal: int = 0


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
