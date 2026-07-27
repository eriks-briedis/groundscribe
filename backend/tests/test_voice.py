"""Line editing and voice alignment (phase 07 §11).

Spec (plan/07 → *AlignVoice* and the prohibited-change guard test): a style-only
pass. Permitted — rhythm, word choice, flow, repetition, formality, mechanical
transitions, unnatural phrasing, excessive abstraction, generic AI patterns.
Prohibited — new claims, new examples, new technical detail, changed evidence,
a changed thesis, removed qualifications, significant structural change. On
discovering a structural problem, route back to substantive revision rather than
silently changing it.

The enforcement is structural rather than advisory, and that is the point of this
module. The voice pass returns *only a body and a list of changes*: it has no field
for a claim, a thesis or a qualification, so the new version is built by copying the
previous one and replacing its prose. A prohibited change is not rejected — it is
unrepresentable.

What is left for the stage to check is whether the declared changes are real, and
whether the body still carries what the previous version promised. A pass that
claims to have rephrased something that was never there is fabricating its own
record, and one that quietly deleted an unresolved marker has published a hole as
though it were an answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain import models as domain_models
from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import StageResult, StageRunner
from groundscribe.stages.drafting import DraftOutcome
from groundscribe.stages.errors import DraftContractError, VoiceContractError
from groundscribe.stages.schemas import ArticleDraft, VoiceChangeKind, VoicePass
from groundscribe.stages.voice import VOICE_STAGE, AlignVoice
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from test_drafting import VOICE, Drafted, draft

#: A phrase the golden draft actually contains, and a plainer way to say it.
BEFORE = "That number is the reason anyone would read this."
AFTER = "That number is why anyone would read this."


def golden_voice_pass(**overrides: Any) -> dict[str, Any]:
    """A style-only pass over the golden draft."""
    body = golden_json("draft.json", suite="draft_to_voice")["body"]
    payload = {
        "schema_version": 1,
        "body": body.replace(BEFORE, AFTER),
        "changes": [
            {
                "kind": "word_choice",
                "before": BEFORE,
                "after": AFTER,
                "reason": "Plainer, and one clause shorter.",
            }
        ],
        "structural_problems": [],
    }
    return payload | overrides


async def align(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
) -> tuple[Drafted, StageResult[DraftOutcome]]:
    """Draft the golden article, accept the review, and run a voice pass over it."""
    drafted = await draft(db_session, snapshot_store)
    # A polish-only review would take the article here; the test takes the edge
    # directly, because what is under test is the voice pass and not the routing.
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.model_client.script_response(
        VOICE_STAGE, payload if payload is not None else golden_voice_pass()
    )
    result = await StageRunner(drafted.context).run(
        AlignVoice(
            previous=drafted.result.value.draft,
            parent=drafted.result.value.version,
            concept=drafted.briefed.concept,
            brief=drafted.briefed.brief,
            voice=VOICE,
        )
    )
    return drafted, result


async def test_a_voice_pass_changes_the_prose_and_nothing_else(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 §11: style only, and the schema is what makes that true."""
    drafted, result = await align(db_session, snapshot_store)
    aligned = result.value.draft
    before = drafted.result.value.draft

    assert isinstance(aligned, ArticleDraft)
    assert AFTER in aligned.body
    assert BEFORE not in aligned.body
    # Everything that is not prose is carried over untouched, by construction.
    assert aligned.thesis == before.thesis
    assert aligned.claims_used == before.claims_used
    assert aligned.qualifications_applied == before.qualifications_applied
    assert aligned.omitted == before.omitted
    assert drafted.context.engine.state is WorkflowState.SCORING


def test_the_permitted_changes_are_exactly_what_the_spec_lists() -> None:
    """A prohibited change has no member to be expressed as."""
    assert {kind.value for kind in VoiceChangeKind} == {
        "rhythm",
        "word_choice",
        "flow",
        "repetition",
        "formality",
        "transition",
        "phrasing",
        "abstraction",
        "ai_pattern",
    }


def test_a_pass_that_declares_nothing_but_changes_the_prose_is_refused() -> None:
    """Every edit is declared, or the change list is decoration."""
    with pytest.raises(ValueError, match="at least one change"):
        VoicePass.model_validate(golden_voice_pass(changes=[]))


async def test_a_change_that_was_never_in_the_prose_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A pass claiming to have rephrased something absent is inventing its record."""
    payload = golden_voice_pass()
    payload["changes"][0]["before"] = "a sentence the draft never contained"

    with pytest.raises(VoiceContractError, match="never contained"):
        await align(db_session, snapshot_store, payload)


async def test_a_change_that_is_not_in_the_result_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The declared result has to be the actual result."""
    payload = golden_voice_pass()
    payload["changes"][0]["after"] = "a sentence the pass did not actually write"

    with pytest.raises(VoiceContractError, match="did not actually write"):
        await align(db_session, snapshot_store, payload)


async def test_a_voice_pass_may_not_delete_an_unresolved_marker(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Deleting a marker publishes a hole as though it were an answer."""
    marker = "[UNRESOLVED: cold-cache p99]"
    body = golden_json("draft.json", suite="draft_to_voice")["body"]
    marked = body.replace("on warm cache.", f"on warm cache. Cold-cache p99 {marker}.")
    drafted = await draft(
        db_session,
        snapshot_store,
        golden_json("draft.json", suite="draft_to_voice")
        | {
            "body": marked,
            "unresolved": [
                {
                    "marker": marker,
                    "question": "What was the cold-cache p99?",
                    "blocking": False,
                    "claim_ids": ["c1"],
                }
            ],
        },
    )
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.model_client.script_response(
        VOICE_STAGE,
        {
            "schema_version": 1,
            "body": marked.replace(f" Cold-cache p99 {marker}.", ""),
            "changes": [
                {
                    "kind": "flow",
                    "before": f" Cold-cache p99 {marker}.",
                    "after": "",
                    "reason": "The aside interrupts the opening.",
                }
            ],
            "structural_problems": [],
        },
    )

    with pytest.raises(VoiceContractError, match="marker"):
        await StageRunner(drafted.context).run(
            AlignVoice(
                previous=drafted.result.value.draft,
                parent=drafted.result.value.version,
                concept=drafted.briefed.concept,
                brief=drafted.briefed.brief,
                voice=VOICE,
            )
        )


async def test_a_voice_pass_may_not_write_material_the_brief_excluded(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The brief still binds a pass that only rewrote sentences.

    The prose is the one thing a voice pass returns, so it is the one thing worth
    re-checking. Every other declaration is copied from the previous version rather
    than generated, and checking those would compare each one against itself.

    An excluded phrase does not have to be *added* to arrive here. A pass rewording
    a nearby paragraph for flow can land on the sentence the brief named, which is
    why this is checked against the result rather than against the diff.
    """
    body = golden_json("draft.json", suite="draft_to_voice")["body"]
    leaked = body.replace(BEFORE, "The internal postmortem covering the deploy is not publishable.")
    payload = golden_voice_pass(
        body=leaked,
        changes=[
            {
                "kind": "phrasing",
                "before": BEFORE,
                "after": "The internal postmortem covering the deploy is not publishable.",
                "reason": "Reads as an aside; folded in the surrounding context.",
            }
        ],
    )

    with pytest.raises(DraftContractError, match="excluded"):
        await align(db_session, snapshot_store, payload)


async def test_a_structural_problem_routes_back_instead_of_being_fixed(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 §11: route back to substantive revision rather than silently changing it."""
    payload = golden_voice_pass(
        structural_problems=[
            {
                "location": "Section: The key that named two of its three inputs",
                "description": (
                    "The section argues two things at once and no amount of rephrasing "
                    "separates them."
                ),
                "suggested_route": "substantive_issue",
            }
        ]
    )

    drafted, result = await align(db_session, snapshot_store, payload)
    execution = result.execution

    assert execution is not None
    (record,) = [
        row for row in execution.decision_records if row.decision_type == "voice_structural_return"
    ]
    assert record.decided_by_type is ActorType.POLICY
    assert record.policy_version
    assert record.inputs["routes"] == ["substantive_issue"]
    assert any(event.event_type == "intervention.requested" for event in execution.trace_events)

    # The style changes it *could* safely make are still applied and stored…
    assert AFTER in result.value.draft.body
    # …but the article does not go on to scoring with a known structural problem.
    assert drafted.context.engine.state is WorkflowState.VOICE_ALIGNING


async def test_the_voice_pass_is_a_new_version_branching_from_its_parent(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 → every version is immutable with retained lineage."""
    drafted, result = await align(db_session, snapshot_store)

    parent = drafted.result.value.version
    assert result.value.version.parent_id == parent.id
    assert result.value.version.ordinal == 1
    (snapshot,) = [s for s in result.outputs if s.artifact_type is ArtifactType.ARTICLE_VERSION]
    assert snapshot.parent_snapshot_id == drafted.result.outputs[0].id
    assert snapshot_store.verify(drafted.result.outputs[0]) is True

    execution = result.execution
    assert execution is not None
    assert result.detail["changes"] == 1
    assert result.detail["voice_profile"] == "default"


async def test_the_voice_pass_never_touches_the_source_model(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 invariant: rewrite and voice stages never modify the source model.

    The rewrite half of this is pinned in ``test_rewrite``. Asserted here too
    because the two stages arrive at it differently: the rewrite is *given* the
    source model and is trusted not to write it, while the voice pass is never
    handed one at all. The second is the stronger arrangement, and a refactor that
    passed one in for convenience would quietly downgrade it to the first.
    """
    _, result = await align(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    produced = {artifact.snapshot.artifact_type for artifact in execution.outputs}
    assert produced == {ArtifactType.ARTICLE_VERSION}
    models = list(
        db_session.execute(
            select(domain_models.ArtifactSnapshot).where(
                domain_models.ArtifactSnapshot.artifact_type == ArtifactType.SOURCE_MODEL
            )
        ).scalars()
    )
    assert len(models) == 1
    assert snapshot_store.verify(models[0]) is True
