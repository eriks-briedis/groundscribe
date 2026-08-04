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

from groundscribe.llm.errors import LLMProviderError, LLMRateLimitError, LLMTimeoutError
from groundscribe.llm.protocol import (
    LENGTH_STOP,
    LLMRequest,
    LLMResponse,
    ProviderMetadata,
    RetryPolicy,
    StreamChunk,
    TokenUsage,
    ToolCall,
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
    """Raised to simulate a scripted failure; carries the injected kind.

    The transport kinds get subclasses that *also* inherit the provider-neutral
    error the real adapters raise, so a test can assert on the injected kind
    while the code under test only ever sees the production taxonomy.
    """

    def __init__(self, failure: InjectableFailure) -> None:
        super().__init__(f"injected LLM failure: {failure.value}")
        self.failure = failure


class InjectedTimeoutError(InjectedFailureError, LLMTimeoutError):
    """An injected timeout, indistinguishable from a real one to the ladder."""


class InjectedRateLimitError(InjectedFailureError, LLMRateLimitError):
    """An injected rate limit, indistinguishable from a real one to the ladder."""


class InjectedProviderError(InjectedFailureError, LLMProviderError):
    """An injected provider error, indistinguishable from a real one to the ladder."""


#: Injected kinds that map onto a provider-neutral transport failure. The
#: content kinds (invalid schema/enum, refusal, tool call) are absent on
#: purpose: they are things a provider *returns*, and scripting them as
#: exceptions would let the ladder retry conditions no retry can fix.
_TRANSPORT_ERRORS: dict[InjectableFailure, type[InjectedFailureError]] = {
    InjectableFailure.TIMEOUT: InjectedTimeoutError,
    InjectableFailure.RATE_LIMIT: InjectedRateLimitError,
    InjectableFailure.PROVIDER_ERROR: InjectedProviderError,
}


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

    def script_response(
        self, call_key: str, output: dict[str, Any], *, usage: TokenUsage | None = None
    ) -> None:
        """Queue a structured response to return for the next call to ``call_key``."""
        self._queue(call_key, LLMResponse(output=output, usage=usage or TokenUsage()))

    def script_text(self, call_key: str, text: str, *, usage: TokenUsage | None = None) -> None:
        """Queue a raw body, valid JSON or not.

        This is how the ladder's content failures are scripted: an unparseable
        body is a *successful* call whose result is unusable, and the raw text
        has to survive verbatim into the record.
        """
        self._queue(call_key, LLMResponse(text=text, usage=usage or TokenUsage()))

    def script_truncated(self, call_key: str, text: str) -> None:
        """Queue a body the provider stopped emitting when the budget ran out.

        Its own scripting method because it is its own outcome: the text alone
        cannot express it — a half-written object and a badly-written one are the
        same string to a parser — and the difference is exactly what the ladder
        has to act on.
        """
        self._queue(call_key, LLMResponse(text=text, stop_reason=LENGTH_STOP))

    def script_refusal(self, call_key: str, reason: str) -> None:
        """Queue a provider refusal — a response, never an exception."""
        self._queue(call_key, LLMResponse(refusal=reason))

    def script_tool_call(
        self,
        call_key: str,
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> None:
        """Queue a model-requested tool call.

        The arguments are stored exactly as given: what the model *asked* for is
        the evidence, so the fake never tidies them on the way through.
        """
        call = ToolCall(
            call_id=call_id or f"{call_key}-tool-{len(self._scripts.get(call_key, ()))}",
            name=name,
            arguments=arguments or {},
        )
        self._queue(call_key, LLMResponse(tool_calls=(call,)))

    def script_failure(self, call_key: str, failure: InjectableFailure) -> None:
        """Queue an injected failure to raise for the next call to ``call_key``."""
        self._scripts.setdefault(call_key, deque()).append(_Step(failure=failure))

    def _queue(self, call_key: str, response: LLMResponse) -> None:
        self._scripts.setdefault(call_key, deque()).append(_Step(response=response))

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
            raise _TRANSPORT_ERRORS.get(step.failure, InjectedFailureError)(step.failure)
        assert step.response is not None  # invariant: a step is a response xor a failure
        return step.response
