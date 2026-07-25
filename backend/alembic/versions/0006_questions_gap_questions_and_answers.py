"""gap questions and answers

Revision ID: 0006_questions
Revises: 0005_ingestion
Create Date: 2026-07-26

plan/06 §3. Phase 02 modelled a gap as a description and a resolved flag, and an
answer as free text. Neither could carry what the spec's answer-provenance test
asks for.

Gaps gain the question actually put to the author, why it matters, the priority
that decides whether it surfaces, a group so related questions arrive together,
an order, and — importantly — ``surfaced``. Storing the prioritisation *decision*
is what distinguishes a gap the policy suppressed from one that was never
generated; without it the two are indistinguishable, and only one is a bug.

Answers gain the question and reason as they stood when asked (copied, so a later
re-wording cannot rewrite what the author was answering), the response type from
the six the queue accepts, who answered, and the diff produced by the rebuild the
answer caused. ``user_answer_gaps`` records every gap an answer closed, which is
more than one when a grouped question is answered in a single go — ``gap_id``
remains the question that was *put*, and the two are different facts.

Added columns carry ``server_default``s matching the ORM defaults so an existing
database upgrades in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0006_questions"
down_revision: str | Sequence[str] | None = "0005_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIFF_FK = "fk_user_answers_diff_snapshot_id_artifact_snapshots"


def upgrade() -> None:
    """Add the question/answer columns and the answer-to-gaps link table."""
    with op.batch_alter_table("source_gaps", schema=None) as batch_op:
        batch_op.add_column(sa.Column("question", sa.String(), nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("why_it_matters", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column(
                "priority",
                sa.Enum(
                    "blocking",
                    "high_value",
                    "optional",
                    name="gappriority",
                    native_enum=False,
                ),
                nullable=False,
                server_default="optional",
            )
        )
        batch_op.add_column(sa.Column("group", sa.String(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(
            sa.Column("surfaced", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("user_answers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("question", sa.String(), nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("why_it_matters", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column(
                "response_type",
                sa.Enum(
                    "answered",
                    "skipped",
                    "unknown",
                    "confidential",
                    "deferred",
                    "premise_incorrect",
                    name="answerresponse",
                    native_enum=False,
                ),
                nullable=False,
                server_default="answered",
            )
        )
        batch_op.add_column(
            sa.Column("answered_by", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("diff_snapshot_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(_DIFF_FK, "artifact_snapshots", ["diff_snapshot_id"], ["id"])

    op.create_table(
        "user_answer_gaps",
        sa.Column("answer_id", sa.String(), nullable=False),
        sa.Column("gap_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["answer_id"], ["user_answers.id"]),
        sa.ForeignKeyConstraint(["gap_id"], ["source_gaps.id"]),
        sa.PrimaryKeyConstraint("answer_id", "gap_id"),
    )


def downgrade() -> None:
    """Drop the link table and the question/answer columns."""
    op.drop_table("user_answer_gaps")

    with op.batch_alter_table("user_answers", schema=None) as batch_op:
        batch_op.drop_constraint(_DIFF_FK, type_="foreignkey")
        batch_op.drop_column("diff_snapshot_id")
        batch_op.drop_column("answered_by")
        batch_op.drop_column("response_type")
        batch_op.drop_column("why_it_matters")
        batch_op.drop_column("question")

    with op.batch_alter_table("source_gaps", schema=None) as batch_op:
        batch_op.drop_column("surfaced")
        batch_op.drop_column("ordinal")
        batch_op.drop_column("group")
        batch_op.drop_column("priority")
        batch_op.drop_column("why_it_matters")
        batch_op.drop_column("question")
