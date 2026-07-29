"""a model call records the retention mode it was captured under

Revision ID: 0020_retention
Revises: 0019_confidentiality

Phase 13 lets a project choose how much of its trace is kept. The choice has to
travel with the call rather than be looked up when the question is asked:
shortening retention today must not rewrite what a run last month was recorded
under, and the expiry sweep needs to know which promise applied to each payload
it is about to drop.

Existing rows read back as ``full``, which is what they were: every payload class
was persisted indefinitely, because there was no other mode to record them under.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_retention"
down_revision = "0019_confidentiality"
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
        "model_invocations",
        sa.Column("retention_mode", _MODE, nullable=False, server_default="full"),
    )


def downgrade() -> None:
    op.drop_column("model_invocations", "retention_mode")
