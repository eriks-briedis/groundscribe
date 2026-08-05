"""Checking a provider configuration before a run depends on it.

Two things about a routing policy cannot be known from the file: whether the
model ids are actually available — pulled, on a local server; permitted, on a
hosted account — and whether those models accept the sampling parameters the file
sets. Both are cheap to find out and expensive to discover halfway through a
pipeline run, where the failure arrives attached to an editorial stage and reads
as a pipeline problem.

So this is the pre-flight: one real call per distinct model, with the parameters
that model would actually be sent, reporting what came back.

**The most demanding parameter set wins.** Several stages share a model with
different temperatures, and parameter acceptance is a property of the model, not
of the stage. Probing with the union of what any stage sets is one call that
catches the rejection; probing with the first stage's settings would pass while a
later stage was still broken.

**A failure is reported, not raised.** The point is a table showing which models
work, and an exception on the first bad one would hide every model after it —
which is the opposite of what somebody configuring an installation needs.

**Clients are per provider.** A policy may route different stages to different
providers, so the probe has to reach each model through the client that would
actually carry it. Probing a local model through a hosted client would answer a
question nobody asked.

The tests below build their own routing policies wherever the point is about
*probing behaviour*, and use the shipped one only where the point is that the
shipped configuration itself holds together. Restating the shipped file's model
ids here would mean editing this file every time an operator changed a model,
which is exactly the coupling that makes a suite feel like an obstacle.
"""

from __future__ import annotations

import pytest

from groundscribe.llm.errors import LLMProviderError, LLMRateLimitError
from groundscribe.llm.pricing import ModelPrice, PricingTable
from groundscribe.llm.probe import probe_models
from groundscribe.llm.protocol import LLMRequest, LLMResponse
from groundscribe.llm.routing import ModelChoice, RoutingPolicy, StageRoute, default_routing_policy
from groundscribe.provenance.schemas import TokenUsage

JUDGE = "judging-model"
CHEAP = "cheap-model"


class Answering:
    """A client that answers, remembering what each call asked for."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self._failures = failures or {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        model = request.runtime.model if request.runtime else ""
        if model in self._failures:
            raise self._failures[model]
        return LLMResponse(text="ok", usage=TokenUsage(input_tokens=8, output_tokens=2))

    def payload_for(self, model: str) -> LLMRequest:
        return next(
            request
            for request in self.requests
            if request.runtime is not None and request.runtime.model == model
        )


def two_model_policy(provider: str = "test-provider") -> RoutingPolicy:
    """A policy with two models, and one model reached by two stages that ask for
    different things — the shape the probe's interesting behaviour is about."""
    return RoutingPolicy(
        version="test",
        default=StageRoute(primary=ModelChoice(provider=provider, model=CHEAP, temperature=0.0)),
        stages={
            "judging": StageRoute(
                primary=ModelChoice(
                    provider=provider, model=JUDGE, temperature=0.0, seed=7, max_output_tokens=8192
                ),
                fallback=ModelChoice(provider=provider, model=CHEAP, temperature=0.0),
            ),
            "writing": StageRoute(
                primary=ModelChoice(provider=provider, model=JUDGE, temperature=0.7, top_p=0.9)
            ),
        },
    )


def shipped_clients(client: Answering) -> dict[str, Answering]:
    """One client per provider the shipped policy actually names."""
    routing = default_routing_policy()
    providers = {
        choice.provider
        for route in [routing.default, *routing.stages.values()]
        for choice in (route.primary, route.fallback)
        if choice is not None
    }
    return dict.fromkeys(providers, client)


async def test_every_distinct_model_is_called_exactly_once() -> None:
    """Once per model, not once per stage.

    A dozen stages share a handful of models. A probe that called per stage would
    make a dozen requests to answer a question about two of them — on a hosted API
    that is a bill, and on a local one it is minutes of model loading.
    """
    routing = two_model_policy()
    client = Answering()

    results = await probe_models(routing, clients={"test-provider": client}, pricing=PricingTable())

    probed = [result.model for result in results]
    assert probed == sorted(set(probed)), "a model should be probed once, in a stable order"
    assert len(client.requests) == len(probed)
    assert set(probed) == {JUDGE, CHEAP}


async def test_the_shipped_configuration_is_probed_end_to_end() -> None:
    """The shipped file is the one an operator actually runs, so it is checked as
    a whole rather than model by model: every model it names is reachable through
    a client for the provider it names."""
    client = Answering()
    routing = default_routing_policy()

    results = await probe_models(routing, clients=shipped_clients(client), pricing=PricingTable())

    assert results, "the shipped policy routes to at least one model"
    assert all(result.ok for result in results)
    assert all(result.usage.input_tokens > 0 for result in results)


async def test_each_result_names_the_stages_that_depend_on_it() -> None:
    """A failing model is only actionable if a person can see what it would have
    broken. "the model is unavailable" is a fact; "it is unavailable, and it is
    what review and scoring run on" is a decision."""
    client = Answering()

    results = await probe_models(
        two_model_policy(), clients={"test-provider": client}, pricing=PricingTable()
    )

    by_model = {result.model: result for result in results}
    assert set(by_model[JUDGE].stages) == {"judging", "writing"}
    assert "default" in by_model[CHEAP].stages
    # Sorted, so two runs of the probe produce the same table.
    assert all(list(result.stages) == sorted(result.stages) for result in results)


async def test_a_model_is_probed_with_the_most_demanding_settings_any_stage_sets() -> None:
    """Parameter acceptance belongs to the model, not the stage.

    One model is asked for temperature 0 with a seed by one stage and 0.7 with
    top_p by another. Probing with only one stage's settings would report a model
    that works while the other stage still failed, which is worse than not probing
    at all — because it is believed.
    """
    client = Answering()

    await probe_models(
        two_model_policy(), clients={"test-provider": client}, pricing=PricingTable()
    )

    runtime = client.payload_for(JUDGE).runtime
    assert runtime is not None
    assert runtime.temperature is not None
    assert runtime.top_p is not None
    assert runtime.seed is not None


async def test_the_probe_call_is_deliberately_tiny() -> None:
    """It answers "does this work", not "is this any good", so it should cost
    almost nothing. A pre-flight that was expensive would be a pre-flight nobody
    ran before the run that needed it."""
    client = Answering()

    await probe_models(
        two_model_policy(), clients={"test-provider": client}, pricing=PricingTable()
    )

    probed = client.payload_for(JUDGE)
    runtime = probed.runtime
    assert runtime is not None
    assert runtime.max_output_tokens is not None
    assert runtime.max_output_tokens <= 32
    assert len(probed.prompt) < 200


async def test_the_probe_prompt_satisfies_the_json_mode_it_sends() -> None:
    """`_demanding` forwards the stages' ``structured_output_mode``, and OpenAI's
    ``json_object`` refuses with a 400 unless the messages mention json.

    So the two have to agree. When they did not, every ``json_mode`` route on that
    provider failed its pre-flight — including working ones — and the failure
    advised deleting the parameter, which would have broken the file it was run to
    protect.
    """
    client = Answering()

    await probe_models(
        two_model_policy(), clients={"test-provider": client}, pricing=PricingTable()
    )

    probed = client.payload_for(JUDGE)
    assert probed.runtime is not None
    assert "json" in probed.prompt.lower(), "the probe sends json mode; the prompt must earn it"


async def test_a_failure_is_reported_with_the_providers_own_words() -> None:
    """Reported, not raised: an exception on the first bad model would hide every
    model after it, which is precisely what somebody configuring an installation
    needs to see."""
    client = Answering(
        failures={JUDGE: LLMProviderError("ollama returned 400: unsupported parameter")}
    )

    results = await probe_models(
        two_model_policy(), clients={"test-provider": client}, pricing=PricingTable()
    )

    by_model = {result.model: result for result in results}
    assert by_model[JUDGE].ok is False
    assert "unsupported parameter" in by_model[JUDGE].detail
    # And the rest were still tried.
    assert by_model[CHEAP].ok is True


async def test_a_rate_limit_is_distinguished_from_a_broken_configuration() -> None:
    """Different problems, different responses: one means wait, the other means
    edit the routing file. A table that showed both as "failed" would send
    somebody to change a config that was correct."""
    client = Answering(failures={JUDGE: LLMRateLimitError("slow down")})

    results = await probe_models(
        two_model_policy(), clients={"test-provider": client}, pricing=PricingTable()
    )

    result = next(result for result in results if result.model == JUDGE)
    assert result.ok is False
    assert result.retryable is True


async def test_a_model_whose_provider_has_no_client_is_reported_not_skipped() -> None:
    """The commonest way a configuration is wrong, and the one a pre-flight exists
    to catch: the routing file names a provider this machine was never configured
    for. Skipping it silently would produce a clean table for an installation that
    cannot run, which is the one outcome worse than a failure."""
    results = await probe_models(
        two_model_policy(provider="ollama"),
        clients={"openai": Answering()},
        pricing=PricingTable(),
    )

    assert results
    assert all(result.ok is False for result in results)
    assert all("ollama" in result.detail for result in results)
    # Not retryable: waiting will not configure a provider.
    assert all(result.retryable is False for result in results)


async def test_each_model_is_probed_through_its_own_providers_client() -> None:
    """A policy may span providers. Probing a local model through a hosted client
    would report on a call that would never be made."""
    local, hosted = Answering(), Answering()
    routing = RoutingPolicy(
        version="test",
        default=StageRoute(primary=ModelChoice(provider="ollama", model=JUDGE)),
        stages={"remote": StageRoute(primary=ModelChoice(provider="openai", model=CHEAP))},
    )

    results = await probe_models(
        routing, clients={"ollama": local, "openai": hosted}, pricing=PricingTable()
    )

    assert all(result.ok for result in results)
    assert [request.runtime.model for request in local.requests if request.runtime] == [JUDGE]
    assert [request.runtime.model for request in hosted.requests if request.runtime] == [CHEAP]


async def test_the_probe_says_whether_a_model_has_a_price() -> None:
    """The difference between a cost metric that works and one that silently reads
    `n/a`. The shipped price table is empty on purpose — and stays empty for local
    models, because local inference is unpriced rather than free — so an
    installation should be told once, here, rather than discovering it on a
    dashboard weeks later."""
    client = Answering()
    priced = PricingTable(
        version="test",
        models={JUDGE: ModelPrice(input_per_million=1.0, output_per_million=1.0)},
    )

    results = await probe_models(
        two_model_policy(), clients={"test-provider": client}, pricing=priced
    )

    by_model = {result.model: result for result in results}
    assert by_model[JUDGE].priced is True
    assert by_model[CHEAP].priced is False


async def test_nothing_is_recorded_by_a_probe() -> None:
    """It is a configuration check, not a run.

    Deliberately takes clients rather than a generator: writing a stage execution
    and a model invocation for a call no article asked for would put fiction in
    the trace, and the trace is the product.
    """
    import inspect

    signature = inspect.signature(probe_models)

    assert "recorder" not in signature.parameters
    assert "execution" not in signature.parameters
    assert set(signature.parameters) == {"routing", "clients", "pricing"}


@pytest.mark.parametrize("attribute", ["model", "stages", "ok", "detail", "priced", "usage"])
async def test_the_result_carries_what_a_table_needs(attribute: str) -> None:
    """Named so a renderer cannot quietly drop a column."""
    (result, *_) = await probe_models(
        two_model_policy(), clients={"test-provider": Answering()}, pricing=PricingTable()
    )

    assert hasattr(result, attribute)
