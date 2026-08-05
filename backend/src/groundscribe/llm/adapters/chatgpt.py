"""OpenAI models over a ChatGPT subscription, via the Codex Responses backend.

A third adapter, and the first that is not paid for per token. It reuses the
OAuth credentials ``codex login`` writes to ``~/.codex/auth.json`` and calls
``chatgpt.com/backend-api/codex/responses``, so a run costs subscription
capacity rather than API credits.

**A separate provider, not a mode of ``openai``.** The two send to different
hosts under different credentials with different retention regimes, and
``allowed_providers`` is the unit in which a project consents to any of that
(phase 13). Registered as ``openai`` this would let a project that permitted the
metered API start posting its material to the ChatGPT backend without anyone
deciding so, and ``writer privacy visibility`` could not tell the two apart.

**Three things this backend does not do**, each established by asking it:

- ``max_output_tokens`` is rejected outright — ``400 Unsupported parameter``.
  There is no output ceiling to set, so truncation-by-budget is not a failure
  mode here and answer length is not a thing the routing policy can control.
- Only ``gpt-5.5`` is served. ``gpt-5``, ``gpt-5-mini``, ``gpt-5-codex``,
  ``gpt-5.5-codex``, ``o3`` and ``gpt-4.1`` all answer *"not supported when
  using Codex with a ChatGPT account"*. One model means a fallback rung cannot
  escalate anywhere, which is why the profile mostly has none.
- Nothing is billed, so nothing is priced. What is scarce is the subscription's
  own rate limit, and that is counted rather than costed — see
  :mod:`groundscribe.llm.quota`.

What it *does* do, and the reason this adapter is worth having: strict
``json_schema`` is honoured. Asked with a schema, it returns that schema; asked
without one, the same prompt came back with invented field names. That is the
difference between a stage that validates first time and one that pays for two
wrong shapes before it guesses right.

**SSE is the only transport.** The backend streams whatever it is asked, so
:meth:`complete` assembles the stream and returns the whole answer. That is the
reverse of the other two adapters, where streaming is the unused extra.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, ClassVar, Final

import httpx

from groundscribe.llm.adapters.openai import schema_rejected, strict_schema
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
    LENGTH_STOP,
    LLMRequest,
    LLMResponse,
    ProviderMetadata,
    RetryPolicy,
    RuntimeConfig,
    StreamChunk,
    TokenUsage,
)

#: Where ``codex login`` leaves its tokens. Overridable so a test never reads a
#: real credential and a non-standard install can say where its own lives.
CODEX_AUTH_FILE_ENV: Final = "GROUNDSCRIBE_CODEX_AUTH_FILE"
DEFAULT_AUTH_FILE: Final = "~/.codex/auth.json"

#: The Codex CLI's own public OAuth client id, which is what the refresh grant
#: is issued against. Public in the OAuth sense — it identifies the client, it
#: does not authenticate it — so it is a constant here rather than a secret.
CODEX_CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_ENDPOINT: Final = "https://auth.openai.com/oauth/token"

DEFAULT_BASE_URL: Final = "https://chatgpt.com/backend-api/codex"
CHATGPT_BASE_URL_ENV: Final = "GROUNDSCRIBE_CHATGPT_BASE_URL"

#: The only model this backend serves. Not a default to be overridden — every
#: other id tested is refused, so a routing profile naming one is a 400 rather
#: than a choice.
ONLY_MODEL: Final = "gpt-5.5"

CLIENT_VERSION: Final = "chatgpt-codex-responses/1"

#: Refresh this far before the JWT expires, so a call never starts with a token
#: that dies mid-flight.
REFRESH_MARGIN_SECONDS: Final = 60.0

#: Sent on every call, and it has to be *stable*.
#:
#: The backend partitions its prompt cache by session, so a value that varies
#: between calls re-sends the whole prefix as new. This pipeline re-issues a
#: ~38k-token prompt on every repair round, which is exactly the case that would
#: pay for it. Found in the reference implementation, where two independently
#: written call sites had both reached for a fresh UUID and neither noticed,
#: because the only symptom is consumption.
SESSION_ID: Final = "groundscribe"


class CodexAuthError(LLMProviderError):
    """The subscription credential is missing, incomplete or unrefreshable.

    A provider error rather than its own species: to the repair ladder this is a
    call that cannot be made, which is what every other provider failure means
    too. The message is what differs, and it says which command fixes it.
    """


class ChatGPTClient:
    """Talks to the ChatGPT Codex backend, and to nothing else."""

    provider: ClassVar[str] = "chatgpt"

    def __init__(
        self,
        *,
        model: str = ONLY_MODEL,
        auth_file: Path | str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 600.0,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        self._auth_file = _auth_path(auth_file)
        base = base_url or os.environ.get(CHATGPT_BASE_URL_ENV) or DEFAULT_BASE_URL
        self._base_url = base.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._retry_policy = retry_policy or RetryPolicy()
        # Expected to price this model at 0.00 rather than to be empty — see
        # `_priced`. Empty is still the safe default here: it reports unknown,
        # and unknown is the honest answer for an installation whose table has
        # not been told this provider exists.
        self._pricing = pricing or PricingTable()
        self._cached: _Token | None = None
        self._metadata = ProviderMetadata(
            provider=self.provider,
            model=model,
            api_version="codex-responses",
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
        """One call, assembled from the stream the backend insists on sending."""
        payload = self.build_payload(request)
        text: list[str] = []
        usage = TokenUsage()
        stop_reason: str | None = None
        refusal: str | None = None

        async for event in self._events(payload):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                text.append(str(event.get("delta") or ""))
            elif kind == "response.refusal.done":
                refusal = str(event.get("refusal") or "") or None
            elif kind in _FINAL_EVENTS:
                body = event.get("response") or {}
                usage = self._priced(_usage(body.get("usage")), body)
                stop_reason = _stop_reason(body)
                # The completed frame carries the whole answer as well as the
                # deltas that built it. Preferred when the deltas produced
                # nothing, so a backend that batches rather than streams — or a
                # reconnect that missed the start — still yields a body.
                if not any(text):
                    text.append(_text_of(body))

        return LLMResponse(
            text="".join(text),
            refusal=refusal,
            usage=usage,
            stop_reason=stop_reason,
        )

    def _priced(self, usage: TokenUsage, body: Mapping[str, Any]) -> TokenUsage:
        """Attach what the call cost, which for a subscription is nothing.

        Zero rather than unset, and the difference is the whole point. Unset
        means *unknown* — the rule the pricing table keeps for a model nobody has
        entered rates for. Here the marginal price of the call is known, and it
        is nothing, because the plan was already paid for. A project mixing this
        profile with a metered one still totals correctly instead of reporting
        n/a for the whole run because one leg of it was free.

        What zero does not say is that the call was free of *consequence*. That
        is what `writer llm quota` answers, and why it exists.

        Priced against the model that answered, as on the metered path: the two
        would disagree the day this backend starts serving a second model, and
        the record should follow what ran.
        """
        served = str(body.get("model") or self._metadata.model)
        cost = self._pricing.price(usage, model=served)
        return usage if cost is None else usage.model_copy(update={"cost_usd": cost})

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """The same call, surfaced as it arrives.

        Unlike the other two adapters this is not the unused half of the
        protocol — it is the transport, and :meth:`complete` is the wrapper.
        """
        payload = self.build_payload(request)
        async for event in self._events(payload):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                yield StreamChunk(text=str(event.get("delta") or ""))
            elif kind in _FINAL_EVENTS:
                yield StreamChunk(usage=_usage((event.get("response") or {}).get("usage")))

    # ------------------------------------------------------------------
    # Building the request
    # ------------------------------------------------------------------

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """The request body, in the Responses vocabulary.

        ``store: false`` on every call. The pipeline keeps its own provenance and
        has a retention policy per project; leaving a second copy on someone
        else's server would put material outside the regime that governs it.
        """
        runtime = request.runtime or RuntimeConfig(provider=self.provider, model=ONLY_MODEL)
        payload: dict[str, Any] = {
            "model": runtime.model or ONLY_MODEL,
            "input": _input(request),
            "stream": True,
            "store": False,
        }
        instructions = _instructions(request)
        if instructions:
            payload["instructions"] = instructions
        if runtime.reasoning_effort:
            payload["reasoning"] = {"effort": runtime.reasoning_effort}
        text_format = _text_format(runtime.structured_output_mode, request)
        if text_format is not None:
            payload["text"] = {"format": text_format}
        return payload

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _events(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Every SSE frame of one call, refreshing once through a 401.

        A cached access token can be invalidated server-side — a fresh ``codex
        login`` rotates it — while its own ``exp`` is still comfortably in the
        future, so the ordinary refresh never fires and every call 401s. One
        forced retry converts that from an outage into a hiccup.
        """
        token = await self._token()
        try:
            async for event in self._open(payload, token):
                yield event
            return
        except _Unauthorized:
            token = await self._token(force=True)

        try:
            async for event in self._open(payload, token):
                yield event
        except _Unauthorized:
            # A 401 that survives a forced refresh is not a stale token, so
            # retrying again would loop against a credential that is simply not
            # accepted. Typed here rather than left to escape: `_Unauthorized`
            # is this module's own signal, and the ladder branches on the phase-04
            # taxonomy — an internal exception reaching it is an unhandled crash
            # wearing the costume of a provider failure.
            raise CodexAuthError(
                "chatgpt rejected the credential even after refreshing it; run `codex login`"
            ) from None

    async def _open(self, payload: dict[str, Any], token: _Token) -> AsyncIterator[dict[str, Any]]:
        try:
            headers = _headers(token)
            async with (
                self._client() as http,
                http.stream("POST", "/responses", json=payload, headers=headers) as response,
            ):
                if response.status_code == 401:
                    await response.aread()
                    raise _Unauthorized
                if response.status_code >= 400:
                    detail = _error_detail(await response.aread())
                    self._raise_for_status(response.status_code, detail)
                async for line in response.aiter_lines():
                    event = _decode_event(line)
                    if event is not None:
                        yield event
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"chatgpt timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"chatgpt unreachable: {exc}") from None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    def _raise_for_status(self, status: int, detail: str) -> None:
        """Turn a status into the typed failure the repair ladder branches on.

        429 is the one that means something different here than on the metered
        path: it is the subscription's own ceiling rather than a per-minute
        throttle, so waiting helps on a scale of hours rather than seconds. It is
        still typed as a rate limit, because that is what the ladder does with
        it — retry rather than edit config — and the message carries the rest.
        """
        if status == 429:
            raise LLMRateLimitError(
                f"chatgpt refused the call, subscription limit reached: {detail}"
            )
        if schema_rejected(detail):
            raise LLMSchemaRejected(f"chatgpt refused the schema: {detail}")
        raise LLMProviderError(f"chatgpt returned {status}: {detail}")

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    async def _token(self, *, force: bool = False) -> _Token:
        if not force and self._cached is not None and self._cached.fresh():
            return self._cached
        stored = _read_auth(self._auth_file)
        if not force and stored.fresh():
            self._cached = stored
            return stored
        refreshed = await self._refresh(stored)
        _write_auth(self._auth_file, refreshed)
        self._cached = refreshed
        return refreshed

    async def _refresh(self, stored: _Token) -> _Token:
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as http:
                response = await http.post(
                    TOKEN_ENDPOINT,
                    json={
                        "client_id": CODEX_CLIENT_ID,
                        "grant_type": "refresh_token",
                        "refresh_token": stored.refresh_token,
                        "scope": "openid profile email",
                    },
                )
        except httpx.HTTPError as exc:
            raise LLMNetworkError(f"chatgpt token refresh unreachable: {exc}") from None
        if response.status_code >= 400:
            raise CodexAuthError(
                f"chatgpt token refresh failed ({response.status_code}); run `codex login`: "
                f"{_error_detail(response.content)}"
            )
        body = response.json()
        access = str(body.get("access_token") or "")
        if not access:
            raise CodexAuthError(
                "chatgpt token refresh returned no access token; run `codex login`"
            )
        return _Token(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or stored.refresh_token),
            id_token=str(body.get("id_token") or stored.id_token),
            account_id=stored.account_id,
            raw=stored.raw,
        )


class _Unauthorized(Exception):
    """Internal: a 401 worth one forced refresh, never raised past this module."""


class _Token:
    """One set of Codex credentials, and when the access half stops working."""

    __slots__ = ("access_token", "account_id", "id_token", "raw", "refresh_token")

    def __init__(
        self,
        *,
        access_token: str,
        refresh_token: str,
        id_token: str = "",
        account_id: str,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.id_token = id_token
        self.account_id = account_id
        self.raw = raw or {}

    def fresh(self, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return _jwt_expiry(self.access_token) - moment > REFRESH_MARGIN_SECONDS


# ----------------------------------------------------------------------
# Translation, kept outside the client so each piece is checkable alone
# ----------------------------------------------------------------------

#: The frames that carry a final answer. ``incomplete`` is included because a
#: response cut short is still a response with usage and a reason attached, and
#: throwing it away would lose the only account of why it stopped.
_FINAL_EVENTS: Final = frozenset({"response.completed", "response.incomplete", "response.failed"})


def _auth_path(explicit: Path | str | None) -> Path:
    raw = str(explicit or os.environ.get(CODEX_AUTH_FILE_ENV) or DEFAULT_AUTH_FILE)
    return Path(raw).expanduser()


def has_credentials(auth_file: Path | str | None = None) -> bool:
    """Whether this machine can reach the subscription at all.

    What registers the provider, and deliberately a *shape* check rather than a
    live one: start-up asking the network whether a credential works would make
    booting the CLI depend on someone else's uptime.
    """
    try:
        _read_auth(_auth_path(auth_file))
    except CodexAuthError:
        return False
    return True


def _read_auth(path: Path) -> _Token:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CodexAuthError(
            f"no ChatGPT credentials at {path}; run `codex login` "
            f"(or set {CODEX_AUTH_FILE_ENV}, or use the metered `openai` provider)"
        ) from None
    except (OSError, ValueError) as exc:
        raise CodexAuthError(f"ChatGPT credentials at {path} are unreadable: {exc}") from None

    tokens = raw.get("tokens") if isinstance(raw, Mapping) else None
    tokens = tokens if isinstance(tokens, Mapping) else {}
    access = str(tokens.get("access_token") or "")
    refresh = str(tokens.get("refresh_token") or "")
    account = str(tokens.get("account_id") or "")
    if not (access and refresh and account):
        missing = ", ".join(
            name
            for name, value in (
                ("access_token", access),
                ("refresh_token", refresh),
                ("account_id", account),
            )
            if not value
        )
        raise CodexAuthError(
            f"ChatGPT credentials at {path} are incomplete ({missing}); re-run `codex login`"
        )
    return _Token(
        access_token=access,
        refresh_token=refresh,
        id_token=str(tokens.get("id_token") or ""),
        account_id=account,
        raw=dict(raw),
    )


def _write_auth(path: Path, token: _Token) -> None:
    """Persist refreshed tokens, so a restart and the Codex CLI reuse them.

    Written through a temporary file and renamed, because the CLI reads this
    path too and a half-written credential file is worse than a stale one. Best
    effort: a refresh that worked should not fail the call it was for because
    the home directory happens to be read-only.
    """
    payload = dict(token.raw)
    payload["tokens"] = dict(payload.get("tokens") or {}) | {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "id_token": token.id_token,
        "account_id": token.account_id,
    }
    payload["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    scratch = path.with_name(f"{path.name}.groundscribe.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        scratch.chmod(0o600)
        scratch.replace(path)
    except OSError:
        scratch.unlink(missing_ok=True)


def _jwt_expiry(token: str) -> float:
    """The ``exp`` claim, unverified.

    Unverified on purpose: this is not an authorisation decision, it is a guess
    at when to refresh. The backend is what validates the token, and a claim
    read wrong costs one extra refresh.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        return float(claims.get("exp") or 0.0)
    except (IndexError, ValueError, TypeError):
        return 0.0


def _headers(token: _Token) -> dict[str, str]:
    return {
        "authorization": f"Bearer {token.access_token}",
        "chatgpt-account-id": token.account_id,
        "openai-beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "content-type": "application/json",
        "accept": "text/event-stream",
        "session_id": SESSION_ID,
    }


def _instructions(request: LLMRequest) -> str:
    """System messages, which the Responses API takes as one instruction block."""
    return "\n\n".join(
        message.content for message in request.messages if str(message.role) == "system"
    )


def _input(request: LLMRequest) -> list[dict[str, Any]]:
    """The conversation, with the rendered prompt last.

    System messages are lifted out into ``instructions`` above, so what remains
    here is the exchange itself.
    """
    items: list[dict[str, Any]] = []
    for message in request.messages:
        role = str(message.role)
        if role == "system":
            continue
        items.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": _content_type(role), "text": message.content}],
            }
        )
    if request.prompt:
        items.append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": request.prompt}],
            }
        )
    return items


def _content_type(role: str) -> str:
    """The Responses API names content by direction, not by role."""
    return "output_text" if role == "assistant" else "input_text"


def _text_format(mode: StructuredOutputMode, request: LLMRequest) -> dict[str, Any] | None:
    """How the schema is enforced, in this backend's spelling.

    The schema goes through the same strict rewrite the metered adapter uses —
    one subset, defined once, because it is the same provider's requirement
    arriving at a different door.
    """
    if mode is StructuredOutputMode.NATIVE_SCHEMA and request.output_schema:
        return {
            "type": "json_schema",
            "name": request.schema_name or request.call_key or "response",
            "strict": True,
            "schema": strict_schema(request.output_schema),
        }
    if mode in (StructuredOutputMode.NATIVE_SCHEMA, StructuredOutputMode.JSON_MODE):
        return {"type": "json_object"}
    return None


def _decode_event(line: str) -> dict[str, Any] | None:
    """One ``data:`` frame, or ``None`` for the framing around it."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _stop_reason(body: Mapping[str, Any]) -> str | None:
    """Why it stopped, translated into the word both other adapters use.

    The Responses API says ``incomplete`` with a reason beside it where Chat
    Completions says ``length``. Normalised to the latter *only* for that one
    case, so :attr:`LLMResponse.truncated` keeps meaning one thing across three
    adapters; every other value is passed through as the backend said it.
    """
    status = str(body.get("status") or "") or None
    if status != "incomplete":
        return status
    details = body.get("incomplete_details")
    reason = str((details or {}).get("reason") or "") if isinstance(details, Mapping) else ""
    return LENGTH_STOP if reason == "max_output_tokens" else (reason or status)


def _text_of(body: Mapping[str, Any]) -> str:
    """The assembled answer from a final frame."""
    chunks: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        for part in item.get("content") or []:
            if isinstance(part, Mapping) and part.get("type") == "output_text":
                chunks.append(str(part.get("text") or ""))
    return "".join(chunks)


def _usage(raw: Any) -> TokenUsage:
    """Tokens as reported. Cost is left unset — nothing here is billed.

    Richer than the metered path, which is worth keeping: this backend breaks
    out reasoning and cached tokens, and reasoning is the half of the output
    that no prompt change can shorten.
    """
    if not isinstance(raw, Mapping):
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
    )


def _error_detail(body: bytes) -> str:
    """The backend's own message, which is the useful half of a failure."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body.decode(errors="replace")[:500]
    if isinstance(parsed, Mapping):
        detail = parsed.get("detail") or parsed.get("error") or parsed
        return str(detail)[:500]
    return str(parsed)[:500]


__all__ = [
    "CLIENT_VERSION",
    "CODEX_AUTH_FILE_ENV",
    "CODEX_CLIENT_ID",
    "DEFAULT_BASE_URL",
    "ONLY_MODEL",
    "ChatGPTClient",
    "CodexAuthError",
    "has_credentials",
]
