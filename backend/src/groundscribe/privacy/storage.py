"""What the trace costs (phase 13).

plan/13 → *Trace-storage controls: compression, dedup (content addressing from
phase 02), retention policies, storage-use reporting*.

Reporting is what makes the other controls actionable. Retention modes and
payload expiry both trade evidence for space, and nobody can make that trade
without knowing what the space is — so the report breaks the number down. "Your
traces are 4GB" prompts no decision; "3.9GB of it is raw provider payloads"
prompts exactly one, and it is a retention mode away.

**Deduplication is reported, not assumed.** Two identical requests are two
snapshot rows and one blob. Summing snapshot sizes would overstate the store by
exactly the amount content addressing is already saving, so the report gives
both: what the records claim, and what is actually on disk.

**Compression is deliberately absent.** The store already dedups, the payloads
are small JSON documents, and a compression layer would change the on-disk format
for a saving nobody has measured. This module is what would produce that
measurement; the decision can be revisited when it says something. Recorded in
KNOWN-ISSUES rather than quietly skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from groundscribe.domain import models as domain_models
from groundscribe.provenance import models


@dataclass(frozen=True)
class TypeUsage:
    """How many artefacts of one kind there are, and how much they claim."""

    count: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class StorageReport:
    """What the artefact store holds, and what deduplication saved.

    ``total_bytes`` is what the records claim; ``stored_bytes`` is what is
    actually on disk once identical content is counted once. Both, because the
    first is what a retention decision is about and the second is what the disk
    is about, and quoting either alone answers the wrong question.
    """

    total_bytes: int = 0
    stored_bytes: int = 0
    snapshots: int = 0
    distinct_blobs: int = 0
    by_type: dict[str, TypeUsage] = field(default_factory=dict)
    project_id: str | None = None

    @property
    def deduplicated_bytes(self) -> int:
        """What content addressing has already saved."""
        return self.total_bytes - self.stored_bytes


def storage_report(session: Session, *, project_id: str | None = None) -> StorageReport:
    """How much the stored artefacts come to, broken down by kind.

    ``project_id`` narrows the report to the snapshots produced by that project's
    runs. Storage is charged to whoever is deciding what to do about it, and an
    installation-wide number is no help to someone deciding about one project.
    """
    snapshots = session.scalars(_query(project_id)).all()

    by_type: dict[str, TypeUsage] = {}
    seen: dict[str, int] = {}
    total = 0
    for snapshot in snapshots:
        total += snapshot.size
        kind = snapshot.artifact_type.value
        current = by_type.get(kind, TypeUsage())
        by_type[kind] = TypeUsage(count=current.count + 1, bytes=current.bytes + snapshot.size)
        # Content hash, not row id: that is the address the blob is stored under,
        # so two rows sharing one hash are one file.
        seen.setdefault(snapshot.content_hash, snapshot.size)

    return StorageReport(
        total_bytes=total,
        stored_bytes=sum(seen.values()),
        snapshots=len(snapshots),
        distinct_blobs=len(seen),
        by_type=by_type,
        project_id=project_id,
    )


def _query(project_id: str | None) -> Select[tuple[domain_models.ArtifactSnapshot]]:
    """Every snapshot, or every snapshot one project's runs produced."""
    query = select(domain_models.ArtifactSnapshot)
    if project_id is None:
        return query
    return (
        query.join(
            models.StageExecution,
            domain_models.ArtifactSnapshot.created_by_execution_id == models.StageExecution.id,
        )
        .join(
            models.PipelineRun,
            models.StageExecution.pipeline_run_id == models.PipelineRun.id,
        )
        .where(models.PipelineRun.project_id == project_id)
    )


__all__ = ["StorageReport", "TypeUsage", "storage_report"]
