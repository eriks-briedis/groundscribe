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

from typing import Any

import pytest

from groundscribe.llm import (
    FakeLLMClient,
    InjectableFailure,
    InjectedFailureError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequest,
    LLMScriptError,
    LLMTimeoutError,
    TokenUsage,
)


def _request(call_key: str, **kw: Any) -> LLMRequest:
    return LLMRequest(call_key=call_key, **kw)


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


# ---------------------------------------------------------------------------
# Phase 04: the fake as a full LLMClient (plan/01 said "expand in phase 04").
#
# The repair ladder is driven entirely by what a client can return, so the fake
# has to be able to produce every outcome the ladder distinguishes: a body that
# does not parse, a refusal, a tool call, and each transport failure as its own
# provider-neutral error type.
# ---------------------------------------------------------------------------


async def test_scripted_raw_text_is_preserved_verbatim() -> None:
    """An unparseable body must survive exactly as emitted.

    The ladder classifies on the raw text, and provenance stores it: normalising
    it here would hide the very defect the record exists to explain.
    """
    client = FakeLLMClient()
    client.script_text("extract", '{"claims": [')

    response = await client.complete(_request("extract"))

    assert response.text == '{"claims": ['
    assert response.raw_text == '{"claims": ['
    assert response.output == {}


async def test_raw_text_falls_back_to_the_scripted_structured_form() -> None:
    """Scripting a dict is the common case; it still has a canonical raw form."""
    client = FakeLLMClient()
    client.script_response("extract", {"b": 2, "a": 1})

    response = await client.complete(_request("extract"))

    # Canonical (sorted, compact) so the same logical body hashes identically.
    assert response.raw_text == '{"a":1,"b":2}'


async def test_a_refusal_is_a_response_not_an_exception() -> None:
    """plan/04: a refusal is captured as a refusal state, not a valid result.

    Modelling it as an exception would collapse it into transport failure, and a
    provider declining to answer is a different problem with a different fix — a
    human, not a retry.
    """
    client = FakeLLMClient()
    client.script_refusal("draft", "I can't help with that.")

    response = await client.complete(_request("draft"))

    assert response.refusal == "I can't help with that."
    assert response.output == {}


async def test_a_model_requested_tool_call_is_returned_with_its_arguments() -> None:
    client = FakeLLMClient()
    client.script_tool_call("draft", name="lookup_metric", arguments={"metric": "p99"})

    response = await client.complete(_request("draft"))

    assert [(call.name, call.arguments) for call in response.tool_calls] == [
        ("lookup_metric", {"metric": "p99"})
    ]
    assert response.tool_calls[0].call_id


async def test_token_and_cost_usage_is_reported() -> None:
    """Token/cost reporting is part of the protocol, so the fake must carry it."""
    client = FakeLLMClient()
    usage = TokenUsage(input_tokens=10, output_tokens=4)
    client.script_response("score", {"ok": True}, usage=usage)

    response = await client.complete(_request("score"))

    assert response.usage.total_tokens == 14


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (InjectableFailure.TIMEOUT, LLMTimeoutError),
        (InjectableFailure.RATE_LIMIT, LLMRateLimitError),
        (InjectableFailure.PROVIDER_ERROR, LLMProviderError),
    ],
)
async def test_transport_failures_raise_the_provider_neutral_error_type(
    failure: InjectableFailure, error_type: type[Exception]
) -> None:
    """Injected transport failures are the *same* types a real adapter raises.

    Otherwise the ladder would be tested against a taxonomy nothing in
    production uses, and the retry types recorded in provenance would only be
    correct in tests.
    """
    client = FakeLLMClient()
    client.script_failure("call", failure)

    with pytest.raises(error_type) as excinfo:
        await client.complete(_request("call"))

    # Still an injected failure, so the phase-01 harness contract holds.
    assert isinstance(excinfo.value, InjectedFailureError)


async def test_a_scripting_mistake_is_not_a_provider_failure() -> None:
    """LLMScriptError must not be retryable: the test is wrong, not the provider."""
    client = FakeLLMClient()
    with pytest.raises(LLMScriptError) as excinfo:
        await client.complete(_request("never_scripted"))
    assert not isinstance(excinfo.value, LLMError)


async def test_streaming_yields_the_body_then_the_usage() -> None:
    client = FakeLLMClient()
    client.script_response("draft", {"x": 1}, usage=TokenUsage(input_tokens=3, output_tokens=1))

    chunks = [chunk async for chunk in client.stream(_request("draft"))]

    assert "".join(chunk.text for chunk in chunks) == '{"x":1}'
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 4


def test_provider_metadata_identifies_the_exact_model() -> None:
    client = FakeLLMClient(model="fake-mini")
    assert (client.metadata.provider, client.metadata.model) == ("fake", "fake-mini")
    assert client.metadata.client_version
