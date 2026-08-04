"""a project chooses which routing policy its stages run against

Revision ID: 0022_routing_profile
Revises: 0021_retention_mode

phase 15. Routing was one file for the installation, so moving a project to
another provider moved every project. This is the per-project half: a profile
name, resolved to ``config/model-routing.<name>.yaml`` beside the default.

Nullable, and NULL means the shipped default. That is not a placeholder for a
value nobody has set yet — it is the answer for every project that has not
chosen, which on an existing installation is all of them. A non-null default
would have to name a profile, and naming one here would move every existing
project onto it, which is the thing this migration exists to stop happening by
accident.

Deliberately not on ``project_constraints``: that table is versioned so an
artefact can say what was in force when it was made, and for routing the
``policy_version`` on every ``stage_executions`` row already says it. Two records
of the same fact can disagree, and the execution's is the one that ran.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_routing_profile"
down_revision = "0021_retention_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("routing_profile", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "routing_profile")
