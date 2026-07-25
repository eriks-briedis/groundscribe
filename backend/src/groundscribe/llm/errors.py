"""Provider-neutral LLM failures (phase 04).

Each provider SDK raises its own exception types. If those reached the repair
ladder, the ladder would have to know every SDK's taxonomy, and the retry types
recorded in provenance (plan/03 → *retry ordering*) would drift apart per
provider. Adapters translate into these instead, so "we were rate limited" means
the same thing in a record whoever produced it.

The split mirrors the retry vocabulary the provenance layer already fixes:
transport failures (network, timeout, rate limit, provider error) are retryable
in kind, while content failures are not errors at all — the provider answered,
the answer was wrong, and that is handled by the repair ladder rather than here.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for every failure surfaced by an LLM client."""


class LLMNetworkError(LLMError):
    """The call did not reach the provider (DNS, connection, TLS)."""


class LLMTimeoutError(LLMError):
    """The provider did not answer within the configured timeout.

    Distinct from a network failure: the request may well have been executed and
    billed, which matters when deciding whether retrying is safe.
    """


class LLMRateLimitError(LLMError):
    """The provider refused the call because of a rate or quota limit."""


class LLMProviderError(LLMError):
    """The provider answered with an error (5xx, invalid request, overload)."""
