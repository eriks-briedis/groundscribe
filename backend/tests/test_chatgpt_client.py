"""The ChatGPT-subscription adapter (phase 15).

Everything here runs against a mock transport and a temporary credential file.
Nothing reads ``~/.codex/auth.json`` and nothing reaches the network: this suite
must be runnable on a machine that has never run ``codex login``, and a test that
silently used a developer's real subscription would be spending someone's
capacity to assert a string.

What is worth pinning is not the happy path — one JSON body, like the others —
but the three things this adapter does that no other one does: it holds a
credential that expires, it assembles its answer from a stream rather than a
body, and it is the reason a project's material can reach a second OpenAI
endpoint under different consent.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from groundscribe.llm.adapters.chatgpt import (
    ONLY_MODEL,
    ChatGPTClient,
    CodexAuthError,
    has_credentials,
)
from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.llm.errors import LLMProviderError, LLMRateLimitError
from groundscribe.llm.pricing import ModelPrice, PricingTable
from groundscribe.llm.protocol import LLMClient, LLMRequest, RuntimeConfig
from groundscribe.provenance.schemas import Message
from groundscribe.stages.schemas import SourceModel

pytestmark = pytest.mark.anyio


def jwt(expires_in: float) -> str:
    """A token whose only meaningful claim is when it stops working."""
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time() + expires_in)}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{claims}.signature"


def auth_file(tmp_path: Path, *, expires_in: float = 3600.0, **overrides: Any) -> Path:
    path = tmp_path / "auth.json"
    tokens = {
        "access_token": jwt(expires_in),
        "refresh_token": "refresh-token",
        "id_token": "id-token",
        "account_id": "acct-1",
    } | overrides
    path.write_text(json.dumps({"tokens": tokens, "OPENAI_API_KEY": None}), encoding="utf-8")
    return path


def sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def completed(
    text: str = '{"schema_version": 1}',
    *,
    status: str = "completed",
    usage: dict[str, Any] | None = None,
    incomplete: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The frame sequence the backend actually sends, trimmed to what is read."""
    body: dict[str, Any] = {
        "status": status,
        "model": ONLY_MODEL,
        "output": [{"content": [{"type": "output_text", "text": text}]}],
        "usage": usage
        or {
            "input_tokens": 1200,
            "output_tokens": 800,
            "output_tokens_details": {"reasoning_tokens": 640},
        },
    }
    if incomplete is not None:
        body["incomplete_details"] = incomplete
    kind = {
        "completed": "response.completed",
        "incomplete": "response.incomplete",
        "failed": "response.failed",
    }[status]
    return [
        {"type": "response.created"},
        *(
            {"type": "response.output_text.delta", "delta": chunk}
            for chunk in (text[: len(text) // 2], text[len(text) // 2 :])
            if chunk
        ),
        {"type": kind, "response": body},
    ]


class Recorder:
    """Answers from a script, keeping every request it was sent."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses) or [
            httpx.Response(
                200,
                content=sse(*completed()),
                headers={"content-type": "text/event-stream"},
            )
        ]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses[min(len(self.requests) - 1, len(self._responses) - 1)]

    @property
    def sent(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)

    def to(self, host_fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if host_fragment in str(r.url)]


def stream_response(*events: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=sse(*events), headers={"content-type": "text/event-stream"}
    )


def build_client(
    tmp_path: Path, recorder: Recorder | None = None, **kwargs: Any
) -> tuple[ChatGPTClient, Recorder]:
    scripted = recorder or Recorder()
    client = ChatGPTClient(
        auth_file=kwargs.pop("auth_file", None) or auth_file(tmp_path),
        transport=httpx.MockTransport(scripted),
        **kwargs,
    )
    return client, scripted


def request(
    *,
    mode: StructuredOutputMode = StructuredOutputMode.NATIVE_SCHEMA,
    schema: dict[str, Any] | None = None,
    messages: tuple[Message, ...] = (),
    **runtime: Any,
) -> LLMRequest:
    return LLMRequest(
        call_key="extract_source_truth",
        prompt="Extract the claims.",
        output_schema=schema,
        messages=messages,
        runtime=RuntimeConfig(
            provider="chatgpt", model=ONLY_MODEL, structured_output_mode=mode, **runtime
        ),
    )


# ----------------------------------------------------------------------
# Identity and conformance
# ----------------------------------------------------------------------


def test_it_satisfies_the_only_llm_surface_the_pipeline_depends_on(tmp_path: Path) -> None:
    """Runtime-checkable for the reason the protocol says: a third adapter is
    exactly when duck-typing past the contract starts to happen."""
    client, _ = build_client(tmp_path)

    assert isinstance(client, LLMClient)
    assert client.provider == "chatgpt"
    assert client.metadata.provider == "chatgpt"
    assert client.metadata.model == ONLY_MODEL


def test_it_is_a_different_provider_from_openai(tmp_path: Path) -> None:
    """The consent boundary, asserted rather than assumed.

    `allowed_providers` is how a project says where its material may go, and it
    is matched by this string. Were it "openai", a project that permitted the
    metered API would silently begin sending to a different host under someone
    else's credential — and `writer privacy visibility` would show one row for
    two destinations.
    """
    from groundscribe.llm.adapters.openai import OpenAIClient

    assert ChatGPTClient.provider != OpenAIClient.provider


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------


async def test_a_live_token_is_used_without_asking_for_a_new_one(tmp_path: Path) -> None:
    client, recorder = build_client(tmp_path, auth_file=auth_file(tmp_path, expires_in=3600))

    await client.complete(request())

    assert not recorder.to("auth.openai.com"), "a valid token needs no refresh"
    assert recorder.requests[-1].headers["authorization"].startswith("Bearer ")
    assert recorder.requests[-1].headers["chatgpt-account-id"] == "acct-1"


async def test_a_token_about_to_expire_is_refreshed_and_written_back(tmp_path: Path) -> None:
    """Refreshed *before* expiry, not after: a token that dies mid-call fails a
    stage that was already running, and the write-back is what lets a restart —
    and the Codex CLI itself — carry on from the new one.
    """
    path = auth_file(tmp_path, expires_in=5)
    recorder = Recorder(
        httpx.Response(200, json={"access_token": jwt(3600), "refresh_token": "rotated"}),
        stream_response(*completed()),
    )
    client, _ = build_client(tmp_path, recorder, auth_file=path)

    await client.complete(request())

    assert recorder.to("auth.openai.com"), "an expiring token is refreshed"
    stored = json.loads(path.read_text())["tokens"]
    assert stored["refresh_token"] == "rotated", "a rotated refresh token replaces the old one"
    assert stored["account_id"] == "acct-1", "the rest of the file survives the rewrite"


async def test_a_401_forces_one_refresh_and_one_retry(tmp_path: Path) -> None:
    """A cached token can be invalidated server-side — a fresh `codex login`
    rotates it — while its own `exp` is still hours away, so ordinary refresh
    never fires and every call 401s until someone restarts something."""
    recorder = Recorder(
        httpx.Response(401, json={"detail": "token_invalidated"}),
        httpx.Response(200, json={"access_token": jwt(3600)}),
        stream_response(*completed()),
    )
    client, _ = build_client(tmp_path, recorder)

    answer = await client.complete(request())

    assert answer.text == '{"schema_version": 1}'
    assert len(recorder.to("auth.openai.com")) == 1, "refreshed once, not in a loop"


async def test_a_second_401_gives_up_rather_than_looping(tmp_path: Path) -> None:
    recorder = Recorder(
        httpx.Response(401, json={"detail": "token_invalidated"}),
        httpx.Response(200, json={"access_token": jwt(3600)}),
        httpx.Response(401, json={"detail": "token_invalidated"}),
    )
    client, _ = build_client(tmp_path, recorder)

    with pytest.raises(LLMProviderError):
        await client.complete(request())


async def test_missing_credentials_name_the_command_that_fixes_them(tmp_path: Path) -> None:
    """The commonest state of a machine that has never used this provider, and
    the error has to be a configuration fact rather than a stack trace."""
    client = ChatGPTClient(
        auth_file=tmp_path / "absent.json", transport=httpx.MockTransport(Recorder())
    )

    with pytest.raises(CodexAuthError, match="codex login"):
        await client.complete(request())


def test_incomplete_credentials_say_which_part_is_missing(tmp_path: Path) -> None:
    path = auth_file(tmp_path, account_id="")

    assert has_credentials(path) is False


def test_credentials_present_registers_the_provider(tmp_path: Path) -> None:
    assert has_credentials(auth_file(tmp_path)) is True


# ----------------------------------------------------------------------
# The request
# ----------------------------------------------------------------------


async def test_the_schema_is_sent_through_the_same_strict_rewrite(tmp_path: Path) -> None:
    """The point of the whole adapter. Strict mode is what makes a stage
    validate first time, and the subset it accepts is defined once — in the
    metered adapter — because it is one provider's requirement arriving at two
    doors."""
    client, recorder = build_client(tmp_path)

    await client.complete(request(schema=SourceModel.model_json_schema()))

    fmt = recorder.sent["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    claim = fmt["schema"]["$defs"]["ExtractedClaim"]
    assert claim["required"] == sorted(claim["properties"])
    assert "minItems" not in fmt["schema"]["$defs"]["Evidence"]["properties"]["segment_ids"]


async def test_nothing_is_stored_on_the_providers_side(tmp_path: Path) -> None:
    """The pipeline keeps its own provenance under a per-project retention
    policy. A second copy on someone else's server would sit outside it."""
    client, recorder = build_client(tmp_path)

    await client.complete(request())

    assert recorder.sent["store"] is False


async def test_no_output_ceiling_is_ever_sent(tmp_path: Path) -> None:
    """`max_output_tokens` is `400 Unsupported parameter` on this backend, so a
    routing profile that set one would break every call. The adapter sends only
    what it is given and gives this nothing — but a stale profile copied from
    the metered one is exactly how it would arrive, so it is pinned."""
    client, recorder = build_client(tmp_path)

    await client.complete(request(max_output_tokens=16384))

    assert "max_output_tokens" not in recorder.sent


async def test_the_session_id_is_stable_because_the_cache_is_partitioned_by_it(
    tmp_path: Path,
) -> None:
    """A varying session id re-sends the whole prefix as new. This pipeline
    re-issues a ~38k-token prompt on every repair round, so the only symptom
    would be capacity quietly disappearing."""
    client, recorder = build_client(tmp_path)

    await client.complete(request())
    await client.complete(request())

    ids = {r.headers.get("session_id") for r in recorder.to("chatgpt.com")}
    assert len(ids) == 1 and ids != {None}


async def test_system_messages_become_instructions_not_input(tmp_path: Path) -> None:
    """The Responses API takes them as a separate block; left in `input` they
    would arrive as an ordinary user turn and stop being instructions."""
    client, recorder = build_client(tmp_path)

    await client.complete(
        request(
            messages=(
                Message(role="system", content="You extract source models."),
                Message(role="user", content="Here is the source."),
            )
        )
    )

    body = recorder.sent
    assert body["instructions"] == "You extract source models."
    assert all(item["role"] != "system" for item in body["input"])
    assert body["input"][-1]["content"][0]["text"] == "Extract the claims."


# ----------------------------------------------------------------------
# The answer
# ----------------------------------------------------------------------


async def test_the_answer_is_assembled_from_the_stream(tmp_path: Path) -> None:
    """SSE is the only transport this backend offers, so `complete` is the
    wrapper and `stream` is the mechanism — the reverse of the other two."""
    client, _ = build_client(tmp_path)

    answer = await client.complete(request())

    assert answer.text == '{"schema_version": 1}'
    assert answer.usage.input_tokens == 1200
    assert answer.usage.output_tokens == 800


async def test_a_subscription_call_costs_zero_rather_than_nothing_known(
    tmp_path: Path,
) -> None:
    """The one place this project's "unknown is not zero" rule inverts.

    An unpriced model reports `None` because nobody knows what it costs. This one
    reports 0.00 because the marginal price *is* nothing — the plan is already
    paid for — and a run mixing this profile with a metered one has to total
    rather than collapse to n/a because one leg of it was free.
    """
    priced = PricingTable(
        version="test",
        models={ONLY_MODEL: ModelPrice(input_per_million=0.0, output_per_million=0.0)},
    )
    client, _ = build_client(tmp_path, pricing=priced)

    answer = await client.complete(request())

    assert answer.usage.cost_usd == 0.0, "zero, and not None"


async def test_an_unpriced_installation_still_reports_unknown(tmp_path: Path) -> None:
    """Zero is what the *table* says, not what the adapter assumes. An
    installation that has never heard of this provider gets the honest answer."""
    client, _ = build_client(tmp_path, pricing=PricingTable())

    assert (await client.complete(request())).usage.cost_usd is None


async def test_a_final_frame_alone_still_yields_the_answer(tmp_path: Path) -> None:
    """A backend that batches instead of streaming, or a reconnect that missed
    the deltas, must not produce an empty body that reads as a refusal."""
    recorder = Recorder(
        stream_response(
            {"type": "response.created"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"content": [{"type": "output_text", "text": '{"ok": true}'}]}],
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            },
        )
    )
    client, _ = build_client(tmp_path, recorder)

    assert (await client.complete(request())).text == '{"ok": true}'


async def test_a_cut_off_answer_reports_truncated_in_the_shared_vocabulary(
    tmp_path: Path,
) -> None:
    """This API says `incomplete` + `max_output_tokens` where Chat Completions
    says `length`. Normalised for that one case only, so `LLMResponse.truncated`
    means the same thing whichever adapter produced the record."""
    recorder = Recorder(
        stream_response(
            *completed(
                '{"claims": [',
                status="incomplete",
                incomplete={"reason": "max_output_tokens"},
            )
        )
    )
    client, _ = build_client(tmp_path, recorder)

    answer = await client.complete(request())

    assert answer.truncated is True


async def test_any_other_stop_reason_is_passed_through_unread(tmp_path: Path) -> None:
    recorder = Recorder(
        stream_response(
            *completed("partial", status="incomplete", incomplete={"reason": "content_filter"})
        )
    )
    client, _ = build_client(tmp_path, recorder)

    answer = await client.complete(request())

    assert answer.stop_reason == "content_filter"
    assert answer.truncated is False


async def test_streaming_yields_text_then_usage(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)

    chunks = [chunk async for chunk in client.stream(request())]

    assert "".join(chunk.text for chunk in chunks) == '{"schema_version": 1}'
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.input_tokens == 1200


# ----------------------------------------------------------------------
# Failures
# ----------------------------------------------------------------------


async def test_a_subscription_limit_is_typed_as_a_rate_limit(tmp_path: Path) -> None:
    """Typed as one because that is what the ladder should do with it — wait
    rather than edit config. The message carries what differs: this ceiling
    lifts on a scale of hours, not seconds."""
    recorder = Recorder(httpx.Response(429, json={"detail": "usage limit reached"}))
    client, _ = build_client(tmp_path, recorder)

    with pytest.raises(LLMRateLimitError, match="subscription limit"):
        await client.complete(request())


async def test_a_refused_model_says_what_the_backend_said(tmp_path: Path) -> None:
    """The commonest way this profile breaks: one model is served and every
    other id is refused, so the backend's own sentence is the whole diagnosis."""
    refused = "The 'gpt-5' model is not supported when using Codex with a ChatGPT account."
    recorder = Recorder(httpx.Response(400, json={"detail": refused}))
    client, _ = build_client(tmp_path, recorder)

    with pytest.raises(LLMProviderError, match="not supported when using Codex"):
        await client.complete(request())
