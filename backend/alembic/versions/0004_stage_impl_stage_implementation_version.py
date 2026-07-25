"""stage implementation version

Revision ID: 0004_stage_impl
Revises: 0003_provenance
Create Date: 2026-07-25

plan/06 → *Stage execution metadata*: a stage execution must record the stage
*implementation version* alongside the prompt, schema and model versions it ran
under. Without it, two executions of the same stage that behaved differently are
indistinguishable whenever the difference came from the code rather than from the
prompt.

Added with a ``server_default`` of the empty string so an existing database
upgrades in place: the engine's own workflow executions have no stage
implementation behind them and legitimately carry no version, which makes an
empty default the honest value rather than a placeholder.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0004_stage_impl"
down_revision: str | Sequence[str] | None = "0003_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the stage implementation version to stage executions."""
    with op.batch_alter_table("stage_executions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("impl_version", sa.String(), nullable=False, server_default="")
        )


def downgrade() -> None:
    """Drop the stage implementation version."""
    with op.batch_alter_table("stage_executions", schema=None) as batch_op:
        batch_op.drop_column("impl_version")
