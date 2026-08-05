"""The first provider this system actually calls (OpenAI).

Phase 04 shipped three adapters as stubs on purpose — a stub that returned a
plausible answer would let the whole suite pass on fiction — and phase 14 shipped
a stack with no provider registered at all. This is the one that reaches the
network, so it is the one that has to be pinned hardest.

Tested against an injected HTTP transport rather than a mocked SDK object graph.
That is the whole design of the adapter: it speaks the Chat Completions wire
format directly, so a test can assert **what was actually sent** — which is the
only thing provenance can be checked against. A mocked client object would let
the adapter and the record agree with each other and both be wrong.

Three properties hold across the file:

**What is sent is what the record says was sent.** Every `RuntimeConfig` field the
routing policy sets has to appear on the wire, or a replay reproduces a call that
never happened.

**A bad answer is a response, not an exception.** A refusal, an unparseable body
and a tool call are all *successful* calls that produced no usable structured
result. Raising on them would destroy the evidence the repair ladder and the
trace are built on — the same position `LLMResponse` takes in phase 04.

**The key is not part of anything.** It goes in a header and appears in no
response, no metadata and no error message.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from groundscribe.llm.adapters.openai import (
    OPENAI_API_KEY_ENV,
    MissingAPIKey,
    OpenAIClient,
    strict_schema,
)
from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.llm.errors import (
    LLMNetworkError,
    LLMProviderError,
    LLMRateLimitError,
    LLMSchemaRejected,
    LLMTimeoutError,
)
from groundscribe.llm.pricing import ModelPrice, PricingTable
from groundscribe.llm.protocol import LLMClient, LLMRequest, RuntimeConfig
from groundscribe.provenance.schemas import Message, ToolDefinition
from groundscribe.scoring.rubric import ScoreDimension
from groundscribe.stages.schemas import (
    ArchitectureProposal,
    ArticleBriefDocument,
    ArticleDraft,
    ArticleScore,
    GapReport,
    RevisionPlanDocument,
    RewrittenArticle,
    SourceModel,
    SubstantiveReview,
    VoicePass,
)

#: Every schema a stage asks a model for. Listed rather than discovered, so a new
#: stage output is added here by the person who wrote it — a set built by walking
#: the package would grow silently and prove nothing about the one just added.
STAGE_SCHEMAS = (
    SourceModel,
    GapReport,
    ArchitectureProposal,
    ArticleBriefDocument,
    ArticleDraft,
    SubstantiveReview,
    RevisionPlanDocument,
    RewrittenArticle,
    VoicePass,
    ArticleScore,
)

KEY = "sk-test-not-a-real-key"

#: A minimal successful body in the shape the Chat Completions API returns.
ANSWER: dict[str, Any] = {
    "id": "chatcmpl-1",
    "model": "gpt-5-2026-01-01",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": '{"schema_version": 1, "claims": []}'},
        }
    ],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 800, "total_tokens": 2000},
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


def build_client(
    recorder: Recorder | None = None, *, model: str = "gpt-5", **kwargs: Any
) -> tuple[OpenAIClient, Recorder]:
    """A client whose transport is a recorder rather than the internet."""
    scripted = recorder or Recorder()
    client = OpenAIClient(
        model=model,
        api_key=KEY,
        transport=httpx.MockTransport(scripted),
        **kwargs,
    )
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
            provider="openai", model="gpt-5", structured_output_mode=mode, **runtime
        ),
    )


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def test_the_client_is_one_of_these_and_says_which_model_it_is() -> None:
    """plan/04 → the model is the *exact* id, never an alias.

    Aliases move. A record saying "the latest model" explains nothing six months
    later, which is the whole reason `ProviderMetadata.model` exists.
    """
    client, _ = build_client(model="gpt-5-mini")

    assert isinstance(client, LLMClient)
    assert client.metadata.provider == "openai"
    assert client.metadata.model == "gpt-5-mini"
    assert client.metadata.client_version, "a record that cannot name the client build is weaker"


def test_a_client_with_no_key_refuses_to_be_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loudly, at construction, rather than as a 401 on the first stage.

    A misconfigured deployment should fail where the mistake was made. Failing at
    the first model call means the failure arrives attached to an editorial stage
    and reads as a pipeline problem.
    """
    monkeypatch.delenv(OPENAI_API_KEY_ENV, raising=False)

    with pytest.raises(MissingAPIKey, match=OPENAI_API_KEY_ENV):
        OpenAIClient(model="gpt-5")


def test_the_key_is_taken_from_the_environment_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one handover every launcher can perform — the same reasoning phase 13
    gave the trace key store."""
    monkeypatch.setenv(OPENAI_API_KEY_ENV, KEY)
    recorder = Recorder()

    client = OpenAIClient(model="gpt-5", transport=httpx.MockTransport(recorder))

    assert client.metadata.provider == "openai"


# ----------------------------------------------------------------------
# What goes on the wire
# ----------------------------------------------------------------------


async def test_the_prompt_is_sent_as_a_message_with_the_key_in_a_header() -> None:
    client, recorder = build_client()

    await client.complete(request())

    assert recorder.sent["model"] == "gpt-5"
    assert recorder.sent["messages"] == [{"role": "user", "content": "Extract the claims."}]
    assert recorder.requests[-1].headers["authorization"] == f"Bearer {KEY}"
    # The key belongs in exactly one place. Anywhere in the body would put it a
    # single logging change away from the record.
    assert KEY not in recorder.requests[-1].content.decode()


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
            runtime=RuntimeConfig(provider="openai", model="gpt-5"),
        )
    )

    assert [message["role"] for message in recorder.sent["messages"]] == [
        "system",
        "user",
        "user",
    ]
    assert recorder.sent["messages"][-1]["content"] == "Now review it."


async def test_every_runtime_setting_the_routing_policy_names_is_sent() -> None:
    """plan/04 → *runtime-configuration capture*, from the other end.

    The record lists what shaped the call; this asserts those same settings
    actually left the process. A setting that is recorded and not sent makes
    every replay of that call a different call.
    """
    client, recorder = build_client()

    await client.complete(
        request(
            temperature=0.7,
            top_p=0.9,
            seed=20260725,
            max_output_tokens=16384,
            stop_sequences=("<<END>>",),
        )
    )

    sent = recorder.sent
    assert sent["temperature"] == 0.7
    assert sent["top_p"] == 0.9
    assert sent["seed"] == 20260725
    assert sent["max_completion_tokens"] == 16384
    assert sent["stop"] == ["<<END>>"]


async def test_a_setting_nobody_asked_for_is_absent_rather_than_defaulted() -> None:
    """Sending a default the routing policy did not choose is the adapter having
    an opinion about editorial behaviour, which is not its job — and it would
    make the recorded runtime config a lie by omission.

    It also matters for reasoning models, which reject some sampling parameters
    outright: a stage that set none must not be given any.
    """
    client, recorder = build_client()

    await client.complete(request())

    assert "temperature" not in recorder.sent
    assert "top_p" not in recorder.sent
    assert "seed" not in recorder.sent
    assert "stop" not in recorder.sent


async def test_json_mode_asks_for_json_and_native_schema_asks_for_the_schema() -> None:
    """plan/04 → the mode changes what a failure *means*, so it must change the
    request as well as the record."""
    schema = {
        "type": "object",
        "properties": {"schema_version": {"type": "integer"}},
        "required": ["schema_version"],
        "additionalProperties": False,
    }

    client, recorder = build_client()
    await client.complete(request(mode=StructuredOutputMode.JSON_MODE))
    assert recorder.sent["response_format"] == {"type": "json_object"}

    await client.complete(request(mode=StructuredOutputMode.NATIVE_SCHEMA, schema=schema))
    native = recorder.sent["response_format"]
    assert native["type"] == "json_schema"
    assert native["json_schema"]["strict"] is True
    assert native["json_schema"]["schema"] == schema


async def test_a_pydantic_schema_is_rewritten_into_the_subset_strict_mode_accepts() -> None:
    """Strict mode refuses an optional property and refuses a content constraint,
    and Pydantic emits both for any schema with a default or a `min_length`.

    Sent unrewritten, `native_schema` is a 400 on every call — which is why this
    profile ran on `json_mode`, and why a claim came back as `statement` where the
    schema says `text`, twice, at full price.

    What is dropped is not lost. `minItems` still holds, enforced by the schema
    that parses the response rather than by the provider; only the shape moves
    from repairable to unrepresentable.
    """
    client, recorder = build_client()
    schema = SourceModel.model_json_schema()

    await client.complete(request(mode=StructuredOutputMode.NATIVE_SCHEMA, schema=schema))

    sent = recorder.sent["response_format"]["json_schema"]["schema"]
    claim = sent["$defs"]["ExtractedClaim"]
    assert claim["required"] == sorted(claim["properties"]), "strict mode has no optional property"
    assert "evidence" in claim["required"], "a defaulted field is promoted, not dropped"
    assert "default" not in claim["properties"]["evidence"]
    assert "minItems" not in sent["$defs"]["Evidence"]["properties"]["segment_ids"]
    assert sent["additionalProperties"] is False
    # The pipeline's own schema is untouched: it is what still parses the answer.
    assert "minItems" in SourceModel.model_json_schema()["$defs"]["Evidence"]["properties"][
        "segment_ids"
    ]


async def test_a_mapping_keyed_by_an_enum_is_written_out_as_its_own_keys() -> None:
    """`Mapping[ScoreDimension, DimensionScore]` is an open object to Pydantic:
    the value schema under `additionalProperties`, the permitted keys under
    `propertyNames`. Strict mode accepts neither, and said so — a 400 on every
    `score_article` call, three times in 1.3 seconds, on a real run.

    Expanded rather than stripped, because the keys *are* a closed set and that is
    what strict mode is good at. This is the one rewrite here that keeps a
    constraint instead of handing it back to Pydantic.
    """
    client, recorder = build_client()

    await client.complete(
        request(
            mode=StructuredOutputMode.NATIVE_SCHEMA,
            schema=ArticleScore.model_json_schema(),
        )
    )

    sent = recorder.sent["response_format"]["json_schema"]["schema"]
    dimensions = sent["properties"]["dimensions"]
    assert "propertyNames" not in json.dumps(sent), "strict mode refuses it anywhere"
    assert dimensions["additionalProperties"] is False
    assert dimensions["required"] == sorted(dimension.value for dimension in ScoreDimension)
    assert dimensions["properties"]["factual_fidelity"] == {"$ref": "#/$defs/DimensionScore"}


async def test_every_stage_schema_survives_the_rewrite_into_strict_mode() -> None:
    """The audit `score_article` was found by, kept as a test.

    A construct strict mode refuses is a 400 on *every* call of the stage that
    uses it — never intermittent, never partial, and discovered halfway through a
    run where it reads as a pipeline fault. One schema had one, and nothing said
    so until the provider did.

    Asserted over the rewritten schema, not the Pydantic one: the pipeline's
    schemas are allowed their constraints, and this is about what leaves.
    """
    for model in STAGE_SCHEMAS:
        sent = strict_schema(model.model_json_schema())
        assert "propertyNames" not in json.dumps(sent), model.__name__
        for node in _objects(sent):
            assert node["required"] == sorted(node["properties"]), model.__name__
            assert node.get("additionalProperties") is False, model.__name__


def _objects(node: Any) -> list[dict[str, Any]]:
    """Every object in a schema that declares properties, at any depth."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            found.append(node)
        for value in node.values():
            found += _objects(value)
    elif isinstance(node, list):
        for value in node:
            found += _objects(value)
    return found


async def test_native_schema_without_a_schema_falls_back_to_asking_for_json() -> None:
    """A stage that declared native enforcement and supplied nothing to enforce
    would otherwise be rejected by the provider for a malformed request — a 400
    that reads as a provider fault rather than a config mistake. Asking for JSON
    is the honest weaker constraint, and the recorded mode still says what was
    asked for."""
    client, recorder = build_client()

    await client.complete(request(mode=StructuredOutputMode.NATIVE_SCHEMA))

    assert recorder.sent["response_format"] == {"type": "json_object"}


async def test_prompted_mode_constrains_nothing() -> None:
    client, recorder = build_client()

    await client.complete(request(mode=StructuredOutputMode.PROMPTED))

    assert "response_format" not in recorder.sent


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
            runtime=RuntimeConfig(provider="openai", model="gpt-5"),
        )
    )

    (tool,) = recorder.sent["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "fetch_url"
    assert tool["function"]["parameters"]["properties"]["url"]["type"] == "string"
    # No description is sent, because `ToolDefinition` does not carry one.
    # Synthesising a sentence would put words in the record that no offer made.
    assert "description" not in tool["function"]


# ----------------------------------------------------------------------
# What comes back
# ----------------------------------------------------------------------


async def test_the_body_is_preserved_verbatim_with_its_usage() -> None:
    """The raw body is what phase 03 stores. Parsing it here and storing the
    parsed form would lose the malformed answer that explains a repair round."""
    client, _ = build_client()

    answer = await client.complete(request())

    assert answer.text == '{"schema_version": 1, "claims": []}'
    assert answer.usage.input_tokens == 1200
    assert answer.usage.output_tokens == 800


async def test_an_unparseable_body_is_returned_rather_than_raised() -> None:
    """A successful call that produced an unusable result. The repair ladder
    exists for exactly this, and it needs the body to work with."""
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "choices": [{"message": {"role": "assistant", "content": "{not json"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )
    )
    client, _ = build_client(recorder)

    answer = await client.complete(request())

    assert answer.text == "{not json"
    assert answer.refusal is None


async def test_a_refusal_comes_back_as_a_refusal_with_its_reason() -> None:
    """Not an exception: the reason is the only thing that tells an author
    whether to rephrase the source or stop."""
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "choices": [
                    {"message": {"role": "assistant", "refusal": "I can't help with that."}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        )
    )
    client, _ = build_client(recorder)

    answer = await client.complete(request())

    assert answer.refusal == "I can't help with that."
    assert answer.text == ""


async def test_a_tool_call_keeps_the_arguments_the_model_actually_sent() -> None:
    """Un-normalised, deliberately: what the model *asked* for is the evidence,
    and tidying it here would hide the malformed request behind a later failure."""
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "fetch_url",
                                        "arguments": '{"url": "/x"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    client, _ = build_client(recorder)

    answer = await client.complete(request())

    (call,) = answer.tool_calls
    assert (call.call_id, call.name) == ("call_1", "fetch_url")
    assert call.arguments == {"url": "/x"}


async def test_unparseable_tool_arguments_survive_instead_of_vanishing() -> None:
    """A model that emitted broken JSON for its arguments has made a mistake worth
    seeing. Dropping the call would leave a trace that cannot explain what
    happened next."""
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": "fetch_url", "arguments": "{broken"},
                                }
                            ]
                        }
                    }
                ],
                "usage": {},
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
        (429, LLMRateLimitError),
        (500, LLMProviderError),
        (503, LLMProviderError),
        (400, LLMProviderError),
        (401, LLMProviderError),
    ],
)
async def test_provider_failures_map_to_the_typed_errors_the_ladder_routes_on(
    status: int, expected: type[Exception]
) -> None:
    """plan/03 → retries are *typed*. A bare failure cannot distinguish "we are
    being rate-limited" from "the request was malformed", and those need
    different responses — which is the whole reason the repair ladder branches."""
    recorder = Recorder(httpx.Response(status, json={"error": {"message": "no"}}))
    client, _ = build_client(recorder)

    with pytest.raises(expected):
        await client.complete(request())


async def test_a_timeout_is_a_timeout_and_a_dead_socket_is_a_network_error() -> None:
    """Two different problems with two different fixes: wait longer, or check
    whether anything is listening."""

    def timing_out(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing there")

    timed_out = OpenAIClient(model="gpt-5", api_key=KEY, transport=httpx.MockTransport(timing_out))
    unreachable = OpenAIClient(model="gpt-5", api_key=KEY, transport=httpx.MockTransport(refused))

    with pytest.raises(LLMTimeoutError):
        await timed_out.complete(request())
    with pytest.raises(LLMNetworkError):
        await unreachable.complete(request())


async def test_a_refused_schema_is_typed_apart_from_any_other_bad_request() -> None:
    """Both are 400, and only one is worth a second attempt.

    An overloaded backend answers differently a second later; a schema outside
    strict mode's subset is refused identically forever, so the ladder has to be
    able to tell them apart before it decides to climb.
    """
    rejected = Recorder(
        httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "Invalid schema for response_format 'ArticleScore': In context="
                        "('properties', 'dimensions'), 'propertyNames' is not permitted."
                    ),
                    "code": "invalid_json_schema",
                }
            },
        )
    )
    malformed = Recorder(httpx.Response(400, json={"error": {"message": "unknown parameter"}}))

    client, _ = build_client(rejected)
    with pytest.raises(LLMSchemaRejected):
        await client.complete(request())

    other, _ = build_client(malformed)
    with pytest.raises(LLMProviderError) as generic:
        await other.complete(request())
    assert not isinstance(generic.value, LLMSchemaRejected), "not every 400 is the schema's fault"


async def test_the_key_never_appears_in_a_failure_message() -> None:
    """plan/13 → secrets never reach logs, prompts, artefacts or traces. An error
    string is the easiest of those to forget, because nobody writes it down on
    purpose — it is whatever the exception happened to carry."""
    recorder = Recorder(httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}))
    client, _ = build_client(recorder)

    with pytest.raises(LLMProviderError) as refused:
        await client.complete(request())

    assert KEY not in str(refused.value)


# ----------------------------------------------------------------------
# Cost
# ----------------------------------------------------------------------


async def test_a_priced_model_reports_what_the_call_cost() -> None:
    """The provider reports tokens; the price table turns them into money.

    Applied here rather than further up because this is the only place that knows
    both halves at once — and `TokenUsage.cost_usd` travels with the response into
    the record, so a cost computed later would be a second opinion about a call
    that had already been written down.
    """
    client, _ = build_client(
        pricing=PricingTable(
            version="test",
            models={"gpt-5": ModelPrice(input_per_million=1.0, output_per_million=10.0)},
        )
    )

    answer = await client.complete(request())

    # 1200 in at $1/M, 800 out at $10/M.
    assert answer.usage.cost_usd == pytest.approx((1200 * 1.0 + 800 * 10.0) / 1_000_000)


async def test_an_unpriced_call_reports_no_cost_rather_than_none_spent() -> None:
    """The shipped table is empty, so this is what an installation does by
    default: report the tokens honestly and decline to invent the money."""
    client, _ = build_client()

    answer = await client.complete(request())

    assert answer.usage.input_tokens == 1200
    assert answer.usage.cost_usd is None


async def test_the_model_that_answered_is_what_gets_priced() -> None:
    """Not the model that was asked for. A provider that served
    `gpt-5-2026-01-01` for a request naming `gpt-5` has to be priced as what it
    actually ran, or a table with per-snapshot rates would silently misprice."""
    client, _ = build_client(
        pricing=PricingTable(
            version="test",
            models={
                "gpt-5": ModelPrice(input_per_million=1.0, output_per_million=1.0),
                "gpt-5-2026-01-01": ModelPrice(input_per_million=2.0, output_per_million=2.0),
            },
        )
    )

    answer = await client.complete(request())

    assert answer.usage.cost_usd == pytest.approx(2.0 * 2000 / 1_000_000)


# ----------------------------------------------------------------------
# Streaming
# ----------------------------------------------------------------------


async def test_streaming_yields_the_text_and_then_the_usage() -> None:
    """Part of the protocol, so it is implemented rather than left as a hole the
    contract tests cannot reach."""
    frames = (
        'data: {"choices":[{"delta":{"content":"p99 "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"fell."}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4}}\n\n'
        "data: [DONE]\n\n"
    )
    recorder = Recorder(httpx.Response(200, text=frames))
    client, _ = build_client(recorder)

    chunks = [chunk async for chunk in client.stream(request())]

    assert "".join(chunk.text for chunk in chunks) == "p99 fell."
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.output_tokens == 4
