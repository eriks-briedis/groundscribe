"""a run's live workflow position

Revision ID: 0014_position
Revises: 0013_jobs
Create Date: 2026-07-29

plan/09 → the resumption phase 05 deferred here. A run's position stops being a
value held in one process for the length of a call and becomes a row, because a
worker runs a stage in a different process from the request that asked for it,
and a human approval arrives in a different request from the validation that
earned it.

Its own table rather than columns on `pipeline_runs`, and the reason is the
dependency direction: `pipeline_runs` belongs to the provenance schema, and
giving it a `WorkflowState` column would make provenance import the workflow.
The workflow already imports provenance.

It stores more than the state name on purpose. The three snapshot references are
what phase 05's guards compare against — an approved architecture, the version
in force, the version that passed validation — and the two JSON counters are the
rewrite ledger. A position holding only the state would let a resumed engine wave
through the replacements those guards exist to catch, and would reset the 3/2/1
rewrite limits on every job.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "0014_position"
down_revision: str | Sequence[str] | None = "0013_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table a run's position is resumed from."""
    op.create_table(
        "workflow_positions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("pipeline_run_id", sa.String(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "source_ingested",
                "source_model_extracting",
                "source_questions_required",
                "source_model_ready",
                "architecture_proposing",
                "architecture_review_required",
                "architecture_approved",
                "brief_generating",
                "brief_review_required",
                "draft_generating",
                "substantive_reviewing",
                "revision_plan_required",
                "substantive_rewriting",
                "voice_aligning",
                "scoring",
                "revision_required",
                "passed",
                "final_validating",
                "human_approval_required",
                "completed",
                "failed",
                "cancelled",
                "stalled",
                name="workflowstate",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("workflow_execution_id", sa.String(), nullable=True),
        sa.Column("approved_architecture_id", sa.String(), nullable=True),
        sa.Column("article_version_id", sa.String(), nullable=True),
        sa.Column("validated_version_id", sa.String(), nullable=True),
        sa.Column("rounds", sa.JSON(), nullable=False),
        sa.Column("grants", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_architecture_id"],
            ["artifact_snapshots.id"],
        ),
        sa.ForeignKeyConstraint(
            ["article_version_id"],
            ["artifact_snapshots.id"],
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["validated_version_id"],
            ["artifact_snapshots.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"],
            ["stage_executions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_run_id", name="uq_workflow_positions_run"),
    )


def downgrade() -> None:
    """Drop the position table; a run's place then lives only in memory again."""
    op.drop_table("workflow_positions")
