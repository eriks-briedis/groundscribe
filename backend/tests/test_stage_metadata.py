"""The full stage-execution record (phase 06 provenance test).

Spec (plan/06 → *Stage execution metadata*): each stage records the full field
set — stage name, input/output snapshot ids, prompt / rubric / schema versions,
model and params, usage, cost, time, retries, tool calls, the routing decision,
and the stage implementation version.

This module is the one test that reads a completed execution the way a person
auditing a run would: start from the execution row and ask whether each of those
facts can be recovered without knowing which stage produced it. Most of them are
guaranteed by phases 03 and 04; the point of asserting them together is that a
*stage* is where they all have to arrive at once, and a field nobody ever asks for
in combination is a field that quietly stops being written.

Rubric versions are the one item absent by design: nothing is scored until phase
08, and `EvaluationRun` already carries `rubric_version` for when it is.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from golden import golden_json, with_segment_ids
from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance.enums import (
    ActorType,
    ExecutionStatus,
    InvocationOutcome,
    RetryType,
    ToolInitiator,
)
from groundscribe.provenance.schemas import TokenUsage
from groundscribe.stages.base import StageRunner
from groundscribe.stages.extraction import EXTRACTION_STAGE, ExtractSourceTruth
from groundscribe.storage.snapshot_store import SnapshotStore
from stage_helpers import SHIPPED_PROVIDER, scripted_context
from test_extraction import ingest_golden

USAGE = TokenUsage(input_tokens=3120, output_tokens=880, cost_usd=0.0042)
REPAIR_USAGE = TokenUsage(input_tokens=3400, output_tokens=910, cost_usd=0.0051)


async def test_a_stage_execution_records_the_whole_field_set(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Every fact plan/06 lists is recoverable from the stored execution."""
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    good = with_segment_ids(golden_json("source_model.json"), source)
    broken = json.loads(json.dumps(good))
    broken["claims"][0]["classification"] = "probably_true"
    model_client.script_response(EXTRACTION_STAGE, broken, usage=USAGE)
    model_client.script_response(EXTRACTION_STAGE, good, usage=REPAIR_USAGE)

    result = await StageRunner(context).run(ExtractSourceTruth(source=source))
    execution = result.execution
    assert execution is not None

    # Stage name, implementation version, status and time.
    assert execution.stage == EXTRACTION_STAGE
    assert execution.impl_version == ExtractSourceTruth.impl_version
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.completed_at is not None
    assert execution.started_at <= execution.completed_at

    # Input and output snapshot ids.
    assert [artifact.snapshot_id for artifact in execution.inputs] == [source.snapshot.id]
    assert [artifact.snapshot.artifact_type for artifact in execution.outputs] == [
        ArtifactType.SOURCE_MODEL
    ]

    # Retries: ordered, typed child invocations rather than a count.
    first, second = execution.model_invocations
    assert first.outcome is InvocationOutcome.INVALID_SCHEMA
    assert second.outcome is InvocationOutcome.ACCEPTED
    assert second.parent_invocation_id == first.id
    assert second.attempt_ordinal == 2
    assert second.retry_type is RetryType.INVALID_SCHEMA

    # Model, provider and the params that shaped the call.
    assert second.provider == SHIPPED_PROVIDER
    assert second.model
    assert second.request_snapshot is not None
    request = json.loads(snapshot_store.read(second.request_snapshot).decode("utf-8"))
    assert request["provider_config"]["temperature"] == 0.0
    assert request["provider_config"]["seed"]
    assert request["provider_config"]["structured_output_mode"]

    # Prompt and schema versions.
    assert second.template_id == EXTRACTION_STAGE
    assert second.template_version
    assert request["template_version"] == second.template_version
    assert request["output_schema_version"] == 1

    # Usage and cost, per attempt — including the attempt that failed.
    assert (first.input_tokens, first.output_tokens) == (3120, 880)
    assert first.cost_usd == 0.0042
    assert (second.input_tokens, second.output_tokens) == (3400, 910)
    assert second.cost_usd == 0.0051

    # Timing per invocation.
    assert second.completed_at is not None

    # The routing decision that chose the model, naming its policy version.
    (routing,) = [
        record for record in execution.decision_records if record.decision_type == "model_routing"
    ]
    assert routing.decided_by_type is ActorType.POLICY
    assert routing.policy_version
    assert routing.outcome == f"{second.provider}/{second.model}"


async def test_the_stage_result_totals_what_the_attempts_consumed(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Cost is answerable per stage without summing invocations by hand.

    The failed attempt counts. A stage that reported only the accepted call would
    under-report exactly the runs that cost the most — the ones that needed
    repairing.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    good = with_segment_ids(golden_json("source_model.json"), source)
    broken = json.loads(json.dumps(good))
    broken["claims"][0]["classification"] = "probably_true"
    model_client.script_response(EXTRACTION_STAGE, broken, usage=USAGE)
    model_client.script_response(EXTRACTION_STAGE, good, usage=REPAIR_USAGE)

    result = await StageRunner(context).run(ExtractSourceTruth(source=source))

    assert result.usage.input_tokens == 3120 + 3400
    assert result.usage.output_tokens == 880 + 910
    assert result.usage.cost_usd is not None
    assert round(result.usage.cost_usd, 6) == 0.0093
    assert result.usage.total_tokens == 8310


async def test_an_unreported_cost_stays_unknown_rather_than_zero(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Not every provider reports cost, and 0.0 is a different claim from "unknown"."""
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    model_client.script_response(
        EXTRACTION_STAGE,
        with_segment_ids(golden_json("source_model.json"), source),
        usage=TokenUsage(input_tokens=1000, output_tokens=200),
    )

    result = await StageRunner(context).run(ExtractSourceTruth(source=source))
    execution = result.execution

    assert execution is not None
    assert execution.model_invocations[0].cost_usd is None
    assert result.usage.cost_usd is None
    assert result.usage.input_tokens == 1000


async def test_a_tool_call_is_recorded_against_the_stage_that_provoked_it(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Tool calls are part of the stage record, including the ones that stopped it.

    The stage has no tool registry in this phase, so a model asking for one halts
    generation — but the request is recorded first, with the arguments the model
    supplied, because "why did this stage stop?" is answerable only from the call
    it tried to make.
    """
    from groundscribe.llm.generation import ToolCallRequested

    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    model_client.script_tool_call(
        EXTRACTION_STAGE, name="read_repository", arguments={"path": "src/render.py"}
    )

    try:
        await StageRunner(context).run(ExtractSourceTruth(source=source))
    except ToolCallRequested as exc:
        assert exc.stage == EXTRACTION_STAGE
    else:  # pragma: no cover - the fake was scripted to request a tool
        raise AssertionError("the scripted tool call did not stop generation")

    execution = next(
        row for row in context.engine.run.stage_executions if row.stage == EXTRACTION_STAGE
    )
    (tool,) = execution.tool_invocations
    assert tool.tool_name == "read_repository"
    assert tool.initiator is ToolInitiator.MODEL_SELECTED
    assert tool.raw_args == {"path": "src/render.py"}
    assert tool.model_invocation_id == execution.model_invocations[0].id
    # The stage failed rather than continuing without the tool's answer.
    assert execution.status is ExecutionStatus.FAILED
