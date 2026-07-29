"""Contract tests for the narrow LLM interface (phase 04).

Spec (plan/04 → Deliverables):

- an ``LLMClient`` protocol covering structured generation, text generation,
  streaming, tool calling, token/cost reporting, retry policy and provider
  metadata, which the phase-01 fake implements;
- stub adapters for OpenAI, Anthropic and Ollama / OpenAI-compatible local;
- runtime-configuration capture listing every setting that shapes a call.

The point of a *narrow* interface is that the rest of the system depends on this
module and never on a provider SDK, so these tests pin the shape of the contract
rather than any provider's behaviour (plan/04 non-goal: no real network calls).
"""

from __future__ import annotations

import pytest

from groundscribe.llm import (
    FakeLLMClient,
    LLMClient,
    LLMRequest,
    ProviderMetadata,
    RetryPolicy,
    RuntimeConfig,
    StructuredOutputMode,
    TokenUsage,
)
from groundscribe.llm.adapters import AnthropicAdapter, OllamaAdapter, OpenAIClient

#: Still stubs. OpenAI left this list when it was wired; the two that remain are
#: the ones nobody has needed yet, and they stay stubs for the reason phase 04
#: gave — a stub that answered plausibly would let the suite pass on fiction.
STUB_ADAPTERS = [
    AnthropicAdapter(model="claude-x"),
    OllamaAdapter(model="llama"),
]
ADAPTER_IDS = ["anthropic", "ollama"]

#: Every client the protocol has to hold for, stubs and the real one alike.
#: The point of listing them together is that wiring a provider must not need a
#: wider interface — the moment it does is the moment provider concepts start
#: leaking into the callers.
ALL_CLIENTS = [*STUB_ADAPTERS, OpenAIClient(model="gpt-x", api_key="sk-not-used-here")]
ALL_IDS = [*ADAPTER_IDS, "openai"]


def _protocol_typed(client: LLMClient) -> LLMClient:
    """Static conformance: mypy rejects a client that misses the protocol."""
    return client


def test_structured_output_modes_are_provider_neutral() -> None:
    """The mode is how the *schema* was enforced, named without provider jargon.

    Providers spell these differently (``response_format``, tool-forcing, plain
    prompting). Recording the provider's own word for it would make two records
    from two providers incomparable, which is the leak this layer exists to stop.
    """
    assert {mode.value for mode in StructuredOutputMode} == {
        "native_schema",
        "json_mode",
        "prompted",
        "none",
    }


def test_runtime_config_captures_every_setting_the_spec_names() -> None:
    """plan/04 → *Runtime-configuration capture*, pinned field for field.

    Asserted as an exact set: a replay is only trustworthy if the record names
    every knob that could have changed the output, so a silently dropped field
    is a defect even though nothing would fail without this test.
    """
    assert set(RuntimeConfig.model_fields) == {
        "provider",
        "model",
        "model_revision",
        "temperature",
        "top_p",
        "seed",
        "max_output_tokens",
        "reasoning_effort",
        "structured_output_mode",
        "tool_choice",
        "stop_sequences",
        "api_version",
        "client_version",
        "timeout_seconds",
        "retry_policy",
    }


def test_runtime_config_serialises_to_a_json_safe_provider_config() -> None:
    """It is snapshotted as JSON, so enums and nested policies must flatten."""
    config = RuntimeConfig(
        provider="fake",
        model="fake-1",
        temperature=0.0,
        stop_sequences=("<<END>>",),
        retry_policy=RetryPolicy(version="1", max_attempts=2),
    )

    dumped = config.as_provider_config()

    assert dumped["provider"] == "fake"
    assert dumped["structured_output_mode"] == "native_schema"
    assert dumped["stop_sequences"] == ["<<END>>"]
    assert dumped["retry_policy"] == {"version": "1", "max_attempts": 2, "backoff_seconds": 0.0}


def test_runtime_config_is_frozen() -> None:
    """A recorded configuration must not drift after the call it describes."""
    config = RuntimeConfig(provider="fake", model="fake-1")
    with pytest.raises(ValueError):
        config.temperature = 0.7  # type: ignore[misc]


def test_retry_policy_is_versioned() -> None:
    """Retry behaviour is a policy, and an unversioned policy is unreviewable."""
    policy = RetryPolicy()
    assert policy.version
    assert policy.max_attempts >= 1


def test_token_usage_reports_tokens_and_cost() -> None:
    usage = TokenUsage(input_tokens=120, output_tokens=48, cost_usd=0.0012)
    assert (usage.input_tokens, usage.output_tokens) == (120, 48)
    assert usage.total_tokens == 168
    assert usage.cost_usd == pytest.approx(0.0012)


def test_the_fake_client_implements_the_protocol() -> None:
    """plan/04: "The phase-01 fake implements this protocol"."""
    client = FakeLLMClient()
    assert isinstance(client, LLMClient)
    assert _protocol_typed(client) is client
    assert client.metadata.provider == "fake"


@pytest.mark.parametrize("adapter", ALL_CLIENTS, ids=ALL_IDS)
def test_every_adapter_satisfies_the_protocol(adapter: LLMClient) -> None:
    """Two stubs and one real client, held to one interface.

    The shape was fixed before anything was wired in, and OpenAI being here
    unchanged is the evidence it was the right shape: making that adapter real
    needed no addition to the protocol, the generator, or any caller.
    """
    assert isinstance(adapter, LLMClient)
    assert _protocol_typed(adapter) is adapter
    assert isinstance(adapter.metadata, ProviderMetadata)
    assert adapter.metadata.provider in {"openai", "anthropic", "ollama"}
    # The retry policy is part of the contract: the repair ladder reads it off
    # the client, so an adapter that omitted it would be unbounded in practice.
    assert isinstance(adapter.retry_policy, RetryPolicy)


@pytest.mark.parametrize("adapter", STUB_ADAPTERS, ids=ADAPTER_IDS)
async def test_the_remaining_stubs_refuse_to_pretend_they_called_a_provider(
    adapter: LLMClient,
) -> None:
    """A stub that returned a plausible answer would be worse than one that
    raises — the tests would then pass on fiction. Anthropic and Ollama are still
    stubs, and must still say so rather than answering."""
    with pytest.raises(NotImplementedError):
        await adapter.complete(LLMRequest(call_key="anything"))
