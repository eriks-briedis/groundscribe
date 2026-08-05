"""LLM-contract tests: the repair ladder (phase 04).

Spec (plan/04 → Deliverables and Test-first specification):

    (1) retry with validation feedback, (2) constrained repair prompt,
    (3) configured fallback model, (4) fail stage → human intervention.
    Every attempt recorded as an ordered child ModelInvocation (phase 03).

plus the typed transport failures (timeout / provider error / rate limit), the
refusal path, and a model-requested tool call captured with
``initiated_by = model``.

The ladder is what stops a wrong-shaped response from either being accepted or
being silently retried forever, so each rung is asserted by the *record it
leaves* — the retry type, the template it used and the model it ran against —
not merely by the final value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from generation_helpers import (
    ClaimVerdict,
    build_generator,
    fake_client,
    started_stage,
)
from groundscribe.llm import (
    FakeLLMClient,
    InjectableFailure,
    LLMError,
    LLMRequest,
    LLMResponse,
    RetryPolicy,
)
from groundscribe.llm.generation import (
    GenerationFailed,
    StructuredGenerator,
    ToolCallRequested,
)
from groundscribe.provenance import models, queries
from groundscribe.provenance.enums import (
    ActorType,
    ExecutionStatus,
    InvocationOutcome,
    RetryType,
    ToolInitiator,
)
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import ToolDefinition
from groundscribe.storage.snapshot_store import SnapshotStore

VARIABLES = {"notes": "p99 fell after the cache change"}
VALID_OUTPUT = {"claim": "p99 fell", "grade": "good"}
BAD_ENUM_OUTPUT = {"claim": "p99 fell", "grade": "probably_fine"}
STAGE = "extract_claims"


@pytest.fixture
def client() -> FakeLLMClient:
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
    client: FakeLLMClient,
) -> StructuredGenerator:
    recorder, _ = execution
    return build_generator(tmp_path, recorder, {"fake": client})


async def _generate(
    generator: StructuredGenerator, stage_execution: models.StageExecution
) -> object:
    return await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )


# ---------------------------------------------------------------------------
# Rung 1 — retry with validation feedback
# ---------------------------------------------------------------------------


async def test_unparseable_json_is_repaired_by_a_feedback_retry(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """plan/04: first attempt unparseable, repair issued, second accepted —
    both recorded in order with the correct attempt types."""
    _, stage_execution = execution
    client.script_text(STAGE, '{"claim": "p99 fell", "grade": ')
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    assert result.value == ClaimVerdict.model_validate(VALID_OUTPUT)
    chain = queries.attempt_chain(result.attempts[0])
    assert [a.outcome for a in chain] == [
        InvocationOutcome.INVALID_JSON,
        InvocationOutcome.ACCEPTED,
    ]
    assert [a.retry_type for a in chain] == [None, RetryType.INVALID_SCHEMA]
    assert [a.attempt_ordinal for a in chain] == [1, 2]


async def test_the_feedback_retry_tells_the_model_what_was_wrong(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
    snapshot_store: SnapshotStore,
) -> None:
    """Rung 1 keeps the original request and *appends* the validation errors.

    Re-sending the identical prompt would be a retry in name only: nothing about
    the call changed, so nothing about the answer would either.
    """
    _, stage_execution = execution
    client.script_response(STAGE, BAD_ENUM_OUTPUT)
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    first, second = queries.attempt_chain(result.attempts[0])
    assert first.outcome is InvocationOutcome.INVALID_SCHEMA
    # The invalid-but-useful response is kept, not overwritten by its repair.
    assert first.parsed_response_snapshot is not None
    assert b"probably_fine" in snapshot_store.read(first.parsed_response_snapshot)

    repaired = queries.reconstruct_effective_request(snapshot_store, second)
    assert repaired.template_id == STAGE, "rung 1 keeps the original template"
    assert len(repaired.messages) == 3, "the feedback is an extra message, not a rewrite"
    assert "grade" in repaired.messages[-1].content
    # The client saw the same thing that was recorded.
    assert client.received_requests[-1].messages[-1].content == repaired.messages[-1].content


# ---------------------------------------------------------------------------
# Rung 2 — constrained repair prompt
# ---------------------------------------------------------------------------


async def test_the_second_rung_issues_the_constrained_repair_prompt(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
    snapshot_store: SnapshotStore,
) -> None:
    """After feedback fails, the task framing is replaced, not repeated.

    The record proves which prompt ran: template id ``repair`` with its own
    version, and a distinct retry type.
    """
    _, stage_execution = execution
    client.script_response(STAGE, BAD_ENUM_OUTPUT)
    client.script_response(STAGE, BAD_ENUM_OUTPUT)
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    chain = queries.attempt_chain(result.attempts[0])
    assert [a.retry_type for a in chain] == [
        None,
        RetryType.INVALID_SCHEMA,
        RetryType.CONTENT_REPAIR,
    ]
    third = queries.reconstruct_effective_request(snapshot_store, chain[2])
    assert (third.template_id, third.template_version) == ("repair", "v1")
    assert "probably_fine" in third.rendered_prompt, "the repair prompt shows the rejected output"


# ---------------------------------------------------------------------------
# Rung 3 — configured fallback model
# ---------------------------------------------------------------------------


async def test_repeated_failure_escalates_to_the_configured_fallback_model(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """plan/04: repeated failures on the primary escalate to the configured
    fallback, recorded as a model-fallback attempt."""
    _, stage_execution = execution
    for _ in range(3):
        client.script_response(STAGE, BAD_ENUM_OUTPUT)
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    chain = queries.attempt_chain(result.attempts[0])
    assert len(chain) == 4
    assert chain[3].retry_type is RetryType.MODEL_FALLBACK
    assert [a.model for a in chain] == ["fake-strong"] * 3 + ["fake-mini"]


# ---------------------------------------------------------------------------
# Rung 4 — fail the stage, ask for a human
# ---------------------------------------------------------------------------


async def test_exhausting_the_ladder_fails_the_stage_and_asks_for_a_human(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
    db_session: Session,
) -> None:
    """The last rung is a human, not another retry.

    Everything recorded so far survives (phase 03 → failed executions retain
    their trace), the stage is failed rather than left running, and the
    escalation is a decision naming the policy that made it.
    """
    _, stage_execution = execution
    for _ in range(5):
        client.script_response(STAGE, BAD_ENUM_OUTPUT)

    with pytest.raises(GenerationFailed) as excinfo:
        await _generate(generator, stage_execution)

    assert excinfo.value.error_type == "invalid_schema"
    assert len(excinfo.value.attempts) == 4, "one initial attempt plus three rungs"
    assert stage_execution.status is ExecutionStatus.FAILED

    decision = db_session.execute(
        select(models.DecisionRecord).where(
            models.DecisionRecord.stage_execution_id == stage_execution.id,
            models.DecisionRecord.decision_type == "repair_escalation",
        )
    ).scalar_one()
    assert decision.decided_by_type is ActorType.POLICY
    assert decision.policy_version
    assert decision.outcome == "human_intervention_required"

    events = queries.timeline(db_session, stage_execution.correlation_id)
    assert "intervention.requested" in {event.event_type for event in events}


# ---------------------------------------------------------------------------
# The failure the ladder cannot repair
# ---------------------------------------------------------------------------


async def test_a_response_cut_off_at_its_budget_is_reported_as_truncated(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """A body the provider stopped mid-value is not malformed; it is unfinished.

    The two look identical to a JSON parser and could not be more different to
    the person reading the error. "Response is not valid JSON: unterminated
    string" sends them to the prompt or the model; the truth is that the answer
    did not fit in the budget this stage was given, and the fix is a number in a
    config file.
    """
    _, stage_execution = execution
    client.script_truncated(STAGE, '{"claim": "p99 fell", "grade": "go')

    with pytest.raises(GenerationFailed) as excinfo:
        await _generate(generator, stage_execution)

    assert excinfo.value.error_type == "truncated"
    assert "output budget" in str(excinfo.value)
    assert "max_output_tokens" in str(excinfo.value)


async def test_a_truncated_response_is_not_sent_back_round_the_ladder(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """Every rung asks the same model for the same answer under the same ceiling.

    Feedback cannot help — the model did not make a mistake — and the fallback
    rung is worse than useless here: it exists to *degrade* the call, so it
    retries with a smaller budget than the one that already proved too small.
    Observed on a real run: four attempts, forty minutes, each cut off at exactly
    the cap, the last one sooner than the first.
    """
    _, stage_execution = execution
    for _ in range(5):
        client.script_truncated(STAGE, '{"claim": "p99 fell", "grade": "go')

    with pytest.raises(GenerationFailed) as excinfo:
        await _generate(generator, stage_execution)

    assert len(excinfo.value.attempts) == 1, "one attempt, then a person"
    assert excinfo.value.attempts[0].outcome is InvocationOutcome.TRUNCATED


async def test_a_refused_schema_is_not_sent_back_round_the_ladder(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """Every rung re-sends the schema the provider just refused.

    A schema outside strict mode's subset is a 400 before any model reads the
    prompt, so feedback has nobody to reach and the fallback rung only changes
    which model would have declined to see it. Observed on a real run: three
    attempts in 1.3 seconds, three identical 400s, nothing generated.

    Distinct from ``INVALID_SCHEMA``, which is a model returning the wrong
    fields — that one *is* what the ladder exists for, and is retried below.
    """
    _, stage_execution = execution
    for _ in range(5):
        client.script_failure(STAGE, InjectableFailure.SCHEMA_REJECTED)

    with pytest.raises(GenerationFailed) as excinfo:
        await _generate(generator, stage_execution)

    assert len(excinfo.value.attempts) == 1, "one attempt, then a person"
    assert excinfo.value.attempts[0].outcome is InvocationOutcome.PROVIDER_ERROR
    assert "retrying cannot help" in excinfo.value.reason
    assert "strict_schema" in excinfo.value.reason, "the reason names where the fix goes"


async def test_a_truncated_body_that_happens_to_parse_is_still_accepted(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """The stop reason is evidence, not a verdict.

    A model can stop at its ceiling having already closed the object — the
    schema is satisfied and the work is done. Refusing it because of how the
    generation ended would throw away a good answer over a flag.
    """
    _, stage_execution = execution
    client.script_truncated(STAGE, '{"claim": "p99 fell", "grade": "good"}')

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    assert result.value == ClaimVerdict.model_validate(VALID_OUTPUT)


async def test_invalid_output_is_never_returned_as_a_value(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """plan/04 exit criterion: invalid output is never accepted silently."""
    _, stage_execution = execution
    for _ in range(5):
        client.script_text(STAGE, "not json at all")

    with pytest.raises(GenerationFailed):
        await _generate(generator, stage_execution)


# ---------------------------------------------------------------------------
# Typed transport failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "outcome", "retry_type"),
    [
        (InjectableFailure.TIMEOUT, InvocationOutcome.TIMEOUT, RetryType.NETWORK),
        (InjectableFailure.RATE_LIMIT, InvocationOutcome.RATE_LIMITED, RetryType.RATE_LIMIT),
        (
            InjectableFailure.PROVIDER_ERROR,
            InvocationOutcome.PROVIDER_ERROR,
            RetryType.PROVIDER_ERROR,
        ),
    ],
)
async def test_each_transport_failure_surfaces_as_its_own_typed_retry(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
    failure: InjectableFailure,
    outcome: InvocationOutcome,
    retry_type: RetryType,
    snapshot_store: SnapshotStore,
) -> None:
    """plan/04: each surfaces as its typed retry.

    A transport failure says nothing about the prompt, so the retry re-sends the
    *same* request — advancing the content ladder here would repair a response
    that was never received.
    """
    _, stage_execution = execution
    client.script_failure(STAGE, failure)
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    chain = queries.attempt_chain(result.attempts[0])
    assert [a.outcome for a in chain] == [outcome, InvocationOutcome.ACCEPTED]
    assert chain[1].retry_type is retry_type
    retried = queries.reconstruct_effective_request(snapshot_store, chain[1])
    assert retried.template_id == STAGE
    assert len(retried.messages) == 2, "a transport retry does not add repair feedback"


async def test_a_failed_transport_attempt_is_recorded_with_its_error(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """A failed attempt with no response body still gets a record: the fact that
    the call was made is itself provenance."""
    _, stage_execution = execution
    client.script_failure(STAGE, InjectableFailure.TIMEOUT)
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    first = queries.attempt_chain(result.attempts[0])[0]
    assert first.error_message
    assert first.raw_response_snapshot_id is None
    assert first.request_snapshot_id is not None


async def test_transport_retries_are_bounded_by_the_clients_retry_policy(
    tmp_path: Path,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
) -> None:
    """On exhaustion the stage fails rather than returning garbage (plan/04)."""
    recorder, stage_execution = execution
    client = FakeLLMClient(retry_policy=RetryPolicy(version="test", max_attempts=2))
    generator = build_generator(tmp_path, recorder, {"fake": client})
    for _ in range(5):
        client.script_failure(STAGE, InjectableFailure.RATE_LIMIT)

    with pytest.raises(GenerationFailed) as excinfo:
        await _generate(generator, stage_execution)

    assert excinfo.value.error_type == "rate_limited"
    assert len(excinfo.value.attempts) == 2
    assert stage_execution.status is ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


async def test_a_refusal_is_captured_and_routed_to_a_human(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
    db_session: Session,
) -> None:
    """plan/04: a refusal is captured as a refusal state and routed to human
    intervention — not treated as a valid result, and not retried.

    Retrying a refusal is the tempting error: it converts a deliberate provider
    decision into a loop that burns budget and still ends up needing a person.
    """
    _, stage_execution = execution
    client.script_refusal(STAGE, "I can't help with that.")
    client.script_response(STAGE, VALID_OUTPUT)

    with pytest.raises(GenerationFailed) as excinfo:
        await _generate(generator, stage_execution)

    assert excinfo.value.error_type == "refused"
    assert len(excinfo.value.attempts) == 1, "a refusal is not retried"
    assert excinfo.value.attempts[0].outcome is InvocationOutcome.REFUSED
    assert excinfo.value.attempts[0].error_message == "I can't help with that."
    assert stage_execution.status is ExecutionStatus.FAILED

    events = queries.timeline(db_session, stage_execution.correlation_id)
    assert "intervention.requested" in {event.event_type for event in events}


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


async def test_a_model_requested_tool_call_is_recorded_with_its_initiator(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """plan/04: captured as a ToolInvocation with ``initiated_by = model``.

    Running the tool is stage business logic (phases 06-08), so generation
    pauses: the call is recorded, the stage is left alive, and the caller is
    handed the invocation rather than a value it could mistake for an answer.
    """
    _, stage_execution = execution
    client.script_tool_call(STAGE, name="lookup_metric", arguments={"metric": "p99"})

    with pytest.raises(ToolCallRequested) as excinfo:
        await generator.generate(
            stage_execution,
            stage=STAGE,
            template_id=STAGE,
            variables=VARIABLES,
            schema=ClaimVerdict,
            template_version="v1",
            tools=(ToolDefinition(name="lookup_metric", version="1.0.0", requires_approval=True),),
        )

    tool = excinfo.value.tool_invocations[0]
    assert tool.tool_name == "lookup_metric"
    assert tool.initiator is ToolInitiator.MODEL_SELECTED
    assert tool.approval_required is True
    assert tool.raw_args == {"metric": "p99"}
    assert tool.model_invocation_id == excinfo.value.invocation.id
    assert tool.status is ExecutionStatus.PENDING
    # A pause, not a failure: the stage is still running.
    assert stage_execution.status is ExecutionStatus.RUNNING


async def test_a_json_body_that_is_not_an_object_is_treated_as_invalid(
    generator: StructuredGenerator,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    client: FakeLLMClient,
) -> None:
    """Models return bare arrays and strings often enough to matter.

    ``json.loads`` accepts them, so without this check a list would reach
    ``model_validate`` and fail there with a message about the wrong thing.
    """
    _, stage_execution = execution
    client.script_text(STAGE, '["p99 fell"]')
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    first = queries.attempt_chain(result.attempts[0])[0]
    assert first.outcome is InvocationOutcome.INVALID_JSON
    assert first.error_message is not None
    assert "not a JSON object" in first.error_message


async def test_an_unfamiliar_provider_error_is_still_retried_as_a_provider_error(
    tmp_path: Path,
    execution: tuple[ProvenanceRecorder, models.StageExecution],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future adapter may raise an LLMError this phase has never seen.

    It must classify as *something* rather than escaping the ladder: an
    unrecorded exception would leave a stage that simply stopped, with no
    invocation explaining why.
    """

    class OddError(LLMError):
        pass

    recorder, stage_execution = execution
    client = fake_client()
    generator = build_generator(tmp_path, recorder, {"fake": client})
    calls = {"n": 0}
    original = client.complete

    async def flaky(request: LLMRequest) -> LLMResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OddError("something new went wrong")
        return await original(request)

    monkeypatch.setattr(client, "complete", flaky)
    client.script_response(STAGE, VALID_OUTPUT)

    result = await generator.generate(
        stage_execution,
        stage=STAGE,
        template_id=STAGE,
        variables=VARIABLES,
        schema=ClaimVerdict,
        template_version="v1",
    )

    chain = queries.attempt_chain(result.attempts[0])
    assert chain[0].outcome is InvocationOutcome.PROVIDER_ERROR
    assert chain[1].retry_type is RetryType.PROVIDER_ERROR
