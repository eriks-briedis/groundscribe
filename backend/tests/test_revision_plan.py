"""Revision planning, and what it does with findings that disagree (phase 07 §9).

Spec (plan/07 → *CreateRevisionPlan* and the reconciliation test): turn accepted
feedback into a coherent plan — accepted and rejected findings, required versus
optional changes, sections to preserve, claims that must not change, sections to
remove or move, whether the brief or architecture must reopen, and the expected
effect on scores — reconciling contradictory findings, and stored as its own
immutable artefact whose record explains what was combined, deferred or rejected.

plan/07 names the risk this stage exists to prevent: *the rewriter blindly applying
reviewer suggestions*. Two things here are that prevention, and both are tested as
hard as the happy path.

**Nothing the author accepted may go missing.** Every accepted finding has to be
either addressed by a change or explained in a reconciliation. A plan that quietly
dropped one would send the rewriter off with the author's decision half-applied and
nothing to show it had happened.

**A reconciliation must give a reason.** "Combined" with no rationale is not a
reconciliation, it is two findings going into one box. The reason is the only thing
that lets a person judge whether the contradiction was resolved the right way.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain.enums import ArtifactType
from groundscribe.provenance.enums import ActorType, InterventionType
from groundscribe.stages.base import StageResult, StageRunner
from groundscribe.stages.errors import PlanContractError
from groundscribe.stages.planning import (
    PLAN_STAGE,
    CreateRevisionPlan,
    PlanOutcome,
    approve_revision_plan,
)
from groundscribe.stages.review import open_review_ledger
from groundscribe.stages.schemas import ReconciliationKind, RevisionPlanDocument
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowState
from pipeline_helpers import AUTHOR
from test_drafting import Drafted
from test_review import review


def golden_plan(**overrides: Any) -> dict[str, Any]:
    """The golden revision plan, with one field varied per test."""
    return golden_json("revision_plan.json", suite="draft_to_voice") | overrides


async def plan(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
    *,
    accept: tuple[int, ...] = (0, 1, 2),
) -> tuple[Drafted, StageResult[PlanOutcome]]:
    """Review the golden draft, accept some findings, and plan the revision."""
    drafted, reviewed = await review(db_session, snapshot_store)
    ledger = open_review_ledger(drafted.context)
    for index in accept:
        ledger.accept(reviewed.value.findings[index], decided_by=AUTHOR)

    drafted.model_client.script_response(
        PLAN_STAGE, payload if payload is not None else golden_plan()
    )
    result = await StageRunner(drafted.context).run(
        CreateRevisionPlan(
            review=reviewed.value.review,
            review_row=reviewed.value.row,
            review_snapshot=reviewed.outputs[0],
            findings=reviewed.value.findings,
            draft=drafted.result.value.draft,
            brief=drafted.briefed.brief,
        )
    )
    return drafted, result


async def test_accepted_findings_become_a_coherent_plan(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 §9: the whole field set, with required and optional distinguished."""
    _, result = await plan(db_session, snapshot_store)
    planned = result.value.plan

    assert isinstance(planned, RevisionPlanDocument)
    assert planned.summary
    assert [change.required for change in planned.changes] == [True, True, False]
    assert planned.required_changes and planned.optional_changes
    assert planned.preserve_sections
    assert planned.claims_that_must_not_change == ("c1", "c4")
    assert planned.expected_score_effect
    assert planned.reopen_brief is False
    assert planned.reopen_architecture is False


async def test_contradictory_findings_are_reconciled_with_a_stated_reason(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07: the record explains what was combined, deferred and rejected."""
    _, result = await plan(db_session, snapshot_store)
    planned = result.value.plan

    combined = next(r for r in planned.reconciliations if r.kind is ReconciliationKind.COMBINED)
    assert set(combined.finding_refs) == {"i2", "i5"}
    assert "brief" in combined.rationale
    deferred = next(r for r in planned.reconciliations if r.kind is ReconciliationKind.DEFERRED)
    assert deferred.finding_refs == ("i4",)
    assert deferred.rationale

    # And the same reasoning reaches provenance, attributed to the stage's policy.
    execution = result.execution
    assert execution is not None
    (record,) = [row for row in execution.decision_records if row.decision_type == "revision_plan"]
    assert record.decided_by_type is ActorType.POLICY
    assert record.policy_version
    assert [entry["kind"] for entry in record.inputs["reconciliations"]] == [
        "combined",
        "deferred",
    ]
    assert record.inputs["required_changes"] == 2
    assert record.inputs["optional_changes"] == 1


def test_a_reconciliation_without_a_reason_is_refused() -> None:
    """Two findings in one box is not a reconciliation until someone says why."""
    payload = golden_plan()
    payload["reconciliations"][0]["rationale"] = "   "

    with pytest.raises(ValueError, match="rationale"):
        RevisionPlanDocument.model_validate(payload)


def test_a_change_addressing_no_finding_is_refused() -> None:
    """A plan is built from accepted feedback, not from the planner's own opinions."""
    payload = golden_plan()
    payload["changes"][0]["finding_refs"] = []

    with pytest.raises(ValueError, match="finding"):
        RevisionPlanDocument.model_validate(payload)


async def test_an_accepted_finding_the_plan_ignores_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 risk: the rewriter must not receive a half-applied decision.

    Every accepted finding is addressed by a change or explained in a
    reconciliation. Silently dropping one would lose the author's decision with
    nothing to show it ever happened.
    """
    payload = golden_plan(
        changes=[golden_plan()["changes"][0]],
        reconciliations=[golden_plan()["reconciliations"][1]],
    )

    with pytest.raises(PlanContractError, match="i2"):
        await plan(db_session, snapshot_store, payload)


async def test_a_plan_may_not_change_a_claim_it_promised_to_preserve(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A claim listed as unchangeable cannot also be the subject of a change."""
    payload = golden_plan(claims_that_must_not_change=["c1", "c4", "c99"])

    with pytest.raises(PlanContractError, match="c99"):
        await plan(db_session, snapshot_store, payload)


async def test_the_plan_is_its_own_artefact_and_waits_for_approval(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 §9: a separate immutable artefact; the author approves it."""
    drafted, result = await plan(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    (snapshot,) = [s for s in result.outputs if s.artifact_type is ArtifactType.REVISION_PLAN]
    stored = json.loads(snapshot_store.read(snapshot).decode("utf-8"))
    assert RevisionPlanDocument.model_validate(stored) == result.value.plan

    row = result.value.row
    assert row.review_id == result.value.review_id
    assert row.snapshot_id == snapshot.id
    assert row.created_by_execution_id == execution.id
    # The review it was planned from is the recorded input.
    assert [artifact.role for artifact in execution.inputs] == ["review"]

    # Planning does not move the run: approving the plan is the author's act.
    before = drafted.context.engine.state
    approve_revision_plan(drafted.context, plan=result.value, approved_by=AUTHOR)
    after = drafted.context.engine.state

    assert before is WorkflowState.REVISION_PLAN_REQUIRED
    assert after is WorkflowState.SUBSTANTIVE_REWRITING
    interventions = [
        row
        for stage in drafted.context.engine.run.stage_executions
        for row in stage.user_interventions
    ]
    assert InterventionType.APPROVAL in [row.intervention_type for row in interventions]


async def test_a_plan_that_reopens_the_brief_says_so_in_its_record(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Some feedback is not a rewrite: it says the brief was wrong (plan/05 edge)."""
    payload = golden_plan(reopen_brief=True)

    _, result = await plan(db_session, snapshot_store, payload)
    execution = result.execution

    assert execution is not None
    assert result.value.plan.reopen_brief is True
    (record,) = [row for row in execution.decision_records if row.decision_type == "revision_plan"]
    assert record.inputs["reopen_brief"] is True
    assert any(event.event_type == "intervention.requested" for event in execution.trace_events)


def test_the_reconciliation_kinds_are_what_the_spec_names() -> None:
    """Combine, defer, reject: the three things to do with feedback you cannot apply."""
    assert {kind.value for kind in ReconciliationKind} == {"combined", "deferred", "rejected"}


async def test_a_claim_named_by_its_text_reads_as_one_bad_value(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """``claims_that_must_not_change`` holds ids, and the refusal has to show that.

    Observed on a real run: the planner returned twelve claim *sentences* there.
    Joined bare, they made a paragraph with commas in it that no longer read as a
    list of rejected values — the same failure mode as packing several ids into
    one ``source_ref``, and the same fix.
    """
    sentence = "The previous signal pipeline confused evidence existence with product viability."
    payload = golden_plan(claims_that_must_not_change=["c1", sentence])

    with pytest.raises(PlanContractError, match=r"claims_used"):
        await plan(db_session, snapshot_store, payload)


def test_the_plan_prompt_says_claims_are_named_by_id() -> None:
    """v1 asked for "the claims that must not change" and never said in what form.

    ``check_plan`` compares the answer against the draft's ``claims_used``, which
    holds ids — so the prompt described the concept and left the contract to be
    guessed at.
    """
    from groundscribe.paths import prompts_root
    from groundscribe.prompts.store import PromptStore

    rendered = PromptStore(prompts_root()).render(
        "create_revision_plan",
        {"accepted": [], "dismissed": [], "draft": "{}", "brief": "{}", "verdict": "revise"},
    )

    assert "claims_used" in rendered.rendered_prompt
    assert "claim id" in rendered.rendered_prompt


# ----------------------------------------------------------------------
# A plan built from a review nobody has read
# ----------------------------------------------------------------------


async def test_planning_from_an_untouched_review_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The silent failure, and the reason it was silent.

    A finding reaches a plan only once it is accepted or edited, and every finding
    arrives ``proposed``. With none decided, the accepted set is empty — and an
    empty plan satisfies ``check_plan``, because "address every accepted finding"
    is trivially true of none. The rewrite that applies nothing then satisfies
    ``check_rewrite`` for the same reason.

    So the loop ran green and handed back the article unchanged. Observed on a
    real run: review, plan and rewrite all reported success, and the new version
    was identical to its parent to the word.
    """
    with pytest.raises(PlanContractError, match="have been decided"):
        await plan(db_session, snapshot_store, accept=())


async def test_a_review_the_author_rejected_outright_may_still_be_planned(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Deciding against every finding is a decision, and a real one.

    The guard is about a review nobody has been through — not about the accepted
    set being empty, which is a legitimate outcome of reading one and disagreeing.
    """
    drafted, reviewed = await review(db_session, snapshot_store)
    ledger = open_review_ledger(drafted.context)
    for finding in reviewed.value.findings:
        ledger.reject(finding, decided_by=AUTHOR, reason="the brief asks for this")

    from groundscribe.stages.planning import check_triaged

    check_triaged(reviewed.value.findings)


def test_a_review_that_found_nothing_is_not_waiting_on_anybody() -> None:
    """No findings, nothing to decide — and nothing for the guard to object to."""
    from groundscribe.stages.planning import check_triaged

    check_triaged([])
