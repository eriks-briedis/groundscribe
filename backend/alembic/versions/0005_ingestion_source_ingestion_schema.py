"""source ingestion schema

Revision ID: 0005_ingestion
Revises: 0004_stage_impl
Create Date: 2026-07-26

plan/06 §1 — source ingested immutably with segments, hashes, constraints and
confidentiality flags. Three changes, one purpose each:

- ``project_constraints`` — the bounds a project publishes under (audience,
  platform, depth, length, first person, confidential names, allowed providers,
  trace-retention consent), carried as a *lineage* table rather than columns on
  ``projects``: a brief written under an 1800-word limit was written under that
  limit, and editing the row in place would make the artefact's own record wrong.
- ``source_documents`` gains its content hash, the snapshot holding the ingested
  bytes, the input format and the confidentiality flag.
- ``source_segments`` gains its kind, its own content hash and the character
  offsets into the document. Those offsets are what make a claim's citation
  checkable — slice them out of the stored bytes and compare — instead of merely
  plausible.

Every added column carries a ``server_default`` so an existing database upgrades
in place; the defaults match the ORM's, so a row written by either path is the
same row. The new foreign key is named explicitly because SQLite's batch mode
rebuilds the table and cannot drop an anonymous constraint on the way back down.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Autogenerate renders project-defined column types (e.g. UTCDateTime) with
# their full module path, so the module has to be importable in every revision.
import groundscribe.db  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0005_ingestion"
down_revision: str | Sequence[str] | None = "0004_stage_impl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_FK = "fk_source_documents_snapshot_id_artifact_snapshots"


def upgrade() -> None:
    """Add project constraints and the ingestion columns on documents/segments."""
    op.create_table(
        "project_constraints",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_by_execution_id", sa.String(), nullable=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column(
            "depth",
            sa.Enum(
                "overview",
                "practitioner",
                "deep_dive",
                name="articledepth",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("target_length_words", sa.Integer(), nullable=True),
        sa.Column("first_person_allowed", sa.Boolean(), nullable=False),
        sa.Column("confidential_names", sa.JSON(), nullable=False),
        sa.Column("allowed_providers", sa.JSON(), nullable=False),
        sa.Column("trace_retention_consent", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["parent_id"], ["project_constraints.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("source_documents", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_format",
                sa.Enum(
                    "markdown",
                    "plain_text",
                    "pasted_notes",
                    name="sourceformat",
                    native_enum=False,
                ),
                nullable=False,
                server_default="plain_text",
            )
        )
        batch_op.add_column(
            sa.Column("content_hash", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("snapshot_id", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("confidential", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_foreign_key(
            _SNAPSHOT_FK, "artifact_snapshots", ["snapshot_id"], ["id"]
        )

    with op.batch_alter_table("source_segments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.Enum(
                    "heading",
                    "paragraph",
                    "list",
                    "code",
                    "quote",
                    name="segmentkind",
                    native_enum=False,
                ),
                nullable=False,
                server_default="paragraph",
            )
        )
        batch_op.add_column(
            sa.Column("content_hash", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("char_start", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("char_end", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    """Drop the ingestion columns and the project-constraints table."""
    with op.batch_alter_table("source_segments", schema=None) as batch_op:
        batch_op.drop_column("char_end")
        batch_op.drop_column("char_start")
        batch_op.drop_column("content_hash")
        batch_op.drop_column("kind")

    with op.batch_alter_table("source_documents", schema=None) as batch_op:
        batch_op.drop_constraint(_SNAPSHOT_FK, type_="foreignkey")
        batch_op.drop_column("confidential")
        batch_op.drop_column("snapshot_id")
        batch_op.drop_column("content_hash")
        batch_op.drop_column("source_format")

    op.drop_table("project_constraints")
