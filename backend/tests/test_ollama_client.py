"""Ollama, over its native ``/api/chat`` wire format.

The second adapter that actually calls a provider, and the one that makes the
local-first promise in plan/00 true rather than aspirational: no key, no account,
no bytes leaving the machine.

Tested against an injected HTTP transport for the same reason the OpenAI client
is — the adapter speaks the wire format directly, so a test can assert **what was
actually sent**, which is the only thing a provenance record can honestly be
checked against.

Three properties are specific to this provider and are why it does not simply
reuse the OpenAI client against Ollama's compatibility endpoint:

**The budget is honoured.** Ollama's OpenAI-compatible endpoint silently ignores
``max_completion_tokens`` — the key the OpenAI adapter sends — and caps nothing.
The native endpoint spells it ``options.num_predict``. A dropped budget is the
worst kind of bug here: the call succeeds, costs more than it should, and nothing
in the record says the limit never applied.

**The context window is set explicitly.** Ollama defaults ``num_ctx`` to a few
thousand tokens and *silently truncates* the prompt above it. A drafting stage
sending a source model plus a review would lose the middle of its own input and
produce a confident answer about material it never saw.

**Reasoning is off unless a stage asked for it.** On a thinking model the
reasoning trace is spent from the same ``num_predict`` budget as the answer, and
is spent first — so a tight budget returns an empty answer and a ``length`` stop.
``reasoning_effort`` is the routing field that opts in.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from groundscribe.llm.adapters.ollama import (
    OLLAMA_BASE_URL_ENV,
    OllamaClient,
)
from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.llm.errors import (
    LLMNetworkError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from groundscribe.llm.pricing import ModelPrice, PricingTable
from groundscribe.llm.protocol import LLMClient, LLMRequest, RuntimeConfig
from groundscribe.provenance.schemas import Message, ToolDefinition

MODEL = "qwen3.6:35b"

#: A minimal successful body in the shape ``/api/chat`` returns.
ANSWER: dict[str, Any] = {
    "model": MODEL,
    "created_at": "2026-08-04T12:00:00Z",
    "message": {"role": "assistant", "content": '{"schema_version": 1, "claims": []}'},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 1200,
    "eval_count": 800,
}


class Recorder:
    """Answers requests from a script, keeping what it was sent."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses) or [httpx.Response(200, json=ANSWER)]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses[min(len(self.requests) - 1, len(self._responses) - 1)]

    @property
    def sent(self) -> dict[str, Any]:
        """The JSON body of the most recent request."""
        body: dict[str, Any] = json.loads(self.requests[-1].content)
        return body

    @property
    def options(self) -> dict[str, Any]:
        """The sampling block of the most recent request, or an empty one."""
        options: dict[str, Any] = self.sent.get("options", {})
        return options


def build_client(
    recorder: Recorder | None = None, *, model: str = MODEL, **kwargs: Any
) -> tuple[OllamaClient, Recorder]:
    """A client whose transport is a recorder rather than the container."""
    scripted = recorder or Recorder()
    client = OllamaClient(model=model, transport=httpx.MockTransport(scripted), **kwargs)
    return client, scripted


def request(
    *,
    mode: StructuredOutputMode = StructuredOutputMode.JSON_MODE,
    schema: dict[str, Any] | None = None,
    **runtime: Any,
) -> LLMRequest:
    return LLMRequest(
        call_key="extract_source_truth",
        prompt="Extract the claims.",
        output_schema=schema,
        runtime=RuntimeConfig(
            provider="ollama", model=MODEL, structured_output_mode=mode, **runtime
        ),
    )


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def test_the_client_is_one_of_these_and_says_which_model_it_is() -> None:
    """plan/04 → the model is the *exact* id, tag and all.

    ``qwen3.6:35b`` and ``qwen3.6:27b`` are different models behind one family
    name, and a record naming only the family cannot tell two runs apart.
    """
    client, _ = build_client(model="qwen3.6:27b")

    assert isinstance(client, LLMClient)
    assert client.metadata.provider == "ollama"
    assert client.metadata.model == "qwen3.6:27b"
    assert client.metadata.client_version, "a record that cannot name the client build is weaker"


def test_a_client_needs_no_credential_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the local provider.

    The OpenAI client refuses to be built without a key. This one must build with
    nothing configured, because "nothing configured" is the state a local-first
    installation is supposed to work in.
    """
    monkeypatch.delenv(OLLAMA_BASE_URL_ENV, raising=False)

    client = OllamaClient(model=MODEL)

    assert client.metadata.provider == "ollama"


async def test_the_endpoint_comes_from_the_environment_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One variable, because Ollama is as often in a container or on another box
    on the LAN as it is on localhost."""
    monkeypatch.setenv(OLLAMA_BASE_URL_ENV, "http://ollama.lan:11434")
    recorder = Recorder()

    client = OllamaClient(model=MODEL, transport=httpx.MockTransport(recorder))
    await client.complete(request())

    assert str(recorder.requests[-1].url) == "http://ollama.lan:11434/api/chat"


# ----------------------------------------------------------------------
# What goes on the wire
# ----------------------------------------------------------------------


async def test_the_prompt_is_sent_as_a_message_to_the_native_chat_endpoint() -> None:
    client, recorder = build_client()

    await client.complete(request())

    assert recorder.requests[-1].url.path == "/api/chat"
    assert recorder.sent["model"] == MODEL
    assert recorder.sent["messages"] == [{"role": "user", "content": "Extract the claims."}]
    # Non-streaming, explicitly: Ollama streams by default, and a caller that
    # asked for one answer must not have to reassemble it from frames.
    assert recorder.sent["stream"] is False


async def test_prior_messages_are_sent_in_order_ahead_of_the_prompt() -> None:
    """A stage that built a conversation must have it sent as one."""
    client, recorder = build_client()

    await client.complete(
        LLMRequest(
            call_key="review_substantively",
            prompt="Now review it.",
            messages=(
                Message(role="system", content="You are an exacting editor."),
                Message(role="user", content="Here is the draft."),
            ),
            runtime=RuntimeConfig(provider="ollama", model=MODEL),
        )
    )

    assert [message["role"] for message in recorder.sent["messages"]] == ["system", "user", "user"]
    assert recorder.sent["messages"][-1]["content"] == "Now review it."


async def test_every_runtime_setting_the_routing_policy_names_is_sent() -> None:
    """plan/04 → *runtime-configuration capture*, from the other end.

    ``max_output_tokens`` becoming ``num_predict`` is the one that matters most:
    the OpenAI-compatible endpoint ignores the key the other adapter sends, so a
    budget set in routing would simply never apply.
    """
    client, recorder = build_client()

    await client.complete(
        request(
            temperature=0.7,
            top_p=0.9,
            seed=20260725,
            max_output_tokens=16384,
            context_window=32768,
            stop_sequences=("<<END>>",),
        )
    )

    assert recorder.options["temperature"] == 0.7
    assert recorder.options["top_p"] == 0.9
    assert recorder.options["seed"] == 20260725
    assert recorder.options["num_predict"] == 16384
    assert recorder.options["num_ctx"] == 32768
    assert recorder.options["stop"] == ["<<END>>"]


async def test_a_setting_nobody_asked_for_is_absent_rather_than_defaulted() -> None:
    """The same rule the OpenAI adapter holds to: an adapter that supplied its own
    temperature would be forming an editorial opinion the routing config owns, and
    the recorded runtime config would no longer describe the call."""
    client, recorder = build_client()

    await client.complete(request())

    assert "temperature" not in recorder.options
    assert "top_p" not in recorder.options
    assert "seed" not in recorder.options
    assert "num_predict" not in recorder.options
    assert "num_ctx" not in recorder.options
    assert "stop" not in recorder.options


async def test_the_context_window_is_sent_whenever_a_stage_sets_one() -> None:
    """Ollama truncates the prompt at ``num_ctx`` silently.

    Above the window the *middle* of the input is dropped and the model answers
    confidently about material it never saw — no error, no warning, and a
    provenance record that names a prompt the model was never shown in full.
    """
    client, recorder = build_client()

    await client.complete(request(context_window=65536))

    assert recorder.options["num_ctx"] == 65536


async def test_the_model_is_kept_resident_between_calls() -> None:
    """A 24 GB model reloaded between stages costs more wall-clock than the run.

    ``keep_alive`` is a property of how this installation is driven — a pipeline
    makes a dozen calls to one model in a row — rather than an editorial choice,
    which is why it belongs to the client and not to the routing policy.
    """
    client, recorder = build_client()

    await client.complete(request())

    assert recorder.sent["keep_alive"]


# ----------------------------------------------------------------------
# Structured output
# ----------------------------------------------------------------------


async def test_native_schema_sends_the_schema_and_json_mode_asks_for_json() -> None:
    """Ollama constrains decoding against a real JSON Schema, which is stronger
    than anything the compatibility endpoint offers — it is the reason a local
    model can be held to a stage's contract at all."""
    schema = {
        "type": "object",
        "properties": {"schema_version": {"type": "integer"}},
        "required": ["schema_version"],
    }

    client, recorder = build_client()
    await client.complete(request(mode=StructuredOutputMode.JSON_MODE))
    assert recorder.sent["format"] == "json"

    await client.complete(request(mode=StructuredOutputMode.NATIVE_SCHEMA, schema=schema))
    assert recorder.sent["format"] == schema


async def test_native_schema_without_a_schema_falls_back_to_asking_for_json() -> None:
    """The honest weaker constraint, rather than a malformed request that would
    read as a provider fault. The recorded mode still says what was asked for."""
    client, recorder = build_client()

    await client.complete(request(mode=StructuredOutputMode.NATIVE_SCHEMA))

    assert recorder.sent["format"] == "json"


async def test_prompted_mode_constrains_nothing() -> None:
    client, recorder = build_client()

    await client.complete(request(mode=StructuredOutputMode.PROMPTED))

    assert "format" not in recorder.sent


async def test_tools_are_offered_in_the_providers_shape() -> None:
    client, recorder = build_client()

    await client.complete(
        LLMRequest(
            call_key="extract_source_truth",
            prompt="Look it up.",
            tools=(
                ToolDefinition(
                    name="fetch_url",
                    version="1",
                    parameters={"type": "object", "properties": {"url": {"type": "string"}}},
                ),
            ),
            runtime=RuntimeConfig(provider="ollama", model=MODEL),
        )
    )

    (tool,) = recorder.sent["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "fetch_url"
    assert tool["function"]["parameters"]["properties"]["url"]["type"] == "string"
    assert "description" not in tool["function"]


# ----------------------------------------------------------------------
# Reasoning
# ----------------------------------------------------------------------


async def test_reasoning_is_off_unless_a_stage_asked_for_it() -> None:
    """Measured, not assumed: with thinking on and a 256-token budget, a thinking
    model spends the whole budget on its reasoning trace and returns an *empty*
    answer with a ``length`` stop.

    Every stage in the routing policy sets ``max_output_tokens``, so inheriting
    the model's own default here would make the tightest stages return nothing —
    and the repair ladder would read that as a malformed answer rather than an
    exhausted budget, which sends the next person looking at the prompt.

    ``reasoning_effort`` unset means no reasoning was requested. That is the
    field's plain meaning, not an opinion this adapter invented.
    """
    client, recorder = build_client()

    await client.complete(request())

    assert recorder.sent["think"] is False


async def test_a_stage_that_wants_reasoning_gets_it_at_the_effort_it_named() -> None:
    """The judging stages are the ones where the trade is worth making, and it is
    a per-stage decision the routing policy records."""
    client, recorder = build_client()

    await client.complete(request(reasoning_effort="high"))

    assert recorder.sent["think"] == "high"


# ----------------------------------------------------------------------
# What comes back
# ----------------------------------------------------------------------


async def test_the_body_is_preserved_verbatim_with_its_usage() -> None:
    """Ollama reports tokens under its own names; the record uses one vocabulary
    whoever answered."""
    client, _ = build_client()

    answer = await client.complete(request())

    assert answer.text == '{"schema_version": 1, "claims": []}'
    assert answer.usage.input_tokens == 1200
    assert answer.usage.output_tokens == 800


async def test_an_unparseable_body_is_returned_rather_than_raised() -> None:
    """A successful call that produced an unusable result — what the repair ladder
    exists for, and it needs the body to work with."""
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": MODEL,
                "message": {"role": "assistant", "content": "{not json"},
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 2,
            },
        )
    )
    client, _ = build_client(recorder)

    answer = await client.complete(request())

    assert answer.text == "{not json"
    assert answer.refusal is None


async def test_an_answer_truncated_by_the_budget_still_comes_back() -> None:
    """Raising here would destroy the evidence. A ``length`` stop with empty
    content is exactly what an over-tight budget on a thinking model looks like,
    and the trace has to be able to show it."""
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": MODEL,
                "message": {"role": "assistant", "content": "", "thinking": "Let me consider"},
                "done": True,
                "done_reason": "length",
                "prompt_eval_count": 40,
                "eval_count": 256,
            },
        )
    )
    client, _ = build_client(recorder)

    answer = await client.complete(request())

    assert answer.text == ""
    assert answer.usage.output_tokens == 256


async def test_a_tool_call_keeps_the_arguments_the_model_actually_sent() -> None:
    """Ollama returns arguments already decoded and carries no call id, so one is
    synthesised from position — un-normalised otherwise, because what the model
    *asked* for is the evidence."""
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": MODEL,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "fetch_url", "arguments": {"url": "/x"}}}],
                },
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )
    )
    client, _ = build_client(recorder)

    answer = await client.complete(request())

    (call,) = answer.tool_calls
    assert call.name == "fetch_url"
    assert call.arguments == {"url": "/x"}
    assert call.call_id, "a call with no id cannot be matched to its result"


async def test_unparseable_tool_arguments_survive_instead_of_vanishing() -> None:
    """A model that emitted broken JSON for its arguments has made a mistake worth
    seeing. Dropping the call would leave a trace that cannot explain what happened
    next.

    Reachable here even though Ollama normally decodes arguments itself: a model
    behind a compatible server may still emit them as a string, and the adapter
    that assumed otherwise would raise inside the translation rather than record
    the evidence.
    """
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": MODEL,
                "message": {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "fetch_url", "arguments": "{broken"}}],
                },
                "done": True,
            },
        )
    )
    client, _ = build_client(recorder)

    (call,) = (await client.complete(request())).tool_calls

    assert call.arguments == {"__unparsed__": "{broken"}


# ----------------------------------------------------------------------
# When it goes wrong
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, LLMProviderError),
        (400, LLMProviderError),
        (500, LLMProviderError),
        (429, LLMRateLimitError),
    ],
)
async def test_provider_failures_map_to_the_typed_errors_the_ladder_routes_on(
    status: int, expected: type[Exception]
) -> None:
    """plan/03 → retries are *typed*. 404 is the common one here and it means
    something specific: the model is not pulled on this machine."""
    recorder = Recorder(httpx.Response(status, json={"error": f"model {MODEL!r} not found"}))
    client, _ = build_client(recorder)

    with pytest.raises(expected):
        await client.complete(request())


async def test_a_missing_model_says_so_in_words_that_name_the_fix() -> None:
    """The failure an operator actually hits, on the first run after editing
    routing. "not found" plus the model id is the difference between a two-minute
    fix and a bug report."""
    recorder = Recorder(httpx.Response(404, json={"error": "model not found"}))
    client, _ = build_client(recorder)

    with pytest.raises(LLMProviderError) as missing:
        await client.complete(request())

    assert MODEL in str(missing.value)


async def test_a_failure_body_that_is_not_json_still_reports_something_useful() -> None:
    """A proxy or a crashed server answers with HTML or nothing at all. Losing the
    status to a decode error would turn a diagnosable failure into a stack trace
    about parsing."""
    recorder = Recorder(httpx.Response(502, text="<html>bad gateway</html>"))
    client, _ = build_client(recorder)

    with pytest.raises(LLMProviderError) as refused:
        await client.complete(request())

    assert "502" in str(refused.value)


async def test_a_timeout_is_a_timeout_and_a_dead_socket_is_a_network_error() -> None:
    """Two different problems with two different fixes — and on a local provider
    the second one usually means the container is not running."""

    def timing_out(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing there")

    timed_out = OllamaClient(model=MODEL, transport=httpx.MockTransport(timing_out))
    unreachable = OllamaClient(model=MODEL, transport=httpx.MockTransport(refused))

    with pytest.raises(LLMTimeoutError):
        await timed_out.complete(request())
    with pytest.raises(LLMNetworkError) as dead:
        await unreachable.complete(request())

    assert OLLAMA_BASE_URL_ENV in str(dead.value) or "11434" in str(dead.value)


async def test_the_route_decides_how_long_the_call_may_take() -> None:
    """The routing config's ``timeout_seconds`` reaches the socket.

    It did not, for the whole of this project's life. Every client was built
    without a timeout (``app.bootstrap``), so all three sat on the 600s
    constructor default while this profile declared 3600s for the three stages
    that write thousands of words in one call — and a 36B model that does not fit
    in VRAM takes longer than ten minutes to do that. Those stages were killed
    mid-generation, having produced (and paid for) most of an answer, and the
    failure arrived as ``LLMTimeoutError`` rather than as "your timeout is too
    short".

    Asserted on the client the adapter builds, because that is where the number
    either arrives or does not; a test on ``RuntimeConfig`` would have passed
    throughout, which is exactly why nothing caught this.
    """
    client, _ = build_client(timeout_seconds=600.0)

    assert client._client(None).timeout.read == 600.0
    assert client._client(3600.0).timeout.read == 3600.0

    await client.complete(request(timeout_seconds=3600.0))


# ----------------------------------------------------------------------
# Cost
# ----------------------------------------------------------------------


async def test_an_unpriced_local_call_reports_no_cost_rather_than_none_spent() -> None:
    """Local inference is not free — it is *unpriced*, which is a different thing.

    plan/12's rule is that unknown is not zero, and it does not bend because the
    hardware is in the room. An operator who wants a number puts one in
    ``model-pricing.yaml``; until then the metric reads ``n/a`` honestly.
    """
    client, _ = build_client()

    answer = await client.complete(request())

    assert answer.usage.input_tokens == 1200
    assert answer.usage.cost_usd is None


async def test_a_priced_model_reports_what_the_call_cost() -> None:
    """An operator who has worked out their electricity per million tokens is
    entitled to see it, by the same table every other provider uses."""
    client, _ = build_client(
        pricing=PricingTable(
            version="test",
            models={MODEL: ModelPrice(input_per_million=1.0, output_per_million=10.0)},
        )
    )

    answer = await client.complete(request())

    assert answer.usage.cost_usd == pytest.approx((1200 * 1.0 + 800 * 10.0) / 1_000_000)


# ----------------------------------------------------------------------
# Streaming
# ----------------------------------------------------------------------


async def test_streaming_yields_the_text_and_then_the_usage() -> None:
    """Ollama streams newline-delimited JSON objects rather than SSE frames, so
    this is a genuinely different decoder from the OpenAI one."""
    frames = (
        json.dumps({"message": {"content": "p99 "}, "done": False})
        + "\n"
        + json.dumps({"message": {"content": "fell."}, "done": False})
        + "\n"
        + json.dumps(
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 10, "eval_count": 4}
        )
        + "\n"
    )
    recorder = Recorder(httpx.Response(200, text=frames))
    client, _ = build_client(recorder)

    chunks = [chunk async for chunk in client.stream(request())]

    assert "".join(chunk.text for chunk in chunks) == "p99 fell."
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.output_tokens == 4


async def test_a_malformed_stream_frame_is_skipped_rather_than_fatal() -> None:
    """One unreadable frame should cost one frame. Raising mid-stream would throw
    away the tokens already received, which is the opposite of what the rest of
    this adapter does with partial evidence."""
    frames = (
        json.dumps({"message": {"content": "kept"}, "done": False})
        + "\n"
        + "{ not json at all\n"
        + "\n"
        + json.dumps({"message": {"content": ""}, "done": True, "eval_count": 1})
        + "\n"
    )
    recorder = Recorder(httpx.Response(200, text=frames))
    client, _ = build_client(recorder)

    chunks = [chunk async for chunk in client.stream(request())]

    assert "".join(chunk.text for chunk in chunks) == "kept"
