"""LLM-contract tests: structured generation (phase 04).

Spec (plan/04 → Test-first specification), the non-repair half:

- **Valid structured output** parses and validates against the Pydantic schema,
  with one accepted invocation recorded;
- **Prompt render + version capture** — the effective request records the
  template version, inputs, rendered text and message sequence, and changing the
  version changes what is recorded;
- **Model routing** — each stage resolves to its configured model, and an
  override is captured in the execution record;
- runtime configuration is captured on every invocation.

Everything runs against the deterministic fake (plan/04 non-goal: no network).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from generation_helpers import (
    ClaimVerdict,
    Grade,
    build_generator,
    fake_client,
    started_stage,
)
from groundscribe.domain.enums import ArtifactType
from groundscribe.llm import LLMClient
from groundscribe.llm.generation import GenerationError, StructuredGenerator
from groundscribe.llm.routing import RouteOverride
from groundscribe.provenance import models, queries
from groundscribe.provenance.enums import ActorType, InvocationOutcome
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore

VARIABLES = {"notes": "p99 fell after the cache change"}
VALID_OUTPUT = {"claim": "p99 fell after the cache change", "grade": "good"}


@pytest.fixture
def client() -> LLMClient:
    return fake_client()


@pytest.fixture
def execution(
    db_session: Session, snapshot_store: SnapshotStore
) -> tuple[ProvenanceRecorder, models.StageExecution]:
    return started_stage(db_session, snapshot_store)


@pytest.fixture
def generator(
    tmp_path: Path,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
) -> StructuredGenerator:
    recorder, _ = execution
    return build_generator(tmp_path, recorder, {"fake": client})


async def test_valid_structured_output_is_validated_and_recorded_once(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
) -> None:
    """One accepted invocation, and a typed value — not a dict.

    Handing callers a validated Pydantic object rather than raw JSON is what
    stops "the model returned something roughly right" from propagating into the
    editorial artefacts (plan/00 → structured outputs where decisions matter).
    """
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    result = await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
    )

    assert result.value == ClaimVerdict(claim=VALID_OUTPUT["claim"], grade=Grade.GOOD)
    assert len(result.attempts) == 1
    assert result.invocation.outcome is InvocationOutcome.ACCEPTED
    assert result.invocation.retry_type is None
    assert result.invocation.attempt_ordinal == 1


async def test_the_three_response_forms_are_stored_separately(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
) -> None:
    """Phase 03 stores raw/parsed/validated apart; generation must fill all three."""
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    result = await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
    )

    invocation = result.invocation
    assert invocation.raw_response_snapshot is not None
    assert invocation.raw_response_snapshot.artifact_type is ArtifactType.RAW_RESPONSE
    assert invocation.parsed_response_snapshot is not None
    assert invocation.validated_response_snapshot is not None


async def test_the_effective_request_records_the_template_version_and_inputs(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
    snapshot_store: SnapshotStore,
) -> None:
    """plan/04: render captures id, version, inputs, prompt, messages, schema."""
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    result = await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    request = queries.reconstruct_effective_request(snapshot_store, result.invocation)
    assert (request.template_id, request.template_version) == ("extract_claims", "v1")
    assert request.template_variables == VARIABLES
    assert "p99 fell" in request.rendered_prompt
    assert [m.role for m in request.messages] == ["system", "user"]
    assert request.output_schema is not None
    assert "grade" in request.output_schema["properties"]
    assert request.output_schema_version == 1


async def test_changing_the_template_version_changes_the_recorded_version(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
) -> None:
    """plan/04 test-first spec, stated literally."""
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    first = await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )
    second = await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v2",
    )

    assert first.invocation.template_version == "v1"
    assert second.invocation.template_version == "v2"


async def test_the_client_receives_the_rendered_prompt_and_message_sequence(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
) -> None:
    """The recorded request must be the *sent* request, so assert on both ends."""
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    sent = client.last_request  # type: ignore[attr-defined]
    assert sent is not None
    assert "p99 fell" in sent.prompt
    assert [m.role for m in sent.messages] == ["system", "user"]
    assert sent.schema_name == "ClaimVerdict"
    assert sent.output_schema is not None


async def test_each_stage_resolves_to_its_configured_model(
    tmp_path: Path,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
) -> None:
    """plan/04 test-first spec: routing decides the model, per stage."""
    recorder, stage_execution = execution
    generator = build_generator(tmp_path, recorder, {"fake": client})
    client.script_response("draft_article", VALID_OUTPUT)  # type: ignore[attr-defined]

    result = await generator.generate(
        stage_execution,
        stage="draft_article",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
    )

    assert result.route.primary.model == "fake-prose"
    assert result.invocation.model == "fake-prose"


async def test_the_routing_decision_is_recorded_against_its_policy(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
    db_session: Session,
) -> None:
    """Every model choice is a decision, and phase 03 refuses unattributed ones."""
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
    )

    decision = db_session.execute(
        select(models.DecisionRecord).where(
            models.DecisionRecord.stage_execution_id == stage_execution.id,
            models.DecisionRecord.decision_type == "model_routing",
        )
    ).scalar_one()
    assert decision.decided_by_type is ActorType.POLICY
    assert decision.policy_version == "test-1"
    assert decision.outcome == "fake/fake-strong"


async def test_a_routing_override_is_captured_in_the_execution_record(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
    db_session: Session,
) -> None:
    """plan/04 test-first spec: *an override is captured in the execution record*.

    Attributed to the human who asked, not to the policy they overrode — the
    policy did not make this choice, and recording it as though it did would
    misdirect the next person asking why this run differs.
    """
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    result = await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
        override=RouteOverride(model="fake-mini", requested_by="ada", reason="cost check"),
    )

    assert result.invocation.model == "fake-mini"
    decision = db_session.execute(
        select(models.DecisionRecord).where(
            models.DecisionRecord.stage_execution_id == stage_execution.id,
            models.DecisionRecord.decision_type == "model_routing",
        )
    ).scalar_one()
    assert decision.decided_by == "ada"
    assert decision.decided_by_type is ActorType.USER
    assert decision.inputs["overrides"] == {"model": "fake-mini"}
    assert decision.rationale == "cost check"


async def test_the_full_runtime_configuration_is_captured_on_the_invocation(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
    snapshot_store: SnapshotStore,
) -> None:
    """plan/04 exit criterion: every setting that could have changed the output.

    Asserted as an exact key set rather than a spot check: the failure this
    guards against is a field quietly going missing, which no other test would
    notice until a replay disagreed with the original run.
    """
    _, stage_execution = execution
    client.script_response("extract_claims", VALID_OUTPUT)  # type: ignore[attr-defined]

    result = await generator.generate(
        stage_execution,
        stage="extract_claims",
        template_id="extract_claims",
        variables=VARIABLES,
        schema=ClaimVerdict,
    )

    config = queries.reconstruct_effective_request(
        snapshot_store, result.invocation
    ).provider_config
    assert set(config) == {
        "provider",
        "model",
        "model_revision",
        "temperature",
        "top_p",
        "seed",
        "max_output_tokens",
        "reasoning_effort",
        "structured_output_mode",
        "tool_choice",
        "stop_sequences",
        "api_version",
        "client_version",
        "timeout_seconds",
        "retry_policy",
    }
    assert config["model"] == "fake-strong"
    assert config["seed"] == 7
    assert config["stop_sequences"] == ["<<END>>"]
    # The client that answered, not the config, supplies the build identity.
    assert config["client_version"] == "fake-client-1"
    assert config["retry_policy"]["version"]


async def test_a_route_to_a_provider_with_no_client_fails_loudly(
    tmp_path: Path,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: LLMClient,
) -> None:
    """Routing names a provider; something has to hold the clients.

    Failing here is the honest outcome: silently substituting whichever client
    happens to be available would make the recorded provider a fiction.
    """
    recorder, stage_execution = execution
    generator = build_generator(tmp_path, recorder, {"other": client})

    with pytest.raises(GenerationError, match="fake"):
        await generator.generate(
            stage_execution,
            stage="extract_claims",
            template_id="extract_claims",
            variables=VARIABLES,
            schema=ClaimVerdict,
        )
