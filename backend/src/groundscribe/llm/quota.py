"""What a subscription run has consumed, since dollars cannot say (phase 15).

A metered call has a price, and `config/model-pricing.yaml` turns its tokens into
a number anyone can check against a bill. A subscription call has no price: the
marginal cost of the next one is zero right up until the moment it is refused.
Reporting that as ``$0.00`` is true and useless, because the resource that
actually runs out is the plan's own rate limit.

So the same tokens are counted rather than costed, in rolling windows, and the
answer is *consumption* rather than a percentage. No cap is named here on
purpose. Limits differ by plan, change without notice, and are not published in a
form this file could be checked against — the same reason the pricing table
shipped empty. A guessed ceiling would produce "83% used", which is precisely the
kind of number that gets believed.

Nothing new is written to produce this. Every model invocation already records
its provider, its tokens and when it started (plan/03), so consumption is a
projection over the trace, in the same way the job event stream is. A separate
counter would be a second record of one fact, and the two would eventually
disagree about a run somebody had to explain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from groundscribe.provenance import models

#: The windows reported, longest last.
#:
#: Two, because subscription plans are throttled on two scales at once — a short
#: burst window and a long one — and a run that is fine against the first can
#: still exhaust the second. The labels are the units a person reading a plan
#: page will recognise; the durations are rolling rather than calendar, because a
#: rolling window is what a burst limit actually is and a calendar month would
#: answer a question nobody's plan asks.
QUOTA_WINDOWS: Final[tuple[tuple[str, timedelta], ...]] = (
    ("5h", timedelta(hours=5)),
    ("7d", timedelta(days=7)),
)

#: Providers whose calls consume a subscription rather than credits.
SUBSCRIPTION_PROVIDERS: Final[frozenset[str]] = frozenset({"chatgpt"})


@dataclass(frozen=True)
class QuotaWindow:
    """What one provider spent inside one window."""

    provider: str
    label: str
    since: datetime
    calls: int
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def subscription_usage(
    session: Session,
    *,
    providers: Sequence[str] | None = None,
    now: datetime | None = None,
) -> tuple[QuotaWindow, ...]:
    """Consumption per provider per window, oldest window last.

    A provider with no calls in a window is still reported, as zero. Silence
    would be indistinguishable from "nothing recorded yet", and the difference
    matters to somebody deciding whether a run is safe to start.
    """
    moment = now or datetime.now(UTC)
    wanted = tuple(providers) if providers is not None else tuple(sorted(SUBSCRIPTION_PROVIDERS))

    windows: list[QuotaWindow] = []
    for provider in wanted:
        for label, span in QUOTA_WINDOWS:
            since = moment - span
            row = session.execute(
                select(
                    func.count(models.ModelInvocation.id),
                    func.coalesce(func.sum(models.ModelInvocation.input_tokens), 0),
                    func.coalesce(func.sum(models.ModelInvocation.output_tokens), 0),
                ).where(
                    models.ModelInvocation.provider == provider,
                    models.ModelInvocation.started_at >= since,
                )
            ).one()
            windows.append(
                QuotaWindow(
                    provider=provider,
                    label=label,
                    since=since,
                    calls=int(row[0] or 0),
                    input_tokens=int(row[1] or 0),
                    output_tokens=int(row[2] or 0),
                )
            )
    return tuple(windows)


__all__ = [
    "QUOTA_WINDOWS",
    "SUBSCRIPTION_PROVIDERS",
    "QuotaWindow",
    "subscription_usage",
]
