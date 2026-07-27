"""validation report snapshot

Revision ID: 0012_validation
Revises: 0011_plan
Create Date: 2026-07-27

plan/08 → *ValidateFinalOutput*. The report — which checks ran, which objected,
and what was safely corrected — lives in a content-addressed snapshot; the row
keeps its identity, its verdict and its link to the version it checked.

The `passed` flag stays on the row rather than living only inside the document.
"has this version been validated, and did it pass" is a question asked of the
table (by the export guard, by the approval queue) and one answerable from a JSON
blob only by reading every blob.

Storing the checks that *ran* is the reason the document exists at all: a report
listing only its objections cannot distinguish an article that passed a check from
one where the check was never performed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "0012_validation"
down_revision: str | Sequence[str] | None = "0011_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_FK = "fk_validation_reports_snapshot_id_artifact_snapshots"


def upgrade() -> None:
    """Link a validation-report row to the snapshot holding its document."""
    with op.batch_alter_table("validation_reports", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snapshot_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(_SNAPSHOT_FK, "artifact_snapshots", ["snapshot_id"], ["id"])


def downgrade() -> None:
    """Drop the validation report's snapshot link."""
    with op.batch_alter_table("validation_reports", schema=None) as batch_op:
        batch_op.drop_constraint(_SNAPSHOT_FK, type_="foreignkey")
        batch_op.drop_column("snapshot_id")
