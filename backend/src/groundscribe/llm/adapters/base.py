"""Shared skeleton for the provider adapters (phase 04).

Everything an adapter can honestly provide without talking to a provider lives
here: identity, runtime defaults, and the retry policy. What it cannot provide
without the network raises :class:`NotImplementedError` rather than returning
something plausible.

Each subclass also declares its default :class:`StructuredOutputMode`, because
that is the one place the providers genuinely differ in kind — and recording
which mode was used is what makes an invalid-schema failure interpretable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.llm.protocol import (
    LLMRequest,
    LLMResponse,
    ProviderMetadata,
    RetryPolicy,
    StreamChunk,
)


class StubAdapter:
    """A provider adapter with everything but the provider wired up."""

    #: Provider name as it appears in routing config and provenance records.
    provider: ClassVar[str]
    #: How this provider constrains structured output by default.
    structured_output_mode: ClassVar[StructuredOutputMode]

    def __init__(
        self,
        *,
        model: str,
        model_revision: str | None = None,
        api_version: str | None = None,
        client_version: str | None = None,
        timeout_seconds: float = 60.0,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._metadata = ProviderMetadata(
            provider=self.provider,
            model=model,
            model_revision=model_revision,
            api_version=api_version,
            client_version=client_version,
        )
        self._retry_policy = retry_policy or RetryPolicy()
        self.timeout_seconds = timeout_seconds

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError(
            f"the {self.provider} adapter is a stub: wiring the SDK is out of scope for phase 04"
        )

    def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError(
            f"the {self.provider} adapter is a stub: wiring the SDK is out of scope for phase 04"
        )
