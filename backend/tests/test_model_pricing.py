"""What a call cost, when anybody can say (phase 14's cost metric, wired here).

The provider reports tokens; it does not report money. Something has to turn one
into the other, and the question is where the numbers live and what happens when
they are absent.

**Prices are configuration, not code.** They change without warning, they differ
per account, and a number compiled into the package would be wrong for someone on
the day they installed it. So they sit in `config/model-pricing.yaml` beside the
routing policy — versioned, hand-edited, and diffable — for the same reason
prompts and routing do.

**An unpriced model costs `None`, never zero.** phase 12 set the rule and
phase 14 repeated it: an installation reporting $0.00 for a model nobody entered
a price for is stating a fact it does not have, and a cost dashboard reading zero
is exactly the number somebody acts on.

The shipped file is therefore **empty of prices on purpose**, which these tests
assert. Guessing on a user's behalf produces a confidently wrong figure; leaving
it unset produces an honest `None` and a file that says where the real numbers
come from.
"""

from __future__ import annotations

import pytest

from groundscribe.llm.pricing import (
    PRICING_CONFIG_FILENAME,
    ModelPrice,
    PricingTable,
    default_pricing,
)
from groundscribe.paths import config_root
from groundscribe.provenance.schemas import TokenUsage

TABLE = PricingTable(
    version="test",
    models={
        "gpt-5": ModelPrice(input_per_million=1.25, output_per_million=10.0),
        "gpt-5-mini": ModelPrice(input_per_million=0.25, output_per_million=2.0),
    },
)

USAGE = TokenUsage(input_tokens=1_000_000, output_tokens=500_000)


def test_a_priced_model_costs_what_the_arithmetic_says() -> None:
    """Checkable by hand, which is the point of keeping the table this simple:
    one million in at $1.25, half a million out at $10 per million."""
    assert TABLE.price(USAGE, model="gpt-5") == pytest.approx(1.25 + 5.0)


def test_an_unpriced_model_costs_nothing_known() -> None:
    """Not zero. An installation reporting $0.00 for a model nobody priced is
    stating a fact it does not have, and zero is the figure somebody acts on."""
    assert TABLE.price(USAGE, model="o-something-new") is None


def test_a_dated_snapshot_is_priced_by_the_family_it_belongs_to() -> None:
    """Providers answer with a pinned id (`gpt-5-2026-01-01`) even when asked for
    a family (`gpt-5`), and phase 04 insists the *exact* id is what gets recorded.
    Without prefix matching every real call would come back unpriced, which would
    make the table useless precisely when it was configured correctly."""
    assert TABLE.price(USAGE, model="gpt-5-2026-01-01") == pytest.approx(6.25)


def test_the_longest_matching_prefix_wins() -> None:
    """`gpt-5-mini` must not be priced as `gpt-5`. Shortest-match would silently
    charge the cheap model at the flagship's rate — an error in the direction
    nobody checks, because the number merely looks high."""
    assert TABLE.price(USAGE, model="gpt-5-mini-2026-01-01") == pytest.approx(0.25 + 1.0)


def test_a_call_that_used_nothing_costs_zero_rather_than_nothing_known() -> None:
    """Zero tokens against a priced model is a genuine zero: it was measured.
    The distinction the whole module exists for, from the other side."""
    assert TABLE.price(TokenUsage(), model="gpt-5") == 0.0


def test_the_shipped_table_loads_and_names_its_version() -> None:
    """It is config, so it has to be readable, versioned and diffable like the
    routing policy beside it."""
    table = default_pricing()

    assert (config_root() / PRICING_CONFIG_FILENAME).is_file()
    assert table.version


def test_the_shipped_table_ships_no_prices_at_all() -> None:
    """Deliberate, and the reason is worth failing a test over.

    Nobody preparing this repository can know what a given account pays. A price
    written in on a guess produces a cost metric that is confidently wrong, which
    is worse than one that is honestly absent — the first gets believed. The file
    carries the format and where to find the real numbers; the first person who
    needs cost fills in two lines.

    If prices are ever shipped, this test should be deleted along with the
    reasoning above, not quietly adjusted.
    """
    assert default_pricing().models == {}
    assert default_pricing().price(USAGE, model="gpt-5") is None
