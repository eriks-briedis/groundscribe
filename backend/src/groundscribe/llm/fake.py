"""Deterministic fake LLM client for tests.

The fake is fully scripted: every result is either a structured response or an
injected failure that the caller queues up front, keyed by ``call_key``. Because
nothing is generated, identical scripting always yields identical outputs — the
determinism the LLM-contract tests rely on. It also records every effective
request it receives so provenance tests (phase 03) can assert on what was sent,
including for attempts that failed.

Phase 04 makes it a real implementation of :class:`~groundscribe.llm.protocol.LLMClient`
rather than a bare stand-in: the contract tests only mean something if the fake
is exercised through the same interface production code uses.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from groundscribe.llm.protocol import (
    LLMRequest,
    LLMResponse,
    ProviderMetadata,
    RetryPolicy,
    StreamChunk,
)


class InjectableFailure(StrEnum):
    """The failure modes the harness can inject on demand.

    Values mirror the phase-01 deliverable list. ``TOOL_CALL`` and
    ``FALLBACK_TRIGGER`` are not errors in production, but at the harness level
    every non-normal outcome is surfaced uniformly as an injected failure; their
    real control-flow semantics are defined in phase 04.
    """

    INVALID_SCHEMA = "invalid_schema"
    INVALID_ENUM = "invalid_enum"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMIT = "rate_limit"
    REFUSAL = "refusal"
    TOOL_CALL = "tool_call"
    FALLBACK_TRIGGER = "fallback_trigger"


class LLMScriptError(Exception):
    """Raised when the fake is called for a key with no scripted result left.

    Deliberately *not* an :class:`~groundscribe.llm.errors.LLMError`: it means the
    test is wrong, not that a provider failed, and the repair ladder must never
    mistake a scripting mistake for a retryable condition.
    """


class InjectedFailureError(Exception):
    """Raised to simulate a scripted failure; carries the injected kind."""

    def __init__(self, failure: InjectableFailure) -> None:
        super().__init__(f"injected LLM failure: {failure.value}")
        self.failure = failure


@dataclass(frozen=True)
class _Step:
    """One scripted outcome: exactly one of ``response`` or ``failure`` is set."""

    response: LLMResponse | None = None
    failure: InjectableFailure | None = None


class FakeLLMClient:
    """A scripted, deterministic, recording stand-in for a real LLM client."""

    def __init__(
        self,
        *,
        provider: str = "fake",
        model: str = "fake-1",
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._scripts: dict[str, deque[_Step]] = {}
        self._received: list[LLMRequest] = []
        self._metadata = ProviderMetadata(
            provider=provider,
            model=model,
            model_revision="fake-rev",
            api_version="fake-v1",
            client_version="fake-client-1",
        )
        self._retry_policy = retry_policy or RetryPolicy()

    @property
    def metadata(self) -> ProviderMetadata:
        """Provider identity, so a record made against the fake reads like a real one."""
        return self._metadata

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy

    def script_response(self, call_key: str, output: dict[str, Any]) -> None:
        """Queue a structured response to return for the next call to ``call_key``."""
        self._scripts.setdefault(call_key, deque()).append(
            _Step(response=LLMResponse(output=output))
        )

    def script_failure(self, call_key: str, failure: InjectableFailure) -> None:
        """Queue an injected failure to raise for the next call to ``call_key``."""
        self._scripts.setdefault(call_key, deque()).append(_Step(failure=failure))

    @property
    def received_requests(self) -> tuple[LLMRequest, ...]:
        """Every request received, in call order (including failed attempts)."""
        return tuple(self._received)

    @property
    def last_request(self) -> LLMRequest | None:
        """The most recently received request, or ``None`` if never called."""
        return self._received[-1] if self._received else None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Record the request, then return/raise its next scripted outcome."""
        self._received.append(request)
        return self._next_step(request.call_key)

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """Stream the scripted outcome as chunks, ending with usage.

        Streaming is part of the protocol, so the fake implements it rather than
        leaving a hole the contract tests cannot reach.
        """
        response = await self.complete(request)
        yield StreamChunk(text=response.raw_text)
        yield StreamChunk(usage=response.usage)

    def _next_step(self, call_key: str) -> LLMResponse:
        steps = self._scripts.get(call_key)
        if not steps:
            raise LLMScriptError(
                f"no scripted result for call_key {call_key!r} (exhausted or never scripted)"
            )
        step = steps.popleft()
        if step.failure is not None:
            raise InjectedFailureError(step.failure)
        assert step.response is not None  # invariant: a step is a response xor a failure
        return step.response
