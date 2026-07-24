"""Contract tests for the deterministic fake LLM client (phase 01).

Spec (plan/01 Test-first specification → Fake LLM client contract):
- returns scripted structured output for a given call key;
- can be scripted to raise each injectable failure type on demand;
- records the effective request it received (for later provenance assertions);
- is deterministic: identical scripting → identical outputs.

This is the harness later phases test against; it is intentionally minimal
(plan/01 Risk: do not over-build the fake — expand in phase 04).
"""

from __future__ import annotations

import pytest

from groundscribe.llm import (
    FakeLLMClient,
    InjectableFailure,
    InjectedFailureError,
    LLMRequest,
    LLMScriptError,
)


def _request(call_key: str, **kw: object) -> LLMRequest:
    return LLMRequest(call_key=call_key, **kw)  # type: ignore[arg-type]


async def test_returns_scripted_structured_output_for_call_key() -> None:
    client = FakeLLMClient()
    client.script_response("extract_facts", {"facts": ["a", "b"], "count": 2})

    response = await client.complete(_request("extract_facts", prompt="…"))

    assert response.output == {"facts": ["a", "b"], "count": 2}


async def test_scripted_responses_for_a_key_are_returned_in_order() -> None:
    """A sequence per key lets phase 04 exercise retry/repair (invalid → valid)."""
    client = FakeLLMClient()
    client.script_response("route", {"n": 1})
    client.script_response("route", {"n": 2})

    first = await client.complete(_request("route"))
    second = await client.complete(_request("route"))

    assert (first.output, second.output) == ({"n": 1}, {"n": 2})


async def test_records_the_effective_request() -> None:
    client = FakeLLMClient()
    client.script_response("score", {"ok": True})
    request = _request("score", prompt="rate this", params={"temperature": 0.0})

    await client.complete(request)

    assert client.received_requests == (request,)
    assert client.last_request is request
    # Provenance needs the effective prompt/params, not just the key.
    assert client.last_request.prompt == "rate this"
    assert client.last_request.params == {"temperature": 0.0}


def test_there_are_exactly_the_eight_specified_failure_kinds() -> None:
    assert {f.value for f in InjectableFailure} == {
        "invalid_schema",
        "invalid_enum",
        "timeout",
        "provider_error",
        "rate_limit",
        "refusal",
        "tool_call",
        "fallback_trigger",
    }


@pytest.mark.parametrize("failure", list(InjectableFailure))
async def test_each_injectable_failure_can_be_raised(failure: InjectableFailure) -> None:
    client = FakeLLMClient()
    client.script_failure("call", failure)

    with pytest.raises(InjectedFailureError) as excinfo:
        await client.complete(_request("call"))

    assert excinfo.value.failure is failure
    # The failing request is still recorded, so provenance captures failed attempts.
    assert client.last_request == _request("call")


async def test_unscripted_call_raises_script_error() -> None:
    client = FakeLLMClient()
    with pytest.raises(LLMScriptError):
        await client.complete(_request("never_scripted"))


async def test_identical_scripting_yields_identical_outputs() -> None:
    def build() -> FakeLLMClient:
        client = FakeLLMClient()
        client.script_response("a", {"x": 1})
        client.script_failure("b", InjectableFailure.RATE_LIMIT)
        return client

    left, right = build(), build()
    request = _request("a")

    left_out = await left.complete(request)
    right_out = await right.complete(request)

    assert left_out == right_out
    assert left.received_requests == right.received_requests
