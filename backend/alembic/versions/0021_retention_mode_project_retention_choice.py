"""a project declares how much of its trace is kept

Revision ID: 0021_retention_mode
Revises: 0020_retention

plan/13 asks for the retention choice to be *explicit*. It lives with the rest of
the project's constraints rather than in a config file because constraints are
versioned and branch instead of being edited (phase 06): "what was this project's
retention mode when that run was recorded?" then stays answerable, which is
exactly the question someone asks when a trace turns out to be thinner than they
expected.

Existing projects read back as ``full``, which is what they had — every payload
class kept indefinitely, there being no other mode to choose.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_retention_mode"
down_revision = "0020_retention"
branch_labels = None
depends_on = None

_MODE = sa.Enum(
    "full",
    "redacted_full",
    "temporary_raw_retention",
    "no_raw_provider_payloads",
    "metadata_and_structured_only",
    "minimal_operational_logging",
    name="retentionmode",
    native_enum=False,
)


def upgrade() -> None:
    op.add_column(
        "project_constraints",
        sa.Column("trace_retention_mode", _MODE, nullable=False, server_default="full"),
    )


def downgrade() -> None:
    op.drop_column("project_constraints", "trace_retention_mode")
