"""The narrow internal LLM interface (phase 04).

plan/04 → *Provider abstraction*: provider SDK types must never leak into the
domain. The whole system therefore depends on this module — a request shape, a
response shape, and a protocol with four members — and on nothing a provider
ships. The adapters translate; everyone else programs against the protocol.

The interface is deliberately *narrow*. Every capability the spec names is here
(structured generation, text generation, streaming, tool calling, token/cost
reporting, retry policy, provider metadata), but they are expressed through the
shape of one request rather than as a method per capability: a request carrying
an ``output_schema`` is structured generation, one carrying ``tools`` is tool
calling, and one carrying neither is text generation. A wider surface would give
callers more places to depend on provider-specific behaviour, which is the leak
this layer exists to prevent.

``Message``, ``ToolDefinition`` and ``TokenUsage`` are imported from the
provenance schemas rather than redefined: they are the same value objects a stage
execution records, and two definitions would let the sent request and the recorded
request drift. The dependency runs one way only — the LLM layer knows the record
shapes; the provenance layer knows nothing about clients, which is asserted in
``test_provider_isolation``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from groundscribe.llm.enums import StructuredOutputMode
from groundscribe.provenance.schemas import Message, TokenUsage, ToolDefinition

__all__ = [
    "LENGTH_STOP",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "ProviderMetadata",
    "RetryPolicy",
    "RuntimeConfig",
    "StreamChunk",
    "TokenUsage",
    "ToolCall",
]


class ProviderMetadata(BaseModel):
    """Who answered, and with which build.

    ``model`` is the *exact* model id as the provider names it, not an alias:
    aliases move, and a record that says "the latest model" explains nothing six
    months later.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    model_revision: str | None = None
    api_version: str | None = None
    client_version: str | None = None


class RetryPolicy(BaseModel):
    """How many times a *transport* failure may be retried, and how patiently.

    Versioned because it is a policy: "why did this run give up after two
    attempts?" is only answerable if the record names the policy that decided so.

    ``backoff_seconds`` defaults to zero. Sleeping inside a retry loop makes a
    test suite slow and time-dependent; deployments set a real value in config,
    and the default keeps the deterministic tests deterministic.
    """

    model_config = ConfigDict(frozen=True)

    version: str = "1"
    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: float = Field(default=0.0, ge=0.0)


class RuntimeConfig(BaseModel):
    """Everything that shapes a call, captured with the call.

    plan/04 → *Runtime-configuration capture*. The list is exhaustive on purpose:
    a replay that reproduces the prompt but not the temperature, the seed or the
    structured-output mode is not a replay, and the difference is invisible
    unless the record says what the settings were.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    model_revision: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_output_tokens: int | None = None
    #: How much of the model's context window this call may use.
    #:
    #: Recorded because on a locally hosted model it is a *decision*, not a
    #: property of the model: the window is allocated per call, it costs memory,
    #: and a provider that truncates silently above it (Ollama does) produces an
    #: answer about material the model was never shown. A replay that reproduced
    #: the prompt but not the window it was read through is not a replay.
    context_window: int | None = None
    reasoning_effort: str | None = None
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.NATIVE_SCHEMA
    tool_choice: str | None = None
    stop_sequences: tuple[str, ...] = ()
    api_version: str | None = None
    client_version: str | None = None
    timeout_seconds: float | None = None
    retry_policy: RetryPolicy = RetryPolicy()

    def as_provider_config(self) -> dict[str, Any]:
        """The JSON-safe form stored in an effective request's provider config."""
        return self.model_dump(mode="json")


#: What both supported providers call "I stopped because the budget ran out".
#:
#: Ollama reports it as ``done_reason``, OpenAI as ``finish_reason``, and the two
#: happen to agree on this value. Named once so the agreement is a stated fact
#: rather than a coincidence repeated in two adapters.
LENGTH_STOP = "length"


class ToolCall(BaseModel):
    """A tool the model asked to run, as the model asked for it.

    ``arguments`` are the model's own, un-normalised: what it *asked* for is the
    evidence, and normalising before recording would hide the malformed request
    that explains a downstream failure.
    """

    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMRequest(BaseModel):
    """One call, as handed to a client (post-render, post-routing).

    Frozen: a recorded request must reflect exactly what was sent, and a request
    object that could be edited after the fact would make that unprovable.

    ``call_key`` is an opaque label naming the call site. The deterministic fake
    looks up its scripted outcomes by it; real adapters may attach it as request
    metadata. It is not part of the effective request, because it says nothing
    about what the model was asked.

    ``prompt`` and ``messages`` are two spellings of the same thing and a request
    carries **one** of them. Every adapter sends ``messages`` and then appends
    ``prompt``, so a request setting both sends its body twice — which is exactly
    what the pipeline did until the ratio of stored characters to billed tokens
    gave it away: 1.74 chars per token on a `score_article` call whose body was
    ordinary English and JSON, against the ~3.4 the same text tokenizes to when
    sent once. Roughly half of every input token in the system bought a duplicate
    of a string already in the request.

    Stages render a prompt into ``messages`` (:class:`RenderedPrompt` builds the
    user message from the same body); ``prompt`` survives for callers that have no
    conversation to build, which today is :mod:`groundscribe.llm.probe`. Read
    either through :meth:`user_text`.
    """

    model_config = ConfigDict(frozen=True)

    call_key: str
    prompt: str = ""
    schema_name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    messages: tuple[Message, ...] = ()
    tools: tuple[ToolDefinition, ...] = ()
    output_schema: dict[str, Any] | None = None
    runtime: RuntimeConfig | None = None

    def user_text(self) -> str:
        """What the model was actually asked, whichever spelling carried it.

        The last user message, or ``prompt`` when there is no conversation. This
        is the accessor to assert against: a test reading ``prompt`` directly
        passes only while the duplicate exists, which is how the doubling above
        survived as an assertion of correctness.
        """
        for message in reversed(self.messages):
            if str(message.role) == "user":
                return message.content
        return self.prompt


class LLMResponse(BaseModel):
    """What a client returned, before anything has been validated.

    Deliberately permissive: an unparseable body, a refusal and a tool call are
    all *successful* calls that produced no usable structured result, and each
    has to survive into provenance rather than being raised away.

    ``output`` is the convenience form for scripted/structured answers; ``text``
    is what the provider actually emitted. :attr:`raw_text` is what gets stored
    as the raw response, so an invalid body is preserved verbatim.
    """

    output: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    refusal: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    #: Why the provider stopped generating, in the provider's own word.
    #:
    #: Kept verbatim rather than normalised to a flag, because it is evidence: a
    #: record that said "truncated: true" could not later answer *what the
    #: provider actually claimed*. Both providers here spell the one case that
    #: matters the same way — ``"length"`` — which is what :attr:`truncated`
    #: reads. Anything else is passed through unread.
    stop_reason: str | None = None

    @property
    def raw_text(self) -> str:
        """The body as emitted, falling back to the scripted structured form."""
        if self.text:
            return self.text
        if self.output:
            return json.dumps(self.output, sort_keys=True, separators=(",", ":"))
        return ""

    @property
    def truncated(self) -> bool:
        """Whether the provider stopped because the output budget ran out.

        The difference between a model that answered badly and a model that was
        cut off mid-sentence. Only the second is fixed by a number in a config
        file, and only the first is worth another attempt.
        """
        return self.stop_reason == LENGTH_STOP


class StreamChunk(BaseModel):
    """One incremental piece of a streamed response.

    Usage arrives on the final chunk for most providers, so it is optional here
    rather than repeated on every chunk.
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    usage: TokenUsage | None = None


@runtime_checkable
class LLMClient(Protocol):
    """The only LLM surface the rest of groundscribe may depend on.

    Runtime-checkable so conformance is asserted in tests as well as by mypy: a
    protocol enforced only statically degrades the first time something is
    duck-typed past it.
    """

    @property
    def metadata(self) -> ProviderMetadata:
        """Provider, exact model id and client build behind this instance."""
        ...

    @property
    def retry_policy(self) -> RetryPolicy:
        """The transport-retry policy this client was configured with."""
        ...

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Make one call. Raises :mod:`groundscribe.llm.errors` on transport failure."""
        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """Stream the same call incrementally."""
        ...
