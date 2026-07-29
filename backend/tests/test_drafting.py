"""Initial drafting, and what it does about facts it does not have (phase 07 §7).

Spec (plan/07 → *GenerateInitialDraft* and the unresolved-marker test): draft from
the source model, the locked architecture, the brief, the active voice profile and
the project's constraints — and, crucially, *do not resolve missing facts*. The
draft omits unsupported material, qualifies what needs qualifying, inserts a
visible unresolved marker, or asks to go back to gap analysis. It never invents.

"Never invents" cannot be checked by reading prose, so the draft declares what it
did: which claims it used, which it qualified, what it left out and why, and what
it could not resolve. Those declarations are checkable against the source model and
the brief, and they are what the tests here hold the stage to. A draft that used a
claim nobody extracted, dropped a qualification the source demanded, or printed
material the brief excluded is refused — three failures that read perfectly well as
English and are wrong in exactly the way this product exists to prevent.

The run does not go back to gap analysis by itself. The phase-05 table has no edge
from `draft_generating` to the source stages, and adding one is phase-05's
business; a blocking unresolved fact is therefore *recorded as a request* for a
person, and phase 08's routing is what acts on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain import models as domain_models
from groundscribe.domain import schemas as domain_schemas
from groundscribe.domain.enums import ArtifactType, BranchStatus
from groundscribe.llm import FakeLLMClient
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import PipelineContext, StageResult, StageRunner
from groundscribe.stages.drafting import DRAFT_STAGE, DraftOutcome, GenerateInitialDraft
from groundscribe.stages.errors import DraftContractError, EvidenceError
from groundscribe.stages.schemas import ArticleDraft
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.voice.enums import InstructionStrength, VoiceCategory
from groundscribe.voice.schemas import VoiceInstruction, VoiceProfileDocument
from groundscribe.workflow.states import WorkflowState
from pipeline_helpers import BriefedArticle, run_to_approved_brief
from stage_helpers import scripted_context

VOICE = VoiceProfileDocument(
    name="default",
    description="Plain, first-person, technical without ceremony.",
    instructions=(
        VoiceInstruction(
            id="direct",
            category=VoiceCategory.TONE,
            strength=InstructionStrength.STRONG_PREFERENCE,
            text="State the finding, then the evidence. Do not build up to it.",
        ),
        VoiceInstruction(
            id="no-filler",
            category=VoiceCategory.PROHIBITED_PATTERNS,
            strength=InstructionStrength.HARD_RULE,
            text="Never write these phrases.",
            prohibits=("delve", "in today's fast-paced world", "it is important to note"),
        ),
    ),
)


def golden_draft(**overrides: Any) -> dict[str, Any]:
    """The golden draft, with one field varied per test."""
    return golden_json("draft.json", suite="draft_to_voice") | overrides


@dataclass(frozen=True)
class Drafted:
    """One drafted run: the context, its fake, the brief behind it, and the result."""

    context: PipelineContext
    model_client: FakeLLMClient
    briefed: BriefedArticle
    result: StageResult[DraftOutcome]


async def draft(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
) -> Drafted:
    """Run the pipeline to an approved brief, then draft against it."""
    context, model_client = scripted_context(db_session, snapshot_store)
    briefed = await run_to_approved_brief(context, model_client)
    model_client.script_response(DRAFT_STAGE, payload if payload is not None else golden_draft())

    result = await StageRunner(context).run(
        GenerateInitialDraft(
            brief=briefed.brief,
            brief_snapshot=briefed.brief_snapshot,
            concept=briefed.concept,
            source_model=briefed.source_model,
            source_model_snapshot=briefed.source_model_snapshot,
            voice=VOICE,
        )
    )
    return Drafted(context=context, model_client=model_client, briefed=briefed, result=result)


async def test_the_brief_produces_a_draft_that_declares_what_it_did(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 §7: prose, plus the declarations that make the prose checkable."""
    written = (await draft(db_session, snapshot_store)).result.value.draft

    assert isinstance(written, ArticleDraft)
    assert written.body.strip()
    assert written.title and written.thesis
    assert written.claims_used
    assert written.qualifications_applied == ("c1",)
    assert [omitted.reason for omitted in written.omitted]
    assert written.word_count > 100


async def test_a_draft_using_a_claim_nobody_extracted_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The draft argues from the source model, or it argues from nothing."""
    with pytest.raises(EvidenceError, match="c99"):
        await draft(db_session, snapshot_store, golden_draft(claims_used=["c1", "c99"]))


async def test_a_draft_dropping_a_required_qualification_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A conditional number stated flat is the failure the source model exists to stop."""
    with pytest.raises(DraftContractError, match="c1"):
        await draft(db_session, snapshot_store, golden_draft(qualifications_applied=[]))


async def test_a_draft_printing_excluded_material_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The brief said this must not be published; the draft published it.

    Checked against the brief's own words rather than a general notion of
    confidentiality — phase 13 owns that. What the brief excluded by name is what
    the draft may not contain.
    """
    body = golden_json("draft.json", suite="draft_to_voice")["body"]
    leaked = body + "\n\nThe internal postmortem covering the deploy is not publishable.\n"

    with pytest.raises(DraftContractError, match="excluded"):
        await draft(db_session, snapshot_store, golden_draft(body=leaked))


def test_an_unresolved_marker_must_actually_appear_in_the_prose() -> None:
    """A marker nobody can see is an invented fact with extra steps."""
    payload = golden_draft(
        unresolved=[
            {
                "marker": "[UNRESOLVED: cold-cache p99]",
                "question": "What was the cold-cache p99?",
                "blocking": False,
                "claim_ids": ["c1"],
            }
        ]
    )

    with pytest.raises(ValueError, match="does not appear"):
        ArticleDraft.model_validate(payload)


async def test_an_unresolved_fact_is_marked_in_the_prose_rather_than_invented(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07: omit, qualify, or mark — never resolve a fact the source lacks."""
    marker = "[UNRESOLVED: cold-cache p99]"
    body = golden_json("draft.json", suite="draft_to_voice")["body"]
    payload = golden_draft(
        body=body.replace("on warm cache.", f"on warm cache. Cold-cache p99 {marker}."),
        unresolved=[
            {
                "marker": marker,
                "question": "What was the cold-cache p99, over what window?",
                "blocking": False,
                "claim_ids": ["c1"],
            }
        ],
    )

    written = (await draft(db_session, snapshot_store, payload)).result.value.draft

    assert marker in written.body
    assert written.unresolved[0].question


async def test_a_blocking_unresolved_fact_asks_to_go_back_to_the_author(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The draft cannot route itself; it records the request and phase 08 acts on it."""
    marker = "[UNRESOLVED: which locales]"
    body = golden_json("draft.json", suite="draft_to_voice")["body"]
    payload = golden_draft(
        body=body.replace("non-default locale", f"non-default locale {marker}"),
        unresolved=[
            {
                "marker": marker,
                "question": "Which locales were affected?",
                "blocking": True,
                "claim_ids": ["c4"],
            }
        ],
    )

    drafted = await draft(db_session, snapshot_store, payload)
    execution = drafted.result.execution

    assert execution is not None
    (request,) = [
        record for record in execution.decision_records if record.decision_type == "gap_return"
    ]
    assert request.decided_by_type is ActorType.POLICY
    assert request.policy_version
    assert request.inputs["blocking"] == ["Which locales were affected?"]
    assert any(event.event_type == "intervention.requested" for event in execution.trace_events)
    # The draft still exists and the run still advances: the request is for a
    # person, not a refusal to produce anything.
    assert drafted.context.engine.state is WorkflowState.SUBSTANTIVE_REVIEWING


async def test_the_draft_is_an_immutable_version_with_full_provenance(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 → stored as an immutable ArticleVersion with its whole trace."""
    drafted = await draft(db_session, snapshot_store)
    context, result = drafted.context, drafted.result
    execution = result.execution

    assert execution is not None
    assert context.engine.state is WorkflowState.SUBSTANTIVE_REVIEWING

    (snapshot,) = [s for s in result.outputs if s.artifact_type is ArtifactType.ARTICLE_VERSION]
    stored = json.loads(snapshot_store.read(snapshot).decode("utf-8"))
    assert ArticleDraft.model_validate(stored) == result.value.draft

    # Read back through the schema rather than off the row: it asserts the same
    # facts and proves the new version round-trips, which the ORM's `declared_attr`
    # lineage column does not let mypy check on the row itself.
    version = domain_schemas.ArticleVersion.model_validate(result.value.version)
    assert version.ordinal == 0
    assert version.parent_id is None
    assert version.snapshot_id == snapshot.id
    assert version.branch_status is BranchStatus.ACTIVE
    assert version.created_by_execution_id == execution.id

    article = db_session.get(domain_models.Article, result.value.version.article_id)
    assert article is not None
    assert article.project_id == context.project_id
    assert article.title == result.value.draft.title

    # The brief and the source model are both recorded as inputs: the draft is
    # written against one and checked against the other.
    assert {artifact.role for artifact in execution.inputs} == {"article_brief", "source_model"}
    assert drafted.briefed.brief_snapshot.id in {a.snapshot_id for a in execution.inputs}

    # Usage, finish reason and the voice profile in force are on the record.
    assert result.detail["finish_reason"] == "stop"
    assert result.detail["voice_profile"] == "default"
