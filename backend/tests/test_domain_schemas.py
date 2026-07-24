"""Editorial entity Pydantic schema tests (phase 02).

Spec (plan/02 → Deliverables / Test-first specification):
- all 17 editorial entities are modelled with an explicit ``schema_version``;
- **claim classification:** every ``SourceClaim`` carries *exactly one*
  classification from the enum and retains references to its originating
  ``SourceSegment``s;
- branching artefacts expose ``branch_status`` / ``selection_status`` lineage.

These are the wire/domain shapes; DB parity with the ORM rows is covered
separately in ``test_domain_models``.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from groundscribe.domain import schemas
from groundscribe.domain.enums import BranchStatus, ClaimClassification, SelectionStatus

ALL_ENTITY_SCHEMAS = [
    schemas.User,
    schemas.Project,
    schemas.SourceDocument,
    schemas.SourceSegment,
    schemas.SourceClaim,
    schemas.SourceGap,
    schemas.UserAnswer,
    schemas.ContentArchitecture,
    schemas.ArticleConcept,
    schemas.ArticleBrief,
    schemas.Article,
    schemas.ArticleVersion,
    schemas.Review,
    schemas.ReviewIssue,
    schemas.RevisionPlan,
    schemas.VoiceProfile,
    schemas.ValidationReport,
]

LINEAGE_SCHEMAS = [
    schemas.SourceDocument,
    schemas.ContentArchitecture,
    schemas.ArticleBrief,
    schemas.ArticleVersion,
    schemas.Review,
    schemas.VoiceProfile,
    schemas.ValidationReport,
]


def test_all_seventeen_editorial_entities_are_modelled() -> None:
    """The spec names exactly 17 core editorial entities."""
    assert len(ALL_ENTITY_SCHEMAS) == 17
    assert len({s.__name__ for s in ALL_ENTITY_SCHEMAS}) == 17


@pytest.mark.parametrize("schema", ALL_ENTITY_SCHEMAS)
def test_every_entity_records_a_schema_version(schema: type[BaseModel]) -> None:
    """Versioning is first-class: every entity carries a defaulted schema_version."""
    fields = schema.model_fields
    assert "schema_version" in fields
    assert fields["schema_version"].default == 1


def test_source_claim_carries_exactly_one_classification_and_keeps_segments() -> None:
    """A claim has a single required classification and retains its source segments."""
    claim = schemas.SourceClaim(
        id="c1",
        project_id="p1",
        text="Latency dropped 40% after the cache change.",
        classification=ClaimClassification.DIRECTLY_SUPPORTED_FACT,
        segment_ids=["seg-1", "seg-2"],
    )
    assert claim.classification is ClaimClassification.DIRECTLY_SUPPORTED_FACT
    assert claim.segment_ids == ["seg-1", "seg-2"]


def test_source_claim_classification_is_required() -> None:
    """Classification cannot be omitted — every claim must be classified."""
    with pytest.raises(ValidationError):
        schemas.SourceClaim(id="c1", project_id="p1", text="x", segment_ids=[])  # type: ignore[call-arg]


def test_source_claim_rejects_non_enum_classification() -> None:
    """Only the seven enum kinds are accepted as a classification."""
    with pytest.raises(ValidationError):
        schemas.SourceClaim(
            id="c1",
            project_id="p1",
            text="x",
            classification="totally-made-up",
            segment_ids=[],
        )


@pytest.mark.parametrize("schema", LINEAGE_SCHEMAS)
def test_branching_artefacts_default_to_active_pending_lineage(schema: type[BaseModel]) -> None:
    """Lineage-bearing artefacts start as an active, not-yet-selected branch."""
    fields = schema.model_fields
    assert fields["parent_id"].default is None
    assert fields["branch_status"].default is BranchStatus.ACTIVE
    assert fields["selection_status"].default is SelectionStatus.PENDING
