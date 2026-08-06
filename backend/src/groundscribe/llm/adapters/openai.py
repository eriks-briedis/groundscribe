"""OpenAI, over the Chat Completions wire format.

The first adapter in this project that actually calls a provider. Phase 04 left
all three as stubs on purpose; this one is wired because a local-first tool still
has to be usable by someone who does not run models locally.

**No SDK.** The request is assembled as a dict and posted with ``httpx``. That is
a deliberate departure from what the adapter package's docstring anticipated, for
the reason phase 13 wrote its own ``.env`` parser rather than take a dependency:
what this needs is a dozen keys of JSON, and what it must *not* do — send a
setting nobody chose, hide what was transmitted, drift from the recorded request
— is far easier to hold to in code that reads in one screen than through a client
library's option matrix. It also makes the adapter testable against a transport,
so a test can assert what was actually sent rather than what a mock was told.

**Nothing is defaulted here.** Every sampling parameter is sent only if the
routing policy set it. An adapter that supplied its own temperature would be
forming an editorial opinion the routing config is supposed to own, and the
recorded ``RuntimeConfig`` would no longer describe the call. It matters
practically too: reasoning models reject ``temperature`` and ``top_p`` outright,
so a stage that names neither must send neither.

**Failures are typed before they leave.** The repair ladder branches on *why* a
call failed, so an HTTP status becomes the phase-04 error the ladder routes on —
and the API key is scrubbed from anything raised, because an error message is the
one place in a system a secret leaks by accident.
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
    LLMSchemaRejected,
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
    request_timeout,
)

#: Where the key comes from. One variable, read at start-up and never persisted.
#:
#: The environment is the handover every launcher can perform — a shell profile,
#: a systemd unit, a ``launchd`` plist reading from the Keychain, a container
#: secret mounted as a variable — the same reasoning phase 13 gave the trace key
#: store. The standard name rather than a project-specific one, because a machine
#: that already has it set for other tools has already made this choice.
OPENAI_API_KEY_ENV: Final = "OPENAI_API_KEY"

#: Overridable for a compatible gateway (Azure, a proxy, a local shim).
OPENAI_BASE_URL_ENV: Final = "OPENAI_BASE_URL"

DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"

#: Recorded on every invocation as ``client_version``. Bump it when the request
#: this module builds changes shape: a record that cannot name the client build
#: cannot explain why two runs under one routing version differed.
CLIENT_VERSION: Final = "openai-chat-completions/1"

#: What a tool call's arguments become when the model emits invalid JSON for
#: them. Kept rather than dropped — a malformed request the model actually made
#: is the evidence explaining whatever failed next.
UNPARSED_ARGUMENTS_KEY: Final = "__unparsed__"


class MissingAPIKey(Exception):
    """Raised when no key is configured.

    At construction rather than on the first call. A misconfigured deployment
    should fail where the mistake was made; failing at the first model call
    attaches the failure to an editorial stage, where it reads as a pipeline
    problem and sends the next person looking in the wrong place.
    """


class OpenAIClient:
    """Talks to OpenAI's Chat Completions API, and to nothing else."""

    provider: ClassVar[str] = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        organization: str | None = None,
        timeout_seconds: float = 600.0,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        model_revision: str | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        key = (api_key or os.environ.get(OPENAI_API_KEY_ENV, "")).strip()
        if not key:
            raise MissingAPIKey(
                f"no OpenAI key: set {OPENAI_API_KEY_ENV} (an API key from "
                "platform.openai.com — a ChatGPT subscription is a different "
                "product and does not carry one)"
            )
        self._key = key
        base = base_url or os.environ.get(OPENAI_BASE_URL_ENV) or DEFAULT_BASE_URL
        self._base_url = base.rstrip("/")
        self._organization = organization
        self._timeout = timeout_seconds
        self._transport = transport
        self._retry_policy = retry_policy or RetryPolicy()
        # Empty by default, which reports cost as unknown rather than free. The
        # shipped table is empty too, and deliberately so — see `llm.pricing`.
        self._pricing = pricing or PricingTable()
        self._metadata = ProviderMetadata(
            provider=self.provider,
            model=model,
            model_revision=model_revision,
            api_version="v1",
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
        """The table this client costs calls against.

        Readable so an operator can be shown it — `writer llm probe` reports
        whether the model it is about to use has a price, which is the difference
        between a cost metric that works and one that silently reads `n/a`.
        """
        return self._pricing

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """One call. Raises only on *transport* failure, never on a bad answer."""
        payload = self.build_payload(request)
        async with self._client(request_timeout(request, self._timeout)) as http:
            response = await self._send(http, payload)
            return self._interpret(response.json())

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """The same call, incrementally.

        Part of the protocol, so it is implemented rather than left as a hole the
        contract tests cannot reach — nothing in the pipeline streams model
        output today, because progress is streamed from the trace instead
        (phase 09).
        """
        payload = self.build_payload(request) | {
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        async with (
            self._client(request_timeout(request, self._timeout)) as http,
            http.stream("POST", "/chat/completions", json=payload) as response,
        ):
            self._raise_for_status(response)
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
        prints it and the tests assert against it. What was sent is the only
        thing a provenance record can honestly be checked against.
        """
        runtime = request.runtime
        payload: dict[str, Any] = {
            "model": runtime.model if runtime is not None else self._metadata.model,
            "messages": _messages(request),
        }

        if runtime is not None:
            payload.update(_sampling(runtime))
            response_format = _response_format(runtime.structured_output_mode, request)
            if response_format is not None:
                payload["response_format"] = response_format
            if runtime.reasoning_effort:
                payload["reasoning_effort"] = runtime.reasoning_effort
            if runtime.tool_choice:
                payload["tool_choice"] = runtime.tool_choice

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        # No description: `ToolDefinition` does not carry one, and
                        # synthesising a sentence here would put words in the
                        # record that no offer actually made. The version is
                        # deliberately not sent either — it is a fact about the
                        # offer, which the trace records, not a fact the model
                        # needs.
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
        stores the raw response, and an adapter that parsed it here would lose
        the malformed answer that explains a repair round.
        """
        choices = body.get("choices") or [{}]
        message = choices[0].get("message") or {}
        return LLMResponse(
            text=str(message.get("content") or ""),
            refusal=message.get("refusal") or None,
            tool_calls=tuple(_tool_calls(message.get("tool_calls") or ())),
            usage=self._priced(_usage(body.get("usage")), body),
            # Why it stopped, kept because "the answer was cut off" and "the
            # answer was wrong" are different problems with different fixes, and
            # a body that ends mid-string cannot tell them apart on its own.
            stop_reason=choices[0].get("finish_reason") or None,
        )

    def _priced(self, usage: TokenUsage, body: Mapping[str, Any]) -> TokenUsage:
        """Attach what the call cost, if this installation has said.

        Priced against the model that **answered**, not the one that was asked
        for: a provider serving `gpt-5-2026-01-01` for a request naming `gpt-5`
        has to be costed as what it actually ran, or a table with per-snapshot
        rates would silently misprice every call.

        Here rather than further up the stack because this is the only place that
        holds both halves at once, and `cost_usd` travels with the response into
        the record — a cost computed later would be a second opinion about a call
        that had already been written down.
        """
        served = str(body.get("model") or self._metadata.model)
        cost = self._pricing.price(usage, model=served)
        return usage if cost is None else usage.model_copy(update={"cost_usd": cost})

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout if timeout is None else timeout,
            transport=self._transport,
            headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self._key}",
            "content-type": "application/json",
        }
        if self._organization:
            headers["openai-organization"] = self._organization
        return headers

    async def _send(self, http: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = await http.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(self._scrub(f"openai timed out: {exc}")) from None
        except httpx.HTTPError as exc:
            raise LLMNetworkError(self._scrub(f"openai unreachable: {exc}")) from None
        self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Turn a status into the typed failure the repair ladder branches on."""
        if response.status_code < 400:
            return
        detail = self._scrub(_error_detail(response))
        if response.status_code == 429:
            raise LLMRateLimitError(f"openai rate-limited the call: {detail}")
        if schema_rejected(detail):
            raise LLMSchemaRejected(f"openai refused the schema: {detail}")
        raise LLMProviderError(f"openai returned {response.status_code}: {detail}")

    def _scrub(self, text: str) -> str:
        """Remove the key from anything about to be raised.

        plan/13 requires secrets removed *before* anything is written, and an
        exception message is where it happens by accident: nobody puts a key
        there on purpose — it arrives inside whatever the provider echoed back,
        and from there it reaches a log, a trace event and a job row.
        """
        return text.replace(self._key, "[REDACTED:api_key]") if self._key else text


# ----------------------------------------------------------------------
# Translation, kept outside the client so each piece is checkable alone
# ----------------------------------------------------------------------


def _messages(request: LLMRequest) -> list[dict[str, str]]:
    """The conversation, with the rendered prompt last.

    A stage that built a conversation has it sent as one; a stage that rendered a
    single prompt sends one user message. Both, because a phase-04 request
    carries either.
    """
    messages = [
        {"role": str(message.role), "content": message.content} for message in request.messages
    ]
    if request.prompt:
        messages.append({"role": "user", "content": request.prompt})
    return messages


def _sampling(runtime: RuntimeConfig) -> dict[str, Any]:
    """Only the parameters the routing policy actually set.

    Absent, never defaulted — see the module docstring. An adapter supplying its
    own temperature would make the recorded runtime config a lie by omission, and
    would break the reasoning models that reject sampling parameters outright.
    """
    settings: dict[str, Any] = {}
    if runtime.temperature is not None:
        settings["temperature"] = runtime.temperature
    if runtime.top_p is not None:
        settings["top_p"] = runtime.top_p
    if runtime.seed is not None:
        settings["seed"] = runtime.seed
    if runtime.max_output_tokens is not None:
        # ``max_completion_tokens``, not ``max_tokens``: the latter is deprecated
        # and is rejected outright by the reasoning models.
        settings["max_completion_tokens"] = runtime.max_output_tokens
    if runtime.stop_sequences:
        settings["stop"] = list(runtime.stop_sequences)
    return settings


#: JSON Schema keywords strict mode rejects outright, as opposed to ignoring.
#:
#: Every one of them is a *content* rule — how short a string may be, how few
#: items a list needs, what a number may not exceed. Strict mode constrains the
#: shape of the answer and declines to constrain its contents, so these are
#: dropped from what the provider is told rather than translated into something
#: it accepts. Nothing is lost by dropping them: the schema they came from is
#: still what parses the response, so `min_length=1` on a claim list is enforced
#: on the way in and becomes repair feedback exactly as it does under
#: ``json_mode``. The difference is only *who* catches it, and how much of a
#: wrong-shaped answer got paid for first.
#: How Pydantic spells a reference into the root schema's ``$defs``.
_DEFS_PREFIX = "#/$defs/"

#: What a 400 says when the schema, rather than the request, was refused.
#:
#: Two markers because the two OpenAI-family backends surface different halves
#: of the same error object: the metered API reduces it to ``message``, and the
#: subscription backend stringifies the whole mapping, which is where ``code``
#: survives. Matching either is what makes one predicate serve both.
_SCHEMA_REJECTED = ("invalid_json_schema", "invalid schema for")


def schema_rejected(detail: str) -> bool:
    """Whether a 400 means the schema was refused rather than the request.

    Public, and imported by ``adapters/chatgpt.py``, for the reason
    :func:`strict_schema` is: one provider's vocabulary arriving at two doors.

    Read off the message rather than a status code because the status cannot
    tell them apart — a malformed request and an unacceptable schema are both
    400, and only one of them is worth retrying.
    """
    lowered = detail.lower()
    return any(marker in lowered for marker in _SCHEMA_REJECTED)


_STRICT_UNSUPPORTED = frozenset(
    {
        "default",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minProperties",
        "maxProperties",
        "patternProperties",
        #: Only reached when the mapping it constrains could not be expanded into
        #: explicit properties — see :func:`_expanded_mapping`. Dropping it loses
        #: the key constraint at the provider, which Pydantic still enforces on
        #: the way in; keeping it is a 400, which enforces nothing anywhere.
        "propertyNames",
    }
)


def strict_schema(schema: Any) -> Any:
    """A Pydantic JSON schema, rewritten into the subset strict mode accepts.

    Public, and imported by ``adapters/chatgpt.py``. That is the one thing the
    two OpenAI-family adapters share, and it is deliberate in the same way their
    *not* sharing an HTTP base class is: strict mode's subset is one provider's
    requirement arriving at two different doors, so defining it twice would mean
    fixing it twice. Everything about how the request is framed and sent still
    differs, and still lives apart.

    Two rules, applied at every level including ``$defs``:

    **Every property is required.** Strict mode has no notion of an optional
    property — a schema that omits one is refused with a 400. Pydantic marks a
    field optional whenever it has a default, so ``evidence``, ``quote`` and
    ``segment_ids`` all arrive optional and all have to be promoted. Promoted
    rather than made nullable: a field with a list default is answered by ``[]``
    and one with ``""`` by an empty string, both of which the model already
    parses back to the default. Nullability would mean teaching every consumer
    that ``None`` and ``()`` are the same thing.

    **Content constraints are stripped** — see :data:`_STRICT_UNSUPPORTED`.

    **A mapping keyed by an enum becomes its own keys.** Pydantic renders
    ``Mapping[ScoreDimension, DimensionScore]`` as an open object: the value
    schema under ``additionalProperties`` and the permitted keys under
    ``propertyNames``. Strict mode accepts neither — it wants every key named as
    a property and ``additionalProperties: false`` — so the seven names are read
    off the enum and written out. The constraint is not weakened by this, it is
    stated in the spelling the provider understands; and it is the one rewrite
    here that *preserves* a constraint rather than deferring it to Pydantic,
    because a closed set of keys is exactly what strict mode is good at.

    Why this exists at all: without it ``native_schema`` is unusable here, and
    the pipeline pays for the difference. Under ``json_mode`` the provider
    guarantees well-formed JSON and nothing about the fields, so a model that
    calls a claim's body ``statement`` instead of ``text`` produces a full-length,
    fully-billed answer that fails validation — twice over, on the extraction
    that measured this, for 68% of the run's cost and no result.
    """
    return _strict(schema, _definitions(schema))


def _definitions(schema: Any) -> dict[str, Any]:
    """The root schema's ``$defs``, which is where an enum's members live.

    Read once at the top and carried down, rather than looked up per node: by the
    time a nested ``propertyNames`` is reached its ``$ref`` still points at the
    root, and a node that only knows itself cannot follow it.
    """
    if not isinstance(schema, dict):
        return {}
    defs = schema.get("$defs")
    return defs if isinstance(defs, dict) else {}


def _strict(schema: Any, defs: dict[str, Any]) -> Any:
    if isinstance(schema, list):
        return [_strict(item, defs) for item in schema]
    if not isinstance(schema, dict):
        return schema

    schema = _expanded_mapping(schema, defs)
    rewritten = {
        key: _strict(value, defs) for key, value in schema.items() if key not in _STRICT_UNSUPPORTED
    }
    properties = rewritten.get("properties")
    if isinstance(properties, dict):
        # sorted, so the schema sent for one model is byte-identical between
        # runs: it is recorded in the request snapshot, and a set's ordering
        # would make two identical calls look like two different ones.
        rewritten["required"] = sorted(properties)
        rewritten.setdefault("additionalProperties", False)
    return rewritten


def _expanded_mapping(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """An enum-keyed mapping written out as the properties strict mode wants.

    Returns ``schema`` unchanged when this is not one: a mapping whose keys are
    open, or whose values have no schema, has nothing to expand into, and its
    ``propertyNames`` is dropped by :data:`_STRICT_UNSUPPORTED` instead.

    The keys are sorted for the reason ``required`` is: the expanded schema is
    recorded in the request snapshot, and two identical calls must not read as
    two different ones because a mapping iterated in a different order.
    """
    names = _key_names(schema.get("propertyNames"), defs)
    values = schema.get("additionalProperties")
    if names is None or not isinstance(values, dict):
        return schema
    expanded = {key: value for key, value in schema.items() if key != "propertyNames"}
    expanded["properties"] = dict.fromkeys(names, values)
    expanded["additionalProperties"] = False
    return expanded


def _key_names(constraint: Any, defs: dict[str, Any]) -> list[str] | None:
    """The keys a ``propertyNames`` constraint permits, if it names a closed set.

    ``None`` for anything else — a pattern, a bare string type, a ``$ref`` to
    something that is not an enum. Only a closed set can be written out as
    properties, and guessing at an open one would send the provider a schema
    that forbids keys the pipeline accepts.
    """
    if not isinstance(constraint, dict):
        return None
    members = constraint.get("enum")
    if members is None:
        ref = constraint.get("$ref")
        if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
            target = defs.get(ref.removeprefix(_DEFS_PREFIX))
            members = target.get("enum") if isinstance(target, dict) else None
    if not isinstance(members, list) or not members:
        return None
    if not all(isinstance(member, str) for member in members):
        return None
    return sorted(members)


def _response_format(mode: StructuredOutputMode, request: LLMRequest) -> dict[str, Any] | None:
    """How the schema is enforced, in the provider's own spelling.

    ``NATIVE_SCHEMA`` with no schema to enforce degrades to asking for JSON
    rather than sending a malformed request. The provider would answer 400, which
    reads as a provider fault when it is a configuration mistake — and the
    recorded mode still says what was asked for, so the weaker constraint is
    visible rather than silent.
    """
    if mode is StructuredOutputMode.NATIVE_SCHEMA and request.output_schema:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": request.schema_name or request.call_key or "response",
                "strict": True,
                "schema": strict_schema(request.output_schema),
            },
        }
    if mode in (StructuredOutputMode.NATIVE_SCHEMA, StructuredOutputMode.JSON_MODE):
        return {"type": "json_object"}
    return None


def _tool_calls(raw: Any) -> list[ToolCall]:
    """What the model asked to run, as it asked for it."""
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


def _usage(raw: Any) -> TokenUsage:
    """Tokens as reported. Cost is left unset — see :mod:`groundscribe.llm.pricing`.

    The provider does not price the call, so neither does this. A cost invented
    here would be a number nobody could check, and phase 12's rule applies:
    unknown is not zero.
    """
    if not isinstance(raw, Mapping):
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
    )


def _error_detail(response: httpx.Response) -> str:
    """The provider's own message, which is the useful half of a failure."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping):
        return str(error.get("message") or error)
    return str(body)[:500]


def _decode_stream_line(line: str) -> StreamChunk | None:
    """One server-sent frame, or nothing when the line carries no content."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        frame = json.loads(payload)
    except json.JSONDecodeError:
        return None

    choices = frame.get("choices") or []
    text = str((choices[0].get("delta") or {}).get("content") or "") if choices else ""
    usage = _usage(frame.get("usage")) if frame.get("usage") else None
    if not text and usage is None:
        return None
    return StreamChunk(text=text, usage=usage)


__all__ = [
    "CLIENT_VERSION",
    "DEFAULT_BASE_URL",
    "OPENAI_API_KEY_ENV",
    "OPENAI_BASE_URL_ENV",
    "UNPARSED_ARGUMENTS_KEY",
    "MissingAPIKey",
    "OpenAIClient",
    "strict_schema",
]
