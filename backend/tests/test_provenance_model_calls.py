"""Model-invocation provenance tests (phase 03).

Spec (plan/03 → Test-first specification):

- **Effective request reconstructable** — from stored records the exact request
  sent to a model can be rebuilt: template version, rendered prompt, message
  sequence, tool definitions, structured-output schema, provider config.
- **Raw ↔ parsed ↔ validated linkage** — each stays linked, and a response that
  is useful but fails schema validation is preserved alongside its repaired
  successor.
- **Retry ordering** — attempts are ordered, typed child invocations
  (attempt 1 invalid JSON → attempt 2 invalid enum → attempt 3 accepted), not a
  bare count.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance import models, queries
from groundscribe.provenance.enums import InvocationOutcome, RetryType
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.provenance.schemas import EffectiveRequest, Message, ToolDefinition
from groundscribe.storage.snapshot_store import SnapshotStore
from provenance_helpers import make_recorder, seed_project

REQUEST = EffectiveRequest(
    template_id="extract_claims",
    template_version="2.1.0",
    rendered_prompt="Extract every factual claim from the notes below.\n\nnotes: p99 fell",
    messages=[
        Message(role="system", content="You extract claims. Be literal."),
        Message(role="user", content="notes: p99 fell"),
    ],
    tool_definitions=[
        ToolDefinition(name="lookup_metric", version="1.0.0", parameters={"type": "object"})
    ],
    output_schema={"type": "object", "required": ["claims"]},
    provider_config={"temperature": 0.0, "max_tokens": 2048},
)


@pytest.fixture
def recorder(db_session: Session, snapshot_store: SnapshotStore) -> ProvenanceRecorder:
    seed_project(db_session)
    return make_recorder(db_session, snapshot_store)


def _started_stage(recorder: ProvenanceRecorder) -> models.StageExecution:
    run = recorder.start_run(project_id="p1")
    return recorder.start_stage(run, stage="extract_claims")


def test_the_effective_request_can_be_rebuilt_from_stored_records(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """Everything needed to reissue the call survives, not just a prompt string.

    Reconstruction is what makes a provenance record actionable: without the
    message sequence, tool definitions and output schema, "which prompt produced
    this?" cannot be answered, only guessed at.
    """
    execution = _started_stage(recorder)
    invocation = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response={"claims": []},
    )

    rebuilt = queries.reconstruct_effective_request(snapshot_store, invocation)

    assert rebuilt.template_id == "extract_claims"
    assert rebuilt.template_version == "2.1.0"
    assert rebuilt.rendered_prompt == REQUEST.rendered_prompt
    assert [(m.role, m.content) for m in rebuilt.messages] == [
        ("system", "You extract claims. Be literal."),
        ("user", "notes: p99 fell"),
    ]
    assert [(t.name, t.version) for t in rebuilt.tool_definitions] == [("lookup_metric", "1.0.0")]
    assert rebuilt.output_schema == {"type": "object", "required": ["claims"]}
    assert rebuilt.provider_config == {"temperature": 0.0, "max_tokens": 2048}
    # What was persisted is the redacted form; the record says so rather than
    # implying it is byte-identical to what crossed the wire.
    assert rebuilt.redacted is True


def test_template_identity_is_queryable_without_opening_the_snapshot(
    recorder: ProvenanceRecorder,
) -> None:
    """Template id/version are columns too, so "which prompt version?" is a query."""
    execution = _started_stage(recorder)
    invocation = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
    )
    assert (invocation.template_id, invocation.template_version) == ("extract_claims", "2.1.0")


def test_raw_parsed_and_validated_responses_are_three_distinct_snapshots(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """Each response form is stored separately and typed, never overwritten in place."""
    execution = _started_stage(recorder)
    invocation = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        raw_response='{"claims":[{"text":"p99 fell","kind":"fact"}]}',
        parsed_response={"claims": [{"text": "p99 fell", "kind": "fact"}]},
        validated_response={"claims": [{"text": "p99 fell", "classification": "user_observation"}]},
    )

    ids = {
        invocation.request_snapshot_id,
        invocation.raw_response_snapshot_id,
        invocation.parsed_response_snapshot_id,
        invocation.validated_response_snapshot_id,
    }
    assert len(ids) == 4, "response forms must not share a snapshot row"

    assert invocation.raw_response_snapshot is not None
    assert invocation.raw_response_snapshot.artifact_type is ArtifactType.RAW_RESPONSE
    assert invocation.parsed_response_snapshot is not None
    assert invocation.parsed_response_snapshot.artifact_type is ArtifactType.PARSED_RESPONSE
    assert invocation.validated_response_snapshot is not None
    assert invocation.validated_response_snapshot.artifact_type is ArtifactType.VALIDATED_RESPONSE
    # The phase-02 integrity check applies unchanged at the provenance boundary.
    assert snapshot_store.verify(invocation.raw_response_snapshot) is True


def test_a_useful_but_invalid_response_survives_next_to_its_repair(
    recorder: ProvenanceRecorder, snapshot_store: SnapshotStore
) -> None:
    """The attempt that failed validation keeps its parsed content and its link."""
    execution = _started_stage(recorder)
    failed = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.INVALID_SCHEMA,
        raw_response='{"claims":[{"text":"p99 fell","kind":"guess"}]}',
        parsed_response={"claims": [{"text": "p99 fell", "kind": "guess"}]},
    )
    repaired = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        parent=failed,
        retry_type=RetryType.CONTENT_REPAIR,
        parsed_response={"claims": [{"text": "p99 fell", "classification": "user_observation"}]},
        validated_response={"claims": [{"text": "p99 fell", "classification": "user_observation"}]},
    )

    # The failed attempt is not overwritten: its parsed body is still readable.
    assert failed.parsed_response_snapshot is not None
    assert failed.validated_response_snapshot_id is None
    body = snapshot_store.read(failed.parsed_response_snapshot)
    assert b"guess" in body
    # ...and it is linked to the attempt that repaired it, in both directions.
    assert repaired.parent_invocation_id == failed.id
    assert [a.id for a in failed.attempts] == [repaired.id]


def test_repair_attempts_are_ordered_and_each_says_why_it_exists(
    recorder: ProvenanceRecorder,
) -> None:
    """Attempt 1 invalid JSON → attempt 2 invalid enum → attempt 3 accepted.

    The chain records order *and* cause. A retry counter could say "3 attempts"
    and could not distinguish this sequence from three rate-limit retries, which
    calls for a completely different fix.
    """
    execution = _started_stage(recorder)
    first = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.INVALID_JSON,
        raw_response="{claims: [",
    )
    second = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.INVALID_SCHEMA,
        parent=first,
        retry_type=RetryType.INVALID_SCHEMA,
        parsed_response={"claims": [{"kind": "wildly_wrong_enum"}]},
    )
    third = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        parent=second,
        retry_type=RetryType.CONTENT_REPAIR,
        validated_response={"claims": []},
    )

    chain = queries.attempt_chain(first)
    assert [a.id for a in chain] == [first.id, second.id, third.id]
    assert [a.attempt_ordinal for a in chain] == [1, 2, 3]
    assert [a.retry_type for a in chain] == [
        None,
        RetryType.INVALID_SCHEMA,
        RetryType.CONTENT_REPAIR,
    ]
    assert [a.outcome for a in chain] == [
        InvocationOutcome.INVALID_JSON,
        InvocationOutcome.INVALID_SCHEMA,
        InvocationOutcome.ACCEPTED,
    ]


def test_a_follow_up_attempt_must_state_its_retry_type(recorder: ProvenanceRecorder) -> None:
    """Chaining without a cause would reintroduce the bare-count modelling."""
    execution = _started_stage(recorder)
    first = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.TIMEOUT,
    )
    with pytest.raises(ValueError, match="retry_type"):
        recorder.record_model_invocation(
            execution,
            request=REQUEST,
            provider="fake",
            model="fake-1",
            outcome=InvocationOutcome.ACCEPTED,
            parent=first,
        )


def test_a_first_attempt_cannot_claim_a_retry_type(recorder: ProvenanceRecorder) -> None:
    """A retry with nothing to retry is a contradiction; reject it at the boundary."""
    execution = _started_stage(recorder)
    with pytest.raises(ValueError, match="parent"):
        recorder.record_model_invocation(
            execution,
            request=REQUEST,
            provider="fake",
            model="fake-1",
            outcome=InvocationOutcome.ACCEPTED,
            retry_type=RetryType.RATE_LIMIT,
        )


def test_model_fallback_is_recorded_as_a_typed_attempt(recorder: ProvenanceRecorder) -> None:
    """Switching model mid-chain is a retry type, so the provider swap is visible."""
    execution = _started_stage(recorder)
    first = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.PROVIDER_ERROR,
    )
    fallback = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-mini",
        outcome=InvocationOutcome.ACCEPTED,
        parent=first,
        retry_type=RetryType.MODEL_FALLBACK,
    )
    assert fallback.model == "fake-mini"
    assert fallback.retry_type is RetryType.MODEL_FALLBACK
    assert queries.attempt_chain(first)[-1].id == fallback.id


def test_identical_requests_across_attempts_share_one_blob(recorder: ProvenanceRecorder) -> None:
    """Content addressing pays off exactly here: a retry resends the same request."""
    execution = _started_stage(recorder)
    first = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.TIMEOUT,
    )
    second = recorder.record_model_invocation(
        execution,
        request=REQUEST,
        provider="fake",
        model="fake-1",
        outcome=InvocationOutcome.ACCEPTED,
        parent=first,
        retry_type=RetryType.NETWORK,
    )
    assert first.request_snapshot is not None
    assert second.request_snapshot is not None
    assert first.request_snapshot_id != second.request_snapshot_id
    assert first.request_snapshot.content_hash == second.request_snapshot.content_hash
