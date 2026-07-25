"""Source-of-truth extraction (phase 06 §2).

Spec (plan/06 → *ExtractSourceTruth* and the golden/LLM-contract tests): a
structured source model — product facts, development history, classified claims
with linked evidence, publication constraints, lessons and potential arguments —
recorded with its schema version, rendered prompt, the segments included and
excluded (with reasons), the token budget and any truncation, the raw/parsed
responses, validation failures and repairs, and the final accepted model.

The golden assertion is deliberately schema-level, not prose-level: what must hold
is that a representative source produces a source model whose claims are all
classified and whose evidence points at passages that actually exist. Asserting
the model's wording would test the fake, not the pipeline.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json, golden_text, with_segment_ids
from groundscribe.domain.enums import (
    ArticleDepth,
    ArtifactType,
    ClaimClassification,
    SourceFormat,
)
from groundscribe.domain.schemas import EditorialConstraints
from groundscribe.llm import FakeLLMClient
from groundscribe.provenance.enums import ContextDisposition, InvocationOutcome
from groundscribe.stages.base import PipelineContext, StageRunner
from groundscribe.stages.errors import EvidenceError, ProviderNotPermitted
from groundscribe.stages.extraction import (
    EXTRACTION_STAGE,
    EXTRACTION_STRATEGY,
    ExtractSourceTruth,
)
from groundscribe.stages.ingestion import IngestedSource, IngestSource
from groundscribe.stages.schemas import SourceModel
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowState
from stage_helpers import SHIPPED_PROVIDER, scripted_context

CONSTRAINTS = EditorialConstraints(
    audience="senior backend engineers",
    platform="personal blog",
    depth=ArticleDepth.PRACTITIONER,
    target_length_words=1800,
    allowed_providers=(SHIPPED_PROVIDER,),
    trace_retention_consent=True,
)


#: What a model can honestly return when the budget only reached the opening
#: paragraph: one claim, citing the one passage it was shown.
BUDGETED_MODEL: dict[str, Any] = {
    "schema_version": 1,
    "summary": "A read-through cache cut p99 render latency.",
    "claims": [
        {
            "id": "c1",
            "text": "p99 latency fell from 810ms to 120ms.",
            "classification": "directly_supported_fact",
            "evidence": [{"segment_ids": ["S1"], "quote": "p99 latency fell from 810ms to 120ms"}],
            "qualification_required": True,
        }
    ],
}


async def ingest_golden(context: PipelineContext) -> IngestedSource:
    """Ingest the golden source document into ``context``'s project."""
    result = await StageRunner(context).run(
        IngestSource(
            title="Read-through caching for the render pipeline",
            text=golden_text("source.md"),
            source_format=SourceFormat.MARKDOWN,
            constraints=CONSTRAINTS,
        )
    )
    return result.value


def script(client: FakeLLMClient, payload: dict[str, Any]) -> None:
    """Queue ``payload`` as the next structured answer for the extraction stage."""
    client.script_response(EXTRACTION_STAGE, payload)


async def test_the_golden_source_extracts_into_the_expected_source_model(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 golden test: representative source → expected source-model schema."""
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(golden_json("source_model.json"), source))

    result = await StageRunner(context).run(ExtractSourceTruth(source=source))
    model = result.value

    assert isinstance(model, SourceModel)
    assert model.summary
    assert {claim.classification for claim in model.claims} >= {
        ClaimClassification.DIRECTLY_SUPPORTED_FACT,
        ClaimClassification.INTERPRETATION,
        ClaimClassification.OPINION,
        ClaimClassification.HYPOTHESIS,
    }
    assert model.product_facts and model.development_history
    assert model.publication_constraints and model.lessons and model.potential_arguments
    # The source model is the authority on what may be *stated*; extraction that
    # dropped the qualification flag would let a hypothesis be published as fact.
    assert any(claim.qualification_required for claim in model.claims)


async def test_every_claim_is_classified_and_its_evidence_points_at_real_passages(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/02 → every claim carries exactly one classification and cites segments."""
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(golden_json("source_model.json"), source))

    model = (await StageRunner(context).run(ExtractSourceTruth(source=source))).value

    known = {segment.id for segment in source.segments}
    for claim in model.claims:
        assert isinstance(claim.classification, ClaimClassification)
        cited = {sid for item in claim.evidence for sid in item.segment_ids}
        assert cited <= known, f"{claim.id} cites a passage that is not in the source"
        if claim.classification is ClaimClassification.DIRECTLY_SUPPORTED_FACT:
            assert cited, f"{claim.id} is a supported fact with nothing supporting it"


async def test_the_accepted_model_is_stored_as_the_stage_output(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The artefact is the *validated* model, and it round-trips from storage."""
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(golden_json("source_model.json"), source))

    result = await StageRunner(context).run(ExtractSourceTruth(source=source))
    execution = result.execution

    assert execution is not None
    (output,) = [artifact for artifact in execution.outputs if artifact.role == "source_model"]
    snapshot = output.snapshot
    assert snapshot.artifact_type is ArtifactType.SOURCE_MODEL
    stored = json.loads(snapshot_store.read(snapshot).decode("utf-8"))
    assert SourceModel.model_validate(stored) == result.value
    # The source document it was extracted from is recorded as the input.
    assert [artifact.snapshot_id for artifact in execution.inputs] == [source.snapshot.id]


async def test_the_stage_records_what_it_showed_the_model_and_what_it_withheld(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 → included/excluded segments with reasons, budget and truncation."""
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(golden_json("source_model.json"), source))

    result = await StageRunner(context).run(ExtractSourceTruth(source=source))
    execution = result.execution

    assert execution is not None
    (selection,) = execution.context_selections
    assert selection.strategy == EXTRACTION_STRATEGY
    assert selection.strategy_version
    assert selection.token_budget is not None
    assert [item.reference for item in selection.items] == [s.id for s in source.segments]
    assert all(item.disposition is ContextDisposition.SELECTED for item in selection.items)
    assert all(item.reason for item in selection.items)


async def test_a_source_over_the_budget_is_truncated_and_the_record_says_so(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """What the model could not see explains as much as what it could (plan/03).

    The scripted answer cites only the passages that fit, which is the whole point:
    a budgeted run can only produce evidence from what it was actually shown.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(BUDGETED_MODEL, source))

    result = await StageRunner(context).run(ExtractSourceTruth(source=source, token_budget=60))
    execution = result.execution

    assert execution is not None
    (selection,) = execution.context_selections
    dispositions = [item.disposition for item in selection.items]
    assert selection.token_budget == 60
    assert ContextDisposition.SELECTED in dispositions
    assert ContextDisposition.TRUNCATED in dispositions
    assert ContextDisposition.EXCLUDED in dispositions
    excluded = next(i for i in selection.items if i.disposition is ContextDisposition.EXCLUDED)
    assert "budget" in excluded.reason
    # What was sent matches what was recorded: the prompt stops at the budget.
    sent = model_client.last_request
    assert sent is not None
    kept = [i for i in selection.items if i.disposition is not ContextDisposition.EXCLUDED]
    assert all(item.reference in sent.prompt for item in kept)
    assert all(
        item.reference not in sent.prompt
        for item in selection.items
        if item.disposition is ContextDisposition.EXCLUDED
    )


async def test_evidence_citing_a_passage_that_does_not_exist_fails_the_stage(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A citation nobody can check is worse than no citation: it looks checked.

    Not a schema failure — the response is well-formed — so the repair ladder
    cannot see it. The stage checks it against the document it actually sent.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    payload = golden_json("source_model.json")
    payload["claims"][0]["evidence"][0]["segment_ids"] = ["S999"]
    script(model_client, with_segment_ids(payload, source))

    with pytest.raises(EvidenceError, match="S999"):
        await StageRunner(context).run(ExtractSourceTruth(source=source))

    assert context.engine.state is WorkflowState.SOURCE_MODEL_EXTRACTING


async def test_material_is_not_sent_to_a_provider_the_project_has_not_allowed(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/00 → local-first, with *visible* data flow to external providers.

    The check runs before the call, not after: a refusal that arrives once the
    material has already crossed the wire has not protected anything.
    """
    constraints = CONSTRAINTS.model_copy(update={"allowed_providers": ("anthropic",)})
    context, model_client = scripted_context(db_session, snapshot_store, constraints=constraints)
    source = await ingest_golden(context)

    with pytest.raises(ProviderNotPermitted, match=SHIPPED_PROVIDER):
        await StageRunner(context).run(ExtractSourceTruth(source=source))

    assert model_client.received_requests == ()


async def test_an_invalid_classification_is_repaired_by_the_phase_04_ladder(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 LLM-contract: extraction handles invalid-schema → repair correctly."""
    context, model_client = scripted_context(db_session, snapshot_store)
    source = await ingest_golden(context)
    good = with_segment_ids(golden_json("source_model.json"), source)
    broken = json.loads(json.dumps(good))
    broken["claims"][0]["classification"] = "probably_true"
    script(model_client, broken)
    script(model_client, good)

    result = await StageRunner(context).run(ExtractSourceTruth(source=source))
    execution = result.execution

    assert execution is not None
    outcomes = [call.outcome for call in execution.model_invocations]
    assert outcomes == [InvocationOutcome.INVALID_SCHEMA, InvocationOutcome.ACCEPTED]
    assert result.value.claims[0].classification is ClaimClassification.DIRECTLY_SUPPORTED_FACT
