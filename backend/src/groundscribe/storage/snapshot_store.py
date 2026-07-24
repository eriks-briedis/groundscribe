"""Snapshot store: content-addressed artefacts with branching lineage (phase 02).

Bridges the :class:`~groundscribe.storage.blob_store.BlobStore` (the bytes) and
the :class:`~groundscribe.domain.models.ArtifactSnapshot` table (metadata +
lineage). It exposes no in-place update path: an artefact is never mutated, only
*superseded* by forking a new snapshot whose ``parent_snapshot_id`` links back
(plan/00 → immutable, branching snapshots over destructive edits).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.domain.models import ArtifactSnapshot
from groundscribe.storage.blob_store import BlobStore


class SnapshotStore:
    """Write-once artefact store over a session and a content-addressed blob store."""

    def __init__(self, session: Session, blob_store: BlobStore) -> None:
        self._session = session
        self._blobs = blob_store

    def write(
        self,
        *,
        artifact_type: ArtifactType,
        content: bytes,
        schema_version: int = 1,
        created_by_execution_id: str | None = None,
        parent: ArtifactSnapshot | None = None,
    ) -> ArtifactSnapshot:
        """Persist ``content`` as a new snapshot and return it.

        The bytes are deduplicated by the blob store, so two snapshots of
        identical content share one blob and one ``content_hash`` while remaining
        distinct rows with their own identity and lineage.
        """
        ref = self._blobs.put(content)
        snapshot = ArtifactSnapshot(
            id=uuid.uuid4().hex,
            artifact_type=artifact_type,
            schema_version=schema_version,
            created_by_execution_id=created_by_execution_id,
            content_hash=ref.content_hash,
            content_location=ref.location,
            size=ref.size,
            parent_snapshot_id=parent.id if parent is not None else None,
        )
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def fork_from(
        self,
        parent: ArtifactSnapshot,
        *,
        content: bytes,
        schema_version: int | None = None,
        created_by_execution_id: str | None = None,
    ) -> ArtifactSnapshot:
        """Supersede ``parent`` with a child snapshot carrying new content.

        The parent is left byte-for-byte intact; the returned child records the
        parent link, so lineage is preserved and never overwritten.
        """
        return self.write(
            artifact_type=parent.artifact_type,
            content=content,
            schema_version=parent.schema_version if schema_version is None else schema_version,
            created_by_execution_id=created_by_execution_id,
            parent=parent,
        )

    def children_of(self, parent: ArtifactSnapshot) -> list[ArtifactSnapshot]:
        """All snapshots that fork directly from ``parent`` (both branches)."""
        stmt = select(ArtifactSnapshot).where(ArtifactSnapshot.parent_snapshot_id == parent.id)
        return list(self._session.execute(stmt).scalars())

    def read(self, snapshot: ArtifactSnapshot) -> bytes:
        """Return the stored bytes for ``snapshot``."""
        return self._blobs.get(snapshot.content_hash)

    def verify(self, snapshot: ArtifactSnapshot) -> bool:
        """True iff the snapshot's stored bytes still hash to its ``content_hash``."""
        return self._blobs.verify(snapshot.content_hash)
