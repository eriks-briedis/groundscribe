"""Which providers a running installation can actually reach.

Phase 14 shipped `build_runtime` registering *no* clients, with the reason
written into its docstring: "a local-first tool that silently reached an external
provider would be the opposite of what plan/00 promises". Wiring OpenAI did not
change that position, and wiring Ollama does not either — they just give the
deployment two ways to say yes.

Two gates, and they are deliberately different questions:

**Is a provider reachable at all?** Answered by configuration on the machine: a
key is present, or an address is. This is the operator's decision.

**May *this project's* material go there?** Answered by the project's
`allowed_providers` allow-list (phase 13), which defaults to empty. This is the
author's decision, and no amount of configuration substitutes for it.

Registering a client is therefore necessary and never sufficient, which is what
keeps "local-first by default, with visible data flow to external providers" true
on an installation configured entirely for OpenAI — and, just as importantly,
means the local provider is held to the same consent rule rather than waved
through for being local.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from groundscribe.app.bootstrap import provider_clients
from groundscribe.domain.enums import ArticleDepth
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.llm.adapters.chatgpt import CODEX_AUTH_FILE_ENV, ChatGPTClient
from groundscribe.llm.adapters.ollama import OLLAMA_BASE_URL_ENV, OllamaClient
from groundscribe.llm.adapters.openai import OPENAI_API_KEY_ENV, OpenAIClient
from groundscribe.llm.pricing import ModelPrice, PricingTable
from groundscribe.privacy.visibility import LOCAL_PROVIDERS

KEY = "sk-test-not-a-real-key"
ADDRESS = "http://localhost:11434"


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start every test from an unconfigured machine.

    Without this the suite would pass or fail depending on the developer's own
    shell, and the "nothing configured" test would be the first to go.

    ``chatgpt`` needed the rule extended past the environment, because its
    configuration is not an environment variable: it is a *file*, at a
    well-known path, put there by a different tool. On any machine whose owner
    has ever run ``codex login`` the provider was already configured before the
    suite started — which is a fair description of the risk the adapter carries,
    and a terrible property for a test to have.
    """
    monkeypatch.delenv(OPENAI_API_KEY_ENV, raising=False)
    monkeypatch.delenv(OLLAMA_BASE_URL_ENV, raising=False)
    monkeypatch.setenv(CODEX_AUTH_FILE_ENV, str(tmp_path / "no-such-auth.json"))


def codex_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """What `codex login` leaves behind, minus the part that is real."""
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "header.body.signature",
                    "refresh_token": "refresh",
                    "account_id": "acct-1",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(CODEX_AUTH_FILE_ENV, str(path))
    return path


def constraints(*allowed: str) -> EditorialConstraints:
    return EditorialConstraints(
        audience="engineers",
        platform="blog",
        depth=ArticleDepth.PRACTITIONER,
        allowed_providers=allowed,
    )


def test_nothing_configured_means_no_client() -> None:
    """An installation that has configured nothing reaches nothing.

    The phase-14 default, unchanged: a stage that needs a provider fails loudly
    saying which one it wanted, rather than a tool quietly acquiring the ability
    to send someone's source material anywhere.
    """
    assert provider_clients() == {}


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

    clients = provider_clients()

    assert set(clients) == {"openai"}
    assert isinstance(clients["openai"], OpenAIClient)


def test_an_address_makes_ollama_reachable_under_the_name_routing_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local provider is configured by a different *kind* of fact — an address
    rather than a credential — and that difference is the point rather than an
    inconsistency."""
    monkeypatch.setenv(OLLAMA_BASE_URL_ENV, ADDRESS)

    clients = provider_clients()

    assert set(clients) == {"ollama"}
    assert isinstance(clients["ollama"], OllamaClient)


def test_a_machine_running_ollama_has_not_thereby_volunteered_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter defaults to localhost, so registering it unconditionally would
    have been easy and would have been wrong.

    Plenty of machines run Ollama for something else entirely. Reaching it because
    it happened to be listening is the same class of decision as reaching a hosted
    API because a key happened to be in the environment — an installation says yes
    on purpose, once, by setting the address.
    """
    monkeypatch.delenv(OLLAMA_BASE_URL_ENV, raising=False)

    assert "ollama" not in provider_clients()


def test_both_can_be_configured_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A policy may route the cheap deterministic stages locally and the hard ones
    to a hosted model. Nothing in the wiring forbids it, and the per-project
    allow-list is what decides whether it is permitted."""
    monkeypatch.setenv(OPENAI_API_KEY_ENV, KEY)
    monkeypatch.setenv(OLLAMA_BASE_URL_ENV, ADDRESS)

    assert set(provider_clients()) == {"openai", "ollama"}


def test_a_codex_login_makes_chatgpt_reachable_under_the_name_routing_uses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Configured by a file rather than a variable, because that is where the
    credential already lives — and registered under the name a route names."""
    codex_login(tmp_path, monkeypatch)

    clients = provider_clients()

    assert set(clients) == {"chatgpt"}
    assert isinstance(clients["chatgpt"], ChatGPTClient)


def test_a_half_written_credential_does_not_register_chatgpt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An interrupted or partial login leaves a file that parses and cannot be
    used. Registering on its mere presence would turn "no account id" into a
    stage failure halfway through a run, rather than a provider that was never
    offered."""
    path = codex_login(tmp_path, monkeypatch)
    path.write_text(json.dumps({"tokens": {"access_token": "a.b.c"}}), encoding="utf-8")

    assert provider_clients() == {}


def test_chatgpt_is_reachable_without_being_permitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate that matters most for this provider, because its credential is
    not this application's — it belongs to the Codex CLI and is sitting at a
    well-known path on any machine whose owner has ever logged in there. Finding
    it says nothing about whether this pipeline was meant to spend it, so a
    project still has to name `chatgpt` before a single segment moves."""
    codex_login(tmp_path, monkeypatch)

    assert "chatgpt" in provider_clients()
    assert constraints().permits_provider("chatgpt") is False
    assert constraints("openai").permits_provider("chatgpt") is False
    assert constraints("chatgpt").permits_provider("chatgpt") is True


@pytest.mark.parametrize(
    ("variable", "value", "provider"),
    [(OPENAI_API_KEY_ENV, KEY, "openai"), (OLLAMA_BASE_URL_ENV, ADDRESS, "ollama")],
)
def test_one_client_serves_every_model_the_routing_config_names(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, provider: str
) -> None:
    """The model is a property of the *route*, not of the client.

    `ResolvedRoute.runtime_config` builds each call's `RuntimeConfig` from the
    stage's chosen model, and the recorded invocation stores that. So one client
    per provider is right, and a client per model would be a second place for the
    model to be decided — which is how a run ends up recording one model and
    calling another.
    """
    monkeypatch.setenv(variable, value)

    client = provider_clients()[provider]

    assert client.metadata.provider == provider
    assert client.metadata.client_version, "the record must be able to name the client build"


@pytest.mark.parametrize(
    ("variable", "value", "provider"),
    [(OPENAI_API_KEY_ENV, KEY, "openai"), (OLLAMA_BASE_URL_ENV, ADDRESS, "ollama")],
)
def test_the_client_is_built_with_the_installations_own_prices(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, provider: str
) -> None:
    """So a call reports cost without anything downstream having to look it up.

    The local client gets the same table, which is not pointless: local inference
    is unpriced rather than free, and an operator who has worked out their own
    per-million figure is entitled to see it reported like anyone else's.
    """
    monkeypatch.setenv(variable, value)
    table = PricingTable(
        version="test",
        models={"any-model": ModelPrice(input_per_million=1.0, output_per_million=1.0)},
    )

    client = provider_clients(pricing=table)[provider]

    assert isinstance(client, OpenAIClient | OllamaClient)
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


def test_the_allow_list_governs_the_local_provider_too() -> None:
    """Being local is not consent.

    It would be easy to argue that material never leaving the machine needs no
    permission — and the allow-list would then mean something different depending
    on which provider it was asked about, which is how a rule stops being
    trustworthy. A project names its providers, all of them.
    """
    assert constraints().permits_provider("ollama") is False
    assert constraints("openai").permits_provider("ollama") is False
    assert constraints("ollama").permits_provider("ollama") is True


def test_openai_is_not_a_local_provider_and_ollama_is() -> None:
    """The visibility surface reports local vs external, and this is where that
    answer comes from. Getting it wrong would tell a person their material was
    staying on the machine while it left."""
    assert "openai" not in LOCAL_PROVIDERS
    assert "ollama" in LOCAL_PROVIDERS
