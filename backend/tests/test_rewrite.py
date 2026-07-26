"""Substantive rewrite: applying the plan, and branching from a parent (phase 07 §10).

Spec (plan/07 → *RewriteSubstantively*, plus the immutable-branching-lineage and
no-source-mutation tests): apply the approved revision plan — structure, order,
evidence amount, thesis wording, examples, scope are all in scope — but do not
alter the source model or invent facts; create a new ``ArticleVersion`` linked to
its parent; and allow *multiple rewrites to branch from the same parent*, which is
how two prompts, models or strategies get compared.

The rewrite inherits every check drafting has, because it can break the same
promises: a claim nobody extracted, a qualification dropped, excluded material
printed. It adds two of its own, both aimed at plan/07's named risk of a rewriter
that does what it likes with the feedback:

- every *required* change in the plan is applied, and a skipped one is named;
- a claim the plan promised would not change is still argued in the rewrite.

The branch test is the one that matters for phase 12: two rewrites from one parent
are two children, not one child overwritten twice, and both keep their lineage.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain import models as domain_models
from groundscribe.domain import schemas as domain_schemas
from groundscribe.domain.enums import ArtifactType, BranchStatus
from groundscribe.stages.base import StageResult, StageRunner
from groundscribe.stages.drafting import DraftOutcome
from groundscribe.stages.errors import DraftContractError, RewriteContractError
from groundscribe.stages.planning import approve_revision_plan
from groundscribe.stages.rewriting import REWRITE_STAGE, RewriteSubstantively
from groundscribe.stages.schemas import RewrittenArticle
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowState
from pipeline_helpers import AUTHOR
from test_drafting import VOICE, Drafted
from test_revision_plan import plan


def golden_rewrite(**overrides: Any) -> dict[str, Any]:
    """The golden draft, rewritten: same shape, plus what the plan asked for."""
    draft = golden_json("draft.json", suite="draft_to_voice")
    rewritten = draft | {
        "body": draft["body"].replace(
            "on warm cache. That number",
            "on warm cache, on the article pages. That number",
        ),
        "changes_applied": ["ch1", "ch2"],
        "changes_skipped": ["ch3"],
        "skip_reasons": [
            "The deploy window is named two sentences later; repeating it reads oddly."
        ],
    }
    return rewritten | overrides


async def rewrite(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
) -> tuple[Drafted, StageResult[DraftOutcome]]:
    """Plan a revision, approve it, and rewrite against it."""
    drafted, planned = await plan(db_session, snapshot_store)
    approve_revision_plan(drafted.context, plan=planned.value, approved_by=AUTHOR)
    drafted.model_client.script_response(
        REWRITE_STAGE, payload if payload is not None else golden_rewrite()
    )
    result = await StageRunner(drafted.context).run(
        RewriteSubstantively(
            plan=planned.value.plan,
            plan_snapshot=planned.value.snapshot,
            previous=drafted.result.value.draft,
            parent=drafted.result.value.version,
            concept=drafted.briefed.concept,
            brief=drafted.briefed.brief,
            source_model=drafted.briefed.source_model,
            voice=VOICE,
        )
    )
    return drafted, result


async def test_the_rewrite_applies_the_plan_and_says_what_it_skipped(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 §10: the plan is the instruction, and departures are declared."""
    drafted, result = await rewrite(db_session, snapshot_store)
    written = result.value.draft

    assert isinstance(written, RewrittenArticle)
    assert written.changes_applied == ("ch1", "ch2")
    assert written.changes_skipped == ("ch3",)
    assert written.skip_reasons
    assert "on the article pages" in written.body
    assert drafted.context.engine.state is WorkflowState.SUBSTANTIVE_REVIEWING


async def test_a_rewrite_that_drops_a_required_change_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 risk: the rewriter is not free to decide which feedback mattered."""
    with pytest.raises(RewriteContractError, match="ch2"):
        await rewrite(
            db_session,
            snapshot_store,
            golden_rewrite(changes_applied=["ch1"], changes_skipped=["ch2", "ch3"]),
        )


async def test_a_rewrite_skipping_a_change_without_a_reason_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An optional change may be skipped; skipping it silently may not."""
    with pytest.raises(ValueError, match="reason"):
        await rewrite(db_session, snapshot_store, golden_rewrite(skip_reasons=[]))


async def test_a_rewrite_abandoning_a_protected_claim_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The plan promised these claims would survive; the rewrite dropped one."""
    with pytest.raises(RewriteContractError, match="c4"):
        await rewrite(
            db_session, snapshot_store, golden_rewrite(claims_used=["c1", "c2", "c3", "c5"])
        )


async def test_a_rewrite_inventing_a_claim_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The rewrite inherits drafting's checks: it can break the same promises."""
    with pytest.raises(DraftContractError, match="excluded"):
        await rewrite(
            db_session,
            snapshot_store,
            golden_rewrite(
                body=golden_rewrite()["body"]
                + "\n\nThe internal postmortem covering the deploy is not publishable.\n"
            ),
        )


async def test_the_rewrite_branches_from_its_parent(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 → a new version linked to its parent; the parent stays readable."""
    drafted, result = await rewrite(db_session, snapshot_store)

    parent = drafted.result.value.version
    child = domain_schemas.ArticleVersion.model_validate(result.value.version)
    assert child.parent_id == parent.id
    assert child.ordinal == 1
    assert child.branch_status is BranchStatus.ACTIVE
    assert parent.branch_status is BranchStatus.SUPERSEDED

    (snapshot,) = [s for s in result.outputs if s.artifact_type is ArtifactType.ARTICLE_VERSION]
    assert snapshot.parent_snapshot_id == drafted.result.outputs[0].id
    # The parent's own bytes are untouched.
    assert snapshot_store.verify(drafted.result.outputs[0]) is True


async def test_two_rewrites_may_branch_from_the_same_parent(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07: two rewrites from one parent, for comparing prompts or models.

    This is what phase 12's experimentation rests on, so it is pinned here rather
    than assumed from phase 02's lineage support.
    """
    drafted, first = await rewrite(db_session, snapshot_store)
    parent = drafted.result.value.version

    drafted.model_client.script_response(
        REWRITE_STAGE, golden_rewrite(title="A cache key is a specification")
    )
    second = await StageRunner(drafted.context).run(
        RewriteSubstantively(
            plan=(await _plan_of(drafted)),
            plan_snapshot=first.value.version.snapshot,
            previous=drafted.result.value.draft,
            parent=parent,
            concept=drafted.briefed.concept,
            brief=drafted.briefed.brief,
            source_model=drafted.briefed.source_model,
            voice=VOICE,
            transitions=False,
        )
    )

    assert second.value.version.parent_id == parent.id
    assert second.value.version.id != first.value.version.id
    assert second.value.version.ordinal == 2
    children = list(
        db_session.execute(
            select(domain_models.ArticleVersion).where(
                domain_models.ArticleVersion.parent_id == parent.id
            )
        ).scalars()
    )
    assert len(children) == 2


async def test_the_rewrite_never_touches_the_source_model(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 invariant: rewrite and voice stages never modify the source model."""
    drafted, result = await rewrite(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    produced = {artifact.snapshot.artifact_type for artifact in execution.outputs}
    assert ArtifactType.SOURCE_MODEL not in produced
    assert produced == {ArtifactType.ARTICLE_VERSION}
    # The source model snapshot the run extracted is still the only one, unchanged.
    models = list(
        db_session.execute(
            select(domain_models.ArtifactSnapshot).where(
                domain_models.ArtifactSnapshot.artifact_type == ArtifactType.SOURCE_MODEL
            )
        ).scalars()
    )
    assert len(models) == 1
    assert snapshot_store.verify(models[0]) is True


async def _plan_of(drafted: Drafted) -> Any:
    """The plan this run approved, read back from its snapshot."""
    import json

    for execution in drafted.context.engine.run.stage_executions:
        for artifact in execution.outputs:
            if artifact.snapshot.artifact_type is ArtifactType.REVISION_PLAN:
                from groundscribe.stages.schemas import RevisionPlanDocument

                return RevisionPlanDocument.model_validate(
                    json.loads(drafted.context.snapshots.read(artifact.snapshot).decode("utf-8"))
                )
    raise AssertionError("the run produced no revision plan")
