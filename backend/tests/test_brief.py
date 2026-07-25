"""Article-brief generation (phase 06 §6).

Spec (plan/06 → *GenerateArticleBrief* and the golden brief test): per approved
article, a brief-as-contract carrying title, thesis, audience, reader knowledge,
reader problem, opening direction, argument structure, evidence per section,
required examples, claims requiring qualification, required conclusion, length,
platform constraints, active voice profile, style overrides, excluded material,
reserved material and a definition of done — with mandatory and optional
distinguished throughout.

"Contract" is the operative word, and it is what the two stage-level checks are
for. A brief that cites a claim the source model marked as needing qualification,
without carrying that qualification forward, has quietly licensed the draft to
state it flat. A brief that omits the source's own publication constraints has
licensed publishing them. Neither failure is visible in the brief itself — both
are only visible against the source model — which is exactly why the stage checks
rather than trusting.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import PipelineContext, StageResult, StageRunner
from groundscribe.stages.brief import BRIEF_STAGE, BriefOutcome, GenerateArticleBrief
from groundscribe.stages.errors import BriefContractError, EvidenceError
from groundscribe.stages.override import approve_architecture
from groundscribe.stages.schemas import ArticleBriefDocument, SourceModel
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowState
from stage_helpers import scripted_context
from test_architecture import propose

AUTHOR = "u1"


async def brief_for(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
    *,
    extra_response: dict[str, Any] | None = None,
) -> tuple[PipelineContext, StageResult[BriefOutcome], SourceModel]:
    """Run the pipeline through approval and generate the brief for the lead article."""
    context, model_client = scripted_context(db_session, snapshot_store)
    proposed = await propose(context, model_client)
    approve_architecture(
        context,
        architecture=proposed.value.architecture,
        snapshot=proposed.outputs[0],
        approved_by=AUTHOR,
    )
    source_model = _source_model_of(context)
    model_client.script_response(
        BRIEF_STAGE, payload if payload is not None else golden_json("brief.json")
    )
    if extra_response is not None:
        model_client.script_response(BRIEF_STAGE, extra_response)

    concept = proposed.value.concept(proposed.value.proposal.decision.selected)
    assert concept is not None
    result = await StageRunner(context).run(
        GenerateArticleBrief(
            concept=concept,
            article=proposed.value.proposal.article(concept.id),  # type: ignore[arg-type]
            source_model=source_model,
            architecture_snapshot=proposed.outputs[0],
        )
    )
    return context, result, source_model


def _source_model_of(context: PipelineContext) -> SourceModel:
    """The source model this run extracted, read back from its snapshot."""
    for execution in context.engine.run.stage_executions:
        for artifact in execution.outputs:
            if artifact.snapshot.artifact_type is ArtifactType.SOURCE_MODEL:
                return SourceModel.model_validate(
                    json.loads(context.snapshots.read(artifact.snapshot).decode("utf-8"))
                )
    raise AssertionError("the run produced no source model")


async def test_the_approved_concept_produces_the_expected_brief(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 golden test: approved concept → brief with every required field."""
    _, result, _ = await brief_for(db_session, snapshot_store)
    brief = result.value.brief

    assert isinstance(brief, ArticleBriefDocument)
    assert brief.title and brief.thesis
    assert brief.audience and brief.reader_knowledge and brief.reader_problem
    assert brief.opening_direction and brief.required_conclusion
    assert brief.target_length_words == 1800
    assert brief.platform_constraints and brief.voice_profile
    assert brief.style_overrides and brief.excluded_material and brief.reserved_material
    assert len(brief.argument_structure) == 4
    assert all(section.heading and section.purpose for section in brief.argument_structure)
    assert brief.argument_structure[0].required_examples


async def test_the_brief_distinguishes_mandatory_from_optional(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 exit criterion: mandatory vs optional distinguished, in both places."""
    _, result, _ = await brief_for(db_session, snapshot_store)
    brief = result.value.brief

    assert [section.mandatory for section in brief.argument_structure] == [
        True,
        True,
        True,
        False,
    ]
    assert len(brief.mandatory_criteria) == 4
    assert len(brief.optional_criteria) == 1
    assert brief.mandatory_criteria != brief.definition_of_done


def test_a_definition_of_done_with_nothing_mandatory_is_refused() -> None:
    """A definition of done where everything is optional defines nothing."""
    payload = golden_json("brief.json")
    for criterion in payload["definition_of_done"]:
        criterion["mandatory"] = False

    with pytest.raises(ValueError, match="mandatory"):
        ArticleBriefDocument.model_validate(payload)


def test_a_brief_with_no_definition_of_done_is_refused() -> None:
    """The definition of done is what makes the brief a contract rather than a wish."""
    payload = golden_json("brief.json")
    payload["definition_of_done"] = []

    with pytest.raises(ValueError, match="definition_of_done"):
        ArticleBriefDocument.model_validate(payload)


def test_a_brief_with_no_mandatory_section_is_refused() -> None:
    """An argument structure of entirely optional sections is not a structure."""
    payload = golden_json("brief.json")
    for section in payload["argument_structure"]:
        section["mandatory"] = False

    with pytest.raises(ValueError, match="mandatory"):
        ArticleBriefDocument.model_validate(payload)


async def test_a_brief_citing_an_unknown_claim_fails_the_stage(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The brief argues from the source model, or it argues from nothing."""
    payload = golden_json("brief.json")
    payload["argument_structure"][0]["claim_ids"] = ["c99"]

    with pytest.raises(EvidenceError, match="c99"):
        await brief_for(db_session, snapshot_store, payload)


async def test_a_brief_dropping_a_required_qualification_fails_the_stage(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A qualification the source model demanded cannot be lost in the brief.

    The brief is what the draft is written against. Citing a claim that needs
    qualifying without carrying the qualification forward licenses the draft to
    state it flat, and nothing downstream would know the difference.
    """
    payload = golden_json("brief.json")
    payload["claims_requiring_qualification"] = []

    with pytest.raises(BriefContractError, match="c1"):
        await brief_for(db_session, snapshot_store, payload)


async def test_a_brief_omitting_the_sources_publication_constraints_fails_the_stage(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """What the source said may not be published must be excluded by name."""
    payload = golden_json("brief.json")
    payload["excluded_material"] = []

    with pytest.raises(BriefContractError, match="postmortem"):
        await brief_for(db_session, snapshot_store, payload)


async def test_the_brief_is_stored_and_parks_for_review(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The brief is an artefact, a row, and a human control point."""
    context, result, _ = await brief_for(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    assert context.engine.state is WorkflowState.BRIEF_REVIEW_REQUIRED
    assert context.engine.is_paused

    (snapshot,) = [s for s in result.outputs if s.artifact_type is ArtifactType.ARTICLE_BRIEF]
    stored = json.loads(snapshot_store.read(snapshot).decode("utf-8"))
    assert ArticleBriefDocument.model_validate(stored) == result.value.brief

    row = result.value.row
    assert row.concept_id == result.value.brief_concept_id
    assert row.created_by_execution_id == execution.id
    assert row.snapshot_id == snapshot.id
    # The architecture it was briefed from is the recorded input.
    assert [artifact.role for artifact in execution.inputs] == ["content_architecture"]


async def test_the_brief_records_the_contract_it_committed_to(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The definition of done is a decision: what "finished" means for this article."""
    _, result, _ = await brief_for(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    (record,) = [row for row in execution.decision_records if row.decision_type == "article_brief"]
    assert record.decided_by_type is ActorType.POLICY
    assert record.policy_version
    assert record.outcome == "Your cache key is a specification"
    assert len(record.inputs["definition_of_done"]) == 5
    assert record.inputs["mandatory_criteria"] == 4
    assert record.inputs["target_length_words"] == 1800


async def test_the_brief_carries_the_projects_constraints_not_the_models_guess(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Length and voice are the project's to set; the brief records what was in force."""
    payload = golden_json("brief.json")
    payload["target_length_words"] = 400

    with pytest.raises(BriefContractError, match="1800"):
        await brief_for(db_session, snapshot_store, payload)


def test_the_brief_names_every_field_the_spec_lists() -> None:
    """plan/06 enumerates the brief's fields; a missing one is a missing clause."""
    assert set(ArticleBriefDocument.model_fields) == {
        "schema_version",
        "title",
        "thesis",
        "audience",
        "reader_knowledge",
        "reader_problem",
        "opening_direction",
        "argument_structure",
        "claims_requiring_qualification",
        "required_conclusion",
        "target_length_words",
        "platform_constraints",
        "voice_profile",
        "style_overrides",
        "excluded_material",
        "reserved_material",
        "definition_of_done",
    }
