"""an invocation records the cached and reasoning halves of what it spent

Revision ID: 0024_token_breakdown
Revises: 0023_auto_advance

Both figures were being reported by the provider on every call and discarded by
the adapter that read them. `chatgpt.py`'s own docstring said as much — "this
backend breaks out reasoning and cached tokens, and reasoning is the half of the
output that no prompt change can shorten" — and then read neither.

What that cost was the ability to answer two questions.

**What a run actually charged.** Cached input is billed below the input rate, so
counting it at full price over-states every run that got a cache hit. The pricing
table's note on `gpt-5` records the consequence in the other direction: its rates
were reconstructed from a real run, and "cached input is billed lower than the
input rate, so a run with cache hits would imply a slightly higher true rate".
The reconstruction was checkable only because that run happened to have no hits.

**What `reasoning_effort: high` costs.** Six of thirteen stages run at high, and
reasoning bills at the output rate — eight times the input rate on `gpt-5`. There
was no number behind that choice, which made it a decision nobody could revisit,
including on the subscription profile where capacity is the whole budget.

Nullable, and every existing row stays null. That is the rule the rest of this
schema keeps and the one that matters most here: `cost_usd` is nullable so that
"nobody priced this model" cannot be read as "this call was free", and the same
distinction applies twice over. A backfilled zero would assert that the 45 calls
already on file used no reasoning tokens, which is false — six stages ran at high
effort — and a wrong number gets believed where an absent one gets checked.

Components of the existing totals rather than additions to them. A provider
counts cached input inside `input_tokens` and reasoning inside `output_tokens`,
so anything summing all four would double-charge; `ModelPrice.cost` subtracts
rather than adds, for the same reason.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_token_breakdown"
down_revision = "0023_auto_advance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_invocations",
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "model_invocations",
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_invocations", "reasoning_tokens")
    op.drop_column("model_invocations", "cached_input_tokens")
