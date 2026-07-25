"""architecture concepts and locking

Revision ID: 0007_architecture
Revises: 0006_questions
Create Date: 2026-07-26

plan/06 §4 and §5.

A concept gains its ``thesis`` — kept apart from ``angle`` because they are
different things: the angle is how the material is approached, the thesis is what
the article asserts, and the brief is a contract against the second one. It also
gains an order, so a series reads in the order the architecture chose rather than
in whatever order the rows come back.

An architecture gains the snapshot holding the proposal it was persisted from, and
``locked``/``locked_by``. Locking is what makes plan/05's "no approved
architecture mutates silently" checkable at the row: a change after approval must
fork a new version and name who authorised it, and the flag is how "was this
approved when it changed?" is answered without replaying the run.

Added columns carry ``server_default``s matching the ORM defaults so an existing
database upgrades in place; the new foreign key is named because SQLite's batch
mode cannot drop an anonymous constraint on the way back down.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0007_architecture"
down_revision: str | Sequence[str] | None = "0006_questions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_FK = "fk_content_architectures_snapshot_id_artifact_snapshots"


def upgrade() -> None:
    """Add concept thesis/order and architecture snapshot/locking columns."""
    with op.batch_alter_table("article_concepts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("thesis", sa.String(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("content_architectures", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snapshot_id", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("locked_by", sa.String(), nullable=True))
        batch_op.create_foreign_key(_SNAPSHOT_FK, "artifact_snapshots", ["snapshot_id"], ["id"])


def downgrade() -> None:
    """Drop the architecture locking and concept ordering columns."""
    with op.batch_alter_table("content_architectures", schema=None) as batch_op:
        batch_op.drop_constraint(_SNAPSHOT_FK, type_="foreignkey")
        batch_op.drop_column("locked_by")
        batch_op.drop_column("locked")
        batch_op.drop_column("snapshot_id")

    with op.batch_alter_table("article_concepts", schema=None) as batch_op:
        batch_op.drop_column("ordinal")
        batch_op.drop_column("thesis")
