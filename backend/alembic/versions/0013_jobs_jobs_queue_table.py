"""the job queue's real table

Revision ID: 0013_jobs
Revises: 0012_validation
Create Date: 2026-07-29

plan/09 → *Jobs table + worker*. Phase 03 created a five-column shell so the
record partition would be complete before the queue existed; this revision
replaces it with the table the queue actually needs.

Replaces rather than alters. The shell was never written to by any code path —
nothing enqueued a job before this phase — so there is no data to preserve, and
a drop/create says that plainly instead of hiding it behind fourteen ALTERs that
would each have to invent a server default for a column no row has.

Two shapes here are decisions rather than mechanics:

- ``active_key`` is a nullable column under a unique constraint, not a partial
  unique index over "status in (pending, running)". Both express "at most one
  live job per unit of work"; only one of them is written identically on SQLite
  and PostgreSQL (plan/00 → Tech stack: avoid backend-specific behaviour).
- ``status`` gets its own ``jobstatus`` vocabulary rather than reusing
  ``executionstatus``. A job can be *superseded* — decided against before it
  ran — which is meaningless for a stage execution, and widening the shared
  enum would have offered that status to rows that can never hold it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "0013_jobs"
down_revision: str | Sequence[str] | None = "0012_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_STATUS = sa.Enum(
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
    name="jobstatus",
    native_enum=False,
)

_EXECUTION_STATUS = sa.Enum(
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    name="executionstatus",
    native_enum=False,
)


def upgrade() -> None:
    """Replace the phase-03 shell with the queue's working table."""
    op.drop_table("jobs")
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("status", _JOB_STATUS, nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("pipeline_run_id", sa.String(), nullable=False),
        sa.Column("stage_execution_id", sa.String(), nullable=True),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("active_key", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", groundscribe.db.UTCDateTime(), nullable=True),
        sa.Column("heartbeat_at", groundscribe.db.UTCDateTime(), nullable=True),
        sa.Column("created_at", groundscribe.db.UTCDateTime(), nullable=False),
        sa.Column("completed_at", groundscribe.db.UTCDateTime(), nullable=True),
        sa.Column("superseded_by_id", sa.String(), nullable=True),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_jobs_project_id_projects"
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"], ["pipeline_runs.id"], name="fk_jobs_pipeline_run_id_pipeline_runs"
        ),
        sa.ForeignKeyConstraint(
            ["stage_execution_id"],
            ["stage_executions.id"],
            name="fk_jobs_stage_execution_id_stage_executions",
        ),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["jobs.id"], name="fk_jobs_superseded_by_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_jobs_active_key"),
    )


def downgrade() -> None:
    """Restore the phase-03 shell, empty as it was."""
    op.drop_table("jobs")
    op.create_table(
        "jobs",
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("status", _EXECUTION_STATUS, nullable=False),
        sa.Column("pipeline_run_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", groundscribe.db.UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
