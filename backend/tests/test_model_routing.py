"""Per-stage model routing contract tests (phase 04).

Spec (plan/04):

- a *versioned* per-stage model-routing config — which provider/model/params each
  stage uses (extraction = strong structured model, drafting = prose model,
  validation = cheap/deterministic) — that is overridable and captured per
  execution;
- test-first: *each stage resolves to its configured model; an override is
  captured in the execution record*.

The resolver is exercised against a throwaway config so the tests state the
contract rather than today's model choices; one test then holds the shipped
``config/model-routing.yaml`` to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundscribe.llm import ProviderMetadata, RetryPolicy, StructuredOutputMode
from groundscribe.llm.routing import (
    RouteOverride,
    RoutingConfigError,
    RoutingPolicy,
    default_routing_policy,
)

CONFIG = """
version: "test-1"
description: Routing used by the routing tests.
default:
  primary:
    provider: fake
    model: fake-default
stages:
  extract_claims:
    primary:
      provider: fake
      model: fake-strong
      temperature: 0.0
      seed: 7
      structured_output_mode: native_schema
    fallback:
      provider: fake
      model: fake-mini
  draft_article:
    primary:
      provider: fake
      model: fake-prose
      temperature: 0.8
  validate_article:
    primary:
      provider: fake
      model: fake-cheap
      temperature: 0.0
"""


@pytest.fixture
def policy(tmp_path: Path) -> RoutingPolicy:
    path = tmp_path / "model-routing.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return RoutingPolicy.from_yaml(path)


def test_each_stage_resolves_to_its_configured_model(policy: RoutingPolicy) -> None:
    """plan/04 test-first spec: stated literally.

    Different stages want genuinely different models — a structured extractor, a
    prose writer, a cheap deterministic checker — and hard-coding one model per
    call site is how that turns into an unauditable mess.
    """
    assert policy.resolve("extract_claims").primary.model == "fake-strong"
    assert policy.resolve("draft_article").primary.model == "fake-prose"
    assert policy.resolve("validate_article").primary.model == "fake-cheap"
    assert policy.resolve("extract_claims").primary.seed == 7


def test_an_unconfigured_stage_falls_back_to_the_declared_default(policy: RoutingPolicy) -> None:
    """A new stage must run, but the record has to admit it was not routed
    deliberately — silently inheriting a default is fine; hiding it is not."""
    resolved = policy.resolve("some_new_stage")
    assert resolved.primary.model == "fake-default"
    assert resolved.used_default is True
    assert policy.resolve("draft_article").used_default is False


def test_the_resolved_route_carries_the_policy_version(policy: RoutingPolicy) -> None:
    """Captured per execution, so "why this model?" is answerable after the
    config has moved on."""
    assert policy.version == "test-1"
    assert policy.resolve("draft_article").policy_version == "test-1"


def test_an_override_replaces_only_the_fields_it_names(policy: RoutingPolicy) -> None:
    resolved = policy.resolve(
        "draft_article",
        override=RouteOverride(model="fake-experimental", requested_by="ada"),
    )

    assert resolved.primary.model == "fake-experimental"
    # Untouched fields still come from the policy, not from a bare default.
    assert resolved.primary.temperature == 0.8
    assert resolved.primary.provider == "fake"


def test_an_override_is_captured_with_who_asked_for_it(policy: RoutingPolicy) -> None:
    """plan/04: *an override is captured in the execution record*.

    The captured form is the fields that changed plus the requester — enough for
    a decision record to be attributable, which phase 03 refuses to store
    without.
    """
    resolved = policy.resolve(
        "draft_article",
        override=RouteOverride(model="fake-experimental", temperature=0.2, requested_by="ada"),
    )

    assert resolved.overridden_by == "ada"
    assert resolved.overrides == {"model": "fake-experimental", "temperature": 0.2}
    assert policy.resolve("draft_article").overrides == {}
    assert policy.resolve("draft_article").overridden_by is None


def test_an_override_cannot_be_anonymous() -> None:
    """An unattributable override is exactly what phase 03 refuses to record."""
    with pytest.raises(ValueError, match="requested_by"):
        RouteOverride(model="fake-experimental", requested_by="")


def test_a_stage_may_declare_the_fallback_model_the_ladder_escalates_to(
    policy: RoutingPolicy,
) -> None:
    """Rung 3 of the repair ladder needs a *configured* fallback, not a guess."""
    extraction = policy.resolve("extract_claims")
    assert extraction.fallback is not None
    assert extraction.fallback.model == "fake-mini"
    assert extraction.choice(use_fallback=True).model == "fake-mini"
    assert policy.resolve("draft_article").fallback is None


def test_a_resolved_route_becomes_the_runtime_config_of_a_call(policy: RoutingPolicy) -> None:
    """The routing decision and the recorded runtime configuration must be the
    same object of truth: two paths would drift, and the record would describe a
    call that never happened."""
    metadata = ProviderMetadata(
        provider="fake",
        model="fake-strong",
        model_revision="2026-07-01",
        api_version="v1",
        client_version="0.1.0",
    )
    runtime = policy.resolve("extract_claims").runtime_config(metadata, RetryPolicy(version="9"))

    assert (runtime.provider, runtime.model) == ("fake", "fake-strong")
    assert runtime.temperature == 0.0
    assert runtime.seed == 7
    assert runtime.structured_output_mode is StructuredOutputMode.NATIVE_SCHEMA
    # Identity of the build that answered comes from the client, not the config.
    assert runtime.model_revision == "2026-07-01"
    assert runtime.api_version == "v1"
    assert runtime.client_version == "0.1.0"
    assert runtime.retry_policy.version == "9"


def test_the_fallback_route_produces_its_own_runtime_config(policy: RoutingPolicy) -> None:
    """The model swap must be visible in the record, not implied by the retry type."""
    resolved = policy.resolve("extract_claims")
    assert resolved.fallback is not None
    metadata = ProviderMetadata(provider="fake", model="fake-mini")
    assert resolved.runtime_config(metadata, RetryPolicy(), use_fallback=True).model == "fake-mini"


def test_asking_for_a_fallback_that_is_not_configured_is_an_error(policy: RoutingPolicy) -> None:
    resolved = policy.resolve("draft_article")
    with pytest.raises(RoutingConfigError, match="fallback"):
        resolved.runtime_config(
            ProviderMetadata(provider="fake", model="x"), RetryPolicy(), use_fallback=True
        )


def test_a_missing_or_malformed_config_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RoutingConfigError):
        RoutingPolicy.from_yaml(tmp_path / "absent.yaml")

    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\nstages: not-a-mapping\n", encoding="utf-8")
    with pytest.raises(RoutingConfigError):
        RoutingPolicy.from_yaml(broken)


def test_the_shipped_routing_config_is_versioned_and_covers_the_named_stages() -> None:
    """plan/04 names three routing shapes; the shipped config declares them.

    Later phases add entries as their stages arrive — this asserts the config
    exists, is versioned and routes what phase 04 knows about, not that the whole
    pipeline is enumerated already.
    """
    policy = default_routing_policy()

    assert policy.version
    for stage in ("extract_claims", "draft_article", "validate_article"):
        resolved = policy.resolve(stage)
        assert resolved.used_default is False, f"{stage} is not routed explicitly"
        assert resolved.primary.model


def test_unparseable_yaml_is_refused_like_any_other_bad_config(tmp_path: Path) -> None:
    path = tmp_path / "unparseable.yaml"
    path.write_text("version: '1'\nstages: [unclosed\n", encoding="utf-8")
    with pytest.raises(RoutingConfigError, match="YAML"):
        RoutingPolicy.from_yaml(path)
