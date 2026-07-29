"""Which providers a running installation can actually reach.

Phase 14 shipped `build_runtime` registering *no* clients, with the reason
written into its docstring: "a local-first tool that silently reached an external
provider would be the opposite of what plan/00 promises". Wiring OpenAI does not
change that position — it just gives the deployment a way to say yes.

Two gates, and they are deliberately different questions:

**Is a provider reachable at all?** Answered by configuration on the machine: a
key is present, so a client exists. This is the operator's decision.

**May *this project's* material go there?** Answered by the project's
`allowed_providers` allow-list (phase 13), which defaults to empty. This is the
author's decision, and no amount of configuration substitutes for it.

Registering a client is therefore necessary and never sufficient, which is what
keeps "local-first by default, with visible data flow to external providers"
true even on an installation configured entirely for OpenAI.
"""

from __future__ import annotations

import pytest

from groundscribe.app.bootstrap import openai_clients
from groundscribe.domain.enums import ArticleDepth
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.llm.adapters.openai import OPENAI_API_KEY_ENV, OpenAIClient
from groundscribe.llm.pricing import ModelPrice, PricingTable
from groundscribe.privacy.visibility import LOCAL_PROVIDERS

KEY = "sk-test-not-a-real-key"


def constraints(*allowed: str) -> EditorialConstraints:
    return EditorialConstraints(
        audience="engineers",
        platform="blog",
        depth=ArticleDepth.PRACTITIONER,
        allowed_providers=allowed,
    )


def test_no_key_means_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """An installation that has configured nothing reaches nothing.

    The phase-14 default, unchanged: a stage that needs a provider fails loudly
    saying which one it wanted, rather than a tool quietly acquiring the ability
    to send someone's source material off the machine.
    """
    monkeypatch.delenv(OPENAI_API_KEY_ENV, raising=False)

    assert openai_clients() == {}


def test_a_key_makes_openai_reachable_under_the_name_routing_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyed by the provider string in `config/model-routing.yaml`.

    The generator looks a client up by the provider the route names, so a client
    registered under any other spelling is a client that is never found — and the
    failure would read as "no client for openai" on an installation that had
    configured one.
    """
    monkeypatch.setenv(OPENAI_API_KEY_ENV, KEY)

    clients = openai_clients()

    assert set(clients) == {"openai"}
    assert isinstance(clients["openai"], OpenAIClient)


def test_one_client_serves_every_model_the_routing_config_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model is a property of the *route*, not of the client.

    `RoutingPolicy.runtime_config_for` builds each call's `RuntimeConfig` from the
    stage's chosen model, and the recorded invocation stores that. So one client
    per provider is right, and a client per model would be a second place for the
    model to be decided — which is how a run ends up recording one model and
    calling another.
    """
    monkeypatch.setenv(OPENAI_API_KEY_ENV, KEY)

    client = openai_clients()["openai"]

    assert client.metadata.provider == "openai"
    assert client.metadata.client_version, "the record must be able to name the client build"


def test_the_client_is_built_with_the_installations_own_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a call reports cost without anything downstream having to look it up."""
    monkeypatch.setenv(OPENAI_API_KEY_ENV, KEY)
    table = PricingTable(
        version="test",
        models={"gpt-5": ModelPrice(input_per_million=1.0, output_per_million=1.0)},
    )

    client = openai_clients(pricing=table)["openai"]

    assert isinstance(client, OpenAIClient)
    assert client.pricing is table


def test_a_reachable_provider_is_still_not_a_permitted_one() -> None:
    """plan/13 → the allow-list is the author's decision, not the operator's.

    This is the property that keeps plan/00's promise intact on an installation
    configured entirely for OpenAI: the key says the provider *can* be reached,
    and the project still has to say its material *may* go there. A project that
    has named nothing has consented to nothing.
    """
    assert constraints().permits_provider("openai") is False
    assert constraints("ollama").permits_provider("openai") is False
    assert constraints("openai").permits_provider("openai") is True


def test_openai_is_not_a_local_provider() -> None:
    """The visibility surface reports local vs external, and this is where that
    answer comes from. Getting it wrong would tell a person their material was
    staying on the machine while it left."""
    assert "openai" not in LOCAL_PROVIDERS
