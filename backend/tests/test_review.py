"""Substantive review, and findings as evidence rather than instruction (phase 07 §8).

Spec (plan/07 → *ReviewSubstantively*, *Review acceptance*, and the golden review /
severity-routing / reviewer-as-evidence tests): argument and accuracy, not sentence
polish; every issue carrying severity, category, location, passage, description,
evidence, source and brief references, a recommended correction, a suggested route,
a blocks-publication flag and the reviewer's confidence; the author free to accept,
reject or edit each finding; and accepted/rejected findings staying visible across
rounds so resolved criticism is not reintroduced without new evidence.

Two things carry this section.

**A finding is evidence, not an order.** The reviewer is a model with an opinion
and the author is the person who has to publish it. So every finding is stored with
a status the author sets, a rejection keeps the finding visible rather than
deleting it, and the *reason* is recorded — which is what makes the next round able
to tell "already argued and dismissed" from "never raised".

**Severity is routing, not decoration.** Blocking and major buy an iteration;
optional never does. A review that returned four optional findings has not asked
for a rewrite, and a system that treated it as though it had would loop forever on
suggestions nobody wanted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from golden import golden_json
from groundscribe.domain.enums import ArtifactType, FindingStatus, IssueSeverity
from groundscribe.provenance.enums import InterventionType
from groundscribe.stages.base import StageResult, StageRunner
from groundscribe.stages.review import (
    REVIEW_STAGE,
    ReviewOutcome,
    ReviewSubstantively,
    forces_iteration,
    open_review_ledger,
)
from groundscribe.stages.schemas import ReviewIssueReport, SubstantiveReview
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.policy import FailureCategory
from groundscribe.workflow.states import WorkflowState
from pipeline_helpers import AUTHOR
from test_drafting import Drafted, draft


def golden_review(**overrides: Any) -> dict[str, Any]:
    """The golden review, with one field varied per test."""
    return golden_json("review.json", suite="draft_to_voice") | overrides


async def review(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
) -> tuple[Drafted, StageResult[ReviewOutcome]]:
    """Draft the golden article, then review it."""
    drafted = await draft(db_session, snapshot_store)
    drafted.model_client.script_response(
        REVIEW_STAGE, payload if payload is not None else golden_review()
    )
    result = await StageRunner(drafted.context).run(
        ReviewSubstantively(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            source_model=drafted.briefed.source_model,
        )
    )
    return drafted, result


async def test_a_draft_reviews_into_structured_findings(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07 golden test: draft → review with correctly structured issues."""
    _, result = await review(db_session, snapshot_store)
    assessed = result.value.review

    assert isinstance(assessed, SubstantiveReview)
    assert assessed.verdict and assessed.summary
    assert len(assessed.dimensions_assessed) >= 3
    assert [issue.severity for issue in assessed.issues] == [
        IssueSeverity.BLOCKING,
        IssueSeverity.MAJOR,
        IssueSeverity.MINOR,
        IssueSeverity.OPTIONAL,
    ]

    blocking = assessed.issues[0]
    assert blocking.category and blocking.location and blocking.passage
    assert blocking.description and blocking.evidence
    assert blocking.source_ref == "c1"
    assert blocking.brief_ref
    assert blocking.recommended_correction
    assert blocking.suggested_route is FailureCategory.FACTUAL_GAP
    assert blocking.blocks_publication is True
    assert 0.0 <= blocking.reviewer_confidence <= 1.0


def test_severity_decides_whether_a_finding_buys_an_iteration() -> None:
    """plan/07 severity routing: optional never forces a full iteration."""
    assert forces_iteration(IssueSeverity.BLOCKING) is True
    assert forces_iteration(IssueSeverity.MAJOR) is True
    assert forces_iteration(IssueSeverity.MINOR) is False
    assert forces_iteration(IssueSeverity.OPTIONAL) is False

    review = SubstantiveReview.model_validate(golden_review())
    assert review.requires_iteration is True
    assert [issue.id for issue in review.iteration_forcing] == ["i1", "i2"]

    polish_only = SubstantiveReview.model_validate(
        golden_review(issues=[golden_review()["issues"][3]])
    )
    assert polish_only.requires_iteration is False


def test_a_blocking_finding_must_say_it_blocks_publication() -> None:
    """Severity and the publication flag are one judgement; disagreeing is a bug."""
    payload = golden_review()
    payload["issues"][0]["blocks_publication"] = False

    with pytest.raises(ValueError, match="blocks_publication"):
        SubstantiveReview.model_validate(payload)


def test_a_finding_must_point_at_something() -> None:
    """A criticism with nothing behind it is an opinion the author cannot weigh.

    Evidence, a source claim, or a brief clause — any one will do. A reader-fit
    finding legitimately rests on the brief rather than on the source, which is why
    the rule is "point at something" rather than "cite the source model".
    """
    payload = golden_review()
    payload["issues"][0]["evidence"] = "   "
    payload["issues"][0]["source_ref"] = ""
    payload["issues"][0]["brief_ref"] = ""

    with pytest.raises(ValueError, match="points at no evidence"):
        SubstantiveReview.model_validate(payload)


async def test_a_review_citing_a_claim_that_does_not_exist_fails_the_stage(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A finding pointing at nothing cannot be acted on or argued with."""
    from groundscribe.stages.errors import EvidenceError

    payload = golden_review()
    payload["issues"][0]["source_ref"] = "c99"

    with pytest.raises(EvidenceError, match="c99"):
        await review(db_session, snapshot_store, payload)


async def test_the_review_is_stored_with_its_findings_and_parks_for_the_author(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The review is an artefact; its findings are rows the author acts on."""
    drafted, result = await review(db_session, snapshot_store)
    execution = result.execution

    assert execution is not None
    (snapshot,) = [s for s in result.outputs if s.artifact_type is ArtifactType.REVIEW]
    stored = json.loads(snapshot_store.read(snapshot).decode("utf-8"))
    assert SubstantiveReview.model_validate(stored) == result.value.review

    row = result.value.row
    assert row.article_version_id == drafted.result.value.version.id
    assert row.verdict == result.value.review.verdict
    assert row.round == 0
    assert [issue.ref for issue in result.value.findings] == ["i1", "i2", "i3", "i4"]
    assert all(finding.status is FindingStatus.PROPOSED for finding in result.value.findings)
    assert result.value.findings[0].fingerprint

    # A blocking finding needs a plan; the run parks for the author to make one.
    assert drafted.context.engine.state is WorkflowState.REVISION_PLAN_REQUIRED


async def test_a_polish_only_review_is_accepted_rather_than_replanned(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Nothing that forces an iteration means the substance is settled (plan/05 edge)."""
    payload = golden_review(
        issues=[golden_json("review.json", suite="draft_to_voice")["issues"][3]]
    )

    drafted, _ = await review(db_session, snapshot_store, payload)

    assert drafted.context.engine.state is WorkflowState.VOICE_ALIGNING


async def test_the_author_accepts_rejects_and_edits_individual_findings(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07: reviewer output is evidence, not an unquestionable instruction."""
    drafted, result = await review(db_session, snapshot_store)
    ledger = open_review_ledger(drafted.context)
    blocking, major, minor, optional = result.value.findings

    ledger.accept(blocking, decided_by=AUTHOR)
    ledger.reject(major, decided_by=AUTHOR, reason="The follow-up article was cancelled.")
    ledger.edit(
        minor,
        decided_by=AUTHOR,
        recommended_correction="Name the deploy window; the duration is not the point.",
        reason="The correction is right but the emphasis is wrong.",
    )

    assert blocking.status is FindingStatus.ACCEPTED
    assert major.status is FindingStatus.REJECTED
    assert major.decision_reason == "The follow-up article was cancelled."
    assert minor.status is FindingStatus.EDITED
    assert minor.recommended_correction.startswith("Name the deploy window")
    assert optional.status is FindingStatus.PROPOSED
    assert all(finding.decided_by == AUTHOR for finding in (blocking, major, minor))

    interventions = ledger.execution.user_interventions
    assert [row.intervention_type for row in interventions] == [
        InterventionType.APPROVAL,
        InterventionType.REJECTION,
        InterventionType.EDIT,
    ]
    assert ledger.accepted() == (blocking, minor)


async def test_a_rejected_finding_stays_visible_and_is_not_re_raised_unchanged(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """plan/07: resolved criticism is not reintroduced without new evidence.

    Suppression rather than deletion. The reviewer is entitled to raise it again —
    it may have found something new — so the second round's finding is recorded,
    marked as previously dismissed, and kept out of the plan unless its evidence
    changed.
    """
    drafted, first = await review(db_session, snapshot_store)
    ledger = open_review_ledger(drafted.context)
    ledger.reject(first.value.findings[1], decided_by=AUTHOR, reason="The follow-up was cancelled.")

    # Round two raises the same finding, word for word.
    drafted.model_client.script_response(REVIEW_STAGE, golden_review())
    second = await StageRunner(drafted.context).run(
        ReviewSubstantively(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            source_model=drafted.briefed.source_model,
            previous_findings=first.value.findings,
            transitions=False,
        )
    )

    repeated = next(
        f for f in second.value.findings if f.fingerprint == first.value.findings[1].fingerprint
    )
    assert repeated.status is FindingStatus.SUPPRESSED
    assert repeated.decision_reason
    assert "dismissed" in repeated.decision_reason
    assert second.value.row.round == 1

    # It is still *there*: suppression is a status, not a deletion.
    assert repeated.description == first.value.findings[1].description
    assert [f.ref for f in second.value.findings] == ["i1", "i2", "i3", "i4"]


async def test_a_repeated_finding_with_new_evidence_is_raised_again(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The reviewer may reopen a dismissed point — with something new behind it."""
    drafted, first = await review(db_session, snapshot_store)
    ledger = open_review_ledger(drafted.context)
    ledger.reject(first.value.findings[1], decided_by=AUTHOR, reason="Out of scope.")

    payload = golden_review()
    payload["issues"][1]["evidence"] = (
        "The follow-up article was cancelled, so nothing reserves it."
    )
    drafted.model_client.script_response(REVIEW_STAGE, payload)
    second = await StageRunner(drafted.context).run(
        ReviewSubstantively(
            draft=drafted.result.value.draft,
            version=drafted.result.value.version,
            version_snapshot=drafted.result.outputs[0],
            brief=drafted.briefed.brief,
            source_model=drafted.briefed.source_model,
            previous_findings=first.value.findings,
            transitions=False,
        )
    )

    reopened = second.value.findings[1]
    assert reopened.status is FindingStatus.PROPOSED
    assert reopened.fingerprint != first.value.findings[1].fingerprint


def test_the_finding_statuses_are_exactly_what_the_workflow_needs() -> None:
    """Five states, each one a different thing to do with a criticism."""
    assert {status.value for status in FindingStatus} == {
        "proposed",
        "accepted",
        "rejected",
        "edited",
        "suppressed",
    }


def test_a_report_summarises_what_the_author_decided() -> None:
    """The plan is built from accepted findings; the report is how it finds them."""
    report = ReviewIssueReport(accepted=("i1",), rejected=("i2",), edited=("i3",), suppressed=())

    assert report.actionable == ("i1", "i3")
    assert report.dismissed == ("i2",)
