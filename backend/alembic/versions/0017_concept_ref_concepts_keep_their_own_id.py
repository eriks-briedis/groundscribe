"""article concepts keep their own id, and remember the model's label

Revision ID: 0017_concept_ref
Revises: 0016_gap_ref

Phase 06 keyed ``article_concepts`` on the label the model gave each proposed
article, so a second proposal that reused one collided. The row now has its own
id and keeps the label as ``ref`` — the same shape ``source_gaps`` took in 0016
and ``review_issues`` has had since phase 07.

It matters more here than in either of those: a concept's id is what the API
addresses an *article* by, so every draft, review, score and approval for the
life of a project hung off an identifier a language model chose.

Existing rows keep their id and take their label from it: it *was* the label, so
nothing is invented and every article, brief and version that references the row
still resolves.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_concept_ref"
down_revision = "0016_gap_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "article_concepts", sa.Column("ref", sa.String(), nullable=False, server_default="")
    )
    op.execute("UPDATE article_concepts SET ref = id WHERE ref = ''")


def downgrade() -> None:
    op.drop_column("article_concepts", "ref")
