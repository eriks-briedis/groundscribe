"""Structured generation: routing, rendering, validation, provenance (phase 04).

This is the one path a stage takes to ask a model for structured data. It ties
together the four pieces phase 04 builds — the routing policy, the prompt store,
the client protocol and the phase-03 recorder — so that a stage cannot make a
model call that goes unrecorded, unrouted, or unvalidated.

Two boundaries are deliberate:

- **The generator never returns unvalidated output.** A response that does not
  parse, does not validate, or is a refusal fails the stage instead. plan/04:
  invalid output is never accepted silently.
- **The generator does not own the stage lifecycle.** It fails a stage when it
  gives up (there is nothing else honest to do with a run whose model call cannot
  be completed), but starting, completing and re-entering stages is the state
  machine's job in phase 05.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from groundscribe.llm.protocol import LLMClient, LLMRequest, RuntimeConfig
from groundscribe.llm.routing import ResolvedRoute, RouteOverride, RoutingPolicy
from groundscribe.prompts import PromptStore
from groundscribe.provenance import models
from groundscribe.provenance.enums import ActorType, InvocationOutcome, RetryType
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest, ToolDefinition


class GenerationError(Exception):
    """The generator was asked to do something it is not configured for."""


class GenerationFailed(Exception):
    """A stage's model call could not be completed and the stage was failed.

    Carries the attempts so a caller (and a human) can see *how* it failed
    without going back to the database; ``error_type`` is the same value stored
    on the failed stage execution.
    """

    def __init__(
        self,
        *,
        stage: str,
        error_type: str,
        reason: str,
        attempts: tuple[models.ModelInvocation, ...],
    ) -> None:
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.error_type = error_type
        self.reason = reason
        self.attempts = attempts


@dataclass(frozen=True)
class GenerationResult[T: BaseModel]:
    """A completed generation: the validated value and how it was produced."""

    value: T
    invocation: models.ModelInvocation
    attempts: tuple[models.ModelInvocation, ...]
    route: ResolvedRoute
    request: EffectiveRequest = field(repr=False)


class StructuredGenerator:
    """Routes, renders, calls, validates and records one structured model call.

    Clients are supplied per provider name. Routing names a provider, so
    something has to map that name to a client; doing it here — and failing when
    the mapping is missing — keeps the recorded provider honest, where silently
    using whichever client was available would make it a fiction.
    """

    def __init__(
        self,
        *,
        clients: Mapping[str, LLMClient],
        recorder: ProvenanceRecorder,
        prompts: PromptStore,
        routing: RoutingPolicy,
    ) -> None:
        self._clients = dict(clients)
        self._recorder = recorder
        self._prompts = prompts
        self._routing = routing

    async def generate[T: BaseModel](
        self,
        execution: models.StageExecution,
        *,
        stage: str,
        template_id: str,
        variables: dict[str, Any],
        schema: type[T],
        template_version: str | None = None,
        tools: tuple[ToolDefinition, ...] = (),
        override: RouteOverride | None = None,
    ) -> GenerationResult[T]:
        """Ask the stage's configured model for a value of ``schema``."""
        route = self._routing.resolve(stage, override=override)
        self._record_routing_decision(execution, route, override)
        client = self._client_for(route.primary.provider)

        rendered = self._prompts.render(template_id, variables, version=template_version)
        runtime = route.runtime_config(client.metadata, client.retry_policy)
        output_schema = schema.model_json_schema()
        request = rendered.to_effective_request(
            provider_config=runtime.as_provider_config(),
            tool_definitions=tools,
            output_schema=output_schema,
        )

        response = await client.complete(self._llm_request(stage, request, schema, runtime, tools))
        raw = response.raw_text

        parsed, parse_error = _parse(raw)
        if parse_error is not None:
            invocation = self._record(
                execution, request, runtime, InvocationOutcome.INVALID_JSON, raw, error=parse_error
            )
            raise self._fail(execution, stage, "invalid_json", parse_error, (invocation,))

        try:
            value = schema.model_validate(parsed)
        except ValidationError as exc:
            reason = "; ".join(format_validation_errors(exc))
            invocation = self._record(
                execution,
                request,
                runtime,
                InvocationOutcome.INVALID_SCHEMA,
                raw,
                parsed=parsed,
                error=reason,
            )
            raise self._fail(execution, stage, "invalid_schema", reason, (invocation,)) from exc

        invocation = self._record(
            execution,
            request,
            runtime,
            InvocationOutcome.ACCEPTED,
            raw,
            parsed=parsed,
            validated=value.model_dump(mode="json"),
        )
        return GenerationResult(
            value=value,
            invocation=invocation,
            attempts=(invocation,),
            route=route,
            request=request,
        )

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _client_for(self, provider: str) -> LLMClient:
        client = self._clients.get(provider)
        if client is None:
            raise GenerationError(
                f"routing asked for provider {provider!r} but no client is configured "
                f"(have: {', '.join(sorted(self._clients)) or 'none'})"
            )
        return client

    def _llm_request(
        self,
        stage: str,
        request: EffectiveRequest,
        schema: type[BaseModel],
        runtime: RuntimeConfig,
        tools: tuple[ToolDefinition, ...],
    ) -> LLMRequest:
        """The live request. Unredacted on purpose — redaction is a *storage* rule."""
        return LLMRequest(
            call_key=stage,
            prompt=request.rendered_prompt,
            schema_name=schema.__name__,
            messages=tuple(request.messages),
            tools=tools,
            output_schema=request.output_schema,
            runtime=runtime,
        )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record_routing_decision(
        self,
        execution: models.StageExecution,
        route: ResolvedRoute,
        override: RouteOverride | None,
    ) -> models.DecisionRecord:
        """Record which model was chosen and on whose authority.

        An overridden route is attributed to the person who asked, not to the
        policy they overrode: the policy did not make this choice, and recording
        it as though it did would misdirect the next person asking why this run
        differs from the last.
        """
        overridden = override is not None
        return self._recorder.record_decision(
            execution,
            decision_type="model_routing",
            decided_by=override.requested_by if override is not None else "routing_policy",
            decided_by_type=ActorType.USER if overridden else ActorType.POLICY,
            policy_version=route.policy_version,
            inputs={
                "stage": route.stage,
                "used_default": route.used_default,
                "overrides": route.overrides,
            },
            outcome=f"{route.primary.provider}/{route.primary.model}",
            rationale=override.reason if override is not None else "",
        )

    def _record(
        self,
        execution: models.StageExecution,
        request: EffectiveRequest,
        runtime: RuntimeConfig,
        outcome: InvocationOutcome,
        raw: str,
        *,
        parsed: dict[str, Any] | None = None,
        validated: dict[str, Any] | None = None,
        parent: models.ModelInvocation | None = None,
        retry_type: RetryType | None = None,
        error: str | None = None,
    ) -> models.ModelInvocation:
        """Write one invocation, failed attempts included."""
        return self._recorder.record_model_invocation(
            execution,
            request=request,
            provider=runtime.provider,
            model=runtime.model,
            outcome=outcome,
            raw_response=raw or None,
            parsed_response=parsed,
            validated_response=validated,
            parent=parent,
            retry_type=retry_type,
            error_message=error,
        )

    def _fail(
        self,
        execution: models.StageExecution,
        stage: str,
        error_type: str,
        reason: str,
        attempts: tuple[models.ModelInvocation, ...],
    ) -> GenerationFailed:
        """Fail the stage, keeping every attempt recorded, and hand back the error.

        Returned rather than raised so the call site reads ``raise self._fail(…)``
        and the traceback starts where the decision was made.
        """
        self._recorder.fail_stage(execution, error_type=error_type, error_message=reason)
        return GenerationFailed(
            stage=stage, error_type=error_type, reason=reason, attempts=attempts
        )


def _parse(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a response body, returning the failure rather than raising it.

    An unparseable body is data about the model, not an exception in this
    program: it has to be recorded before anything else happens to it.
    """
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"response is not valid JSON: {exc}"
    if not isinstance(loaded, dict):
        return None, f"response is not a JSON object but {type(loaded).__name__}"
    return loaded, None


def format_validation_errors(exc: ValidationError) -> list[str]:
    """Validation failures as feedback a model can act on.

    Locations are joined with dots and the message is kept verbatim, because this
    text is fed back to the model on the repair path — a stringified exception
    object is not something a model can correct against.
    """
    return [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    ]


__all__ = [
    "GenerationError",
    "GenerationFailed",
    "GenerationResult",
    "StructuredGenerator",
    "format_validation_errors",
]
