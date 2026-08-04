"""A pre-flight check on a provider configuration.

A routing policy names model ids and sampling parameters. Two things about it
cannot be known from the file:

1. **Whether those model ids exist for this key.** Names change, and access
   differs by account.
2. **Whether those models accept those parameters.** Reasoning models reject
   ``temperature`` and ``top_p`` outright.

Both are cheap to find out and expensive to discover halfway through a run, where
the failure arrives attached to an editorial stage and reads as a pipeline
problem rather than a configuration one. So this sends one tiny call per distinct
model and reports what came back.

**Nothing is recorded.** It takes a client rather than a generator, deliberately:
writing a stage execution and a model invocation for a call no article asked for
would put fiction in the trace, and the trace is the product.

**Failures are reported, not raised.** The output is a table of which models
work; an exception on the first bad one would hide every model after it, which is
precisely what somebody configuring an installation needs to see.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from groundscribe.llm.errors import LLMError, LLMRateLimitError, LLMTimeoutError
from groundscribe.llm.pricing import PricingTable
from groundscribe.llm.protocol import LLMRequest, LLMResponse, RuntimeConfig
from groundscribe.llm.routing import ModelChoice, RoutingPolicy
from groundscribe.provenance.schemas import TokenUsage

#: What the probe asks for. Short, answerable by any model, and cheap: this
#: establishes that a call *works*, not that the model is any good at editing. A
#: pre-flight that cost real money is a pre-flight nobody runs before the run
#: that needed it.
PROBE_PROMPT = "Reply with the single word: ready."

#: A ceiling low enough that a probe of a whole routing policy is rounding error.
PROBE_MAX_TOKENS = 16

#: Failures worth trying again rather than editing config for. A rate limit means
#: wait; a rejected parameter means change the file. A table that showed both as
#: "failed" would send somebody to change a config that was correct.
RETRYABLE = (LLMRateLimitError, LLMTimeoutError)


class Completer(Protocol):
    """The one thing a probe needs: something that answers a request."""

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class ModelProbe:
    """What one model did when asked the simplest possible question."""

    model: str
    #: Every stage that would route here, primary or fallback. A failing model is
    #: only actionable if a person can see what it would have broken.
    stages: tuple[str, ...]
    ok: bool
    detail: str
    #: Whether ``config/model-pricing.yaml`` can cost this model. The shipped
    #: table is empty on purpose, so an installation that has not filled it in
    #: should learn that here rather than from a dashboard weeks later.
    priced: bool
    usage: TokenUsage
    #: Whether trying again is the right response, as against editing the config.
    retryable: bool = False


async def probe_models(
    routing: RoutingPolicy, *, clients: Mapping[str, Completer], pricing: PricingTable
) -> tuple[ModelProbe, ...]:
    """Call every model the policy routes to, once, and report what happened.

    Clients are supplied per provider name, the same shape the generator takes and
    for the same reason: a policy may route different stages to different
    providers, and probing a local model through a hosted client would answer a
    question nobody asked. A model whose provider has no client is *reported* as
    unconfigured rather than skipped — silence would read as "fine".

    Sequentially, not concurrently. A dozen simultaneous calls is the fastest way
    to be rate-limited by the very account this is checking, and the answer would
    then be about the burst rather than about the configuration. It also matters
    for a local provider, where concurrent calls to one machine measure contention
    rather than whether the configuration works.
    """
    results: list[ModelProbe] = []
    for model, (stages, choices) in sorted(_models(routing).items()):
        results.append(
            await _probe(model, stages=stages, choices=choices, clients=clients, pricing=pricing)
        )
    return tuple(results)


def _models(routing: RoutingPolicy) -> dict[str, tuple[list[str], list[ModelChoice]]]:
    """Every distinct model, with the stages and route choices that reach it.

    Once per model rather than once per stage: twelve stages share a handful of
    models, and charging for a dozen requests to answer a question about two of
    them is how a pre-flight stops being run.
    """
    found: dict[str, tuple[list[str], list[ModelChoice]]] = defaultdict(lambda: ([], []))
    for stage, route in [("default", routing.default), *routing.stages.items()]:
        for choice in (route.primary, route.fallback):
            if choice is None:
                continue
            stages, choices = found[choice.model]
            if stage not in stages:
                stages.append(stage)
            choices.append(choice)
    return dict(found)


def _demanding(model: str, choices: list[ModelChoice]) -> RuntimeConfig:
    """The most demanding settings any stage asks of this model.

    Parameter acceptance is a property of the *model*, not of the stage that
    happens to be calling it. Several stages share one model with different
    temperatures, so probing with the first stage's settings would report a
    working model while another stage stayed broken — which is worse than not
    probing, because it is believed.

    So: if any stage sets a parameter, the probe sends it. What is rejected here
    is what would have been rejected in a run.
    """
    provider = choices[0].provider
    return RuntimeConfig(
        provider=provider,
        model=model,
        temperature=_first(choice.temperature for choice in choices),
        top_p=_first(choice.top_p for choice in choices),
        seed=_first(choice.seed for choice in choices),
        reasoning_effort=_first(choice.reasoning_effort for choice in choices),
        structured_output_mode=choices[0].structured_output_mode,
        # Deliberately not the stage's budget: this asks for one word.
        max_output_tokens=PROBE_MAX_TOKENS,
        timeout_seconds=min(choice.timeout_seconds or 60.0 for choice in choices),
    )


async def _probe(
    model: str,
    *,
    stages: list[str],
    choices: list[ModelChoice],
    clients: Mapping[str, Completer],
    pricing: PricingTable,
) -> ModelProbe:
    provider = choices[0].provider
    priced_here = pricing.entry_for(model) is not None
    client = clients.get(provider)
    if client is None:
        # Not an exception: this is the single commonest thing a pre-flight has to
        # report, and it is a configuration fact rather than a provider failure.
        return ModelProbe(
            model=model,
            stages=tuple(sorted(stages)),
            ok=False,
            detail=(
                f"routing wants provider {provider!r} but this machine has no client "
                f"for it (configured: {', '.join(sorted(clients)) or 'none'})"
            ),
            priced=priced_here,
            usage=TokenUsage(),
        )

    request = LLMRequest(
        call_key=f"probe:{model}",
        # Plain text, whatever the stages ask for: a structured-output mode that
        # a model rejects should fail loudly in a *run*, but a probe that could
        # not tell "this model does not exist" from "this model will not do JSON"
        # would answer neither question.
        prompt=PROBE_PROMPT,
        runtime=_demanding(model, choices),
    )
    priced = priced_here
    try:
        answer = await client.complete(request)
    except LLMError as exc:
        return ModelProbe(
            model=model,
            stages=tuple(sorted(stages)),
            ok=False,
            detail=str(exc),
            priced=priced,
            usage=TokenUsage(),
            retryable=isinstance(exc, RETRYABLE),
        )
    return ModelProbe(
        model=model,
        stages=tuple(sorted(stages)),
        ok=True,
        detail=(answer.refusal or answer.raw_text or "answered").strip()[:120],
        priced=priced,
        usage=answer.usage,
    )


def _first[T](values: Iterable[T | None]) -> T | None:
    """The first value anybody set, or ``None`` when nobody did."""
    for value in values:
        if value is not None:
            return value
    return None


__all__ = [
    "PROBE_MAX_TOKENS",
    "PROBE_PROMPT",
    "RETRYABLE",
    "Completer",
    "ModelProbe",
    "probe_models",
]
