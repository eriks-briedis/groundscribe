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
