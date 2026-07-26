"""review findings

Revision ID: 0010_review
Revises: 0009_usage
Create Date: 2026-07-26

plan/07 §8. Phase 02 modelled a review issue as a severity and a description,
which is a complaint rather than a finding. The spec's field set is what makes a
finding *actionable*: where it points, the passage it concerns, the evidence
behind it, the source claim or brief clause it rests on, what would correct it,
which kind of stage should do the correcting, whether it stops publication, and
how sure the reviewer is.

Three of the columns are not the reviewer's at all:

- ``status`` and ``decision_reason`` are the author's. Reviewer output is evidence,
  not instruction (plan/07), so a finding carries what the author decided about it
  and why — and a rejection keeps the finding rather than deleting it, which is
  what lets a later round tell "already argued" from "never raised".
- ``fingerprint`` identifies "the same finding" across rounds. Derived from what
  the finding says and the evidence behind it, never from ``ref``, which the
  reviewer renumbers from one every round. Including the evidence is deliberate:
  raising a dismissed point with something new behind it produces a different
  fingerprint, which is exactly when re-raising it is legitimate.

``severity`` becomes a value-stored enum: it decides whether a finding buys a
revision round, and a free-text severity would eventually be spelled two ways.

Reviews gain a round number and their snapshot. Added columns carry
``server_default``s matching the ORM defaults so an existing database upgrades in
place; the new foreign key is named because SQLite's batch mode cannot drop an
anonymous constraint on the way down.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0010_review"
down_revision: str | Sequence[str] | None = "0009_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_FK = "fk_reviews_snapshot_id_artifact_snapshots"

_TEXT_COLUMNS = (
    "ref",
    "category",
    "location",
    "passage",
    "evidence",
    "source_ref",
    "brief_ref",
    "recommended_correction",
    "suggested_route",
    "fingerprint",
    "decided_by",
    "decision_reason",
)


def upgrade() -> None:
    """Give a finding everything needed to act on it, and the author's verdict."""
    with op.batch_alter_table("review_issues", schema=None) as batch_op:
        for column in _TEXT_COLUMNS:
            batch_op.add_column(sa.Column(column, sa.String(), nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column(
                "blocks_publication", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column("reviewer_confidence", sa.Float(), nullable=False, server_default="0.5")
        )
        batch_op.add_column(
            sa.Column(
                "status",
                sa.Enum(
                    "proposed",
                    "accepted",
                    "rejected",
                    "edited",
                    "suppressed",
                    name="findingstatus",
                    native_enum=False,
                ),
                nullable=False,
                server_default="proposed",
            )
        )
        batch_op.alter_column(
            "severity",
            existing_type=sa.String(),
            type_=sa.Enum(
                "blocking",
                "major",
                "minor",
                "optional",
                name="issueseverity",
                native_enum=False,
            ),
            existing_nullable=False,
        )

    with op.batch_alter_table("reviews", schema=None) as batch_op:
        batch_op.add_column(sa.Column("round", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("snapshot_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(_SNAPSHOT_FK, "artifact_snapshots", ["snapshot_id"], ["id"])


def downgrade() -> None:
    """Drop the finding field set and the review's round/snapshot."""
    with op.batch_alter_table("reviews", schema=None) as batch_op:
        batch_op.drop_constraint(_SNAPSHOT_FK, type_="foreignkey")
        batch_op.drop_column("snapshot_id")
        batch_op.drop_column("round")

    with op.batch_alter_table("review_issues", schema=None) as batch_op:
        batch_op.alter_column(
            "severity",
            existing_type=sa.Enum(
                "blocking",
                "major",
                "minor",
                "optional",
                name="issueseverity",
                native_enum=False,
            ),
            type_=sa.String(),
            existing_nullable=False,
        )
        batch_op.drop_column("status")
        batch_op.drop_column("reviewer_confidence")
        batch_op.drop_column("blocks_publication")
        for column in reversed(_TEXT_COLUMNS):
            batch_op.drop_column(column)
