"""Deterministic fake LLM client for tests.

The fake is fully scripted: every result is either a structured response or an
injected failure that the caller queues up front, keyed by ``call_key``. Because
nothing is generated, identical scripting always yields identical outputs — the
determinism the LLM-contract tests rely on. It also records every effective
request it receives so provenance tests (phase 03) can assert on what was sent,
including for attempts that failed.

Kept intentionally minimal (plan/01): the full provider/prompt interface, real
adapters, and repair/fallback semantics are defined in phase 04.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class LLMRequest(BaseModel):
    """An effective request as seen by the client (post-render, post-redaction).

    Frozen so a recorded request cannot be mutated after the fact — recorded
    provenance must reflect exactly what was sent.
    """

    model_config = ConfigDict(frozen=True)

    call_key: str
    prompt: str = ""
    schema_name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """A structured model response. Free-form prose lives under a schema field."""

    output: dict[str, Any] = Field(default_factory=dict)


class LLMScriptError(Exception):
    """Raised when the fake is called for a key with no scripted result left."""


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

    def __init__(self) -> None:
        self._scripts: dict[str, deque[_Step]] = {}
        self._received: list[LLMRequest] = []

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
        steps = self._scripts.get(request.call_key)
        if not steps:
            raise LLMScriptError(
                f"no scripted result for call_key {request.call_key!r} "
                f"(exhausted or never scripted)"
            )
        step = steps.popleft()
        if step.failure is not None:
            raise InjectedFailureError(step.failure)
        assert step.response is not None  # invariant: a step is a response xor a failure
        return step.response
