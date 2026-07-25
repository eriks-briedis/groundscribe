"""SQLAlchemy model & ArtifactSnapshot store tests (phase 02).

Spec (plan/02 → Test-first specification):
- **Schema/DB parity:** each Pydantic schema round-trips to/from its SQLAlchemy
  row without loss; ``schema_version`` is recorded.
- **Claim classification:** a ``SourceClaim`` persists exactly one classification
  and retains its originating ``SourceSegment`` references (a real m2m link).
- **Snapshot immutability:** a written snapshot's content cannot be mutated;
  superseding it creates a *new* snapshot with a new hash and a
  ``parent_snapshot_id`` link.
- **Content-addressed dedup:** storing identical content twice yields one blob.
- **Lineage branching:** a single parent can have multiple children; each child
  records its parent; querying children returns both branches.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain import models, schemas
from groundscribe.domain.enums import (
    ArticleDepth,
    ArtifactType,
    BranchStatus,
    ClaimClassification,
)
from groundscribe.storage.blob_store import BlobStore
from groundscribe.storage.snapshot_store import SnapshotStore


def _seed_connected_graph(session: Session) -> dict[str, schemas._Entity]:
    """Persist one of every editorial entity in dependency order.

    Returns the originating Pydantic instances keyed by entity name so a parity
    round-trip can reload each row and validate it back to an equal schema.
    """
    originals: dict[str, schemas._Entity] = {}

    def add(name: str, schema: schemas._Entity, orm: object) -> None:
        session.add(orm)
        originals[name] = schema

    user = schemas.User(id="u1", name="Ada", email="ada@example.com")
    add("User", user, models.User(**user.model_dump()))

    project = schemas.Project(id="p1", user_id="u1", title="Caching write-up")
    add("Project", project, models.Project(**project.model_dump()))

    # Tuple-valued constraints are JSON columns; the ORM takes lists and the
    # schema validates them back into tuples, which is exactly what parity has to
    # prove for this entity (phase 06 §1).
    constraints = schemas.ProjectConstraints(
        id="pc1",
        project_id="p1",
        audience="senior backend engineers",
        platform="personal blog",
        depth=ArticleDepth.PRACTITIONER,
        target_length_words=1800,
        confidential_names=("Northwind",),
        allowed_providers=("ollama",),
        trace_retention_consent=True,
    )
    add(
        "ProjectConstraints",
        constraints,
        models.ProjectConstraints(
            **constraints.model_dump()
            | {
                "confidential_names": list(constraints.confidential_names),
                "allowed_providers": list(constraints.allowed_providers),
            }
        ),
    )

    doc = schemas.SourceDocument(id="d1", project_id="p1", title="Benchmark notes")
    add("SourceDocument", doc, models.SourceDocument(**doc.model_dump()))

    seg1 = schemas.SourceSegment(id="seg-1", document_id="d1", ordinal=0, text="p50 fell")
    seg2 = schemas.SourceSegment(id="seg-2", document_id="d1", ordinal=1, text="p99 fell")
    seg1_orm = models.SourceSegment(**seg1.model_dump())
    seg2_orm = models.SourceSegment(**seg2.model_dump())
    add("SourceSegment", seg1, seg1_orm)
    session.add(seg2_orm)

    claim = schemas.SourceClaim(
        id="c1",
        project_id="p1",
        text="Latency dropped after the cache change.",
        classification=ClaimClassification.DIRECTLY_SUPPORTED_FACT,
        segment_ids=["seg-1", "seg-2"],
    )
    claim_orm = models.SourceClaim(
        id="c1",
        project_id="p1",
        text=claim.text,
        classification=claim.classification,
        segments=[seg1_orm, seg2_orm],
    )
    add("SourceClaim", claim, claim_orm)

    gap = schemas.SourceGap(id="g1", project_id="p1", description="No cold-cache number")
    add("SourceGap", gap, models.SourceGap(**gap.model_dump()))

    answer = schemas.UserAnswer(id="a1", gap_id="g1", text="Cold cache adds 12ms")
    add("UserAnswer", answer, models.UserAnswer(**answer.model_dump()))

    arch = schemas.ContentArchitecture(id="arch1", project_id="p1", summary="One deep-dive")
    add("ContentArchitecture", arch, models.ContentArchitecture(**arch.model_dump()))

    concept = schemas.ArticleConcept(id="con1", architecture_id="arch1", title="Cache dive")
    add("ArticleConcept", concept, models.ArticleConcept(**concept.model_dump()))

    brief = schemas.ArticleBrief(id="b1", concept_id="con1", scope="Just the cache path")
    add("ArticleBrief", brief, models.ArticleBrief(**brief.model_dump()))

    article = schemas.Article(id="art1", project_id="p1", title="Caching deep dive")
    add("Article", article, models.Article(**article.model_dump()))

    version = schemas.ArticleVersion(id="v1", article_id="art1", ordinal=0)
    add("ArticleVersion", version, models.ArticleVersion(**version.model_dump()))

    review = schemas.Review(id="r1", article_version_id="v1", verdict="revise")
    add("Review", review, models.Review(**review.model_dump()))

    issue = schemas.ReviewIssue(id="i1", review_id="r1", severity="major", description="Scope")
    add("ReviewIssue", issue, models.ReviewIssue(**issue.model_dump()))

    plan = schemas.RevisionPlan(id="rp1", review_id="r1", summary="Trim section 3")
    add("RevisionPlan", plan, models.RevisionPlan(**plan.model_dump()))

    voice = schemas.VoiceProfile(id="vp1", user_id="u1", name="Ada default")
    add("VoiceProfile", voice, models.VoiceProfile(**voice.model_dump()))

    report = schemas.ValidationReport(id="vr1", article_version_id="v1", passed=True)
    add("ValidationReport", report, models.ValidationReport(**report.model_dump()))

    session.flush()
    return originals


SCHEMA_FOR: dict[str, type[BaseModel]] = {
    "User": schemas.User,
    "Project": schemas.Project,
    "ProjectConstraints": schemas.ProjectConstraints,
    "SourceDocument": schemas.SourceDocument,
    "SourceSegment": schemas.SourceSegment,
    "SourceClaim": schemas.SourceClaim,
    "SourceGap": schemas.SourceGap,
    "UserAnswer": schemas.UserAnswer,
    "ContentArchitecture": schemas.ContentArchitecture,
    "ArticleConcept": schemas.ArticleConcept,
    "ArticleBrief": schemas.ArticleBrief,
    "Article": schemas.Article,
    "ArticleVersion": schemas.ArticleVersion,
    "Review": schemas.Review,
    "ReviewIssue": schemas.ReviewIssue,
    "RevisionPlan": schemas.RevisionPlan,
    "VoiceProfile": schemas.VoiceProfile,
    "ValidationReport": schemas.ValidationReport,
}


def test_every_entity_round_trips_schema_to_row_without_loss(db_session: Session) -> None:
    """Persist one of each entity, reload it, and validate back to an equal schema."""
    originals = _seed_connected_graph(db_session)
    db_session.commit()

    for name, original in originals.items():
        model_cls = getattr(models, name)
        row = db_session.get(model_cls, original.id)
        assert row is not None, f"{name} row not found after commit"
        reloaded = SCHEMA_FOR[name].model_validate(row)
        assert reloaded == original, f"{name} did not round-trip losslessly"
        assert reloaded.schema_version == 1


def test_source_claim_persists_classification_and_segment_links(db_session: Session) -> None:
    """The claim keeps its single classification and both source-segment references."""
    _seed_connected_graph(db_session)
    db_session.commit()

    claim = db_session.get(models.SourceClaim, "c1")
    assert claim is not None
    assert claim.classification is ClaimClassification.DIRECTLY_SUPPORTED_FACT
    assert sorted(seg.id for seg in claim.segments) == ["seg-1", "seg-2"]
    # The link is navigable from the segment side too.
    seg = db_session.get(models.SourceSegment, "seg-1")
    assert seg is not None
    assert "c1" in {c.id for c in seg.claims}


def test_snapshot_write_is_content_addressed_and_dedupes(
    db_session: Session, tmp_path: Path
) -> None:
    """Two snapshots of identical content share one blob and one content hash."""
    store = SnapshotStore(db_session, BlobStore(tmp_path))
    content = b'{"body": "same prose"}'

    a = store.write(artifact_type=ArtifactType.ARTICLE_VERSION, content=content)
    b = store.write(artifact_type=ArtifactType.ARTICLE_VERSION, content=content)

    assert a.id != b.id
    assert a.content_hash == b.content_hash
    assert a.content_location == b.content_location
    blobs = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(blobs) == 1


def test_snapshot_content_is_immutable_supersession_forks_a_child(
    db_session: Session, tmp_path: Path
) -> None:
    """Superseding a snapshot leaves it intact and creates a linked child."""
    store = SnapshotStore(db_session, BlobStore(tmp_path))
    parent = store.write(artifact_type=ArtifactType.ARTICLE_VERSION, content=b"draft one")

    child = store.fork_from(parent, content=b"draft two")

    assert child.parent_snapshot_id == parent.id
    assert child.content_hash != parent.content_hash
    # The parent is untouched: same hash, still the original bytes.
    assert store.read(parent) == b"draft one"
    assert store.verify(parent) is True
    # There is no in-place update path on the store.
    assert not hasattr(store, "update")


def test_snapshot_lineage_supports_multiple_children(db_session: Session, tmp_path: Path) -> None:
    """One parent draft can fork into two rewrites; both are its children."""
    store = SnapshotStore(db_session, BlobStore(tmp_path))
    parent = store.write(artifact_type=ArtifactType.ARTICLE_VERSION, content=b"base draft")

    rewrite_a = store.fork_from(parent, content=b"rewrite A")
    rewrite_b = store.fork_from(parent, content=b"rewrite B")

    children = store.children_of(parent)
    assert {c.id for c in children} == {rewrite_a.id, rewrite_b.id}
    assert all(c.parent_snapshot_id == parent.id for c in children)


def test_snapshot_tampering_is_detected(db_session: Session, tmp_path: Path) -> None:
    """A snapshot whose blob is mutated on disk fails verification."""
    blob_store = BlobStore(tmp_path)
    store = SnapshotStore(db_session, blob_store)
    snap = store.write(artifact_type=ArtifactType.REVIEW, content=b"verdict: accept")

    (tmp_path / snap.content_location).write_bytes(b"verdict: reject")

    assert store.verify(snap) is False


def test_new_snapshot_defaults_to_active_branch(db_session: Session, tmp_path: Path) -> None:
    store = SnapshotStore(db_session, BlobStore(tmp_path))
    snap = store.write(artifact_type=ArtifactType.VOICE_PROFILE, content=b"voice v1")
    assert snap.branch_status is BranchStatus.ACTIVE


def test_children_of_returns_empty_for_a_leaf(db_session: Session, tmp_path: Path) -> None:
    store = SnapshotStore(db_session, BlobStore(tmp_path))
    leaf = store.write(artifact_type=ArtifactType.SOURCE_MODEL, content=b"only child-free")
    assert store.children_of(leaf) == []


def test_article_version_can_reference_a_snapshot(db_session: Session, tmp_path: Path) -> None:
    """An ArticleVersion's snapshot_id is a real FK into artifact_snapshots."""
    _seed_connected_graph(db_session)
    store = SnapshotStore(db_session, BlobStore(tmp_path))
    snap = store.write(artifact_type=ArtifactType.ARTICLE_VERSION, content=b"prose")

    version = db_session.get(models.ArticleVersion, "v1")
    assert version is not None
    version.snapshot_id = snap.id
    db_session.commit()

    reloaded = db_session.execute(
        select(models.ArticleVersion).where(models.ArticleVersion.id == "v1")
    ).scalar_one()
    assert reloaded.snapshot_id == snap.id
