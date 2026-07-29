"""evaluation datasets, experiment arms, results and preferences

Revision ID: 0018_experiments
Revises: 0017_concept_ref

Phase 12's own tables. ``experiment_runs`` existed as a shell from phase 03 —
present so provenance records could reference an experiment from the start — and
gains the four columns that give it meaning: the dataset it runs over, what it is
for, and when it finished.

Everything else is new. The datasets and their entries are the corpus; the arms
are the configurations under test; the results are one arm against one entry; the
preferences are a person saying which arm did better. Rows rather than payloads,
because every question asked of them is asked of the set.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_experiments"
down_revision = "0017_concept_ref"
branch_labels = None
depends_on = None

_EXECUTION_STATUS = sa.String()


def upgrade() -> None:
    op.create_table(
        "evaluation_datasets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sensitive_included", sa.JSON(), nullable=False),
    )
    op.create_table(
        "evaluation_dataset_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.Column(
            "dataset_id", sa.String(), sa.ForeignKey("evaluation_datasets.id"), nullable=False
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "stage_execution_id",
            sa.String(),
            sa.ForeignKey("stage_executions.id"),
            nullable=False,
        ),
        sa.Column(
            "reference_snapshot_id",
            sa.String(),
            sa.ForeignKey("artifact_snapshots.id"),
            nullable=False,
        ),
    )
    op.create_table(
        "experiment_arms",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.Column(
            "experiment_id", sa.String(), sa.ForeignKey("experiment_runs.id"), nullable=False
        ),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("baseline", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("variables", sa.JSON(), nullable=False),
    )
    op.create_table(
        "experiment_results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.Column(
            "experiment_id", sa.String(), sa.ForeignKey("experiment_runs.id"), nullable=False
        ),
        sa.Column("arm_id", sa.String(), sa.ForeignKey("experiment_arms.id"), nullable=False),
        sa.Column(
            "entry_id",
            sa.String(),
            sa.ForeignKey("evaluation_dataset_entries.id"),
            nullable=False,
        ),
        sa.Column("job_id", sa.String(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "stage_execution_id", sa.String(), sa.ForeignKey("stage_executions.id"), nullable=True
        ),
        sa.Column("status", _EXECUTION_STATUS, nullable=False, server_default="pending"),
        sa.Column("error_message", sa.String(), nullable=True),
    )
    op.create_table(
        "experiment_preferences",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.Column(
            "experiment_id", sa.String(), sa.ForeignKey("experiment_runs.id"), nullable=False
        ),
        sa.Column(
            "entry_id",
            sa.String(),
            sa.ForeignKey("evaluation_dataset_entries.id"),
            nullable=False,
        ),
        sa.Column(
            "preferred_arm_id", sa.String(), sa.ForeignKey("experiment_arms.id"), nullable=False
        ),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Batch mode, because ``dataset_id`` carries a foreign key and SQLite cannot
    # ALTER a constraint into an existing table. Alembic's copy-and-move strategy
    # is a no-op on PostgreSQL, so one spelling serves both backends — which is
    # what plan/00 asks for when it says to avoid SQLite-specific behaviour.
    with op.batch_alter_table("experiment_runs") as batch:
        batch.add_column(sa.Column("dataset_id", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("description", sa.String(), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("created_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_experiment_runs_dataset_id", "evaluation_datasets", ["dataset_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("experiment_runs") as batch:
        batch.drop_constraint("fk_experiment_runs_dataset_id", type_="foreignkey")
        batch.drop_column("completed_at")
        batch.drop_column("created_by")
        batch.drop_column("description")
        batch.drop_column("dataset_id")
    op.drop_table("experiment_preferences")
    op.drop_table("experiment_results")
    op.drop_table("experiment_arms")
    op.drop_table("evaluation_dataset_entries")
    op.drop_table("evaluation_datasets")
