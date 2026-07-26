"""revision plan snapshot

Revision ID: 0011_plan
Revises: 0010_review
Create Date: 2026-07-26

plan/07 §9. The plan *document* — the changes, what must not change, and the
reconciliations that explain what was combined, deferred or rejected — lives in a
content-addressed snapshot; the row keeps its identity and its link back to the
review it was planned from.

Its own artefact rather than a field on the review, because "what the reviewer
said" and "what we decided to do about it" are two documents, and a person
arguing about the second should not have to read it through the first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0011_plan"
down_revision: str | Sequence[str] | None = "0010_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_FK = "fk_revision_plans_snapshot_id_artifact_snapshots"


def upgrade() -> None:
    """Link a revision-plan row to the snapshot holding its document."""
    with op.batch_alter_table("revision_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snapshot_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(_SNAPSHOT_FK, "artifact_snapshots", ["snapshot_id"], ["id"])


def downgrade() -> None:
    """Drop the revision plan's snapshot link."""
    with op.batch_alter_table("revision_plans", schema=None) as batch_op:
        batch_op.drop_constraint(_SNAPSHOT_FK, type_="foreignkey")
        batch_op.drop_column("snapshot_id")
