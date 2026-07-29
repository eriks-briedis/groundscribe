"""source segments and claims carry confidentiality flags

Revision ID: 0019_confidentiality
Revises: 0018_experiments

Phase 13's enforcement points — the request builder, final validation, and every
export — ask one question of each span of source material: may this cross this
boundary? Until now the only answer available was a project-wide list of
confidential *names* and a per-document boolean, neither of which can say that
one paragraph of an otherwise publishable postmortem must not be printed.

Two columns rather than one, on both tables. ``confidentiality`` is the
classification a person set (publishable / internal / confidential);
``excluded`` holds any boundaries they named on top of it. Storing only the
resolved set would lose which was which, and a record that cannot say what was
chosen cannot be argued with.

Claims are flagged independently of the segments behind them because extraction
can narrow a publishable paragraph into a claim that names a customer. With flags
on segments alone, the only way to withhold that claim would be to withhold the
paragraph it came from.

Both columns arrive with a server default so the rows already on disk answer the
question. Existing material reads back as *publishable*, which is the only safe
reading of a run that predates the flags: it has already been sent to a provider
and printed in an article, and any other default would retroactively make past
runs violations.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_confidentiality"
down_revision = "0018_experiments"
branch_labels = None
depends_on = None

_TABLES = ("source_segments", "source_claims")

_CLASSIFICATION = sa.Enum(
    "publishable",
    "internal",
    "confidential",
    name="confidentiality",
    native_enum=False,
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "confidentiality",
                _CLASSIFICATION,
                nullable=False,
                server_default="publishable",
            ),
        )
        op.add_column(
            table,
            sa.Column("excluded", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "excluded")
        op.drop_column(table, "confidentiality")
