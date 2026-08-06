"""What a call cost, when anybody can say.

Providers report tokens. They do not report money, and the exchange rate between
the two is account-specific and changes without notice. So this is the one place
that knows about cost, and it is deliberately small: a table loaded from config,
and one multiplication.

**Prices are configuration, not code**, for the reason prompts and routing
already are (plan/04): they change often, a change to any of them changes what
the system reports, and a person must be able to open, diff and edit them without
forking the package. ``config/model-pricing.yaml`` sits beside the routing policy
and is versioned the same way.

**An unpriced model costs ``None``, never zero.** Phase 12 set the rule and
phase 14 repeated it for the metrics surface. An installation reporting $0.00 for
a model nobody entered a price for is stating a fact it does not have — and zero
is the number somebody acts on. Zero tokens against a *priced* model is a
different thing entirely, and reads as the genuine zero it is.

**The shipped table is empty on purpose.** Nobody preparing this repository can
know what a given account pays; a guessed price produces a cost metric that is
confidently wrong, which is worse than one honestly absent because the first gets
believed. The file carries the format and where to find the real numbers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from groundscribe.paths import config_root
from groundscribe.provenance.schemas import TokenUsage

#: Where the table lives inside the config root.
PRICING_CONFIG_FILENAME: Final = "model-pricing.yaml"

#: Prices are quoted per million tokens because that is how providers quote them.
#: Converting at the point of entry would mean the file no longer matched the
#: page it was copied from, which is the one thing that makes it checkable.
TOKENS_PER_UNIT: Final = 1_000_000


class PricingConfigError(Exception):
    """Raised when the pricing table cannot be read or does not validate.

    Loud rather than silent: an unreadable price table that fell back to "no
    prices" would present as a cost metric that quietly stopped working, which is
    indistinguishable from an installation nobody has priced yet.
    """


class ModelPrice(BaseModel):
    """What one model charges, per million tokens in and out.

    ``cached_input_per_million`` is optional and, when a provider reports cached
    input at all, is what that portion is billed at. Left unset it changes
    nothing: cached tokens are priced as ordinary input, which is what happened
    before the breakdown was recorded and is the safe direction — over-stating a
    cost is a figure somebody questions, while under-stating one is a figure they
    believe.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_million: float = Field(ge=0.0)
    output_per_million: float = Field(ge=0.0)
    cached_input_per_million: float | None = Field(default=None, ge=0.0)
    note: str = ""

    def cost(self, usage: TokenUsage) -> float:
        """What the call cost, with cached input billed at its own rate if there is one.

        The cached tokens are *subtracted* from the input total rather than added
        beside it, because providers report them as a component of it. Adding
        would charge the same tokens twice, which is the arithmetic error this
        change most invites.
        """
        cached = usage.cached_input_tokens or 0
        rate = self.cached_input_per_million
        if rate is None or cached <= 0:
            return (
                usage.input_tokens * self.input_per_million
                + usage.output_tokens * self.output_per_million
            ) / TOKENS_PER_UNIT
        # Defensive: a provider reporting more cached tokens than input tokens is
        # reporting something this cannot price, and a negative charge is worse
        # than an over-estimate.
        cached = min(cached, usage.input_tokens)
        return (
            (usage.input_tokens - cached) * self.input_per_million
            + cached * rate
            + usage.output_tokens * self.output_per_million
        ) / TOKENS_PER_UNIT


class PricingTable(BaseModel):
    """A versioned map from model id to what it charges."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "0"
    description: str = ""
    currency: str = "usd"
    models: dict[str, ModelPrice] = Field(default_factory=dict)

    def price(self, usage: TokenUsage, *, model: str) -> float | None:
        """What this call cost, or ``None`` when the model has no price here.

        Matched on the **longest listed prefix**, because a provider answers with
        a pinned snapshot id (``gpt-5-2026-01-01``) even when asked for a family
        (``gpt-5``), and phase 04 records the exact id it answered with. Without
        prefix matching, every real call would come back unpriced precisely when
        the table had been filled in correctly.

        Longest rather than first: ``gpt-5-mini`` must never be priced as
        ``gpt-5``. Shortest match would charge the cheap model at the flagship's
        rate, and that is an error nobody catches, because the number only looks
        high.
        """
        entry = self.entry_for(model)
        return None if entry is None else round(entry.cost(usage), 10)

    def entry_for(self, model: str) -> ModelPrice | None:
        """The price that applies to ``model``: exact, or its dated snapshot.

        Prefix matching exists for **snapshots** — ``gpt-5-2026-01-01`` priced by
        a ``gpt-5`` entry — and for nothing else. A bare ``startswith`` would also
        make ``gpt-5-mini`` match ``gpt-5``, charging the cheap model at the
        flagship's rate for an installation that had priced only one of them.
        That is the error nobody catches, because the number merely looks high.

        So the remainder after the prefix has to look like a version suffix: a
        dash followed by a digit. ``-2026-01-01`` qualifies, ``-mini`` does not,
        and an unpriced sibling stays honestly unpriced.
        """
        if model in self.models:
            return self.models[model]
        candidates = [name for name in self.models if _is_snapshot_of(model, name)]
        if not candidates:
            return None
        return self.models[max(candidates, key=len)]

    @classmethod
    def from_yaml(cls, path: Path) -> PricingTable:
        """Load a pricing table from a YAML file."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise PricingConfigError(f"cannot read pricing config {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise PricingConfigError(f"invalid YAML in pricing config {path}: {exc}") from exc
        try:
            return cls.model_validate(raw or {})
        except ValidationError as exc:
            raise PricingConfigError(f"invalid pricing config {path}: {exc}") from exc


def _is_snapshot_of(model: str, family: str) -> bool:
    """Whether ``model`` is a dated build of ``family`` rather than a relative."""
    if not model.startswith(family):
        return False
    suffix = model[len(family) :]
    return len(suffix) > 1 and suffix[0] == "-" and suffix[1].isdigit()


def default_pricing() -> PricingTable:
    """The shipped pricing table from the config root.

    A missing file is an empty table rather than an error: pricing is optional,
    and an installation that has not entered any is in a valid state — it simply
    reports cost as unknown.
    """
    path = config_root() / PRICING_CONFIG_FILENAME
    return PricingTable.from_yaml(path) if path.is_file() else PricingTable()


__all__ = [
    "PRICING_CONFIG_FILENAME",
    "TOKENS_PER_UNIT",
    "ModelPrice",
    "PricingConfigError",
    "PricingTable",
    "default_pricing",
]
