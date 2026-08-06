"""User architecture override and locking (phase 06 §5).

Spec (plan/06 → *User architecture override* and the override-provenance test):
merge / split / remove / reorder / rename / edit-thesis / reassign-evidence;
trade-off warnings surfaced *without blocking*; architecture locking (versioned,
cannot silently change); and override provenance — before/after snapshots, a
structured diff, the reason, the warnings shown and accepted, and a lineage
branch.

Two properties carry the section.

**Warnings advise, they do not veto.** The author knows things the pipeline does
not — that the removed article is being saved for a talk, that the merged piece is
for a different audience. A warning that blocked would be a policy pretending to
be information. So every test that produces warnings also asserts the override
still applied.

**Nothing about an approved architecture changes quietly.** After approval the
row is locked, and a change has to fork a new version *and* name who authorised
it. The final test goes around the override API deliberately, to show the engine
guard is what enforces this rather than the convenience function.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain.enums import ArtifactType, BranchStatus
from groundscribe.llm import FakeLLMClient
from groundscribe.provenance.enums import ActorType, InterventionType
from groundscribe.stages.architecture import ArchitectureOutcome
from groundscribe.stages.base import PipelineContext, StageResult
from groundscribe.stages.errors import OverrideRejected
from groundscribe.stages.override import (
    ArchitectureOverride,
    OverrideCommand,
    OverrideOperation,
    apply_overrides,
    approve_architecture,
    override_architecture,
)
from groundscribe.stages.schemas import ArchitectureProposal, RiskLevel
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.errors import AttributionRequired, SilentMutationError
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from stage_helpers import scripted_context
from test_architecture import propose

AUTHOR = "u1"


def golden_proposal() -> ArchitectureProposal:
    """The golden architecture, parsed."""
    return ArchitectureProposal.model_validate(golden_json("architecture.json"))


async def approved(
    db_session: Session, snapshot_store: SnapshotStore
) -> tuple[PipelineContext, FakeLLMClient, StageResult[ArchitectureOutcome]]:
    """A run parked with an approved, locked architecture."""
    context, model_client = scripted_context(db_session, snapshot_store)
    proposed = await propose(context, model_client)
    approve_architecture(
        context,
        architecture=proposed.value.architecture,
        snapshot=proposed.outputs[0],
        approved_by=AUTHOR,
    )
    return context, model_client, proposed


def test_merge_combines_two_articles_and_keeps_both_sets_of_claims() -> None:
    """A merge is a scope decision: the result argues everything both did."""
    merged, warnings = apply_overrides(
        golden_proposal(),
        (
            OverrideCommand(
                operation=OverrideOperation.MERGE,
                article_ids=("a1", "a2"),
                title="Cache keys and determinism",
            ),
        ),
    )

    assert [article.id for article in merged.articles] == ["a1"]
    assert merged.articles[0].title == "Cache keys and determinism"
    assert set(merged.articles[0].supporting_claim_ids) == {"c2", "c3", "c4", "c5"}
    assert merged.series.is_series is False
    assert any(warning.code == "merged_theses" for warning in warnings)


def test_split_divides_the_claims_between_two_articles() -> None:
    """A split names which claims go where; the schema then requires each to have some."""
    split, warnings = apply_overrides(
        golden_proposal(),
        (
            OverrideCommand(
                operation=OverrideOperation.SPLIT,
                article_ids=("a1",),
                new_ids=("a1a", "a1b"),
                titles=("The locale bug", "The error-page bug"),
                claim_ids=("c3", "c4"),
            ),
        ),
    )

    ids = [article.id for article in split.articles]
    assert ids == ["a1a", "a1b", "a2"]
    assert split.article("a1a") is not None
    assert split.article("a1a").supporting_claim_ids == ("c3", "c4")  # type: ignore[union-attr]
    assert split.article("a1b").supporting_claim_ids == ("c5",)  # type: ignore[union-attr]
    assert any(warning.code == "thin_after_split" for warning in warnings)


def test_remove_warns_about_the_claims_it_orphans_and_applies_anyway() -> None:
    """plan/06: warnings are surfaced without blocking."""
    reduced, warnings = apply_overrides(
        golden_proposal(),
        (OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",)),),
    )

    assert [article.id for article in reduced.articles] == ["a1"]
    orphaned = next(warning for warning in warnings if warning.code == "orphaned_claims")
    assert "c2" in orphaned.message


def test_reorder_rename_edit_thesis_and_reassign_evidence() -> None:
    """The remaining four operations, each doing exactly what it says."""
    proposal = golden_proposal()

    reordered, _ = apply_overrides(
        proposal,
        (OverrideCommand(operation=OverrideOperation.REORDER, order=("a2", "a1")),),
    )
    assert [article.id for article in reordered.articles] == ["a2", "a1"]
    assert reordered.series.reading_order == ("a2", "a1")

    renamed, _ = apply_overrides(
        proposal,
        (
            OverrideCommand(
                operation=OverrideOperation.RENAME, article_ids=("a1",), title="Keys as contracts"
            ),
        ),
    )
    assert renamed.articles[0].title == "Keys as contracts"

    rethought, warnings = apply_overrides(
        proposal,
        (
            OverrideCommand(
                operation=OverrideOperation.EDIT_THESIS,
                article_ids=("a1",),
                thesis="Invalidation is the hard part of caching.",
            ),
        ),
    )
    assert rethought.articles[0].thesis == "Invalidation is the hard part of caching."
    assert any(warning.code == "thesis_evidence_unchecked" for warning in warnings)

    reassigned, _ = apply_overrides(
        proposal,
        (
            OverrideCommand(
                operation=OverrideOperation.REASSIGN_EVIDENCE,
                article_ids=("a2",),
                claim_ids=("c2", "c3", "c5"),
            ),
        ),
    )
    assert reassigned.article("a2").supporting_claim_ids == ("c2", "c3", "c5")  # type: ignore[union-attr]


def test_an_operation_naming_an_unknown_article_is_refused() -> None:
    """An override that edits nothing is a mistake, not a no-op."""
    with pytest.raises(OverrideRejected, match="a9"):
        apply_overrides(
            golden_proposal(),
            (OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a9",)),),
        )


def test_removing_every_article_is_refused() -> None:
    """There is no architecture with no articles; that is a cancellation."""
    with pytest.raises(OverrideRejected, match="at least one article"):
        apply_overrides(
            golden_proposal(),
            (OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a1", "a2")),),
        )


async def test_approving_an_architecture_locks_it(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 → the approved architecture is locked; plan/00 → a human control point."""
    context, _, proposed = await approved(db_session, snapshot_store)

    architecture = proposed.value.architecture
    assert architecture.locked is True
    assert architecture.locked_by == AUTHOR
    assert context.engine.state is WorkflowState.ARCHITECTURE_APPROVED
    assert context.engine.approved_architecture is not None
    interventions = [
        row
        for execution in context.engine.run.stage_executions
        for row in execution.user_interventions
    ]
    assert [row.intervention_type for row in interventions] == [InterventionType.APPROVAL]


async def test_an_override_forks_a_version_with_a_diff_and_a_branch(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/06 override-provenance test: before/after, diff, warnings, lineage branch."""
    context, _, proposed = await approved(db_session, snapshot_store)
    before_snapshot = proposed.outputs[0]

    result = override_architecture(
        context,
        architecture=proposed.value.architecture,
        proposal=proposed.value.proposal,
        snapshot=before_snapshot,
        commands=(OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",)),),
        requested_by=AUTHOR,
        reason="The determinism piece is going in a talk instead.",
    )

    assert isinstance(result, ArchitectureOverride)
    # Before and after both exist; the before is untouched.
    assert result.before_snapshot.id == before_snapshot.id
    assert snapshot_store.verify(before_snapshot) is True
    assert json.loads(snapshot_store.read(before_snapshot).decode("utf-8"))["articles"]

    # After forks from before: a lineage branch, not a replacement.
    assert result.after_snapshot.parent_snapshot_id == before_snapshot.id
    assert result.architecture.parent_id == proposed.value.architecture.id
    assert proposed.value.architecture.branch_status is BranchStatus.SUPERSEDED
    assert result.architecture.locked is False

    # A structured diff, stored as its own artefact.
    assert result.diff_snapshot.artifact_type is ArtifactType.STRUCTURED_DIFF
    entries = json.loads(snapshot_store.read(result.diff_snapshot).decode("utf-8"))["entries"]
    assert any(entry["path"].startswith("articles.1") for entry in entries)

    # And the run is back at review with the new version.
    assert context.engine.state is WorkflowState.ARCHITECTURE_REVIEW_REQUIRED


async def test_an_override_records_its_reason_and_the_warnings_shown_and_accepted(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The warnings the author saw are part of the record of what they decided."""
    context, _, proposed = await approved(db_session, snapshot_store)

    result = override_architecture(
        context,
        architecture=proposed.value.architecture,
        proposal=proposed.value.proposal,
        snapshot=proposed.outputs[0],
        commands=(OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",)),),
        requested_by=AUTHOR,
        reason="The determinism piece is going in a talk instead.",
        accepted_warnings=("orphaned_claims",),
    )

    assert [warning.code for warning in result.warnings] == ["orphaned_claims"]
    record = result.decision
    assert record.decided_by == AUTHOR
    assert record.rationale == "The determinism piece is going in a talk instead."
    assert [shown["code"] for shown in record.inputs["warnings_shown"]] == ["orphaned_claims"]
    assert record.inputs["warnings_accepted"] == ["orphaned_claims"]
    assert record.inputs["operations"] == ["remove"]
    (intervention,) = result.execution.user_interventions
    assert intervention.intervention_type is InterventionType.OVERRIDE


async def test_an_unaccepted_warning_is_still_recorded_as_shown(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Shown-and-ignored and never-shown are different, and only one is a bug."""
    context, _, proposed = await approved(db_session, snapshot_store)

    result = override_architecture(
        context,
        architecture=proposed.value.architecture,
        proposal=proposed.value.proposal,
        snapshot=proposed.outputs[0],
        commands=(OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",)),),
        requested_by=AUTHOR,
        reason="Dropping it.",
    )

    assert [shown["code"] for shown in result.decision.inputs["warnings_shown"]] == [
        "orphaned_claims"
    ]
    assert result.decision.inputs["warnings_accepted"] == []


async def test_an_anonymous_override_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An override nobody is accountable for cannot be reviewed (plan/03)."""
    context, _, proposed = await approved(db_session, snapshot_store)

    with pytest.raises(AttributionRequired, match="requested_by"):
        override_architecture(
            context,
            architecture=proposed.value.architecture,
            proposal=proposed.value.proposal,
            snapshot=proposed.outputs[0],
            commands=(OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",)),),
            requested_by="",
            reason="",
        )


async def test_replacing_an_approved_architecture_around_the_api_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The guard, not the convenience function, is what makes the lock real.

    plan/05's invariant is that an approved architecture is *superseded*, never
    replaced. A caller that writes a fresh snapshot and submits it has produced a
    replacement wearing no signature, and the engine refuses it.
    """
    context, _, _ = await approved(db_session, snapshot_store)
    context.engine.apply(
        WorkflowAction.REOPEN_ARCHITECTURE, actor_id=AUTHOR, actor_type=ActorType.USER
    )
    execution = context.engine.begin_stage("hand_rolled")
    unlinked = context.recorder.record_output(
        execution,
        artifact_type=ArtifactType.CONTENT_ARCHITECTURE,
        content={"articles": []},
        role="content_architecture",
    )

    with pytest.raises(SilentMutationError):
        context.engine.apply(WorkflowAction.SUBMIT_ARCHITECTURE, artifacts=(unlinked,))


def test_the_seven_operations_are_exactly_what_the_spec_names() -> None:
    """plan/06 names seven; a stray one would be a feature nobody asked for."""
    assert {operation.value for operation in OverrideOperation} == {
        "merge",
        "split",
        "remove",
        "reorder",
        "rename",
        "edit_thesis",
        "reassign_evidence",
    }


def test_a_split_that_leaves_an_article_without_claims_is_refused() -> None:
    """Every article argues something; a split that empties one has lost material."""
    with pytest.raises(OverrideRejected, match="claims"):
        apply_overrides(
            golden_proposal(),
            (
                OverrideCommand(
                    operation=OverrideOperation.SPLIT,
                    article_ids=("a2",),
                    new_ids=("a2a", "a2b"),
                    titles=("One", "Two"),
                    claim_ids=("c2", "c3"),
                ),
            ),
        )


def test_thin_content_risk_is_raised_when_a_split_leaves_one_claim() -> None:
    """The warning exists because the schema cannot tell thin from concise."""
    split, _ = apply_overrides(
        golden_proposal(),
        (
            OverrideCommand(
                operation=OverrideOperation.SPLIT,
                article_ids=("a1",),
                new_ids=("a1a", "a1b"),
                titles=("One", "Two"),
                claim_ids=("c3",),
            ),
        ),
    )

    assert split.article("a1a").thin_content_risk is RiskLevel.HIGH  # type: ignore[union-attr]


def test_overrides_apply_in_order() -> None:
    """Several operations compose, each seeing the result of the last."""
    result, _ = apply_overrides(
        golden_proposal(),
        (
            OverrideCommand(operation=OverrideOperation.RENAME, article_ids=("a1",), title="First"),
            OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",)),
            OverrideCommand(
                operation=OverrideOperation.EDIT_THESIS,
                article_ids=("a1",),
                thesis="A cache key is a specification.",
            ),
        ),
    )

    assert [article.id for article in result.articles] == ["a1"]
    assert result.articles[0].title == "First"
    assert result.articles[0].thesis == "A cache key is a specification."


async def test_an_anonymous_approval_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Approval is the human control point; an unsigned one records nothing useful."""
    context, model_client = scripted_context(db_session, snapshot_store)
    proposed = await propose(context, model_client)

    with pytest.raises(AttributionRequired, match="approved_by"):
        approve_architecture(
            context,
            architecture=proposed.value.architecture,
            snapshot=proposed.outputs[0],
            approved_by="",
        )


async def test_an_edited_architecture_carries_its_concepts(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The branch is what approval reads, so it has to hold the articles.

    A proposal that is only a snapshot is a document. Concepts are what approval
    opens an article from, what the board lists, and what auto-advance picks the
    run's article out of — and branching used to copy the row and the snapshot
    and leave the concepts on the version it superseded.

    Every symptom of that is silent: an empty architecture board, an approval
    that opens no articles, and a run parked in `architecture_approved` with
    nothing queued and nothing to write. None of them names the cause.
    """
    from groundscribe.domain import models as domain_models

    context, _model_client, proposed = await approved(db_session, snapshot_store)
    before = proposed.value.architecture

    override = override_architecture(
        context,
        architecture=before,
        proposal=proposed.value.proposal,
        snapshot=proposed.outputs[0],
        commands=[OverrideCommand(operation=OverrideOperation.REMOVE, article_ids=("a2",))],
        requested_by=AUTHOR,
        reason="one article is enough",
    )

    concepts = (
        db_session.query(domain_models.ArticleConcept)
        .filter(domain_models.ArticleConcept.architecture_id == override.architecture.id)
        .all()
    )
    assert [concept.ref for concept in concepts] == ["a1"], (
        "the edited architecture has no articles to open"
    )
    # And they carry the edit, rather than being copied from what it replaced.
    assert concepts[0].title == override.proposal.articles[0].title
