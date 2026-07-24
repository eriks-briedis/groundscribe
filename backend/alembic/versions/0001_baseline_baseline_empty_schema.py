"""baseline empty schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-24

The root revision. It intentionally creates no schema: domain tables are added
by later phases (starting with the domain model in phase 02). Keeping an empty,
reversible baseline gives every environment a common starting point.
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: the baseline introduces no schema."""


def downgrade() -> None:
    """No-op: nothing to undo for the empty baseline."""
