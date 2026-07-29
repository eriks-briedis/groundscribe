"""Checking a provider configuration before a run depends on it.

Two things about a routing policy cannot be known from the file: whether the
model ids exist for a given key, and whether those models accept the sampling
parameters the file sets — reasoning models reject ``temperature`` and ``top_p``
outright. Both are cheap to find out and expensive to discover halfway through a
pipeline run, where the failure arrives attached to an editorial stage and reads
as a pipeline problem.

So this is the pre-flight: one real call per distinct model, with the parameters
that model would actually be sent, reporting what came back.

**The most demanding parameter set wins.** Several stages share a model with
different temperatures, and parameter acceptance is a property of the model, not
of the stage. Probing with the union of what any stage sets is one call that
catches the rejection; probing with the first stage's settings would pass while
a later stage was still broken.

**A failure is reported, not raised.** The point is a table showing which models
work, and an exception on the first bad one would hide every model after it —
which is the opposite of what somebody configuring an installation needs.
"""

from __future__ import annotations

import pytest

from groundscribe.llm.errors import LLMProviderError, LLMRateLimitError
from groundscribe.llm.pricing import ModelPrice, PricingTable
from groundscribe.llm.probe import probe_models
from groundscribe.llm.protocol import LLMRequest, LLMResponse
from groundscribe.llm.routing import default_routing_policy
from groundscribe.provenance.schemas import TokenUsage


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


async def test_every_distinct_model_is_called_exactly_once() -> None:
    """Once per model, not once per stage.

    Twelve stages share a handful of models. A probe that called per stage would
    charge for a dozen requests to answer a question about two of them, which is
    the sort of pre-flight people stop running.
    """
    routing = default_routing_policy()
    client = Answering()

    results = await probe_models(routing, client=client, pricing=PricingTable())

    probed = [result.model for result in results]
    assert probed == sorted(set(probed)), "a model should be probed once, in a stable order"
    assert len(client.requests) == len(probed)
    assert set(probed) == {
        route.model
        for stage in routing.stages.values()
        for route in (stage.primary, stage.fallback)
        if route is not None
    } | {routing.default.primary.model}


async def test_each_result_names_the_stages_that_depend_on_it() -> None:
    """A failing model is only actionable if a person can see what it would have
    broken. "gpt-5 is unavailable" is a fact; "gpt-5 is unavailable, and it is
    what extraction, review and scoring run on" is a decision."""
    client = Answering()

    results = await probe_models(default_routing_policy(), client=client, pricing=PricingTable())

    by_model = {result.model: result for result in results}
    assert "extract_source_truth" in by_model["gpt-5"].stages
    assert "validate_article" in by_model["gpt-5-mini"].stages
    # Sorted, so two runs of the probe produce the same table.
    assert all(list(result.stages) == sorted(result.stages) for result in results)


async def test_a_model_is_probed_with_the_most_demanding_settings_any_stage_sets() -> None:
    """Parameter acceptance belongs to the model, not the stage.

    `gpt-5` is asked for temperature 0 by scoring and 0.7 with top_p by drafting.
    Probing with only one stage's settings would report a model that works while
    the other stage still failed, which is worse than not probing at all.
    """
    client = Answering()

    await probe_models(default_routing_policy(), client=client, pricing=PricingTable())

    runtime = client.payload_for("gpt-5").runtime
    assert runtime is not None
    assert runtime.temperature is not None
    assert runtime.top_p is not None
    assert runtime.seed is not None


async def test_the_probe_call_is_deliberately_tiny() -> None:
    """It answers "does this work", not "is this any good", so it should cost
    almost nothing. A pre-flight that was expensive would be a pre-flight nobody
    ran before the run that needed it."""
    client = Answering()

    await probe_models(default_routing_policy(), client=client, pricing=PricingTable())

    runtime = client.payload_for("gpt-5").runtime
    assert runtime is not None
    assert runtime.max_output_tokens is not None
    assert runtime.max_output_tokens <= 32
    assert len(client.payload_for("gpt-5").prompt) < 200


async def test_a_failure_is_reported_with_the_providers_own_words() -> None:
    """Reported, not raised: an exception on the first bad model would hide every
    model after it, which is precisely what somebody configuring an installation
    needs to see."""
    client = Answering(
        failures={"gpt-5": LLMProviderError("openai returned 400: unsupported parameter")}
    )

    results = await probe_models(default_routing_policy(), client=client, pricing=PricingTable())

    by_model = {result.model: result for result in results}
    assert by_model["gpt-5"].ok is False
    assert "unsupported parameter" in by_model["gpt-5"].detail
    # And the rest were still tried.
    assert by_model["gpt-5-mini"].ok is True


async def test_a_rate_limit_is_distinguished_from_a_broken_configuration() -> None:
    """Different problems, different responses: one means wait, the other means
    edit the routing file. A table that showed both as "failed" would send
    somebody to change a config that was correct."""
    client = Answering(failures={"gpt-5": LLMRateLimitError("slow down")})

    results = await probe_models(default_routing_policy(), client=client, pricing=PricingTable())

    result = next(result for result in results if result.model == "gpt-5")
    assert result.ok is False
    assert result.retryable is True


async def test_the_probe_says_whether_a_model_has_a_price() -> None:
    """The difference between a cost metric that works and one that silently
    reads `n/a`. The shipped price table is empty on purpose, so an installation
    that has not filled it in should be told once, here, rather than discovering
    it on a dashboard weeks later."""
    client = Answering()
    priced = PricingTable(
        version="test",
        models={"gpt-5": ModelPrice(input_per_million=1.0, output_per_million=1.0)},
    )

    results = await probe_models(default_routing_policy(), client=client, pricing=priced)

    by_model = {result.model: result for result in results}
    assert by_model["gpt-5"].priced is True
    assert by_model["gpt-5-mini"].priced is False


async def test_a_probe_of_a_working_installation_says_so_in_one_place() -> None:
    """The summary a person actually reads."""
    results = await probe_models(
        default_routing_policy(), client=Answering(), pricing=PricingTable()
    )

    assert all(result.ok for result in results)
    assert all(result.usage.input_tokens > 0 for result in results)


async def test_nothing_is_recorded_by_a_probe() -> None:
    """It is a configuration check, not a run.

    Deliberately takes a client rather than a generator: writing a stage
    execution and a model invocation for a call no article asked for would put
    fiction in the trace, and the trace is the product.
    """
    import inspect

    signature = inspect.signature(probe_models)

    assert "recorder" not in signature.parameters
    assert "execution" not in signature.parameters
    assert set(signature.parameters) == {"routing", "client", "pricing"}


@pytest.mark.parametrize("attribute", ["model", "stages", "ok", "detail", "priced", "usage"])
async def test_the_result_carries_what_a_table_needs(attribute: str) -> None:
    """Named so a renderer cannot quietly drop a column."""
    (result, *_) = await probe_models(
        default_routing_policy(), client=Answering(), pricing=PricingTable()
    )

    assert hasattr(result, attribute)
