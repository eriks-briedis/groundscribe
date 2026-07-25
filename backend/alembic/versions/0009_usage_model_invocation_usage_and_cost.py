"""model invocation usage and cost

Revision ID: 0009_usage
Revises: 0008_brief
Create Date: 2026-07-26

plan/06 → *Stage execution metadata* requires usage and cost among the facts a
stage records. Phase 03 modelled neither: ``TokenUsage`` existed in the LLM layer
and was dropped on the floor at the recorder.

Recorded per *attempt*, not per stage, and on failed attempts too. A run that
counted only its accepted calls would under-report exactly the runs that cost the
most — the ones that needed repairing — and the stage total is summed from these
rows so it cannot drift from what was stored.

``cost_usd`` is nullable rather than defaulted to zero. Not every provider reports
cost, and "free" is a different claim from "unknown"; a zero here would be the
system inventing a number nobody supplied. The token counts do default to zero,
because a call that reported no tokens consumed none that we can attribute.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0009_usage"
down_revision: str | Sequence[str] | None = "0008_brief"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record what each model call consumed, and what it cost if the provider said."""
    with op.batch_alter_table("model_invocations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop the usage and cost columns."""
    with op.batch_alter_table("model_invocations", schema=None) as batch_op:
        batch_op.drop_column("cost_usd")
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
