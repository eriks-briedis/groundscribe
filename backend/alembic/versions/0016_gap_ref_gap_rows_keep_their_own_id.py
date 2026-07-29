"""gap rows keep their own id, and remember the model's label

Revision ID: 0016_gap_ref
Revises: 0015_voice

Phase 06 keyed ``source_gaps`` on the label the model gave each question, so a
second round that reused one collided. The row now has its own id and keeps the
label as ``ref`` — the pattern phase 07 already uses for review findings, where a
reviewer renumbers from one every round and two rounds must coexist.

Existing rows keep their id and take their label from it: it *was* the label,
so nothing is invented and nothing that referenced the row breaks.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_gap_ref"
down_revision = "0015_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_gaps", sa.Column("ref", sa.String(), nullable=False, server_default=""))
    op.execute("UPDATE source_gaps SET ref = id WHERE ref = ''")


def downgrade() -> None:
    op.drop_column("source_gaps", "ref")
