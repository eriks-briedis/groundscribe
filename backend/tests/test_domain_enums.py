"""Editorial domain enum tests (phase 02).

Spec (plan/02 → Deliverables / Test-first specification):
- the claim classification enum must be *complete*: exactly the seven kinds the
  source-of-truth extraction distinguishes;
- lineage-bearing artefacts carry a ``branch_status`` and ``selection_status``;
- ``ArtifactSnapshot`` records an ``artifact_type``.

Enums are the vocabulary later phases route and score on, so locking their exact
membership here is what keeps those decisions typed and testable.
"""

from __future__ import annotations

from enum import StrEnum

from groundscribe.domain.enums import (
    ArtifactType,
    BranchStatus,
    ClaimClassification,
    SelectionStatus,
)


def test_claim_classification_is_complete() -> None:
    """The seven claim kinds from the spec, no more and no fewer."""
    assert issubclass(ClaimClassification, StrEnum)
    assert {c.value for c in ClaimClassification} == {
        "directly_supported_fact",
        "user_observation",
        "interpretation",
        "hypothesis",
        "opinion",
        "unknown",
        "unsupported_claim",
    }


def test_branch_and_selection_status_members() -> None:
    """Branch/selection status vocabularies used by every lineage-bearing artefact."""
    assert issubclass(BranchStatus, StrEnum)
    assert issubclass(SelectionStatus, StrEnum)
    assert {s.value for s in BranchStatus} == {"active", "superseded", "abandoned"}
    assert {s.value for s in SelectionStatus} == {"pending", "selected", "rejected"}


def test_artifact_type_covers_snapshotted_artefacts() -> None:
    """Every artefact that gets content-addressed has an ``ArtifactType``."""
    assert issubclass(ArtifactType, StrEnum)
    assert {a.value for a in ArtifactType} == {
        "source_document",
        "source_model",
        "content_architecture",
        "article_concept",
        "article_brief",
        "article_version",
        "review",
        "revision_plan",
        "voice_profile",
        "validation_report",
    }
