"""Structured generation and the repair ladder (phase 04).

This is the one path a stage takes to ask a model for structured data. It ties
together the four pieces phase 04 builds — the routing policy, the prompt store,
the client protocol and the phase-03 recorder — so that a stage cannot make a
model call that goes unrecorded, unrouted, or unvalidated.

The ladder (plan/04 → *Structured outputs*) is the interesting part:

1. **feedback retry** — re-send the original request with the validation errors
   appended. The model keeps its full task context and is told only what was
   wrong.
2. **constrained repair** — replace the task framing with the dedicated repair
   prompt carrying the schema, the rejected output and the errors. By this point
   the original framing has failed twice, and repeating it is what keeps it
   failing the same way.
3. **model fallback** — re-issue the current request against the stage's
   *configured* fallback model.
4. **escalate** — fail the stage and ask for a human.

Every rung is recorded as an ordered, typed child invocation, so the sequence is
legible afterwards; a bare retry count could not tell this apart from three
rate-limit retries, which needs a completely different fix.

Two boundaries are deliberate:

- **Nothing unvalidated is ever returned.** Invalid output, a refusal, or an
  exhausted ladder fails the stage instead.
- **The generator does not own the stage lifecycle.** It fails a stage when it
  gives up — there is nothing else honest to do with a run whose model call
  cannot be completed — but starting, completing and re-entering stages is the
  state machine's job in phase 05.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from groundscribe.llm.errors import (
    LLMError,
    LLMNetworkError,
    LLMProviderError,
    LLMRateLimitError,
    LLMSchemaRejected,
    LLMTimeoutError,
)
from groundscribe.llm.protocol import LLMClient, LLMRequest, RuntimeConfig, TokenUsage, ToolCall
from groundscribe.llm.routing import ResolvedRoute, RouteOverride, RoutingPolicy
from groundscribe.prompts import PromptStore, RenderedPrompt
from groundscribe.provenance import models
from groundscribe.provenance.enums import (
    ActorType,
    ExecutionStatus,
    InvocationOutcome,
    RetryType,
    ToolInitiator,
)
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest, Message, ToolDefinition


class GenerationError(Exception):
    """The generator was asked to do something it is not configured for."""


class GenerationFailed(Exception):
    """A stage's model call could not be completed and the stage was failed.

    Carries the attempts so a caller (and a human) can see *how* it failed
    without going back to the database; ``error_type`` is the value stored on the
    failed stage execution.
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


class ToolCallRequested(Exception):
    """The model asked to run a tool, so generation paused.

    Not a failure: the stage is left running and every record is intact.
    Executing tools is stage business logic (phases 06-08), so this is raised
    rather than returned — a caller cannot then mistake a paused generation for
    an answer, which a nullable return value would invite.
    """

    def __init__(
        self,
        *,
        stage: str,
        invocation: models.ModelInvocation,
        tool_invocations: tuple[models.ToolInvocation, ...],
        attempts: tuple[models.ModelInvocation, ...],
    ) -> None:
        names = ", ".join(tool.tool_name for tool in tool_invocations)
        super().__init__(f"{stage}: model requested tool call(s): {names}")
        self.stage = stage
        self.invocation = invocation
        self.tool_invocations = tool_invocations
        self.attempts = attempts


class RepairRung(StrEnum):
    """One step of the structured-output repair ladder."""

    FEEDBACK_RETRY = "feedback_retry"
    CONSTRAINED_REPAIR = "constrained_repair"
    MODEL_FALLBACK = "model_fallback"


#: Which retry type each rung records. Fixed here rather than at the call sites
#: so the ladder and the provenance vocabulary cannot drift apart.
RUNG_RETRY_TYPES: dict[RepairRung, RetryType] = {
    RepairRung.FEEDBACK_RETRY: RetryType.INVALID_SCHEMA,
    RepairRung.CONSTRAINED_REPAIR: RetryType.CONTENT_REPAIR,
    RepairRung.MODEL_FALLBACK: RetryType.MODEL_FALLBACK,
}


class RepairPolicy(BaseModel):
    """The ladder, versioned.

    Versioned because escalating to a human is a decision, and phase 03 refuses
    to record a policy decision that cannot name the policy version that made it.
    """

    model_config = ConfigDict(frozen=True)

    version: str = "1"
    ladder: tuple[RepairRung, ...] = (
        RepairRung.FEEDBACK_RETRY,
        RepairRung.CONSTRAINED_REPAIR,
        RepairRung.MODEL_FALLBACK,
    )
    feedback_template_id: str = "repair_feedback"
    repair_template_id: str = "repair"
    #: Hard stop, whatever the ladder and the retry policy say between them. A
    #: loop that cannot terminate is worse than a stage that fails.
    max_total_attempts: int = 8


class _Form(StrEnum):
    """Which request shape the next attempt should send."""

    ORIGINAL = auto()
    FEEDBACK = auto()
    REPAIR = auto()


@dataclass(frozen=True)
class GenerationResult[T: BaseModel]:
    """A completed generation: the validated value and how it was produced."""

    value: T
    invocation: models.ModelInvocation
    attempts: tuple[models.ModelInvocation, ...]
    route: ResolvedRoute
    request: EffectiveRequest = field(repr=False)

    @property
    def usage(self) -> TokenUsage:
        """What every attempt consumed, including the ones that failed.

        Summed from the records rather than tracked alongside them, so the total
        cannot drift from what was stored. Cost stays ``None`` unless at least one
        attempt reported one: zero is a claim that the calls were free.
        """
        costs = [call.cost_usd for call in self.attempts if call.cost_usd is not None]
        return TokenUsage(
            input_tokens=sum(call.input_tokens for call in self.attempts),
            output_tokens=sum(call.output_tokens for call in self.attempts),
            cost_usd=sum(costs) if costs else None,
        )


@dataclass(frozen=True)
class _Transport:
    """How one transport failure is classified into the record vocabulary."""

    outcome: InvocationOutcome
    retry_type: RetryType


#: Checked in order; the first matching exception type wins. A network failure
#: records as a provider error because phase 03's outcome vocabulary is fixed and
#: has no network member — the *retry* type keeps the distinction that matters.
_TRANSPORT_CLASSES: tuple[tuple[type[LLMError], _Transport], ...] = (
    (LLMTimeoutError, _Transport(InvocationOutcome.TIMEOUT, RetryType.NETWORK)),
    (LLMRateLimitError, _Transport(InvocationOutcome.RATE_LIMITED, RetryType.RATE_LIMIT)),
    (LLMNetworkError, _Transport(InvocationOutcome.PROVIDER_ERROR, RetryType.NETWORK)),
    (LLMProviderError, _Transport(InvocationOutcome.PROVIDER_ERROR, RetryType.PROVIDER_ERROR)),
)


class StructuredGenerator:
    """Routes, renders, calls, validates, repairs and records one model call.

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
        repair_policy: RepairPolicy | None = None,
    ) -> None:
        self._clients = dict(clients)
        self._recorder = recorder
        self._prompts = prompts
        self._routing = routing
        self._repair = repair_policy or RepairPolicy()

    @property
    def routing(self) -> RoutingPolicy:
        """The policy this generator routes through.

        Exposed so a stage can ask *where a call would go* before making it — the
        provider-access check in phase 06 has to resolve the same route the call
        will use, and a second copy of the policy would eventually disagree.
        """
        return self._routing

    def with_routing(self, routing: RoutingPolicy) -> StructuredGenerator:
        """The same generator, routing through ``routing`` instead.

        How a project's routing profile (phase 15) reaches the call: the runtime
        builds one generator for the process, and a run rebinds it to its own
        project's policy on the way into the pipeline context. Everything else is
        shared deliberately — the clients are connection pools and the prompt
        store is a cache, and duplicating either per run would make choosing a
        profile cost a reconnect.

        Returns a new generator rather than mutating: two runs of different
        projects are in flight in the same process the moment there is a second
        worker, and a policy swapped in place would be swapped under both.
        """
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone._routing = routing
        return clone

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
        """Ask the stage's configured model for a value of ``schema``.

        Raises :class:`GenerationFailed` when the ladder is exhausted, the
        provider refuses, or transport retries run out — never a partly-valid
        value. Raises :class:`ToolCallRequested` when the model asks for a tool.
        """
        route = self._routing.resolve(stage, override=override)
        self._record_routing_decision(execution, route, override)

        rendered = self._prompts.render(template_id, variables, version=template_version)
        output_schema = schema.model_json_schema()
        rungs = self._available_rungs(route)

        attempts: list[models.ModelInvocation] = []
        parent: models.ModelInvocation | None = None
        retry_type: RetryType | None = None
        rung_index = 0
        form = _Form.ORIGINAL
        use_fallback = False
        transport_attempts = 0
        errors: list[str] = []
        rejected_output = ""

        while True:
            client = self._client_for(route.choice(use_fallback=use_fallback).provider)
            runtime = route.runtime_config(
                client.metadata, client.retry_policy, use_fallback=use_fallback
            )
            request = self._build_request(
                form=form,
                rendered=rendered,
                schema=schema,
                output_schema=output_schema,
                tools=tools,
                runtime=runtime,
                errors=errors,
                rejected_output=rejected_output,
            )

            try:
                response = await client.complete(
                    self._llm_request(stage, request, schema, runtime, tools)
                )
            except LLMError as exc:
                transport = _classify_transport(exc)
                invocation = self._record(
                    execution, request, runtime, transport.outcome, "", parent, retry_type, str(exc)
                )
                attempts.append(invocation)
                if isinstance(exc, LLMSchemaRejected):
                    # Recorded, then escalated — never retried, for the reason a
                    # truncated response is not. Every rung re-sends the schema
                    # the provider just refused: feedback has no model to reach,
                    # and the fallback rung changes which model would have read a
                    # prompt that was never accepted. Three attempts, no
                    # generation, same 400.
                    raise self._escalate(
                        execution,
                        stage,
                        transport.outcome.value,
                        _schema_reason(exc),
                        tuple(attempts),
                    ) from exc
                transport_attempts += 1
                if transport_attempts >= runtime.retry_policy.max_attempts:
                    raise self._escalate(
                        execution, stage, transport.outcome.value, str(exc), tuple(attempts)
                    ) from exc
                parent, retry_type = invocation, transport.retry_type
                continue

            raw = response.raw_text
            usage = response.usage

            if response.refusal is not None:
                # Not retried: a refusal is a deliberate provider decision, and
                # looping on it burns budget to arrive at the same human.
                invocation = self._record(
                    execution,
                    request,
                    runtime,
                    InvocationOutcome.REFUSED,
                    raw,
                    parent,
                    retry_type,
                    response.refusal,
                    usage=usage,
                )
                attempts.append(invocation)
                raise self._escalate(
                    execution,
                    stage,
                    InvocationOutcome.REFUSED.value,
                    response.refusal,
                    tuple(attempts),
                )

            if response.tool_calls:
                invocation = self._record(
                    execution,
                    request,
                    runtime,
                    InvocationOutcome.ACCEPTED,
                    raw,
                    parent,
                    retry_type,
                    usage=usage,
                )
                attempts.append(invocation)
                raise ToolCallRequested(
                    stage=stage,
                    invocation=invocation,
                    tool_invocations=self._record_tool_calls(
                        execution, invocation, response.tool_calls, tools
                    ),
                    attempts=tuple(attempts),
                )

            attempted = _evaluate(raw, schema, truncated=response.truncated)
            if attempted.outcome is InvocationOutcome.TRUNCATED:
                # Recorded, then escalated — never repaired. Every rung asks the
                # same model for the same answer under the same ceiling: feedback
                # cannot help because the model made no mistake, and the fallback
                # rung is designed to *degrade* the call, so it retries with less
                # room than the budget that already proved too small. Climbing the
                # ladder here spends the wall-clock of a whole generation, three
                # more times, to arrive at the same sentence.
                invocation = self._record(
                    execution,
                    request,
                    runtime,
                    InvocationOutcome.TRUNCATED,
                    raw,
                    parent,
                    retry_type,
                    "; ".join(attempted.errors),
                    usage=usage,
                )
                attempts.append(invocation)
                raise self._escalate(
                    execution,
                    stage,
                    InvocationOutcome.TRUNCATED.value,
                    "; ".join(attempted.errors),
                    tuple(attempts),
                )

            if attempted.value is not None:
                invocation = self._record(
                    execution,
                    request,
                    runtime,
                    InvocationOutcome.ACCEPTED,
                    raw,
                    parent,
                    retry_type,
                    usage=usage,
                    parsed=attempted.parsed,
                    validated=attempted.value.model_dump(mode="json"),
                )
                attempts.append(invocation)
                return GenerationResult(
                    value=attempted.value,
                    invocation=invocation,
                    attempts=tuple(attempts),
                    route=route,
                    request=request,
                )

            errors = attempted.errors
            invocation = self._record(
                execution,
                request,
                runtime,
                attempted.outcome,
                raw,
                parent,
                retry_type,
                "; ".join(errors),
                usage=usage,
                parsed=attempted.parsed,
            )
            attempts.append(invocation)
            rejected_output = raw

            exhausted = rung_index >= len(rungs) or len(attempts) >= self._repair.max_total_attempts
            if exhausted:
                raise self._escalate(
                    execution, stage, attempted.outcome.value, "; ".join(errors), tuple(attempts)
                )

            rung = rungs[rung_index]
            rung_index += 1
            parent, retry_type = invocation, RUNG_RETRY_TYPES[rung]
            if rung is RepairRung.FEEDBACK_RETRY:
                form = _Form.FEEDBACK
            elif rung is RepairRung.CONSTRAINED_REPAIR:
                form = _Form.REPAIR
            else:
                # The fallback re-issues whatever the current request form is;
                # only the model changes, which is what makes the swap the one
                # variable under test.
                use_fallback = True

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------

    def _available_rungs(self, route: ResolvedRoute) -> tuple[RepairRung, ...]:
        """The ladder minus rungs this route cannot take.

        A stage with no configured fallback simply has a shorter ladder — the
        alternative, silently falling back to some other model, would put a model
        nobody chose into the record.
        """
        return tuple(
            rung
            for rung in self._repair.ladder
            if rung is not RepairRung.MODEL_FALLBACK or route.fallback is not None
        )

    def _build_request(
        self,
        *,
        form: _Form,
        rendered: RenderedPrompt,
        schema: type[BaseModel],
        output_schema: dict[str, Any],
        tools: tuple[ToolDefinition, ...],
        runtime: RuntimeConfig,
        errors: Sequence[str],
        rejected_output: str,
    ) -> EffectiveRequest:
        """Build the effective request for the next attempt."""
        provider_config = runtime.as_provider_config()
        if form is _Form.REPAIR:
            repair = self._prompts.render(
                self._repair.repair_template_id,
                {
                    "schema_name": schema.__name__,
                    "output_schema": json.dumps(output_schema, indent=2, sort_keys=True),
                    "previous_output": rejected_output,
                    "validation_errors": list(errors),
                },
            )
            return repair.to_effective_request(
                provider_config=provider_config,
                tool_definitions=tools,
                output_schema=output_schema,
            )

        extra: tuple[Message, ...] = ()
        if form is _Form.FEEDBACK:
            feedback = self._prompts.render(
                self._repair.feedback_template_id, {"validation_errors": list(errors)}
            )
            extra = (Message(role="user", content=feedback.rendered_prompt),)

        return rendered.to_effective_request(
            provider_config=provider_config,
            tool_definitions=tools,
            output_schema=output_schema,
            extra_messages=extra,
        )

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
        """The live request. Unredacted on purpose — redaction is a *storage* rule.

        ``prompt`` is deliberately not set. ``request.messages`` already ends with
        the user message built from ``rendered_prompt``, and every adapter sends
        the messages *and then* appends ``prompt`` — so setting both put the whole
        body on the wire twice. The effective request still records
        ``rendered_prompt`` beside the messages, because provenance wants the body
        in the form the template produced it; it is the wire payload that must
        carry it once.
        """
        return LLMRequest(
            call_key=stage,
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
        parent: models.ModelInvocation | None = None,
        retry_type: RetryType | None = None,
        error: str | None = None,
        *,
        usage: TokenUsage | None = None,
        parsed: dict[str, Any] | None = None,
        validated: dict[str, Any] | None = None,
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
            usage=usage,
            error_message=error,
        )

    def _record_tool_calls(
        self,
        execution: models.StageExecution,
        invocation: models.ModelInvocation,
        calls: Sequence[ToolCall],
        offered: Sequence[ToolDefinition],
    ) -> tuple[models.ToolInvocation, ...]:
        """Record what the model asked to run, before anything runs it.

        Recorded as ``PENDING`` with the arguments the model supplied: with no
        tool registry in this phase there is nothing to normalise the arguments
        *into*, so the normalised form is the raw form and the record says so
        rather than inventing a second version of the same fact.
        """
        definitions = {definition.name: definition for definition in offered}
        recorded: list[models.ToolInvocation] = []
        for call in calls:
            definition = definitions.get(call.name)
            recorded.append(
                self._recorder.record_tool_invocation(
                    execution,
                    tool_name=call.name,
                    tool_version=definition.version if definition is not None else "unknown",
                    initiator=ToolInitiator.MODEL_SELECTED,
                    raw_args=dict(call.arguments),
                    normalised_args=dict(call.arguments),
                    raw_result={},
                    normalised_result={},
                    status=ExecutionStatus.PENDING,
                    model_invocation=invocation,
                    approval_required=(
                        definition.requires_approval if definition is not None else True
                    ),
                )
            )
        return tuple(recorded)

    def _escalate(
        self,
        execution: models.StageExecution,
        stage: str,
        error_type: str,
        reason: str,
        attempts: tuple[models.ModelInvocation, ...],
    ) -> GenerationFailed:
        """Rung 4: record the escalation, fail the stage, hand back the error.

        Three writes, none of them optional. The decision names the policy that
        gave up (phase 03 refuses an unattributed one), the trace event is what a
        human-facing queue will read in phase 09, and failing the stage is what
        stops the run from looking healthy while producing nothing.

        Returned rather than raised so the call site reads ``raise
        self._escalate(…)`` and the traceback starts where the decision was made.
        """
        self._recorder.record_decision(
            execution,
            decision_type="repair_escalation",
            decided_by="repair_ladder",
            decided_by_type=ActorType.POLICY,
            policy_version=self._repair.version,
            inputs={
                "stage": stage,
                "error_type": error_type,
                "attempts": len(attempts),
                "ladder": [rung.value for rung in self._repair.ladder],
            },
            outcome="human_intervention_required",
            rationale=reason,
        )
        self._recorder.emit(
            event_type="intervention.requested",
            actor_type=ActorType.SYSTEM,
            actor_id="repair_ladder",
            execution=execution,
            payload={"stage": stage, "error_type": error_type, "attempts": len(attempts)},
        )
        self._recorder.fail_stage(execution, error_type=error_type, error_message=reason)
        return GenerationFailed(
            stage=stage, error_type=error_type, reason=reason, attempts=attempts
        )


def _classify_transport(exc: LLMError) -> _Transport:
    """Map a provider-neutral failure onto the record vocabulary."""
    for error_type, transport in _TRANSPORT_CLASSES:
        if isinstance(exc, error_type):
            return transport
    return _Transport(InvocationOutcome.PROVIDER_ERROR, RetryType.PROVIDER_ERROR)


#: What a person is told when a stage's answer did not fit in its budget.
#:
#: It names the setting because that is the whole fix, and because the message it
#: replaces — "response is not valid JSON: unterminated string" — sends a reader
#: to the prompt, the schema and the model before they think to look at a number.
#: Nothing about the JSON was wrong; there was simply less of it than the answer
#: needed.
#:
#: It no longer names ``config/model-routing.yaml``. Since profiles (phase 15) a
#: project can be running under ``model-routing.<name>.yaml``, and the reader who
#: most needs this message is the one who just moved, whose edit to the file named
#: here would change nothing and look like the fix had failed. The routing policy
#: is not in scope at this point in the ladder, so the message says how to ask
#: rather than guessing.
_TRUNCATED_MESSAGE = (
    "the model stopped because it reached this stage's output budget, so the "
    "answer is cut off mid-value and cannot be parsed. Raise max_output_tokens "
    "for this stage in the routing policy this project runs under — "
    "`writer project routing <project-id>` names it, and 'default' means "
    "config/model-routing.yaml — keeping it inside the context_window where the "
    "policy sets one, since the prompt shares it. Or give the stage less to "
    "produce."
)


def _schema_reason(exc: LLMSchemaRejected) -> str:
    """The provider's complaint, with what to do about it attached.

    The provider names the offending field and stops there, which is the half a
    person cannot act on: the schema it refused is not the one in the repository
    but the rewrite ``strict_schema`` produced, and the fix is a rule there
    rather than an edit to the stage's Pydantic model.
    """
    return (
        f"{exc}. The provider refused the schema itself, so no model read the prompt and "
        "retrying cannot help. Strict mode accepts a subset of JSON Schema; "
        "`strict_schema` in llm/adapters/openai.py rewrites into it, and a construct it "
        "does not yet handle is a rule missing there. Setting this stage's "
        "structured_output_mode to json_mode in its routing profile is the way past it "
        "meanwhile, at the cost strict mode is there to avoid."
    )


@dataclass(frozen=True)
class _Attempted[T: BaseModel]:
    """What one response turned out to be, before anything is recorded.

    Keeps parsing and validation off the ladder's control flow: the loop then
    reads as "what happened, record it, decide the next rung" instead of nesting
    the accepted path three levels inside two failure branches.
    """

    outcome: InvocationOutcome
    parsed: dict[str, Any] | None
    value: T | None
    errors: list[str]


def _evaluate[T: BaseModel](raw: str, schema: type[T], *, truncated: bool = False) -> _Attempted[T]:
    """Parse and validate one response body, keeping every failure as data.

    ``truncated`` is the provider's own account of why it stopped, and it only
    changes the reading of a body that failed to parse: a model that closed its
    object before running out of room produced a usable answer, whatever the stop
    reason said, and rejecting it over a flag would throw away finished work.
    """
    parsed, parse_error = _parse(raw)
    if parsed is None:
        if truncated:
            return _Attempted(InvocationOutcome.TRUNCATED, None, None, [_TRUNCATED_MESSAGE])
        return _Attempted(
            InvocationOutcome.INVALID_JSON, None, None, [parse_error or "unparseable response"]
        )
    try:
        value = schema.model_validate(parsed)
    except ValidationError as exc:
        return _Attempted(
            InvocationOutcome.INVALID_SCHEMA, parsed, None, format_validation_errors(exc)
        )
    return _Attempted(InvocationOutcome.ACCEPTED, parsed, value, [])


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
    "RepairPolicy",
    "RepairRung",
    "StructuredGenerator",
    "ToolCallRequested",
    "format_validation_errors",
]
