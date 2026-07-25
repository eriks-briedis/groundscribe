"""Editorial domain enumerations (phase 02).

These vocabularies are the fixed points later phases route, score, and branch on.
Keeping them as :class:`~enum.StrEnum` means the stored value is a stable string
(portable across SQLite and Postgres, and readable in provenance records) while
the code still gets a typed member.
"""

from __future__ import annotations

from enum import StrEnum


class ClaimClassification(StrEnum):
    """How well a source claim is grounded.

    From *Source-of-truth extraction → Claims and evidence*: extraction assigns
    exactly one of these to every claim so downstream stages know what may be
    stated as fact versus what is the author's interpretation or opinion.
    """

    DIRECTLY_SUPPORTED_FACT = "directly_supported_fact"
    USER_OBSERVATION = "user_observation"
    INTERPRETATION = "interpretation"
    HYPOTHESIS = "hypothesis"
    OPINION = "opinion"
    UNKNOWN = "unknown"
    UNSUPPORTED_CLAIM = "unsupported_claim"


class SourceFormat(StrEnum):
    """How a source document arrived, which decides how it is segmented.

    From *Editorial workflow §1 Source ingestion*: Markdown, plain text and pasted
    notes are the three supported inputs. The distinction is kept after parsing
    because it explains the segmentation: a heading in Markdown is a structural
    fact, whereas the same line in pasted notes is just a line.
    """

    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"
    PASTED_NOTES = "pasted_notes"


class SegmentKind(StrEnum):
    """What one parsed passage of a source document is.

    Recorded per segment because extraction treats them differently — a code
    block is evidence to quote verbatim, a heading is structure, a blockquote is
    usually someone else's words and may not be attributable to the author.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    CODE = "code"
    QUOTE = "quote"


class ArticleDepth(StrEnum):
    """How deep the finished article goes, declared per project.

    A project constraint rather than a stage decision: it bounds scope for every
    stage at once (extraction's token budget, the architecture's article count,
    the brief's length), and letting each stage infer it would let them disagree.
    """

    OVERVIEW = "overview"
    PRACTITIONER = "practitioner"
    DEEP_DIVE = "deep_dive"


class BranchStatus(StrEnum):
    """Lifecycle of one branch in an artefact's lineage.

    ``ACTIVE`` is a live branch; ``SUPERSEDED`` has a chosen successor;
    ``ABANDONED`` was explored and dropped. Branching never destroys a branch —
    it changes its status (plan/00 → immutable, branching snapshots).
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


class SelectionStatus(StrEnum):
    """Whether a branch has been chosen at a human/routing decision point."""

    PENDING = "pending"
    SELECTED = "selected"
    REJECTED = "rejected"


class ArtifactType(StrEnum):
    """The kind of artefact a content-addressed :class:`ArtifactSnapshot` holds.

    Two groups share one vocabulary because they share one store. The editorial
    kinds are the product's subject matter (phase 02); the provenance kinds are
    the payloads of a model call (phase 03), which are content-addressed for the
    same two reasons: the integrity check that detects tampering applies to them
    unchanged, and a repair attempt that resends a nearly identical request
    dedups against the original instead of storing it twice.
    """

    # Editorial artefacts (phase 02).
    SOURCE_DOCUMENT = "source_document"
    SOURCE_MODEL = "source_model"
    CONTENT_ARCHITECTURE = "content_architecture"
    ARTICLE_CONCEPT = "article_concept"
    ARTICLE_BRIEF = "article_brief"
    ARTICLE_VERSION = "article_version"
    REVIEW = "review"
    REVISION_PLAN = "revision_plan"
    VOICE_PROFILE = "voice_profile"
    VALIDATION_REPORT = "validation_report"

    # Provenance payloads (phase 03). Kept as three distinct response kinds
    # rather than one, so a response that parses but fails validation is stored
    # beside its repaired successor instead of replacing it.
    EFFECTIVE_REQUEST = "effective_request"
    RAW_RESPONSE = "raw_response"
    PARSED_RESPONSE = "parsed_response"
    VALIDATED_RESPONSE = "validated_response"
