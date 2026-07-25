"""article brief snapshot

Revision ID: 0008_brief
Revises: 0007_architecture
Create Date: 2026-07-26

plan/06 §6. The brief *document* — every clause the spec enumerates, from the
opening direction to the definition of done — lives in a content-addressed
snapshot, not in columns. Seventeen clauses as seventeen columns would mean a
migration every time an editorial judgement changed, and none of them are joined
on; the row is the brief's identity, its lineage, and the two fields anything
queries by.

This adds the link from the row to that snapshot, named explicitly because
SQLite's batch mode cannot drop an anonymous constraint on the way back down.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0008_brief"
down_revision: str | Sequence[str] | None = "0007_architecture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_FK = "fk_article_briefs_snapshot_id_artifact_snapshots"


def upgrade() -> None:
    """Link a brief row to the snapshot holding its document."""
    with op.batch_alter_table("article_briefs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snapshot_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(_SNAPSHOT_FK, "artifact_snapshots", ["snapshot_id"], ["id"])


def downgrade() -> None:
    """Drop the brief's snapshot link."""
    with op.batch_alter_table("article_briefs", schema=None) as batch_op:
        batch_op.drop_constraint(_SNAPSHOT_FK, type_="foreignkey")
        batch_op.drop_column("snapshot_id")
