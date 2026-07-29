"""the voice system's tables

Revision ID: 0015_voice
Revises: 0014_position
Create Date: 2026-07-29

plan/10 → the personal voice system. Three tables, each holding something no
other row can reconstruct.

`voice_profile_versions` gives the phase-02 profile shell a scope, a version and
a snapshot. The document itself lives in the content-addressed store like every
other artefact; the row is its identity and its place in the hierarchy. Scope is
encoded as two nullable foreign keys — a project and an article, at most one set
— rather than one generic "target" column, so the database enforces that the
thing being scoped to actually exists.

`manual_edits` records a person changing prose by hand and, crucially, whether
that edit may be used as evidence about their style. The flag is stored at the
moment of the edit because it cannot be recovered later: a corrected statistic
and a reworded sentence look identical afterwards, and only one of them says
anything about how the author writes.

`voice_suggestions` is an inferred rule waiting for an answer. It is a row
precisely because the answer has not been given: a suggestion held in memory
would be one that disappeared, and one applied on detection would be the silent
self-modification plan/10 forbids. Its status reuses `findingstatus` rather than
inventing a parallel vocabulary — a suggestion about the author's style has the
same fates as a finding about their article, and two enums meaning the same three
things would drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "0015_voice"
down_revision: str | Sequence[str] | None = "0014_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the profile versions, the hand edits and the pending suggestions."""
    op.create_table(
        "voice_profile_versions",
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column(
            "scope",
            sa.Enum("global", "project", "article", name="voicescope", native_enum=False),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("article_id", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "branch_status",
            sa.Enum("active", "superseded", "abandoned", name="branchstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "selection_status",
            sa.Enum("pending", "selected", "rejected", name="selectionstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["voice_profile_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["voice_profiles.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["artifact_snapshots.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "manual_edits",
        sa.Column("article_version_id", sa.String(), nullable=False),
        sa.Column("before", sa.String(), nullable=False),
        sa.Column("after", sa.String(), nullable=False),
        sa.Column("made_by", sa.String(), nullable=False),
        sa.Column("voice_training_eligible", sa.Boolean(), nullable=False),
        sa.Column("made_at", groundscribe.db.UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["article_version_id"],
            ["article_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["made_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "voice_suggestions",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column(
            "scope",
            sa.Enum("global", "project", "article", name="voicescope", native_enum=False),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("habit", sa.String(), nullable=False),
        sa.Column("instruction", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
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
        ),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", groundscribe.db.UTCDateTime(), nullable=False),
        sa.Column("decided_at", groundscribe.db.UTCDateTime(), nullable=True),
        sa.Column("resulting_version_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["resulting_version_id"],
            ["voice_profile_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop them; the voice system reverts to the phase-02 profile shell."""
    op.drop_table("voice_suggestions")
    op.drop_table("manual_edits")
    op.drop_table("voice_profile_versions")
