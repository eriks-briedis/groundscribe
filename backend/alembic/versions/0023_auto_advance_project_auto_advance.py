"""a project's run starts pipeline-owned work without being asked

Revision ID: 0023_auto_advance
Revises: 0022_routing_profile

phase 16. Every stage the pipeline owns still had to be started by hand: a run
that had just finished extraction sat in ``source_model_ready`` until somebody
pressed a button to propose an architecture, and again at every step after it.
The gates a *person* owns were doing their job; the ones nobody owns were being
pressed by a person for no reason.

Non-null, defaulting to true, which is a behaviour change for every existing
project and is meant to be. The alternative — default off, so nothing changes
until each project opts in — would leave the setting switched off everywhere it
matters and describe the new behaviour as the exception. Auto-advance never
crosses a gate a person owns, so the change it makes to an existing run is that
work the run was already waiting to do begins without a click.

On ``project_constraints`` rather than ``projects``, unlike ``routing_profile``
one revision back, and for the reason that one is not: routing already has a
per-execution record of what was in force (``policy_version``), so versioning it
twice would give two answers. Nothing records whether a run was driving itself,
and "did anyone ask for this draft?" is exactly the question the versioned
constraints exist to answer for the artefact in front of you.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_auto_advance"
down_revision = "0022_routing_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_constraints",
        sa.Column("auto_advance", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("project_constraints", "auto_advance")
