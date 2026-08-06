"""Cutting an unsupported claim without spending a round (IMPROVEMENTS §11).

The stage exists because a publication condition was binary while its remedy had
one size: a fabricated mechanism and a six-word rhetorical flourish failed the
article identically, and both were answered with a full substantive round. The
measured cost of that on 2026-08-06 was 31 minutes, six model calls, 315k input
tokens and ten triage decisions by hand — to arrive at a draft failing on a
sentence a person removes in four seconds.

What is under test is mostly the *guard*. The stage skips the revision plan and
skips the voice pass, and it is entitled to do both only because the correction
cannot have touched anything else. That is a property of the output here rather
than a sentence in a prompt — the model returns edits and never a body — so these
tests are largely a list of the ways a pass could try to be a rewrite instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from groundscribe.provenance.enums import ActorType
from groundscribe.stages.base import StageResult, StageRunner
from groundscribe.stages.claims import (
    CORRECT_CLAIMS_STAGE,
    CorrectClaims,
    apply_corrections,
    check_corrections,
)
from groundscribe.stages.drafting import DraftOutcome
from groundscribe.stages.errors import ClaimCorrectionError
from groundscribe.stages.schemas import ArticleDraft, ClaimsCorrected, UnresolvedMarker
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.workflow.states import WorkflowAction, WorkflowState
from test_drafting import Drafted, draft

#: A claim id in the shape a score reports one, and a span of the golden draft
#: that carries something like it. The span is quoted from the golden body so the
#: guard has something real to locate.
CLAIM = "u001_draft_beats_blank_page"
SPAN = "That number is the reason anyone would read this."


def corrections(**overrides: Any) -> dict[str, Any]:
    """A pass that cuts the one claim it was asked about."""
    return {
        "schema_version": 1,
        "corrections": [
            {
                "claim": CLAIM,
                "before": SPAN,
                "after": "",
                "reason": "The source does not support it, and the argument does not need it.",
            }
        ],
        "refused": [],
    } | overrides


async def correct(
    db_session: Session,
    snapshot_store: SnapshotStore,
    payload: dict[str, Any] | None = None,
    *,
    claims: tuple[str, ...] = (CLAIM,),
) -> tuple[Drafted, StageResult[DraftOutcome]]:
    """Draft the golden article and run a correction pass over it."""
    drafted = await draft(db_session, snapshot_store)
    drafted.context.engine.apply(WorkflowAction.ACCEPT_REVIEW)
    drafted.context.engine.apply(WorkflowAction.SUBMIT_VOICE_PASS)
    drafted.context.engine.apply(WorkflowAction.SCORE_FAILED)
    drafted.context.engine.apply(WorkflowAction.CORRECT_CLAIMS)
    drafted.model_client.script_response(
        CORRECT_CLAIMS_STAGE, payload if payload is not None else corrections()
    )
    result = await StageRunner(drafted.context).run(
        CorrectClaims(
            previous=drafted.result.value.draft,
            parent=drafted.result.value.version,
            concept=drafted.briefed.concept,
            claims=claims,
        )
    )
    return drafted, result


# ---------------------------------------------------------------------------
# What a correction does
# ---------------------------------------------------------------------------


async def test_the_passage_is_cut_and_nothing_else_moves(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The declarations are carried over because they never came back from the model."""
    drafted, result = await correct(db_session, snapshot_store)
    cut = result.value.draft
    before = drafted.result.value.draft

    assert SPAN not in cut.body
    assert len(cut.body) < len(before.body)
    assert cut.thesis == before.thesis
    assert cut.claims_used == before.claims_used
    assert cut.qualifications_applied == before.qualifications_applied
    assert cut.omitted == before.omitted


async def test_a_correction_goes_straight_back_to_scoring(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """No voice pass in between, which is most of the saving.

    Prose that only lost a clause has not been re-voiced, so there is nothing to
    realign — and the voice pass is where the measured loop lost the article,
    earning fresh `voice_adherence` deductions on every round it had been sent
    back for fidelity. The guard on what may be touched is what makes skipping it
    safe rather than merely cheap.
    """
    drafted, _ = await correct(db_session, snapshot_store)

    assert drafted.context.engine.state is WorkflowState.SCORING


async def test_it_costs_one_model_call(db_session: Session, snapshot_store: SnapshotStore) -> None:
    """The round it replaces was six: review, plan, rewrite, review, voice, score."""
    _, result = await correct(db_session, snapshot_store)

    assert len(result.invocations) == 1


async def test_what_was_cut_and_what_was_refused_is_written_down(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A refusal is the half somebody may want to disagree with."""
    await correct(
        db_session,
        snapshot_store,
        corrections(corrections=[], refused=[CLAIM]),
    )

    from groundscribe.provenance import models

    decision = (
        db_session.query(models.DecisionRecord)
        .filter(models.DecisionRecord.decision_type == "claim_correction")
        .one()
    )
    assert decision.decided_by_type is ActorType.POLICY
    assert decision.inputs["refused"] == [CLAIM]
    assert decision.outcome == "refused"


# ---------------------------------------------------------------------------
# The guard, which is the design
# ---------------------------------------------------------------------------


async def test_a_passage_the_article_does_not_contain_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """An edit that cannot be located is one nobody can check."""
    with pytest.raises(ClaimCorrectionError, match="does not contain"):
        await correct(
            db_session,
            snapshot_store,
            corrections(
                corrections=[{"claim": CLAIM, "before": "a sentence nobody wrote", "after": ""}]
            ),
        )


async def test_a_claim_the_score_did_not_fail_on_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """The stage may only touch what the score localised.

    Without this the trigger's conservatism buys nothing: the run qualifies for a
    cheap correction on the strength of one removable claim, and the pass then
    edits whatever else it fancied while it was in there.
    """
    with pytest.raises(ClaimCorrectionError, match="not one of the claims"):
        await correct(
            db_session,
            snapshot_store,
            corrections(
                corrections=[{"claim": "u999_something_else", "before": SPAN, "after": ""}]
            ),
        )


async def test_a_replacement_that_is_not_shorter_is_a_rewrite(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """Removing and qualifying both shorten. Anything else is prose being written.

    This is the check that stops "qualify it down to what the source says" being
    read as "say something else instead" — which is the exact failure that made
    `create_revision_plan` v3 necessary, one stage over: a correction that
    removed one unsupported claim by writing two.
    """
    with pytest.raises(ClaimCorrectionError, match="not shorter"):
        await correct(
            db_session,
            snapshot_store,
            corrections(
                corrections=[
                    {
                        "claim": CLAIM,
                        "before": SPAN,
                        "after": SPAN + " Probably, in most cases, for many readers.",
                    }
                ]
            ),
        )


def test_an_unresolved_marker_may_not_be_cut() -> None:
    """Deleting a marker publishes a hole as though it were an answer.

    The same rule the voice pass keeps, and it has to be restated here rather
    than inherited: a marker sitting inside a passage being cut goes with it,
    silently, and every other check passes — the span is quoted correctly, the
    claim was asked about, and the replacement is shorter. The only thing wrong
    with the edit is what it took along.

    Checked against a draft built here rather than the golden one, which carries
    no marker. Skipping when the fixture happens not to exercise a guard is how a
    guard stops being exercised.
    """
    marker = "[UNRESOLVED: what the p99 was before the change]"
    previous = ArticleDraft(
        title="A cache, and what it cost",
        thesis="Caching is a measurement problem.",
        body=f"The cache landed in March. {marker} Latency fell after it.",
        unresolved=(UnresolvedMarker(marker=marker, question="what was the p99 before?"),),
    )
    corrected = ClaimsCorrected(
        corrections=(
            {"claim": CLAIM, "before": f"March. {marker} Latency", "after": "March. Latency"},
        )
    )

    with pytest.raises(ClaimCorrectionError, match="marker"):
        check_corrections(corrected, previous, (CLAIM,))


async def test_refusing_a_claim_nobody_asked_about_is_refused(
    db_session: Session, snapshot_store: SnapshotStore
) -> None:
    """A refusal has to be about something that was asked."""
    with pytest.raises(ClaimCorrectionError, match="not one of the claims"):
        await correct(
            db_session,
            snapshot_store,
            corrections(corrections=[], refused=["u999_never_mentioned"]),
        )


def test_a_pass_that_neither_cuts_nor_refuses_is_not_an_answer() -> None:
    """The schema refuses it, so the stage never has to."""
    with pytest.raises(ValueError, match="cut something or say which claim"):
        ClaimsCorrected(corrections=(), refused=())


def test_a_passage_occurring_twice_is_cut_once() -> None:
    """Two occurrences are two passages, and the score named one.

    Replacing both would edit text nobody looked at — which is the same class of
    error as the guards above, arriving through the applier rather than through
    the model.
    """
    body = "The cache helped. The cache helped. And then it did not."
    corrected = ClaimsCorrected(
        corrections=({"claim": CLAIM, "before": "The cache helped. ", "after": ""},)
    )

    assert apply_corrections(body, corrected) == "The cache helped. And then it did not."
