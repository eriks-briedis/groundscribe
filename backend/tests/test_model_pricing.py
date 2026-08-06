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


def test_a_sibling_model_is_not_priced_as_its_shorter_relative() -> None:
    """The same hazard with the entry missing rather than present.

    Prefix matching exists for dated snapshots and nothing else. A bare
    `startswith` would price `gpt-5-mini` off the `gpt-5` entry on an
    installation that had only priced the flagship — quietly charging the cheap
    model at the expensive rate, which reads as a plausible bill.

    Found by the probe reporting a model as priced when nobody had priced it.
    """
    flagship_only = PricingTable(
        version="test",
        models={"gpt-5": ModelPrice(input_per_million=1.25, output_per_million=10.0)},
    )

    assert flagship_only.entry_for("gpt-5-mini") is None
    assert flagship_only.price(USAGE, model="gpt-5-mini") is None
    # ...while a dated build of the flagship itself still is priced.
    assert flagship_only.entry_for("gpt-5-2026-01-01") is not None


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


def test_every_shipped_price_says_where_it_came_from() -> None:
    """This replaces `test_the_shipped_table_ships_no_prices_at_all`, deleted on
    2026-08-05 when the table was filled in — as that test's own docstring asked,
    rather than adjusted around.

    Its reasoning was that nobody preparing this repository can know what an
    account pays, so a guessed price produces a cost metric that is confidently
    wrong. That still holds. What changed is that this installation stopped
    having to guess: a metered run spent 161,017 input and 77,690 output tokens
    and was charged almost exactly what these rates predict.

    So the guard moves rather than disappears. An empty table made a wrong number
    impossible by making every number impossible; what makes it checkable now is
    that each entry carries the provenance of its figures, including which of
    them is corroborated and which is still list price. A rate nobody can trace
    is the one that gets believed.
    """
    table = default_pricing()

    assert table.models, "the table is filled in; an empty one is now a regression"
    for model, entry in table.models.items():
        assert entry.note, f"{model} carries a price with no account of where it came from"


def test_an_unpriced_model_is_still_unknown_rather_than_free() -> None:
    """The rule the empty table used to enforce for everything, kept for whatever
    is not in the table: a model nobody has priced costs `None`, never `0.00`.

    Filling in two models does not make the third one free, and a fallback that
    silently costed zero would understate exactly the runs — the ones that failed
    over to another model — whose cost most wants explaining.
    """
    assert default_pricing().price(USAGE, model="some-model-nobody-priced") is None


# ---------------------------------------------------------------------------
# Cached input, which is billed at its own rate
# ---------------------------------------------------------------------------


def test_cached_input_is_billed_at_the_cached_rate() -> None:
    """A tenth of the input rate on `gpt-5`, and it has to actually apply.

    The figure existed on the provider's response from the first call and was
    dropped by the adapter reading it, so every run that got a cache hit was
    costed as though it had not. The pricing table's own note on `gpt-5` names
    the consequence from the other side: its rates were reconstructed from a real
    run, and a run with cache hits would have implied a higher true rate.
    """
    table = default_pricing()
    plain = TokenUsage(input_tokens=100_000, output_tokens=10_000)
    cached = TokenUsage(input_tokens=100_000, output_tokens=10_000, cached_input_tokens=90_000)

    assert table.price(plain, model="gpt-5") == pytest.approx(0.225)
    # 10k at 1.25/M + 90k at 0.125/M + 10k output at 10/M.
    assert table.price(cached, model="gpt-5") == pytest.approx(0.12375)


def test_cached_tokens_are_part_of_the_input_total_not_extra() -> None:
    """Providers count cached input *inside* `input_tokens`.

    So the cached portion is subtracted and re-priced, never added beside. Adding
    would charge the same tokens twice, which is the arithmetic error this whole
    change most invites — and it would look like a plausible bill.
    """
    price = ModelPrice(input_per_million=10.0, output_per_million=0.0, cached_input_per_million=0.0)
    everything_cached = TokenUsage(input_tokens=1_000_000, cached_input_tokens=1_000_000)

    assert price.cost(everything_cached) == pytest.approx(0.0)
    assert price.cost(TokenUsage(input_tokens=1_000_000)) == pytest.approx(10.0)


def test_a_model_with_no_cached_rate_prices_cached_tokens_as_ordinary_input() -> None:
    """The conservative direction, and the one that needs no migration.

    An over-stated cost is a figure somebody questions; an under-stated one is a
    figure they believe. So a table that has not been told the cached rate keeps
    charging full price rather than guessing a discount.
    """
    price = ModelPrice(input_per_million=10.0, output_per_million=0.0)
    cached = TokenUsage(input_tokens=1_000_000, cached_input_tokens=900_000)

    assert price.cost(cached) == pytest.approx(10.0)


def test_more_cached_tokens_than_input_cannot_produce_a_negative_charge() -> None:
    """A provider reporting that is reporting something this cannot price.

    Clamping rather than raising: the figure is an annotation on a call that has
    already happened and been paid for, and refusing to cost it would lose the
    call from every total to make a point about the provider's arithmetic.
    """
    price = ModelPrice(input_per_million=10.0, output_per_million=0.0, cached_input_per_million=1.0)

    assert price.cost(TokenUsage(input_tokens=1_000, cached_input_tokens=9_999)) >= 0.0
