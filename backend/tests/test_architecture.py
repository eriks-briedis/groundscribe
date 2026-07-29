"""Content-architecture proposal (phase 06 §4).

Spec (plan/06 → *ProposeContentArchitecture* and the golden architecture test):
analyse distinct arguments, evidence per argument, overlap, standalone-ability,
reader knowledge, platform constraints, competing theses and thin-content risk;
output proposed article model(s), series-level considerations, and a structured
decision record naming the selected architecture, its supporting claims, the
alternatives *and why they were rejected*, a confidence, the uncertainties, and
the policy version.

The exit criterion this phase is judged on — "records alternatives-considered and
confidence" — is enforced in the schema rather than asserted in the stage. A
proposal that lists no rejected alternative has not chosen anything; it has
reported the first idea it had, and the difference is invisible unless something
insists on it.

Claim references are checked against the source model for the same reason
extraction checks its segment citations: an architecture arguing from a claim the
source model does not contain is a structurally valid proposal for an article that
cannot honestly be written.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json, with_segment_ids
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.llm import FakeLLMClient
from groundscribe.provenance.enums import ActorType, InvocationOutcome
from groundscribe.stages.architecture import (
    ARCHITECTURE_STAGE,
    ArchitectureOutcome,
    ProposeContentArchitecture,
)
from groundscribe.stages.base import PipelineContext, StageResult, StageRunner
from groundscribe.stages.errors import EvidenceError
from groundscribe.stages.extraction import ExtractSourceTruth
from groundscribe.stages.schemas import ArchitectureProposal, RiskLevel
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from stage_helpers import scripted_context
from test_extraction import ingest_golden, script


async def extract(context: PipelineContext, model_client: FakeLLMClient) -> StageResult[Any]:
    """Ingest and extract the golden source, leaving the model ready for architecture."""
    source = await ingest_golden(context)
    script(model_client, with_segment_ids(golden_json("source_model.json"), source))
    extracted = await StageRunner(context).run(ExtractSourceTruth(source=source))
    # Nothing blocking was found, so extraction completes and the model is ready.
    context.engine.apply(WorkflowAction.COMPLETE_EXTRACTION)
    return extracted


async def propose(
    context: PipelineContext,
    model_client: FakeLLMClient,
    payload: dict[str, Any] | None = None,
) -> StageResult[ArchitectureOutcome]:
    """Run the whole chain up to and including the architecture proposal."""
    extracted = await extract(context, model_client)
    model_client.script_response(
        ARCHITECTURE_STAGE, payload if payload is not None else golden_json("architecture.json")
    )
    return await StageRunner(context).run(
        ProposeContentArchitecture(
            source_model=extracted.value, source_model_snapshot=extracted.outputs[0]
        )
    )


async def test_the_golden_source_model_proposes_the_expected_architecture(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 golden test: source model → architecture schema with a decision record."""
    context, model_client = scripted_context(db_session, snapshot_store)

    proposal = (await propose(context, model_client)).value.proposal

    assert isinstance(proposal, ArchitectureProposal)
    assert len(proposal.articles) == 2
    first = proposal.articles[0]
    assert first.thesis and first.evidence_summary and first.reader_knowledge_assumed
    assert first.standalone is True
    assert first.thin_content_risk is RiskLevel.LOW
    assert proposal.articles[1].overlaps_with == ("a1",)
    assert proposal.competing_theses
    assert proposal.series.is_series and proposal.series.reading_order == ("a1", "a2")


async def test_the_decision_records_its_alternatives_and_confidence(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 exit criterion: alternatives-considered and confidence are recorded."""
    context, model_client = scripted_context(db_session, snapshot_store)

    proposed = await propose(context, model_client)
    decision = proposed.value.proposal.decision

    assert decision.selected == "a1"
    assert 0.0 <= decision.confidence <= 1.0
    assert len(decision.alternatives_considered) == 2
    assert all(alternative.reason_rejected for alternative in decision.alternatives_considered)
    assert decision.uncertainties

    # And the same facts reach provenance, attributed to the stage's policy version.
    execution = proposed.execution
    assert execution is not None
    (record,) = [
        row for row in execution.decision_records if row.decision_type == "content_architecture"
    ]
    assert record.decided_by_type is ActorType.POLICY
    assert record.policy_version
    assert record.outcome == "a1"
    assert record.inputs["confidence"] == pytest.approx(0.72)
    assert len(record.inputs["alternatives_considered"]) == 2


def test_a_proposal_with_no_rejected_alternative_is_refused() -> None:
    """Choosing means rejecting; a proposal that rejected nothing chose nothing."""
    payload = golden_json("architecture.json")
    payload["decision"]["alternatives_considered"] = []

    with pytest.raises(ValueError, match="alternative"):
        ArchitectureProposal.model_validate(payload)


def test_an_alternative_with_no_reason_is_refused() -> None:
    """Considered-and-dropped, with no reason, is not a record of anything."""
    payload = golden_json("architecture.json")
    payload["decision"]["alternatives_considered"][0]["reason_rejected"] = "  "

    with pytest.raises(ValueError, match="why it was rejected"):
        ArchitectureProposal.model_validate(payload)


def test_a_series_must_order_every_article_it_contains() -> None:
    """A series whose reading order omits an article has not been sequenced."""
    payload = golden_json("architecture.json")
    payload["series"]["reading_order"] = ["a1"]

    with pytest.raises(ValueError, match="reading order"):
        ArchitectureProposal.model_validate(payload)


def test_the_selected_architecture_must_be_one_of_the_proposed_articles() -> None:
    """A decision naming nothing that exists cannot be acted on."""
    payload = golden_json("architecture.json")
    payload["decision"]["selected"] = "a9"

    with pytest.raises(ValueError, match="selected"):
        ArchitectureProposal.model_validate(payload)


async def test_an_architecture_arguing_from_an_unknown_claim_fails_the_stage(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A structurally valid proposal for an article that cannot honestly be written."""
    context, model_client = scripted_context(db_session, snapshot_store)
    payload = golden_json("architecture.json")
    payload["articles"][0]["supporting_claim_ids"] = ["c99"]

    with pytest.raises(EvidenceError, match="c99"):
        await propose(context, model_client, payload)

    assert context.engine.state is WorkflowState.ARCHITECTURE_PROPOSING


async def test_the_proposal_is_stored_with_its_concepts_and_parks_for_review(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The architecture is an artefact; its articles are rows; a person approves it."""
    context, model_client = scripted_context(db_session, snapshot_store)

    proposed = await propose(context, model_client)
    execution = proposed.execution

    assert execution is not None
    assert context.engine.state is WorkflowState.ARCHITECTURE_REVIEW_REQUIRED
    assert context.engine.is_paused

    (snapshot,) = [
        s for s in proposed.outputs if s.artifact_type is ArtifactType.CONTENT_ARCHITECTURE
    ]
    stored = json.loads(snapshot_store.read(snapshot).decode("utf-8"))
    assert ArchitectureProposal.model_validate(stored) == proposed.value.proposal

    architecture = proposed.value.architecture
    assert architecture.created_by_execution_id == execution.id
    assert architecture.snapshot_id == snapshot.id
    concepts = list(db_session.execute(select(domain_models.ArticleConcept)).scalars())
    assert [concept.ref for concept in concepts] == ["a1", "a2"]
    assert all(concept.architecture_id == architecture.id for concept in concepts)
    assert all(concept.thesis for concept in concepts)
    # The source model it was derived from is the recorded input.
    assert [artifact.role for artifact in execution.inputs] == ["source_model"]


async def test_an_invalid_risk_level_is_repaired_by_the_phase_04_ladder(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 LLM-contract: architecture handles invalid-schema → repair correctly."""
    context, model_client = scripted_context(db_session, snapshot_store)
    extracted = await extract(context, model_client)

    broken = golden_json("architecture.json")
    broken["articles"][0]["thin_content_risk"] = "not_really_a_risk"
    model_client.script_response(ARCHITECTURE_STAGE, broken)
    model_client.script_response(ARCHITECTURE_STAGE, golden_json("architecture.json"))

    proposed = await StageRunner(context).run(
        ProposeContentArchitecture(
            source_model=extracted.value, source_model_snapshot=extracted.outputs[0]
        )
    )

    execution = proposed.execution
    assert execution is not None
    assert [call.outcome for call in execution.model_invocations] == [
        InvocationOutcome.INVALID_SCHEMA,
        InvocationOutcome.ACCEPTED,
    ]
    assert proposed.value.proposal.articles[0].thin_content_risk is RiskLevel.LOW


async def test_a_second_proposal_may_reuse_a_label_without_colliding(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The model names its articles; two proposals may name them the same thing.

    The same shape as phase 06's gap labels and phase 07's review findings, found
    the same way: phase 12 replays a stage, and a model asked to shape the same
    source again hands back ``a1`` and ``a2``. Keying the concept row on that
    label made the second proposal a primary-key collision — an ``IntegrityError``
    out of a job, which names neither the cause nor anything a person can do.

    So the row keeps its own id and remembers the model's label as ``ref``. The
    id is what the API addresses an article by, and it must not be a name a model
    chose.
    """
    context, model_client = scripted_context(db_session, snapshot_store)
    extracted = await extract(context, model_client)
    stage = ProposeContentArchitecture(
        source_model=extracted.value, source_model_snapshot=extracted.outputs[0]
    )

    model_client.script_response(ARCHITECTURE_STAGE, golden_json("architecture.json"))
    first = await StageRunner(context).run(stage)
    # The same source, shaped again, with the model reusing its own labels. Run
    # as a replay does — no transitions — because the run has already parked for
    # review and re-proposing is not a second trip round the machine.
    model_client.script_response(ARCHITECTURE_STAGE, golden_json("architecture.json"))
    second = await StageRunner(context).run(stage, enter=False, transitions=False)

    concepts = list(db_session.execute(select(domain_models.ArticleConcept)).scalars())
    assert [concept.ref for concept in concepts] == ["a1", "a2", "a1", "a2"]
    assert len({concept.id for concept in concepts}) == 4, "four rows, not two written over"
    assert first.value.concept("a1") is not None, "and a label still finds its row"
    assert second.value.concept("a1") is not None
    assert first.value.concept("a1") is not second.value.concept("a1")
