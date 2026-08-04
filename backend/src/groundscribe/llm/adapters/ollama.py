"""Ollama, over its native ``/api/chat`` wire format.

The adapter that makes plan/00's local-first promise true rather than
aspirational: no key, no account, no bytes leaving the machine. Phase 04 shipped
this as a stub because nothing could call a provider yet; it is wired now for the
mirror image of the reason OpenAI was — a tool that only works for people willing
to send their material to a hosted API is not local-first.

**No SDK**, for the reason the OpenAI adapter gives: the request is a dict posted
with ``httpx``, so a test can assert what was actually sent rather than what a
mock was told.

**The native endpoint, not the OpenAI-compatible one.** Ollama serves
``/v1/chat/completions`` too, and reusing :class:`~groundscribe.llm.adapters.openai.OpenAIClient`
against it would have been a far smaller change. It was measured and rejected:
that endpoint silently ignores ``max_completion_tokens``, which is exactly the
key the OpenAI adapter sends, so every ``max_output_tokens`` in the routing policy
would have quietly stopped applying — a call that succeeds, runs long, and leaves
a record naming a limit that never existed. The native endpoint also carries
three things the compatibility layer has no spelling for at all: ``num_ctx``,
``keep_alive`` and ``think``.

**Nothing is defaulted**, the same rule the OpenAI adapter holds to and for the
same reason: an adapter with its own opinion about temperature makes the recorded
``RuntimeConfig`` a lie by omission. The one exception is ``think``, and it is a
considered one — see :func:`_thinking`.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, Final

import httpx

from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.llm.errors import (
    LLMNetworkError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from groundscribe.llm.pricing import PricingTable
from groundscribe.llm.protocol import (
    LLMRequest,
    LLMResponse,
    ProviderMetadata,
    RetryPolicy,
    RuntimeConfig,
    StreamChunk,
    TokenUsage,
    ToolCall,
)

#: Where Ollama is listening. One variable, and its presence is what registers the
#: provider — there is no key here to stand in for the decision.
OLLAMA_BASE_URL_ENV: Final = "OLLAMA_BASE_URL"

DEFAULT_BASE_URL: Final = "http://localhost:11434"

#: Recorded on every invocation as ``client_version``. Bump it when the request
#: this module builds changes shape: a record that cannot name the client build
#: cannot explain why two runs under one routing version differed.
CLIENT_VERSION: Final = "ollama-native-chat/1"

#: How long the server keeps the model in memory after a call.
#:
#: A pipeline makes a dozen calls to one model in a row, and on a machine where
#: the model does not fit entirely in VRAM, reloading it between stages costs more
#: wall-clock than the generation does. That is a fact about how this application
#: drives the provider, not an editorial choice, which is why it belongs to the
#: client rather than to the per-stage routing policy.
DEFAULT_KEEP_ALIVE: Final = "15m"

#: What a tool call's arguments become when the model emits something that is not
#: an object. Kept rather than dropped — the malformed request the model actually
#: made is the evidence explaining whatever failed next.
UNPARSED_ARGUMENTS_KEY: Final = "__unparsed__"


class OllamaClient:
    """Talks to a local Ollama server, and to nothing else."""

    provider: ClassVar[str] = "ollama"

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 600.0,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        model_revision: str | None = None,
        pricing: PricingTable | None = None,
        keep_alive: str = DEFAULT_KEEP_ALIVE,
    ) -> None:
        base = base_url or os.environ.get(OLLAMA_BASE_URL_ENV) or DEFAULT_BASE_URL
        self._base_url = base.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._keep_alive = keep_alive
        self._retry_policy = retry_policy or RetryPolicy()
        # Empty by default, which reports cost as unknown rather than free. Local
        # inference is not free — it is *unpriced*, and plan/12's rule that unknown
        # is not zero does not bend because the hardware is in the room.
        self._pricing = pricing or PricingTable()
        self._metadata = ProviderMetadata(
            provider=self.provider,
            model=model,
            model_revision=model_revision,
            api_version="api",
            client_version=CLIENT_VERSION,
        )

    # ------------------------------------------------------------------
    # The protocol
    # ------------------------------------------------------------------

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy

    @property
    def pricing(self) -> PricingTable:
        """The table this client costs calls against — read by ``llm probe``."""
        return self._pricing

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """One call. Raises only on *transport* failure, never on a bad answer."""
        payload = self.build_payload(request)
        async with self._client() as http:
            response = await self._send(http, payload)
            return self._interpret(response.json())

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """The same call, incrementally.

        Part of the protocol, so it is implemented rather than left as a hole the
        contract tests cannot reach — nothing in the pipeline streams model output
        today, because progress is streamed from the trace instead (phase 09).
        """
        payload = self.build_payload(request) | {"stream": True}
        async with (
            self._client() as http,
            http.stream("POST", "/api/chat", json=payload) as response,
        ):
            self._raise_for_status(response, model=str(payload.get("model", "")))
            async for line in response.aiter_lines():
                chunk = _decode_stream_line(line)
                if chunk is not None:
                    yield chunk

    # ------------------------------------------------------------------
    # Building the request
    # ------------------------------------------------------------------

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """The request body, with nothing in it the caller did not ask for.

        Public because it is the thing worth inspecting: ``writer llm probe``
        prints it and the tests assert against it. What was sent is the only thing
        a provenance record can honestly be checked against.
        """
        runtime = request.runtime
        payload: dict[str, Any] = {
            "model": runtime.model if runtime is not None else self._metadata.model,
            "messages": _messages(request),
            # Explicit: Ollama streams by default, and a caller that asked for one
            # answer must not have to reassemble it from frames.
            "stream": False,
            "keep_alive": self._keep_alive,
        }

        if runtime is not None:
            options = _options(runtime)
            if options:
                payload["options"] = options
            constraint = _format(runtime.structured_output_mode, request)
            if constraint is not None:
                payload["format"] = constraint
            payload["think"] = _thinking(runtime)

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        # No description: `ToolDefinition` does not carry one, and
                        # synthesising a sentence here would put words in the
                        # record that no offer actually made.
                        "name": tool.name,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        return payload

    # ------------------------------------------------------------------
    # Reading the answer
    # ------------------------------------------------------------------

    def _interpret(self, body: Mapping[str, Any]) -> LLMResponse:
        """Translate one answer, keeping everything that explains it.

        The body is returned as *text*, never parsed into ``output``: phase 03
        stores the raw response, and an adapter that parsed it here would lose the
        malformed answer that explains a repair round.

        An empty answer comes back as an empty answer. On a thinking model with a
        budget too tight for the reasoning trace that is exactly what the provider
        returns, and the trace has to be able to show it rather than raise it away.
        """
        message = body.get("message") or {}
        return LLMResponse(
            text=str(message.get("content") or ""),
            tool_calls=tuple(_tool_calls(message.get("tool_calls") or ())),
            usage=self._priced(_usage(body), body),
            # ``done_reason`` is ``"length"`` when generation stopped at
            # ``num_predict``. Without it a body cut off mid-string is
            # indistinguishable from a model that emitted nonsense, and the two
            # have nothing in common: one is a budget, the other is a prompt.
            stop_reason=str(body.get("done_reason") or "") or None,
        )

    def _priced(self, usage: TokenUsage, body: Mapping[str, Any]) -> TokenUsage:
        """Attach what the call cost, if this installation has said what that is.

        Priced against the model that **answered**, for the reason the OpenAI
        adapter gives: a tag that resolves to a different build has to be costed as
        what it actually ran.
        """
        served = str(body.get("model") or self._metadata.model)
        cost = self._pricing.price(usage, model=served)
        return usage if cost is None else usage.model_copy(update={"cost_usd": cost})

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            headers={"content-type": "application/json"},
        )

    async def _send(self, http: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
        model = str(payload.get("model", ""))
        try:
            response = await http.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"ollama timed out after {self._timeout}s at {self._base_url}: {exc}"
            ) from None
        except httpx.HTTPError as exc:
            # The commonest failure on a local provider by a wide margin, so the
            # message names the fix: the server is not running, or it is not where
            # this installation was told to look.
            raise LLMNetworkError(
                f"ollama unreachable at {self._base_url} ({exc}) — is the server "
                f"running? Set {OLLAMA_BASE_URL_ENV} if it lives elsewhere"
            ) from None
        self._raise_for_status(response, model=model)
        return response

    def _raise_for_status(self, response: httpx.Response, *, model: str) -> None:
        """Turn a status into the typed failure the repair ladder branches on."""
        if response.status_code < 400:
            return
        detail = _error_detail(response)
        if response.status_code == 429:
            raise LLMRateLimitError(f"ollama rate-limited the call: {detail}")
        if response.status_code == 404:
            # The failure an operator actually hits on the first run after editing
            # routing, and the one where naming the fix saves the most time.
            raise LLMProviderError(
                f"ollama has no model {model!r} ({detail}) — pull it first: ollama pull {model}"
            )
        raise LLMProviderError(f"ollama returned {response.status_code}: {detail}")


# ----------------------------------------------------------------------
# Translation, kept outside the client so each piece is checkable alone
# ----------------------------------------------------------------------


def _messages(request: LLMRequest) -> list[dict[str, str]]:
    """The conversation, with the rendered prompt last."""
    messages = [
        {"role": str(message.role), "content": message.content} for message in request.messages
    ]
    if request.prompt:
        messages.append({"role": "user", "content": request.prompt})
    return messages


def _options(runtime: RuntimeConfig) -> dict[str, Any]:
    """Only the sampling parameters the routing policy actually set.

    ``num_predict`` and ``num_ctx`` are the two carrying real risk if they go
    missing, and they fail in opposite directions:

    * ``num_predict`` absent means the budget never applies, and a stage that asked
      for 4096 tokens runs until the model decides to stop.
    * ``num_ctx`` absent means Ollama allocates its own small default and
      **silently truncates the prompt** above it. No error, no warning — the model
      answers confidently about material it was never shown, and the record names
      a prompt that was never delivered in full.
    """
    options: dict[str, Any] = {}
    if runtime.temperature is not None:
        options["temperature"] = runtime.temperature
    if runtime.top_p is not None:
        options["top_p"] = runtime.top_p
    if runtime.seed is not None:
        options["seed"] = runtime.seed
    if runtime.max_output_tokens is not None:
        options["num_predict"] = runtime.max_output_tokens
    if runtime.context_window is not None:
        options["num_ctx"] = runtime.context_window
    if runtime.stop_sequences:
        options["stop"] = list(runtime.stop_sequences)
    return options


def _format(mode: StructuredOutputMode, request: LLMRequest) -> Any | None:
    """How the schema is enforced, in the provider's own spelling.

    Ollama constrains *decoding* against a real JSON Schema, which is a stronger
    guarantee than the compatibility endpoint's JSON mode, and the reason a local
    model can be held to a stage's contract at all.

    ``NATIVE_SCHEMA`` with no schema to enforce degrades to asking for JSON rather
    than sending a malformed request, and the recorded mode still says what was
    asked for — so the weaker constraint is visible rather than silent.
    """
    if mode is StructuredOutputMode.NATIVE_SCHEMA and request.output_schema:
        return request.output_schema
    if mode in (StructuredOutputMode.NATIVE_SCHEMA, StructuredOutputMode.JSON_MODE):
        return "json"
    return None


def _thinking(runtime: RuntimeConfig) -> Any:
    """Whether the model may reason before answering, and how hard.

    The one setting this adapter sends unconditionally, which is a deliberate
    departure from the no-defaults rule the rest of the module follows.

    The reason is measured rather than assumed. On a thinking model the reasoning
    trace is spent from the same ``num_predict`` budget as the answer, and it is
    spent *first*: with thinking on and a 256-token budget, the provider returns
    256 tokens of reasoning, an empty ``content`` and a ``length`` stop. Every
    stage in the routing policy sets ``max_output_tokens``, so inheriting the
    model's own default would make the tightest stages return nothing at all — and
    the repair ladder would read that as a malformed answer rather than an
    exhausted budget, sending the next person to look at the prompt.

    ``reasoning_effort`` unset therefore means thinking off. That is the field's
    plain meaning — no reasoning effort was requested — rather than an opinion
    invented here, and the recorded ``RuntimeConfig`` says the same thing. A stage
    that wants reasoning names an effort and gets it.
    """
    effort = (runtime.reasoning_effort or "").strip().lower()
    if not effort or effort in {"none", "off", "false"}:
        return False
    if effort in {"true", "on"}:
        return True
    # A level ("low"/"medium"/"high"), passed through as the provider spells it. An
    # effort this model does not support is rejected loudly, which is what a
    # configuration mistake deserves to be.
    return effort


def _tool_calls(raw: Any) -> list[ToolCall]:
    """What the model asked to run, as it asked for it.

    Ollama returns arguments already decoded and carries no call id, so one is
    synthesised from position — otherwise a call cannot be matched to its result.
    """
    calls: list[ToolCall] = []
    for index, call in enumerate(raw):
        function = call.get("function") or {}
        calls.append(
            ToolCall(
                call_id=str(call.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=_arguments(function.get("arguments")),
            )
        )
    return calls


def _arguments(raw: Any) -> dict[str, Any]:
    """The model's own arguments, un-normalised, kept even when malformed."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {UNPARSED_ARGUMENTS_KEY: raw}
    return parsed if isinstance(parsed, dict) else {UNPARSED_ARGUMENTS_KEY: raw}


def _usage(body: Mapping[str, Any]) -> TokenUsage:
    """Tokens as reported, under Ollama's own names.

    ``eval_count`` includes any reasoning tokens, which is the honest figure: they
    were generated, they were paid for in time and memory, and a count that hid
    them would make an exhausted budget inexplicable.
    """
    return TokenUsage(
        input_tokens=int(body.get("prompt_eval_count") or 0),
        output_tokens=int(body.get("eval_count") or 0),
    )


def _error_detail(response: httpx.Response) -> str:
    """The provider's own message, which is the useful half of a failure."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, Mapping):
        return str(body.get("error") or body)[:500]
    return str(body)[:500]


def _decode_stream_line(line: str) -> StreamChunk | None:
    """One newline-delimited JSON frame.

    Ollama streams NDJSON rather than server-sent events, so there is no ``data:``
    prefix and no ``[DONE]`` sentinel — the final frame is the one carrying the
    token counts.
    """
    if not line.strip():
        return None
    try:
        frame = json.loads(line)
    except json.JSONDecodeError:
        return None

    text = str((frame.get("message") or {}).get("content") or "")
    usage = _usage(frame) if frame.get("done") else None
    if not text and usage is None:
        return None
    return StreamChunk(text=text, usage=usage)


__all__ = [
    "CLIENT_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_KEEP_ALIVE",
    "OLLAMA_BASE_URL_ENV",
    "UNPARSED_ARGUMENTS_KEY",
    "OllamaClient",
]
